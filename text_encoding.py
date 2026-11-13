# text_encoding.py
# テキストの文字コード判定と、BOM・改行まわりの純粋関数(Qt に依存しない部分)。
#
# もとは filer_viewer.py の先頭にあったものを切り出した(2026-11-13、テキスト
# エディタ editor/ の新設に合わせて)。使う側:
#   - filer_viewer      簡易テキストビュア(再 export しているので従来どおり
#                       filer_viewer.detect_encoding でも引ける)
#   - filer_preview / filer_diff   filer_viewer 経由で使う(変更なし)
#   - editor.document   開くときの判定と、保存時の BOM・文字コードの往復
#
# 判定の順序はビュア時代から決まっている: BOM → UTF-8 として正しいか
# (先頭の数 MB、途中で切れた最後の1文字は見逃す) → cp932。
import codecs

# 表示名と Python のコーデック名。キーはあふｗのテキストビュアの切り替えキー。
ENCODING_KEYS = {
    "O": "utf-8",
    "S": "cp932",
    "U": "utf-16-le",
    "I": "utf-16-be",
    "E": "euc_jp",
    "J": "iso2022_jp",
}
ENCODING_LABELS = {
    "utf-8": "UTF-8",
    "cp932": "SJIS",
    "utf-16-le": "UTF-16LE",
    "utf-16-be": "UTF-16BE",
    "euc_jp": "EUC-JP",
    "iso2022_jp": "JIS",
}

_BOMS = {
    "utf-8": codecs.BOM_UTF8,
    "utf-16-le": codecs.BOM_UTF16_LE,
    "utf-16-be": codecs.BOM_UTF16_BE,
}


def detect_encoding(head: bytes):
    """(コーデック名, 本文の始まりの位置)。BOM があればそれ、無ければ UTF-8 として
    正しいか(途中で切れた最後の1文字は見逃す)、だめなら cp932。"""
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8", 3
    if head.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", 2
    if head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", 2
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        decoder.decode(head, final=False)
        return "utf-8", 0
    except UnicodeDecodeError:
        return "cp932", 0


def bom_length(head: bytes, encoding: str) -> int:
    """手で切り替えたときの本文の始まり。その文字コードの BOM があれば飛ばす。"""
    bom = _BOMS.get(encoding)
    if bom and head.startswith(bom):
        return len(bom)
    return 0


def bom_bytes(encoding: str) -> bytes:
    """その文字コードの BOM のバイト列。BOM の概念が無い文字コードでは空。"""
    return _BOMS.get(encoding, b"")


def newline_of(encoding: str) -> bytes:
    """その文字コードでの改行(LF)のバイト列。行の区切りはこれで探す。"""
    if encoding == "utf-16-le":
        return b"\n\x00"
    if encoding == "utf-16-be":
        return b"\x00\n"
    return b"\n"


def decode_line(raw: bytes, encoding: str) -> str:
    """1行ぶんのバイト列を文字にする(読めないバイトは置換文字)。行末の改行は落とす。"""
    text = raw.decode(encoding, errors="replace")
    return text.rstrip("\r\n")


def find_in_line(text: str, word: str):
    """大文字小文字を無視して word を探す。見つかった桁(文字単位)か None。"""
    if not word:
        return None
    index = text.casefold().find(word.casefold())
    return index if index >= 0 else None
