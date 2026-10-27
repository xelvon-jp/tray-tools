# filer_phone.py
# あっふ からスマホへファイルを渡す(PC→スマホの一方通行、QR コード経由)。
#
# 仕掛け: ダイアログを開いている間だけ、この PC で小さな HTTP サーバを立て、
# 選んだファイル専用の URL(推測できない合言葉=トークン入り)を QR コードで
# 見せる。スマホのカメラで読めば、同じ Wi-Fi の中だけでダウンロードできる。
# インターネットは通らない。ダイアログを閉じればサーバは止まり、URL は
# 無効になる。渡せるのは選んだファイルだけ(PC の中を覗く口は無い)。
#
# 有効なのは「環境1」の PC だけ(settings.json の environment == 1。
# setup.py の環境選択で決める)。あっふ 側は filer.py が environment を見て、
# 有効なときだけ複写メニューの項目を出す。既定は無効で、項目も出ない。
#
# 画像はダウンロード用ページにそのまま表示する(iPhone では長押しで
# 「写真に追加」できる)。ダウンロード用のリンクも併記する。
import html
import mimetypes
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
AUTO_CLOSE_SECONDS = 15 * 60   # 開きっぱなしの保険(ダイアログが自動で閉じる)


def is_image(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTS


def lan_ip() -> str:
    """この PC の LAN 側 IP。UDP の宛先を決めるだけで実トラフィックは出ない。
    取れなければ 127.0.0.1(スマホからは届かないので、画面で注意を出す)。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _size_text(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


class ShareServer:
    """選んだファイルだけを、合言葉入りの URL で配る小さなサーバ。

    イベント(表示・ダウンロード)は self.events に溜まる。画面側が
    タイマーで覗いて表示を更新する(サーバのスレッドから Qt を触らない)。"""

    def __init__(self, paths):
        self.files = [p for p in paths if os.path.isfile(p)]
        self.token = secrets.token_urlsafe(18)
        self.events: list = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("0.0.0.0", 0), self._make_handler())
        self.port = self._httpd.server_address[1]
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}/k/{self.token}/"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="filer-phone-share")
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def pop_events(self) -> list:
        with self._lock:
            events, self.events = self.events, []
        return events

    def _note(self, kind: str, name: str) -> None:
        with self._lock:
            self.events.append((kind, name))

    # --- 応答の中身 ---
    def _index_html(self) -> bytes:
        parts = [
            "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
            "<title>あっふ から受け取る</title>",
            "<style>body{font-family:sans-serif;margin:16px;background:#14142b;"
            "color:#eee}h1{font-size:20px}h2{font-size:16px;word-break:break-all}"
            "img{max-width:100%;height:auto;background:#fff}"
            "a{color:#8ab4ff}section{margin:0 0 28px}</style></head><body>",
            "<h1>あっふ から受け取る</h1>",
            f"<p>{len(self.files)}件のファイルです。"
            "画像は長押しすると「写真に追加」できます。</p>",
        ]
        for i, path in enumerate(self.files):
            name = html.escape(os.path.basename(path))
            size = _size_text(os.path.getsize(path))
            parts.append("<section>")
            parts.append(f"<h2>{name}</h2>")
            if is_image(path):
                parts.append(f"<img src=\"f/{i}\" alt=\"{name}\">")
            parts.append(f"<p>{size} ・ <a href=\"d/{i}\">ダウンロード</a></p>")
            parts.append("</section>")
        parts.append("</body></html>")
        return "".join(parts).encode("utf-8")

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # アクセスログは出さない
                pass

            def do_GET(self):  # noqa: N802 (http.server の流儀)
                path = self.path.split("?", 1)[0]
                seg = [s for s in path.split("/") if s]
                if len(seg) < 2 or seg[0] != "k" or seg[1] != server.token:
                    self._reply(404, b"not found", "text/plain; charset=utf-8")
                    return
                rest = seg[2:]
                if not rest:
                    self._reply(200, server._index_html(),
                                "text/html; charset=utf-8")
                    return
                if len(rest) == 2 and rest[0] in ("f", "d") and rest[1].isdigit():
                    self._serve(int(rest[1]), download=rest[0] == "d")
                    return
                self._reply(404, b"not found", "text/plain; charset=utf-8")

            def _reply(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve(self, index, download):
                if not 0 <= index < len(server.files):
                    self._reply(404, b"not found", "text/plain; charset=utf-8")
                    return
                path = server.files[index]
                name = os.path.basename(path)
                try:
                    size = os.path.getsize(path)
                    stream = open(path, "rb")
                except OSError:
                    self._reply(404, b"gone", "text/plain; charset=utf-8")
                    return
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                disposition = "attachment" if download else "inline"
                server._note("download" if download else "view", name)
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(size))
                    self.send_header(
                        "Content-Disposition",
                        f"{disposition}; filename*=UTF-8''{quote(name)}")
                    self.end_headers()
                    while True:
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # スマホ側が途中で切った
                finally:
                    stream.close()

        return Handler


# ===============================================================
# QR とダイアログ(PySide6 と segno はここだけ、遅延 import)
# ===============================================================
def qr_pixmap(text: str, target: int = 264):
    """URL の QR を QPixmap で返す。描き方は diagnostics.qr_to_qimage と同じ
    (segno の行列を白地に黒で塗る)。片方を変えたら、もう片方も見ること。"""
    import segno
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter, QPixmap

    qr = segno.make_qr(text.encode("utf-8"), error="m", mode="byte",
                       boost_error=False)
    matrix = [list(row) for row in qr.matrix]
    cells = len(matrix)
    border = 4
    scale = max(1, target // (cells + border * 2))
    size = (cells + border * 2) * scale
    image = QImage(size, size, QImage.Format_RGB32)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    try:
        black = QColor("#000000")
        for y, row in enumerate(matrix):
            for x, bit in enumerate(row):
                if bit:
                    painter.fillRect(
                        QRect((x + border) * scale, (y + border) * scale,
                              scale, scale), black)
    finally:
        painter.end()
    return QPixmap.fromImage(image)


def show_share_dialog(parent, paths) -> int:
    """QR ダイアログをモーダルで開く。開いている間だけサーバが動く。

    戻り値はダウンロードされた件数(呼び出し側のログ用)。"""
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

    server = ShareServer(paths)
    server.start()
    url = server.url
    downloads = 0
    try:
        dialog = QDialog(parent)
        dialog.setWindowTitle("スマホへ送る")
        layout = QVBoxLayout(dialog)
        intro = QLabel(f"{len(server.files)}件のファイルを送れます。\n"
                       "スマホのカメラで QR コードを読んでください"
                       "（同じ Wi-Fi にいる必要があります）。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        qr_label = QLabel()
        qr_label.setAlignment(Qt.AlignCenter)
        qr_label.setPixmap(qr_pixmap(url))
        layout.addWidget(qr_label)
        url_label = QLabel(url)
        url_label.setWordWrap(True)
        url_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(url_label)
        if url.startswith("http://127.0.0.1"):
            warn = QLabel("⚠ LAN につながっていないようです"
                          "（スマホから届かないアドレスになっています）。")
            warn.setWordWrap(True)
            layout.addWidget(warn)
        status = QLabel("まだ受け取られていません。"
                        f"この画面は {AUTO_CLOSE_SECONDS // 60} 分で自動的に閉じます。")
        status.setWordWrap(True)
        layout.addWidget(status)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)

        def poll():
            nonlocal downloads
            for kind, name in server.pop_events():
                if kind == "download":
                    downloads += 1
                    status.setText(f"受け取りました: {name}（これまで {downloads} 件）")

        timer = QTimer(dialog)
        timer.timeout.connect(poll)
        timer.start(500)
        QTimer.singleShot(AUTO_CLOSE_SECONDS * 1000, dialog.accept)
        dialog.exec()
    finally:
        server.stop()
    return downloads
