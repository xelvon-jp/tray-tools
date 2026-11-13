# editor/reformat.py
# テキストの整形。音声入力の長文を読みやすくするため、段落の中の
# 改行はいったん取り除き、句点で文ごとに改行し直す。行が折り返し
# の桁数を超えるときだけ、読点でも改行する。Markdown として壊れ
# ないよう、コードフェンス・見出し・表・リンクと画像の記法・
# インラインコード・かっこの中では改行しない。リストと引用は、続きの行が
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
#   - 文末の記号(。！？)が続くときは、記号の並びとその後ろの閉じ
#     括弧・引用符までを同じ文に残し、並びの後ろでだけ改行する
#     (「え？！」が「え？／！」に割れない)。
#   - 対応するかっこの内側は保護範囲とし、中の句点・読点では
#     改行しない。入れ子にも対応する。ただし、対応する閉じが
#     開きから300字以内に見つからないかっこは組にせず、ただの
#     文字として扱う(閉じ忘れや、（「あ。）」のような種類の
#     食い違いで、段落の残り全部が整形されなくなるのを防ぐ)。
#     対応の付かない閉じかっこは無視する。
#   - 行頭にある番号つきの印は、リストの並びとして続く位置
#     (塊の頭、同じ深さか浅い位置で項目が続くとき)でのみ、項
#     目の始まりとして扱う。それ以外(段落の途中、今の項目より
#     深い字下げの位置)では、原則として段落や項目の続きとし
#     て扱い、例外は「1.」で、後ろに「2.」の項目が同じ深さで
#     連番に続くときだけ始まりになる(連番の判定では、項目の
#     続きの字下げ行を飛ばして先を見る)。「1)」のような閉じ
#     かっこ付きの番号は始まりにならない(日本語の文中の手順
#     でよく出るため。CommonMark とはここが違う)。整形で生ま
#     れた行が、2回目にリストとして拾われ直して字下げが深く
#     なり、冪等でなくなるのを防ぐため。
#   - 文の頭に残った空白(句点の後ろの空白など)は取り除く。読点
#     で改行した続きの行も同じ。整形を2回かけても結果が変わら
#     ない(冪等)ための決め。
#   - 文書の頭の YAML フロントマター(--- で囲まれた範囲)、リン
#     クの定義行([ラベル]: URL)、先頭に | の無い表(次の行が区切
#     りの罫線になっているもの)、HTML のブロック(ブロック要素
#     のタグで始まるまとまり)は、壊れる前と同じ内容を保つため
#     触らない。HTML のブロックの終わりは、コメント・処理命令・
#     宣言と script / style / pre では閉じの印がある行まで、それ
#     以外は CommonMark と同じく空行まで。
#   - 性能: 保護範囲の印は対象の文字列ごとに1回だけ作り、読点
#     の改行は1回の走査で決める(長い1文に読点が多くても時間が
#     二乗に伸びない)。
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
_LEADING_SPACE = " \t　"

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
# リンクの定義行 [ラベル]: URL(脚注の定義も同じ形なので一緒に守る)。
_LINK_DEF_RX = re.compile(r"^\s{0,3}\[[^\]]+\]:[ \t]*\S")
# YAML フロントマターの区切り行。
_FRONT_MATTER_RX = re.compile(r"^(---|\.\.\.)[ \t]*$")
# HTML のブロックの始まり(ブロック要素のタグ、コメント、宣言)。
_HTML_START_RX = re.compile(
    r"^\s{0,3}(<!--|<\?|<![A-Za-z]|</?([A-Za-z][A-Za-z0-9-]*)\b)")
_HTML_BLOCK_TAGS = frozenset(
    "address article aside base basefont blockquote body caption "
    "center col colgroup dd details dialog dir div dl dt fieldset "
    "figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 "
    "head header hr html iframe legend li link main menu meta nav "
    "noframes ol p pre script section style summary table tbody td "
    "tfoot th thead title tr track ul".split())
