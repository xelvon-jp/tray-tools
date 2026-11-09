# tests/test_front_desk_playbooks.py
# プレイブック（モデルなし実行）の振り分けと PB-1 を確かめる。
#
# 外部コマンドは FakePC（git と traytools_send.py の真似）で回す
# ので、Linux でも通る。実機の git / 常駐相手の確認は有効化手順
# （playbook-spec.md）の実依頼で別に行う。
#
#   python3 tests/test_front_desk_playbooks.py    (Linux でも可)
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で
# 書いてある (tests/test_front_desk.py と同じ形)。
import json
import sys
import tempfile
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
from front_desk.executor import ExecOutcome, FakeExecutor
from front_desk.intake import FetchResult
from front_desk.log import JsonlLog
from front_desk.pipeline import DeskPipeline
from front_desk.playbooks import (CommandResult, PlaybookContext,
                                  PlaybookExecutor, RoutingExecutor,
                                  traytools_sync)
from front_desk.statefile import DeskStateStore, ProcessedStore


# ---- 偽物の PC ----------------------------------------------------------

class FakePC:
    """git と traytools_send.py の振る舞いを真似る Runner。"""

    def __init__(self):
        self.calls = []            # [argv]
        self.branch = "main"
        self.dirty = ""
        self.check_text = "差し支えありません"
        self.head = "a" * 40
        self.target = "b" * 40
        self.fetch_fails = False
        self.merge_fails = False
        self.restart_rc = 0
        self.restart_fails_once = False  # 最初の restart だけ失敗する
        self.status_rc = 0
        self.head_after_apply = None   # None なら target どおり

    # Runner の口
    def run(self, argv, timeout_sec):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return self._git(argv[5:])
        return self._send(argv[2:])

    def _git(self, args):
        cmd = args[0]
        if cmd == "rev-parse" and "--is-inside-work-tree" in args:
            return CommandResult(0, "true\n")
        if cmd == "rev-parse" and "--abbrev-ref" in args:
            return CommandResult(0, self.branch + "\n")
        if cmd == "rev-parse" and args[-1] == "HEAD":
            return CommandResult(0, self.head + "\n")
        if cmd == "rev-parse":
            return CommandResult(0, self.target + "\n")
        if cmd == "status":
            return CommandResult(0, self.dirty)
        if cmd == "fetch":
            if self.fetch_fails:
                return CommandResult(1, "fatal: could not fetch")
            return CommandResult(0, "")
        if cmd == "merge":
            if self.merge_fails:
                return CommandResult(1, "fatal: not possible to fast-forward")
            self.head = self.head_after_apply or self.target
            return CommandResult(0, "Fast-forward")
        if cmd == "reset":
            self.head = args[-1]
            return CommandResult(0, "")
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    def _send(self, args):
        if args[:2] == ["restart", "--check"]:
            return CommandResult(0, self.check_text)
        if args[:1] == ["restart"]:
            if self.restart_fails_once:
                self.restart_fails_once = False
                return CommandResult(1, "再起動できませんでした")
            return CommandResult(self.restart_rc, "再起動しました")
        if args[:1] == ["status"]:
            return CommandResult(self.status_rc, "状態")
        raise AssertionError(f"想定外の send 呼び出し: {args}")

    # 検証用の小物
    def called(self, *prefix):
        return [c for c in self.calls if _argv_tail(c)[:len(prefix)] == list(prefix)]

    def called_exact(self, *tail):
        return [c for c in self.calls if _argv_tail(c) == list(tail)]


def _argv_tail(argv):
    if argv[0] == "git":
        return argv[5:]
    return argv[2:]


def make_ctx(pc, settings=None):
    return PlaybookContext(runner=pc, settings=dict(settings or {}),
                           sleep_fn=lambda s: None)


def make_request(body, tier="playbook", kind="ops", rid="req-t1"):
    if not isinstance(body, str):
        body = json.dumps(body)
    return model.Request(id=rid, tier=tier, kind=kind, body=body)


