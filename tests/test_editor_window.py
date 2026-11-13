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

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QAction,
    QGuiApplication,
    QTextCursor,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QDialog, QToolBar  # noqa: E402

import copilot_io  # noqa: E402
import editor  # noqa: E402
from editor import outline  # noqa: E402
from editor.highlighter import STAR_BACKGROUND  # noqa: E402
from editor.keymap import DEFAULT_KEYMAP  # noqa: E402
from editor.window import (  # noqa: E402
    PATTERN_APPLYING_KEY,
    PatternSettingsDialog,
    trial_patterns,
)

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


def test_open_path_asks_before_discarding():
    """未保存の編集があるときに別のファイルを開く経路(重大1)。
    確認でキャンセルしたら内容が残り、破棄を選んだら開ける。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "first.txt")
        second = os.path.join(tmp, "second.txt")
        Path(first).write_bytes("最初の文書\n".encode("utf-8"))
        Path(second).write_bytes("次の文書\n".encode("utf-8"))
        window = make_window(first)
        try:
            type_text(window, "X")
            assert window.is_dirty()
            window.ask_save_changes = lambda: "cancel"
            assert window.open_path(second) is False
            assert window.text().startswith("X")  # 編集はそのまま残る
            assert window.current_document().path == first
            assert window.is_dirty()
            window.ask_save_changes = lambda: "discard"
            assert window.open_path(second) is True
            assert window.text() == "次の文書\n"
            assert not window.is_dirty()
        finally:
            discard_close(window)


def test_open_path_with_save_answer():
    """確認で「保存」を選んだら、元のファイルを書いてから開く。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = os.path.join(tmp, "first.txt")
        second = os.path.join(tmp, "second.txt")
        Path(first).write_bytes("前\n".encode("utf-8"))
        Path(second).write_bytes("後\n".encode("utf-8"))
        window = make_window(first)
        try:
            type_text(window, "Y")
            window.ask_save_changes = lambda: "save"
            assert window.open_path(second) is True
            assert Path(first).read_bytes() == "Y前\n".encode("utf-8")
            assert window.text() == "後\n"
        finally:
            discard_close(window)


def test_send_selection_input_occupied_confirm_flow():
    """入力欄に書きかけがあると、確認を挟んでから送り直す(重大2の補足)。"""
    calls = []

    def fake_sender(text, force=False):
        calls.append(force)
        if not force:
            return copilot_io.SendResult(
                False, "入力欄に書きかけの文があります", needs_confirm=True)
        return copilot_io.SendResult(True, "送信できました(テスト)")

    window = make_window(sender=fake_sender)
    try:
        window.ask_overwrite_input = lambda: True
        window._edit.setPlainText("送りたい範囲")
        window._edit.selectAll()
        window.send_selection()
        _wait_send(window)
        assert calls == [False, True]  # 確認のあと force で送り直した
        assert window.statusBar().currentMessage() == "送信できました(テスト)"
    finally:
        discard_close(window)


def test_send_selection_input_occupied_declined():
    """上書きの確認で断ったら、送り直さずに終わる。"""
    calls = []

    def fake_sender(text, force=False):
        calls.append(force)
        return copilot_io.SendResult(
            False, "入力欄に書きかけの文があります", needs_confirm=True)

    window = make_window(sender=fake_sender)
    try:
        window.ask_overwrite_input = lambda: False
        window._edit.setPlainText("送りたい範囲")
        window._edit.selectAll()
        window.send_selection()
        _wait_send(window)
        assert calls == [False]  # 送り直していない
        assert "送信をやめました" in window.statusBar().currentMessage()
    finally:
        discard_close(window)


def _wheel_event(delta_y, modifiers):
    return QWheelEvent(
        QPointF(5, 5), QPointF(5, 5), QPoint(0, 0), QPoint(0, delta_y),
        Qt.MouseButton.NoButton, modifiers,
        Qt.ScrollPhase.NoScrollPhase, False)


