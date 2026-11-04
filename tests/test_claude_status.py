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


def test_tray_tooltip_helpers():
    import feature_screen as fs
    assert fs._join_state("Rapture", "Claude 実行中") == "Rapture（Claude 実行中）"
    assert fs._join_state("Rapture（スリープ抑止中）", "Claude 確認待ち") == \
        "Rapture（スリープ抑止中・Claude 確認待ち）"
    long_lines = ["Rapture（Claude 実行中）"] + ["あ" * 40] * 4
    text = fs._fit_tooltip(long_lines)
    assert len(text) <= fs.TOOLTIP_MAX_CHARS and text.startswith("Rapture（Claude 実行中）")
    assert text.count("\n") == 2   # 収まらない行は後ろから落とす


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
