# tests/test_editor_rxfind.py
# 正規表現の検索・置換(editor/rxfind.py)を確かめる。実処理は別
# プロセスで走るので、不正な式のエラーと、固まる式の時間切れも
# ここで見る。Qt には触らない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_rxfind.py
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from editor import rxfind  # noqa: E402


def test_find_next_prev_regex():
    text = "foo 123 bar 45 baz"
    result = rxfind.run_operation("find_next", text=text, pattern=r"\d+",
                                  start=0)
    assert result["span"] == (4, 7)
    result = rxfind.run_operation("find_next", text=text, pattern=r"\d+",
                                  start=7)
    assert result["span"] == (12, 14)
    result = rxfind.run_operation("find_next", text=text, pattern=r"\d+",
                                  start=14)
    assert result["span"] is None
    result = rxfind.run_operation("find_prev", text=text, pattern=r"\d+",
                                  start=len(text))
    assert result["span"] == (12, 14)
    result = rxfind.run_operation("find_prev", text=text, pattern=r"\d+",
                                  start=12)
    assert result["span"] == (4, 7)


def test_find_case_flag():
    text = "Foo foo"
    result = rxfind.run_operation("find_next", text=text, pattern="foo",
                                  start=0, case_sensitive=False)
    assert result["span"] == (0, 3)
    result = rxfind.run_operation("find_next", text=text, pattern="foo",
                                  start=0, case_sensitive=True)
    assert result["span"] == (4, 7)


def test_replace_one_with_group_reference():
    result = rxfind.run_operation(
        "replace_one", text="2026-11-13", pattern=r"(\d+)-(\d+)-(\d+)",
        repl=r"\1/\2/\3", start=0)
    assert result["span"] == (0, 10)
    assert result["replacement"] == "2026/11/13"


def test_replace_all_with_group_reference():
    result = rxfind.run_operation(
        "replace_all", text="a1 b22 c333", pattern=r"(\d+)",
        repl=r"[\1]")
    assert result["text"] == "a[1] b[22] c[333]"
    assert result["count"] == 3


def test_plain_mode_is_literal():
    # regex=False では、パターンの記号も置換文字列も文字どおり。
    result = rxfind.run_operation(
        "replace_all", text="a.c aqc a.c", pattern="a.c",
        repl=r"\1", regex=False)
    assert result["text"] == r"\1 aqc \1"
    assert result["count"] == 2


def test_invalid_pattern_is_fault():
    try:
        rxfind.run_operation("find_next", text="abc", pattern="(", start=0)
    except rxfind.RegexFault as e:
        assert "不正" in str(e)
    else:
        raise AssertionError("RegexFault になるはず")


def test_invalid_replacement_is_fault():
    try:
        rxfind.run_operation(
            "replace_all", text="abc", pattern=r"(a)", repl=r"\9")
    except rxfind.RegexFault as e:
        assert "置換文字列" in str(e)
    else:
        raise AssertionError("RegexFault になるはず")


def test_catastrophic_pattern_times_out():
    # 破滅的なバックトラックの式は、時間切れの失敗になる(固まらない)。
    try:
        rxfind.run_operation(
            "find_next", text="a" * 28 + "b", pattern=r"^(a+)+$",
            start=0, timeout=1.0)
    except rxfind.RegexFault as e:
        assert "時間がかかりすぎ" in str(e)
    else:
        raise AssertionError("RegexFault になるはず")


def test_pure_helpers():
    rx = rxfind.compile_search(r"\d+", regex=True)
    assert rxfind.find_span_next("ab 12 cd", rx, 0) == (3, 5)
    assert rxfind.find_span_prev("ab 12 cd", rx, 8) == (3, 5)
    # 空マッチは同じ場所で止まらない。
    rx_empty = rxfind.compile_search(r"x*", regex=True)
    span = rxfind.find_span_next("ab", rx_empty, 0)
    assert span is not None and span[0] > 0 or span == (0, 0)
    new_text, count = rxfind.replace_all_text(
        "a1b2", rxfind.compile_search(r"(\d)"), r"<\1>")
    assert new_text == "a<1>b<2>" and count == 2
    try:
        rxfind.compile_search("(", regex=True)
    except rxfind.RegexFault:
        pass
    else:
        raise AssertionError("RegexFault になるはず")


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
