# editor/convert.py
# 選んだ範囲の文字変換。全角・半角の相互変換と、英字の大文字・小文字。
# Qt に依存しない純粋関数。
#
# 全角・半角は unicodedata の NFKC を使わず、対応表で変換する。
# NFKC は ① → 1 や ㈱ → (株) のような、頼んでいない分解までして
# しまうため。ここで扱うのは次の範囲だけ:
#   - 全角の英数字・記号(U+FF01〜U+FF5E) ⇔ 半角(U+0021〜U+007E)
#   - 全角スペース(U+3000) ⇔ 半角スペース
#   - 全角カタカナ ⇔ 半角カタカナ(濁点・半濁点の合成と分解を含む)
# 大文字・小文字は英字(A-Z / a-z)だけを対象にする(str.upper は
# ß → SS のような、英字以外の変化まで起こすため使わない)。

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
