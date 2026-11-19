# tests/test_front_desk_playbooks7.py
# PB-4035 (mcp_hands の公開開閉) の進み方。
# tailscale コマンドは FakeRunner、サーバ生存と Pushover は
# 差し替え、状態ファイルは一時フォルダ。Linux でも通る。
# 実行器は playbooks5 と同じ素の assert 方式。
#
#   python3 tests/test_front_desk_playbooks7.py
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from front_desk import model, playbooks
from front_desk.playbooks import (CommandResult, PlaybookContext, mcp_hands)

STATUS_ON = ("# Funnel on:\n"
             "#     - https://thinkcentre-yota.tail086c1a.ts.net\n"
             "|-- proxy http://127.0.0.1:8765\n")
STATUS_OFF = "No serve config\n"


class FakeRunner:
    """tailscale funnel の真似。状態を持ち、呼び出しを記録する。"""

    def __init__(self, funnel_on=False):
        self.calls = []
        self.funnel_on = funnel_on
        self.fail_on = False
        self.fail_off = False

    def run(self, argv, timeout_sec):
        self.calls.append(list(argv))
        if "status" in argv:
            return CommandResult(0, STATUS_ON if self.funnel_on
                                 else STATUS_OFF)
        if "--bg" in argv:
            if self.fail_on:
                return CommandResult(1, "funnel start failed")
            self.funnel_on = True
            return CommandResult(0, "Funnel started")
        if "off" in argv:
            if self.fail_off:
                return CommandResult(1, "off failed")
            self.funnel_on = False
            return CommandResult(0, "")
        return CommandResult(0, "")

    def funnel_cmds(self):
        return [c for c in self.calls if "funnel" in c]


@contextlib.contextmanager
def patched(alive=True):
    notes = []
    orig_alive = playbooks._mcp_server_alive
    orig_notify = playbooks._notify_pushover
    playbooks._mcp_server_alive = lambda url: alive
    playbooks._notify_pushover = \
        lambda title, message: notes.append((title, message)) or True
    try:
        yield notes
    finally:
        playbooks._mcp_server_alive = orig_alive
        playbooks._notify_pushover = orig_notify


def make_ctx(runner, state_path):
    return PlaybookContext(
        runner=runner,
        settings={"state_path": str(state_path)},
        sleep_fn=lambda *_: None)


def read_state(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def test_open_success():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner()
        with patched(alive=True) as notes:
            out = mcp_hands(make_ctx(runner, state), {"action": "open"})
        assert out.ok, out.summary
        assert runner.funnel_on
        st = read_state(state)
        assert st["status"] == "open" and st["by"] == "pb4035"
        assert abs(st["deadline_epoch"] - (time.time() + 2 * 3600)) < 60
        assert len(notes) == 1
    print("ok open_success")


def test_open_server_down_blocked():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner()
        with patched(alive=False):
            out = mcp_hands(make_ctx(runner, state), {"action": "open"})
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert runner.funnel_cmds() == []
        assert not state.exists()
    print("ok open_server_down_blocked")


def test_open_already_open_extends():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner(funnel_on=True)
        with patched(alive=True) as notes:
            out = mcp_hands(make_ctx(runner, state),
                            {"action": "open", "hours": 3})
        assert out.ok, out.summary
        assert not any("--bg" in c for c in runner.funnel_cmds())
        st = read_state(state)
        assert abs(st["deadline_epoch"] - (time.time() + 3 * 3600)) < 60
        assert notes == []
    print("ok open_already_open_extends")


def test_open_state_write_failure_rolls_back():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "missing-dir" / "funnel_state.json"
        runner = FakeRunner()
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner, state), {"action": "open"})
        assert not out.ok
        assert runner.funnel_on is False  # 巻き戻しで閉じた
        assert any("off" in c for c in runner.funnel_cmds())
    print("ok open_state_write_failure_rolls_back")


def test_open_command_failure():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner()
        runner.fail_on = True
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner, state), {"action": "open"})
        assert not out.ok
        assert not state.exists()
    print("ok open_command_failure")


def test_close_success():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner(funnel_on=True)
        with patched(alive=True) as notes:
            out = mcp_hands(make_ctx(runner, state), {"action": "close"})
        assert out.ok, out.summary
        assert runner.funnel_on is False
        st = read_state(state)
        assert st["status"] == "closed" and st["deadline_epoch"] is None
        assert len(notes) == 1
    print("ok close_success")


def test_close_command_failure():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner(funnel_on=True)
        runner.fail_off = True
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner, state), {"action": "close"})
        assert not out.ok
        assert runner.funnel_on is True
    print("ok close_command_failure")


def test_status_reports():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner(funnel_on=False)
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner, state), {"action": "status"})
        assert out.ok and "閉じています" in out.summary
        runner2 = FakeRunner(funnel_on=True)
        with patched(alive=False):
            out = mcp_hands(make_ctx(runner2, state), {"action": "status"})
        assert out.ok and "公開中" in out.summary
        assert "応答しません" in out.summary
        assert runner2.funnel_cmds() and \
            all("status" in c for c in runner2.funnel_cmds())
    print("ok status_reports")


def test_extend_open_and_closed():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner(funnel_on=False)
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner, state), {"action": "extend"})
        assert not out.ok and out.state == model.RESULT_BLOCKED
        runner2 = FakeRunner(funnel_on=True)
        playbooks._mcp_write_state(str(state), {
            "status": "open", "deadline_epoch": int(time.time()),
            "deadline": None, "updated_at": None, "by": "pb4035"})
        with patched(alive=True):
            out = mcp_hands(make_ctx(runner2, state),
                            {"action": "extend", "hours": 1})
        assert out.ok, out.summary
        st = read_state(state)
        assert abs(st["deadline_epoch"] - (time.time() + 3600)) < 60
    print("ok extend_open_and_closed")


def test_bad_params_rejected():
    with tempfile.TemporaryDirectory() as d:
        state = Path(d) / "funnel_state.json"
        runner = FakeRunner()
        ctx = make_ctx(runner, state)
        for params in ({}, {"action": "peek"},
                       {"action": "open", "path": "x"},
                       {"action": "open", "hours": 0},
                       {"action": "open", "hours": 13},
                       {"action": "open", "hours": "2"},
                       {"action": "open", "hours": True}):
            out = mcp_hands(ctx, params)
            assert not out.ok and out.state == model.RESULT_REJECTED, \
                (params, out.summary)
        assert runner.calls == []
    print("ok bad_params_rejected")


def test_registered():
    assert playbooks.PLAYBOOKS.get("pb4035") is mcp_hands
    assert playbooks.PLAYBOOK_ALIASES.get("mcp_hands") == "pb4035"
    print("ok registered")


def main():
    test_open_success()
    test_open_server_down_blocked()
    test_open_already_open_extends()
    test_open_state_write_failure_rolls_back()
    test_open_command_failure()
    test_close_success()
    test_close_command_failure()
    test_status_reports()
    test_extend_open_and_closed()
    test_bad_params_rejected()
    test_registered()
    print("all 11 tests passed")


if __name__ == "__main__":
    main()
