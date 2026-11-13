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
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QToolBar  # noqa: E402

import copilot_io  # noqa: E402
import editor  # noqa: E402
from editor import macros as macmod  # noqa: E402
from editor import outline  # noqa: E402
from editor import rxfind  # noqa: E402
from editor.highlighter import (  # noqa: E402
    CODE_BLOCK_BACKGROUND,
    HEADING_BACKGROUNDS,
    INLINE_CODE_BACKGROUND,
    STAR_BACKGROUND,
)
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


def wait_rx(window, timeout_ms=15000):
    """正規表現の非同期処理が終わるまで待つ(結果が本文や選択や
    ステータスに反映されるのは、裏スレッドからのシグナルの後)。"""
    waited = 0
    while window._rx_pending is not None and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    assert window._rx_pending is None, "正規表現の処理が終わらない"
    QTest.qWait(10)


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
        # 遅延保存なので、その場ではまだ書かれない(書き込みの嵐対策)。
        assert all("font_size" not in entry for entry in saved)
        edit.wheelEvent(_wheel_event(
            -120, Qt.KeyboardModifier.ControlModifier))
        assert window.font_size() == base
        # 止まってから保存が流れると、最後の大きさが1回だけ書かれる。
        window._flush_pending_font_size()
        font_writes = [e for e in saved if "font_size" in e]
        assert font_writes == [{"font_size": base}]
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


def _select_blocks(window, first, last, to_line_start=False):
    """first〜last ブロックを選択する。to_line_start が真なら選択の
    終わりを last ブロックの行頭にする(その行を含めない形)。"""
    document = window._edit.document()
    start = document.findBlockByNumber(first).position()
    end_block = document.findBlockByNumber(last)
    end = end_block.position()
    if not to_line_start:
        end += len(end_block.text())
    cursor = window._edit.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    window._edit.setTextCursor(cursor)


def test_sort_lines_whole_document_and_undo():
    window = make_window()
    try:
        window._edit.setPlainText("c\na\nb")
        assert window.execute_command("sort_lines", {"order": "asc"})
        assert window.text() == "a\nb\nc"
        # 全体が1回の undo で戻る。
        window._edit.undo()
        assert window.text() == "c\na\nb"
        assert window.execute_command("sort_lines", {"order": "desc"})
        assert window.text() == "c\nb\na"
        # 既にそろっている文書では何も起きない。
        assert window.execute_command("sort_lines", {"order": "desc"})
        assert window.text() == "c\nb\na"
    finally:
        discard_close(window)


def test_sort_lines_selection_range():
    window = make_window()
    try:
        window._edit.setPlainText("b\na\nd\nc")
        _select_blocks(window, 0, 1)
        assert window.execute_command("sort_lines", {"order": "asc"})
        # 対象は選択の2行だけ。残りは動かない。
        assert window.text() == "a\nb\nd\nc"
        # 選択の終わりが行頭ちょうどのときは、その行は含めない。
        window._edit.setPlainText("b\na\nd\nc")
        _select_blocks(window, 0, 2, to_line_start=True)
        assert window.execute_command("sort_lines", {"order": "asc"})
        assert window.text() == "a\nb\nd\nc"
    finally:
        discard_close(window)


def test_remove_duplicate_lines():
    window = make_window()
    try:
        window._edit.setPlainText("a\nb\na\nc\nb")
        assert window.execute_command("remove_duplicate_lines")
        # 最初の出現を残し、順番は保たれる。1回の undo で戻る。
        assert window.text() == "a\nb\nc"
        window._edit.undo()
        assert window.text() == "a\nb\na\nc\nb"
        # 範囲の中だけが対象(範囲外の重複は残る)。
        _select_blocks(window, 0, 2)
        assert window.execute_command("remove_duplicate_lines")
        assert window.text() == "a\nb\nc\nb"
        # 同じ範囲に重複が無ければ何も変わらない。
        _select_blocks(window, 0, 2)
        assert window.execute_command("remove_duplicate_lines")
        assert window.text() == "a\nb\nc\nb"
    finally:
        discard_close(window)


def test_sort_and_dedupe_keep_trailing_newline():
    window = make_window()
    try:
        # 末尾の改行の後ろの空ブロックは対象から外す。含めると
        # 並べ替えで末尾の改行が消え、空行が先頭へ来る(レビュー指摘)。
        window._edit.setPlainText("b\na\n")
        assert window.execute_command("sort_lines", {"order": "asc"})
        assert window.text() == "a\nb\n"
        window._edit.setPlainText("a\na\n")
        assert window.execute_command("remove_duplicate_lines")
        assert window.text() == "a\n"
    finally:
        discard_close(window)


