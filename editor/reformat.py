# editor/reformat.py
# テキストの整形。音声入力の長文を読みやすくするため、段落の中の
# 改行はいったん取り除き、句点で文ごとに改行し直す。行が折り返し
# の桁数を超えるときだけ、読点でも改行する。Markdown として壊れ
# ないよう、コードフェンス・見出し・表・リンクと画像の記法・
# インラインコードの中では改行しない。リストと引用は、続きの行が
# そろう形(リストは内容の開始位置まで字下げ、引用は記号を付け直
# す)で出す。段落の行頭に字下げは入れない(Markdown でコード
# ブロック扱いになるため)。
#
# このファイルは Qt に依存しない純粋関数。窓(editor/window.py)は
# 対象の行範囲(改行を含まない行の列)を渡し、返った行の列で置き
# 換える。挿入される改行は、窓側の置き換えの作法(行範囲の置換)
# により文書の改行設定に従う。
#
# 仕様に書き切れなかった分として決めたこと:
#   - 文の終わりは「。」のほか「！」「？」も同じに扱う(日本語の
#     文の終わりとして同格なため)。読点は「、」だけ。
#   - 桁数は unicodedata の東アジア幅で数える(幅 W / F の文字は
#     2桁、それ以外は1桁)。
#   - 空行の並びは空行1つにそろえる。対象範囲の頭と末尾の空行は
#     落とす(行範囲の置換という窓側の作法に合わせる)。
#   - インデントされたコードブロック(塊の全行が半角空白4つ以上
#     かタブで始まる)は、そのまま触らない。
#   - 引用の遅延継続(「>」の無い行が引用の段落を続ける書き方)
#     は扱わない。「>」の無い行は引用の外の段落として扱う。
#   - 入れ子のリストは、インデントの深い項目も独立した項目とし
#     て扱う(インデントは保持するので、見た目の構造は保たれる)。
#   - 段落の行の繋ぎ目では、両側が英数字のときだけ半角空白を
#     1つ入れる(単語がくっついて意味が変わるのを防ぐ)。
import re
import unicodedata

_SENTENCE_ENDS = "。！？"
# 句点の後ろに続けて同じ行に残す閉じ括弧・引用符。
_CLOSERS = "」』】〕〉》）｝”’" + ")]}\"'"
_COMMA = "、"

_FENCE_RX = re.compile(r"^(\s{0,3})(`{3,}|~{3,})(.*)$")
_HEADING_RX = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_TABLE_RX = re.compile(r"^\s{0,3}\|")
_LIST_RX = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])([ \t]+)(.*)$")
_QUOTE_RX = re.compile(r"^\s{0,3}>[ \t]?")
_HR_RX = re.compile(r"^\s{0,3}([-*_])([ \t]*\1){2,}[ \t]*$")
_LINK_RX = re.compile(
    r"!?\[[^\]]*\]\([^)]*\)"                    # [文](URL) / ![代替](URL)
    r"|!?\[[^\]]*\]\[[^\]]*\]"                  # [文][参照]
    r"|<[A-Za-z][A-Za-z0-9+.\-]*:[^<>\s]*>"     # <https://…> の自動リンク
    r"|<[^<>\s]+@[^<>\s]+>"                     # <メールアドレス>
)


