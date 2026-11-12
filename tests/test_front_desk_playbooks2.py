# tests/test_front_desk_playbooks2.py
# PB-2〜PB-8 の進み方を確かめる (PB-1 は test_front_desk_playbooks.py)。
#
# 外部コマンドは FakeMachine (git / traytools_send.py / PowerShell の
# タスク操作の真似) で回し、ファイル操作が要るもの (PB-4/5/7/8) は
# 一時フォルダの実ファイルで確かめる。Linux でも通る。
#
#   python3 tests/test_front_desk_playbooks2.py    (Linux でも可)
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で
# 書いてある (tests/test_front_desk.py と同じ形)。
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

from front_desk import model, playbooks
from front_desk.config import DeskConfig
from front_desk.playbooks import (CommandResult, PlaybookContext,
                                  PlaybookExecutor, config_set,
                                  daemon_sync, deploy_approve, desk_sync,
                                  pc_capture, traytools_restart,
                                  watcher_sync)


# ---- 偽物の PC ----------------------------------------------------------

class FakeMachine:
    """git / traytools_send.py / スケジュールタスク操作の真似。"""

    def __init__(self):
        self.calls = []
        # git
        self.branch = "main"
        self.dirty = ""
        self.head = "a" * 40
        self.target = "b" * 40
        self.merge_fails = False
        # traytools_send.py
        self.check_text = "差し支えありません"
        self.restart_rc = 0
        self.status_rc = 0
        self.capture_rc = 0
        self.capture_output = ""
        # スケジュールタスク
        self.task_states = {}
        self.stop_rc = 0
        self.start_rc = 0
        self.spawn_rc = 0
        self.start_sets_running = True
        self.on_task_start = None

    def run(self, argv, timeout_sec):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return self._git(argv[5:])
        if argv[0] == "powershell":
            return self._ps(argv[-1])
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
            return CommandResult(0, "")
        if cmd == "merge":
            if self.merge_fails:
                return CommandResult(1, "fatal: not possible to fast-forward")
            self.head = self.target
            return CommandResult(0, "Fast-forward")
        if cmd == "reset":
            self.head = args[-1]
            return CommandResult(0, "")
        raise AssertionError(f"想定外の git 呼び出し: {args}")

    def _send(self, args):
        if args[:2] == ["restart", "--check"]:
            return CommandResult(0, self.check_text)
        if args[:1] == ["restart"]:
            return CommandResult(self.restart_rc, "再起動しました")
        if args[:1] == ["status"]:
            return CommandResult(self.status_rc, "状態")
        if args[:1] == ["capture"]:
            return CommandResult(self.capture_rc, self.capture_output)
        raise AssertionError(f"想定外の send 呼び出し: {args}")

    def _ps(self, script):
        if script.startswith("Start-Process"):
            return CommandResult(self.spawn_rc, "")
        name = script.split("-TaskName", 1)[1].strip().split()[0]
        if script.startswith("Stop-ScheduledTask"):
            if self.stop_rc == 0:
                self.task_states[name] = "Ready"
            return CommandResult(self.stop_rc, "")
        if script.startswith("Start-ScheduledTask"):
            if self.start_rc == 0 and self.start_sets_running:
                self.task_states[name] = "Running"
            if self.on_task_start:
                self.on_task_start(name)
            return CommandResult(self.start_rc, "")
        if "Get-ScheduledTask" in script:
            return CommandResult(0, self.task_states.get(name, "Ready") + "\n")
        raise AssertionError(f"想定外の PowerShell 呼び出し: {script}")

    # 検証用の小物
    def git_called(self, *prefix):
        tails = [c[5:] for c in self.calls if c[0] == "git"]
        return [t for t in tails if t[:len(prefix)] == list(prefix)]

    def send_called(self, *prefix):
        tails = [c[2:] for c in self.calls if c[0] not in ("git", "powershell")]
        return [t for t in tails if t[:len(prefix)] == list(prefix)]

    def ps_scripts(self):
        return [c[-1] for c in self.calls if c[0] == "powershell"]

    def ps_called(self, fragment):
        return [s for s in self.ps_scripts() if fragment in s]


