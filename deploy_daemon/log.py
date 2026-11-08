"""全操作の追記型 JSONL ログ。承認カードの代わりの監査証跡（仕様書 第1・7節）。"""
from __future__ import annotations

import json
from pathlib import Path

from .model import now_iso


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def log(self, event: str, **fields) -> None:
        record = {"at": now_iso(), "event": event}
        record.update(fields)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # ログが書けないこと自体で配備を止めない（状態ファイル側に痕跡は残る）
            pass
