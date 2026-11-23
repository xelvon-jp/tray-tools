# -*- coding: utf-8 -*-
"""AI対話モードの診断ログ（Qt/COM 非依存）。データ置き場の dialogue.log（UTF-8、小さくローテーション）。

目的: 実機で「ヘッドセットのボタンが効かない」のようなときに、どこまで届いているかを後から読めるようにする
（独占の登録結果・押下の受信・確定の呼び出しと結果・ワーカー起動・配達結果の要約）。
**個人の発話内容は書かない**（長さ・件数・状態だけ）。書けなくても本体は止めない（例外は握りつぶす）。
"""
from __future__ import annotations

import logging
import logging.handlers
import threading
import time

from paths import data_dir

_LOCK = threading.Lock()
_LOGGER: logging.Logger | None = None
MAX_BYTES = 128 * 1024
BACKUPS = 1


def _logger() -> logging.Logger | None:
    global _LOGGER
    with _LOCK:
        if _LOGGER is None:
            lg = logging.getLogger("kabeuchi.dialogue.diag")
            lg.propagate = False
            lg.setLevel(logging.INFO)
            try:
                h = logging.handlers.RotatingFileHandler(data_dir() / "dialogue.log", maxBytes=MAX_BYTES,
                                                         backupCount=BACKUPS, encoding="utf-8")
                h.setFormatter(logging.Formatter("%(message)s"))
                lg.addHandler(h)
            except Exception:
                pass
            _LOGGER = lg
        return _LOGGER


def fmt_line(event: str, fields: dict, now: float | None = None) -> str:
    """1行の形（テストしやすいよう分けてある）: `2026-11-22 14:32:05.123 event k=v k=v`。"""
    t = time.time() if now is None else now
    ms = int((t - int(t)) * 1000)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{ms:03d}"
    parts = []
    for k, v in fields.items():
        s = str(v).replace("\r", " ").replace("\n", " ")
        parts.append(f"{k}={s}")
    return f"{stamp} {event}" + (" " + " ".join(parts) if parts else "")


def dlog(event: str, **fields) -> None:
    """診断ログへ1行。失敗しても何も起きない。発話の内容は渡さないこと（長さ・件数・状態だけ）。"""
    try:
        lg = _logger()
        if lg is not None:
            lg.info(fmt_line(event, fields))
    except Exception:
        pass


def log_path():
    """診断ログの場所（⚙メニューなどで案内する用）。"""
    return data_dir() / "dialogue.log"
