# editor/outline.py
# アウトライン(見出し行)の抽出と階層付け。Qt に依存しない純粋関数。
#
# 既定の拾い方は2種類:
#   (a) 行頭(空白の後でもよい)に箇条書きの記号が来る行。階層は行頭の
#       空白の深さで決める
#   (b) Markdown の見出し(# から ######)。階層は # の数で決める
# 既定パターンはこの2種を名前付きグループで表してあり、階層の決め方も
# グループから読む。利用者が設定で別の正規表現に変えたときは、名前付き
# グループが無ければ行頭の空白の深さに倒す(相対的な深さだけ分かれば、
# 親子の決まり方は壊れない)。
#
# 親子は「直前にある、より浅い階層の行」を親にする(build_outline_tree)。
import re
from dataclasses import dataclass, field

# 記号の行: indent のあとに記号。Markdown: indent のあとに # が1〜6個で、
# 後ろに空白が要る(「#見出し」のように空白が無いものは見出しにしない)。
DEFAULT_OUTLINE_PATTERN = (
    r"^(?P<indent>[ \t]*)"
    r"(?:(?P<symbol>[■□●○◆◇▲△▼▽▶▷・])|(?P<marks>#{1,6})(?=[ \t]))"
)
# 強調表示する行: 行頭(空白の後でもよい)に星印。
DEFAULT_HIGHLIGHT_PATTERN = r"^[ \t]*[★☆]"

DEFAULT_OUTLINE_RX = re.compile(DEFAULT_OUTLINE_PATTERN)
DEFAULT_HIGHLIGHT_RX = re.compile(DEFAULT_HIGHLIGHT_PATTERN)

# 階層への数え方: 空白はタブを4桁に広げ、2桁で1段とする。
TAB_WIDTH = 4
INDENT_PER_LEVEL = 2


@dataclass
class OutlineEntry:
    line: int   # 0始まりの行番号(ジャンプ先)
    level: int  # 階層。1始まりで、浅いほど小さい
    text: str   # 表示用(前後の空白を除いた行)


@dataclass
class OutlineNode:
    entry: OutlineEntry
    children: list = field(default_factory=list)


def compile_pattern(pattern: str):
    """正規表現の文字列をコンパイルする。不正なら re.error を上げる。"""
    return re.compile(pattern)


def _as_regex(pattern):
    """pattern が None なら既定、文字列ならコンパイル済みとして扱う。"""
    if pattern is None:
        return DEFAULT_OUTLINE_RX
    if hasattr(pattern, "match"):
        return pattern
    return re.compile(pattern)


def _indent_level(indent: str) -> int:
    """行頭の空白の深さから階層(1始まり)を決める。"""
    return 1 + len(indent.expandtabs(TAB_WIDTH)) // INDENT_PER_LEVEL


def _level_of(match, line: str) -> int:
    groups = match.groupdict()
    marks = groups.get("marks")
    if marks:
        return len(marks)
    indent = groups.get("indent")
    if indent is None:
        # カスタムパターン(名前付きグループが無い)ときは、その行自身の
        # 行頭の空白で決める。
        indent = line[:len(line) - len(line.lstrip(" \t"))]
    return _indent_level(indent)


def extract_outline(text: str, pattern=None) -> list:
    """text から見出し行を拾い、OutlineEntry の並び(文書順)を返す。

    pattern は正規表現の文字列かコンパイル済み。None なら既定の拾い方。
    各行の先頭で match するものだけを見出しとする。"""
    rx = _as_regex(pattern)
    entries = []
    for number, line in enumerate(text.split("\n")):
        match = rx.match(line)
        if match is not None:
            entries.append(OutlineEntry(
                line=number, level=_level_of(match, line),
                text=line.strip()))
    return entries


def build_outline_tree(entries) -> list:
    """見出しの並びを親子の木にする。親は「直前にある、より浅い階層の
    行」。浅い行が現れたら、同じか深い階層のものを積みから下ろす。"""
    roots = []
    stack = []
    for entry in entries:
        node = OutlineNode(entry=entry)
        while stack and stack[-1].entry.level >= entry.level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots
