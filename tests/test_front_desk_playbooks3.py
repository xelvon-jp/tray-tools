# tests/test_front_desk_playbooks3.py
# 4桁 ID 化 (別名解決) と PB-4031 (PowerShell 実行) の進み方を確かめる。
#
#   python3 tests/test_front_desk_playbooks3.py    (Linux でも可)
import json
import os
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

from front_desk import activity, model, playbooks
from front_desk.config import DeskConfig
from front_desk.playbooks import (CommandResult, PlaybookContext,
                                  PlaybookExecutor, powershell_run)


def make_ctx(runner, settings=None):
    return PlaybookContext(runner=runner, settings=settings or {},
                           sleep_fn=lambda *_: None)


def make_request(body, rid="req-p3"):
    if not isinstance(body, str):
        body = json.dumps(body)
    return model.Request(id=rid, tier="playbook", kind="ops", body=body)


# ---- 別名解決 -------------------------------------------------------------

def _with_fake_pb():
    playbooks.PLAYBOOKS["pb9001"] = (
        lambda ctx, params: playbooks.ExecOutcome(ok=True, summary="fake"))
    playbooks.PLAYBOOK_ALIASES["old_test"] = "pb9001"


def _drop_fake_pb():
    playbooks.PLAYBOOKS.pop("pb9001", None)
    playbooks.PLAYBOOK_ALIASES.pop("old_test", None)


def test_alias_request_and_old_allowed():
    _with_fake_pb()
    try:
        config = DeskConfig(allowed_kinds=("ops",),
                            allowed_playbooks=("old_test",))
        ex = PlaybookExecutor(config, runner=object())
        out = ex.run(make_request({"playbook": "old_test"}))
        assert out.ok, out
        out = ex.run(make_request({"playbook": "pb9001"}))
        assert out.ok, out
    finally:
        _drop_fake_pb()


def test_alias_request_and_new_allowed():
    _with_fake_pb()
    try:
        config = DeskConfig(allowed_kinds=("ops",),
                            allowed_playbooks=("pb9001",))
        ex = PlaybookExecutor(config, runner=object())
        out = ex.run(make_request({"playbook": "old_test"}))
        assert out.ok, out
    finally:
        _drop_fake_pb()


def test_alias_activity_records_canonical():
    _with_fake_pb()
    path = os.path.join(tempfile.mkdtemp(), "desk_activity.json")
    try:
        config = DeskConfig(allowed_kinds=("ops",),
                            allowed_playbooks=("pb9001",))
        ex = PlaybookExecutor(config, runner=object(), activity_path=path)
        out = ex.run(make_request({"playbook": "old_test"}))
        assert out.ok, out
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        assert d["playbook"] == "pb9001", d
    finally:
        _drop_fake_pb()


# ---- PB-4031: PowerShell 実行 ---------------------------------------------

class FakePS:
    """run_bytes だけを持つ偽 Runner。.ps1 の中身を読んで控える。"""

    def __init__(self, result=None):
        self.result = result or CommandResult(0, "hello\n")
        self.argv = None
        self.timeout = None
        self.script_bytes = None
        self.script_path = None

    def run(self, argv, timeout_sec):
        raise AssertionError("pb4031 は run_bytes を使うはず")

    def run_bytes(self, argv, timeout_sec):
        self.argv = list(argv)
        self.timeout = timeout_sec
        self.script_path = argv[-1]
        with open(self.script_path, "rb") as fh:
            self.script_bytes = fh.read()
        return self.result


def test_pb4031_happy_path():
    fake = FakePS()
    out = powershell_run(make_ctx(fake), {"script": "Write-Output 'hi'"})
    assert out.ok, out
    assert fake.argv[0] == "powershell.exe" and "-File" in fake.argv
    assert fake.script_bytes.startswith(b"\xef\xbb\xbf")
    text = fake.script_bytes.decode("utf-8")
    assert "OutputEncoding" in text and "Write-Output 'hi'" in text
    assert not os.path.exists(fake.script_path)  # 一時ファイルは消える
    assert "hello" in out.detail and "sha256:" in out.detail


def test_pb4031_nonzero_exit():
    fake = FakePS(CommandResult(3, "boom"))
    out = powershell_run(make_ctx(fake), {"script": "exit 3"})
    assert not out.ok and "exit 3" in out.summary


def test_pb4031_timeout():
    fake = FakePS(CommandResult(None, "partial", timed_out=True))
    out = powershell_run(make_ctx(fake),
                         {"script": "sleep 999", "timeout_sec": 30})
    assert not out.ok and "タイムアウト" in out.summary
    assert fake.timeout == 30


def test_pb4031_guards():
    fake = FakePS()
    ctx = make_ctx(fake)
    for params in ({}, {"script": "  "}, {"script": "x" * 20001},
                   {"script": "ok", "bogus": 1},
                   {"script": "ok", "timeout_sec": "abc"},
                   {"script": "ok", "timeout_sec": True}):
        out = powershell_run(ctx, params)
        assert not out.ok and out.state == model.RESULT_BLOCKED, (params, out)
    assert fake.argv is None  # ガードで止まったら実行しない


def test_pb4031_timeout_clamped_and_output_tailed():
    fake = FakePS(CommandResult(0, "x" * 7000))
    out = powershell_run(make_ctx(fake),
                         {"script": "ok", "timeout_sec": 99999})
    assert out.ok and fake.timeout == 600
    assert "省略" in out.detail and len(out.detail) < 7000


def test_pb4031_via_executor_requires_allowed():
    config = DeskConfig(allowed_kinds=("ops",), allowed_playbooks=())
    ex = PlaybookExecutor(config, runner=FakePS())
    out = ex.run(make_request({"playbook": "pb4031",
                               "params": {"script": "ok"}}))
    assert out.state == model.RESULT_REJECTED
    config = DeskConfig(allowed_kinds=("ops",),
                        allowed_playbooks=("pb4031",))
    ex = PlaybookExecutor(config, runner=FakePS())
    out = ex.run(make_request({"playbook": "pb4031",
                               "params": {"script": "ok"}}))
    assert out.ok, out


ALL_TESTS = [test_alias_request_and_old_allowed,
             test_alias_request_and_new_allowed,
             test_alias_activity_records_canonical,
             test_pb4031_happy_path,
             test_pb4031_nonzero_exit,
             test_pb4031_timeout,
             test_pb4031_guards,
             test_pb4031_timeout_clamped_and_output_tailed,
             test_pb4031_via_executor_requires_allowed]


if __name__ == "__main__":
    for fn in ALL_TESTS:
        fn()
        print(f"PASS {fn.__name__}")
