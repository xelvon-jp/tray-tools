# tests/test_editor_mixed_newline.py
# 保存時の改行の混在の扱い(Nagato の決定、2026-11-13)を確かめる。
#   - 混在していない文書は、その改行のまま保存する(従来どおり)
#   - 混在した文書は、明示的な保存(上書き保存・名前を付けて保存)
#     のときに3択(混在のまま／CRLF に統一／LF に統一)で聞く。
#     やめたら保存しない
#   - ダイアログ無しの書き込み(save_to の直接呼び出し、grep して
#     置換の一括書き込み)では聞かない
# 問い合わせは窓の差し替え口(ask_mixed_newline)で受けるので、
# 実ダイアログは開かない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_mixed_newline.py
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

from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import editor  # noqa: E402
from editor import document as docmod  # noqa: E402
from editor import greplace  # noqa: E402
from editor.window import SaveOptionsDialog  # noqa: E402

APP = None

MIXED = "a\r\nb\nc\r\n".encode("utf-8")   # CRLF, LF, CRLF(doc.newline は CRLF)


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(path=None, **kwargs):
    app()
    window = editor.create_window(path, **kwargs)
    window.show()
    return window


def discard_close(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def _mixed_window(tmp, data=MIXED):
    path = os.path.join(tmp, "a.txt")
    Path(path).write_bytes(data)
    return path, make_window(path)


# ---------------------------------------------------------------
# 文書モデル(Qt なし): 行ごとの組み立てと「混在のまま」保存
# ---------------------------------------------------------------
def test_kinds_are_mixed():
    assert docmod.kinds_are_mixed([docmod.CRLF, docmod.LF, None])
    assert not docmod.kinds_are_mixed([docmod.CRLF, docmod.CRLF, None])
    assert not docmod.kinds_are_mixed([docmod.LF, None])
    assert not docmod.kinds_are_mixed([docmod.CRLF, docmod.CR, None])
    assert not docmod.kinds_are_mixed([])
    assert not docmod.kinds_are_mixed(None)


def test_join_with_line_newlines():
    text = "a\nb\nc"
    kinds = [docmod.CRLF, docmod.LF, None]
    assert docmod.join_with_line_newlines(text, kinds, docmod.CRLF) == \
        "a\r\nb\nc"
    # 控えが足りない行は既定で補い、余りは無視する。
    assert docmod.join_with_line_newlines(text, [docmod.LF], docmod.CRLF) == \
        "a\nb\r\nc"
    assert docmod.join_with_line_newlines(
        text, [docmod.LF, docmod.LF, docmod.LF, docmod.LF],
        docmod.CRLF) == "a\nb\nc"
    # 控えが無い・種別としてあり得ない値の行も既定に倒す。
    assert docmod.join_with_line_newlines(text, None, docmod.LF) == text
    assert docmod.join_with_line_newlines(
        text, ["??", docmod.LF, None], docmod.CRLF) == "a\r\nb\nc"
    # CR も行の種別として保たれる。改行の無い本文はそのまま。
    assert docmod.join_with_line_newlines(
        "a\nb", [docmod.CR, None], docmod.CRLF) == "a\rb"
    assert docmod.join_with_line_newlines("abc", kinds, docmod.CRLF) == "abc"


def test_save_document_keep_mixed_bytes():
    # line_newlines を渡して保存すると、混在のままのバイト列になり、
    # 文書の控え(混在・行ごとの種別)も混在のまま残る。
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes(MIXED)
        doc = docmod.load_document(path)
        assert doc.mixed_newlines
        docmod.save_document(doc, line_newlines=doc.line_newlines)
        assert Path(path).read_bytes() == MIXED
        assert doc.mixed_newlines
        assert doc.line_newlines == [docmod.CRLF, docmod.LF, docmod.CRLF, None]
        assert doc.newline == docmod.CRLF  # 改行設定そのものは変わらない


def test_save_document_keep_mixed_with_new_line():
    # 編集で行が増えた場合、増えた行の改行は文書の改行設定になり、
    # 元の行の種別は保たれる。
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("a\r\nb\nc".encode("utf-8"))
        doc = docmod.load_document(path)
        doc.text = "a\nb\nc\nd"  # 末尾に行を足した本文
        kinds = [docmod.CRLF, docmod.LF, docmod.CRLF, None]
        docmod.save_document(doc, line_newlines=kinds)
        assert Path(path).read_bytes() == "a\r\nb\nc\r\nd".encode("utf-8")
        assert doc.mixed_newlines


# ---------------------------------------------------------------
# 窓: 上書き保存の3択
# ---------------------------------------------------------------
def test_save_asks_and_keep_preserves_bytes_and_asks_again():
    with tempfile.TemporaryDirectory() as tmp:
        path, window = _mixed_window(tmp)
        try:
            calls = []
            window.ask_mixed_newline = lambda: calls.append(1) or "keep"
            assert window.save()
            assert calls == [1]                     # 聞かれた
            assert Path(path).read_bytes() == MIXED  # 混在のまま
            assert window._doc.mixed_newlines
            assert "改行混在" in window._enc_label.text()
            # 混在のままを選んだので、次に保存するときもまた聞く。
            assert window.save()
            assert calls == [1, 1]
            assert Path(path).read_bytes() == MIXED
        finally:
            discard_close(window)


def test_save_mixed_keep_after_edit():
    # 編集しても、元の行の改行は保たれ、増えた行は文書の改行設定
    # (この文書では CRLF)になる。
    with tempfile.TemporaryDirectory() as tmp:
        path, window = _mixed_window(tmp, "a\r\nb\nc".encode("utf-8"))
        try:
            cursor = window._edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            window._edit.setTextCursor(cursor)
            cursor.insertText("\nd")
            window.ask_mixed_newline = lambda: "keep"
            assert window.save()
            assert Path(path).read_bytes() == \
                "a\r\nb\nc\r\nd".encode("utf-8")
        finally:
            discard_close(window)


def test_save_mixed_unify_lf_updates_state_and_stops_asking():
    with tempfile.TemporaryDirectory() as tmp:
        path, window = _mixed_window(tmp)
        try:
            calls = []
            window.ask_mixed_newline = lambda: calls.append(1) or "lf"
            assert window.save()
            assert Path(path).read_bytes() == "a\nb\nc\n".encode("utf-8")
            # 選んだ結果が文書の状態に反映される(改行設定・混在・表示)。
            assert window._doc.newline == docmod.LF
            assert not window._doc.mixed_newlines
            assert window._enc_label.text() == "UTF-8 / LF"
            # 統一したので、次回の保存では聞かれない。
            window.ask_mixed_newline = lambda: calls.append(1) or "lf"
            assert window.save()
            assert calls == [1]
        finally:
            discard_close(window)


def test_save_mixed_cancel_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        path, window = _mixed_window(tmp)
        try:
            window.ask_mixed_newline = lambda: None  # やめる
            assert not window.save()
            assert Path(path).read_bytes() == MIXED
            assert not os.path.exists(path + ".bak")  # 書き込み自体が無い
            assert window._doc.mixed_newlines
        finally:
            discard_close(window)


def test_save_not_asked_when_not_mixed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("a\r\nb\r\n".encode("utf-8"))
        window = make_window(path)
        try:
            calls = []
            window.ask_mixed_newline = lambda: calls.append(1) or "keep"
            assert window.save()
            assert calls == []  # 混在していなければ聞かれない
            assert Path(path).read_bytes() == "a\r\nb\r\n".encode("utf-8")
        finally:
            discard_close(window)


def test_save_to_direct_does_not_ask():
    # ダイアログ無しで書く経路(save_to の直接呼び出し)では聞かず、
    # 従来どおり文書の改行設定でそろえる。
    with tempfile.TemporaryDirectory() as tmp:
        path, window = _mixed_window(tmp)
        try:
            calls = []
            window.ask_mixed_newline = lambda: calls.append(1) or "keep"
            assert window.save_to(path)
            assert calls == []
            assert Path(path).read_bytes() == \
                "a\r\nb\r\nc\r\n".encode("utf-8")
            assert not window._doc.mixed_newlines
        finally:
            discard_close(window)


# ---------------------------------------------------------------
# 窓: 名前を付けて保存(形式の画面に「混在のまま」が出る)
# ---------------------------------------------------------------
def test_save_options_dialog_keep_choice_only_when_mixed():
    app()
    doc = docmod.TextDocument(newline=docmod.CRLF, mixed_newlines=True)
    dialog = SaveOptionsDialog(doc)
    try:
        items = [dialog._newline.itemText(i)
                 for i in range(dialog._newline.count())]
        assert items == ["混在のまま", "CRLF", "LF"]
        # 既定は「混在のまま」で、values() の改行は None。
        assert dialog.values()[1] is None
    finally:
        dialog.close()
    doc = docmod.TextDocument(newline=docmod.LF, mixed_newlines=False)
    dialog = SaveOptionsDialog(doc)
    try:
        items = [dialog._newline.itemText(i)
                 for i in range(dialog._newline.count())]
        assert items == ["CRLF", "LF"]
        assert dialog.values()[1] == docmod.LF
    finally:
        dialog.close()


def test_save_as_with_keep_choice_preserves_mixed():
    # 形式の画面で「混在のまま」(改行=None)が返ったら、別名の
    # 保存先へも行ごとの改行を保って書く。
    import editor.window as window_module

    with tempfile.TemporaryDirectory() as tmp:
        src, window = _mixed_window(tmp)
        dst = os.path.join(tmp, "dst.txt")
        try:
            class FakeFileDialog:
                @staticmethod
                def getSaveFileName(*_args, **_kwargs):
                    return dst, ""

            class FakeOptionsDialog:
                def __init__(self, doc, parent=None):
                    self._doc = doc

                def exec(self):
                    return QDialog.DialogCode.Accepted

                def values(self):
                    return self._doc.encoding, None, self._doc.bom

            orig_file = window_module.QFileDialog
            orig_options = window_module.SaveOptionsDialog
            window_module.QFileDialog = FakeFileDialog
            window_module.SaveOptionsDialog = FakeOptionsDialog
            try:
                assert window.save_as()
            finally:
                window_module.QFileDialog = orig_file
                window_module.SaveOptionsDialog = orig_options
            assert Path(dst).read_bytes() == MIXED
            assert window._doc.path == dst
            assert window._doc.mixed_newlines
        finally:
            discard_close(window)


# ---------------------------------------------------------------
# 一括書き込み(grep して置換)では聞かない・従来どおり
# ---------------------------------------------------------------
def test_greplace_batch_write_does_not_preserve_or_ask():
    # grep して置換の書き込みは文書モデルの直接呼び出しで、窓の
    # 問い合わせは介在しない。混在ファイルも従来どおり、文書の
    # 改行設定(最初の改行)でそろえて書かれる。
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes(MIXED)
        preview = greplace.preview_replace(tmp, "b", "B")
        assert len(preview.targets) == 1
        result = greplace.execute_replace(preview, "b", "B")
        assert result.files_written == 1
        assert Path(path).read_bytes() == \
            "a\r\nB\r\nc\r\n".encode("utf-8")


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
