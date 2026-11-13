# editor/document.py
# テキストエディタの文書モデル(Qt に依存しない部分)。
#
# 守ること(仕様): 保存のとき、開いたときの文字コードと改行(CRLF/LF)を
# そのまま守る。変換は利用者が選んだときだけ。だから読み込みでは
# 「文字コード・BOM の有無・改行」を控えておき、保存ではそれを使って
# バイト列に戻す。本文は編集しやすいよう、改行を "\n" にそろえて持つ。
#
# 文字コードの判定は text_encoding(ビュアと共用)に任せる。
from dataclasses import dataclass

import text_encoding

CRLF = "\r\n"
LF = "\n"
NEWLINE_LABELS = {CRLF: "CRLF", LF: "LF"}

# 判定に使う先頭の長さ(filer_viewer.DETECT_BYTES と同じ)。
DETECT_HEAD_BYTES = 4 * 1024 * 1024

# 新規文書の既定。相手は Windows の道具なので、文字コードは BOM 無し
# UTF-8、改行は CRLF。
DEFAULT_ENCODING = "utf-8"
DEFAULT_NEWLINE = CRLF


@dataclass
class TextDocument:
    path: str = None            # 保存先。無題なら None
    encoding: str = DEFAULT_ENCODING
    bom: bool = False           # 開いたときに BOM が付いていたか
    newline: str = DEFAULT_NEWLINE
    text: str = ""              # 本文。改行は "\n" に正規化済み
    lossy_decode: bool = False  # 読み替え(置換文字)が発生したか

    @property
    def display_name(self) -> str:
        if not self.path:
            return "無題"
        import os

        return os.path.basename(self.path)


def detect_newline(text: str) -> str:
    """本文で最初に出てくる改行の種類。改行が無ければ CRLF(Windows の道具
    としての既定)。混在していても最初のものに合わせる(保存で全体が
    そろうのは、利用者が触った結果として自然な範囲に留める)。"""
    index = text.find("\n")
    if index > 0 and text[index - 1] == "\r":
        return CRLF
    if index == -1:
        return DEFAULT_NEWLINE
    return LF


def load_document(path: str) -> TextDocument:
    """ファイルを読み、文字コードと改行を控えた TextDocument を返す。

    読めないバイトがあったら置換文字で読み、lossy_decode を立てる
    (ビュアと同じ倒し方。保存すると置換文字がそのまま書かれるので、
    窓側はその旨を表示する)。"""
    with open(path, "rb") as f:
        data = f.read()
    encoding, start = text_encoding.detect_encoding(data[:DETECT_HEAD_BYTES])
    raw = data[start:]
    lossy = False
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode(encoding, errors="replace")
        lossy = True
    newline = detect_newline(text)
    return TextDocument(
        path=path,
        encoding=encoding,
        bom=start > 0,
        newline=newline,
        text=text.replace("\r\n", "\n"),
        lossy_decode=lossy,
    )


def encode_document(doc: TextDocument, encoding=None, newline=None, bom=None) -> bytes:
    """doc をバイト列に戻す。encoding / newline / bom を渡すと、その
    変換で書く(「名前を付けて保存」で利用者が選んだ場合だけ渡す)。

    変換先の文字コードで書けない文字があると UnicodeEncodeError を
    上げる。黙って置換文字にすると中身が壊れるので、呼び側が
    利用者に伝える前提で隠さない。"""
    enc = encoding if encoding is not None else doc.encoding
    nl = newline if newline is not None else doc.newline
    use_bom = doc.bom if bom is None else bom
    text = doc.text
    if nl == CRLF:
        text = text.replace("\n", "\r\n")
    body = text.encode(enc)
    return (text_encoding.bom_bytes(enc) if use_bom else b"") + body


def save_document(doc: TextDocument, path=None, encoding=None, newline=None,
                  bom=None) -> str:
    """doc をファイルへ書く。成功したら doc 側の控え(保存先・文字コード・
    改行・BOM)を、実際に書いた内容に合わせて更新して、書いたパスを返す。"""
    target = path if path is not None else doc.path
    if not target:
        raise ValueError("保存先が決まっていません")
    data = encode_document(doc, encoding=encoding, newline=newline, bom=bom)
    with open(target, "wb") as f:
        f.write(data)
    doc.path = target
    if encoding is not None:
        doc.encoding = encoding
    if newline is not None:
        doc.newline = newline
    if bom is not None:
        doc.bom = bom
    return target


# ---------------------------------------------------------------
# 検索(窓から使う純粋関数。tests/test_editor_document.py で確かめる)
# ---------------------------------------------------------------
def find_next(text: str, word: str, start: int, case_sensitive=False) -> int:
    """start 以後に word が出る位置。無ければ -1。巻き戻しは窓側の仕事。

    大文字小文字を区別しない場合は casefold で比べる。casefold で長さが
    変わる文字(ß 等)では、返る位置が元の文字列とずれることがある
    (ビュアの find_in_line と同じ割り切り)。"""
    if not word:
        return -1
    start = max(0, min(start, len(text)))
    if case_sensitive:
        return text.find(word, start)
    return text.casefold().find(word.casefold(), start)


def find_prev(text: str, word: str, start: int, case_sensitive=False) -> int:
    """start より前に word が最後に出る位置。無ければ -1。"""
    if not word:
        return -1
    start = max(0, min(start, len(text)))
    if case_sensitive:
        return text.rfind(word, 0, start)
    return text.casefold().rfind(word.casefold(), 0, start)
