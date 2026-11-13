# tests/test_editor_document.py
# テキストエディタの文書モデル(editor/document.py)と、文字コードの共通
# モジュール(text_encoding.py)、キー割り当ての表(editor/keymap.py)を確かめる。
# Qt には触らない(読み書きの往復と検索の純粋関数だけ)。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_document.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_copilot_status.py と同じ形)。
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import text_encoding  # noqa: E402
from editor import document as docmod  # noqa: E402
from editor.keymap import DEFAULT_KEYMAP, merge_keymap  # noqa: E402

SAMPLE = "あいうえお\n日本語のテキストです。\n3行目、ABC。\n"


def _write(path, data: bytes):
    Path(path).write_bytes(data)


def _roundtrip(encoding, newline, bom=False):
    """バイト列で書いて、読み直して、別名で保存したら元のバイト列と
    一致することを確かめる(文字コード × 改行 × BOM の往復)。"""
    text = SAMPLE.replace("\n", newline)
    data = text.encode(encoding)
    if bom:
        data = text_encoding.bom_bytes(encoding) + data
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.txt")
        dst = os.path.join(tmp, "out.txt")
        _write(src, data)
        doc = docmod.load_document(src)
        assert doc.encoding == encoding, (doc.encoding, encoding)
        assert doc.newline == newline, (doc.newline, newline)
        assert doc.bom == bom
        assert doc.text == SAMPLE, repr(doc.text)
        assert not doc.lossy_decode
        docmod.save_document(doc, path=dst)
        assert Path(dst).read_bytes() == data, encoding


def test_roundtrip_utf8_crlf():
    _roundtrip("utf-8", docmod.CRLF)


def test_roundtrip_utf8_lf():
    _roundtrip("utf-8", docmod.LF)


def test_roundtrip_sjis_crlf():
    _roundtrip("cp932", docmod.CRLF)


def test_roundtrip_sjis_lf():
    _roundtrip("cp932", docmod.LF)


def test_roundtrip_utf8_bom():
    _roundtrip("utf-8", docmod.CRLF, bom=True)


def test_roundtrip_utf16le_bom():
    _roundtrip("utf-16-le", docmod.CRLF, bom=True)


def test_detect_newline():
    assert docmod.detect_newline("a\r\nb") == docmod.CRLF
    assert docmod.detect_newline("a\nb") == docmod.LF
    assert docmod.detect_newline("改行なし") == docmod.CRLF  # 既定
    assert docmod.detect_newline("a\nb\r\n") == docmod.LF    # 最初の改行で決める


def test_save_with_conversion():
    """利用者が選んだときだけ変換される(保存ダイアログの経路)。"""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "in.txt")
        dst = os.path.join(tmp, "out.txt")
        _write(src, SAMPLE.replace("\n", "\r\n").encode("cp932"))
        doc = docmod.load_document(src)
        docmod.save_document(doc, path=dst, encoding="utf-8", newline=docmod.LF)
        assert Path(dst).read_bytes() == SAMPLE.encode("utf-8")
        assert doc.encoding == "utf-8" and doc.newline == docmod.LF


def test_save_unencodable_raises():
    """変換先で書けない文字は、黙って化けさせず例外で知らせる。"""
    doc = docmod.TextDocument(text="雪だるま ☃")
    try:
        docmod.encode_document(doc, encoding="cp932")
    except UnicodeEncodeError:
        return
    raise AssertionError("UnicodeEncodeError が出ませんでした")


def test_detect_encoding_shared():
    """text_encoding の判定はビュア時代の期待値のまま(filer と共用)。"""
    assert text_encoding.detect_encoding(b"\xef\xbb\xbfabc") == ("utf-8", 3)
    assert text_encoding.detect_encoding(b"\xff\xfea\x00") == ("utf-16-le", 2)
    assert text_encoding.detect_encoding("日本語".encode("utf-8")) == ("utf-8", 0)
    assert text_encoding.detect_encoding("日本語".encode("cp932")) == ("cp932", 0)
    assert text_encoding.bom_bytes("utf-8") == b"\xef\xbb\xbf"
    assert text_encoding.bom_bytes("cp932") == b""


def test_find_next_prev():
    text = "foo bar Foo baz foo"
    assert docmod.find_next(text, "foo", 0) == 0
    assert docmod.find_next(text, "foo", 1) == 8   # "Foo" も大小無視では当たる
    assert docmod.find_next(text, "foo", 9) == 16
    assert docmod.find_next(text, "foo", 17) == -1       # 巻き戻しはしない
    assert docmod.find_prev(text, "foo", len(text)) == 16
    assert docmod.find_prev(text, "foo", 16) == 8
    assert docmod.find_next(text, "FOO", 0) == 0          # 既定は区別しない
    assert docmod.find_next(text, "FOO", 0, case_sensitive=True) == -1
    assert docmod.find_next(text, "Foo", 0, case_sensitive=True) == 8
    assert docmod.find_next(text, "", 0) == -1
    assert docmod.find_prev(text, "", 5) == -1


def test_keymap_merge():
    table = merge_keymap()
    assert table == DEFAULT_KEYMAP and table is not DEFAULT_KEYMAP
    table = merge_keymap({"save": "Ctrl+Alt+S", "unknown": "F9", "open": 42})
    assert table["save"] == "Ctrl+Alt+S"
    assert "unknown" not in table
    assert table["open"] == DEFAULT_KEYMAP["open"]       # 型違いは捨てる
    assert merge_keymap(None) == DEFAULT_KEYMAP


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