def make_executor(pc, allowed=("traytools_sync",), settings=None):
    config = DeskConfig(
        allowed_kinds=("inspect", "ops"),
        allowed_playbooks=tuple(allowed),
        playbook_settings=settings or {})
    return PlaybookExecutor(config, runner=pc)


# ---- 振り分けと形式 -------------------------------------------------------

def test_reject_non_ops_kind():
    ex = make_executor(FakePC())
    out = ex.run(make_request({"playbook": "traytools_sync"}, kind="inspect"))
    assert out.state == model.RESULT_REJECTED, out


def test_reject_non_json_body():
    ex = make_executor(FakePC())
    out = ex.run(make_request("ただの文章"))
    assert out.state == model.RESULT_REJECTED, out


def test_reject_unknown_playbook():
    ex = make_executor(FakePC())
    out = ex.run(make_request({"playbook": "nope"}))
    assert out.state == model.RESULT_REJECTED, out


def test_reject_not_enabled():
    ex = make_executor(FakePC(), allowed=())
    out = ex.run(make_request({"playbook": "traytools_sync"}))
    assert out.state == model.RESULT_REJECTED, out


def test_reject_params_not_dict():
    ex = make_executor(FakePC())
    out = ex.run(make_request({"playbook": "traytools_sync", "params": [1]}))
    assert out.state == model.RESULT_REJECTED, out


def test_reject_sync_params():
    pc = FakePC()
    out = traytools_sync(make_ctx(pc), {"branch": "other"})
    assert out.state == model.RESULT_REJECTED, out
    assert pc.calls == []


def test_config_parses_playbooks():
    cfg = DeskConfig.from_json({
        "allowed_playbooks": ["traytools_sync", 3, ""],
        "playbook_settings": {
            "traytools_sync": {"repo_path": "R:\\x", "n": 1},
            "bad": "not-a-dict",
        },
    })
    assert cfg.allowed_playbooks == ("traytools_sync",)
    assert cfg.playbook_settings == {"traytools_sync": {"repo_path": "R:\\x"}}


def test_routing_by_tier():
    pc = FakePC()
    playbooks = make_executor(pc)
    models = FakeExecutor(ExecOutcome(ok=True, summary="モデル側"))
    router = RoutingExecutor(playbooks, models)
    out = router.run(make_request({"playbook": "traytools_sync"}))
    assert out.ok, out
    out = router.run(make_request("本文", tier="haiku", kind="inspect"))
    assert out.ok and out.summary == "モデル側", out


# ---- PB-1 の進み方 ---------------------------------------------------------

def test_sync_happy_path():
    pc = FakePC()
    out = traytools_sync(make_ctx(pc), {})
    assert out.ok, out
    assert pc.called("merge", "--ff-only"), pc.calls
    assert pc.called_exact("restart"), pc.calls
    assert "step verify.liveness: ok" in out.detail
    assert "aaaaaaa" in out.summary and "bbbbbbb" in out.summary


def test_sync_already_latest():
    pc = FakePC()
    pc.target = pc.head
    out = traytools_sync(make_ctx(pc), {})
    assert out.ok, out
    assert not pc.called("merge"), pc.calls
    assert not pc.called_exact("restart"), pc.calls
    assert not pc.called_exact("restart", "--check"), pc.calls


