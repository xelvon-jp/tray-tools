"""配備係自身の状態 JSON と、処理済み届け出の記録。

状態 JSON は「各要素は自分の状態だけを書く」原則に従い、配備係自身の
状態と、自分が処理した届け出の状態だけを持つ。ダッシュボード／ブリッジは
これを読むだけ。書き込みは一時ファイル経由の差し替えで原子的に行う。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .model import now_iso


class StateWriter:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._daemon = {"state": "idle", "summary": ""}
        self._deliveries: dict = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._daemon = data.get("daemon", self._daemon)
                self._deliveries = data.get("deliveries", {})
            except (OSError, ValueError):
                pass  # 壊れた状態ファイルは作り直す（届け出は受け取り口に残っている）

    def set_daemon(self, state: str, summary: str = "") -> None:
        self._daemon = {"state": state, "summary": summary}
        self._flush()

    def set_delivery(
        self, delivery_id: str, state: str, summary: str = "", ref: str | None = None
    ) -> None:
        # ref は任意（状態ダイジェスト用）。渡されなければ既存の記録を保つ。
        # 古い状態ファイルに ref が無くても、そのまま読み書きできる。
        entry = {"state": state, "summary": summary, "updated_at": now_iso()}
        kept_ref = ref or (self._deliveries.get(delivery_id) or {}).get("ref")
        if kept_ref:
            entry["ref"] = kept_ref
        self._deliveries[delivery_id] = entry
        self._flush()

    def snapshot(self) -> dict:
        return {
            "updated_at": now_iso(),
            "daemon": dict(self._daemon),
            "deliveries": dict(self._deliveries),
        }

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self.path)


class SeenStore:
    """処理済み届け出 ID の記録（重複排除、仕様書 A5）。

    終端状態になった ID と、次回送り（DEFERRED）の試行回数を持つ。
    配備係が落ちても届け出は受け取り口に残るので、ここの記録は
    「同じ届け出を二度がけしない」ための補助に徹する。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._terminal: dict = {}
        self._attempts: dict = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._terminal = data.get("terminal", {})
                self._attempts = data.get("attempts", {})
            except (OSError, ValueError):
                pass

    def is_terminal(self, delivery_id: str) -> bool:
        return delivery_id in self._terminal

    def terminal_state(self, delivery_id: str) -> str | None:
        return self._terminal.get(delivery_id)

    def mark_terminal(self, delivery_id: str, state: str) -> None:
        self._terminal[delivery_id] = state
        self._attempts.pop(delivery_id, None)
        self._flush()

    def attempts(self, delivery_id: str) -> int:
        return int(self._attempts.get(delivery_id, 0))

    def bump_attempt(self, delivery_id: str) -> int:
        count = self.attempts(delivery_id) + 1
        self._attempts[delivery_id] = count
        self._flush()
        return count

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"terminal": self._terminal, "attempts": self._attempts},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
