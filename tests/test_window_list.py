# tests/test_window_list.py
# window_list.py の純関数(絞り込み・引数解釈・窓の特定・重なり判定・整形)と、
# mcp_server の windows / capture window= の翻訳を確かめる。画面は出さず入力も送らない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_window_list.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import mcp_server  # noqa: E402
import window_list as wl  # noqa: E402


def win(hwnd, exe, title="t", rect=(0, 0, 100, 100), minimized=False, toolwindow=False,
        elevated=False, foreground=False, topmost=False, monitor=1):
    return {
        "hwnd": hwnd, "exe": exe, "pid": hwnd, "title": title, "cls": "C", "rect": None if minimized else rect,
        "monitor": monitor, "minimized": minimized, "topmost": topmost, "toolwindow": toolwindow,
        "elevated": elevated, "foreground": foreground,
    }


def test_parse_windows_args():
    assert wl.parse_windows_args([]) == (None, None, False, None)
    assert wl.parse_windows_args(["exe=chrome", "title=Docs", "--all"]) == ("chrome", "Docs", True, None)
    assert wl.parse_windows_args(["all=true"])[2] is True
    assert wl.parse_windows_args(["all=false"])[2] is False
    assert wl.parse_windows_args(["foo=1"])[3] is not None
    assert wl.parse_windows_args(["exe"])[3] is not None  # = が無い


def test_filter_default_excludes_minimized_tool_untitled():
    ws = [
        win(1, "a.exe"),
        win(2, "b.exe", minimized=True),
        win(3, "c.exe", toolwindow=True),
        win(4, "d.exe", title="  "),
    ]
    assert [w["hwnd"] for w in wl.filter_windows(ws)] == [1]
    assert [w["hwnd"] for w in wl.filter_windows(ws, show_all=True)] == [1, 2, 3, 4]


def test_filter_exe_title_substring_case_insensitive():
    ws = [win(1, "Chrome.exe", title="Inbox - Mail"), win(2, "code.exe", title="main.py")]
    assert [w["hwnd"] for w in wl.filter_windows(ws, exe="CHROM")] == [1]
    assert [w["hwnd"] for w in wl.filter_windows(ws, title="MAIN")] == [2]
    assert wl.filter_windows(ws, exe="chrome", title="main") == []


def test_parse_window_spec():
    assert wl.parse_window_spec("chrome") == ("exe", "chrome")
    assert wl.parse_window_spec("chrome.exe") == ("exe", "chrome.exe")
    assert wl.parse_window_spec("12345") == ("hwnd", 12345)
    assert wl.parse_window_spec("0x1F") == ("hwnd", 31)
    assert wl.parse_window_spec("0xZZ") is None
    assert wl.parse_window_spec("  ") is None


def test_resolve_by_exe_unique_and_ambiguous_and_missing():
    ws = [win(1, "chrome.exe"), win(2, "code.exe"), win(3, "chromedriver.exe")]
    target, cands, err = wl.resolve_window(ws, ("exe", "chrome"))
    assert target["hwnd"] == 1 and err is None  # 完全一致なので chromedriver は拾わない
    target, cands, err = wl.resolve_window(ws + [win(4, "Chrome.EXE")], ("exe", "chrome.exe"))
    assert target is None and [w["hwnd"] for w in cands] == [1, 4] and "hwnd" in err
    target, cands, err = wl.resolve_window(ws, ("exe", "nothing"))
    assert target is None and cands == [] and err
    # ツール窓・タイトル空は exe 指定では候補にしない
    target, _, _ = wl.resolve_window([win(1, "x.exe", toolwindow=True), win(2, "x.exe")], ("exe", "x"))
    assert target["hwnd"] == 2


def test_resolve_by_hwnd_includes_minimized():
    ws = [win(7, "a.exe", minimized=True)]
    target, _, err = wl.resolve_window(ws, ("hwnd", 7))
    assert target["hwnd"] == 7 and err is None
    assert wl.resolve_window(ws, ("hwnd", 8))[0] is None


def test_overlap_only_windows_in_front():
    target = win(10, "t.exe", rect=(0, 0, 100, 100))
    front = win(11, "front.exe", rect=(50, 0, 100, 100))      # 手前で半分重なる
    behind = win(12, "behind.exe", rect=(0, 0, 100, 100))     # 奥は写らない
    zorder = [front, target, behind]
    result = wl.find_overlaps(target, zorder)
    assert [w["hwnd"] for w in result] == [11]


def test_overlap_ignores_minimized_far_and_tiny():
    target = win(10, "t.exe", rect=(0, 0, 100, 100))
    ws = [
        win(1, "min.exe", minimized=True),
        win(2, "far.exe", rect=(500, 500, 100, 100)),
        win(3, "edge.exe", rect=(99, 99, 100, 100)),           # 1px だけ触れる(1/10000)
        win(4, "real.exe", rect=(90, 0, 100, 100)),            # 10%
        target,
    ]
    assert [w["hwnd"] for w in wl.find_overlaps(target, ws)] == [4]


def test_overlap_target_missing_rect_or_not_in_list():
    assert wl.find_overlaps(win(1, "a.exe", minimized=True), []) == []
    target = win(10, "t.exe")
    # Z順リストに target が居なければ全部が手前扱い(呼び出し側は必ず含めて渡す前提)
    assert len(wl.find_overlaps(target, [win(1, "a.exe")])) == 1


def test_format_overlaps_caps_list():
    assert wl.format_overlaps([]) == ""
    many = [win(i, f"e{i}.exe", title=f"w{i}") for i in range(8)]
    text = wl.format_overlaps(many)
    assert text.startswith("重なり: ") and "ほか3件" in text and "w0(e0.exe)" in text


def test_format_line_one_line_and_flags():
    line = wl.format_line(win(5, "a.exe", title="hello\nworld", foreground=True, topmost=True, elevated=None))
    assert "\n" not in line
    assert "hwnd=5" in line and "a.exe" in line and "rect=0,0,100,100" in line and "mon1" in line
    assert "前面" in line and "TOPMOST" in line and "権限不明(アクセス拒否)" in line
    mini = wl.format_line(win(6, "b.exe", minimized=True, elevated=True))
    assert "最小化" in mini and "admin" in mini
    assert len(wl.format_line(win(7, "c.exe", title="x" * 200))) < 200


def test_mcp_translation_windows():
    assert mcp_server._to_command("windows", {}) == ("windows", [])
    assert mcp_server._to_command("windows", {"exe": "chrome", "title": "Docs", "all": True}) == (
        "windows", ["exe=chrome", "title=Docs", "--all"])
    assert mcp_server.TOOLS_BY_NAME["windows"]["annotations"]["readOnlyHint"] is True


def test_mcp_translation_capture_window():
    assert mcp_server._to_command("capture", {"window": "chrome.exe"}) == ("capture", ["--window=chrome.exe"])
    for bad in ({"window": "a", "screen": 1}, {"window": "a", "select": True},
                {"window": "a", "x": 1, "y": 1, "width": 1, "height": 1}, {"window": "  "}):
        try:
            mcp_server._to_command("capture", bad)
        except ValueError:
            continue
        raise AssertionError(f"通ってしまった: {bad}")
    # 既存の呼び方は変わらない
    assert mcp_server._to_command("capture", {}) == ("capture", [])
    assert mcp_server._to_command("capture", {"screen": 2}) == ("capture", ["2"])


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