def test_sync_dirty_tree_blocks_without_changes():
    pc = FakePC()
    pc.dirty = " M filer.py\n"
    out = traytools_sync(make_ctx(pc), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not pc.called("fetch"), pc.calls
    assert not pc.called("merge"), pc.calls
    assert not pc.called("restart"), pc.calls


def test_sync_wrong_branch_blocks():
    pc = FakePC()
    pc.branch = "muse/other"
    out = traytools_sync(make_ctx(pc), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not pc.called("fetch"), pc.calls


def test_sync_restart_check_blocked_by_resident():
    pc = FakePC()
    pc.check_text = "見送るべきです（付箋が開いています）"
    out = traytools_sync(make_ctx(pc), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not pc.called("merge"), pc.calls
    assert not pc.called_exact("restart"), pc.calls


def test_sync_restart_check_unreadable_blocks():
    pc = FakePC()
    pc.check_text = ""
    out = traytools_sync(make_ctx(pc), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not pc.called("merge"), pc.calls
    assert not pc.called_exact("restart"), pc.calls


def test_sync_fetch_failure_blocks():
    pc = FakePC()
    pc.fetch_fails = True
    out = traytools_sync(make_ctx(pc), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not pc.called("merge"), pc.calls


def test_sync_merge_failure_is_failed_without_restart():
    pc = FakePC()
    pc.merge_fails = True
    out = traytools_sync(make_ctx(pc), {})
    assert not out.ok and out.state == "", out
    assert not pc.called_exact("restart"), pc.calls
    assert not pc.called("reset"), pc.calls


def test_sync_restart_failure_rolls_back():
    pc = FakePC()
    pc.restart_fails_once = True
    out = traytools_sync(make_ctx(pc), {})
    assert not out.ok, out
    resets = pc.called("reset", "--hard")
    assert resets and resets[0][-1] == "a" * 40, pc.calls
    assert "巻き戻して復旧" in out.summary, out.summary


def test_sync_rollback_skipped_when_tree_dirty_after_apply():
    pc = FakePC()
    pc.restart_fails_once = True
    real_run = pc.run

    def run(argv, timeout_sec):
        result = real_run(argv, timeout_sec)
        if argv[0] == "git" and "merge" in argv:
            pc.dirty = " M changed.py\n"
        return result

    pc.run = run
    out = traytools_sync(make_ctx(pc), {})
    assert not out.ok, out
    assert not pc.called("reset"), pc.calls
    assert "巻き戻しはしませんでした" in out.summary, out.summary


def test_sync_verify_head_mismatch_rolls_back():
    pc = FakePC()
    pc.head_after_apply = "c" * 40
    out = traytools_sync(make_ctx(pc), {})
    assert not out.ok, out
    assert pc.called("reset", "--hard"), pc.calls


# ---- パイプライン越し -------------------------------------------------------

class FakeTransport:
    def __init__(self, requests=()):
        self._requests = list(requests)
        self.results = []
        self.states = []

    def list_requests(self):
        done_ids = {rid for rid, r, _ in self.results if r.terminal}
        return FetchResult(requests=tuple(
            r for r in self._requests if r.id not in done_ids))

    def post_result(self, request_id, result, at):
        self.results.append((request_id, result, at))
        return True

    def post_state(self, state):
        self.states.append(state)
        return True


def make_pipeline(tmp, transport, executor, **cfg):
    base = DeskConfig(base_dir=str(tmp), **cfg)
    return DeskPipeline(
        base, transport, executor,
        log=JsonlLog(str(tmp / "operations.jsonl")),
        state_store=DeskStateStore(str(tmp / "state.json")),
        processed_store=ProcessedStore(str(tmp / "processed.json")))


def test_pipeline_playbook_request_done():
    pc = FakePC()
    playbooks = make_executor(pc)
    router = RoutingExecutor(playbooks, FakeExecutor())
    req = make_request({"playbook": "traytools_sync"})
    transport = FakeTransport([req])
    with tempfile.TemporaryDirectory() as td:
        pipe = make_pipeline(
            Path(td), transport, router,
            allowed_kinds=("inspect", "ops"))
        assert pipe.poll_once() == 1
    rid, result, _at = transport.results[-1]
    assert rid == req.id and result.state == model.RESULT_DONE, result
    assert result.ok


def test_pipeline_ops_rejected_when_kind_not_allowed():
    pc = FakePC()
    router = RoutingExecutor(make_executor(pc), FakeExecutor())
    transport = FakeTransport([make_request({"playbook": "traytools_sync"})])
    with tempfile.TemporaryDirectory() as td:
        pipe = make_pipeline(Path(td), transport, router)  # allowed は inspect のみ
        assert pipe.poll_once() == 1
    _rid, result, _at = transport.results[-1]
    assert result.state == model.RESULT_REJECTED, result
    assert pc.calls == []


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