def char_width(ch):
    """表示の桁数。全角(東アジア幅 W / F)は2、それ以外は1。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(ch) for ch in text)


# ---------------------------------------------------------------
# 改行しない範囲(インラインコードとリンク)の保護
# ---------------------------------------------------------------
def _code_spans(text):
    """バッククォートの並びで囲まれた範囲 (開始, 終了) の列。
    閉じが無い並びは通常の文字として扱う。"""
    spans = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        run = j - i
        close = None
        k = j
        while k < n:
            if text[k] == "`":
                last = k
                while last < n and text[last] == "`":
                    last += 1
                if last - k == run:
                    close = last
                    break
                k = last
            else:
                k += 1
        if close is None:
            i = j
        else:
            spans.append((i, close))
            i = close
    return spans


def _protected_mask(text):
    """文字ごとに「改行を入れてはいけない範囲か」の印(0/1)。
    インラインコードの中と、リンク・画像の記法全体が対象。"""
    mask = bytearray(len(text))
    spans = _code_spans(text)
    for m in _LINK_RX.finditer(text):
        start, end = m.span()
        # コードの中にあるものはリンクではないので重なりは捨てる。
        if any(start < c_end and c_start < end for c_start, c_end in spans):
            continue
        spans.append((start, end))
    for start, end in spans:
        for i in range(start, end):
            mask[i] = 1
    return mask


# ---------------------------------------------------------------
# 文への分割と、読点での改行
# ---------------------------------------------------------------
def _split_sentences(text):
    """句点(。！？)で文に分ける。句点の後ろに閉じ括弧や引用符が
    続く場合は、それらも同じ文に残す。保護範囲の中の句点では
    分けない。"""
    mask = _protected_mask(text)
    sentences = []
    start = 0
    i, n = 0, len(text)
    while i < n:
        if text[i] in _SENTENCE_ENDS and not mask[i]:
            j = i + 1
            while j < n and text[j] in _CLOSERS and not mask[j]:
                j += 1
            sentences.append(text[start:j])
            start = j
            i = j
        else:
            i += 1
    if start < n:
        sentences.append(text[start:])
    return [s for s in sentences if s]


def _wrap_at_commas(sentence, budget):
    """1文を、桁数(budget)を超えるときだけ読点で改行した行の列
    にする。budget 以内に読点が無ければ、最初の読点で改行する
    (読点自体が無ければ、そのまま1行)。「。」だけのような残り
    しか生まれない位置では改行しない。"""
    if budget <= 0 or display_width(sentence) <= budget:
        return [sentence]
    lines = []
    rest = sentence
    while display_width(rest) > budget:
        mask = _protected_mask(rest)
        cut = None
        width = 0
        for i, ch in enumerate(rest):
            w = char_width(ch)
            if width + w > budget:
                break
            width += w
            if ch == _COMMA and not mask[i]:
                cut = i + 1
        if cut is None:
            for i, ch in enumerate(rest):
                if ch == _COMMA and not mask[i]:
                    cut = i + 1
                    break
        if cut is None:
            break
        if not rest[cut:].strip(_SENTENCE_ENDS + _CLOSERS):
            break
        lines.append(rest[:cut])
        rest = rest[cut:]
    lines.append(rest)
    return lines


def _break_text(text, budget):
    """文字列を、文ごと(＋長ければ読点)の行の列にする。"""
    out = []
    for sentence in _split_sentences(text):
        out.extend(_wrap_at_commas(sentence, budget))
    return out


# ---------------------------------------------------------------
# 段落・リスト・引用の単位への切り分け
# ---------------------------------------------------------------
def _join_lines(lines):
    """段落や項目の行を1つの文字列に繋ぐ。行の前後の空白は取り
    除く。繋ぎ目が英数字どうしのときだけ半角空白を1つ入れる。"""
    text = ""
    for line in lines:
        piece = line.strip()
        if not piece:
            continue
        if text and text[-1].isascii() and text[-1].isalnum() \
                and piece[0].isascii() and piece[0].isalnum():
            text += " "
        text += piece
    return text


def _group_units(lines):
    """空行で区切られた塊の行を、単位(段落・項目・引用・そのまま
    の行)の列に切り分ける。"""
    units = []
    cur = None

    def flush():
        nonlocal cur
        if cur is not None:
            units.append(cur)
            cur = None

    for line in lines:
        if _QUOTE_RX.match(line):
            inner = _QUOTE_RX.sub("", line, count=1)
            if cur is not None and cur["kind"] == "quote":
                cur["lines"].append(inner)
            else:
                flush()
                cur = {"kind": "quote", "lines": [inner]}
            continue
        m = _LIST_RX.match(line)
        if m:
            flush()
            cur = {"kind": "item", "indent": m.group(1),
                   "marker": m.group(2), "lines": [m.group(4)]}
            continue
        if _HEADING_RX.match(line) or _TABLE_RX.match(line) \
                or _HR_RX.match(line):
            flush()
            units.append({"kind": "raw", "lines": [line]})
            continue
        if cur is not None and cur["kind"] in ("para", "item"):
            cur["lines"].append(line)
        else:
            flush()
            cur = {"kind": "para", "lines": [line]}
    flush()
    return units


def _format_item(unit, max_columns):
    indent = unit["indent"]
    marker = unit["marker"]
    content = _join_lines(unit["lines"])
    if not content:
        return [indent + marker]
    first_prefix = indent + marker + " "
    rest_prefix = indent + " " * (display_width(marker) + 1)
    budget = max_columns - display_width(first_prefix)
    pieces = _break_text(content, budget)
    out = [first_prefix + pieces[0]]
    out.extend(rest_prefix + piece for piece in pieces[1:])
    return out


def _format_quote(unit, max_columns):
    inner = format_lines(unit["lines"], max(0, max_columns - 2))
    if not inner:
        return [">"]
    return [">" if not line else "> " + line for line in inner]


def _format_flow_block(lines, max_columns):
    """空行で区切られた塊(フェンスの外)の整形。"""
    if all(line.startswith("    ") or line.startswith("\t")
           for line in lines):
        # インデントされたコードブロックはそのまま。
        return list(lines)
    out = []
    for unit in _group_units(lines):
        kind = unit["kind"]
        if kind == "raw":
            out.extend(unit["lines"])
        elif kind == "para":
            text = _join_lines(unit["lines"])
            if text:
                out.extend(_break_text(text, max_columns))
        elif kind == "item":
            out.extend(_format_item(unit, max_columns))
        elif kind == "quote":
            out.extend(_format_quote(unit, max_columns))
    return out


# ---------------------------------------------------------------
# フェンスと空行を扱う全体の入口
# ---------------------------------------------------------------
def _fence_open(line):
    """フェンスの始まりなら (文字, 並びの長さ)、でなければ None。
    バッククォートのフェンスは、情報文字列にバッククォートを
    含めない(CommonMark の決まり)。"""
    m = _FENCE_RX.match(line)
    if not m:
        return None
    run = m.group(2)
    if run[0] == "`" and "`" in m.group(3):
        return None
    return run[0], len(run)


def _is_fence_close(line, char, length):
    m = re.match(r"^\s{0,3}(" + re.escape(char) + r"+)\s*$", line)
    return bool(m) and len(m.group(1)) >= length


def format_lines(lines, max_columns):
    """行の列(改行を含まない)を整形した行の列を返す。

    コードフェンスの中身はそのまま。塊と塊の区切りは空行1つに
    そろえ、頭と末尾の空行は落とす。"""
    out = []
    flow = []
    pending_sep = False     # 次の塊の前に空行を1つ置く

    def emit_block(block_lines):
        nonlocal pending_sep
        if not block_lines:
            return
        if out and pending_sep:
            out.append("")
        out.extend(block_lines)
        pending_sep = False

    def flush_flow():
        if flow:
            emit_block(_format_flow_block(flow, max_columns))
            flow.clear()

    fence = None            # [文字, 長さ, 行の列]
    for line in lines:
        if fence is not None:
            fence[2].append(line)
            if _is_fence_close(line, fence[0], fence[1]):
                emit_block(fence[2])
                fence = None
            continue
        opened = _fence_open(line)
        if opened is not None:
            flush_flow()
            fence = [opened[0], opened[1], [line]]
            continue
        if not line.strip():
            flush_flow()
            if out:
                pending_sep = True
            continue
        flow.append(line)
    flush_flow()
    if fence is not None:
        emit_block(fence[2])
    return out


def format_text(text, max_columns):
    """改行(\\n)区切りの文字列を整形する(テストや一括処理用)。"""
    return "\n".join(format_lines(text.split("\n"), max_columns))
