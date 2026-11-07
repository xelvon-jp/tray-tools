# tests/test_claude_status.py
# Claude Code の状態表示(claude_status.py と hooks/claude_hook.py の状態書き込み)を確かめる。
# 常駐には送らない(claude_hook.send を差し替える)。トレイの文字列組み立ても見る。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_claude_status.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_screen_mirror.py と同じ形)。
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "hooks"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import claude_status as cs  # noqa: E402


def test_apply_plan_items_advance_done_start():
    plan = cs.apply_plan([], "mcp__ccd_turn__update_plan", {"items": [
        {"content": "読む", "status": "in_progress"},
        {"content": "書く", "status": "pending"},
        {"content": "試す", "status": "pending"}]})
    assert cs.plan_text(plan) == "1/3 読む"
    plan = cs.apply_plan(plan, "mcp__ccd_turn__update_plan", {"advance": True})
    assert cs.plan_text(plan) == "2/3 書く"
    plan = cs.apply_plan(plan, "mcp__ccd_turn__update_plan", {"done": [2], "start": 3})
    assert cs.plan_text(plan) == "3/3 試す" and plan[1]["status"] == "completed"
    plan = cs.apply_plan(plan, "mcp__ccd_turn__update_plan", {"advance": True})
    assert cs.plan_text(plan) == "3/3 済"
    # 壊れた入力では元のまま
    assert cs.apply_plan(plan, "mcp__ccd_turn__update_plan", "x") == plan
    todo = cs.apply_plan([], "TodoWrite", {"todos": [
        {"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]})
    assert cs.plan_text(todo) == "2/2 b"
    assert cs.plan_text([]) == ""


def test_list_and_ttl():
    now = 1_000_000.0
    with tempfile.TemporaryDirectory() as tmp:
        cs.write("run", {"state": cs.RUNNING, "started": now - 252, "updated": now - 5,
                         "project": "tray-tools"}, tmp)
        cs.write("ask", {"state": cs.ASK, "asked": now - 120, "updated": now - 120,
                         "project": "obsidian"}, tmp)
        cs.write("done", {"state": cs.DONE, "started": now - 600, "ended": now - 60,
                          "updated": now - 60, "project": "beads"}, tmp)
        cs.write("old", {"state": cs.DONE, "ended": now - cs.DONE_TTL - 1,
                         "updated": now - cs.DONE_TTL - 1}, tmp)
        cs.write("stale", {"state": cs.RUNNING, "updated": now - cs.STALE_TTL - 1}, tmp)
        cs.write("../evil", {"state": cs.RUNNING, "updated": now}, tmp)   # パスに混ぜない
        assert not Path(tmp).parent.joinpath("evil.json").exists()
        entries = cs.list_sessions(tmp, now=now)
        evil = [e for e in entries if e.get("project") is None]
        assert len(evil) == 1 and Path(evil[0]["_path"]).parent == Path(tmp)   # 中に留まる
        entries = [e for e in entries if e.get("project")]
        names = [e.get("project") for e in entries]
        assert names[:3] == ["obsidian", "tray-tools", "beads"], names   # 確認待ちが先頭
        assert cs.overall_state(entries) == cs.ASK
        assert cs.describe(entries[1], now)[0] == "tray-tools　4分12秒"
        assert "承認を待っています（2分00秒）" in cs.describe(entries[0], now)[0]
        assert cs.describe(entries[2], now)[0].startswith("beads　9分00秒で完了")
        assert cs.menu_label(entries[0], now) == "確認待ち　obsidian　2分00秒"
        assert cs.clear_finished(tmp) == 2
        assert [e.get("project") for e in cs.list_sessions(tmp, now=now)
                if e.get("project")] == ["obsidian", "tray-tools"]
        assert cs.overall_state([]) is None


def test_hook_writes_status_through_a_turn():
    import claude_hook as hook
    sent = []
    hook.send = lambda command, args: (sent.append((command, list(args))), True)[1]
    hook.is_away = lambda config: False
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")   # 本物の hook.log を汚さない
        config = dict(hook.DEFAULTS, min_seconds=0)
        event = {"session_id": "s-1", "cwd": r"R:\claude\tray-tools"}
        hook.on_start(config, event)
        data = cs.read("s-1")
        assert data["state"] == cs.RUNNING and data["project"] == "tray-tools"
        hook.on_tool(config, dict(event, tool_name="mcp__ccd_turn__update_plan", tool_input={
            "items": [{"content": "読む", "status": "in_progress"},
                      {"content": "書く", "status": "pending"}]}))
        hook.on_tool(config, dict(event, tool_name="mcp__ccd_turn__update_plan",
                                  tool_input={"advance": True}))
        assert cs.plan_text(cs.read("s-1")["plan"]) == "2/2 書く"
        # 計画以外のツールでは何も書かない
        before = cs.read("s-1")["updated"]
        time.sleep(0.01)
        hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
        assert cs.read("s-1")["updated"] == before
        # 確認待ち → 次のツールが走ったら実行中へ戻る
        hook.on_ask(config, event)
        assert cs.read("s-1")["state"] == cs.ASK
        assert ("notify", ["Claude（tray-tools）\n確認待ちで止まっています"]) in sent
        hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
        data = cs.read("s-1")
        assert data["state"] == cs.RUNNING and data.get("asked") is None
        hook.on_stop(config, event)
        data = cs.read("s-1")
        assert data["state"] == cs.DONE and data["ended"] >= data["started"]
        assert any(c == "notify" and "応答が終わりました" in a[0] for c, a in sent), sent
        # 次のターンで計画は持ち越さない
        hook.on_start(config, event)
        assert cs.read("s-1")["plan"] == [] and cs.read("s-1")["state"] == cs.RUNNING
        # 書くのを切れば触らない
        hook.on_fail(dict(config, status_enabled=False), event)
        assert cs.read("s-1")["state"] == cs.RUNNING
        hook.on_fail(config, event)
        assert cs.read("s-1")["state"] == cs.FAIL


def test_pretool_phase():
    import claude_hook as hook
    import claude_progress as cp
    hook.send = lambda command, args: True
    hook.is_away = lambda config: False
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")
        config = dict(hook.DEFAULTS, min_seconds=0)
        event = {"session_id": "p-1", "cwd": r"R:\claude\tray-tools"}
        # 状態ファイルが無いセッションには何も書かない
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        assert cs.read("p-1") == {}
        hook.on_start(config, event)
        assert cs.read("p-1")["phase"] == "responding"
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        data = cs.read("p-1")
        assert data["state"] == cs.RUNNING and data["phase"] == "tool" and data["tool_name"] == "Bash"
        # 同じ内容なら書かない
        before = data["updated"]
        time.sleep(0.01)
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        assert cs.read("p-1")["updated"] == before
        hook.on_pretool(config, dict(event, tool_name="Read"))
        assert cs.read("p-1")["tool_name"] == "Read"
        # ツールが終わったら応答中へ。その後の PostToolUse は書かない
        hook.on_tool(config, dict(event, tool_name="Read", tool_input={}))
        data = cs.read("p-1")
        assert data["phase"] == "responding" and data["tool_name"] is None
        before = data["updated"]
        time.sleep(0.01)
        hook.on_tool(config, dict(event, tool_name="Read", tool_input={}))
        assert cs.read("p-1")["updated"] == before
        # running 以外は上書きしない(遅れて届いた pretool で戻さない)
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        hook.on_ask(config, event)
        data = cs.read("p-1")
        assert data["state"] == cs.ASK and data.get("phase") is None
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        data = cs.read("p-1")
        assert data["state"] == cs.ASK and data.get("phase") is None
        hook.on_stop(config, event)
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        data = cs.read("p-1")
        assert data["state"] == cs.DONE and data.get("phase") is None
        hook.on_fail(config, event)
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        assert cs.read("p-1")["state"] == cs.FAIL and cs.read("p-1").get("phase") is None
        # 確認待ちが解けて走り出したら running + responding
        hook.on_start(config, event)
        hook.on_pretool(config, dict(event, tool_name="Bash"))
        hook.on_ask(config, event)
        hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
        data = cs.read("p-1")
        assert data["state"] == cs.RUNNING and data["phase"] == "responding"
    # パネルの言い回し
    assert cp.running_label({"state": cs.RUNNING}) == "Claude 応答中"
    assert cp.running_label({"phase": "responding"}) == "Claude 応答中"
    assert cp.running_label({"phase": "tool"}) == "Claude 処理中"
    assert cp.running_label({"phase": "tool", "tool_name": "Bash"}) == "Claude 処理中（Bash）"
    # mcp__サーバー__ツール は末尾のツール名だけ見せる。普通の長い名前は12文字で切る
    mcp = cp.running_label({"phase": "tool", "tool_name": "mcp__tray-tools__capture"})
    assert mcp == "Claude 処理中（capture）", mcp
    long = cp.running_label({"phase": "tool", "tool_name": "VeryLongToolNameHere"})
    assert long == "Claude 処理中（VeryLongTool…）", long


def test_title_tells_sessions_apart():
    assert cs.prompt_title("<pasted_content id=x>\n長い\n貼り付け</pasted_content>  修正して") == \
        "長い 貼り付け 修正して"
    entry = {"project": "claude", "title": "私の方でなにかすることある？", "state": cs.DONE,
             "started": 100.0, "ended": 155.0, "updated": 155.0}
    assert cs.display_name(entry) == "claude「私の方でなにかする…」"
    assert cs.describe(entry, 200.0)[0].startswith("claude「私の方でなにかする…」　55秒で完了")
    assert cs.display_name({"project": "claude"}) == "claude"
    assert "「私の方でなにかすることある？」" in cs.menu_label(entry, 200.0)


def test_resident_change_ignores_hooks_and_tests():
    import filer_update as u
    assert u.resident_files(["hooks/claude_hook.py", "tests/test_x.py", "filer.py"]) == []
    assert u.resident_files(["hooks/claude_hook.py", "claude_status.py"]) == ["claude_status.py"]
    calls = []

    def run(args, timeout):
        calls.append(args)
        return 0, "hooks/claude_hook.py\ntests/test_claude_status.py\n", ""
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".git").mkdir()
        assert u.changed_files("a" * 40, "b" * 40, tmp, run=run) == [
            "hooks/claude_hook.py", "tests/test_claude_status.py"]
        assert calls == [["diff", "--name-only", "a" * 40, "b" * 40]]
        assert u.changed_files("a" * 40, "b" * 40, tmp, run=lambda a, t: (128, "", "bad")) is None
        assert u.changed_files("", "b" * 40, tmp, run=run) is None


def test_jump_url_and_fraction():
    assert cs.jump_url({"host_session_id": "local_271781ac-b747-46d1-9974-448b994962c3"}) == \
        "claude://code/continue?session=local_271781ac-b747-46d1-9974-448b994962c3"
    for bad in ("", "local_", "local_a&b=1", "session_x", "local_" + "a" * 65, None):
        assert cs.jump_url({"host_session_id": bad}) == "", bad
    assert cs.open_in_app({}) is False
    assert cs.plan_fraction([]) is None
    plan = [{"status": "completed"}, {"status": "in_progress"}, {"status": "pending"},
            {"status": "pending"}]
    assert cs.plan_fraction(plan) == 1.5 / 4


def test_app_session_title():
    """アプリが保存しているセッション名を出す。取れなければ「フォルダ名「書き出し」」。"""
    import json
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp, "acct", "org")
        folder.mkdir(parents=True)
        path = folder / "local_t1.json"
        path.write_text(json.dumps({"sessionId": "local_t1", "title": "Tray-Tools_1104(ローカル)"},
                                   ensure_ascii=False), encoding="utf-8")
        old = cs.APP_SESSIONS_DIR
        cs.APP_SESSIONS_DIR = Path(tmp)
        try:
            entry = {"project": "claude", "title": "書き出し", "host_session_id": "local_t1"}
            assert cs.display_name(entry) == "Tray-Tools_1104(ローカル)"
            assert cs.display_name(entry, 5) == "Tray-Tools_1104(ロー…"   # 5+14=19 文字に切る
            # アプリで名前を変えたら付いてくる(更新時刻が変われば読み直す)
            path.write_text(json.dumps({"title": "新しい名前"}, ensure_ascii=False),
                            encoding="utf-8")
            os.utime(path, (time.time() + 5, time.time() + 5))
            assert cs.display_name(entry) == "新しい名前"
            # 名前が無い・ファイルが無い・形が違う ID なら今までの表示
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (time.time() + 10, time.time() + 10))
            assert cs.display_name(entry) == "claude「書き出し」"
            assert cs.display_name(dict(entry, host_session_id="local_none")) == "claude「書き出し」"
            assert cs.app_session_title("../x") == ""
        finally:
            cs.APP_SESSIONS_DIR = old


def test_background_work_after_reply():
    """応答を終えても裏の処理が残っていれば「裏で作業中」。起こされて動けば実行中に戻る。"""
    text = "\n".join([
        '{"toolUseResult":{"backgroundTaskId":"aaa1"}}',
        '{"toolUseResult":{"backgroundTaskId":"bbb2"}}',
        '{"toolUseResult":{"backgroundTaskId":"ccc3"}}',
        '{"content":"<task-notification>\\n<task-id>aaa1</task-id>\\n<status>completed"}',
        '{"name":"TaskStop","input":{"task_id":"ccc3"}}',
    ])
    assert cs.pending_background(text) == ["bbb2"]
    assert cs.pending_background("") == [] and cs.pending_background_in("Z:/nope.jsonl") == []

    import claude_hook as hook
    hook.send = lambda command, args: True
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")
        transcript = Path(tmp, "t.jsonl")
        transcript.write_text(text, encoding="utf-8")
        config = dict(hook.DEFAULTS, min_seconds=0)
        event = {"session_id": "bg", "cwd": "R:/x", "transcript_path": str(transcript)}
        hook.on_start(config, event)
        hook.on_stop(config, event)
        data = cs.read("bg")
        assert data["state"] == cs.DONE and data["background"] == 1
        assert cs.background_count(data) == 1
        assert "裏で作業中" in cs.menu_label(data) and "裏で作業中" in cs.describe(data)[0]
        assert cs.background_count(data, data["ended"] + cs.BACKGROUND_TTL + 1) == 0
        # 完了より前に起こされたフック(遅れて届いた最後のツールの分)では戻さない
        hook.HOOK_STARTED = data["ended"] - 1
        hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
        assert cs.read("bg")["state"] == cs.DONE
        # 裏の処理が終わって起こされ、ツールが走った → 実行中に戻り、「裏で作業中」は消える
        hook.HOOK_STARTED = data["ended"] + 5
        hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
        data = cs.read("bg")
        assert data["state"] == cs.RUNNING and data["background"] == 0 and data["ended"] is None
        # 次の終わりで、もう残りが無ければ普通の完了
        transcript.write_text(text + '\n{"content":"<task-id>bbb2</task-id>"}', encoding="utf-8")
        hook.on_stop(config, event)
        assert cs.read("bg")["background"] == 0


def test_remove_one_session():
    with tempfile.TemporaryDirectory() as tmp:
        cs.write("s1", {"state": cs.DONE, "updated": time.time()}, tmp)
        outside = Path(tmp).parent / "keep_me.json"
        assert cs.remove({"session_id": "s1"}, tmp) is True
        assert not cs.status_path("s1", tmp).exists()
        assert cs.remove({"session_id": "s1"}, tmp) is False      # もう無い
        assert cs.remove({"session_id": "../keep_me", "_path": str(outside)}, tmp) is False
        assert cs.remove({}, tmp) is False


def test_hook_records_host_session():
    import claude_hook as hook
    hook.send = lambda command, args: True
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")
        old = os.environ.get("CLAUDE_CODE_HOST_SESSION_ID")
        try:
            os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "local_abc-123"
            hook.on_start(dict(hook.DEFAULTS), {"session_id": "h1", "cwd": "R:/x"})
            assert cs.read("h1")["host_session_id"] == "local_abc-123"
            os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = "../../evil"
            hook.on_start(dict(hook.DEFAULTS), {"session_id": "h2", "cwd": "R:/x"})
            assert "host_session_id" not in cs.read("h2")
        finally:
            if old is None:
                os.environ.pop("CLAUDE_CODE_HOST_SESSION_ID", None)
            else:
                os.environ["CLAUDE_CODE_HOST_SESSION_ID"] = old


def test_progress_panel_rows_and_grab():
    import claude_progress as cp
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    now = time.time()
    seen = {}
    running = {"session_id": "a", "state": cs.RUNNING, "started": now - 252, "project": "tray-tools",
               "title": "メニューの項目押したとき", "host_session_id": "local_a",
               "plan": [{"content": "読む", "status": "completed"},
                        {"content": "書く", "status": "completed"},
                        {"content": "テストと実常駐で確認", "status": "in_progress"},
                        {"content": "x", "status": "pending"}, {"content": "y", "status": "pending"},
                        {"content": "z", "status": "pending"}]}
    asking = {"session_id": "b", "state": cs.ASK, "asked": now - 120, "project": "obsidian",
              "title": "週報まとめて", "plan": [{"content": "ノートを読む", "status": "in_progress"},
                                               {"content": "b", "status": "pending"}]}
    noplan = {"session_id": "c", "state": cs.RUNNING, "started": now - 38, "project": "claude",
              "title": "入れるだけ試して", "plan": []}
    done = {"session_id": "d", "state": cs.DONE, "started": now - 90, "ended": now - 1,
            "project": "beads"}
    rows = cp.rows_to_show([asking, running, noplan, done], now, seen)
    assert [r["session_id"] for r in rows] == ["b", "a", "c", "d"]
    assert not any(r["_compact"] for r in rows)
    # 終わったものは、最初に見てから LINGER_SECONDS 経つと1行に折りたたむ(消さない)
    later = cp.rows_to_show([running, done], now + cp.LINGER_SECONDS + 1, seen)
    assert [(r["session_id"], r["_compact"]) for r in later] == [("a", False), ("d", True)]
    # 折りたたんだ行は終わった新しい順に MAX_COMPACT まで
    many = [dict(done, session_id=f"d{i}", ended=now - 100 + i) for i in range(8)]
    seen_many = {}
    cp.rows_to_show(many, now, seen_many)            # ここで「終わった」と初めて見る
    folded = cp.rows_to_show(many, now + 1000, seen_many)
    assert [r["session_id"] for r in folded] == ["d7", "d6", "d5", "d4", "d3"]
    assert cp.panel_height(later) == cp.PAD_Y * 2 + cp.ROW_HEIGHT + cp.COMPACT_HEIGHT
    with tempfile.TemporaryDirectory() as tmp:
        opened = []
        panel = cp.ClaudeProgressPanel(on_open=opened.append, state_path=Path(tmp, "s.json"))
        panel.set_entries([asking, running, noplan])
        assert panel.isVisible() and panel.height() == cp.PAD_Y * 2 + cp.ROW_HEIGHT * 3
        # 行のクリックで、その行のセッションを開く
        point = QPointF(panel._row_rect(1).center())
        panel.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, point, Qt_left(),
                                          Qt_left(), no_mod()))
        assert opened and opened[0]["session_id"] == "a"
        out = os.environ.get("PROGRESS_GRAB_DIR")
        if out:
            Path(out).mkdir(parents=True, exist_ok=True)
            panel.grab().save(str(Path(out) / "progress_panel.png"))
        # 終わったものだけになっても、折りたたんだ行で残り、押せばそのセッションを開く
        finished = dict(running, state=cs.DONE, ended=now - 60)
        panel._first_seen_done["a"] = now - cp.LINGER_SECONDS - 1
        panel._first_seen_done["d"] = now - cp.LINGER_SECONDS - 2
        panel.set_entries([noplan, finished, dict(done, host_session_id="local_d")])
        assert panel.isVisible() and [r["_compact"] for r in panel._rows] == [False, True, True]
        assert panel.height() == cp.PAD_Y * 2 + cp.ROW_HEIGHT + cp.COMPACT_HEIGHT * 2
        if out:
            panel.grab().save(str(Path(out) / "progress_panel_folded.png"))
        opened.clear()
        # 折りたたんだ行は終わった新しい順(d が後に終わった)
        assert [r["session_id"] for r in panel._rows] == ["c", "d", "a"]
        point = QPointF(panel._row_rect(2).center())
        panel.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, point, Qt_left(),
                                          Qt_left(), no_mod()))
        assert opened and opened[0]["session_id"] == "a"
        # ✕ でその行だけ消える(開きはしない)。消す本体は呼んだ側に任せる
        removed = []
        panel._on_remove = removed.append
        opened.clear()
        point = QPointF(panel._close_rect(1).center())
        panel.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, point, Qt_left(),
                                          Qt_left(), no_mod()))
        assert [r["session_id"] for r in removed] == ["d"] and opened == []
        assert [r["session_id"] for r in panel._rows] == ["c", "a"]
        # 何も無くなればパネルごと隠す
        panel.set_entries([])
        assert not panel.isVisible()
        # 位置の保存は該当キーだけ差し替える
        cp.save_state({"enabled": False}, Path(tmp, "s.json"))
        cp.save_state({"pos": [10, 20]}, Path(tmp, "s.json"))
        assert cp.load_state(Path(tmp, "s.json")) == {"enabled": False, "pos": [10, 20]}
        panel.close()