def test_line_numbers_toggle_and_persist():
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        assert window.line_numbers_visible()  # 既定はオン
        assert window._edit.line_number_area_width() > 0
        assert window._act_line_numbers.isChecked()
        window.set_line_numbers_visible(False)
        assert not window.line_numbers_visible()
        assert window._edit.line_number_area_width() == 0
        assert not window._act_line_numbers.isChecked()
        assert saved[-1] == {"show_line_numbers": False}
        window._act_line_numbers.setChecked(True)  # アクションから戻す
        assert window.line_numbers_visible()
        assert saved[-1] == {"show_line_numbers": True}
    finally:
        discard_close(window)


def test_line_numbers_off_from_settings():
    window = make_window(editor_settings={"show_line_numbers": False})
    try:
        assert not window.line_numbers_visible()
        assert window._edit.line_number_area_width() == 0
        assert not window._act_line_numbers.isChecked()
    finally:
        discard_close(window)


def test_font_default_ms_gothic():
    window = make_window()
    try:
        assert window._edit.font().family() == "MS Gothic"
        assert window.font_size() == 10
    finally:
        discard_close(window)


def test_font_from_settings():
    window = make_window(
        editor_settings={"font_family": "Consolas", "font_size": 14})
    try:
        assert window._edit.font().family() == "Consolas"
        assert window.font_size() == 14
    finally:
        discard_close(window)


def test_ctrl_wheel_changes_font_size():
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        edit = window._edit
        base = window.font_size()
        edit.wheelEvent(_wheel_event(
            120, Qt.KeyboardModifier.ControlModifier))
        assert window.font_size() == base + 1
        assert saved[-1] == {"font_size": base + 1}
        edit.wheelEvent(_wheel_event(
            -120, Qt.KeyboardModifier.ControlModifier))
        assert window.font_size() == base
        # Ctrl 無しのホイールはスクロールで、大きさは変わらない。
        edit.wheelEvent(_wheel_event(
            120, Qt.KeyboardModifier.NoModifier))
        assert window.font_size() == base
    finally:
        discard_close(window)


def test_font_size_clamp():
    window = make_window()
    try:
        edit = window._edit
        edit.set_font_size(32)
        assert window.font_size() == 32
        edit.wheelEvent(_wheel_event(
            120, Qt.KeyboardModifier.ControlModifier))
        assert window.font_size() == 32  # 上限で止まる
        edit.set_font_size(7)
        edit.wheelEvent(_wheel_event(
            -120, Qt.KeyboardModifier.ControlModifier))
        assert window.font_size() == 7  # 下限で止まる
        edit.set_font_size(100)
        assert window.font_size() == 32
    finally:
        discard_close(window)


def test_toolbar_has_main_actions():
    window = make_window()
    try:
        toolbars = window.findChildren(QToolBar)
        assert len(toolbars) == 1
        actions = toolbars[0].actions()
        for action in (window._act_open, window._act_save,
                       window._act_save_as, window._act_find,
                       window._act_send, window._act_outline,
                       window._act_line_numbers):
            assert action in actions
    finally:
        discard_close(window)


def test_outline_pane_tree_and_jump():
    window = make_window()
    try:
        window._edit.setPlainText(
            "■ 大見出し\n本文\n  ■ 小見出し\n■ 次の大見出し")
        window._rebuild_outline()
        tree = window._outline_pane.tree()
        assert tree.topLevelItemCount() == 2
        first = tree.topLevelItem(0)
        assert first.text(0) == "■ 大見出し"
        assert first.childCount() == 1
        assert first.child(0).text(0) == "■ 小見出し"
        # 子の項目を選ぶと、その行へカーソルが移る。
        tree.itemClicked.emit(first.child(0), 0)
        assert window._edit.textCursor().blockNumber() == 2
    finally:
        discard_close(window)


def test_jump_to_line_with_wrapped_lines():
    # 折り返しのある長い行が並ぶ文書でも、ブロック番号で飛ぶので
    # 目標の行そのものへ着く(見た目の行で数えると別の行へ飛ぶ)。
    window = make_window()
    try:
        window.resize(500, 400)
        lines = ["あ" * 300] * 50
        lines[19] = "■ 目標の見出し"
        window._edit.setPlainText("\n".join(lines))
        app().processEvents()
        window._jump_to_line(19)
        assert window._edit.textCursor().blockNumber() == 19
        window._jump_to_line(0)
        assert window._edit.textCursor().blockNumber() == 0
    finally:
        discard_close(window)