def test_convert_commands_on_selection():
    window = make_window()
    try:
        window._edit.setPlainText("ＡＢＣ１２３ カタカナ ガ あ")
        window._edit.selectAll()
        assert window.execute_command("convert_halfwidth")
        # 英数字・カタカナ(濁点は分解)・スペースが半角に。ひらがなは不変。
        assert window.text() == "ABC123 ｶﾀｶﾅ ｶﾞ あ"
        window._edit.undo()
        assert window.text() == "ＡＢＣ１２３ カタカナ ガ あ"
        window._edit.selectAll()
        assert window.execute_command("convert_lower")
        assert window.text() == "ＡＢＣ１２３ カタカナ ガ あ"  # 全角英字は対象外
        window._edit.setPlainText("aBc ｱｲｳ")
        window._edit.selectAll()
        assert window.execute_command("convert_upper")
        assert window.text() == "ABC ｱｲｳ"
        window._edit.selectAll()
        assert window.execute_command("convert_fullwidth")
        assert window.text() == "ＡＢＣ　アイウ"
        # 選択が無ければ何もしない。
        window._edit.setPlainText("Ａ")
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        window._edit.setTextCursor(cursor)
        assert window.execute_command("convert_halfwidth")
        assert window.text() == "Ａ"
    finally:
        discard_close(window)


def test_sort_and_convert_are_recorded_and_replayed():
    window = make_window()
    try:
        window._edit.setPlainText("b\na")
        window.set_macro_recording(True)
        window.execute_command("sort_lines", {"order": "asc"})
        window.set_macro_recording(False)
        commands = window.recorded_commands()
        assert {"cmd": "sort_lines", "args": {"order": "asc"}} in commands
        # JSON の往復でも新しい命令が通る(手書きマクロと同じ形)。
        _name, parsed = macmod.macro_from_json(
            macmod.macro_to_json(commands))
        assert parsed == commands
        other = make_window()
        try:
            other._edit.setPlainText("d\nc")
            assert other.play_macro(parsed)
            assert other.text() == "c\nd"
            hand = [{"cmd": "convert_upper", "args": {}},
                    {"cmd": "sort_lines", "args": {"order": "desc"}}]
            other._edit.setPlainText("b\na")
            other._edit.selectAll()
            assert other.play_macro(hand)
            assert other.text() == "B\nA"
        finally:
            discard_close(other)
    finally:
        discard_close(window)


def test_ctrl_k_scoped_to_text_edit():
    window = make_window()
    try:
        window._edit.setPlainText("hello world")
        # 検索欄にフォーカスがあるとき、Ctrl+K は本文に効かない。
        window.show_find_bar()
        app().processEvents()
        assert window._find_edit.hasFocus()
        QTest.keyClick(window._find_edit, Qt.Key.Key_K,
                       Qt.KeyboardModifier.ControlModifier)
        app().processEvents()
        assert window.text() == "hello world"
        # 本文にフォーカスがあれば、行頭からなら行全体が消える。
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        window._edit.setTextCursor(cursor)
        window._edit.setFocus()
        app().processEvents()
        QTest.keyClick(window._edit, Qt.Key.Key_K,
                       Qt.KeyboardModifier.ControlModifier)
        app().processEvents()
        assert window.text() == ""
    finally:
        discard_close(window)


def _count_tree_items(item):
    total = 1
    for i in range(item.childCount()):
        total += _count_tree_items(item.child(i))
    return total


def test_outline_truncates_at_limit():
    window = make_window()
    try:
        total = outline.OUTLINE_ENTRY_LIMIT + 100
        window._edit.setPlainText(
            "\n".join(f"■ 見出し{i}" for i in range(total)))
        window._rebuild_outline()
        pane = window._outline_pane
        count = sum(
            _count_tree_items(pane.tree().topLevelItem(i))
            for i in range(pane.tree().topLevelItemCount()))
        assert count == outline.OUTLINE_ENTRY_LIMIT
        assert pane._truncated_label.isVisibleTo(pane)
        # 少ない文書では注意書きは出ない。
        window._edit.setPlainText("■ 一つだけ")
        window._rebuild_outline()
        assert not pane._truncated_label.isVisibleTo(pane)
    finally:
        discard_close(window)