def Qt_left():
    from PySide6.QtCore import Qt
    return Qt.LeftButton


def no_mod():
    from PySide6.QtCore import Qt
    return Qt.NoModifier


def test_hook_reads_utf8_stdin():
    """フックの標準入力は UTF-8。Windows の既定(cp932)で解くと日本語が化ける。"""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, TEMP=tmp, TMP=tmp)
        env.pop("PYTHONUTF8", None)
        payload = {"session_id": "u8", "cwd": "R:/claude/日本語の作業",
                   "tool_name": "mcp__ccd_turn__update_plan",
                   "tool_input": {"items": [{"content": "テストと確認", "status": "in_progress"}]}}
        import json
        for action in ("start", "tool"):
            subprocess.run([sys.executable, str(ROOT / "hooks" / "claude_hook.py"), action],
                           input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           env=env, capture_output=True, timeout=30)
        data = cs.read("u8", Path(tmp, "tray-tools-hooks", "status"))
        assert data.get("project") == "日本語の作業", data
        assert cs.plan_text(data.get("plan")) == "1/1 テストと確認", data


def test_tray_tooltip_helpers():
    import feature_screen as fs
    assert fs._join_state("Rapture", "Claude 実行中") == "Rapture（Claude 実行中）"
    assert fs._join_state("Rapture（スリープ抑止中）", "Claude 確認待ち") == \
        "Rapture（スリープ抑止中・Claude 確認待ち）"
    long_lines = ["Rapture（Claude 実行中）"] + ["あ" * 40] * 4
    text = fs._fit_tooltip(long_lines)
    assert len(text) <= fs.TOOLTIP_MAX_CHARS and text.startswith("Rapture（Claude 実行中）")
    assert text.count("\n") == 2   # 収まらない行は後ろから落とす
    # 進捗パネルを出しているときは、ツールチップに Claude の中身を重ねない
    from types import SimpleNamespace
    shown = []
    stub = SimpleNamespace(
        _states_text="Rapture", _disk_newer=False,
        _claude_entries=[{"state": cs.RUNNING, "started": time.time(), "project": "x"}],
        _claude_progress_enabled=True,
        tray_icon=SimpleNamespace(setToolTip=shown.append))
    fs.ScreenFeature._update_tooltip(stub)
    assert shown[-1] == "Rapture"
    stub._claude_progress_enabled = False
    fs.ScreenFeature._update_tooltip(stub)
    assert shown[-1].startswith("Rapture（Claude 実行中）\nx　")


