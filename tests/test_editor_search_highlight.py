# tests/test_editor_search_highlight.py
# 検索に一致した箇所すべての色付け(件5)を確かめる。
#
#   - 通常検索・リアルタイム検索・正規表現検索で、現在の検索語に
#     一致する箇所すべてに色が付く(現在のヒットは選択の色で
#     見分けるため、色付けからは除く)
#   - 色は配色の search_match(色の設定画面で変えられる)
#   - 検索バーを閉じたら消える
#   - 編集したら付け直される(古い位置に残らない)
#   - 分割ウィンドウでは両ペインに付く
#   - 件数の上限(SEARCH_HIGHLIGHT_LIMIT)を超えたら先頭から
#     上限まで
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_search_highlight.py
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

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
from editor.window import SEARCH_HIGHLIGHT_LIMIT  # noqa: E402

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
    window.close()
    window.deleteLater()
    app().processEvents()


def wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app().processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_plain_search_highlights_all_but_current():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("foo あ foo\nう foo え\n")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        # 現在のヒット (0, 3) は選択中で、色付けからは除かれる。
        cursor = edit.textCursor()
        assert (cursor.selectionStart(), cursor.selectionEnd()) == (0, 3)
        assert edit.match_highlights() == [(6, 9), (12, 15)]
        # 次のヒットへ移ると、色付けの顔ぶれも入れ替わる。
        assert window._do_find(True)
        assert edit.match_highlights() == [(0, 3), (12, 15)]
    finally:
        discard_close(window)


def test_match_color_comes_from_scheme():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("foo と foo\n")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        expected = QColor(edit.color_scheme()["search_match"])
        colored = [selection for selection in edit.extraSelections()
                   if selection.format.background() == expected]
        assert len(colored) == len(edit.match_highlights()) == 1
        span = (colored[0].cursor.selectionStart(),
                colored[0].cursor.selectionEnd())
        assert span == (6, 9)
    finally:
        discard_close(window)


def test_incremental_search_highlights():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("bar bar bar\n")
        window.show_find_bar()
        window._find_edit.setText("bar")
        window._on_find_edited("bar")   # 打ちながらの検索と同じ経路
        app().processEvents()
        assert edit.match_highlights() == [(4, 7), (8, 11)]
    finally:
        discard_close(window)


def test_highlights_follow_edits():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("foo だけ\n")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        assert edit.match_highlights() == []
        # 一致を増やす編集をしたら、間引きの後に付け直される。
        # 付け直しの時点では選択が無いので、最初の foo も対象に
        # 戻る。新しい foo は (10, 13)。
        cursor = edit.textCursor()
        cursor.setPosition(len("foo だけ\n"))
        edit.setTextCursor(cursor)
        edit.textCursor().insertText("もう foo\n")
        assert wait_until(
            lambda: edit.match_highlights() == [(0, 3), (10, 13)])
    finally:
        discard_close(window)


def test_closing_find_bar_clears_highlights():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("foo foo\n")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        assert edit.match_highlights() == [(4, 7)]
        window._hide_find_bar()
        assert edit.match_highlights() == []
    finally:
        discard_close(window)


def test_regex_search_highlights():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("a1 b22 c333 d4\n")
        window.show_find_bar()
        window._find_regex.setChecked(True)
        window._find_edit.setText("[0-9]+")
        assert window._do_find(True)
        # 別プロセスの結果が返ってから付く。
        assert wait_until(
            lambda: edit.match_highlights() == [(4, 6), (8, 11), (13, 14)])
    finally:
        discard_close(window)


def test_split_panes_both_highlighted():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("foo foo foo\n")
        window.show_find_bar()
        window._find_edit.setText("foo")
        assert window._do_find(True)
        window.split_current_view("horizontal")
        app().processEvents()
        view = window._current_view()
        assert len(view.panes()) == 2
        for pane in view.panes():
            assert pane.match_highlights() == [(4, 7), (8, 11)]
    finally:
        discard_close(window)


def test_highlights_move_with_tab():
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "a.txt"
        first.write_text("foo foo\n", encoding="utf-8")
        window = make_window(str(first))
        try:
            window.show_find_bar()
            window._find_edit.setText("foo")
            assert window._do_find(True)
            first_view = window._current_view()
            assert first_view.edit.match_highlights() == [(4, 7)]
            window.new_document()
            app().processEvents()
            second_view = window._current_view()
            assert second_view is not first_view
            second_view.edit.setPlainText("foo foo foo\n")
            app().processEvents()
            # 付け直し(編集の間引きか、明示の付け直し)で今のタブへ。
            window._refresh_match_highlights()
            assert second_view.edit.match_highlights() != []
            assert first_view.edit.match_highlights() == []
        finally:
            discard_close(window)


def test_highlight_limit_caps_from_head():
    window = make_window()
    try:
        edit = window._edit
        edit.setPlainText("x " * (SEARCH_HIGHLIGHT_LIMIT + 500))
        window.show_find_bar()
        window._find_edit.setText("x")
        assert window._do_find(True)
        spans = edit.match_highlights()
        # 上限まで数え、現在のヒット(先頭)を除いたぶんが付く。
        # 先頭から上限までの範囲だけが対象(残りには付かない)。
        assert len(spans) == SEARCH_HIGHLIGHT_LIMIT - 1
        assert spans[0] == (2, 3)
        assert spans[-1] == (2 * (SEARCH_HIGHLIGHT_LIMIT - 1),
                             2 * (SEARCH_HIGHLIGHT_LIMIT - 1) + 1)
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
