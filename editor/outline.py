# editor/outline.py
# アウトライン(見出し行)の抽出と階層付け。Qt に依存しない純粋関数。
#
# 既定の拾い方は2種類:
#   (a) 行頭(空白の後でもよい)に箇条書きの記号が来る行
#   (b) Markdown の見出し(# から ######)。階層は # の数で決める
# 階層の決め方:
#   - 見出しは # の数がそのまま階層
#   - 箇条書きは「直前に現れた見出しの階層 + 1 + 字下げの段数」。
#     見出しがまだ無い所では、字下げの段数だけで決める(1 + 段数)
#   - 字下げの段数は桁で数える: 半角スペース1つ = 1桁、全角スペース
#     (U+3000)1つ = 2桁、タブ1つ = 2桁(= 1段)。2桁で1段
# 既定パターンはこの2種を名前付きグループ(marks / symbol / indent)で
# 表してあり、階層の決め方もグループから読む。利用者が設定で別の
# 正規表現に変えたときは、名前付きグループが無ければ行頭の空白の
# 深さだけで決める(相対的な深さだけ分かれば、親子の決まり方は
# 壊れない)。
#
# 親子は「直前にある、より浅い階層の行」を親にする(build_outline_tree)。
#
# 遅い正規表現(^(a+)+$ のような破滅的なバックトラック)への制限:
#   各行の match は先頭 MATCH_HEAD_CHARS 字だけを見る。長い行の末尾に
#   意地の悪い並びがあっても、見る範囲が頭で切れるので固まらない。
#   ただし範囲の中で完結する意地の悪い並びはこれだけでは防げないので、
#   設定ダイアログ側でも試験してから保存する(window.py の試験)。
import re
import time
from dataclasses import dataclass, field

# 記号の行: indent のあとに記号。Markdown: indent のあとに # が1〜6個で、
# 後ろに空白が要る(「#見出し」のように空白が無いものは見出しにしない)。
# indent には全角スペース(U+3000)も含める(日本語の文書の字下げ)。
DEFAULT_OUTLINE_PATTERN = (
    r"^(?P<indent>[ \t\u3000]*)"
    r"(?:(?P<symbol>[■□●○◆◇▲△▼▽▶▷・])|(?P<marks>#{1,6})(?=[ \t\u3000]))"
)
# 強調表示する行: 行頭(空白の後でもよい)に星印。
DEFAULT_HIGHLIGHT_PATTERN = r"^[ \t\u3000]*[★☆]"

DEFAULT_OUTLINE_RX = re.compile(DEFAULT_OUTLINE_PATTERN)
DEFAULT_HIGHLIGHT_RX = re.compile(DEFAULT_HIGHLIGHT_PATTERN)

# 字下げの数え方: 2桁で1段。半角スペースは1桁、全角スペースとタブは
# 2桁(タブ1つ = 1段)。
INDENT_PER_LEVEL = 2
SPACE_COLUMNS = 1
FULLWIDTH_SPACE_COLUMNS = 2
TAB_COLUMNS = 2

# 各行の match で見る先頭の長さ(遅い正規表現への制限)。
MATCH_HEAD_CHARS = 200

# アウトラインの項目数の上限。大きい文書(ログ等)で、拾う件数も
# ツリーの構築も際限なく重くならないための打ち切り。超えたら
# 呼び側が「打ち切った」ことを表示に出す。
OUTLINE_ENTRY_LIMIT = 5000

# 設定ダイアログの試験: この秒数を超えたら「遅すぎる」として保存しない。
TRIAL_TIME_LIMIT_SEC = 1.0


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


def _indent_columns(indent: str) -> int:
    """行頭の空白を桁に数える(半角1、全角スペースとタブは2)。"""
    columns = 0
    for ch in indent:
        if ch == "\t":
            columns += TAB_COLUMNS
        elif ch == "\u3000":
            columns += FULLWIDTH_SPACE_COLUMNS
        else:
            columns += SPACE_COLUMNS
    return columns