def test_outline_pane_toggle_and_persist():
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        assert window.outline_pane_visible()  # 既定はオン
        window.set_outline_pane_visible(False)
        assert not window.outline_pane_visible()
        assert not window._act_outline.isChecked()
        assert saved[-1] == {"show_outline_pane": False}
    finally:
        discard_close(window)
    window2 = make_window(editor_settings={"show_outline_pane": False})
    try:
        assert not window2.outline_pane_visible()
    finally:
        discard_close(window2)


def test_outline_pane_shows_file_info():
    window = make_window()
    try:
        window._edit.setPlainText("1行目\n2行目")
        pane = window._outline_pane
        assert pane._path_label.text() == "（無題）"
        assert "UTF-8" in pane._format_label.text()
        assert "BOM なし" in pane._format_label.text()
        assert pane._counts_label.text() == "2 行 / 7 文字"
    finally:
        discard_close(window)


def test_window_custom_pattern_from_settings():
    window = make_window(editor_settings={"outline_pattern": r"^第.+章"})
    try:
        window._edit.setPlainText("第一章 始まり\n本文")
        window._rebuild_outline()
        assert window._outline_pane.tree().topLevelItemCount() == 1
    finally:
        discard_close(window)


def test_window_set_patterns_reflects_immediately():
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        window._edit.setPlainText("第一章 始まり\n本文\n第二章 続き")
        window._set_patterns(r"^第.+章", r"^!{3,}")
        assert window._outline_pane.tree().topLevelItemCount() == 2
        # 適用の前に「適用中」の印が立ち、式と一緒に印が消える。
        assert saved[0] == {PATTERN_APPLYING_KEY: True}
        assert saved[-1] == {
            "outline_pattern": r"^第.+章",
            "highlight_pattern": r"^!{3,}",
            PATTERN_APPLYING_KEY: False,
        }
    finally:
        discard_close(window)


def test_pattern_dialog_rejects_invalid_regex():
    app()
    dialog = PatternSettingsDialog(
        outline.DEFAULT_OUTLINE_PATTERN, outline.DEFAULT_HIGHLIGHT_PATTERN)
    try:
        dialog._outline_edit.setText("[abc")
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert dialog.validation_error()
        assert "不正" in dialog._error_label.text()
        dialog._outline_edit.setText("^■")
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
        assert dialog.values()[0] == "^■"
    finally:
        dialog.deleteLater()
        app().processEvents()


def test_pattern_dialog_reset_defaults():
    app()
    dialog = PatternSettingsDialog("^x", "^y")
    try:
        dialog._reset_button.click()
        assert dialog.values() == (
            outline.DEFAULT_OUTLINE_PATTERN,
            outline.DEFAULT_HIGHLIGHT_PATTERN)
    finally:
        dialog.deleteLater()
        app().processEvents()


def test_pattern_guard_marks_and_clears_on_startup():
    # 通常の起動では、適用の前に印が立ち、開けたら消えている。
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        assert saved[0] == {PATTERN_APPLYING_KEY: True}
        assert saved[-1] == {PATTERN_APPLYING_KEY: False}
    finally:
        discard_close(window)


def test_pattern_guard_leftover_marker_resets_to_defaults():
    # 「適用中」の印が残った状態で開くと、前回固まった疑いとして、
    # 保存済みの式は使わず既定に戻り、設定にも書き戻される。
    saved = []
    window = make_window(
        editor_settings={
            PATTERN_APPLYING_KEY: True,
            "outline_pattern": r"^第.+章",
            "highlight_pattern": r"^!{3,}",
        },
        on_settings_change=saved.append)
    try:
        assert window._outline_pattern_text == outline.DEFAULT_OUTLINE_PATTERN
        assert (window._highlight_pattern_text
                == outline.DEFAULT_HIGHLIGHT_PATTERN)
        assert saved[0] == {
            PATTERN_APPLYING_KEY: True,
            "outline_pattern": "",
            "highlight_pattern": "",
        }
        assert saved[-1] == {PATTERN_APPLYING_KEY: False}
        # 既定の拾い方で動く(カスタムの「第…章」は拾わない)。
        window._edit.setPlainText("第一章 始まり\n■ 見出し")
        window._rebuild_outline()
        assert window._outline_pane.tree().topLevelItemCount() == 1
    finally:
        discard_close(window)


