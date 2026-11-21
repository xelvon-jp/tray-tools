# -*- coding: utf-8 -*-
"""単一起動（QLocalServer / QLocalSocket）。

最初のプロセスがサーバーを立てて待つ。2つ目は既存へ "show" を送って終了する。
既存プロセスは受け取ったら窓を表示する（前面化は自分の窓だけ）。
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SERVER_NAME = "kabeuchi_stt_v2_single_instance"


def notify_existing(name: str = SERVER_NAME, message: bytes = b"show", timeout_ms: int = 500) -> bool:
    """既存インスタンスがあれば message を送って True。なければ False。"""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(message)
    sock.flush()
    sock.waitForBytesWritten(timeout_ms)
    sock.disconnectFromServer()
    return True


class SingleInstance(QObject):
    def __init__(self, on_message: Callable[[str], None], name: str = SERVER_NAME):
        super().__init__()
        self.name = name
        self.on_message = on_message
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_conn)

    def listen(self) -> bool:
        QLocalServer.removeServer(self.name)  # 異常終了で残った名前を掃除（先に notify_existing で確認済みの前提）
        return self.server.listen(self.name)

    def _on_conn(self) -> None:
        try:
            sock = self.server.nextPendingConnection()
            if sock is None:
                return
            sock.waitForReadyRead(300)
            msg = bytes(sock.readAll()).decode("utf-8", "replace") or "show"
            sock.disconnectFromServer()
            self.on_message(msg)
        except Exception:
            pass
