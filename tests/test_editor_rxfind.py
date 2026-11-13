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


def test_replace_all_multiline_anchor():
    # ^ と $ は行ごとに効く(MULTILINE)。文書全体にだけ効くと、
    # 「行頭に > を付ける」が最初の行にしか効かない(レビュー
    # req-20261113-107 の指摘)。子プロセス側・純粋関数側の両方。
    result = rxfind.run_operation(
        "replace_all", text="a\nb\nc", pattern="^", repl="> ")
    assert result["text"] == "> a\n> b\n> c"
    assert result["count"] == 3
    result = rxfind.run_operation(
        "replace_all", text="a\nb\nc", pattern="$", repl=";")
    assert result["text"] == "a;\nb;\nc;"
    rx = rxfind.compile_search("^", regex=True)
    new_text, count = rxfind.replace_all_text("x\ny", rx, "# ")
    assert new_text == "# x\n# y" and count == 2
    # 行頭の検索も、2行目以降で当たる。
    span = rxfind.run_operation(
        "find_next", text="ab\ncd\nef", pattern="^e", start=0)["span"]
    assert span == (6, 7), span


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


def test_dot_does_not_cross_newlines():
    # . は改行に当たらない(通常のエディタの作法)。行をまたいだ
    # マッチが返ると、複数行がまるごと選択されてしまう。
    result = rxfind.run_operation("find_next", text="a\nb",
                                  pattern="a.b", start=0)
    assert result["span"] is None
    result = rxfind.run_operation("find_next", text="xa by",
                                  pattern="a.b", start=0)
    assert result["span"] == (1, 4)


def test_run_operation_with_non_utf8_stdio():
    # 子プロセスの標準入出力の文字コードが OS の既定(日本語
    # Windows では cp932)になっている環境でも、ヒット位置が
    # ずれないこと。親は UTF-8 で送るので、子が既定のまま読み
    # 書きすると日本語が化けて文字数が変わり、無関係の文字が
    # ヒットする(実機で起きた不具合)。PYTHONIOENCODING は
    # プロセスの起動時にだけ効くので、子の起動環境に入れる
    # ことで日本語 Windows の状態を再現する。
    text = "最初の行です\n次の行に 1 があります\ntest を含む行です\n最後の行\n"
    old = os.environ.get("PYTHONIOENCODING")
    os.environ["PYTHONIOENCODING"] = "cp932"
    try:
        result = rxfind.run_operation("find_next", text=text,
                                      pattern="1", start=0)
        begin = text.index("1")
        assert result["span"] == (begin, begin + 1), result
        result = rxfind.run_operation("find_next", text=text,
                                      pattern="^.*test.*$", start=0)
        line = "test を含む行です"
        begin = text.index(line)
        assert result["span"] == (begin, begin + len(line)), result
        result = rxfind.run_operation("replace_all", text=text,
                                      pattern="行", repl="ギョウ")
        assert result["text"] == text.replace("行", "ギョウ")
        assert result["count"] == 4
    finally:
        if old is None:
            os.environ.pop("PYTHONIOENCODING", None)
        else:
            os.environ["PYTHONIOENCODING"] = old


def test_find_all_spans_and_limit():
    rx = rxfind.compile_search(r"\d+", regex=True)
    spans, truncated = rxfind.find_all_spans("a1 b22 c333", rx, 100)
    assert spans == [(1, 2), (4, 6), (8, 11)]
    assert not truncated
    spans, truncated = rxfind.find_all_spans("a1 b22 c333", rx, 2)
    assert spans == [(1, 2), (4, 6)]
    assert truncated
    # ちょうど上限で終わるときは打ち切りではない。
    spans, truncated = rxfind.find_all_spans("a1 b22", rx, 2)
    assert spans == [(1, 2), (4, 6)]
    assert not truncated


def test_find_all_operation():
    result = rxfind.run_operation("find_all", text="x1 y22 z333",
                                  pattern=r"\d+", regex=True, limit=100)
    assert result["spans"] == [(1, 2), (4, 6), (8, 11)]
    assert result["truncated"] is False
    result = rxfind.run_operation("find_all", text="x1 y22 z333",
                                  pattern=r"\d+", regex=True, limit=1)
    assert result["spans"] == [(1, 2)]
    assert result["truncated"] is True


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