def test_pattern_dialog_rejects_slow_trial():
    app()
    dialog = PatternSettingsDialog(
        outline.DEFAULT_OUTLINE_PATTERN, outline.DEFAULT_HIGHLIGHT_PATTERN,
        trial_runner=lambda patterns: 5.0)
    try:
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "かかりすぎ" in dialog._error_label.text()
    finally:
        dialog.deleteLater()
        app().processEvents()


def test_pattern_dialog_rejects_evil_pattern():
    # ^(a+)+$ は試験文字列で破滅的に遅くなるので、実際の試験
    # (別プロセス)でも保存できない。子プロセスごと打ち切るため、
    # このテスト自体は数秒で終わる。
    app()
    dialog = PatternSettingsDialog(
        r"^(a+)+$", outline.DEFAULT_HIGHLIGHT_PATTERN)
    try:
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "かかりすぎ" in dialog._error_label.text()
    finally:
        dialog.deleteLater()
        app().processEvents()


def test_trial_patterns_benign_returns_seconds():
    seconds = trial_patterns(
        [outline.DEFAULT_OUTLINE_PATTERN, outline.DEFAULT_HIGHLIGHT_PATTERN])
    assert seconds is not None
    assert seconds < outline.TRIAL_TIME_LIMIT_SEC, seconds


def test_star_line_highlight():
    window = make_window()
    try:
        window._edit.setPlainText("普通の行\n★ 重要な行\n  ☆ メモ")
        window._highlighter.rehighlight()
        blocks = []
        block = window._edit.document().firstBlock()
        while block.isValid():
            blocks.append(block)
            block = block.next()
        formats = [b.layout().formats() for b in blocks]
        assert formats[0] == []
        assert formats[1], "星印の行に書式が付いていません"
        assert (formats[1][0].format.background().color()
                == STAR_BACKGROUND)
        assert formats[2], "空白の後の星印の行に書式が付いていません"
        assert (formats[2][0].format.background().color()
                == STAR_BACKGROUND)
    finally:
        discard_close(window)


def test_cut_to_end_of_line():
    window = make_window()
    try:
        window._edit.setPlainText("hello world")
        window._edit.document().setModified(False)
        cursor = window._edit.textCursor()
        cursor.setPosition(5)
        window._edit.setTextCursor(cursor)
        window.cut_to_end_of_line()
        assert window.text() == "hello"
        assert QGuiApplication.clipboard().text() == " world"
        assert window.is_dirty()
        window._edit.undo()  # 1回の undo で戻る
        assert window.text() == "hello world"
    finally:
        discard_close(window)


def test_cut_to_end_at_block_end_does_nothing():
    window = make_window()
    try:
        window._edit.setPlainText("abc")
        window._edit.document().setModified(False)
        cursor = window._edit.textCursor()
        cursor.setPosition(3)
        window._edit.setTextCursor(cursor)
        QGuiApplication.clipboard().setText("前のクリップボード")
        window.cut_to_end_of_line()
        assert window.text() == "abc"
        assert QGuiApplication.clipboard().text() == "前のクリップボード"
        assert not window.is_dirty()
    finally:
        discard_close(window)


def test_cut_to_end_with_selection_cuts_selection():
    window = make_window()
    try:
        window._edit.setPlainText("hello world")
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(4, QTextCursor.MoveMode.KeepAnchor)
        window._edit.setTextCursor(cursor)
        window.cut_to_end_of_line()
        assert window.text() == "o world"
        assert QGuiApplication.clipboard().text() == "hell"
    finally:
        discard_close(window)


def test_cut_to_end_keymap_default():
    window = make_window()
    try:
        assert DEFAULT_KEYMAP["cut_to_end"] == "Ctrl+K"
        action = next(
            a for a in window.findChildren(QAction)
            if a.text().startswith("行末まで切り取り"))
        assert action.shortcut().toString() == "Ctrl+K"
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