def test_delayed_posttool_does_not_revive():
    """レビュー指摘 #3: ターン最後のツールの PostToolUse プロセスが OS の都合で
    遅れて起動し、Stop が ended を書いた「直後」に始まった場合、それは裏の処理に
    起こされた復活ではない。完了済みセッションを RUNNING に戻してはいけない。
    十分に時間が経ってからのツールは、正規の復活として RUNNING に戻す。"""
    import claude_hook as hook
    hook.send = lambda command, args: True
    hook.is_away = lambda config: False
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")
        transcript = Path(tmp, "t.jsonl")
        transcript.write_text(
            '{"toolUseResult":{"backgroundTaskId":"bbb2"}}', encoding="utf-8")
        config = dict(hook.DEFAULTS, min_seconds=0)
        event = {"session_id": "late", "cwd": "R:/x",
                 "transcript_path": str(transcript)}
        try:
            hook.on_start(config, event)
            hook.on_stop(config, event)
            data = cs.read("late")
            assert data["state"] == cs.DONE and data["background"] == 1
            # 終了の1秒後に「起動した」PostToolUse = 遅れて届いた最後のツールの分
            hook.HOOK_STARTED = data["ended"] + 1
            hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
            assert cs.read("late")["state"] == cs.DONE, cs.read("late")
            # 終了から十分経ってのツール = 裏の処理が終わって起こされた復活
            hook.HOOK_STARTED = data["ended"] + 30
            hook.on_tool(config, dict(event, tool_name="Bash", tool_input={}))
            revived = cs.read("late")
            assert revived["state"] == cs.RUNNING and revived["ended"] is None
        finally:
            hook.HOOK_STARTED = time.time()


