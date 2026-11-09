"""タスクトレイ常駐モード（`pythonw -m landrop --tray`）。

受信サーバーを別スレッドで回し、メインスレッドで pystray のトレイアイコンを回す。
コンソールが無いのでログは landrop.log に書く。
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

from .clipboard import set_text
from .config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, Settings, config_path, load_settings, save_value
from .hooks import ReceivedFile, build_hooks
from .server import advertised_host, create_app
from .texts import ReceivedText
from .startup_link import ICON_FILE, STARTUP_LNK, icon_image, set_startup, startup_enabled, write_icon_file

log = logging.getLogger("landrop")



def log_file() -> Path:
    """既定の config.toml なら landrop.log、別の設定（開発用など）なら <設定名>.log。本番と混ざらないように。"""
    cfg = config_path().resolve()
    return PROJECT_ROOT / "landrop.log" if cfg == DEFAULT_CONFIG_PATH else cfg.with_suffix(".log")


LOG_FILE = log_file()
# ---- 常駐本体 ----

def open_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(path)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def setup_logging() -> None:
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sys.excepthook = lambda *exc: log.critical("未処理の例外", exc_info=exc)
    threading.excepthook = lambda a: log.critical("スレッドで未処理の例外", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


class TrayApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_folder: Path | None = None
        self.stopping = False

        hooks = build_hooks(settings)
        hooks.listeners.append(self._on_received)
        self.app = create_app(settings, hooks)
        self.texts = self.app.state.texts
        self.texts.listeners.append(self._on_text)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host=settings.host, port=settings.port, log_config=None, log_level="warning",
                timeout_graceful_shutdown=2,  # ビューアーの SSE 接続が開きっぱなしなので打ち切る
            )
        )
        self.address = f"{advertised_host(settings)}:{settings.port}"
        self.icon = pystray.Icon("LAN Drop", icon_image(), self._title("待機中"), menu=self._menu())

    def _title(self, state: str) -> str:
        return f"LAN Drop - {state}\n{self.address}"

    def _menu(self) -> pystray.Menu:
        Item = pystray.MenuItem
        return pystray.Menu(
            Item("受信フォルダを開く", self.open_last, default=True),  # アイコンの左クリックでもこれ
            Item("保存先を開く", lambda: open_dir(self.settings.save_dir)),
            Item("ビューアーを開く", lambda: self.app.state.board.open_viewer()),
            Item("最近のテキスト", pystray.Menu(self._recent_text_items)),
            pystray.Menu.SEPARATOR,
            Item("画像が1枚だけ届いたとき", None, enabled=False),
            self._setting_item("　クリップボードにコピー", "single_image_clipboard"),
            self._setting_item("　すぐ開く（フォルダの代わり）", "single_image_open"),
            pystray.Menu.SEPARATOR,
            Item("iPhone接続用QRを表示", lambda: webbrowser.open(f"http://localhost:{self.settings.port}/setup")),
            Item("Windowsログイン時に起動", self.toggle_startup, checked=lambda _: startup_enabled()),
            Item("ログを開く", lambda: os.startfile(LOG_FILE)),
            Item("再起動", self.restart),
            Item("終了", self.quit),
        )

    def _setting_item(self, label: str, key: str) -> pystray.MenuItem:
        """設定値の ON/OFF をチェックで切り替え、config.toml にも書き戻す。"""

        def toggle() -> None:
            value = not getattr(self.settings, key)
            setattr(self.settings, key, value)
            try:
                save_value(key, value)
            except Exception:
                log.exception("設定の保存に失敗しました: %s", key)

        return pystray.MenuItem(label, toggle, checked=lambda _: getattr(self.settings, key))

    def _recent_text_items(self):
        recent = self.texts.recent()
        if not recent:
            yield pystray.MenuItem("（まだありません）", None, enabled=False)
        def copier(text: str):
            return lambda: set_text(text)  # pystray は引数0〜2個の関数しか受け付けないのでクロージャで渡す

        for t in recent:
            yield pystray.MenuItem(f"{t.at:%H:%M}  {t.preview(30)}", copier(t.text))

    def _on_text(self, item: ReceivedText) -> None:
        self.icon.title = self._title(f"テキスト受信 {item.at:%H:%M}")
        self.icon.update_menu()

    def _on_received(self, items: list[ReceivedFile]) -> None:
        self.last_folder = items[-1].path.parent
        self.icon.title = self._title(f"最終受信 {datetime.now():%H:%M}（{len(items)}件）")

    def open_last(self) -> None:
        open_dir(self.last_folder or self.settings.save_dir)

    def toggle_startup(self) -> None:
        try:
            set_startup(not startup_enabled())
        except Exception:
            log.exception("スタートアップ登録の変更に失敗しました")
            self.icon.notify("スタートアップ登録の変更に失敗しました。ログを確認してください", "LAN Drop")

    def restart(self) -> None:
        """サーバーを止めてポートを空けてから、同じ設定で新しいプロセスを起こして自分は終わる。

        設定ファイルを手で直したときや、コードを更新したときの反映用。
        """
        log.info("再起動します")
        self.stopping = True
        self.app.state.board.close()
        self.server.should_exit = True
        self._server_thread.join(timeout=10)
        subprocess.Popen(
            [sys.executable, "-m", "landrop", "--tray"],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),  # LANDROP_CONFIG（開発用設定）も引き継ぐ
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        self.icon.stop()

    def quit(self) -> None:
        self.stopping = True
        self.app.state.board.close()
        self.server.should_exit = True
        self.icon.stop()

    def _serve(self) -> None:
        try:
            self.server.run()
        except BaseException:
            log.exception("受信サーバーが停止しました")
        if not self.stopping:
            # 起動直後にポートが取れなかった等。アイコンだけ残っても意味がないので終了する
            log.error("受信サーバーが予期せず終了したため、常駐を終了します")
            self.icon.stop()

    def run(self) -> None:
        self._server_thread = threading.Thread(target=self._serve, name="uvicorn", daemon=True)
        self._server_thread.start()
        log.info("常駐開始 %s 保存先=%s", self.address, self.settings.save_dir)
        self.icon.run()
        log.info("常駐終了")


def main() -> None:
    setup_logging()
    settings = load_settings()
    if port_in_use(settings.port):
        log.warning("ポート %s は使用中のため起動しません（既に起動中？）", settings.port)
        from .toast import toast

        toast("LAN Drop", f"既に起動しています（ポート {settings.port} が使用中）")
        return
    TrayApp(settings).run()
