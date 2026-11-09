# tests/test_front_desk.py
# 窓口（front_desk）の状態機械と実行まわりを確かめる。
#
# 輸送はインメモリのフェイク、実行はフェイク＋実サブプロセス
# （python 自身）で回すので、Linux でも通る。Claude Code の実起動の
# 作法は PC 側の実地確認（A7）待ちで、ここでは触らない。
#
#   python3 tests/test_front_desk.py    (Linux でも可)
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で
# 書いてある (tests/test_deploy_daemon.py と同じ形)。
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from front_desk import model
from front_desk.config import DeskConfig
from front_desk.executor import (ExecOutcome, FakeExecutor,
                                 SubprocessExecutor, summarize_output)
from front_desk.intake import FetchResult, build_endpoints
from front_desk.log import JsonlLog
from front_desk.pipeline import DeskPipeline
from front_desk.statefile import DeskStateStore, ProcessedStore


# ---- フェイク輸送 -------------------------------------------------------

class FakeTransport:
    """Worker の契約を真似るインメモリ実装。"""

    def __init__(self, requests=(), fetch_error=""):
        self._requests = list(requests)
        self._fetch_error = fetch_error
        self.results = []   # [(id, Result, at)]
        self.states = []    # [dict]

    def list_requests(self):
        if self._fetch_error:
            return FetchResult(error=self._fetch_error)
        done_ids = {rid for rid, r, _ in self.results if r.terminal}
        return FetchResult(requests=tuple(
            r for r in self._requests if r.id not in done_ids))

    def post_result(self, request_id, result, at):
        self.results.append((request_id, result, at))
        return True

    def post_state(self, state):
        self.states.append(state)
        return True


def make_request(rid="req-1", tier="haiku", kind="inspect", body="確認して"):
    return model.Request(id=rid, tier=tier, kind=kind, body=body,
                         created_at="2026-11-09T10:00:00+09:00")


def make_pipeline(tmp, transport, executor, **cfg):
    base = DeskConfig(base_dir=str(tmp), **cfg)
    return DeskPipeline(
        base, transport, executor,
        log=JsonlLog(str(tmp / "operations.jsonl")),
        state_store=DeskStateStore(str(tmp / "state.json")),
        processed_store=ProcessedStore(str(tmp / "processed.json")))


# ---- 設定と型 -------------------------------------------------------------

def test_config_defaults_stage1():
    cfg = DeskConfig()
    assert cfg.allowed_kinds == ("inspect",)
    assert cfg.poll_interval_sec == 60
    assert cfg.exec_timeout_sec == 1800


def test_config_from_json_filters():
    cfg = DeskConfig.from_json({
        "worker_base_url": "https://example.workers.dev/",
        "allowed_kinds": ["inspect", "bogus", 3],
        "executor_commands": {
            "haiku": ["claude", "-p"],
            "opus": "not-a-list",
            "bad": [],
        },
        "poll_interval_sec": True,   # bool は int として受けない
        "exec_timeout_sec": 900,
        "unknown_key": "ignored",
    })
    assert cfg.worker_base_url == "https://example.workers.dev"
    assert cfg.allowed_kinds == ("inspect",)
    assert cfg.executor_commands == {"haiku": ["claude", "-p"]}
    assert cfg.poll_interval_sec == 60
    assert cfg.exec_timeout_sec == 900


def test_request_from_json():
    ok = model.Request.from_json({
        "id": "r1", "tier": "opus", "kind": "ops", "body": "x",
        "created_at": "t"})
    assert ok is not None and ok.tier == "opus"
    assert model.Request.from_json({"id": "r1"}) is None
    assert model.Request.from_json({
        "id": "r1", "tier": "gpt", "kind": "ops", "body": "x"}) is None
    assert model.Request.from_json({
        "id": "r1", "tier": "haiku", "kind": "misc", "body": "x"}) is None
    assert model.Request.from_json("nope") is None


def test_endpoints():
    ep = build_endpoints("https://w.example/")
    assert ep["list"] == "https://w.example/requests"
    assert ep["result"] == "https://w.example/requests/{id}/result"
    assert ep["desk_state"] == "https://w.example/desk-state"


def test_summarize_output():
    summary, detail = summarize_output("line1\n\nline2 結論\n")
    assert summary == "line2 結論"
    assert detail.endswith("line2 結論")
    summary, _ = summarize_output("")
    assert summary == ""
    summary, _ = summarize_output("x" * 500)
    assert len(summary) == 300
    summary, _ = summarize_output("a\r\nb\r\n")
    assert summary == "b"


# ---- パイプライン -----------------------------------------------------------

def test_happy_path_and_no_rerun():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport([make_request()])
        executor = FakeExecutor(ExecOutcome(
            ok=True, summary="HEAD は dce669e でした", detail="detail"))
        pipe = make_pipeline(tmp, transport, executor)
        assert pipe.poll_once() == 1
        assert len(executor.calls) == 1
        states = [r.state for _, r, _ in transport.results]
        assert states == [model.RESULT_RUNNING, model.RESULT_DONE]
        final = transport.results[-1][1]
        assert final.terminal and final.ok
        assert final.summary == "HEAD は dce669e でした"
        # 2回目の見回りでは再実行しない
        assert pipe.poll_once() == 0
        assert len(executor.calls) == 1
        # 状態が desk-state へ送られている
        assert transport.states
        assert transport.states[-1]["desk"]["state"] == "idle"


