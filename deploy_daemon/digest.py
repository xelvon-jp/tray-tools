"""状態ダイジェスト（仕様書 第12節：Muse が PC 側の状態を読めること）。

配備係は見回りの節目で、自分の状態（届け出ごとの処理状態、反映対象の
HEAD、自分の状態）を1つの JSON にまとめ、受け取り口へ POST する。
読み手（Muse）は Worker 側の読み取り専用エンドポイントからそれを
読む。Worker 側の実装は collab-workspace の
docs/status-digest/worker-patch/（status_digest.js）。

ダイジェスト送信は本業（見回り）を妨げない：失敗しても例外を投げず、
次の見回りで送り直す。
"""
from __future__ import annotations

import json

from .intake import AUTH_HEADER
from .model import TERMINAL_STATES, DeliveryState

DIGEST_SCHEMA = 1
DIGEST_MAX_DELIVERIES = 20

_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_STATES)


def _ok_for(state) -> bool | None:
    """状態文字列から「終端として成功したか」を引く。

    reported が成功側の終端。それ以外の終端は失敗・保留側なので False。
    終端でない（処理中・不明な）状態は None（まだ分からない）。
    """
    if state == DeliveryState.REPORTED.value:
        return True
    if state in _TERMINAL_VALUES:
        return False
    return None


def _checkout_head(config, git):
    checkout = config.checkout_dir
    if checkout is None:
        return None
    try:
        return git.head(checkout)
    except Exception:  # noqa: BLE001 - 読めなければ null（ダイジェストは止めない）
        return None


def build_digest(config, store, git) -> dict:
    """状態ストアと実チェックアウトからダイジェストを組み立てる。

    store は StateWriter（snapshot() が状態 JSON の形を返す）。
    届け出は updated_at の昇順（最も新しいものが末尾）、直近
    DIGEST_MAX_DELIVERIES 件まで。ref は状態ストアに記録がある場合のみ
    入り、無ければ null。
    """
    snap = store.snapshot()
    daemon = snap.get("daemon") or {}
    raw = snap.get("deliveries") or {}
    entries = []
    for delivery_id, info in raw.items():
        info = info or {}
        state = info.get("state")
        entries.append(
            {
                "id": delivery_id,
                "state": state,
                "ok": _ok_for(state),
                "summary": info.get("summary", ""),
                "ref": info.get("ref"),
                "updated_at": info.get("updated_at"),
            }
        )
    entries.sort(key=lambda e: e.get("updated_at") or "")
    entries = entries[-DIGEST_MAX_DELIVERIES:]
    return {
        "schema": DIGEST_SCHEMA,
        "daemon": {
            "state": daemon.get("state"),
            "apply_mode": config.apply_mode,
            "checkout_dir": (
                str(config.checkout_dir) if config.checkout_dir is not None else None
            ),
            "detail": daemon.get("summary"),
            "updated_at": snap.get("updated_at"),
        },
        "checkout_head": _checkout_head(config, git),
        "deliveries": entries,
    }


class DigestPoster:
    """ダイジェストの送信役。

    届け出を処理した見回りでは毎回送る。処理が無い見回りでは、
    生存を知らせるハートビートとして digest_heartbeat_polls 回の
    見回りに1回送る。送信に成功したときだけ数え直し、失敗したら
    間隔をリセットしない（次の見回りで送り直す）。
    """

    def __init__(self, config, transport, store, git):
        self.config = config
        self.transport = transport
        self.store = store
        self.git = git
        self._polls_since_post = 0

    def maybe_post(self, processed_any: bool) -> bool:
        """送るべきタイミングなら送る。送って成功したら True。

        失敗は握りつぶして False を返す（例外を投げない）。
        """
        cfg = self.config
        if not cfg.digest_enabled:
            return False
        self._polls_since_post += 1
        due = processed_any or self._polls_since_post >= cfg.digest_heartbeat_polls
        if not due:
            return False
        return self._send()

    def post_now(self) -> bool:
        """タイミングに関係なく1回送る（長い処理の途中経過用）。

        見回りの途中から呼ばれる前提で、例外は投げない。
        """
        if not self.config.digest_enabled:
            return False
        return self._send()

    def _send(self) -> bool:
        cfg = self.config
        try:
            digest = build_digest(cfg, self.store, self.git)
            body = json.dumps(digest, ensure_ascii=False).encode("utf-8")
            status, _ = self.transport.request(
                "POST",
                f"{cfg.intake_url.rstrip('/')}/daemon-state",
                {AUTH_HEADER: cfg.intake_key, "Content-Type": "application/json"},
                body,
            )
        except Exception:  # noqa: BLE001 - 送信失敗で見回りを止めない
            return False
        if status != 200:
            return False
        self._polls_since_post = 0
        return True