def make_ctx(machine, settings=None, sleeps=None):
    return PlaybookContext(
        runner=machine, settings=dict(settings or {}),
        sleep_fn=(sleeps.append if sleeps is not None else (lambda s: None)))


def make_request(body, rid="req-t2"):
    if not isinstance(body, str):
        body = json.dumps(body)
    return model.Request(id=rid, tier="playbook", kind="ops", body=body)


# ---- 登録簿 -----------------------------------------------------------------

def test_all_playbooks_registered():
    assert set(playbooks.PLAYBOOKS) == {
        "pb1001", "pb1002", "pb1003", "pb2004",
        "pb3005", "pb4006", "pb4031", "pb4033", "pb4034", "pb1007",
        "pb5008", "pb5012", "pb5015"}


def test_new_playbooks_not_enabled_by_default():
    config = DeskConfig(allowed_kinds=("inspect", "ops"),
                        allowed_playbooks=("traytools_sync",))
    ex = PlaybookExecutor(config, runner=FakeMachine())
    for pid in ("daemon_sync", "deploy_approve", "pc_capture"):
        out = ex.run(make_request({"playbook": pid}))
        assert out.state == model.RESULT_REJECTED, (pid, out)


# ---- PB-2: daemon_sync ---------------------------------------------------------

DAEMON_SETTINGS = {"repo_path": "R:\\claude\\deploy-daemon\\app"}


def _daemon_machine():
    m = FakeMachine()
    m.branch = "muse/deploy-daemon"
    return m


def test_daemon_sync_happy_path():
    m = _daemon_machine()
    sleeps = []
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS, sleeps), {})
    assert out.ok, out
    assert m.git_called("merge", "--ff-only"), m.calls
    scripts = m.ps_scripts()
    stop_at = next(i for i, s in enumerate(scripts)
                   if s.startswith("Stop-ScheduledTask"))
    start_at = next(i for i, s in enumerate(scripts)
                    if s.startswith("Start-ScheduledTask"))
    assert stop_at < start_at, scripts
    assert 10 in sleeps, sleeps  # Stop と Start の間に10秒
    assert "step verify.task: ok" in out.detail


def test_daemon_sync_already_latest():
    m = _daemon_machine()
    m.target = m.head
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS), {})
    assert out.ok, out
    assert not m.git_called("merge"), m.calls
    assert m.ps_scripts() == [], m.calls


def test_daemon_sync_dirty_tree_blocks():
    m = _daemon_machine()
    m.dirty = " M pipeline.py\n"
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not m.git_called("merge"), m.calls
    assert m.ps_scripts() == [], m.calls


def test_daemon_sync_stop_failure_is_failed_without_start():
    m = _daemon_machine()
    m.stop_rc = 1
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS), {})
    assert not out.ok, out
    assert m.git_called("merge"), m.calls
    assert not m.ps_called("Start-ScheduledTask"), m.calls


def test_daemon_sync_task_not_running_fails_verify():
    m = _daemon_machine()
    m.start_sets_running = False
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS), {})
    assert not out.ok, out
    assert "step verify.task: ng" in out.detail


def test_daemon_sync_rejects_params():
    m = _daemon_machine()
    out = daemon_sync(make_ctx(m, DAEMON_SETTINGS), {"x": 1})
    assert out.state == model.RESULT_REJECTED, out
    assert m.calls == []


# ---- PB-3: desk_sync ---------------------------------------------------------

DESK_SETTINGS = {"repo_path": "R:\\claude\\front-desk\\app"}


def _desk_machine():
    m = FakeMachine()
    m.branch = "muse/front-desk"
    return m


