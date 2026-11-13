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


def test_save_backup_and_no_temp_left():
    """上書き保存で、直前の内容が .bak に1つだけ残り、一時ファイルは
    残らない。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        _write(path, "古い内容\n".encode("utf-8"))
        doc = docmod.load_document(path)
        doc.text = "新しい内容\n"
        docmod.save_document(doc)
        assert Path(path).read_text(encoding="utf-8") == "新しい内容\n"
        assert Path(path + ".bak").read_text(encoding="utf-8") == "古い内容\n"
        # 控えは常に1つ(増え続けない)。一時ファイルも残らない。
        doc.text = "もっと新しい\n"
        docmod.save_document(doc)
        assert Path(path + ".bak").read_text(encoding="utf-8") == "新しい内容\n"
        names = os.listdir(tmp)
        assert sorted(names) == ["a.txt", "a.txt.bak"], names


def test_save_preserves_file_permissions():
    # 原子的な保存(os.replace)で新しいファイルになっても、元の
    # アクセス権は引き継がれる(レビュー req-20261113-107 の指摘)。
    # POSIX のアクセス権ビット前提の確認なので、Windows では
    # 飛ばす(Windows 側は属性ビットと作成日時の引き継ぎで、
    # test_capture_restore_file_attrs_units の win32 側が見る)。
    if os.name == "nt":
        print("skip  test_save_preserves_file_permissions: "
              "Windows には POSIX のアクセス権ビットが無いので飛ばします")
        return
    import stat as statmod
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        _write(path, "内容\n".encode("utf-8"))
        os.chmod(path, 0o640)
        doc = docmod.load_document(path)
        doc.text = "書き換えた内容\n"
        docmod.save_document(doc)
        mode = statmod.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o640, oct(mode)
        assert Path(path).read_text(encoding="utf-8") == "書き換えた内容\n"


def test_capture_restore_file_attrs_units():
    # 属性の控えと引き継ぎの単体: 控えが取れ、別のファイルへ適用
    # できる。存在しないファイルの控えは None。アクセス権ビットの
    # 値の確認は POSIX 前提なので Windows では飛ばし、Windows 側は
    # 隠し属性・作成日時の控え(windows)が取れることだけを見る。
    import stat as statmod
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.txt")
        dst = os.path.join(tmp, "dst.txt")
        _write(src, "a".encode("utf-8"))
        _write(dst, "b".encode("utf-8"))
        saved = docmod._capture_file_attrs(src)
        assert saved is not None
        if os.name == "nt":
            # Windows 固有ぶん(隠し属性・作成日時)の控えが取れる。
            assert saved["windows"] is not None
        else:
            os.chmod(src, 0o600)
            os.chmod(dst, 0o644)
            saved = docmod._capture_file_attrs(src)
            assert saved["mode"] == 0o600
            docmod._restore_file_attrs(dst, saved)
            assert statmod.S_IMODE(os.stat(dst).st_mode) == 0o600
        assert docmod._capture_file_attrs(
            os.path.join(tmp, "無い.txt")) is None
        docmod._restore_file_attrs(dst, None)   # 控え無しは無事に何もしない


def test_save_failure_keeps_original():
    """書き込みの途中で失敗しても、元のファイルは壊れない(原子性)。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        _write(path, "元の内容\n".encode("utf-8"))
        doc = docmod.load_document(path)
        doc.text = "書き換えた内容\n"
        real_replace = os.replace

        def failing_replace(_src, _dst):
            raise OSError("置き換えに失敗したふり")

        docmod.os.replace = failing_replace
        try:
            try:
                docmod.save_document(doc)
            except OSError:
                pass
            else:
                raise AssertionError("OSError が上がるはず")
        finally:
            docmod.os.replace = real_replace
        assert Path(path).read_text(encoding="utf-8") == "元の内容\n"
        # 一時ファイルは残らない(控えの .bak は先に作られるので可)。
        assert not any(".tmp" in name for name in os.listdir(tmp))


def test_mixed_newlines_detected_and_cleared():
    assert docmod.has_mixed_newlines("a\r\nb\nc")
    assert not docmod.has_mixed_newlines("a\r\nb\r\n")
    assert not docmod.has_mixed_newlines("a\nb\n")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        _write(path, "a\r\nb\nc\r\n".encode("utf-8"))
        doc = docmod.load_document(path)
        assert doc.mixed_newlines
        assert doc.newline == docmod.CRLF  # 最初の改行で決める(従来どおり)
        docmod.save_document(doc)
        assert not doc.mixed_newlines  # 保存でそろったので控えは落ちる
        assert Path(path).read_bytes() == "a\r\nb\r\nc\r\n".encode("utf-8")


def test_find_unencodable():
    assert docmod.find_unencodable("普通の文", "cp932") == []
    assert docmod.find_unencodable("雪だるま ☃ と ♥", "cp932") == ["☃", "♥"]
    assert docmod.find_unencodable("☃☃♥", "cp932") == ["☃", "♥"]  # 重複なし
    assert docmod.find_unencodable("☃", "utf-8") == []


