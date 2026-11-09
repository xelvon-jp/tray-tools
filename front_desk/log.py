"""front_desk.log — JSONL の操作ログ (配備係と同じ作法)。"""
from __future__ import annotations

import dataclasses
import json
import os
import threading
import traceback


@dataclasses.dataclass(frozen=True)
class LogRecord:
    at: str
    event: str
    request_id: str = ""
    detail: str = ""


class JsonlLog:
    """1行1レコードで追記する。書けなくても処理は止めない。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def write(self, record: LogRecord) -> None:
        line = json.dumps(dataclasses.asdict(record), ensure_ascii=False)
        try:
            with self._lock:
                os.makedirs(os.path.dirname(self._path) or ".",
                            exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass


def format_exception(exc: BaseException) -> str:
    """traceback を1つの文字列にする (loop_error 用)。"""
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