def test_desk_sync_happy_path_defers_restart():
    m = _desk_machine()
    out = desk_sync(make_ctx(m, DESK_SETTINGS), {})
    assert out.ok, out
    assert m.git_called("merge", "--ff-only"), m.calls
    spawns = m.ps_called("Start-Process")
    assert len(spawns) == 1, m.ps_scripts()
    outer = spawns[0]
    # ヘルパーの中身は base64 (UTF-16LE) で -EncodedCommand に渡る。
    # そのまま引用符で囲むと中の単一引用符で壊れるため (2026-11-12 実測)。
    import base64
    import re
    assert "-EncodedCommand" in outer, outer
    assert "Stop-Process" not in outer and "front_desk" not in outer, outer
    encoded = re.search(r"-EncodedCommand','([^']+)'", outer).group(1)
    helper = base64.b64decode(encoded).decode("utf-16-le")
    assert "Stop-ScheduledTask" in helper and "FrontDesk" in helper
    assert "Start-Sleep -Seconds 20" in helper, helper
    # 停止→開始だけでは切り離された本体が生き残る (2026-11-10 実測)。
    # ヘルパーは本体の python プロセスを止めてからタスクを起こす。
    assert "Stop-Process" in helper and "front_desk" in helper, helper
    assert (helper.index("Stop-ScheduledTask")
            < helper.index("Stop-Process")
            < helper.index("Start-ScheduledTask")), helper
    # 窓口自身は直接タスクを止めない (止めると報告できないため)
    direct = [s for s in m.ps_scripts() if s.startswith("Stop-ScheduledTask")]
    assert direct == [], m.ps_scripts()
    assert "遅延実行" in out.summary, out.summary


def test_desk_sync_already_latest():
    m = _desk_machine()
    m.target = m.head
    out = desk_sync(make_ctx(m, DESK_SETTINGS), {})
    assert out.ok, out
    assert not m.git_called("merge"), m.calls
    assert m.ps_scripts() == [], m.calls


def test_desk_sync_spawn_failure_is_failed():
    m = _desk_machine()
    m.spawn_rc = 1
    out = desk_sync(make_ctx(m, DESK_SETTINGS), {})
    assert not out.ok, out
    assert m.git_called("merge"), m.calls


def test_desk_sync_dirty_tree_blocks():
    m = _desk_machine()
    m.dirty = "?? new.py\n"
    out = desk_sync(make_ctx(m, DESK_SETTINGS), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert not m.git_called("merge"), m.calls
    assert m.ps_scripts() == [], m.calls


# ---- PB-6: traytools_restart --------------------------------------------------

def test_restart_happy_path():
    m = FakeMachine()
    out = traytools_restart(make_ctx(m), {})
    assert out.ok, out
    assert m.send_called("restart", "--check"), m.calls
    assert m.send_called("restart"), m.calls
    assert m.send_called("status"), m.calls
    assert not m.git_called("merge"), m.calls  # 反映はしない


def test_restart_blocked_by_resident():
    m = FakeMachine()
    m.check_text = "見送るべきです（画面ミラー中）"
    out = traytools_restart(make_ctx(m), {})
    assert out.state == model.RESULT_BLOCKED, out
    tails = [c[2:] for c in m.calls if c[0] != "powershell"]
    assert tails == [["restart", "--check"]], m.calls


def test_restart_check_unreadable_blocks():
    m = FakeMachine()
    m.check_text = ""
    out = traytools_restart(make_ctx(m), {})
    assert out.state == model.RESULT_BLOCKED, out
    tails = [c[2:] for c in m.calls if c[0] != "powershell"]
    assert tails == [["restart", "--check"]], m.calls


def test_restart_failure_is_failed():
    m = FakeMachine()
    m.restart_rc = 1
    out = traytools_restart(make_ctx(m), {})
    assert not out.ok and out.state == "", out


def test_restart_rejects_params():
    m = FakeMachine()
    out = traytools_restart(make_ctx(m), {"force": True})
    assert out.state == model.RESULT_REJECTED, out
    assert m.calls == []


# ---- PB-5: config_set -----------------------------------------------------------

def _flag_dir(tmp, content=None, name="muse_stall_nudge.json"):
    path = Path(tmp) / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    return path


def test_config_set_happy_path():
    with tempfile.TemporaryDirectory() as td:
        path = _flag_dir(td, '{"enabled": true, "note": "keep"}\n')
        ctx = make_ctx(FakeMachine(), {"base_dir": td})
        out = config_set(ctx, {"file": "stall_nudge", "key": "enabled",
                               "value": False, "expected": True})
        assert out.ok, out
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"enabled": False, "note": "keep"}, data
        assert "step verify.value: ok" in out.detail


