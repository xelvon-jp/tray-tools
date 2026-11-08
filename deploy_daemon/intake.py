"""郵便受け Worker「変更の受け取り口」のクライアント。

契約（Worker パッチ側と合わせる正本は README.md）：
  GET  {intake_url}/deliveries
       -> 200 {"deliveries": [ {届け出}, ... ]}
       終端の報告が済んだ届け出は返らない。
  POST {intake_url}/deliveries/{id}/report
       body: {"state", "ok", "terminal", "summary", "detail", "at"}
       -> 200 {"ok": true}
  認証はどちらの向きもヘッダ x-deploy-key: <キー> の1本。

輸送は小さなインターフェース越しにしてあり、テストでは
インメモリの FakeTransport に差し替える。
"""
from __future__ import annotations

import json
import urllib.request

from .model import Delivery, now_iso

AUTH_HEADER = "x-deploy-key"
TERMINAL_FLAG = "terminal"


class IntakeError(RuntimeError):
    pass


class UrllibTransport:
    """実輸送（urllib のみ）。request(method, url, headers, body) -> (status, bytes)"""

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def request(self, method: str, url: str, headers: dict, body: bytes | None):
        req = urllib.request.Request(url, data=body, method=method)
        for name, value in headers.items():
            req.add_header(name, value)
        if not req.has_header("User-agent"):
            # Cloudflare は Python 既定の User-Agent を 1010 で弾く
            # （郵便受けで実測済み）。curl 相当の UA を付ける。
            req.add_header("User-Agent", "curl/8.5.0")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if hasattr(e, "read") else b""


class FakeTransport:
    """テスト用のインメモリ輸送。Worker の契約どおりに振る舞う。

    終端（terminal=true）の報告を受けた届け出は、それ以降 GET で返らない。
    """

    def __init__(self, deliveries=()):
        self._pending = [dict(d) for d in deliveries]
        self.reports: list = []  # [{"id", "body"}]
        self.fetch_count = 0
        self._finished: set = set()

    def add_delivery(self, delivery: dict) -> None:
        self._pending.append(dict(delivery))

    def request(self, method: str, url: str, headers: dict, body: bytes | None):
        if method == "GET" and url.rstrip("/").endswith("/deliveries"):
            self.fetch_count += 1
            items = [d for d in self._pending if d.get("id") not in self._finished]
            return 200, json.dumps({"deliveries": items}).encode("utf-8")
        marker = "/deliveries/"
        if method == "POST" and marker in url and url.endswith("/report"):
            delivery_id = url.split(marker, 1)[1][: -len("/report")]
            payload = json.loads(body.decode("utf-8")) if body else {}
            self.reports.append({"id": delivery_id, "body": payload})
            if payload.get(TERMINAL_FLAG):
                self._finished.add(delivery_id)
            return 200, b'{"ok": true}'
        return 404, b'{"error": "not found"}'


class IntakeClient:
    def __init__(self, base_url: str, key: str, transport=None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.transport = transport if transport is not None else UrllibTransport()

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {AUTH_HEADER: self.key}
        if extra:
            headers.update(extra)
        return headers

    def fetch_pending(self) -> list:
        status, raw = self.transport.request(
            "GET", f"{self.base_url}/deliveries", self._headers(), None
        )
        if status != 200:
            raise IntakeError(f"受け取り口の取得に失敗しました (HTTP {status})")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise IntakeError(f"受け取り口の応答が読めません: {e}") from e
        deliveries = []
        for item in data.get("deliveries", []):
            deliveries.append(Delivery.from_dict(item))
        return deliveries

    def report(
        self,
        delivery_id: str,
        state: str,
        ok: bool,
        summary: str,
        detail: str = "",
        terminal: bool = False,
    ) -> None:
        body = {
            "state": state,
            "ok": ok,
            "terminal": terminal,
            "summary": summary,
            "detail": detail,
            "at": now_iso(),
        }
        status, _ = self.transport.request(
            "POST",
            f"{self.base_url}/deliveries/{delivery_id}/report",
            self._headers({"Content-Type": "application/json"}),
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
        )
        if status != 200:
            raise IntakeError(
                f"受け取り口への報告に失敗しました (HTTP {status}, id={delivery_id})"
            )
