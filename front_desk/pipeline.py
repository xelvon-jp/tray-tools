"""front_desk.pipeline — 見回りと依頼の処理。

流れ (1件ずつ、同時実行はしない):
  取得 → (前回の中断分を interrupted で終端) → 種別の許可を確認 →
  running を書く → 実行 → 結果を報告 (terminal) → 処理済みに記録。

実行の二重防止は ProcessedStore が担う。結果の報告に失敗しても
処理済みには記録する (副作用のある実行をやり直さないため)。
失敗はログに残る。
"""
from __future__ import annotations

import datetime
import time
import traceback

from . import activity, model
from .config import DeskConfig
from .log import JsonlLog, LogRecord

HEARTBEAT_SEC = 60      # 合図ファイルへ heartbeat_at を書く間隔 (秒)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def build_desk_state(*, state: str, detail: str = "",
                     current_request: str = "") -> dict:
    """desk-state へ送る状態 (最新だけが残る)。"""
    return {
        "schema": 1,
        "desk": {
            "state": state,
            "detail": detail,
            "current_request": current_request,
            "updated_at": now_iso(),
        },
    }


class DeskPipeline:
    def __init__(self, config: DeskConfig, transport, executor, *,
                 log: JsonlLog, state_store, processed_store,
                 activity_path=None, heartbeat_sec=HEARTBEAT_SEC):
        self._config = config
        self._transport = transport
        self._executor = executor
        self._log = log
        self._state_store = state_store
        self._processed = processed_store
        self._reconciled = False
        self._idle_polls = 0
        self._desk_state = "idle"
        self._desk_detail = "起動しました"
        self._long_poll_wait = config.effective_long_poll_wait_sec
        self.last_fetch_error = False
        # ブリッジ表示用の合図ファイルへのハートビート。activity_path が
        # 無ければ書かない (テストや --once の単発実行では騒がない)。
        self._activity_path = activity_path
        self._heartbeat_sec = heartbeat_sec
        self._last_heartbeat = None

    def _beat(self) -> None:
        if not self._activity_path:
            return
        now = time.monotonic()
        if (self._last_heartbeat is not None
                and now - self._last_heartbeat < self._heartbeat_sec):
            return
        self._last_heartbeat = now
        activity.heartbeat(path=self._activity_path)

    # -- 状態 ------------------------------------------------------------

    def _set_state(self, state: str, detail: str = "",
                   current_request: str = "", *, post: bool = False) -> None:
        self._desk_state = state
        self._desk_detail = detail
        snapshot = build_desk_state(
            state=state, detail=detail, current_request=current_request)
        try:
            self._state_store.save(snapshot)
        except OSError:
            pass
        if post:
            self._post_state(snapshot)

    def _post_state(self, snapshot: dict = None) -> None:
        if snapshot is None:
            snapshot = build_desk_state(
                state=self._desk_state, detail=self._desk_detail)
        try:
            self._transport.post_state(snapshot)
        except Exception:
            pass  # 状態の送信失敗で本体を止めない

    # -- 前回の中断分の始末 ----------------------------------------------

    def _reconcile(self) -> None:
        if self._reconciled:
            return
        self._reconciled = True
        for rid in self._processed.unfinished_started():
            result = model.Result(
                state=model.RESULT_INTERRUPTED, ok=False, terminal=True,
                summary="前回の実行が途中で中断されました（再実行はしません）",
            )
            posted = self._transport.post_result(rid, result, now_iso())
            self._processed.mark_done(rid)
            self._log.write(LogRecord(
                at=now_iso(), event="request_interrupted",
                request_id=rid,
                detail="posted" if posted else "post_failed"))

    # -- 1件の処理 ---------------------------------------------------------

    def _handle(self, request: model.Request) -> None:
        if request.kind not in self._config.allowed_kinds:
            result = model.Result(
                state=model.RESULT_REJECTED, ok=False, terminal=True,
                summary=f"種別 {request.kind} はまだ許可されていません",
            )
            posted = self._transport.post_result(
                request.id, result, now_iso())
            self._processed.mark_done(request.id)
            self._log.write(LogRecord(
                at=now_iso(), event="request_rejected",
                request_id=request.id,
                detail=f"kind={request.kind} "
                       + ("posted" if posted else "post_failed")))
            return

        self._processed.mark_started(request.id, now_iso())
        self._set_state(
            "working", f"{request.id} を実行中です",
            current_request=request.id, post=True)
        self._transport.post_result(
            request.id,
            model.Result(state=model.RESULT_RUNNING, ok=False,
                         terminal=False, summary="実行中です"),
            now_iso())
        try:
            outcome = self._executor.run(request)
        except Exception:
            outcome = None
            tb = traceback.format_exc()
            result = model.Result(
                state=model.RESULT_FAILED, ok=False, terminal=True,
                summary="実行中に予期しない例外が発生しました",
                detail=tb[-2000:])
            posted = self._transport.post_result(
                request.id, result, now_iso())
            self._processed.mark_done(request.id)
            self._log.write(LogRecord(
                at=now_iso(), event="request_failed",
                request_id=request.id,
                detail="exception " + ("posted" if posted
                                        else "post_failed")))
            return

        if outcome.state:
            state = outcome.state
        elif outcome.timed_out:
            state = model.RESULT_BLOCKED
        elif outcome.ok:
            state = model.RESULT_DONE
        else:
            state = model.RESULT_FAILED
        result = model.Result(
            state=state, ok=outcome.ok, terminal=True,
            summary=outcome.summary, detail=outcome.detail)
        posted = self._transport.post_result(request.id, result, now_iso())
        self._processed.mark_done(request.id)
        self._log.write(LogRecord(
            at=now_iso(),
            event="request_done" if outcome.ok else "request_failed",
            request_id=request.id,
            detail=f"state={state} " + ("posted" if posted
                                         else "post_failed")))

    # -- 見回り --------------------------------------------------------------

    def poll_once(self) -> int:
        """1回の見回り。処理した件数を返す。"""
        self._beat()
        self._reconcile()
        fetched = self._transport.list_requests()
        if fetched.error:
            self.last_fetch_error = True
            self._log.write(LogRecord(
                at=now_iso(), event="poll_error", detail=fetched.error))
            self._set_state("error", f"取得に失敗しました: {fetched.error}",
                            post=True)
            return 0
        self.last_fetch_error = False
        processed = 0
        for request in fetched.requests:
            if self._processed.is_done(request.id):
                continue
            self._handle(request)
            processed += 1
        if processed:
            self._idle_polls = 0
            self._set_state(
                "idle", f"見回り完了（{processed}件処理）", post=True)
        else:
            self._idle_polls += 1
            if self._idle_polls >= max(
                    1, self._config.state_heartbeat_polls):
                self._idle_polls = 0
                self._set_state("idle", "見回り完了（0件処理）", post=True)
            else:
                self._set_state("idle", "見回り完了（0件処理）")
        return processed

    def _next_sleep(self, elapsed: float) -> int:
        """次の見回りまでの待ち (秒)。

        ロングポールが効いているときは取得自体が待つので、取得に
        時間がかかった周はすぐ次を張る。取得がすぐ返った周 (相手が
        ロングポールに応じない旧サーバ) は5秒あけて空転を避ける。
        取得が失敗した周は通常間隔に戻して休ませる。
        """
        wait = self._long_poll_wait
        if wait <= 0 or self.last_fetch_error:
            return max(1, self._config.poll_interval_sec)
        if elapsed >= wait * 0.8:
            return 1
        return 5

    def run_forever(self, sleep_fn=time.sleep, should_stop=None) -> None:
        """見回りを続ける。1回の失敗で全体を止めない。"""
        self._set_state("idle", "起動しました", post=True)
        while True:
            if should_stop is not None and should_stop():
                break
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:
                self._log.write(LogRecord(
                    at=now_iso(), event="loop_error",
                    detail=traceback.format_exc()))
                self._set_state("error", "見回りで例外が発生しました",
                                post=True)
                self.last_fetch_error = True
            sleep_fn(self._next_sleep(time.monotonic() - started))
        self._set_state("stopped", "停止しました", post=True)
