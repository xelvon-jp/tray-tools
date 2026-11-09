"""front_desk.intake — Worker の依頼置き場への接続。

契約は collab-workspace の docs/front-desk/worker-patch/README.md。
窓口鍵 (x-desk-key) はコードに埋め込まず、実行時に渡す。
"""
from __future__ import annotations

import dataclasses
import json
import typing
import urllib.error
import urllib.request

from .model import Request, Result

# Cloudflare は Python 既定の UA を弾くため、curl 相当の UA を送る
# (配備係の intake で実証済み)。
USER_AGENT = "curl/8.5.0"
DESK_KEY_HEADER = "x-desk-key"
REQUEST_TIMEOUT_SEC = 30


def build_endpoints(base_url: str) -> dict:
    """基底 URL から各エンドポイントを組み立てる (純粋関数)。"""
    base = (base_url or "").rstrip("/")
    return {
        "list": base + "/requests",
        "result": base + "/requests/{id}/result",
        "desk_state": base + "/desk-state",
    }


@dataclasses.dataclass(frozen=True)
class FetchResult:
    requests: tuple = ()  # tuple[Request, ...]
    error: str = ""


class DeskTransport(typing.Protocol):
    def list_requests(self) -> FetchResult: ...
    def post_result(self, request_id: str, result: Result, at: str) -> bool: ...
    def post_state(self, state: dict) -> bool: ...


class UrllibTransport:
    """urllib による実装。取得の失敗は例外にせず error で返す。"""

    def __init__(self, base_url: str, desk_key: str,
                 timeout_sec: int = REQUEST_TIMEOUT_SEC):
        self._endpoints = build_endpoints(base_url)
        self._key = desk_key
        self._timeout = timeout_sec

    def _headers(self) -> dict:
        return {
            DESK_KEY_HEADER: self._key,
            "User-Agent": USER_AGENT,
        }

    def list_requests(self) -> FetchResult:
        req = urllib.request.Request(
            self._endpoints["list"], headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return FetchResult(error=f"{type(exc).__name__}: {exc}")
        items = payload.get("requests") if isinstance(payload, dict) else None
        parsed = []
        if isinstance(items, list):
            for item in items:
                request = Request.from_json(item)
                if request is not None:
                    parsed.append(request)
        return FetchResult(requests=tuple(parsed))

    def _post(self, url: str, payload: dict) -> bool:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={**self._headers(),
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def post_result(self, request_id: str, result: Result, at: str) -> bool:
        url = self._endpoints["result"].replace(
            "{id}", urllib.parse.quote(request_id, safe=""))
        return self._post(url, result.to_json(at))

    def post_state(self, state: dict) -> bool:
        return self._post(self._endpoints["desk_state"], state)
