"""front_desk.statefile — 状態と処理済み記録の原子的書き込み。"""
from __future__ import annotations

import json
import os
import tempfile
import threading


def _atomic_write_json(path: str, data) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DeskStateStore:
    """窓口自身の状態 (ダッシュボードと desk-state の元)。"""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def save(self, state: dict) -> None:
        with self._lock:
            _atomic_write_json(self._path, state)

    def load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


class ProcessedStore:
    """処理済み (done) と実行開始済み (started) の依頼 id の記録。

    started にあって done に無い id は「前回の実行が途中で止まった」
    ことを意味し、再起動時に interrupted として終端させる。
    """

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._done: list = []
        self._started: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            done = data.get("done")
            if isinstance(done, list):
                self._done = [d for d in done if isinstance(d, str)]
            started = data.get("started")
            if isinstance(started, dict):
                self._started = {
                    k: v for k, v in started.items()
                    if isinstance(k, str) and isinstance(v, str)
                }

    def _save(self) -> None:
        _atomic_write_json(self._path, {
            "done": self._done,
            "started": self._started,
        })

    def is_done(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._done

    def mark_started(self, request_id: str, at: str) -> None:
        with self._lock:
            if request_id not in self._started:
                self._started[request_id] = at
                self._save()

    def mark_done(self, request_id: str) -> None:
        with self._lock:
            self._started.pop(request_id, None)
            if request_id not in self._done:
                self._done.append(request_id)
            self._save()

    def unfinished_started(self) -> list:
        """started のまま done になっていない id の一覧。"""
        with self._lock:
            return sorted(
                rid for rid in self._started if rid not in self._done)