def test_set_patterns_with_defaults_saves_empty():
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        # 「既定に戻す」相当: 既定と同じ式は、文字列でなく空で保存する
        # (空 = 組み込み既定を使う)。
        window._set_patterns(outline.DEFAULT_OUTLINE_PATTERN,
                             outline.DEFAULT_HIGHLIGHT_PATTERN)
        assert saved[-1]["outline_pattern"] == ""
        assert saved[-1]["highlight_pattern"] == ""
        # 拾い方そのものは既定のまま変わらない。
        window._edit.setPlainText("■ 見出し\n本文")
        window._rebuild_outline()
        assert window._outline_pane.tree().topLevelItemCount() == 1
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# 第3段: アイコン・正規表現の検索/置換・外部変更・保存の確認・表示
# ---------------------------------------------------------------
def test_window_icon_is_editor_icon():
    window = make_window()
    try:
        icon = window.windowIcon()
        assert not icon.isNull()
        assert icon.availableSizes()
    finally:
        discard_close(window)


def test_regex_find_selects_matches():
    window = make_window()
    try:
        window._edit.setPlainText("foo 123 bar 45")
        window._find_edit.setText(r"\d+")
        window._find_regex.setChecked(True)
        assert window.execute_command("find_next")
        wait_rx(window)   # 正規表現の検索は非同期(裏スレッド)
        assert window._edit.textCursor().selectedText() == "123"
        assert window.execute_command("find_next")
        wait_rx(window)
        assert window._edit.textCursor().selectedText() == "45"
        # 記録の引数には、正規表現で探したことが残る。
        name, args = window._recorder.last_command
        assert name == "find_next"
        assert args["text"] == r"\d+" and args["regex"] is True
    finally:
        discard_close(window)


def test_regex_replace_all_with_group_reference():
    window = make_window()
    try:
        window._edit.setPlainText("abc123 def456")
        window._find_edit.setText(r"(\d+)")
        window._replace_edit.setText(r"[\1]")
        window._find_regex.setChecked(True)
        assert window.execute_command("replace_all")
        wait_rx(window)   # 正規表現の置換は非同期(裏スレッド)
        assert window.text() == "abc[123] def[456]"
        name, args = window._recorder.last_command
        assert name == "replace_all"
        assert args == {"text": r"(\d+)", "repl": r"[\1]",
                        "case": False, "regex": True}
        # 全体が1回の undo で戻る。
        window._edit.undo()
        assert window.text() == "abc123 def456"
    finally:
        discard_close(window)


def test_replace_next_plain_and_regex():
    window = make_window()
    try:
        window._edit.setPlainText("foo foo foo")
        window._find_edit.setText("foo")
        window._replace_edit.setText("bar")
        assert window.execute_command("replace_next")
        assert window.text() == "bar foo foo"
        assert window._edit.textCursor().selectedText() == "bar"
        assert window.execute_command("replace_next")
        assert window.text() == "bar bar foo"
        # 正規表現でも1件ずつ置ける。
        window._edit.setPlainText("a1 a2")
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        window._edit.setTextCursor(cursor)
        window._find_edit.setText(r"(\d)")
        window._replace_edit.setText(r"<\1>")
        window._find_regex.setChecked(True)
        assert window.execute_command("replace_next")
        wait_rx(window)   # 正規表現の置換は非同期(裏スレッド)
        assert window.text() == "a<1> a2"
    finally:
        discard_close(window)


def test_regex_replace_runs_off_gui_thread():
    # 遅い正規表現でも、待ちは裏スレッド: execute_command は処理の
    # 完了を待たずに戻り、結果は後から反映される(レビュー
    # req-20261113-107 の指摘)。
    window = make_window()
    original = rxfind.run_operation

    def slow_run(op, **kwargs):
        time.sleep(0.4)
        return original(op, **kwargs)

    try:
        window._edit.setPlainText("a1 b2")
        window._find_edit.setText(r"(\d)")
        window._replace_edit.setText(r"<\1>")
        window._find_regex.setChecked(True)
        rxfind.run_operation = slow_run
        started = time.monotonic()
        assert window.execute_command("replace_all")
        elapsed = time.monotonic() - started
        assert elapsed < 0.3, elapsed      # GUI スレッドで待っていない
        assert window.text() == "a1 b2"    # まだ反映されていない
        wait_rx(window)
        assert window.text() == "a<1> b<2>"
    finally:
        rxfind.run_operation = original
        discard_close(window)


