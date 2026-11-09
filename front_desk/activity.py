# -*- coding: utf-8 -*-
"""ブリッジ表示用の合図ファイル。

プレイブックの実行開始と結果を %LOCALAPPDATA%\\traytools\\desk_activity.json
へ書く。トレイ側 (claude_progress) が読んで、ブリッジに「プレイブック実行中」
と結果を出すためのもの。書き込みに失敗しても実行自体は止めない。
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


def default_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or "."
    return os.path.join(base, "traytools", "desk_activity.json")


def _write(data: dict, path: Optional[str]) -> None:
    path = path or default_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def begin(playbook_id: str, request_id: str, path: Optional[str] = None,
          now: Optional[float] = None) -> None:
    _write({"state": "running", "playbook": playbook_id,
            "request_id": request_id,
            "started_at": time.time() if now is None else now,
            "finished_at": None, "ok": None, "summary": ""}, path)


def finish(playbook_id: str, request_id: str, outcome,
           path: Optional[str] = None, started_at: Optional[float] = None,
           now: Optional[float] = None) -> None:
    _write({"state": "done", "playbook": playbook_id,
            "request_id": request_id,
            "started_at": started_at,
            "finished_at": time.time() if now is None else now,
            "ok": bool(getattr(outcome, "ok", False)),
            "summary": getattr(outcome, "summary", "") or ""}, path)
