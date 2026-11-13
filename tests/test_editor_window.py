# tests/test_editor_window.py
# テキストエディタの窓(editor/window.py)を offscreen で確かめる。
# 開く・保存(文字コードと改行の保持)・検索・未保存の確認・Copilot へ
# 送る(fake の sender)まで。実ファイルと実 Qt を使うが、画面は出さない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_window.py
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import copilot_io  # noqa: E402
import editor  # noqa: E402
from editor.keymap import DEFAULT_KEYMAP  # noqa: E402

APP = None


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
    window.ask_save_changes = lambda: "discard"
    window.close()
    window.deleteLater()
    app().processEvents()


def type_text(window, text):
    """実際の編集と同じ経路(アンドゥに乗り、変更ありになる)で入れる。"""
    window._edit.textCursor().insertText(text)


def test_open_sjis_crlf_and_save_roundtrip():
    data = "1行目\r\n日本語です\r\n".encode("cp932")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes(data)
        window = make_window(path)
        try:
            assert window.text() == "1行目\n日本語です\n"
            assert window._enc_label.text().startswith("SJIS / CRLF")
            assert "a.txt" in window.windowTitle()
            assert "*" not in window.windowTitle()
            assert window.save()  # 触らずに上書き保存 → バイト列は不変
            assert Path(path).read_bytes() == data
        finally:
            discard_close(window)


def test_edit_marks_dirty_and_save_keeps_format():
    data = "abc\n".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "b.txt")
        Path(path).write_bytes(data)
        window = make_window(path)
        try:
            type_text(window, "Z")
            assert "*" in window.windowTitle()
            assert window.save()
            assert "*" not in window.windowTitle()
            # LF のまま、末尾に Z が付く(改行が CRLF に化けない)
            assert Path(path).read_bytes() == "Zabc\n".encode("utf-8")
        finally:
            discard_close(window)


def test_save_to_converts_encoding():
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "c.txt")
        dst = os.path.join(tmp, "d.txt")
        Path(src).write_bytes("日本語\n".encode("utf-8"))
        window = make_window(src)
        try:
            assert window.save_to(dst, encoding="cp932")
            assert Path(dst).read_bytes() == "日本語\n".encode("cp932")
            assert window._enc_label.text().startswith("SJIS")
            assert window.current_document().path == dst
        finally:
            discard_close(window)


def test_find_next_prev_and_wrap():
    window = make_window()
    try:
        window._edit.setPlainText("foo bar foo baz foo")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        assert window._edit.textCursor().selectedText() == "foo"
        assert window._edit.textCursor().selectionStart() == 0
        assert window._do_find(True)
        assert window._edit.textCursor().selectionStart() == 8
        assert window._do_find(True)
        assert window._edit.textCursor().selectionStart() == 16
        assert window._do_find(True)  # 末尾の次は先頭へ巻き戻る
        assert window._edit.textCursor().selectionStart() == 0
        assert window._do_find(False)  # 前へ: 先頭の前は末尾へ
        assert window._edit.textCursor().selectionStart() == 16
        window._find_edit.setText("zzz")
        assert not window._do_find(True)
    finally:
        discard_close(window)


def test_find_case_sensitivity():
    window = make_window()
    try:
        window._edit.setPlainText("foo")
        window.show_find_bar()
        window._find_edit.setText("FOO")
        assert window._do_find(True)          # 既定は区別しない
        window._edit.textCursor().setPosition(0)
        window._find_case.setChecked(True)
        assert not window._do_find(True)      # 区別すると見つからない
    finally:
        discard_close(window)


def test_close_confirmation():
    window = make_window()
    emitted = []
    window.closed.connect(emitted.append)
    try:
        type_text(window, "x")
        window.ask_save_changes = lambda: "cancel"
        window.close()
        assert window.isVisible() and emitted == []   # キャンセルで閉じない
        window.ask_save_changes = lambda: "discard"
        window.close()
        assert not window.isVisible()
        assert len(emitted) == 1 and len(emitted[0]) == 4  # ジオメトリ [x,y,w,h]
    finally:
        window.deleteLater()
        app().processEvents()


def _wait_send(window, timeout=5.0):
    deadline = time.time() + timeout
    while window._send_thread is not None and time.time() < deadline:
        app().processEvents()
        time.sleep(0.02)
    app().processEvents()
    assert window._send_thread is None, "送信スレッドが終わりません"


def test_send_selection_via_sender():
    sent = []

    def fake_sender(text):
        sent.append(text)
        return copilot_io.SendResult(True, "送信できました(テスト)")

    window = make_window(sender=fake_sender)
    try:
        window._edit.setPlainText("1行目\n2行目")
        window._edit.selectAll()
        window.send_selection()
        _wait_send(window)
        # 段落区切りは U+2029 ではなく \n で渡る
        assert sent == ["1行目\n2行目"], sent
        assert window.statusBar().currentMessage() == "送信できました(テスト)"
    finally:
        discard_close(window)


def test_send_without_selection_and_failure():
    sent = []
    window = make_window(sender=lambda text: sent.append(text)
                         or copilot_io.SendResult(False, "失敗の文"))
    try:
        window._edit.setPlainText("abc")
        window.send_selection()  # 選択が無いので送らない
        assert sent == []
        assert "選択範囲がありません" in window.statusBar().currentMessage()
        window._edit.selectAll()
        window.send_selection()
        _wait_send(window)
        assert sent == ["abc"]
        assert window.statusBar().currentMessage() == "失敗の文"
    finally:
        discard_close(window)


def test_keymap_shortcuts_and_override():
    window = make_window()
    try:
        by_text = {}
        for action in window.findChildren(QAction):
            by_text.setdefault(action.text(), action)
        save_action = next(a for t, a in by_text.items() if t.startswith("上書き保存"))
        assert save_action.shortcut().toString() == DEFAULT_KEYMAP["save"]
    finally:
        discard_close(window)
    window2 = make_window(keymap_overrides={"save": "Ctrl+Alt+S"})
    try:
        save_action = next(
            a for a in window2.findChildren(QAction)
            if a.text().startswith("上書き保存"))
        assert save_action.shortcut().toString() == "Ctrl+Alt+S"
    finally:
        discard_close(window2)


def test_new_document_after_discard():
    window = make_window()
    try:
        type_text(window, "何か")
        window.ask_save_changes = lambda: "discard"
        window.new_document()
        assert window.text() == ""
        assert "無題" in window.windowTitle()
        assert "*" not in window.windowTitle()
    finally:
        discard_close(window)


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