def test_regex_invalid_pattern_reports_error():
    window = make_window()
    try:
        window._edit.setPlainText("abc")
        window._find_edit.setText("(")
        window._find_regex.setChecked(True)
        window.execute_command("find_next")
        wait_rx(window)   # 不正な式の知らせも非同期で返る
        assert "不正" in window.statusBar().currentMessage()
        assert window.text() == "abc"  # 本文は変わらない
    finally:
        discard_close(window)


def _rewrite_externally(path, data: bytes):
    """外部の道具が書き換えたふり(大きさと更新時刻を両方変える)。"""
    Path(path).write_bytes(data)
    st = os.stat(path)
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def test_external_overwrite_needs_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("元の文\n".encode("utf-8"))
        window = make_window(path)
        try:
            _rewrite_externally(path, "外部の文（長い）\n".encode("utf-8"))
            type_text(window, "X")
            window.ask_external_overwrite = lambda: False
            assert not window.save()  # 確認で断ると保存しない
            assert Path(path).read_bytes() == "外部の文（長い）\n".encode("utf-8")
            assert window.is_dirty()
            window.ask_external_overwrite = lambda: True
            assert window.save()      # 承知すれば上書きする
            assert "X" in Path(path).read_text(encoding="utf-8")
        finally:
            discard_close(window)


def test_external_change_reload_when_clean():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("前\n".encode("utf-8"))
        window = make_window(path)
        try:
            _rewrite_externally(path, "外部で変わった本文\n".encode("utf-8"))
            window.ask_external_reload = lambda: True
            window._handle_external_change()
            assert window.text() == "外部で変わった本文\n"
            assert not window.is_dirty()
            assert not window.external_change_pending
        finally:
            discard_close(window)


def test_external_change_pending_when_dirty():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("前\n".encode("utf-8"))
        window = make_window(path)
        try:
            type_text(window, "編")
            _rewrite_externally(path, "外部で変わった本文\n".encode("utf-8"))
            window.ask_external_reload = lambda: True
            window._handle_external_change()
            # 編集中は読み直さず、知らせるだけ(編集は残る)。
            assert window.external_change_pending
            assert "編" in window.text()
            assert "外部で変更" in window.statusBar().currentMessage()
        finally:
            discard_close(window)


def test_unencodable_save_needs_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("本文\n".encode("cp932"))
        window = make_window(path)
        try:
            type_text(window, "☃")  # cp932 では書けない文字
            seen = []
            window.ask_unencodable_save = (
                lambda chars: seen.append(chars) or False)
            assert not window.save()  # 断ると保存しない(黙って化けない)
            assert seen and "☃" in seen[0]
            assert Path(path).read_bytes() == "本文\n".encode("cp932")
            window.ask_unencodable_save = lambda chars: True
            assert window.save()      # 承知すれば置換文字で書く
            assert b"?" in Path(path).read_bytes()
        finally:
            discard_close(window)


def test_mixed_newline_notice_on_open():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("a\r\nb\nc\r\n".encode("utf-8"))
        window = make_window(path)
        try:
            assert "混在" in window.statusBar().currentMessage()
        finally:
            discard_close(window)


def test_find_window_for_path_helper():
    from editor.window import find_window_for_path
    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "a.txt")
        p2 = os.path.join(tmp, "b.txt")
        Path(p1).write_text("a", encoding="utf-8")
        Path(p2).write_text("b", encoding="utf-8")
        window = make_window(p1)
        try:
            # 同じファイルを開いている窓が見つかる(二重に開かない)。
            assert find_window_for_path([window], p1) is window
            assert find_window_for_path([window], p2) is None
            assert find_window_for_path([], p1) is None
            assert find_window_for_path([window], None) is None
        finally:
            discard_close(window)


def test_editor_package_public_surface():
    # あっふ(filer)とトレイ(feature_screen)は `import editor` して
    # パッケージの公開名だけを使う。__init__ の公開が欠けると入口が
    # 全部 AttributeError で落ちる(レビュー req-20261113-108 の指摘)。
    # editor.window からでなく、パッケージ経由で確かめる。
    for name in ("create_window", "dirty_window_state",
                 "find_window_for_path", "EditorWindow"):
        assert callable(getattr(editor, name)), name
    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "a.txt")
        Path(p1).write_text("a", encoding="utf-8")
        window = make_window(p1)
        try:
            assert editor.find_window_for_path([window], p1) is window
            assert editor.find_window_for_path([window], None) is None
        finally:
            discard_close(window)


