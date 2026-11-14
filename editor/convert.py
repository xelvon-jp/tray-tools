# editor/convert.py
# 選んだ範囲の文字変換。全角・半角の相互変換と、英字の大文字・小文字。
# Qt に依存しない純粋関数。
#
# タブとスペースの変換(行単位)もここに置く。タブ → スペースの展開、
# 行頭のスペース → タブ、行頭のスペースの全角・半角の組み直し。
#
# 全角・半角は unicodedata の NFKC を使わず、対応表で変換する。
# NFKC は ① → 1 や ㈱ → (株) のような、頼んでいない分解までして
# しまうため。ここで扱うのは次の範囲だけ:
#   - 全角の英数字・記号(U+FF01〜U+FF5E) ⇔ 半角(U+0021〜U+007E)
#   - 全角スペース(U+3000) ⇔ 半角スペース
#   - 全角カタカナ ⇔ 半角カタカナ(濁点・半濁点の合成と分解を含む)
# 大文字・小文字は英字(A-Z / a-z)だけを対象にする(str.upper は
# ß → SS のような、英字以外の変化まで起こすため使わない)。
import unicodedata

# 半角カタカナ(U+FF61〜U+FF9F)と全角の対応。半角側はコードポイントの
# 並びで作り、全角側は同じ順序の文字列を当てる。
_HALF_KANA = "".join(chr(cp) for cp in range(0xFF61, 0xFFA0))
_FULL_KANA = (
    "。「」、・ヲァィゥェォャュョッー"
    "アイウエオカキクケコサシスセソ"
    "タチツテトナニヌネノハヒフヘホ"
    "マミムメモヤユヨラリルレロワン"
    "\u309b\u309c"
)
assert len(_HALF_KANA) == len(_FULL_KANA) == 63

_KANA_TO_HALF = dict(zip(_FULL_KANA, _HALF_KANA))
_KANA_TO_FULL = dict(zip(_HALF_KANA, _FULL_KANA))

_HALF_DAKUTEN = "\uff9e"   # ﾞ
_HALF_HANDAKUTEN = "\uff9f"  # ﾟ

# 濁点が付く全角カナ → 元の全角カナ。半角では「元の半角 + ﾞ」になる。
_VOICED_TO_BASE = {
    "ガ": "カ", "ギ": "キ", "グ": "ク", "ゲ": "ケ", "ゴ": "コ",
    "ザ": "サ", "ジ": "シ", "ズ": "ス", "ゼ": "セ", "ゾ": "ソ",
    "ダ": "タ", "ヂ": "チ", "ヅ": "ツ", "デ": "テ", "ド": "ト",
    "バ": "ハ", "ビ": "ヒ", "ブ": "フ", "ベ": "ヘ", "ボ": "ホ",
    "ヴ": "ウ", "ヷ": "ワ", "ヸ": "ヰ", "ヹ": "ヱ", "ヺ": "ヲ",
}
# 半濁点が付く全角カナ → 元の全角カナ。半角では「元の半角 + ﾟ」。
_SEMIVOICED_TO_BASE = {
    "パ": "ハ", "ピ": "ヒ", "プ": "フ", "ペ": "ヘ", "ポ": "ホ",
}
# 逆引き(半角→全角の合成用)。(元の半角カナ, 濁点か半濁点か) → 全角。
# 半角カナに元字が無いもの(ヸ・ヹ。ヰ・ヱの半角は無い)は合成できない
# ので、表に入れない(変換ではそのまま残る)。
_BASE_TO_VOICED = {
    (_KANA_TO_HALF[base], True): voiced
    for voiced, base in _VOICED_TO_BASE.items()
    if base in _KANA_TO_HALF
}
_BASE_TO_VOICED.update({
    (_KANA_TO_HALF[base], False): voiced
    for voiced, base in _SEMIVOICED_TO_BASE.items()
    if base in _KANA_TO_HALF
})

_FULLWIDTH_OFFSET = 0xFEE0  # 半角(0x21〜0x7E) + この値 = 全角


def to_halfwidth(text: str) -> str:
    """全角 → 半角。英数字・記号・スペース・カタカナが対象。

    濁点付きのカタカナは「半角カナ + ﾞ」の2文字になる(半角カナには
    合成済みの字が無いため)。対象外の文字はそのまま残す。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - _FULLWIDTH_OFFSET))
        elif ch == "\u3000":
            out.append(" ")
        elif ch in _VOICED_TO_BASE and _VOICED_TO_BASE[ch] in _KANA_TO_HALF:
            out.append(_KANA_TO_HALF[_VOICED_TO_BASE[ch]])
            out.append(_HALF_DAKUTEN)
        elif (ch in _SEMIVOICED_TO_BASE
                and _SEMIVOICED_TO_BASE[ch] in _KANA_TO_HALF):
            out.append(_KANA_TO_HALF[_SEMIVOICED_TO_BASE[ch]])
            out.append(_HALF_HANDAKUTEN)
        elif ch in _KANA_TO_HALF:
            out.append(_KANA_TO_HALF[ch])
        else:
            out.append(ch)
    return "".join(out)


def to_fullwidth(text: str) -> str:
    """半角 → 全角。英数字・記号・スペース・カタカナが対象。

    半角カナの後ろに ﾞ / ﾟ が続けば、濁点・半濁点付きの1文字に
    合成する。単独の ﾞ / ﾟ は全角の ゛ / ゜ になる。"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + _FULLWIDTH_OFFSET))
            i += 1
        elif ch == " ":
            out.append("\u3000")
            i += 1
        elif ch in _KANA_TO_FULL:
            nxt = text[i + 1] if i + 1 < len(text) else ""
            voiced = _BASE_TO_VOICED.get((ch, True)) if nxt == _HALF_DAKUTEN else None
            semi = _BASE_TO_VOICED.get((ch, False)) if nxt == _HALF_HANDAKUTEN else None
            if voiced is not None:
                out.append(voiced)
                i += 2
            elif semi is not None:
                out.append(semi)
                i += 2
            else:
                out.append(_KANA_TO_FULL[ch])
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def to_lower(text: str) -> str:
    """英字の大文字 → 小文字。英字以外は変えない。"""
    return "".join(
        chr(ord(ch) + 0x20) if "A" <= ch <= "Z" else ch for ch in text)