def test_config_set_rejects_unknown_file():
    out = config_set(make_ctx(FakeMachine(), {"base_dir": "/nonexistent"}),
                     {"file": "settings", "key": "x", "value": 1})
    assert out.state == model.RESULT_REJECTED, out


def test_config_set_rejects_missing_value():
    out = config_set(make_ctx(FakeMachine()),
                     {"file": "stall_nudge", "key": "enabled"})
    assert out.state == model.RESULT_REJECTED, out


def test_config_set_missing_file_blocks():
    with tempfile.TemporaryDirectory() as td:
        out = config_set(make_ctx(FakeMachine(), {"base_dir": td}),
                         {"file": "logview", "key": "open", "value": True})
        assert out.state == model.RESULT_BLOCKED, out


def test_config_set_invalid_json_blocks():
    with tempfile.TemporaryDirectory() as td:
        _flag_dir(td, "{not json")
        out = config_set(make_ctx(FakeMachine(), {"base_dir": td}),
                         {"file": "stall_nudge", "key": "enabled",
                          "value": False})
        assert out.state == model.RESULT_BLOCKED, out


def test_config_set_absent_key_blocks():
    with tempfile.TemporaryDirectory() as td:
        _flag_dir(td, '{"enabled": true}\n')
        out = config_set(make_ctx(FakeMachine(), {"base_dir": td}),
                         {"file": "stall_nudge", "key": "other",
                          "value": 1})
        assert out.state == model.RESULT_BLOCKED, out


def test_config_set_expected_mismatch_blocks_and_keeps_file():
    with tempfile.TemporaryDirectory() as td:
        path = _flag_dir(td, '{"enabled": true}\n')
        out = config_set(make_ctx(FakeMachine(), {"base_dir": td}),
                         {"file": "stall_nudge", "key": "enabled",
                          "value": False, "expected": False})
        assert out.state == model.RESULT_BLOCKED, out
        assert path.read_text(encoding="utf-8") == '{"enabled": true}\n'


def test_config_set_verify_failure_rolls_back():
    with tempfile.TemporaryDirectory() as td:
        original = '{"enabled": true}\n'
        path = _flag_dir(td, original)
        real_write = playbooks._write_text_atomic
        calls = {"n": 0}

        def flaky(p, text):
            calls["n"] += 1
            if calls["n"] == 1:
                # 最初の書き込みだけ、検証で合わない内容にする
                return real_write(p, '{"enabled": "tampered"}\n')
            return real_write(p, text)

        playbooks._write_text_atomic = flaky
        try:
            out = config_set(make_ctx(FakeMachine(), {"base_dir": td}),
                             {"file": "stall_nudge", "key": "enabled",
                              "value": False})
        finally:
            playbooks._write_text_atomic = real_write
        assert not out.ok, out
        assert "step rollback: ok" in out.detail, out.detail
        assert path.read_text(encoding="utf-8") == original


# ---- PB-4: deploy_approve -------------------------------------------------------

def _daemon_state(tmp, deliveries):
    base = Path(tmp) / "deploy-daemon"
    (base / "state.json").parent.mkdir(parents=True, exist_ok=True)
    (base / "state.json").write_text(
        json.dumps({"daemon": {"state": "idle"}, "deliveries": deliveries}),
        encoding="utf-8")
    return base


def test_deploy_approve_rejects_bad_id():
    ctx = make_ctx(FakeMachine(), {"daemon_base_dir": "/x"})
    for bad in ("req-1", "dd-2026-11-10", "dd-20261110", "", None, 42):
        out = deploy_approve(ctx, {"delivery_id": bad})
        assert out.state == model.RESULT_REJECTED, (bad, out)


def test_deploy_approve_not_configured_blocks():
    out = deploy_approve(make_ctx(FakeMachine(), {}),
                         {"delivery_id": "dd-20261110-01"})
    assert out.state == model.RESULT_BLOCKED, out
    assert "not_configured" in out.detail, out.detail