def test_view_toggles_and_color_scheme():
    from PySide6.QtGui import QColor, QPalette
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        window.set_invisibles_visible(True)
        assert window.invisibles_visible()
        assert saved[-1] == {"show_invisibles": True}
        # 現在行の強調は extra selections に乗る(矩形と同居)。
        assert len(window._edit.extraSelections()) == 1
        window.set_current_line_highlight(False)
        assert not window.current_line_highlight()
        assert len(window._edit.extraSelections()) == 0
        window.set_wide_cursor(True)
        assert window.wide_cursor()
        assert window._edit.cursorWidth() == 3
        window.apply_color_scheme("秀丸風")
        assert window.color_scheme_name() == "秀丸風"
        assert window._edit.palette().color(
            QPalette.ColorRole.Base) == QColor("#ffffff")
        assert window._edit.color_scheme()["current_line"] == "#ffffcc"
        assert saved[-1] == {"color_scheme": "秀丸風"}
    finally:
        discard_close(window)


def test_view_settings_read_at_open():
    window = make_window(editor_settings={
        "show_invisibles": True, "highlight_current_line": False,
        "wide_cursor": True, "color_scheme": "秀丸風"})
    try:
        assert window.invisibles_visible()
        assert not window.current_line_highlight()
        assert window.wide_cursor()
        assert window.color_scheme_name() == "秀丸風"
    finally:
        discard_close(window)


def test_wrap_modes_switch_and_persist():
    from PySide6.QtWidgets import QPlainTextEdit
    saved = []
    window = make_window(on_settings_change=saved.append)
    try:
        window._edit.setPlainText("あ" * 100 + "\n短い行")
        before = window.text()
        # 既定はウィンドウ幅。
        assert window.wrap_mode() == "window"
        assert (window._edit.lineWrapMode()
                == QPlainTextEdit.LineWrapMode.WidgetWidth)
        window.resize(1600, 600)
        app().processEvents()
        window.set_wrap_mode("column")
        assert window.wrap_mode() == "column"
        # 桁数指定は QPlainTextEdit に無いので、折り返し自体は窓幅で
        # 行い、右の余白で見える幅を桁数ぶんに狭める。
        assert (window._edit.lineWrapMode()
                == QPlainTextEdit.LineWrapMode.WidgetWidth)
        assert window.wrap_column() == 80   # 既定は80桁
        assert window._edit.wrap_column() == 80
        assert saved[-1] == {"wrap_mode": "column"}
        margin = window._edit.viewportMargins().right()
        assert margin > 0, margin
        window.set_wrap_column(40)
        assert window.wrap_column() == 40
        assert window._edit.wrap_column() == 40
        assert saved[-1] == {"wrap_column": 40}
        # 桁数が減ると余白は広がる。
        assert window._edit.viewportMargins().right() > margin
        window.set_wrap_mode("none")
        assert (window._edit.lineWrapMode()
                == QPlainTextEdit.LineWrapMode.NoWrap)
        # 方式を離れたら余白は戻る。
        assert window._edit.wrap_column() is None
        assert window._edit.viewportMargins().right() == 0
        assert saved[-1] == {"wrap_mode": "none"}
        window.set_wrap_mode("window")
        assert (window._edit.lineWrapMode()
                == QPlainTextEdit.LineWrapMode.WidgetWidth)
        assert window._edit.viewportMargins().right() == 0
        # 折り返しは見た目だけ: 文書は変わっていない。
        assert window.text() == before
        assert not window._edit.document().isModified()
        assert window._wrap_actions["window"].isChecked()
        assert not window._wrap_actions["none"].isChecked()
    finally:
        discard_close(window)


def test_wrap_settings_read_at_open():
    window = make_window(editor_settings={
        "wrap_mode": "column", "wrap_column": 42})
    try:
        assert window.wrap_mode() == "column"
        assert window.wrap_column() == 42
        assert window._edit.wrap_column() == 42
    finally:
        discard_close(window)


