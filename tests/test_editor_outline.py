# tests/test_editor_outline.py
# アウトラインの抽出と階層付け(editor/outline.py、Qt 非依存)を確かめる。
# 記号行の階層・Markdown の階層・親の決まり方・ジャンプ先の行番号と、
# 既定パターンとカスタムパターンの効き、星印パターンの一致判定。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_outline.py
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from editor import outline  # noqa: E402


def lines_levels(entries):
    return [(e.line, e.level) for e in entries]


def test_symbol_headings_and_levels():
    text = "■ 大見出し\n本文です\n  ■ 中見出し\n    ● 小見出し\n・ 記号は丸でもよい"
    entries = outline.extract_outline(text)
    assert lines_levels(entries) == [(0, 1), (2, 2), (3, 3), (4, 1)], entries
    assert entries[0].text == "■ 大見出し"
    assert entries[2].text == "● 小見出し"


def test_symbol_variety():
    text = "□ a\n○ b\n◆ c\n◇ d\n▲ e\n△ f\n▼ g\n▽ h\n▶ i"
    entries = outline.extract_outline(text)
    assert len(entries) == 9, entries
    assert all(e.level == 1 for e in entries)


def test_tab_indent_level():
    # タブは4桁に広げて2桁1段 → 階層3(4スペースと同じ)。
    entries = outline.extract_outline("\t■ タブ\n    ■ 4スペース")
    assert lines_levels(entries) == [(0, 3), (1, 3)], entries


def test_markdown_headings():
    text = "# 章\n本文\n## 節\n### 項\n###### 最深\n####### 7つは見出しでない\n#空白なしは見出しでない"
    entries = outline.extract_outline(text)
    assert lines_levels(entries) == [(0, 1), (2, 2), (3, 3), (4, 6)], entries
    assert entries[1].text == "## 節"


def test_non_heading_lines_are_skipped():
    text = "ただの文\n行の途中 ■ は見出しでない\n- リスト記号は対象外\n"
    assert outline.extract_outline(text) == []


def test_tree_parent_rule():
    text = ("■ a\n  ■ b\n    ■ c\n  ■ d\n■ e\n"
            "      ■ f\n■ g")
    roots = outline.build_outline_tree(outline.extract_outline(text))
    assert [n.entry.text for n in roots] == ["■ a", "■ e", "■ g"]
    a, e, g = roots
    assert [n.entry.text for n in a.children] == ["■ b", "■ d"]
    assert [n.entry.text for n in a.children[0].children] == ["■ c"]
    # e の直後の深い行 f は、浅い行が間になくても e の子になる。
    assert [n.entry.text for n in e.children] == ["■ f"]
    assert g.children == []
    # ジャンプ先は 0 始まりの行番号。
    assert a.children[0].children[0].entry.line == 2
    assert e.children[0].entry.line == 5


def test_mixed_markdown_and_symbol_tree():
    text = "# 章\n  ■ 節のつもり\n## 節\n■ 記号の大見出し"
    roots = outline.build_outline_tree(outline.extract_outline(text))
    assert [n.entry.text for n in roots] == ["# 章", "■ 記号の大見出し"]
    assert [n.entry.text for n in roots[0].children] == ["■ 節のつもり", "## 節"]


def test_custom_pattern_uses_indent_levels():
    entries = outline.extract_outline(
        "1. 見出し\n  2. 子\nただの文\n3. 次", pattern=r"^\s*\d+\.\s")
    assert lines_levels(entries) == [(0, 1), (1, 2), (3, 1)], entries


def test_custom_pattern_compiled_object():
    rx = outline.compile_pattern(r"^第.+章")
    entries = outline.extract_outline("第一章 始まり\n本文\n第二章 続き", rx)
    assert [e.line for e in entries] == [0, 2]


def test_invalid_pattern_raises():
    try:
        outline.compile_pattern("[abc")
    except re.error:
        pass
    else:
        raise AssertionError("不正な正規表現で re.error が上がりませんでした")


def test_highlight_default_pattern():
    rx = outline.DEFAULT_HIGHLIGHT_RX
    assert rx.match("★ 重要")
    assert rx.match("  ☆ メモ")
    assert rx.match("\t★ タブの後")
    assert not rx.match("行の途中 ★ は対象外")
    assert not rx.match("✦ これは星印でない")
    # 既定の文字列からコンパイルしても同じ判定になる。
    rx2 = re.compile(outline.DEFAULT_HIGHLIGHT_PATTERN)
    assert rx2.match("★ 重要")


def test_default_patterns_are_valid_regex():
    re.compile(outline.DEFAULT_OUTLINE_PATTERN)
    re.compile(outline.DEFAULT_HIGHLIGHT_PATTERN)


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