def test_rejected_kind_not_executed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport([make_request(kind="ops")])
        executor = FakeExecutor()
        pipe = make_pipeline(tmp, transport, executor)
        assert pipe.poll_once() == 1
        assert executor.calls == []
        final = transport.results[-1][1]
        assert final.state == model.RESULT_REJECTED and final.terminal


def test_executor_failure_and_timeout():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport([
            make_request("req-f"), make_request("req-t")])
        executor = FakeExecutor(by_id={
            "req-f": ExecOutcome(ok=False, summary="失敗しました"),
            "req-t": ExecOutcome(ok=False, timed_out=True,
                                 summary="時間切れ"),
        })
        pipe = make_pipeline(tmp, transport, executor)
        assert pipe.poll_once() == 2
        by_id = {rid: r for rid, r, _ in transport.results if r.terminal}
        assert by_id["req-f"].state == model.RESULT_FAILED
        assert by_id["req-t"].state == model.RESULT_BLOCKED


def test_fetch_error_is_survivable():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport(fetch_error="URLError: boom")
        pipe = make_pipeline(tmp, transport, FakeExecutor())
        assert pipe.poll_once() == 0
        assert transport.states[-1]["desk"]["state"] == "error"


def test_interrupted_reconciled_on_first_poll():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 前回の実行が開始だけ記録されて終わっていない状態を作る
        store = ProcessedStore(str(tmp / "processed.json"))
        store.mark_started("req-old", "2026-11-09T09:00:00+09:00")
        transport = FakeTransport([make_request("req-old")])
        executor = FakeExecutor()
        pipe = make_pipeline(tmp, transport, executor)
        assert pipe.poll_once() == 0   # 実行はせず終端だけ
        assert executor.calls == []
        rid, result, _ = transport.results[-1]
        assert rid == "req-old"
        assert result.state == model.RESULT_INTERRUPTED and result.terminal


def test_executor_exception_becomes_failed():
    class BoomExecutor:
        def run(self, request):
            raise RuntimeError("boom")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport([make_request()])
        pipe = make_pipeline(tmp, transport, BoomExecutor())
        assert pipe.poll_once() == 1
        final = transport.results[-1][1]
        assert final.state == model.RESULT_FAILED and final.terminal
        assert "boom" in final.detail


def test_state_heartbeat_cadence():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        transport = FakeTransport([])
        pipe = make_pipeline(tmp, transport, FakeExecutor(),
                             state_heartbeat_polls=2)
        pipe.poll_once()
        assert transport.states == []          # 1回目は何もしない
        pipe.poll_once()
        assert len(transport.states) == 1      # 2回目で heartbeat


# ---- 実サブプロセス実行 ------------------------------------------------------

def test_subprocess_executor_roundtrip():
    ex = SubprocessExecutor(
        {"haiku": [sys.executable, "-c",
                   "import sys; data = sys.stdin.read();"
                   " print('中間'); print('結論: ' + data.upper())"]},
        timeout_sec=10)
    out = ex.run(make_request(body="abc"))
    assert out.ok
    assert out.summary == "結論: ABC"
    assert "中間" in out.detail


def test_subprocess_executor_nonzero_and_missing():
    ex = SubprocessExecutor(
        {"haiku": [sys.executable, "-c", "import sys; sys.exit(3)"]},
        timeout_sec=10)
    out = ex.run(make_request())
    assert not out.ok and not out.timed_out
    out2 = ex.run(make_request(tier="opus"))
    assert not out2.ok and "未設定" in out2.summary


def test_subprocess_executor_timeout():
    ex = SubprocessExecutor(
        {"haiku": [sys.executable, "-c", "import time; time.sleep(5)"]},
        timeout_sec=1)
    out = ex.run(make_request())
    assert not out.ok and out.timed_out


def test_subprocess_executor_uses_utf8():
    import front_desk.executor as ex_mod
    seen = {}
    real_run = ex_mod.subprocess.run

    class FakeProc:
        returncode = 0
        stdout = "結果\n結論: OK"
        stderr = ""

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return FakeProc()

    ex_mod.subprocess.run = spy
    try:
        ex = SubprocessExecutor({"haiku": ["claude", "-p"]}, timeout_sec=5)
        out = ex.run(make_request())
    finally:
        ex_mod.subprocess.run = real_run
    assert out.ok and out.summary == "結論: OK"
    assert seen.get("encoding") == "utf-8"
    assert seen.get("errors") == "replace"


# ---- 実行器 -----------------------------------------------------------------

def _run_all():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok - {name}")
        except Exception:
            failed += 1
            import traceback
            print(f"NG - {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
