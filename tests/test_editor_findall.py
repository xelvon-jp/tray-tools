# tests/test_editor_findall.py
# 文書内のすべて検索(editor/findall.py の純粋部分)を確かめる。
# Qt には触らない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_findall.py
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

from editor import findall as findallmod  # noqa: E402
from editor import rxfind  # noqa: E402

TEXT = "1行目\n探す語 あり\n3行目\nまた探す語と探す語\n"


def test_find_all_lists_lines_and_count():
    result = findallmod.find_all(TEXT, "探す語")
    assert [(m.line, m.text) for m in result.matches] == [
        (2, "探す語 あり"), (4, "また探す語と探す語")]
    # 1行に2回ヒットしても、一覧に出るのはその行1件。
    assert len(result.matches) == 2
    assert not result.truncated


def test_find_all_case_sensitivity():
    text = "Foo\nfoo\nFOO\n"
    result = findallmod.find_all(text, "foo")
    assert [m.line for m in result.matches] == [1, 2, 3]
    result = findallmod.find_all(text, "foo", case_sensitive=True)
    assert [m.line for m in result.matches] == [2]


def test_find_all_regex_and_literal():
    text = "abc\na.c\naxc\n"
    # 文字どおりなら、正規表現のメタ文字はそのまま探す。
    result = findallmod.find_all(text, "a.c")
    assert [m.line for m in result.matches] == [2]
    # 正規表現なら . が任意の1字に効く。
    result = findallmod.find_all(text, "a.c", regex=True)
    assert [m.line for m in result.matches] == [1, 2, 3]
    # ^ は行の頭に効く(行ごとに探すため)。
    result = findallmod.find_all("xa\nax\n", "^a", regex=True)
    assert [m.line for m in result.matches] == [2]


def test_find_all_invalid_regex():
    try:
        findallmod.find_all(TEXT, "(", regex=True)
    except rxfind.RegexFault as e:
        assert "正規表現が不正です" in str(e)
    else:
        raise AssertionError("不正な正規表現で RegexFault にならなかった")


def test_find_all_no_hit_and_empty():
    result = findallmod.find_all(TEXT, "無い語")
    assert result.matches == [] and not result.truncated
    assert findallmod.find_all(TEXT, "").matches == []
    assert findallmod.find_all("", "探す語").matches == []


def test_find_all_truncation():
    text = "語\n" * 10
    result = findallmod.find_all(text, "語", max_results=3)
    assert [m.line for m in result.matches] == [1, 2, 3]
    assert result.truncated


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