def test_concurrent_update_status_keeps_both_fields():
    """レビュー指摘 #4: pretool と posttool のフックが並行に走り、読み→書きの
    間に割り込まれても、互いの項目(plan と phase)を落とさない。

    割り込みを決定的に作るため、最初の write を途中で止めて、その間に
    もう1つの update_status を最後まで走らせる。"""
    import threading

    import claude_hook as hook
    hook.send = lambda command, args: True
    with tempfile.TemporaryDirectory() as tmp:
        cs.STATUS_DIR = Path(tmp, "status")
        hook.STATE_DIR = Path(tmp)
        hook.LOG_PATH = Path(tmp, "hook.log")
        config = dict(hook.DEFAULTS, min_seconds=0)
        event = {"session_id": "race", "cwd": "R:/x"}
        cs.write("race", {"session_id": "race", "state": cs.RUNNING,
                          "plan": [], "phase": "responding"})
        real_write = cs.write
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_first_write(session_id, data, directory=None):
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(timeout=5)
            return real_write(session_id, data, directory)

        cs.write = slow_first_write
        errors = []

        def run_a():
            try:
                hook.update_status(config, event, None,
                                   plan=[{"content": "a", "status": "in_progress"}])
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def run_b():
            try:
                hook.update_status(config, event, None, phase="tool",
                                   tool_name="Bash")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        thread_a = threading.Thread(target=run_a)
        thread_b = threading.Thread(target=run_b)
        try:
            thread_a.start()
            assert entered.wait(timeout=5), "最初の書き込みに届いていない"
            # A が読み終えて書きかけの間に、B が phase を書きに来る。
            # 直しが効いていれば B は A の書き終わりまで待たされ、A の plan を
            # 読んでから自分の phase を足す。効いていなければ B が先に書いて、
            # A の古い読みに上書きされて phase が消える。
            thread_b.start()
            time.sleep(0.3)
        finally:
            release.set()
            thread_a.join(timeout=5)
            thread_b.join(timeout=5)
            cs.write = real_write
        assert not errors, errors
        data = cs.read("race")
        assert cs.plan_text(data.get("plan")) == "1/1 a", data
        assert data.get("phase") == "tool" and data.get("tool_name") == "Bash", data


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