def test_wrap_right_margin_calculation():
    # 桁数折り返しの余白の計算(純粋関数)。目標の本文幅は
    # 桁数 × 文字幅 + 文書の余白(両側)。第1引数は「余白で動かない幅」。
    from editor.text_edit import wrap_right_margin
    # 幅1000・文字幅10・文書の余白4・80桁: 目標808、余白は192。
    assert wrap_right_margin(1000, 10, 4, 80) == 192
    # 幅が足りなければ 0(負にはしない)。
    assert wrap_right_margin(500, 10, 4, 80) == 0
    # 桁数を減らすと余白は広がる。
    assert wrap_right_margin(1000, 10, 4, 40) == 592
    # 桁数なし・文字幅なしは 0。
    assert wrap_right_margin(1000, 10, 4, None) == 0
    assert wrap_right_margin(1000, 0, 4, 80) == 0


def test_wrap_column_margin_tracks_resize_and_font():
    # 桁数方式の余白は、窓のリサイズとフォントサイズの変更に追随する。
    window = make_window(editor_settings={
        "wrap_mode": "column", "wrap_column": 80})
    try:
        edit = window._edit
        window.resize(1600, 600)
        app().processEvents()
        margin_wide = edit.viewportMargins().right()
        assert margin_wide > 0
        # 見える幅がほぼ目標(80桁ぶん)に収まっている。スクロールバー
        # の幅は常時差し引いて計算するので、その分までのずれは許す。
        char = edit.fontMetrics().averageCharWidth()
        target = 80 * char + 2 * edit.document().documentMargin()
        assert abs(edit.viewport().width() - target) <= (
            edit._scrollbar_extent() + char)
        # 窓を狭めると余白は減り、目標以下では 0 になる。
        window.resize(700, 600)
        app().processEvents()
        assert edit.viewportMargins().right() < margin_wide
        window.resize(320, 600)
        app().processEvents()
        assert edit.viewportMargins().right() == 0
        # フォントを大きくすると文字幅が広がり、余白は減る。
        window.resize(1600, 600)
        app().processEvents()
        before = edit.viewportMargins().right()
        assert before > 0
        edit.set_font_size(edit.font_size() + 6)
        app().processEvents()
        assert edit.viewportMargins().right() < before
    finally:
        discard_close(window)


def test_wrap_column_font_change_does_not_recurse():
    # 再現手順(レビュー req-20261113-109): 桁数40で窓を広げてから
    # フォントを大きくすると、余白の再計算が resizeEvent と往復して
    # スタックオーバーフローでプロセスごと落ちた。処理が終わることと、
    # 余白が安定している(再計算しても変わらない)ことを確かめる。
    window = make_window(editor_settings={
        "wrap_mode": "column", "wrap_column": 40})
    try:
        window.resize(1600, 600)
        app().processEvents()
        edit = window._edit
        edit.set_font_size(20)
        app().processEvents()
        first = edit.viewportMargins().right()
        edit._update_area_width()
        app().processEvents()
        assert edit.viewportMargins().right() == first
    finally:
        discard_close(window)


def _select(window, start, end):
    cursor = window._edit.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    window._edit.setTextCursor(cursor)
    window._refresh_position()
    return window._pos_label.text()


def test_selection_counts_in_status_bar():
    window = make_window()
    try:
        window._edit.setPlainText("ab\ncd\nef")
        # 選択が無いときは従来の表示のまま。
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        window._edit.setTextCursor(cursor)
        window._refresh_position()
        assert window._pos_label.text() == "1 行 1 桁"
        # 全文: 改行2つを含む8文字、3行。
        assert _select(window, 0, 8) == "3 行 3 桁（選択 8 文字 / 3 行）"
        # 1行の中の選択: 2文字、1行。
        assert "（選択 2 文字 / 1 行）" in _select(window, 0, 2)
        # 行末の改行まで含む選択: 3文字で、またがる行は1行。
        assert "（選択 3 文字 / 1 行）" in _select(window, 0, 3)
        # 改行をまたぐ選択: "b\nc" は3文字、2行。
        assert "（選択 3 文字 / 2 行）" in _select(window, 1, 4)
    finally:
        discard_close(window)


def _block_formats(window):
    """各行(ブロック)の書式の並びを返す。"""
    window._highlighter.rehighlight()
    result = []
    block = window._edit.document().firstBlock()
    while block.isValid():
        result.append(block.layout().formats())
        block = block.next()
    return result


