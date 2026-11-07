"""PC 側のビューアー（iPhone をリモコンにして、受信した写真・動画を PC の画面に映す）。

iPhone のページで送ったファイルのサムネをタップ → POST /api/show → PC のブラウザで開いている
/viewer が SSE で受け取って表示を切り替える。ビューアーが開いていなければブラウザで開く。

写真アプリで1枚ずつ開く方式にしなかったのは、窓が増えたり前面に来なかったりするうえ、
動画は別アプリになって切り替えが重いため。ビューアー1枚に流し込めば、切り替えは一瞬で済む。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
import logging
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
log = logging.getLogger("landrop")

REGISTRY_SIZE = 20000  # 過去の受信フォルダも一覧するので多めに（1件数十バイト）
OPENING_GRACE_SEC = 10  # ブラウザを開いてからビューアーが繋がるまでの猶予（連打で窓が増えないように）


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    return "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "other"


@dataclass
class Shown:
    id: str
    path: Path


class ShowBoard:
    """受信ファイルに表示用の ID を振り、「いま PC に映すもの」と「どのビューアーに映すか」を持つ。

    ビューアーは常に1つだけ有効（active）。新しく開いたものが有効になり、古いものは
    「別のウィンドウで表示中」と出して止まる。止まったものはクリックで有効に戻せる。
    どこに映っているのか迷わないようにするため。
    """

    def __init__(self, open_viewer: Callable[[], None] | None = None, clock=time.monotonic) -> None:
        self._files: OrderedDict[str, Path] = OrderedDict()
        self._ids: dict[Path, str] = {}
        self._lock = threading.Lock()
        self.version = 0  # 表示内容か有効なビューアーが変わるたびに増える（SSE の配信のきっかけ）
        self.current: Shown | None = None
        self.playlist: list[str] = []  # いま映しているものと同じ一覧（iPhone 側に並んでいる順）
        self.index = 0
        # 接続中のビューアー（SSE）と、それぞれが画面に見えているか。
        # 接続しているだけでは数えない：裏に隠れたタブや最小化した窓を「開いている」扱いにすると、
        # タップしても何も出てこない（実際に起きた）。
        self._viewers: dict[str, bool] = {}  # 接続中の cid → 接続時に見えていたか（接続順）
        self._visibility: dict[str, bool] = {}  # cid → その後に報告された見え方（接続の登録より先に届くこともある）
        self.active: str | None = None
        self.closed = False  # 終了・再起動時に立てると SSE の配信ループが抜ける
        self._open_viewer = open_viewer or (lambda: None)
        self._clock = clock
        self._opened_at = float("-inf")

    def register(self, path: Path) -> str:
        """ファイルに ID を振る。同じファイルには同じ ID（フォルダを何度一覧しても増えない）。

        ID は推測できない乱数なので、サムネイルの取得（/thumb/{id}）では鍵の代わりにもなる。
        ID は一覧・アップロードの応答（トークン必須）でしか渡さない。
        """
        with self._lock:
            if (file_id := self._ids.get(path)) is not None:
                self._files.move_to_end(file_id)
                return file_id
            file_id = uuid.uuid4().hex[:12]
            self._files[file_id] = path
            self._ids[path] = file_id
            while len(self._files) > REGISTRY_SIZE:
                _, old = self._files.popitem(last=False)
                self._ids.pop(old, None)
        return file_id

    def path_of(self, file_id: str) -> Path | None:
        with self._lock:
            return self._files.get(file_id)

    def show(self, file_id: str, ids: list[str] | None = None) -> bool:
        """file_id を映す。ids は iPhone 側に並んでいる一覧（ビューアーの ←→ で前後に動くため）。"""
        path = self.path_of(file_id)
        if path is None or not path.is_file():
            return False
        with self._lock:
            ids = [i for i in (ids or []) if i in self._files]
            self.playlist = ids if file_id in ids else [file_id]
            self.index = self.playlist.index(file_id)
            self.current = Shown(file_id, path)
            self.version += 1
            need_open = False
            if not self._is_visible(self.active):
                # 有効なビューアーが見えていない。見えている別のビューアーがあればそちらに切り替え、
                # 無ければ新しく開く（開いたものは接続した時点で有効になる）
                visible = [cid for cid in self._viewers if self._is_visible(cid)]
                if visible:
                    self.active = visible[-1]
                else:
                    now = self._clock()
                    need_open = now - self._opened_at > OPENING_GRACE_SEC
                    if need_open:
                        self._opened_at = now
            shown_on = "ビューアーを開く" if need_open else ("表示" if self._is_visible(self.active) else "開くのを待機中")
        log.info("PCで表示: %s（%s。接続中のビューアー %d）", path.name, shown_on, len(self._viewers))
        if need_open:
            self._open_viewer()
        return True

    def state(self, cid: str | None = None) -> dict:
        cur = self.current
        base = {"version": self.version, "active": cid is None or cid == self.active}
        if cur is None:
            return {**base, "id": None}
        return {
            **base,
            "id": cur.id,
            "name": cur.path.name,
            "kind": kind_of(cur.path),
            "url": f"/viewer/file/{cur.id}",
            "pos": self.index + 1,
            "total": len(self.playlist),
        }

    def step(self, delta: int) -> bool:
        """ビューアーの ←→。一覧の中で前後に動く（消されたファイルは飛ばす）。端では止まる。"""
        with self._lock:
            i = self.index
            while 0 <= i + delta < len(self.playlist):
                i += delta
                path = self._files.get(self.playlist[i])
                if path is not None and path.is_file():
                    self.index = i
                    self.current = Shown(self.playlist[i], path)
                    self.version += 1
                    return True
        return False

    def delete_current(self, trash: Callable[[list[Path]], None] | None = None) -> Path | None:
        """いま映しているファイルをごみ箱へ移し、一覧の次（最後なら前）を映す。消したファイルを返す。

        HEIC から変換した JPEG なら、_heic に残している元の HEIC も一緒にごみ箱へ移す。
        """
        if trash is None:
            from .trash import to_recycle_bin as trash
        with self._lock:
            cur = self.current
        if cur is None:
            return None
        originals = [cur.path.parent / "_heic" / (cur.path.stem + ext) for ext in (".HEIC", ".heic", ".HEIF", ".heif")]
        trash([cur.path, *{p for p in originals if p.is_file()}])  # 失敗したら例外のまま（何も変えない）
        with self._lock:
            if self.current is not cur:
                return cur.path
            del self.playlist[self.index]
            self._ids.pop(cur.path, None)
            self._files.pop(cur.id, None)
            if self.playlist:
                self.index = min(self.index, len(self.playlist) - 1)
                nid = self.playlist[self.index]
                self.current = Shown(nid, self._files[nid])
            else:
                self.index = 0
                self.current = None
            self.version += 1
        log.info("ごみ箱へ移動: %s", cur.path)
        return cur.path

    def open_viewer(self) -> None:
        """ビューアーを開く（トレイのメニューから。開いているかどうかに関係なく開く）。"""
        self._opened_at = self._clock()
        self._open_viewer()

    def close(self) -> None:
        """サーバー停止前に呼ぶ。開いたままの SSE 接続を待って停止が遅れたり、打ち切りのエラーが出たりしないように。"""
        self.closed = True

    def _is_visible(self, cid: str | None) -> bool:
        if cid is None or cid not in self._viewers:
            return False
        return self._visibility.get(cid, self._viewers[cid])

    @property
    def active_visible(self) -> bool:
        with self._lock:
            return self._is_visible(self.active)

    def viewer_connected(self, cid: str, visible: bool) -> None:
        with self._lock:
            self._viewers[cid] = visible
            # 新しく開いた窓を有効にする。ただし、見えていない窓（裏のタブが再起動後に繋ぎ直してきた等）が
            # 見えている有効な窓から役目を奪わないようにする
            if visible or not self._is_visible(self.active):
                self.active = cid
                self.version += 1

    def set_visible(self, cid: str, visible: bool) -> None:
        with self._lock:
            self._visibility[cid] = visible

    def activate(self, cid: str) -> bool:
        """止まっているビューアーがクリックされた → そちらを有効にする。"""
        with self._lock:
            if cid not in self._viewers:
                return False
            self.active = cid
            self.version += 1
            return True

    def viewer_disconnected(self, cid: str) -> None:
        with self._lock:
            self._viewers.pop(cid, None)
            self._visibility.pop(cid, None)
            if self.active == cid:
                # 残っている中で、見えているもの → 一番新しいもの の順に引き継ぐ
                visible = [c for c in self._viewers if self._is_visible(c)]
                rest = visible or list(self._viewers)
                self.active = rest[-1] if rest else None
                self.version += 1


def find_edge() -> str | None:
    """msedge.exe の場所。App Paths レジストリ → 既定のインストール先の順に探す。"""
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                    path = winreg.QueryValue(key, None)
                    if path and Path(path).is_file():
                        return path
            except OSError:
                continue
    except ImportError:  # Windows 以外
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (p := Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe").is_file():
            return str(p)
    return None


def viewer_command(url: str, edge: str) -> list[str]:
    # --app でタブもアドレスバーも無い専用ウィンドウになる（タイトルとアイコンはページのもの）
    return [edge, f"--app={url}", "--window-size=1280,800"]


def browser_opener(port: int, launcher: Callable[[list[str]], object] | None = None) -> Callable[[], None]:
    """ビューアーを開く関数を返す。Edge があればアプリモードの専用ウィンドウ、無ければ既定のブラウザ。"""
    url = f"http://localhost:{port}/viewer"
    launch = launcher or (lambda cmd: subprocess.Popen(cmd, close_fds=True))

    def open_viewer() -> None:
        edge = find_edge()
        if edge:
            launch(viewer_command(url, edge))
        else:
            webbrowser.open(url)

    return open_viewer