def test_deploy_approve_happy_path_and_idempotent():
    with tempfile.TemporaryDirectory() as td:
        base = _daemon_state(
            td, {"dd-20261110-01": {"state": "awaiting_apply_approval"}})
        ctx = make_ctx(FakeMachine(), {"daemon_base_dir": str(base)})
        out = deploy_approve(ctx, {"delivery_id": "dd-20261110-01"})
        assert out.ok, out
        approval = base / "approvals" / "dd-20261110-01.approve"
        assert approval.is_file()
        out2 = deploy_approve(ctx, {"delivery_id": "dd-20261110-01"})
        assert out2.ok and "設置済み" in out2.summary, out2


def test_deploy_approve_terminal_blocks():
    with tempfile.TemporaryDirectory() as td:
        base = _daemon_state(td, {"dd-20261110-02": {"state": "reported"}})
        out = deploy_approve(
            make_ctx(FakeMachine(), {"daemon_base_dir": str(base)}),
            {"delivery_id": "dd-20261110-02"})
        assert out.state == model.RESULT_BLOCKED, out
        assert not (base / "approvals" / "dd-20261110-02.approve").exists()


def test_deploy_approve_unknown_delivery_blocks():
    with tempfile.TemporaryDirectory() as td:
        base = _daemon_state(td, {})
        out = deploy_approve(
            make_ctx(FakeMachine(), {"daemon_base_dir": str(base)}),
            {"delivery_id": "dd-20261110-99"})
        assert out.state == model.RESULT_BLOCKED, out


# ---- PB-7: watcher_sync ----------------------------------------------------------

def _watcher_setup(tmp, source_text="new code", target_text="old code"):
    root = Path(tmp)
    src = root / "checkout" / "muse_monitor.py"
    src.parent.mkdir(parents=True)
    src.write_text(source_text, encoding="utf-8")
    inst = root / "claude-bridge"
    inst.mkdir()
    target = inst / "muse_monitor.py"
    target.write_text(target_text, encoding="utf-8")
    events = inst / "muse_monitor.events.json"
    events.write_text('{"events": []}', encoding="utf-8")
    settings = {
        "source_path": str(src),
        "target_path": str(target),
        "events_path": str(events),
        "verify_timeout_sec": "4",
    }
    return src, target, events, settings


def test_watcher_sync_happy_path():
    with tempfile.TemporaryDirectory() as td:
        _src, target, events, settings = _watcher_setup(td)
        m = FakeMachine()
        m.on_task_start = lambda name: events.write_text(
            events.read_text(encoding="utf-8") + '\n{"ev": "monitor_start"}',
            encoding="utf-8")
        sleeps = []
        out = watcher_sync(make_ctx(m, settings, sleeps), {})
        assert out.ok, out
        assert target.read_text(encoding="utf-8") == "new code"
        backups = list(target.parent.glob("muse_monitor_*_backup.py"))
        assert len(backups) == 1, list(target.parent.iterdir())
        assert backups[0].read_text(encoding="utf-8") == "old code"
        assert any(s >= 10 for s in sleeps), sleeps
        scripts = m.ps_scripts()
        stop_at = next(i for i, s in enumerate(scripts)
                       if s.startswith("Stop-ScheduledTask"))
        start_at = next(i for i, s in enumerate(scripts)
                        if s.startswith("Start-ScheduledTask"))
        assert stop_at < start_at, scripts
        assert "MuseBridgeMonitor" in scripts[stop_at]


def test_watcher_sync_same_hash_is_noop():
    with tempfile.TemporaryDirectory() as td:
        _src, target, _events, settings = _watcher_setup(
            td, source_text="same", target_text="same")
        m = FakeMachine()
        out = watcher_sync(make_ctx(m, settings), {})
        assert out.ok, out
        assert m.ps_scripts() == [], m.calls
        assert list(target.parent.glob("*_backup.py")) == []


def test_watcher_sync_not_configured_blocks():
    out = watcher_sync(make_ctx(FakeMachine(), {}), {})
    assert out.state == model.RESULT_BLOCKED, out
    assert "not_configured" in out.detail, out.detail


def test_watcher_sync_source_missing_blocks():
    with tempfile.TemporaryDirectory() as td:
        _src, _target, _events, settings = _watcher_setup(td)
        settings["source_path"] = str(Path(td) / "nope.py")
        out = watcher_sync(make_ctx(FakeMachine(), settings), {})
        assert out.state == model.RESULT_BLOCKED, out


