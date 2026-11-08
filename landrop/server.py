"""FastAPI アプリ本体。"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import secrets
import socket
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import qrcode
import qrcode.image.svg
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .hooks import HookRegistry, ReceivedFile, build_hooks
from .netinfo import lan_ip
from .storage import FileTooLarge, PartFile, SessionDirs, reuse_same_file, safe_filename, save_fileobj
from .texts import MAX_TEXT_BYTES, ReceivedText, TextHub, build_texts
from .gallery import Thumbnails, list_folders, list_media, recent_entries, recent_media, resolve_folder
from .viewer import ShowBoard, browser_opener, kind_of

STATIC_DIR = Path(__file__).parent / "static"
TOKEN_HEADER = "X-LanDrop-Token"
RECENT_STATE_FILE = ".landrop_recent.json"  # 直近の受信の記録（保存先に隠しファイルで置く）


def advertised_host(settings: Settings) -> str:
    """QR・案内に載せるPCのアドレス。

    既定は `PC名.local`。Windows 10 以降は自分のPC名への mDNS 問い合わせに標準で応答するので、
    DHCP で IP が変わってもホーム画面のアイコンやショートカットがそのまま使える。
    advertise_host = "ip" なら現在のIP、それ以外の値ならその文字列をそのまま使う。
    """
    if settings.advertise_host == "ip":
        return lan_ip()
    return settings.advertise_host or f"{socket.gethostname()}.local"


def setup_url(settings: Settings, host: str | None = None) -> str:
    """iPhone で開くURL。トークンは #フラグメントに載せる（サーバーへは送られず、ログにも残らない）。"""
    scheme = "http"  # HTTPS対応時に settings.https で切り替える
    host = host or advertised_host(settings)
    return f"{scheme}://{host}:{settings.port}/#t={settings.token}"