# 対応するかっこ(開き → 閉じ)。内側では改行しない。
_BRACKET_PAIRS = {
    "（": "）", "(": ")",
    "「": "」", "『": "』",
    "【": "】", "〔": "〕",
    "〈": "〉", "《": "》",
    "｛": "}", "{": "}",
    "［": "］", "[": "]",
}
_BRACKET_CLOSES = frozenset(_BRACKET_PAIRS.values())


def char_width(ch):
    """表示の桁数。全角(東アジア幅 W / F)は2、それ以外は1。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(ch) for ch in text)


# ---------------------------------------------------------------
# 改行しない範囲(インラインコード・リンク・かっこ)の保護
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


# 対応する閉じを探す範囲(開きかっこからの字数)。この範囲に
# 閉じが無い開きかっこは組にせず、ただの文字として扱う。
_BRACKET_LIMIT = 300


def _bracket_spans(text):
    """対応するかっこで囲まれた範囲 (開始, 終了) の列。入れ子は
    いちばん外側の範囲だけ返す(内側はその部分集合)。対応の付か
    ない閉じかっこは無視する。開きかっこから _BRACKET_LIMIT 字
    以内に閉じが見つからない組は捨てる(開きかっこはただの文字
    として扱われる)。遠い外側の組を捨てても、内側の組は残る。"""
    pairs = []
    stack = []          # 開きかっこの位置の列
    for i, ch in enumerate(text):
        if ch in _BRACKET_PAIRS:
            stack.append(i)
        elif ch in _BRACKET_CLOSES:
            if stack and _BRACKET_PAIRS[text[stack[-1]]] == ch:
                pairs.append((stack.pop(), i + 1))
    pairs = [(s, e) for s, e in pairs if e - 1 - s <= _BRACKET_LIMIT]
    pairs.sort(key=lambda pair: (pair[0], -pair[1]))
    spans = []
    for start, end in pairs:
        if spans and spans[-1][0] <= start and end <= spans[-1][1]:
            continue    # 外側の範囲に含まれている
        spans.append((start, end))
    return spans


def _protected_mask(text):
    """文字ごとに「改行を入れてはいけない範囲か」の印(0/1)。
    インラインコードの中、リンク・画像の記法全体、対応するかっ
    この内側が対象。対象の文字列ごとに1回だけ作る。"""
    mask = bytearray(len(text))
    spans = _code_spans(text)
    for m in _LINK_RX.finditer(text):
        start, end = m.span()
        # コードの中にあるものはリンクではないので重なりは捨てる。
        if any(start < c_end and c_start < end for c_start, c_end in spans):
            continue
        spans.append((start, end))
    spans.extend(_bracket_spans(text))
    for start, end in spans:
        for i in range(start, end):
            mask[i] = 1
    return mask


# ---------------------------------------------------------------
# 文への分割と、読点での改行
# ---------------------------------------------------------------
def _split_sentences(text, mask):
    """句点(。！？)で文に分けた (文, その範囲の印) の列。文末の
    記号が続くあいだ(と、その後ろの閉じ括弧・引用符)は同じ文に
    残す。保護範囲の中の句点では分けない。文の頭に残った空白は
    取り除く。"""
    sentences = []
    start = 0
    i, n = 0, len(text)
    while i < n:
        if text[i] in _SENTENCE_ENDS and not mask[i]:
            j = i + 1
            while j < n and not mask[j] \
                    and (text[j] in _SENTENCE_ENDS or text[j] in _CLOSERS):
                j += 1
            sentences.append((start, j))
            start = j
            i = j
        else:
            i += 1
    if start < n:
        sentences.append((start, n))
    out = []
    for first, last in sentences:
        while first < last and text[first] in _LEADING_SPACE:
            first += 1
        if first < last:
            out.append((text[first:last], mask[first:last]))
    return out


def _wrap_at_commas(sentence, budget, mask):
    """1文を、桁数(budget)を超えるときだけ読点で改行した行の列
    にする。budget 以内に読点が無ければ、最初の読点で改行する
    (読点自体が無ければ、そのまま1行)。「。」だけのような残り
    しか生まれない位置では改行しない。印は作り直さず、走査は
    全体で1回に収める(長い文でも時間が二乗にならない)。"""
    n = len(sentence)
    if budget <= 0 or display_width(sentence) <= budget:
        return [sentence]
    lines = []
    start = 0           # 現在の行の開始位置
    width = 0           # 現在の行の表示幅
    last_comma = None   # 桁数に収まる最後の読点の直後の位置
    i = 0
    while i < n:
        w = char_width(sentence[i])
        if i > start and width + w > budget:
            cut = last_comma
            if cut is None:
                # 収まる読点が無ければ、最初の読点で改行する。
                j = i
                while j < n:
                    if sentence[j] == _COMMA and not mask[j]:
                        cut = j + 1
                        break
                    j += 1
            if cut is None:
                break
            if not sentence[cut:].strip(
                    _SENTENCE_ENDS + _CLOSERS + _LEADING_SPACE):
                break
            lines.append(sentence[start:cut])
            start = cut
            while start < n and sentence[start] in _LEADING_SPACE:
                start += 1
            i = start
            width = 0
            last_comma = None
            continue
        width += w
        if sentence[i] == _COMMA and not mask[i]:
            last_comma = i + 1
        i += 1
    if start < n:
        lines.append(sentence[start:])
    return lines


def _break_text(text, budget):
    """文字列を、文ごと(＋長ければ読点)の行の列にする。保護範囲
    の印はここで1回だけ作る。"""
    mask = _protected_mask(text)
    out = []
    for sentence, smask in _split_sentences(text, mask):
        out.extend(_wrap_at_commas(sentence, budget, smask))
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


def _is_table_delimiter(line):
    """表の区切り行か。| と - と : と空白だけでできていて、セル
    が2つ以上あるか、両端が | で囲まれた1セルの形。"""
    s = line.strip()
    if "-" not in s or "|" not in s:
        return False
    if re.fullmatch(r"\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?", s):
        return True
    return bool(re.fullmatch(r"\|\s*:?-+:?\s*\|", s))


def _is_html_block_start(line):
    """HTML のブロックの始まりの行か。ブロック要素のタグ(開き・
    閉じ)、コメント、処理命令、宣言で始まる行。"""
    m = _HTML_START_RX.match(line)
    if not m:
        return False
    tag = m.group(2)
    return tag is None or tag.lower() in _HTML_BLOCK_TAGS


def _html_block_end(lines, i):
    """HTML のブロックが終わる位置(その行を含まない)。コメント・
    処理命令・宣言と script / style / pre は、閉じの印がある行
    まで。それ以外のタグは CommonMark と同じく空行(＝この塊の
    終わり)まで。"""
    first = lines[i]
    stripped = first.lstrip()
    closer = None
    if "<!--" in first:
        closer = "-->"
    elif stripped.startswith("<?"):
        closer = "?>"
    elif stripped.startswith("<!"):
        closer = ">"
    else:
        m = _HTML_START_RX.match(first)
        tag = m.group(2) if m else None
        if tag and tag.lower() in ("script", "style", "pre"):
            closer = "</" + tag.lower()
    if closer is None:
        return len(lines)
    for j in range(i, len(lines)):
        if closer in lines[j]:
            return j + 1
    return len(lines)


def _followed_by_second(lines, i, indent_width):
    """lines[i] の後ろに、「2.」の項目が連番で続いているか。
    項目の続きの行(字下げされた、リストでない行)は飛ばして見
    る。「2.」の行は、字下げの深さが lines[i] と同じときだけ
    連番とみなす(外側のリストの「2.」は数えない)。それ以外の
    行に当たれば、連番は続いていない。"""
    for line in lines[i + 1:]:
        m = _LIST_RX.match(line)
        if m:
            if m.group(2) == "2." \
                    and display_width(m.group(1)) == indent_width:
                return True
            if display_width(m.group(1)) <= indent_width:
                return False
            continue
        if line[:1] in (" ", "\t", "　"):
            continue
        return False
    return False


def _numbered_starts_item(cur, marker, indent, lines, i):
    """番号つきの印の行が、新しい項目の始まりになるか。
    塊の頭では常になる。同じ深さか浅い位置で項目が続くときも
    なる(リストの並び)。それ以外(段落の途中、今の項目より深い
    字下げの位置)では、散文の決めで判定する: 閉じかっこの付い
    た番号(「1)」など)はならず、「1.」は後ろに「2.」の項目が
    連番で続くときだけなる。整形で生まれた行(項目の続きの行
    など)が、2回目にリストとして拾われ直して字下げが深くな
    り、冪等でなくなるのを防ぐため。"""
    if cur is None:
        return True
    if cur["kind"] == "item" \
            and display_width(indent) <= display_width(cur["indent"]):
        return True
    if marker.endswith(")"):
        return False
    if int(marker[:-1]) != 1:
        return False
    return _followed_by_second(lines, i, display_width(indent))


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

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _QUOTE_RX.match(line):
            inner = _QUOTE_RX.sub("", line, count=1)
            if cur is not None and cur["kind"] == "quote":
                cur["lines"].append(inner)
            else:
                flush()
                cur = {"kind": "quote", "lines": [inner]}
            i += 1
            continue
        m = _LIST_RX.match(line)
        if m and (not m.group(2)[0].isdigit()
                  or _numbered_starts_item(
                      cur, m.group(2), m.group(1), lines, i)):
            flush()
            cur = {"kind": "item", "indent": m.group(1),
                   "marker": m.group(2), "lines": [m.group(4)]}
            i += 1
            continue
        if _is_html_block_start(line):
            # HTML のブロックは終わりの位置まで触らない。
            flush()
            end = _html_block_end(lines, i)
            units.append({"kind": "raw", "lines": list(lines[i:end])})
            i = end
            continue
        if _HEADING_RX.match(line) or _TABLE_RX.match(line) \
                or _HR_RX.match(line) or _LINK_DEF_RX.match(line):
            flush()
            units.append({"kind": "raw", "lines": [line]})
            i += 1
            continue
        if i + 1 < n and _is_table_delimiter(lines[i + 1]):
            # 先頭に | の無い表。見出し行＋区切り行＋ | を含む行が
            # 続くあいだを行のまま保つ。
            flush()
            raw = [line, lines[i + 1]]
            i += 2
            while i < n and "|" in lines[i]:
                raw.append(lines[i])
                i += 1
            units.append({"kind": "raw", "lines": raw})
            continue
        if cur is not None and cur["kind"] in ("para", "item"):
            cur["lines"].append(line)
        else:
            flush()
            cur = {"kind": "para", "lines": [line]}
        i += 1
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


def _front_matter_end(lines):
    """文書の頭の YAML フロントマターの終わりの行番号(区切り行を
    含む)。フロントマターでなければ None。"""
    if not lines or not _FRONT_MATTER_RX.match(lines[0]) \
            or lines[0].strip().startswith("..."):
        return None
    for j in range(1, len(lines)):
        if _FRONT_MATTER_RX.match(lines[j]):
            return j
    return None


def format_lines(lines, max_columns):
    """行の列(改行を含まない)を整形した行の列を返す。

    コードフェンスの中身と文書頭のフロントマターはそのまま。
    塊と塊の区切りは空行1つにそろえ、頭と末尾の空行は落とす。"""
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

    fm_end = _front_matter_end(lines)
    if fm_end is not None:
        emit_block(list(lines[:fm_end + 1]))
        lines = lines[fm_end + 1:]

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
