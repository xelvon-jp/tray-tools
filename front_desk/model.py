"""front_desk.model — 依頼と結果のデータ型 (Worker 契約と同形)。"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

# Worker 側 (requests.js) の許可値と揃える。
REQUEST_TIERS = ("haiku", "opus")
REQUEST_KINDS = ("inspect", "ops", "general")

# 結果の state (窓口が書く値)。
RESULT_RUNNING = "running"
RESULT_DONE = "done"
RESULT_FAILED = "failed"
RESULT_BLOCKED = "blocked"
RESULT_REJECTED = "rejected"
RESULT_INTERRUPTED = "interrupted"


@dataclasses.dataclass(frozen=True)
class Request:
    """Worker から取得した依頼1件。"""

    id: str
    tier: str
    kind: str
    body: str
    created_at: str = ""

    @classmethod
    def from_json(cls, data: Any) -> Optional["Request"]:
        """形が合わないものは None (呼び出し側で読み捨てる)。"""
        if not isinstance(data, dict):
            return None
        rid = data.get("id")
        tier = data.get("tier")
        kind = data.get("kind")
        body = data.get("body")
        if not all(isinstance(v, str) and v for v in (rid, tier, kind, body)):
            return None
        if tier not in REQUEST_TIERS or kind not in REQUEST_KINDS:
            return None
        created = data.get("created_at")
        return cls(
            id=rid,
            tier=tier,
            kind=kind,
            body=body,
            created_at=created if isinstance(created, str) else "",
        )


@dataclasses.dataclass(frozen=True)
class Result:
    """窓口が Worker へ返す結果。terminal=True で ack (処理完了)。"""

    state: str
    ok: bool
    terminal: bool
    summary: str = ""
    detail: str = ""

    def to_json(self, at: str) -> dict:
        return {
            "state": self.state,
            "ok": bool(self.ok),
            "terminal": bool(self.terminal),
            "summary": self.summary,
            "detail": self.detail,
            "at": at,
        }