def create_app(
    settings: Settings,
    hooks: HookRegistry | None = None,
    texts: TextHub | None = None,
    board: ShowBoard | None = None,
) -> FastAPI:
    hooks = hooks or build_hooks(settings)
    texts = texts or build_texts(settings)
    board = board or ShowBoard(browser_opener(settings.port))

    app = FastAPI(title="LAN Drop", version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.hooks = hooks
    app.state.texts = texts
    app.state.board = board
    sessions = SessionDirs(
        settings.save_dir,
        settings.session_gap_sec,
        settings.date_folders,
        state_file=settings.save_dir / RECENT_STATE_FILE if settings.date_folders else None,
    )
    app.state.sessions = sessions

    # 認証・サイズ上限・受信セッションの開始は本文を読む前に判定したいので、エンドポイントの
    # 依存関係ではなく ASGI ミドルウェアでやる（FastAPI はフォームを先にパースしてから依存関係を解決するため）
    app.add_middleware(
        GuardMiddleware, token=settings.token, max_body=settings.max_file_bytes + 1024 * 1024, sessions=sessions
    )

    async def finish(saved: list[ReceivedFile]) -> JSONResponse:
        for it in saved:
            it.path, it.duplicate = await run_in_threadpool(reuse_same_file, it.path)
        # 既にあったファイルは変換などを済ませてあるので、新しく保存したものだけ通す
        saved = [it if it.duplicate else await run_in_threadpool(hooks.apply_transforms, it) for it in saved]
        batch = await run_in_threadpool(sessions.add, [it.path for it in saved])
        for it in saved:
            it.batch = batch
        # id は iPhone のページから「PC で表示」するときに使う
        body = {"saved": [
            {"name": it.path.name, "size": it.size, "id": board.register(it.path), "duplicate": it.duplicate}
            for it in saved
        ]}
        return JSONResponse(body, background=BackgroundTask(hooks.notify, saved))

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "?"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    # 同梱のライブラリ（SweetAlert2 など）。ネットに繋がっていなくても動くよう CDN は使わない
    app.mount("/vendor", StaticFiles(directory=STATIC_DIR / "vendor"), name="vendor")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        # ビューアーの専用ウィンドウ（Edge のアプリモード）はこのアイコンをタイトルバーとタスクバーに出す
        return FileResponse(STATIC_DIR / "icon.ico", media_type="image/x-icon")

    @app.get("/api/ping")
    def ping() -> dict:
        return {"app": "lan-drop", "version": __version__, "host": socket.gethostname()}

    @app.post("/api/upload")
    async def upload(request: Request, files: list[UploadFile] = File(...)) -> JSONResponse:
        """multipart/form-data。フィールド名 `files`（複数可）。Safari のページとショートカットの「フォーム」用。"""
        saved: list[ReceivedFile] = []
        for uf in files:
            name = safe_filename(uf.filename, fallback=default_name(uf.content_type))
            try:
                path, size = await run_in_threadpool(
                    save_fileobj, uf.file, sessions.current, name, settings.max_file_bytes
                )
            except FileTooLarge:
                raise HTTPException(status_code=413, detail=f"{name} が {settings.max_file_mb} MB を超えています")
            saved.append(ReceivedFile(path, uf.filename or name, size, client_of(request)))
        return await finish(saved)

    @app.post("/api/upload/raw")
    async def upload_raw(request: Request, filename: str = "") -> JSONResponse:
        """本文がファイルそのもの（1ファイル）。ショートカットの「ファイル」本文用。

        ファイル名は ?filename= か X-Filename ヘッダー（URLエンコード可）。無ければ日時から作る。
        """
        original = filename or unquote(request.headers.get("X-Filename", ""))
        name = safe_filename(original, fallback=default_name(request.headers.get("content-type")))
        pf = await run_in_threadpool(PartFile, sessions.current, name, settings.max_file_bytes)
        try:
            async for chunk in request.stream():
                pf.write(chunk)
            if pf.size == 0:
                raise HTTPException(status_code=400, detail="本文が空です")
            path = pf.commit()
        except FileTooLarge:
            pf.discard()
            raise HTTPException(status_code=413, detail=f"{settings.max_file_mb} MB を超えています")
        except BaseException:
            pf.discard()
            raise
        return await finish([ReceivedFile(path, original or name, pf.size, client_of(request))])

    @app.post("/api/text")
    async def post_text(request: Request) -> dict:
        """iPhone → PC のテキスト。JSON {"text": "..."} か、本文がそのままテキスト（text/plain）。"""
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_TEXT_BYTES:
                raise HTTPException(status_code=413, detail="テキストが長すぎます")
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                text = json.loads(body).get("text", "")
            except (ValueError, AttributeError):
                raise HTTPException(status_code=400, detail="JSON の形式が違います")
        else:
            text = body.decode("utf-8", errors="replace")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="テキストが空です")
        await run_in_threadpool(texts.receive, ReceivedText(text, client_of(request)))
        return {"ok": True, "length": len(text), "copied": settings.text_to_clipboard, "shown": settings.text_popup}

    @app.get("/api/text/pc")
    async def get_pc_text() -> dict:
        """PC → iPhone のテキスト。PC のクリップボードにある文字を返す。"""
        if not settings.share_pc_clipboard:
            raise HTTPException(status_code=403, detail="PCのクリップボード共有は無効です（share_pc_clipboard）")
        from .clipboard import get_text

        return {"text": await run_in_threadpool(get_text)}

    thumbs = Thumbnails()

    @app.get("/api/folders")
    async def folders() -> dict:
        """「PCで表示」の候補：直近・一つ前の受信と、受信フォルダ（日付ごと、新しい順）。"""
        return {
            "recent": await run_in_threadpool(recent_entries, sessions),
            "folders": await run_in_threadpool(list_folders, settings.save_dir),
        }

    @app.get("/api/folder")
    async def folder_items(name: str = "", recent: int | None = None) -> dict:
        """受信フォルダ（name）か直近の受信（recent=0 が直近、1 が一つ前）の写真・動画。
        id は /api/show と /thumb/{id} に使う。"""
        if recent is not None:
            paths = await run_in_threadpool(recent_media, sessions, recent)
            if paths is None:
                raise HTTPException(status_code=404, detail="その回の受信の記録がありません")
        else:
            folder = resolve_folder(settings.save_dir, name)
            if folder is None:
                raise HTTPException(status_code=404, detail="フォルダが見つかりません")
            paths = await run_in_threadpool(list_media, folder)
        return {"items": [{"id": board.register(p), "name": p.name, "kind": kind_of(p)} for p in paths]}

    @app.get("/thumb/{file_id}", include_in_schema=False)
    async def thumb(file_id: str) -> Response:
        """サムネイル。<img src> はヘッダーを付けられないので、トークンの代わりに推測できない id で守る。"""
        path = board.path_of(file_id)
        if path is None or kind_of(path) != "image" or not path.is_file():
            raise HTTPException(status_code=404, detail="サムネイルがありません")
        try:
            data = await run_in_threadpool(thumbs.get, path)
        except OSError:
            raise HTTPException(status_code=404, detail="画像を読めませんでした")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/show")
    async def show(request: Request) -> dict:
        """受信したファイルを PC のビューアーに映す。JSON {"id": "...", "ids": [並んでいる一覧の id]}。"""
        try:
            data = await request.json()
            file_id = str(data["id"])
            ids = [str(i) for i in data.get("ids") or []]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(status_code=400, detail="JSON の形式が違います")
        if not await run_in_threadpool(board.show, file_id, ids):
            raise HTTPException(status_code=404, detail="PC側にそのファイルがありません（削除した？）")
        return {"ok": True, "viewers": int(board.active_visible)}

    def require_local(request: Request) -> None:
        if client_of(request) not in ("127.0.0.1", "::1"):
            raise HTTPException(status_code=403, detail="このページはPC本体からのみ開けます")

    @app.get("/viewer", include_in_schema=False)
    def viewer_page(request: Request) -> FileResponse:
        require_local(request)
        return FileResponse(STATIC_DIR / "viewer.html", headers={"Cache-Control": "no-cache"})

    @app.get("/viewer/events", include_in_schema=False)
    async def viewer_events(request: Request, cid: str = "", visible: int = 1) -> StreamingResponse:
        """ビューアーへの通知（Server-Sent Events）。cid はビューアーごとの ID、visible は接続時に画面に見えているか。"""
        require_local(request)
        cid = cid or uuid.uuid4().hex

        async def stream():
            board.viewer_connected(cid, bool(visible))
            try:
                last, idle = -1, 0.0
                while not board.closed:
                    if board.version != last:
                        last = board.version
                        yield f"data: {json.dumps(board.state(cid), ensure_ascii=False)}\n\n"
                        idle = 0.0
                    elif idle >= 15:
                        yield ": keep-alive\n\n"  # 途中の機器に切られないように
                        idle = 0.0
                    await asyncio.sleep(0.2)
                    idle += 0.2
            finally:
                board.viewer_disconnected(cid)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    @app.post("/viewer/visibility", include_in_schema=False)
    def viewer_visibility(request: Request, cid: str, visible: int) -> dict:
        """ビューアーが裏に隠れた／前に出た（visibilitychange）。"""
        require_local(request)
        board.set_visible(cid, bool(visible))
        return {"ok": True}

    @app.post("/viewer/step", include_in_schema=False)
    def viewer_step(request: Request, delta: int) -> dict:
        """ビューアーの ←→ キー。"""
        require_local(request)
        return {"ok": board.step(max(-1, min(1, delta)))}

    @app.post("/viewer/delete", include_in_schema=False)
    async def viewer_delete(request: Request, id: str) -> dict:
        """ビューアーの Delete キー（確認のあと）。いま映しているファイルをごみ箱へ。
        id は確認画面を出した時点のもの。その間に表示が変わっていたら消さない。"""
        require_local(request)
        cur = board.current
        if cur is None or cur.id != id:
            raise HTTPException(status_code=409, detail="表示が切り替わったので削除しませんでした")
        from .trash import TrashError

        try:
            path = await run_in_threadpool(board.delete_current)
        except TrashError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True, "deleted": path.name if path else None}

    @app.post("/viewer/activate", include_in_schema=False)
    def viewer_activate(request: Request, cid: str) -> dict:
        """「別のウィンドウで表示中」になっているビューアーがクリックされた。"""
        require_local(request)
        return {"ok": board.activate(cid)}

    @app.get("/viewer/file/{file_id}", include_in_schema=False)
    def viewer_file(request: Request, file_id: str) -> FileResponse:
        require_local(request)
        path = board.path_of(file_id)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="ファイルが見つかりません")
        return FileResponse(path)

    @app.get("/api/qr")
    def connect_qr() -> Response:
        """接続用の QR（SVG）。iPhone の設定画面に出して、家族の iPhone などで読み取ってもらう用。
        中身はトークン入りの URL なので、トークンを知っている端末にしか出さない（/api/ 以下は認証付き）。"""
        return Response(qr_svg(setup_url(settings)), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @app.get("/setup", include_in_schema=False)
    def setup(request: Request) -> HTMLResponse:
        """PC自身のブラウザからだけ開ける案内ページ（QRコードとトークン表示）。"""
        require_local(request)
        url = setup_url(settings)
        html = (STATIC_DIR / "setup.html").read_text(encoding="utf-8")
        html = (
            html.replace("{{QR_SVG}}", qr_svg(url))
            .replace("{{URL}}", url)
            .replace("{{IP_URL}}", setup_url(settings, lan_ip()))
            .replace("{{TOKEN}}", settings.token)
            .replace("{{BASE}}", url.split("/#")[0])
            .replace("{{SAVE_DIR}}", str(settings.save_dir))
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return app


def qr_svg(text: str) -> str:
    """QR コードの SVG（XML 宣言なし。HTML にそのまま埋め込める）。"""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=12)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]


def default_name(content_type: str | None) -> str:
    """ファイル名が来なかったときの名前（拡張子なし。Content-Type から推測できれば付ける）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    if (content_type or "").startswith("image/heic"):
        ext = ".heic"
    return f"landrop_{stamp}{ext}"


class GuardMiddleware:
    """/api/ 以下にトークン認証と Content-Length 上限をかけ、アップロードの受信セッションを数える。"""

    def __init__(self, app: ASGIApp, token: str, max_body: int, sessions: SessionDirs) -> None:
        self.app = app
        self.token = token.encode()
        self.max_body = max_body
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}

        given = headers.get(TOKEN_HEADER.lower(), "")
        auth = headers.get("authorization", "")
        if not given and auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given or not secrets.compare_digest(given.encode(), self.token):
            return await JSONResponse({"detail": "トークンが違います"}, status_code=401)(scope, receive, send)

        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > self.max_body:
            return await JSONResponse({"detail": "ファイルが大きすぎます"}, status_code=413)(scope, receive, send)

        if not scope["path"].startswith("/api/upload"):
            return await self.app(scope, receive, send)
        self.sessions.begin()
        try:
            await self.app(scope, receive, send)
        finally:
            self.sessions.end()
