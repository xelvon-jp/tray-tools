"""`python -m landrop` で受信サーバーを起動する。`--tray` ならタスクトレイ常駐（pythonw 向け）。"""

from __future__ import annotations

import logging
import sys

import qrcode
import uvicorn

from . import __version__
from .config import config_path, load_settings
from .netinfo import lan_ip
from .server import create_app, setup_url


def main() -> None:
    if "--tray" in sys.argv[1:]:
        from .tray import main as tray_main

        return tray_main()

    settings = load_settings()
    url = setup_url(settings)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    print(f"\nLAN Drop {__version__}")
    print(f"  設定     : {config_path()}")
    print(f"  保存先   : {settings.save_dir}")
    print(f"  iPhoneで : 下のQRをカメラで読む（または {url} を開く）")
    print(f"  名前で届かないとき : {setup_url(settings, lan_ip())}")
    print(f"  PCで案内 : http://localhost:{settings.port}/setup  （QR・トークン・ショートカット用URL）\n")
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        pass  # cp932 のコンソールでは描けないことがある。/setup のQRを使う
    sys.stdout.flush()

    on_off = lambda b: "ON" if b else "OFF"  # noqa: E731
    print(
        f"  日付フォルダ {on_off(settings.date_folders)} / フォルダを開く {on_off(settings.open_folder)}"
        f" / 通知 {on_off(settings.notify)} / HEIC→JPEG {on_off(settings.heic_to_jpeg)}\n"
    )
    if settings.https or settings.mdns:
        print("※ https / mdns はまだ未実装です（設定は無視されます）\n")

    # ビューアーの SSE 接続が開きっぱなしなので、終了時は2秒で打ち切る
    uvicorn.run(
        create_app(settings), host=settings.host, port=settings.port, log_level="warning", timeout_graceful_shutdown=2
    )


if __name__ == "__main__":
    main()