def test_external_changed_stamp():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        _write(path, "内容1\n".encode("utf-8"))
        doc = docmod.load_document(path)
        assert not docmod.external_changed(doc)
        # 外部で書き換える(大きさも更新時刻も変える)。
        _write(path, "内容2は長い\n".encode("utf-8"))
        st = os.stat(path)
        os.utime(path, (st.st_atime, st.st_mtime + 10))
        assert docmod.external_changed(doc)
        # 自分で保存し直せば、印が更新されて「変わっていない」に戻る。
        docmod.save_document(doc)
        assert not docmod.external_changed(doc)
        # ファイルが消えたら「変わった」側に倒す。
        os.unlink(path)
        assert docmod.external_changed(doc)


def test_keymap_merge():
    table = merge_keymap()
    assert table == DEFAULT_KEYMAP and table is not DEFAULT_KEYMAP
    table = merge_keymap({"save": "Ctrl+Alt+S", "unknown": "F9", "open": 42})
    assert table["save"] == "Ctrl+Alt+S"
    assert "unknown" not in table
    assert table["open"] == DEFAULT_KEYMAP["open"]       # 型違いは捨てる
    assert merge_keymap(None) == DEFAULT_KEYMAP


def test_keymap_save_as_is_f12():
    # 名前を付けて保存は F12(Nagato の指定)。
    assert DEFAULT_KEYMAP["save_as"] == "F12"


def test_line_endings_kinds():
    # 行ごとの改行種別。最終行(改行で終わらない行)は None。
    assert docmod.line_endings("") == [None]
    assert docmod.line_endings("abc") == [None]
    assert docmod.line_endings("a\nb\n") == [docmod.LF, docmod.LF, None]
    assert docmod.line_endings("a\r\nb") == [docmod.CRLF, None]
    assert docmod.line_endings("a\rb") == [docmod.CR, None]
    assert docmod.line_endings("a\r\nb\nc\rd") == [
        docmod.CRLF, docmod.LF, docmod.CR, None]
    # 末尾が改行で終われば、最後の空行の分が付く。
    assert docmod.line_endings("a\r") == [docmod.CR, None]


def _naive_line_endings(text):
    """従来の1文字ずつ走査する形(結果の物差し)。"""
    kinds = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            kinds.append(docmod.LF)
            i += 1
        elif ch == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                kinds.append(docmod.CRLF)
                i += 2
            else:
                kinds.append(docmod.CR)
                i += 1
        else:
            i += 1
    kinds.append(None)
    return kinds


def test_line_endings_matches_naive():
    # 正規表現で数える今の形が、従来の走査と常に同じ結果になること
    # (速くしただけで、種別の並びは変えない)。
    import random

    samples = ["", "abc", "\n", "\r", "\r\n", "\n\r", "\r\r\n", "a\r",
               "a\n\nb", "\r\n\r\n", "あ\r\nい\nう\r"]
    rng = random.Random(20261113)
    tokens = ["a", "あ", " ", "\n", "\r", "\r\n", "xyz"]
    for _ in range(200):
        samples.append("".join(
            rng.choice(tokens) for _ in range(rng.randrange(0, 40))))
    for text in samples:
        assert docmod.line_endings(text) == _naive_line_endings(text), text
    # 大きな本文でも並びの長さと先頭・末尾が合う(行数 + 1)。
    big = "行です\r\n" * 20000 + "尾\n"
    kinds = docmod.line_endings(big)
    assert len(kinds) == 20002
    assert kinds[0] == docmod.CRLF and kinds[-2] == docmod.LF
    assert kinds[-1] is None


def test_load_keeps_line_endings_and_save_unifies():
    # CRLF と LF が混在したファイルは、行ごとの種別を控えて読み、
    # 保存すると doc.newline にそろい、控えもそろった形になる。
    data = "a\r\nb\nc".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "mixed.txt")
        _write(src, data)
        doc = docmod.load_document(src)
        assert doc.newline == docmod.CRLF
        assert doc.line_newlines == [docmod.CRLF, docmod.LF, None]
        docmod.save_document(doc, path=src)
        assert doc.line_newlines == [docmod.CRLF, docmod.CRLF, None]
        again = docmod.load_document(src)
        assert again.line_newlines == [docmod.CRLF, docmod.CRLF, None]
        assert Path(src).read_bytes() == "a\r\nb\r\nc".encode("utf-8")


def test_load_keeps_cr_line_endings():
    # 単独の CR で終わる行も、種別は CR として控える。
    data = "a\r\nb\nc\rd".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "mixed.txt")
        _write(src, data)
        doc = docmod.load_document(src)
        assert doc.line_newlines == [
            docmod.CRLF, docmod.LF, docmod.CR, None], doc.line_newlines


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