def test_watcher_sync_no_monitor_start_rolls_back():
    with tempfile.TemporaryDirectory() as td:
        _src, target, _events, settings = _watcher_setup(td)
        m = FakeMachine()  # on_task_start 無し = monitor_start が来ない
        out = watcher_sync(make_ctx(m, settings), {})
        assert not out.ok, out
        assert target.read_text(encoding="utf-8") == "old code"
        assert "step rollback.file: ok" in out.detail, out.detail
        starts = [s for s in m.ps_scripts()
                  if s.startswith("Start-ScheduledTask")]
        assert len(starts) == 2, m.ps_scripts()  # 本番1回 + 巻き戻し1回


def test_watcher_sync_stop_failure_rolls_back():
    with tempfile.TemporaryDirectory() as td:
        _src, target, _events, settings = _watcher_setup(td)
        m = FakeMachine()
        m.stop_rc = 1
        out = watcher_sync(make_ctx(m, settings), {})
        assert not out.ok, out
        assert target.read_text(encoding="utf-8") == "old code"


# ---- PB-8: pc_capture -------------------------------------------------------------

def _capture_setup(tmp):
    root = Path(tmp)
    img = root / "captures" / "shot.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    onedrive = root / "OneDrive"
    onedrive.mkdir()
    m = FakeMachine()
    m.capture_output = f"OK 画面全体(1面) 800x600\n{img}\n"
    return img, onedrive, m


def test_pc_capture_happy_path():
    with tempfile.TemporaryDirectory() as td:
        img, onedrive, m = _capture_setup(td)
        ctx = make_ctx(m, {"onedrive_dir": str(onedrive)})
        out = pc_capture(ctx, {})
        assert out.ok, out
        dest = onedrive / "traytools-remote-capture" / "shot.png"
        assert dest.read_bytes() == img.read_bytes()
        assert str(dest) in out.summary, out.summary
        assert m.send_called("capture"), m.calls
        assert m.send_called("status"), m.calls


def test_pc_capture_with_screen_number():
    with tempfile.TemporaryDirectory() as td:
        _img, onedrive, m = _capture_setup(td)
        out = pc_capture(make_ctx(m, {"onedrive_dir": str(onedrive)}),
                         {"screen": 2})
        assert out.ok, out
        assert m.send_called("capture", "2"), m.calls


def test_pc_capture_rejects_bad_screen():
    m = FakeMachine()
    for bad in (0, -1, "2", True, 1.5):
        out = pc_capture(make_ctx(m, {"onedrive_dir": "/x"}),
                         {"screen": bad})
        assert out.state == model.RESULT_REJECTED, (bad, out)
    assert m.calls == []


def test_pc_capture_resident_down_blocks():
    with tempfile.TemporaryDirectory() as td:
        _img, onedrive, m = _capture_setup(td)
        m.status_rc = 1
        out = pc_capture(make_ctx(m, {"onedrive_dir": str(onedrive)}), {})
        assert out.state == model.RESULT_BLOCKED, out
        assert not m.send_called("capture"), m.calls


def test_pc_capture_no_onedrive_blocks():
    m = FakeMachine()
    saved = os.environ.pop("OneDrive", None)
    try:
        out = pc_capture(make_ctx(m, {}), {})
    finally:
        if saved is not None:
            os.environ["OneDrive"] = saved
    assert out.state == model.RESULT_BLOCKED, out
    assert "not_configured" in out.detail, out.detail
    assert m.calls == []


def test_pc_capture_failure_is_failed():
    with tempfile.TemporaryDirectory() as td:
        _img, onedrive, m = _capture_setup(td)
        m.capture_rc = 1
        m.capture_output = "ERR 撮影できません"
        out = pc_capture(make_ctx(m, {"onedrive_dir": str(onedrive)}), {})
        assert not out.ok and out.state == "", out


def test_pc_capture_unreadable_path_is_failed():
    with tempfile.TemporaryDirectory() as td:
        _img, onedrive, m = _capture_setup(td)
        m.capture_output = "OK 撮りました\nZ:\\no\\such\\file.png\n"
        out = pc_capture(make_ctx(m, {"onedrive_dir": str(onedrive)}), {})
        assert not out.ok, out


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