def test_markdown_heading_background():
    # .md では見出し行全体に背景が付き、階層で濃さが変わる。
    # .txt では効かない。切り替え(設定)でも消える。
    with tempfile.TemporaryDirectory() as tmp:
        md_path = os.path.join(tmp, "note.md")
        Path(md_path).write_text(
            "# 見出し1\n## 見出し2\n普通の行\n", encoding="utf-8")
        txt_path = os.path.join(tmp, "note.txt")
        Path(txt_path).write_text("# 見出し1\n", encoding="utf-8")
        window = make_window(md_path)
        try:
            assert window._highlighter.markdown_enabled()
            formats = _block_formats(window)
            assert formats[0], "見出し1に書式が付いていません"
            assert (formats[0][0].format.background().color()
                    == HEADING_BACKGROUNDS[0])
            assert (formats[1][0].format.background().color()
                    == HEADING_BACKGROUNDS[1])
            assert HEADING_BACKGROUNDS[0] != HEADING_BACKGROUNDS[1]
            assert formats[2] == []
            # 切り替えで消える(設定にも残る)。
            window.set_markdown_highlight(False)
            assert not window._highlighter.markdown_enabled()
            assert _block_formats(window)[0] == []
            window.set_markdown_highlight(True)
            assert window._highlighter.markdown_enabled()
        finally:
            discard_close(window)
        plain = make_window(txt_path)
        try:
            assert not plain._highlighter.markdown_enabled()
            assert _block_formats(plain)[0] == []
        finally:
            discard_close(plain)


def test_markdown_code_bold_link_quote_bullet():
    with tempfile.TemporaryDirectory() as tmp:
        md_path = os.path.join(tmp, "doc.markdown")
        Path(md_path).write_text(
            "太字は **強調** です\n"
            "```\n"
            "code **not bold**\n"
            "```\n"
            "[リンク](https://example.com) と `code`\n"
            "> 引用の行\n"
            "- 箇条書き\n",
            encoding="utf-8")
        window = make_window(md_path)
        try:
            formats = _block_formats(window)
            # 太字の範囲に太字の書式が付く。
            bold = [fr for fr in formats[0]
                    if fr.format.fontWeight() >= 700]
            assert bold, formats[0]
            # コードブロックの内側は範囲の背景(太字にはならない)。
            assert formats[2], "コードブロックに書式が付いていません"
            assert (formats[2][0].format.background().color()
                    == CODE_BLOCK_BACKGROUND)
            assert not [fr for fr in formats[2]
                        if fr.format.fontWeight() >= 700]
            # 区切りの行(```)もコード側の背景。
            assert (formats[1][0].format.background().color()
                    == CODE_BLOCK_BACKGROUND)
            assert (formats[3][0].format.background().color()
                    == CODE_BLOCK_BACKGROUND)
            # リンクには色と下線、行内コードには背景。
            link = [fr for fr in formats[4] if fr.format.fontUnderline()]
            assert link and link[0].format.foreground().color().isValid()
            code = [fr for fr in formats[4]
                    if fr.format.background().color()
                    == INLINE_CODE_BACKGROUND]
            assert code, formats[4]
            # 引用と箇条書きの記号にも書式が付く。
            assert formats[5], "引用に書式が付いていません"
            assert formats[6], "箇条書きに書式が付いていません"
        finally:
            discard_close(window)


def test_open_path_at_line():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "a.txt")
        p2 = os.path.join(tmp, "b.txt")
        Path(p1).write_text("a1\na2\na3\n", encoding="utf-8")
        Path(p2).write_text("b1\nb2\nb3\nb4\n", encoding="utf-8")
        window = make_window(p1)
        try:
            assert window.open_path_at_line(p2, 3)
            assert window.current_document().path == p2
            assert window._edit.textCursor().blockNumber() == 2
            # 同じファイルへは読み直さない(未保存の編集が残る)。
            type_text(window, "X")
            assert window.open_path_at_line(p2, 1)
            assert "X" in window.text()
            assert window._edit.textCursor().blockNumber() == 0
        finally:
            discard_close(window)


def test_grep_dialog_lists_and_jumps():
    from editor.grep_dialog import GrepDialog
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "a.txt")
        Path(target).write_text("1行目\n探す語\n", encoding="utf-8")
        jumped = []
        dialog = GrepDialog(
            None, folder=tmp, word="探す語",
            on_jump=lambda p, line: jumped.append((p, line)))
        dialog.start_search()
        deadline = time.time() + 30
        while dialog._thread is not None and time.time() < deadline:
            app().processEvents()
            time.sleep(0.02)
        app().processEvents()
        assert dialog._results.count() == 1
        item = dialog._results.item(0)
        assert "a.txt" in item.text() and "2" in item.text()
        dialog._on_item_activated(item)
        assert jumped == [(target, 2)]
        dialog.deleteLater()