def to_upper(text: str) -> str:
    """英字の小文字 → 大文字。英字以外は変えない。"""
    return "".join(
        chr(ord(ch) - 0x20) if "a" <= ch <= "z" else ch for ch in text)


# ---------------------------------------------------------------
# タブとスペースの変換(行単位)
# ---------------------------------------------------------------
def _display_width_of(ch) -> int:
    """表示の桁数。全角(東アジア幅 W / F)は2、それ以外は1
    (reformat.py の char_width と同じ数え方)。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _tab_width(tab_width) -> int:
    """タブ幅の整え。1以上の整数でなければ 8 に倒す(窓側は実効値を
    渡すので、ここは保険)。"""
    try:
        width = int(tab_width)
    except (TypeError, ValueError):
        return 8
    return width if width >= 1 else 8


def tab_to_space(line: str, tab_width) -> str:
    """1行の中のタブを、タブ幅で次のタブ位置までの半角スペースに
    広げる。桁は表示幅で数える(全角文字は2)ので、全角文字の後ろの
    タブは短くなる。タブ位置ちょうどにあるタブは、タブ幅ぶん
    進む(空白がタブ幅ぶん入る)。"""
    width = _tab_width(tab_width)
    out = []
    column = 0
    for ch in line:
        if ch == "\t":
            count = width - (column % width)
            out.append(" " * count)
            column += count
        else:
            out.append(ch)
            column += _display_width_of(ch)
    return "".join(out)


def space_to_tab(line: str, tab_width) -> str:
    """1行の行頭の空白だけを、タブ位置にそろう分はタブに直す。

    対象は行頭の並び(半角スペースとタブ)だけで、文中の空白は
    触らない。既にあるタブはそのまま通す(次のタブ位置まで進む
    として数える)。タブ位置まで届かない余りのスペースはそのまま
    残す。結果の表示幅は、元の行頭と一致する。"""
    width = _tab_width(tab_width)
    end = 0
    while end < len(line) and line[end] in (" ", "\t"):
        end += 1
    lead, rest = line[:end], line[end:]
    if not lead:
        return line
    # 行頭の各桁が、スペースかタブの区間かを並べる(タブは次の
    # タブ位置までの区間として桁を占める)。
    cells = []
    for ch in lead:
        if ch == " ":
            cells.append(" ")
        else:
            stop = (len(cells) // width + 1) * width
            cells.extend("\t" * (stop - len(cells)))
    out = []
    column = 0
    total = len(cells)
    while column < total:
        if (column % width == 0 and column + width <= total
                and all(cell == " " for cell in cells[column:column + width])):
            # タブ位置から次のタブ位置まで、全部スペースで埋まって
            # いる区間はタブ1つに置き換えられる。
            out.append("\t")
            column += width
        elif cells[column] == "\t":
            # 既にあるタブの区間の頭(進み方が「1桁」か「タブ位置
            # までの跳躍」だけなので、区間の途中には来ない)。
            # そのまま通し、次のタブ位置まで進む。
            stop = (column // width + 1) * width
            out.append("\t")
            column = stop
        else:
            out.append(" ")
            column += 1
    return "".join(out) + rest


_FULL_SPACE = "　"


def _lead_space_units(line: str):
    """行頭の、半角スペースと全角スペースだけからなる部分の
    (単位数, 残りの本文)。単位は半角スペース1・全角スペース2。
    タブなど他の文字が来たところで頭の部分は終わる。"""
    end = 0
    units = 0
    while end < len(line) and line[end] in (" ", _FULL_SPACE):
        units += 1 if line[end] == " " else 2
        end += 1
    return units, line[end:]


def lead_half_to_full(line: str) -> str:
    """行頭のスペースを、幅(半角1・全角2の単位数)を保ったまま
    全角スペース優先に組み直す。半角4個 → 全角2個、半角3個 →
    全角1個 + 半角1個、全角1個 + 半角2個 → 全角2個。奇数で余る
    単位は半角スペース1個になる。行頭以外のスペースは触らない。"""
    units, rest = _lead_space_units(line)
    return _FULL_SPACE * (units // 2) + " " * (units % 2) + rest


def lead_full_to_half(line: str) -> str:
    """行頭のスペースを、幅を保ったまま全部半角スペースに
    組み直す(全角1個 → 半角2個)。行頭以外のスペースは触らない。"""
    units, rest = _lead_space_units(line)
    return " " * units + rest
