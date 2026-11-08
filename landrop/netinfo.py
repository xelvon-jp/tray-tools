"""PCのLAN側IPアドレスの推定。"""

from __future__ import annotations

import socket


def lan_ip() -> str:
    # UDP の connect は実際にはパケットを送らず、経路上の送信元アドレスだけ決まる
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
