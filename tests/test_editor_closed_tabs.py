# tests/test_editor_closed_tabs.py
# 閉じたタブを開き直す(Ctrl+Shift+T)を確かめる。
#
#   - タブを閉じると、内容とカーソル位置が「閉じたタブ」の控えに
#     残り、Ctrl+Shift+T で直近に閉じたものから順に戻る
#   - 直近10件まで残り、古いものから捨てる
#   - 閉じたタブの控えは EDDy を再起動しても残る
#   - 同じファイルが既に開いている場合は、そのタブを前に出す
#   - ファイルが外で消えていたら、控えの内容を無題の文書として
#     開き直す(控えが無ければ、その記録は捨てる)
#
# セッションの置き場はテストごとに一時フォルダへ向ける
# (session_path 引数)。本物の %LOCALAPPDATA% は汚さない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_closed_tabs.py
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

from PySide6.QtGui import QAction, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
from editor import keymap as keymapmod  # noqa: E402
from editor import session as sessionmod  # noqa: E402
from editor.keymap_dialog import KeymapSettingsDialog  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(session_dir, path=None):
    app()
    window = editor.create_window(
        path, session_path=Path(session_dir) / "editor_session.json")
    window.show()
    return window


def close_window(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def type_text(window, text):
    """実際の編集と同じ経路(アンドゥに乗り、変更ありになる)で入れる。"""
    window._edit.textCursor().insertText(text)


def set_cursor(window, position, anchor=None):
    cursor = window._edit.textCursor()
    cursor.setPosition(position if anchor is None else anchor)
    if anchor is not None:
        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
    window._edit.setTextCursor(cursor)


def test_reopen_closed_tab_restores_text_and_cursor():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("元の文です\n".encode("utf-8"))
        window = make_window(tmp, path)
        try:
            type_text(window, "追記")
            set_cursor(window, 3)
            window.close_tab()
            assert len(window._closed_tabs) == 1
            window.reopen_closed_tab()
            assert window.tab_count() == 2
            assert window.current_document().path == path
            assert window.text() == "追記元の文です\n"
            assert window._edit.textCursor().position() == 3
            assert window.is_dirty()
            assert window._closed_tabs == []
        finally:
            close_window(window)


def test_reopen_closed_tab_repeats_to_older():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.txt")
        b = os.path.join(tmp, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(tmp, a)
        try:
            assert window.open_path(b)
            assert window.tab_count() == 2
            window.close_tab()  # 今のタブ B を閉じる
            window.close_tab()  # 今のタブ A を閉じる
            assert len(window._closed_tabs) == 2
            # 直近に閉じた A から戻る。
            window.reopen_closed_tab()
            assert window.current_document().path == a
            assert window.open_paths() == [a]
            # 繰り返すと、古い方の B が戻る。
            window.reopen_closed_tab()
            assert window.current_document().path == b
            assert window.open_paths() == [a, b]
            assert window._closed_tabs == []
            # もう無いときは、タブは増えず知らせるだけ。
            before = window.tab_count()
            window.reopen_closed_tab()
            assert window.tab_count() == before
            assert window.statusBar().currentMessage() == \
                "開き直すタブがありません"
        finally:
            close_window(window)


def test_reopen_closed_tab_limit_ten():
    with tempfile.TemporaryDirectory() as tmp:
        window = make_window(tmp)
        try:
            type_text(window, "内容0")
            for i in range(1, 11):
                window.new_document()
                type_text(window, f"内容{i}")
            assert window.tab_count() == 11
            for _ in range(11):
                window.close_tab(0)
            # 直近10件まで残り、最初に閉じた「内容0」は捨てられる。
            assert len(window._closed_tabs) == 10
            assert window._closed_tabs[-1]["text"] == "内容1"
            stored = sessionmod.load_closed_tabs_file(
                Path(tmp) / "editor_closed_tabs.json")
            assert len(stored) == 10
            # 直近に閉じたものから順に10件戻る。
            for expected in range(10, 0, -1):
                window.reopen_closed_tab()
                assert window.text() == f"内容{expected}"
            assert window._closed_tabs == []
            before = window.tab_count()
            window.reopen_closed_tab()
            assert window.tab_count() == before
        finally:
            close_window(window)


def test_reopen_closed_tab_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        original = "元の文です\n".encode("utf-8")
        Path(path).write_bytes(original)
        window = make_window(tmp, path)
        type_text(window, "追記")
        set_cursor(window, 3)
        window.close_tab()
        assert (Path(tmp) / "editor_closed_tabs.json").exists()
        close_window(window)
        # ファイル自体は書き換わっていない(控えだけが取れた)。
        assert Path(path).read_bytes() == original
        window2 = make_window(tmp)
        try:
            window2.reopen_closed_tab()
            assert window2.current_document().path == path
            assert window2.text() == "追記元の文です\n"
            assert window2._edit.textCursor().position() == 3
            assert window2.is_dirty()
        finally:
            close_window(window2)


def test_reopen_closed_tab_activates_existing():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("あ\n".encode("utf-8"))
        window = make_window(tmp, path)
        try:
            window.close_tab()
            assert window.open_path(path)
            assert window.tab_count() == 1
            window.reopen_closed_tab()
            # 重ねて開かず、そのタブが前に出るだけ。
            assert window.tab_count() == 1
            assert window.current_document().path == path
            assert window._closed_tabs == []
        finally:
            close_window(window)


def test_reopen_closed_tab_missing_file_becomes_untitled():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("元の文です\n".encode("utf-8"))
        window = make_window(tmp, path)
        window.ask_external_reload = lambda: False
        try:
            type_text(window, "追記")
            os.unlink(path)
            app().processEvents()
            window.close_tab()
            window.reopen_closed_tab()
            # 控えの内容が、ファイルに紐付かない無題の文書で戻る。
            assert window.current_document().path is None
            assert window.text() == "追記元の文です\n"
            assert window.is_dirty()
        finally:
            close_window(window)


def test_reopen_closed_tab_missing_clean_file_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("あ\n".encode("utf-8"))
        window = make_window(tmp, path)
        window.ask_external_reload = lambda: False
        try:
            os.unlink(path)
            app().processEvents()
            window.close_tab()
            window.reopen_closed_tab()
            # 控えが無いので、戻せるものは無い。記録は捨てる。
            assert window.tab_count() == 1
            assert window.open_paths() == []
            assert window._closed_tabs == []
            assert "見つからない" in window.statusBar().currentMessage()
        finally:
            close_window(window)


def test_reopen_closed_tab_keeps_dirty_draft_when_file_reopened():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("元の文です\n".encode("utf-8"))
        window = make_window(tmp, path)
        try:
            type_text(window, "追記")
            window.close_tab()
            assert window.open_path(path)
            assert window.text() == "元の文です\n"
            window.reopen_closed_tab()
            # 同じファイルは重ねて開かない。控えだけが持つ未保存の
            # 編集は、無題の文書として別タブに開き直す。
            assert window.tab_count() == 2
            assert window.current_document().path is None
            assert window.text() == "追記元の文です\n"
            assert window.contains_path(path)
        finally:
            close_window(window)


def test_closed_tabs_file_roundtrip_and_limit():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "editor_closed_tabs.json"
        entries = [{"path": f"f{i}.txt", "dirty": False, "cursor": 0,
                    "anchor": 0, "index": i} for i in range(12)]
        sessionmod.save_closed_tabs_file(path, entries)
        loaded = sessionmod.load_closed_tabs_file(path)
        assert len(loaded) == 10
        assert [entry["path"] for entry in loaded] == \
            [f"f{i}.txt" for i in range(10)]
        # 壊れた値は捨てる(控えが要るのに本文が無いものも含む)。
        bad = {"version": 1, "tabs": [
            {"path": "ok.txt", "dirty": False, "cursor": 0, "anchor": 0},
            "not-a-dict",
            {"path": 42, "dirty": False},
            {"path": "dirty-no-text.txt", "dirty": True, "cursor": 0},
        ]}
        assert sessionmod.sanitize_closed_tabs(bad) == [
            {"path": "ok.txt", "dirty": False, "cursor": 0, "anchor": 0,
             "index": 0}]
        assert sessionmod.load_closed_tabs_file(Path(tmp) / "nope.json") == []
        # 共有の並びとファイルへの書き出し。
        shared = sessionmod.closed_tabs(path)
        shared.clear()
        shared.append({"path": "x.txt", "dirty": False, "cursor": 1,
                       "anchor": 1, "index": 0})
        sessionmod.save_closed_tabs(path)
        assert sessionmod.load_closed_tabs_file(path)[0]["path"] == "x.txt"


def test_reopen_closed_tab_via_keypress():
    from PySide6.QtCore import Qt  # noqa: E402  このテストだけ使う
    from PySide6.QtTest import QTest  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("あ\n".encode("utf-8"))
        window = make_window(tmp, path)
        try:
            window.close_tab()
            assert window.tab_count() == 1
            window._edit.setFocus()
            app().processEvents()
            QTest.keyClick(window._edit, Qt.Key.Key_T,
                           Qt.KeyboardModifier.ControlModifier
                           | Qt.KeyboardModifier.ShiftModifier)
            app().processEvents()
            assert window.current_document().path == path
        finally:
            close_window(window)


def test_keymap_reopen_closed_tab_without_conflict():
    assert keymapmod.DEFAULT_KEYMAP["reopen_closed_tab"] == "Ctrl+Shift+T"
    names = [name for name, _label in keymapmod.COMMAND_LABELS]
    assert "reopen_closed_tab" in names
    with tempfile.TemporaryDirectory() as tmp:
        window = make_window(tmp)
        try:
            assert window.keymap()["reopen_closed_tab"] == "Ctrl+Shift+T"
            conflicts = window.keymap_conflicts()
            assert not any(
                "Ctrl+Shift+T" in keys for keys in conflicts), conflicts
            # メニューとキー割り当ての画面にも載る。
            actions = window.findChildren(QAction)
            assert any(
                action.objectName() == "reopen_closed_tab"
                for action in actions)
            dialog = KeymapSettingsDialog(keymap=window.keymap())
            try:
                assert dialog is not None
            finally:
                dialog.close()
        finally:
            close_window(window)


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
