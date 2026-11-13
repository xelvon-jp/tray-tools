# tests/test_editor_reformat.py
# テキストの整形(editor/reformat.py、Qt 非依存)を確かめる。
# 句点での改行、読点の桁数判定、Markdown の保護(フェンス・
# 見出し・表・リンク・インラインコード)、リストと引用の続き。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_reformat.py
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from editor import reformat  # noqa: E402

fmt = reformat.format_lines


def test_sentence_breaks_at_kuten():
    assert fmt(["今日は晴れです。明日も晴れでしょう。"], 80) == [
        "今日は晴れです。", "明日も晴れでしょう。"]
    # 文の終わりは ！ ？ も同じに扱う。
    assert fmt(["行きますか？行きます！"], 80) == ["行きますか？", "行きます！"]
    # 句点が無ければ1行のまま。
    assert fmt(["ただの文章"], 80) == ["ただの文章"]


def test_closers_stay_with_sentence():
    # 句点の後ろの閉じ括弧は、句点と同じ行に残る。
    assert fmt(["彼は「行きます。」と言った。次です。"], 80) == [
        "彼は「行きます。」", "と言った。", "次です。"]
    assert fmt(["（終わり。）次です。"], 80) == ["（終わり。）", "次です。"]


def test_paragraph_newlines_are_joined_first():
    # 段落の中の既存の改行はいったん取り除いてから整え直す。
    assert fmt(["今日は", "晴れです。次です。"], 80) == [
        "今日は晴れです。", "次です。"]
    # 英数字どうしの繋ぎ目だけ半角空白が入る。
    assert fmt(["abc", "def"], 80) == ["abc def"]
    assert fmt(["あい", "うえ"], 80) == ["あいうえ"]


def test_comma_break_only_over_column():
    # 桁数を超えなければ、読点では改行しない。
    assert fmt(["ああああ、ああ。"], 80) == ["ああああ、ああ。"]
    # 超えるときは、桁数に収まる最後の読点で改行する(全角は2桁)。
    assert fmt(["ああああ、ああああ、ああああ。"], 10) == [
        "ああああ、", "ああああ、", "ああああ。"]
    # 桁数の内側に読点が無ければ、最初の読点で改行する。
    assert fmt(["ああああああ、いい。"], 10) == ["ああああああ、", "いい。"]
    # 読点が無ければ、長くても改行できない。
    assert fmt(["ああああああああああ。"], 10) == ["ああああああああああ。"]
    # 半角は1桁で数える。
    assert fmt(["aaaa、bbbb、cccc。"], 10) == ["aaaa、", "bbbb、", "cccc。"]


def test_code_fence_is_untouched():
    lines = ["```", "中。です。", "改行もそのまま", "```", "", "外。です。"]
    assert fmt(lines, 80) == [
        "```", "中。です。", "改行もそのまま", "```", "", "外。", "です。"]
    # 言語指定つき、閉じが無いまま終わっても中身は触らない。
    lines = ["```python", "a。b", "c。d"]
    assert fmt(lines, 80) == lines
    # チルダのフェンスも同じ。
    lines = ["~~~", "中。です。", "~~~"]
    assert fmt(lines, 80) == lines


def test_heading_table_and_rule_are_untouched():
    # 見出し行の中では改行しない。
    assert fmt(["# 見出し。です"], 80) == ["# 見出し。です"]
    # 表(| で始まる行)の中では改行しない。
    assert fmt(["| a。b | c。d |"], 80) == ["| a。b | c。d |"]
    # 罫線はそのまま。
    assert fmt(["---", "本文。次。"], 80) == ["---", "本文。", "次。"]


def test_link_and_inline_code_are_protected():
    # リンク記法の中の句点では分けない。
    assert fmt(["[あ。い](https://example.com)です。次。"], 80) == [
        "[あ。い](https://example.com)です。", "次。"]
    assert fmt(["![絵。](a.png)を見た。次。"], 80) == [
        "![絵。](a.png)を見た。", "次。"]
    # インラインコードの中の句点では分けない。
    assert fmt(["`a。b`です。次。"], 80) == ["`a。b`です。", "次。"]
    # 保護範囲の中の読点では改行しない(範囲の外で文が終わる)。
    text = "[ああああ、ああああ](https://example.com)です。"
    assert fmt([text], 10) == [text]


def test_list_continuation_indent():
    # 文ごとの改行では、続きの行が内容の開始位置まで字下げされる。
    assert fmt(["- 一文目。二文目。"], 80) == ["- 一文目。", "  二文目。"]
    assert fmt(["1. 一文目。二文目。"], 80) == ["1. 一文目。", "   二文目。"]
    # 読点の改行でも同じ字下げ。
    assert fmt(["- ああああ、ああああ。"], 10) == [
        "- ああああ、", "  ああああ。"]
    # 続きの行(次の行に書かれた分)は項目の中身として繋がる。
    assert fmt(["- 一文目。", "二文目。", "- 別の項目。"], 80) == [
        "- 一文目。", "  二文目。", "- 別の項目。"]
    # インデントのある(入れ子の)項目は、インデントを保つ。
    assert fmt(["- 親。一文。", "  - 子。一文。二文。"], 80) == [
        "- 親。", "  一文。", "  - 子。", "    一文。", "    二文。"]


def test_quote_continuation_prefix():
    assert fmt(["> 今日は。次です。"], 80) == ["> 今日は。", "> 次です。"]
    # 読点の改行でも、続きの行に引用記号が付く。
    assert fmt(["> ああああ、ああああ。"], 10) == [
        "> ああああ、", "> ああああ。"]
    # 引用の中の空行は、記号だけの行になる。
    assert fmt(["> 一段目。", ">", "> 二段目。"], 80) == [
        "> 一段目。", ">", "> 二段目。"]
    # 引用の中のリストも整う。
    assert fmt(["> - 一文目。二文目。"], 80) == [
        "> - 一文目。", ">   二文目。"]


def test_blank_lines_collapse_and_blocks_stay_separate():
    assert fmt(["前の文。", "", "", "", "後の文。"], 80) == [
        "前の文。", "", "後の文。"]
    # 頭と末尾の空行は落とす。
    assert fmt(["", "", "文。", ""], 80) == ["文。"]
    # 空の入力は空のまま。
    assert fmt([], 80) == []
    assert fmt(["", ""], 80) == []


def test_indented_code_block_is_untouched():
    lines = ["    a。b", "    c。d"]
    assert fmt(lines, 80) == lines


def test_no_leading_indent_on_paragraphs():
    # 段落の行頭に字下げは入らない(元の字下げも取り除かれる)。
    assert fmt(["  字下げされた文。次の文。"], 80) == [
        "字下げされた文。", "次の文。"]


def test_display_width_counts_fullwidth_as_two():
    assert reformat.display_width("あa") == 3
    assert reformat.display_width("Ａ1") == 3


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
