# tests/test_editor_split.py
# EDDy の分割表示(同じ文書を2ペインで同時に眺めて編集する)を
# offscreen で確かめる。
#
#   - 分割はタブごと。左右・上下の両方と、向きの変更、分割をやめる
#   - 両ペインは同じ QTextDocument を共有し、片方の編集がもう片方へ
#     即座に反映される。編集マーク・ブックマークも両ペインでそろう
#   - カーソルとスクロールはペインごとに独立
#   - フォーカスのあるペインが現在のペインで、命令の対象になる。
#     ペイン間の移動は focus_next_pane(キー割り当ての対象にもなる)
#   - 分割したまま保存・タブを閉じる・窓を閉じる・再読み込みが壊れない
#   - 分割の状態はセッションの控えに残り、次に開くと戻る
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_split.py
import json
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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

import editor  # noqa: E402
from editor import session as sessionmod  # noqa: E402
from editor.keymap import COMMAND_LABELS, DEFAULT_KEYMAP  # noqa: E402

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
    app().processEvents()
    return window


def close_window(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def write_file(tmp, name, text):
    path = os.path.join(tmp, name)
    Path(path).write_bytes(text.encode("utf-8"))
    return path


def set_cursor(edit, position, anchor=None):
    cursor = edit.textCursor()
    cursor.setPosition(position if anchor is None else anchor)
    if anchor is not None:
        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
    edit.setTextCursor(cursor)


LONG_TEXT = "".join(f"行{i}\n" for i in range(60))


def test_split_start_change_orientation_and_unsplit():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        other = write_file(tmp, "b.txt", "別の文書\n")
        window = make_window(path)
        try:
            view = window._current_view()
            assert view.split_edit is None
            assert not window.is_split()
            window.split_current_view("horizontal")
            app().processEvents()
            assert view.split_edit is not None
            assert window.is_split()
            assert view.split_name() == "horizontal"
            assert view.splitter.orientation() == Qt.Orientation.Horizontal
            assert len(view.panes()) == 2
            # 向きだけの変更では、2枚目は作り直されない。
            second = view.split_edit
            window.split_current_view("vertical")
            assert view.split_edit is second
            assert view.split_name() == "vertical"
            assert view.splitter.orientation() == Qt.Orientation.Vertical
            # 別のタブは分割されない。タブを行き来しても分割は残る。
            assert window.open_path(other)
            assert window._current_view().split_edit is None
            window.activate_prev_tab()
            assert window._current_view() is view
            assert view.split_edit is second
            # 分割をやめる。
            window.unsplit_current_view()
            app().processEvents()
            assert view.split_edit is None
            assert not window.is_split()
            assert view.active_edit() is view.edit
            # やめたあとも文書は無事。
            assert window.text() == LONG_TEXT
        finally:
            close_window(window)


def test_shared_document_and_edits_reflect_both_ways():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", "あいう\nえお\n")
        window = make_window(path)
        try:
            view = window._current_view()
            window.split_current_view("horizontal")
            first, second = view.edit, view.split_edit
            assert first.document() is second.document()
            # 2枚目で編集 → 1枚目と窓の本文に反映される。
            set_cursor(second, 0)
            second.textCursor().insertText("X")
            app().processEvents()
            assert first.toPlainText().startswith("Xあいう")
            assert window.text().startswith("Xあいう")
            # 1枚目で編集 → 2枚目に反映される。
            set_cursor(first, 3)
            first.textCursor().insertText("Y")
            app().processEvents()
            assert second.toPlainText() == window.text()
            assert window.is_dirty()
        finally:
            close_window(window)


def test_marks_and_bookmarks_are_shared():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        window = make_window(path)
        try:
            view = window._current_view()
            window.split_current_view("vertical")
            first, second = view.edit, view.split_edit
            # 2枚目での編集が、両ペインの編集マークに出る。
            set_cursor(second, 0)
            second.textCursor().insertText("X")
            app().processEvents()
            assert first.edited_lines() == second.edited_lines()
            assert 0 in first.edited_lines()
            # ブックマークは1枚目で付けても2枚目で見える(逆も同じ)。
            first.toggle_bookmark(5)
            assert second.bookmarks() == [5]
            assert window.bookmarks() == [6]
            second.toggle_bookmark(10)
            assert first.bookmarks() == [5, 10]
            # 保存すると、編集マークは両ペインとも消える。
            assert window.save()
            assert first.edited_lines() == []
            assert second.edited_lines() == []
            # ブックマークは保存では消えない(両ペインとも)。
            assert first.bookmarks() == [5, 10]
            assert second.bookmarks() == [5, 10]
        finally:
            close_window(window)


def test_cursor_and_scroll_are_independent():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        window = make_window(path)
        try:
            view = window._current_view()
            window.split_current_view("horizontal")
            app().processEvents()
            first, second = view.edit, view.split_edit
            set_cursor(first, 4)
            set_cursor(second, 30)
            assert first.textCursor().position() == 4
            assert second.textCursor().position() == 30
            first.verticalScrollBar().setValue(0)
            second.verticalScrollBar().setValue(
                second.verticalScrollBar().maximum())
            app().processEvents()
            assert first.verticalScrollBar().value() == 0
            assert second.verticalScrollBar().value() > 0
        finally:
            close_window(window)


def test_focus_next_pane_and_commands_target_active_pane():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        window = make_window(path)
        try:
            view = window._current_view()
            # 分割していないときは何もしない。
            window.focus_next_pane()
            assert view.active_edit() is view.edit
            window.split_current_view("horizontal")
            app().processEvents()
            first, second = view.edit, view.split_edit
            # 分割した直後は2枚目が現在のペイン。
            assert view.active_edit() is second
            assert window._edit is second
            # ペイン間を往復する。
            window.focus_next_pane()
            app().processEvents()
            assert view.active_edit() is first
            assert window._edit is first
            window.focus_next_pane()
            app().processEvents()
            assert view.active_edit() is second
            # 命令は現在のペイン(2枚目)だけに効く。
            set_cursor(first, 0)
            set_cursor(second, 0)
            assert window.execute_command("move_line_down")
            assert second.textCursor().blockNumber() == 1
            assert first.textCursor().blockNumber() == 0
            # 2枚目にフォーカスがあるまま分割をやめると、現在の
            # ペインは1枚目に戻る。
            window.unsplit_current_view()
            app().processEvents()
            assert view.active_edit() is view.edit
            assert window._edit is view.edit
        finally:
            close_window(window)


def test_save_and_reload_while_split():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", "元の文\n")
        window = make_window(path)
        try:
            view = window._current_view()
            window.split_current_view("horizontal")
            # 2枚目から編集して保存する。
            set_cursor(view.split_edit, 0)
            view.split_edit.textCursor().insertText("追記")
            app().processEvents()
            assert window.save()
            assert Path(path).read_bytes() == \
                "追記元の文\n".encode("utf-8")
            assert view.split_edit is not None  # 分割は残る
            # 外部変更の再読み込みでも壊れない。
            Path(path).write_bytes("外の文\n".encode("utf-8"))
            assert window.reload_from_disk()
            assert view.edit.toPlainText() == "外の文\n"
            assert view.split_edit.toPlainText() == "外の文\n"
            assert view.split_edit is not None
        finally:
            close_window(window)


def test_close_tab_and_window_while_split():
    with tempfile.TemporaryDirectory() as tmp:
        a = write_file(tmp, "a.txt", "あ\n")
        b = write_file(tmp, "b.txt", "い\n")
        window = make_window(a)
        try:
            assert window.open_path(b)
            window.split_current_view("vertical")
            assert window.is_split()
            assert window.tab_count() == 2
            # 分割したタブを閉じても、残りのタブは無事。
            window.close_tab()
            app().processEvents()
            assert window.tab_count() == 1
            assert window.current_document().path == a
            assert window.text() == "あ\n"
            # 分割したまま窓を閉じても壊れない。
            window.split_current_view("horizontal")
            assert window.is_split()
        finally:
            close_window(window)


def test_split_pane_follows_display_settings():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        window = make_window(path)
        try:
            view = window._current_view()
            window.split_current_view("horizontal")
            first, second = view.edit, view.split_edit
            window.set_invisibles_visible(True)
            assert first.show_invisibles() and second.show_invisibles()
            window.set_line_numbers_visible(False)
            assert not first.line_numbers_visible()
            assert not second.line_numbers_visible()
            window.set_wrap_mode("none")
            for edit in (first, second):
                assert edit.lineWrapMode() == \
                    QPlainTextEdit.LineWrapMode.NoWrap
            window.apply_color_scheme("秀丸風")
            assert first.color_scheme() == second.color_scheme()
            # 分割してから変えた設定が、両ペインに行き渡ることの確認。
            assert second.font().family() == first.font().family()
            assert second.font_size() == first.font_size()
        finally:
            close_window(window)


def test_keymap_includes_split_commands_without_default_keys():
    names = [name for name, _label in COMMAND_LABELS]
    assert names == list(DEFAULT_KEYMAP)  # 並びがそろっている前提の確認
    for name in ("split_horizontal", "split_vertical",
                 "close_split", "focus_next_pane"):
        assert DEFAULT_KEYMAP[name] == ""  # 既定ではキーなし
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        window = make_window(path)
        try:
            assert window.keymap()["focus_next_pane"] == ""
            # 割り当て画面と同じ経路で F6 を付けると、その場で効く。
            window.apply_keymap_settings({"focus_next_pane": "F6"})
            assert window.keymap()["focus_next_pane"] == "F6"
            action = window._keymap_actions["focus_next_pane"][0]
            assert action.shortcut().toString() == "F6"
            view = window._current_view()
            window.split_current_view("horizontal")
            app().processEvents()
            assert view.active_edit() is view.split_edit
            action.trigger()
            app().processEvents()
            assert view.active_edit() is view.edit
        finally:
            close_window(window)


def test_session_restores_split():
    with tempfile.TemporaryDirectory() as tmp:
        path = write_file(tmp, "a.txt", LONG_TEXT)
        session_path = Path(tmp) / "editor_session.json"
        window = editor.create_window(
            path, session_path=session_path)
        window.show()
        app().processEvents()
        try:
            view = window._current_view()
            window.split_current_view("vertical")
            set_cursor(view.edit, 3)
            set_cursor(view.split_edit, 25)
            # 控え(ドラフト)が要る状態にもしておく。
            view.split_edit.textCursor().insertText("控")
            app().processEvents()
        finally:
            close_window(window)
        # 記録の形: 分割の向き・2枚目のカーソル・幅が残っている。
        data = json.loads(session_path.read_text(encoding="utf-8"))
        entry = data["windows"][0]["tabs"][0]
        assert entry["split"] == "vertical"
        assert entry["split_cursor"] >= 25
        assert len(entry["split_sizes"]) == 2
        assert all(size > 0 for size in entry["split_sizes"])
        window2 = editor.create_window(session_path=session_path)
        window2.show()
        app().processEvents()
        try:
            view2 = window2._current_view()
            assert view2.split_edit is not None
            assert view2.split_name() == "vertical"
            assert view2.edit.textCursor().position() == 3
            assert view2.split_edit.textCursor().position() == \
                entry["split_cursor"]
            assert "控" in window2.text()  # ドラフトの編集も戻る
            assert view2.edit.document() is view2.split_edit.document()
        finally:
            close_window(window2)


def test_session_sanitize_split_fields():
    def sanitize(tab):
        result = sessionmod.sanitize_session(
            {"windows": [{"tabs": [tab], "active": 0}]})
        return result[0]["tabs"][0]

    base = {"path": None, "dirty": False, "cursor": 0, "anchor": 0}
    good = sanitize({**base, "split": "horizontal",
                     "split_cursor": 7, "split_anchor": 5,
                     "split_sizes": [300, 200]})
    assert good["split"] == "horizontal"
    assert good["split_cursor"] == 7
    assert good["split_anchor"] == 5
    assert good["split_sizes"] == [300, 200]
    # おかしな向きは記録ごと落とす(分割だけ捨て、タブは残す)。
    bad = sanitize({**base, "split": "diagonal", "split_cursor": 7})
    assert "split" not in bad
    # 幅が壊れていれば幅だけ捨てる。
    bad_sizes = sanitize({**base, "split": "vertical",
                          "split_sizes": [300, -1]})
    assert bad_sizes["split"] == "vertical"
    assert "split_sizes" not in bad_sizes
    # カーソルが壊れていれば 0 に倒す。
    bad_cursor = sanitize({**base, "split": "vertical",
                           "split_cursor": -3})
    assert bad_cursor["split_cursor"] == 0


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
