"""届け出と処理状態の定義。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def now_iso() -> str:
    """状態ファイルやログに書く時刻（ローカル時刻、秒まで）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class DeliveryState(str, Enum):
    """届け出の処理状態。pipeline の状態機械が遷移させる。

    REPORTED まで進めば成功側の終端。REJECTED / TEST_FAILED / CONFLICT /
    HELD / ROLLED_BACK / ROLLBACK_FAILED / FETCH_FAILED は失敗・保留側の終端。
    DEFERRED は終端ではない（ref がまだ GitHub に見えない等の一時的な待ち）。
    AWAITING_APPLY_APPROVAL も終端ではない（第1段の既定モードで、テスト合格後に
    承認シグナルを待つステージ）。
    """

    RECEIVED = "received"
    STAGED = "staged"
    TESTED = "tested"
    AWAITING_APPLY_APPROVAL = "awaiting_apply_approval"
    APPLIED = "applied"
    RESTARTED = "restarted"
    VERIFIED = "verified"
    REPORTED = "reported"
    DEFERRED = "deferred"
    REJECTED = "rejected"
    TEST_FAILED = "test_failed"
    CONFLICT = "conflict"
    HELD = "held"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    FETCH_FAILED = "fetch_failed"


TERMINAL_STATES = frozenset(
    {
        DeliveryState.REPORTED,
        DeliveryState.REJECTED,
        DeliveryState.TEST_FAILED,
        DeliveryState.CONFLICT,
        DeliveryState.HELD,
        DeliveryState.ROLLED_BACK,
        DeliveryState.ROLLBACK_FAILED,
        DeliveryState.FETCH_FAILED,
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


@dataclass(frozen=True)
class Delivery:
    """郵便受けの受け取り口から届く「変更の届け出」。

    変更そのものは GitHub へ push 済みで、届け出はその参照だけを持つ
    （仕様書 第5節フェーズ5・6：合図→納品の順）。bundle_ref は将来の
    フォールバック経路用に予約した参照で、第1増分では未対応。
    """

    id: str
    repo: str  # "tray-tools" または "xelvon-jp/tray-tools" 形式
    branch: str
    ref: str  # 反映したいコミットの SHA
    summary: str = ""
    created_at: str = ""
    paths: tuple = ()  # 変更ファイルの宣言（一次チェック用）
    bundle_ref: str | None = None

    @property
    def repo_name(self) -> str:
        return self.repo.rstrip("/").split("/")[-1]

    @classmethod
    def from_dict(cls, data: dict) -> "Delivery":
        if not isinstance(data, dict):
            raise ValueError("届け出がオブジェクトではありません")
        delivery_id = str(data.get("id") or "").strip()
        repo = str(data.get("repo") or "").strip()
        branch = str(data.get("branch") or "").strip()
        ref = str(data.get("ref") or "").strip().lower()
        bundle_ref = data.get("bundle_ref") or None
        if not delivery_id:
            raise ValueError("届け出に id がありません")
        if not repo:
            raise ValueError(f"届け出 {delivery_id} に repo がありません")
        if not branch:
            raise ValueError(f"届け出 {delivery_id} に branch がありません")
        if not ref and not bundle_ref:
            raise ValueError(f"届け出 {delivery_id} に ref がありません")
        if ref and not _SHA_RE.match(ref):
            raise ValueError(f"届け出 {delivery_id} の ref が SHA 形式ではありません: {ref}")
        paths = data.get("paths") or ()
        if isinstance(paths, str):
            paths = (paths,)
        return cls(
            id=delivery_id,
            repo=repo,
            branch=branch,
            ref=ref,
            summary=str(data.get("summary") or ""),
            created_at=str(data.get("created_at") or ""),
            paths=tuple(str(p) for p in paths),
            bundle_ref=str(bundle_ref) if bundle_ref else None,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "branch": self.branch,
            "ref": self.ref,
            "summary": self.summary,
            "created_at": self.created_at,
            "paths": list(self.paths),
            "bundle_ref": self.bundle_ref,
        }


@dataclass
class DeliveryRecord:
    """1件の届け出の処理記録（履歴つき）。"""

    delivery: Delivery
    state: DeliveryState = DeliveryState.RECEIVED
    history: list = field(default_factory=list)  # [{"state","at","note"}]
    prev_head: str | None = None  # 反映直前の実チェックアウト HEAD（ロールバック用）
    detail: str = ""

    def transition(self, state: DeliveryState, note: str = "") -> None:
        self.state = state
        self.history.append({"state": state.value, "at": now_iso(), "note": note})

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def ok(self) -> bool:
        return self.state == DeliveryState.REPORTED