def _all_blocks(window):
    blocks = []
    block = window._edit.document().begin()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    return blocks


def test_save_as_f12_saves_with_current_format():
    # F12 = 名前を付けて保存。保存先が現在の文書になり、文字コードと
    # 改行は今の文書のものを引き継ぐ(形式ダイアログの既定のまま)。
    import editor.window as window_module
    from PySide6.QtGui import QKeySequence

    data = "あ\r\nい\r\n".encode("cp932")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.txt")
        dst = os.path.join(tmp, "dst.txt")
        Path(src).write_bytes(data)
        window = make_window(src)
        try:
            assert window._act_save_as.shortcut() == QKeySequence("F12")

            class FakeFileDialog:
                @staticmethod
                def getSaveFileName(*_args, **_kwargs):
                    return dst, ""

            class FakeOptionsDialog:
                """形式ダイアログの既定(今の文書の値)をそのまま返す差し替え。"""

                def __init__(self, doc, parent=None):
                    self._doc = doc

                def exec(self):
                    return QDialog.DialogCode.Accepted

                def values(self):
                    doc = self._doc
                    return doc.encoding, doc.newline, doc.bom

            orig_file = window_module.QFileDialog
            orig_options = window_module.SaveOptionsDialog
            window_module.QFileDialog = FakeFileDialog
            window_module.SaveOptionsDialog = FakeOptionsDialog
            try:
                assert window.save_as()
            finally:
                window_module.QFileDialog = orig_file
                window_module.SaveOptionsDialog = orig_options
            assert Path(dst).read_bytes() == data
            assert window._doc.path == dst
            assert "dst.txt" in window.windowTitle()
        finally:
            discard_close(window)


def test_newline_glyphs_split_by_kind_and_eof():
    # CRLF・LF・CR が混在した文書では、行ごとに改行の記号が違い、
    # 最後の行には EOF の印が付く。保存すると記号もそろう。
    from editor.text_edit import EOF_MARK, NEWLINE_GLYPHS

    # 記号は種別どうしで見分けられ、MS Gothic にある字の範囲。
    assert NEWLINE_GLYPHS["\n"] == "↓"
    assert NEWLINE_GLYPHS["\r"] == "←"
    assert NEWLINE_GLYPHS["\r\n"] == "←↓"
    assert len(set(NEWLINE_GLYPHS.values())) == 3
    data = "a\r\nb\nc\rd".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mixed.txt")
        Path(path).write_bytes(data)
        window = make_window(path)
        try:
            window.set_invisibles_visible(True)
            edit = window._edit
            blocks = _all_blocks(window)
            assert len(blocks) == 4
            assert [edit.block_end_mark(b) for b in blocks] == [
                "←↓", "↓", "←", EOF_MARK]
            # 不可視文字を描いても落ちない。
            edit.viewport().grab()
            # 保存すると改行が CRLF にそろい、記号もそろう。
            assert window.save()
            assert [edit.block_end_mark(b) for b in _all_blocks(window)] == [
                "←↓", "←↓", "←↓", EOF_MARK]
            assert Path(path).read_bytes() == "a\r\nb\r\nc\r\nd".encode()
        finally:
            discard_close(window)


def test_eof_mark_on_trailing_empty_line():
    # 末尾が改行で終わる文書では、最後の空行の位置に EOF の印が付く。
    from editor.text_edit import EOF_MARK

    data = "a\nb\n".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "lf.txt")
        Path(path).write_bytes(data)
        window = make_window(path)
        try:
            edit = window._edit
            blocks = _all_blocks(window)
            assert len(blocks) == 3
            assert [edit.block_end_mark(b) for b in blocks] == [
                "↓", "↓", EOF_MARK]
        finally:
            discard_close(window)


def test_newline_glyph_fallback_for_edited_lines():
    # 読み込んだ後に増えた行は、控えが無いので文書の改行設定(CRLF)
    # の記号で描く。
    from editor.text_edit import EOF_MARK

    data = "a\r\n".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "crlf.txt")
        Path(path).write_bytes(data)
        window = make_window(path)
        try:
            edit = window._edit
            cursor = edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            edit.setTextCursor(cursor)
            type_text(window, "x\ny")
            blocks = _all_blocks(window)
            assert len(blocks) == 3
            assert [edit.block_end_mark(b) for b in blocks] == [
                "←↓", "←↓", EOF_MARK]
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