def _indent_steps(indent: str) -> int:
    """行頭の空白の深さを段数にする。"""
    return _indent_columns(indent) // INDENT_PER_LEVEL


def _leading_indent(line: str) -> str:
    """行頭の空白(半角・タブ・全角スペース)の部分を返す。"""
    return line[:len(line) - len(line.lstrip(" \t\u3000"))]


def extract_outline(text: str, pattern=None) -> list:
    """text から見出し行を拾い、OutlineEntry の並び(文書順)を返す。

    pattern は正規表現の文字列かコンパイル済み。None なら既定の拾い方。
    各行の先頭 MATCH_HEAD_CHARS 字で match するものだけを見出しとする。"""
    entries, _truncated = extract_outline_ex(text, pattern)
    return entries


def extract_outline_ex(text: str, pattern=None, limit=None):
    """extract_outline の拡張版。(見出しの並び, 打ち切ったか) を返す。

    limit が正の数なら、その件数で拾うのを打ち切る。打ち切ったかは、
    残りの行にもう1件でも見出しがあるかで正確に判定する(最後の
    1件がたまたま上限と一致しただけなら打ち切りではない)。"""
    rx = _as_regex(pattern)
    entries = []
    truncated = False
    last_heading_level = 0  # 直前に現れた見出し(#)の階層。0 = まだ無い
    for number, line in enumerate(text.split("\n")):
        match = rx.match(line[:MATCH_HEAD_CHARS])
        if match is None:
            continue
        if limit is not None and len(entries) >= limit:
            truncated = True
            break
        groups = match.groupdict()
        marks = groups.get("marks")
        if marks:
            level = len(marks)
            last_heading_level = level
        elif groups.get("symbol") is not None:
            # 箇条書きは、直前の見出しの下にぶら下げる。
            level = (last_heading_level + 1
                     + _indent_steps(groups.get("indent") or ""))
        else:
            # 名前付きグループの無いカスタムパターンは、その行自身の
            # 行頭の空白の深さだけで決める。
            indent = groups.get("indent")
            if indent is None:
                indent = _leading_indent(line)
            level = 1 + _indent_steps(indent)
        entries.append(OutlineEntry(
            line=number, level=level, text=line.strip()))
    return entries, truncated


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


def adversarial_samples():
    """遅い正規表現が引っかかる、意地の悪い試験文字列の並び。

    設定ダイアログの試験で使う。当たりやすい字('a')の長い並びに、
    当たらない字を末尾へ付けたものが中心(^(a+)+$ のような式は、
    失敗が確定するまで組み合わせを試し尽くすので遅くなる)。
    実際の適用と同じく、先頭 MATCH_HEAD_CHARS 字だけが対象。"""
    samples = [
        "a" * 40 + "b",
        "a" * 40,
        "a" * 39 + "ab",
        " " * 20 + "a" * 30 + "!",
        "x" * 60 + "y",
        "ab" * 30 + "c",
        "a" * 300 + "b",  # 先頭制限で頭だけが対象になる長い行
        "■ 見出しの行",
        "★ 強調の行",
        "ただの本文の行です",
    ]
    return [sample[:MATCH_HEAD_CHARS] for sample in samples]


def run_pattern_trial(patterns) -> float:
    """patterns(正規表現の文字列の並び)を試験文字列全部に match し、
    かかった秒数を返す。不正な式は re.error を上げる。

    設定ダイアログの試験の実体。窓の側はこれを別プロセスで呼ぶ
    (window.trial_patterns)。ここ自体は遅い式では戻ってこないので、
    直接呼ぶのは安全と分かっている式だけにすること。"""
    samples = adversarial_samples()
    start = time.perf_counter()
    for pattern in patterns:
        rx = re.compile(pattern)
        for sample in samples:
            rx.match(sample)
    return time.perf_counter() - start
