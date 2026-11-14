# tests/test_editor_macro_py.py
# マクロの Python 化(editor/macro_py.py と別窓・窓側の橋)を
# offscreen で確かめる。記録 → Python 変換 → 再生で結果が同じに
# なること、記録可能性の総点検(キー操作が一通り記録できる)、
# ステップ実行、実行環境の約束(子プロセスで実行し、本体を
# 巻き込まない・開いても実行しない・停止できる・閉じても子が
# 残らない・親側でも名前の制限と呼び出し回数の上限を持つ)、
# 登録マクロの Python 形式。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_macro_py.py
import os
import sys
import tempfile
import threading
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

from PySide6.QtCore import QEvent, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

import editor  # noqa: E402
from editor import macro_py  # noqa: E402
from editor import macros as macmod  # noqa: E402
from editor.macro_dialog import MacroLibraryDialog  # noqa: E402
from editor.macro_py_dialog import (  # noqa: E402
    MacroPythonDialog, PANE_WIDTH, _NEXT_LINE_COLOR)

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(text="", cursor_pos=0):
    app()
    window = editor.create_window()
    window.show()
    window._edit.setPlainText(text)
    cursor = window._edit.textCursor()
    cursor.setPosition(cursor_pos)
    window._edit.setTextCursor(cursor)
    window._edit.setFocus()
    return window


def discard_close(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def set_cursor(window, pos):
    cursor = window._edit.textCursor()
    cursor.setPosition(pos)
    window._edit.setTextCursor(cursor)


CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# ---------------------------------------------------------------
# 変換(命令の列 → Python)
# ---------------------------------------------------------------
def test_commands_to_python_shape():
    commands = [
        {"cmd": "insert_text", "args": {"text": "a\nb"}},
        {"cmd": "move_char_left", "args": {"count": 3, "extend": False}},
        {"cmd": "move_char_left", "args": {"count": 1, "extend": False}},
        {"cmd": "find_next", "args": {"text": "x", "case": True,
                                      "regex": False}},
        {"cmd": "goto_line", "args": {"line": 5}},
        {"cmd": "sort_lines", "args": {"order": "desc"}},
        {"cmd": "bogus_cmd", "args": {}},
    ]
    src = macro_py.commands_to_python(commands, "試し")
    assert src.startswith("# EDDy マクロ「試し」\n")
    assert "ed.insert_text('a\\nb')" in src
    assert "ed.move_char_left(count=3)" in src
    # 既定値だけの命令は引数を落として読みやすくする。
    assert "ed.move_char_left()" in src
    assert "ed.find_next(text='x', case=True)" in src
    assert "ed.goto_line(5)" in src
    assert "ed.sort_lines('desc')" in src
    # 知らない命令は黙って落とさず、コメントで残す。
    assert "bogus_cmd" in src and "命令がありません" in src
    # 生成されたものは、そのまま検証を通る。
    assert macro_py.validate_python(src) is None


def test_validate_python_rules():
    assert macro_py.validate_python("ed.insert_text('x')\n") is None
    assert macro_py.validate_python("") is None
    error = macro_py.validate_python("import os\n")
    assert error is not None and "import" in error
    error = macro_py.validate_python("ed._secret()\n")
    assert error is not None and "行目" in error
    error = macro_py.validate_python("().__class__\n")
    assert error is not None
    error = macro_py.validate_python("x = (\n")
    assert error is not None and "1 行目" in error


# ---------------------------------------------------------------
# 実行(ステッパ)
# ---------------------------------------------------------------
class _FakeEd:
    def __init__(self):
        self.calls = []

    def insert_text(self, text):
        self.calls.append(("insert_text", text))
        return True

    def move_line_down(self):
        self.calls.append(("move_line_down",))
        return True

    def move_char_left(self):
        self.calls.append(("move_char_left",))
        return True

    def line_count(self):
        return 3


def test_stepper_steps_and_queries():
    ed = _FakeEd()
    source = (
        "ed.insert_text('a')\n"
        "n = ed.line_count()\n"
        "for i in range(2):\n"
        "    ed.move_line_down()\n"
        "print('done', n)\n"
    )
    stepper = macro_py.PythonStepper(source, ed)
    assert stepper.total == 4
    assert stepper.next_lineno() == 1
    assert stepper.step() == 1
    assert ed.calls == [("insert_text", "a")]
    assert stepper.step() == 2
    assert stepper.step() == 3    # for 文は全体で1ステップ
    assert ed.calls == [("insert_text", "a"),
                        ("move_line_down",), ("move_line_down",)]
    assert not stepper.done
    stepper.run_all()
    assert stepper.done
    assert stepper.output == ["done 3"]
    assert stepper.step() is None


def test_stepper_pumps_during_long_statement():
    # ループを含む文の実行中も、画面の更新 (pump) が進むこと。
    ed = _FakeEd()
    pumps = []
    stepper = macro_py.PythonStepper(
        "for i in range(40):\n    ed.insert_text('x')\n",
        ed, pump=lambda: pumps.append(1))
    assert stepper.step() == 1
    assert len(ed.calls) == 40
    assert len(pumps) >= 2


def test_stepper_errors_are_wrapped_with_line():
    ed = _FakeEd()
    stepper = macro_py.PythonStepper("ed.bogus()\n", ed)
    try:
        stepper.step()
    except macro_py.PythonMacroError as e:
        assert e.lineno == 1
    else:
        raise AssertionError("PythonMacroError にならない")
    assert stepper.done and stepper.error is not None


def test_execution_environment_blocks_builtins_and_imports():
    ed = _FakeEd()
    # open は builtins に無いので、名前として解決できない。
    stepper = macro_py.PythonStepper("open('x')\n", ed)
    try:
        stepper.step()
    except macro_py.PythonMacroError as e:
        assert "open" in str(e)
    else:
        raise AssertionError("open が通ってしまった")
    # import は検証で落とす(セッション自体を作れない)。
    try:
        macro_py.PythonStepper("import os\n", ed)
    except macro_py.PythonMacroError:
        pass
    else:
        raise AssertionError("import が通ってしまった")


def test_runaway_is_stopped_by_limits():
    ed = _FakeEd()
    old_line = macro_py.LINE_EVENT_LIMIT
    old_api = macro_py.API_CALL_LIMIT
    try:
        macro_py.LINE_EVENT_LIMIT = 3000
        stepper = macro_py.PythonStepper(
            "x = 0\nwhile True:\n    x += 1\n", ed)
        try:
            stepper.run_all()
        except macro_py.PythonMacroError as e:
            assert "上限" in str(e)
        else:
            raise AssertionError("無限ループが止まらない")
        macro_py.API_CALL_LIMIT = 50
        stepper = macro_py.PythonStepper(
            "for i in range(1000):\n    ed.move_char_left()\n", ed)
        try:
            stepper.run_all()
        except macro_py.PythonMacroError as e:
            assert "上限" in str(e)
        else:
            raise AssertionError("呼び出し回数の上限で止まらない")
    finally:
        macro_py.LINE_EVENT_LIMIT = old_line
        macro_py.API_CALL_LIMIT = old_api


# ---------------------------------------------------------------
# 記録 → Python → 再生
# ---------------------------------------------------------------
def _drive_live(window):
    """実キーで一通りの操作をする(記録の対象)。"""
    edit = window._edit
    QTest.keyClicks(edit, "X")
    QTest.keyClick(edit, Qt.Key.Key_Right, CTRL)      # 単語移動
    QTest.keyClicks(edit, "Y")
    QTest.keyClick(edit, Qt.Key.Key_Backspace)        # Y を消す
    QTest.keyClick(edit, Qt.Key.Key_End, CTRL)        # 文書の末尾
    QTest.keyClicks(edit, "Z")
    QTest.keyClick(edit, Qt.Key.Key_Home)             # 行頭
    QTest.keyClick(edit, Qt.Key.Key_PageUp)           # 1画面上
    QTest.keyClick(edit, Qt.Key.Key_A, CTRL)          # すべて選択
    QTest.keyClicks(edit, "Q")                        # 選択を上書き


def test_record_python_replay_equivalence():
    initial = "hello world\nsecond line\nthird"
    live = make_window(initial, cursor_pos=0)
    try:
        live.set_macro_recording(True)
        _drive_live(live)
        live.set_macro_recording(False)
        recorded = live.recorded_commands()
        assert live._macro_skips == 0, live._macro_skips
        kinds = [c["cmd"] for c in recorded]
        for expected in ("insert_text", "move_word_right",
                         "move_document_end", "move_page_up",
                         "select_all", "delete_selection"):
            assert expected in kinds, kinds
        source = macro_py.commands_to_python(recorded, "実演")
        assert macro_py.validate_python(source) is None
        want_text = live.text()
        want_pos = live._edit.textCursor().position()
    finally:
        discard_close(live)

    replayed = make_window(initial, cursor_pos=0)
    try:
        assert replayed.play_python_macro(source)
        assert replayed.text() == want_text
        assert replayed._edit.textCursor().position() == want_pos
    finally:
        discard_close(replayed)

    via_commands = make_window(initial, cursor_pos=0)
    try:
        assert via_commands.play_macro(recorded)
        assert via_commands.text() == want_text
        assert via_commands._edit.textCursor().position() == want_pos
    finally:
        discard_close(via_commands)


def test_recordability_audit():
    # キー操作の一通りが、記録できない操作にならずに記録される。
    many_lines = "\n".join(f"line{i}" for i in range(30))
    cases = [
        ("打鍵", "ab", 0, lambda e: QTest.keyClicks(e, "xy")),
        ("Backspace", "ab", 2,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Backspace)),
        ("Delete", "ab", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Delete)),
        ("左", "ab", 2, lambda e: QTest.keyClick(e, Qt.Key.Key_Left)),
        ("右", "ab", 0, lambda e: QTest.keyClick(e, Qt.Key.Key_Right)),
        ("上", "ab\ncd", 4, lambda e: QTest.keyClick(e, Qt.Key.Key_Up)),
        ("下", "ab\ncd", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Down)),
        ("Home", "ab", 2, lambda e: QTest.keyClick(e, Qt.Key.Key_Home)),
        ("End", "ab", 0, lambda e: QTest.keyClick(e, Qt.Key.Key_End)),
        ("Ctrl+左", "one two", 7,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Left, CTRL)),
        ("Ctrl+右", "one two", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Right, CTRL)),
        ("Ctrl+Home", "ab\ncd", 4,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Home, CTRL)),
        ("Ctrl+End", "ab\ncd", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_End, CTRL)),
        ("PageUp", many_lines, 29 * 6,
         lambda e: QTest.keyClick(e, Qt.Key.Key_PageUp)),
        ("PageDown", many_lines, 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_PageDown)),
        ("Ctrl+A", "ab", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_A, CTRL)),
        ("Ctrl+Backspace", "foo bar", 7,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Backspace, CTRL)),
        ("Shift+右(選択拡張)", "ab", 0,
         lambda e: QTest.keyClick(e, Qt.Key.Key_Right, SHIFT)),
    ]
    for label, text, pos, drive in cases:
        window = make_window(text, cursor_pos=pos)
        try:
            window.set_macro_recording(True)
            drive(window._edit)
            window.set_macro_recording(False)
            assert window._macro_skips == 0, (label, window._macro_skips)
            assert window.recorded_commands(), label
        finally:
            discard_close(window)


def test_ctrl_backspace_records_word_delete():
    window = make_window("foo bar", cursor_pos=7)
    try:
        window.set_macro_recording(True)
        QTest.keyClick(window._edit, Qt.Key.Key_Backspace, CTRL)
        window.set_macro_recording(False)
        assert window._macro_skips == 0
        recorded = window.recorded_commands()
        assert len(recorded) == 1
        assert recorded[0]["cmd"] == "delete_backward"
        assert window.text().startswith("foo")
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# 窓側の再生(問い合わせ・分岐・エラー表示)
# ---------------------------------------------------------------
def test_python_macro_queries_branch_and_print():
    window = make_window("one\ntwo", cursor_pos=0)
    try:
        source = (
            "if ed.line_count() > 1:\n"
            "    ed.move_document_end()\n"
            "    ed.insert_text('END')\n"
            "else:\n"
            "    ed.insert_text('ONE')\n"
        )
        assert window.play_python_macro(source)
        assert window.text() == "one\ntwoEND"
        assert window.play_python_macro("print(ed.text())\n")
        message = window.statusBar().currentMessage()
        assert message.startswith("print: "), message
    finally:
        discard_close(window)


def test_python_macro_error_goes_to_status():
    window = make_window("ab", cursor_pos=0)
    try:
        assert not window.play_python_macro("ed.bogus()\n")
        message = window.statusBar().currentMessage()
        assert "エラー" in message and "1 行目" in message, message
        assert not window.play_python_macro("import os\n")
        assert "エラー" in window.statusBar().currentMessage()
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# 別窓(ステップ実行・編集・コピー・保存)
# ---------------------------------------------------------------
def test_python_dialog_live_sync_and_user_edit_guard():
    # ライブ表示: 記録側のソースが増えたら差し替わる。利用者が
    # 編集した後は、ライブ側で上書きしない。
    state = {"source": "ed.insert_text('A')\n"}
    dialog = MacroPythonDialog(
        None, source=state["source"], live_source=lambda: state["source"])
    try:
        state["source"] = "ed.insert_text('A')\ned.insert_text('B')\n"
        dialog._sync_live()
        assert dialog.source() == state["source"]
        dialog._edit.setPlainText("ed.insert_text('自分の編集')\n")
        state["source"] = "ed.insert_text('C')\n"
        dialog._sync_live()
        assert "自分の編集" in dialog.source()
    finally:
        dialog.close()


def test_python_dialog_step_stops_when_tab_switched():
    # ステップ実行の合間にタブを切り替えると、続きは実行されずに
    # 止まる (アオイの実測 req-20261114-22: 2文目以降が切り替え
    # 先のタブで実行された)。
    window = make_window("ab\ncd\nef", cursor_pos=0)
    dialog = MacroPythonDialog(
        window,
        source="ed.insert_text('X')\ned.insert_text('Y')\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    try:
        dialog.show()
        assert dialog.step_once()
        assert window.text() == "Xab\ncd\nef"
        window.new_document()
        assert window.text() == ""
        assert not dialog.step_once()
        assert window.text() == ""  # 切り替え先は変わっていない
    finally:
        dialog.close()
        discard_close(window)


def test_python_dialog_live_sync_pauses_during_session():
    # 実行の途中 (セッションがある間) は、ライブ表示が本文を
    # 書き換えない (アオイの指摘 req-20261114-22 低)。
    window = make_window("ab", cursor_pos=0)
    original = "ed.insert_text('A')\ned.insert_text('B')\n"
    state = {"source": original}
    dialog = MacroPythonDialog(
        window,
        source=state["source"],
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run,
        live_source=lambda: state["source"])
    try:
        dialog.show()
        assert dialog.step_once()
        # セッションは継続中 (2文のうち1文だけ実行した)。
        assert dialog.session() is not None
        assert not dialog.session().done
        state["source"] = original + "ed.insert_text('C')\n"
        dialog._sync_live()
        assert dialog.source() == original
    finally:
        dialog.close()
        discard_close(window)


def test_python_dialog_live_sync_replaces_prepared_session():
    # 用意しただけのセッション (「最初から」で備えたもの) は、
    # ライブ表示の差し替えを妨げない (捨ててから差し替える)。
    window = make_window("ab", cursor_pos=0)
    state = {"source": "ed.insert_text('A')\n"}
    dialog = MacroPythonDialog(
        window,
        source=state["source"],
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run,
        live_source=lambda: state["source"])
    try:
        dialog.show()
        dialog.reset_session()
        assert dialog.session() is not None
        assert dialog.session().position == 0
        state["source"] = "ed.insert_text('A')\ned.insert_text('B')\n"
        dialog._sync_live()
        assert dialog.source() == state["source"]
        assert dialog.session() is None
    finally:
        dialog.close()
        discard_close(window)


def test_python_dialog_step_edit_copy_save():
    window = make_window("ab\ncd\nef", cursor_pos=0)
    saved = []
    dialog = MacroPythonDialog(
        window,
        source=("ed.insert_text('X')\n"
                "ed.move_line_down()\n"
                "ed.insert_text('Y')\n"),
        name="試し",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run,
        on_save=lambda n, s: saved.append((n, s)))
    try:
        dialog.show()
        assert dialog.step_once()
        assert window.text() == "Xab\ncd\nef"
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.step_once()
        assert window._edit.textCursor().blockNumber() == 1
        assert dialog.step_once()
        assert window.text() == "Xab\nYcd\nef"
        # 最後まで終わると、完了したセッションは備え直され、
        # 先頭の文が次になる (印も先頭へ戻る)。
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.session().position == 0
        # ステップをまたいでも、実行全体が undo の1単位になる。
        window._edit.undo()
        assert window.text() == "ab\ncd\nef"
        # 編集するとセッションは捨てられる。
        dialog._edit.setPlainText("ed.delete_line()\n")
        assert dialog.session() is None
        assert dialog.source() == "ed.delete_line()\n"
        # コピーはソースそのものが入る。
        dialog._on_copy()
        assert QGuiApplication.clipboard().text() == "ed.delete_line()\n"
        # 保存は (名前, ソース) で渡る。
        dialog._on_save_clicked()
        assert saved == [("試し", "ed.delete_line()\n")]
    finally:
        dialog.close()
        discard_close(window)


def test_python_dialog_run_all_and_reset():
    window = make_window("ab", cursor_pos=2)
    dialog = MacroPythonDialog(
        window,
        source="ed.insert_text('1')\ned.insert_text('2')\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    try:
        dialog.show()
        assert dialog.step_once()
        assert window.text() == "ab1"
        dialog.reset_session()
        # 「最初から」は、先頭の文が次になるよう備える (セッ
        # ションが用意され、黄色の印と状態表示で次が分かる)。
        assert dialog.session() is not None
        assert dialog.session().position == 0
        assert dialog.session().next_lineno() == 1
        assert "次は 1 行目" in dialog._status_label.text()
        # ハイライトは黄色だけ (水色のカーソル行は出さない)。
        selections = dialog._edit.extraSelections()
        assert len(selections) == 1
        assert selections[0].format.background() == _NEXT_LINE_COLOR
        # 通し実行はカーソルのある行から新しく始める。ステップ
        # 実行でペインのカーソルは次の文へ進んでいるので、先頭
        # へ戻してから押す。
        cursor = dialog._edit.textCursor()
        cursor.setPosition(0)
        dialog._edit.setTextCursor(cursor)
        assert dialog.run_all()
        assert window.text() == "ab112"
        # 最後まで終わると、完了したセッションは備え直される。
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.session().position == 0
    finally:
        dialog.close()
        discard_close(window)


def _make_pane(window, source, name=""):
    """右ペイン (Python マクロ画面) を作る。"""
    return MacroPythonDialog(
        window,
        source=source,
        name=name,
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run,
        flush_run=window._flush_python_run)


def _pane_cursor_to_line(dialog, lineno):
    """ペインのソース欄のカーソルを、行番号 (1始まり) の行頭へ
    移す。"""
    block = dialog._edit.document().findBlockByNumber(lineno - 1)
    cursor = dialog._edit.textCursor()
    cursor.setPosition(block.position())
    dialog._edit.setTextCursor(cursor)


_THREE_STATEMENTS = ("ed.insert_text('1')\n"
                     "ed.insert_text('2')\n"
                     "ed.insert_text('3')\n")


def test_pane_title_shows_name_or_new():
    # 見出しはマクロ名。名前が無ければ「新規マクロ」。
    window = make_window("ab", cursor_pos=0)
    named = _make_pane(window, _THREE_STATEMENTS, name="印")
    unnamed = _make_pane(window, _THREE_STATEMENTS)
    try:
        assert named._title_label.text() == "印"
        assert unnamed._title_label.text() == "新規マクロ"
    finally:
        named.close()
        unnamed.close()
        discard_close(window)


def test_pane_breakpoint_stops_and_continues():
    # 通し実行はブレークポイントのある行の手前で止まり (その
    # 行はまだ実行しない)、もう一度押すと止まっている位置
    # から続ける。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        dialog.toggle_breakpoint(3)
        assert dialog.breakpoints() == [3]
        assert not dialog.run_all()
        assert window.text() == "ab12"
        session = dialog.session()
        assert session is not None and not session.done
        assert session.next_lineno() == 3
        assert dialog.run_all()
        assert window.text() == "ab123"
        # 最後まで終わると、完了したセッションは備え直される。
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.session().position == 0
    finally:
        dialog.close()
        discard_close(window)


def test_recording_continues_after_pane_step():
    # 記録開始 → 操作① → ペインでステップ実行 (セッションは
    # 終わっていない) → 操作② → 記録終了、で操作②も記録に
    # 残る (陽太さんの実機報告 2026-11-14: 「再生中」の印が
    # セッション全体に残り、その間の手作業が記録から漏れた)。
    # ステップ実行で本文へ入った編集自体は、記録に混ざらない。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(
        window, "ed.insert_text('S')\ned.insert_text('T')\n")
    try:
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "X")          # 操作①
        dialog.show()
        assert dialog.step_once()
        assert window.text() == "abXS"
        assert dialog.session() is not None
        assert not dialog.session().done    # セッションは継続中
        QTest.keyClicks(window._edit, "Y")          # 操作②
        window.set_macro_recording(False)
        assert window.text() == "abXSY"
        assert window._macro_skips == 0
        assert window.recorded_commands() == [
            {"cmd": "insert_text", "args": {"text": "X"}},
            {"cmd": "insert_text", "args": {"text": "Y"}},
        ]
    finally:
        dialog.close()
        if window._python_dialog is not None:
            window._python_dialog.close()
        discard_close(window)


def test_pane_breakpoint_toggle_via_gutter_signal():
    # ガターのクリック (breakpoint_toggled) で付け外しでき、
    # 範囲外の行は無視される。
    window = make_window("ab", cursor_pos=0)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        dialog._edit.breakpoint_toggled.emit(2)
        assert dialog.breakpoints() == [2]
        dialog._edit.breakpoint_toggled.emit(2)
        assert dialog.breakpoints() == []
        dialog.toggle_breakpoint(99)
        assert dialog.breakpoints() == []
    finally:
        dialog.close()
        discard_close(window)


def test_pane_run_all_starts_at_cursor_line():
    # 通し実行は、カーソルのある行の文から新しく始める。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        _pane_cursor_to_line(dialog, 2)
        assert dialog.run_all()
        assert window.text() == "ab23"
        # 最後まで終わると、完了したセッションは備え直される。
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.session().position == 0
    finally:
        dialog.close()
        discard_close(window)


def test_pane_run_all_cursor_inside_multiline_statement():
    # 複数行にまたがる文の途中の行にカーソルがあるときは、
    # その文の先頭から始める (文の途中からは始めない)。
    window = make_window("ab", cursor_pos=2)
    source = ("ed.insert_text('1')\n"
              "for ch in '23':\n"
              "    ed.insert_text(ch)\n")
    dialog = _make_pane(window, source)
    try:
        dialog.show()
        _pane_cursor_to_line(dialog, 3)
        assert dialog.run_all()
        # 1 行目は飛ばし、for 文 (2行目から) を実行する。
        assert window.text() == "ab23"
        # 最後まで終わると、完了したセッションは備え直される。
        assert dialog.session() is not None
        assert not dialog.session().done
        assert dialog.session().position == 0
    finally:
        dialog.close()
        discard_close(window)


def test_pane_step_completion_returns_marker_to_first_line():
    # ステップ実行で最後まで終わると、完了表示 (「終わりま
    # した」) を残したまま、黄色い印が先頭の文へ戻る (Nagato
    # の確定仕様 2026-11-14)。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(
        window, "ed.insert_text('1')\nprint('hello')\n"
                "ed.insert_text('2')\n")
    try:
        dialog.show()
        assert dialog.step_once()
        first_session = dialog.session()
        assert dialog.step_once()
        assert "print: hello" in dialog._status_label.text()
        assert dialog.step_once()
        assert window.text() == "ab12"
        # 完了表示は、終えたセッションから出たものが残る。
        assert "終わりました" in dialog._status_label.text()
        # セッションは備え直され、印とカーソルは先頭の文にある。
        assert dialog.session() is not first_session
        assert dialog.session().position == 0
        assert dialog.session().next_lineno() == 1
        selections = dialog._edit.extraSelections()
        assert len(selections) == 1
        assert selections[0].format.background() == _NEXT_LINE_COLOR
        assert selections[0].cursor.selectionStart() == 0
        assert dialog._edit.textCursor().position() == 0
    finally:
        dialog.close()
        discard_close(window)


def test_pane_run_all_completion_returns_marker_to_first_line():
    # 通し実行で最後まで終わったときも、印は先頭へ戻る。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        assert dialog.run_all()
        assert window.text() == "ab123"
        assert "終わりました" in dialog._status_label.text()
        assert dialog.session() is not None
        assert dialog.session().position == 0
        selections = dialog._edit.extraSelections()
        assert len(selections) == 1
        assert selections[0].cursor.selectionStart() == 0
        # 備え直したセッションで、そのまま再実行もできる。
        assert dialog.run_all()
        assert window.text() == "ab123123"
    finally:
        dialog.close()
        discard_close(window)


def test_run_all_action_opens_pane_and_runs():
    # 「通し実行」アクション (Shift+F2): ペインが開いていなけ
    # れば開いてから、いまのマクロを通し実行する。開いている
    # ペインに対しては、同じペインで通し実行する。
    window = make_window("ab", cursor_pos=2)
    try:
        window._current_python_source = (
            "ed.insert_text('1')\ned.insert_text('2')\n")
        window._act_macro_run_all.trigger()
        app().processEvents()
        pane = window._python_dialog
        assert pane is not None
        assert window.text() == "ab12"
        window._act_macro_run_all.trigger()
        app().processEvents()
        assert window._python_dialog is pane
        assert window.text() == "ab1212"
    finally:
        if window._python_dialog is not None:
            window._python_dialog.close()
        discard_close(window)


def test_pane_error_does_not_return_marker():
    # エラーで終わったときは、印は戻らない (位置を見失わない
    # ため)。終えたセッションも、備え直さずそのまま残る。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(
        window, "ed.insert_text('1')\ned.bogus()\n")
    try:
        dialog.show()
        assert dialog.step_once()
        session = dialog.session()
        assert not dialog.step_once()
        assert window.text() == "ab1"
        assert dialog.session() is session
        assert session.error is not None
        assert dialog._edit.extraSelections() == []
    finally:
        dialog.close()
        discard_close(window)


def test_pane_click_moves_execution_point():
    # ステップ実行の途中で行をクリックすると、実行位置が
    # その行の文へ移る。すでに実行した編集は取り消さない。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        assert dialog.step_once()
        assert window.text() == "ab1"
        dialog._edit.line_clicked.emit(3)
        session = dialog.session()
        assert session is not None
        assert session.position == 2
        assert session.next_lineno() == 3
        assert dialog.step_once()
        assert window.text() == "ab13"
    finally:
        dialog.close()
        discard_close(window)


def test_pane_click_without_session_does_nothing():
    # セッションが無いときのクリックは通常のカーソル移動。
    # セッションは作られない。
    window = make_window("ab", cursor_pos=2)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        dialog._edit.line_clicked.emit(2)
        assert dialog.session() is None
        assert window.text() == "ab"
    finally:
        dialog.close()
        discard_close(window)


def test_pane_buttons_enabled_while_running():
    # 実行中も、通し実行・ステップ実行・停止は押せるまま。
    # 実行中に通し実行・ステップ実行を押し直しても、二重実行
    # にはならない (何もしない)。停止はいつでも効く。
    window = make_window("ab", cursor_pos=0)
    dialog = _make_pane(window, "while True:\n    pass\n")
    try:
        dialog.show()
        seen = []

        def check():
            seen.append((
                dialog._run_button.isEnabled(),
                dialog._step_button.isEnabled(),
                dialog._stop_button.isEnabled(),
                dialog.run_all(),
                dialog.step_once()))
            dialog.stop_run()

        QTimer.singleShot(300, check)
        assert not dialog.run_all()
        assert seen == [(True, True, True, False, False)]
        session = dialog.session()
        assert session is not None and session.stopped
    finally:
        dialog.close()
        discard_close(window)


def test_pane_breakpoint_snaps_to_statement_start():
    # 印は文の開始行に付く。複数行の文の途中行をクリックしたら
    # 開始行へ寄せ、空行・文の間のコメント行には付けない
    # (アオイの指摘 req-20261114-26: 開始行しか見ない実行側
    # では、途中行の印は止まらなかった)。
    window = make_window("ab", cursor_pos=0)
    source = ("ed.insert_text('1')\n"       # 1行目
              "# コメント\n"                # 2行目
              "for ch in '23':\n"           # 3行目
              "    ed.insert_text(ch)\n"    # 4行目
              "\n"                          # 5行目
              "ed.insert_text('6')\n")      # 6行目
    dialog = _make_pane(window, source)
    try:
        dialog.show()
        dialog._edit.breakpoint_toggled.emit(4)
        assert dialog.breakpoints() == [3]
        dialog._edit.breakpoint_toggled.emit(2)
        assert dialog.breakpoints() == [3]
        dialog._edit.breakpoint_toggled.emit(5)
        assert dialog.breakpoints() == [3]
        # 開始行をクリックすると外れる。
        dialog._edit.breakpoint_toggled.emit(3)
        assert dialog.breakpoints() == []
    finally:
        dialog.close()
        discard_close(window)


def test_pane_breakpoint_on_middle_line_stops_run():
    # 複数行の文の途中行に置いた印でも、通し実行はその文の
    # 手前で止まる。
    window = make_window("ab", cursor_pos=2)
    source = ("ed.insert_text('1')\n"
              "for ch in '23':\n"
              "    ed.insert_text(ch)\n"
              "ed.insert_text('4')\n")
    dialog = _make_pane(window, source)
    try:
        dialog.show()
        dialog.toggle_breakpoint(3)
        assert dialog.breakpoints() == [2]
        assert not dialog.run_all()
        assert window.text() == "ab1"
        assert dialog.session().next_lineno() == 2
        assert dialog.run_all()
        assert window.text() == "ab1234"
    finally:
        dialog.close()
        discard_close(window)


def test_pane_breakpoint_follows_inserted_line():
    # 先頭に行を足すと、印がその分だけ下がる (アオイの指摘
    # req-20261114-26: 編集すると印の行がずれたままだった)。
    window = make_window("ab", cursor_pos=0)
    dialog = _make_pane(window, _THREE_STATEMENTS)
    try:
        dialog.show()
        dialog.toggle_breakpoint(3)
        assert dialog.breakpoints() == [3]
        cursor = dialog._edit.textCursor()
        cursor.setPosition(0)
        cursor.insertText("ed.insert_text('0')\n")
        assert dialog.breakpoints() == [4]
    finally:
        dialog.close()
        discard_close(window)


def test_pane_width_applies_at_first_show():
    # ペインの既定幅 (PANE_WIDTH) が、初回表示でも効く (生成
    # 直後はスプリッタの幅が未確定で、右が半分を超えていた。
    # アオイの実測 req-20261114-26)。
    window = make_window("ab", cursor_pos=0)
    try:
        window.resize(1000, 600)
        app().processEvents()
        window.show_python_macro_dialog(
            source="ed.insert_text('1')\n", name="幅")
        app().processEvents()
        sizes = window._main_splitter.sizes()
        assert abs(sizes[1] - PANE_WIDTH) <= 5, sizes
    finally:
        if window._python_dialog is not None:
            window._python_dialog.close()
        discard_close(window)


def test_recording_start_replaces_registered_pane():
    # 登録マクロのペインを開いたまま記録を始めると、記録用の
    # ペインに替わる。ライブ表示は記録用にだけ結びつくので、
    # 登録マクロの本文が記録の内容に差し替わることはない
    # (アオイの指摘 req-20261114-26: 見出しの登録名のまま
    # 保存すると、登録マクロを上書きできた)。
    window = make_window("ab", cursor_pos=0)
    try:
        window.register_python_macro(
            "登録済み", "ed.insert_text('R')\n")
        window._open_registered_macro_python("登録済み")
        pane = window._python_dialog
        assert pane is not None
        assert pane.source() == "ed.insert_text('R')\n"
        assert not window._python_pane_live
        window.set_macro_recording(True)
        live_pane = window._python_dialog
        assert live_pane is not None and live_pane is not pane
        assert window._python_pane_live
        assert live_pane._live_source is not None
        # 記録が進んでも、登録マクロの本文は変わらない。
        QTest.keyClicks(window._edit, "Z")
        app().processEvents()
        live_pane._sync_live()
        assert "Z" in live_pane.source()
        entries = window.registered_macros()
        assert entries["登録済み"]["python"] == \
            "ed.insert_text('R')\n"
        window.set_macro_recording(False)
    finally:
        if window._python_dialog is not None:
            window._python_dialog.close()
        discard_close(window)


def test_open_other_macro_while_running_keeps_pane():
    # 実行中のペインがあるときに別のマクロを開こうとしても、
    # ペインは増えず、実行中のものが前に出るだけ (閉じた
    # つもりで新しいものを作ると、実行中のペインへの参照を
    # 失って子プロセスが残るおそれがある。アオイの指摘
    # req-20261114-26)。
    window = make_window("ab", cursor_pos=0)
    dialog = window.show_python_macro_dialog(
        source="while True:\n    pass\n", name="実行中")
    try:
        seen = []

        def check():
            other = window.show_python_macro_dialog(
                source="ed.insert_text('X')\n", name="別の")
            seen.append(other)
            dialog.stop_run()

        QTimer.singleShot(300, check)
        assert not dialog.run_all()
        assert seen == [dialog]
        assert window._python_dialog is dialog
    finally:
        dialog.close()
        discard_close(window)


# ---------------------------------------------------------------
# 登録マクロの Python 形式
# ---------------------------------------------------------------
def test_registered_python_macro_roundtrip():
    window = make_window("ab", cursor_pos=0)
    try:
        window.register_python_macro(
            "印", "ed.insert_text('★')\n", key="Ctrl+Alt+9")
        regs = window.registered_macros()
        assert regs["印"] == {"python": "ed.insert_text('★')\n",
                              "key": "Ctrl+Alt+9"}
        action = window._registered_macro_actions["印"]
        assert action.shortcut().toString() == "Ctrl+Alt+9"
        assert window.play_registered_macro("印")
        assert window.text() == "★ab"
        # 「いまのマクロ」としても Python が残り、再生でまた動く。
        set_cursor(window, 3)
        assert window.play_macro()
        assert window.text() == "★ab★"
    finally:
        discard_close(window)


def test_sanitize_registered_drops_broken_python():
    clean = macmod.sanitize_registered({
        "壊": {"python": "import os\n", "key": ""},
        "空": {"python": "  \n", "key": ""},
        "良": {"python": "ed.delete_line()\n", "key": "F10"},
    })
    assert "壊" not in clean and "空" not in clean
    assert clean["良"] == {"python": "ed.delete_line()\n", "key": "F10"}
    copied = macmod.copy_registered(clean)
    assert copied == clean and copied is not clean


def test_library_dialog_python_entries():
    app()
    dialog = MacroLibraryDialog(macros={
        "P": {"python": "ed.delete_line()\n", "key": ""},
        "T": {"commands": [{"cmd": "delete_line", "args": {}}],
              "key": ""},
    })
    try:
        assert dialog.is_python_macro("P")
        assert not dialog.is_python_macro("T")
        try:
            dialog.macro_text("P")
        except ValueError:
            pass
        else:
            raise AssertionError("Python の登録がテキスト化できた")
        dialog._list.setCurrentRow(0)
        assert dialog._name_label.text().endswith("（Python）")
        assert not dialog._edit_button.isEnabled()
        assert dialog._python_button.isEnabled()
        dialog._on_python_clicked()
        assert dialog.python_requested == "P"
    finally:
        dialog.close()


# ---------------------------------------------------------------
# ファイルの保存・読み込み
# ---------------------------------------------------------------
def test_save_and_load_python_macro_file():
    window = make_window("", cursor_pos=0)
    try:
        window._current_macro = [
            {"cmd": "insert_text", "args": {"text": "zz"}}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.py")
            assert window.save_macro(path, "m")
            content = Path(path).read_text(encoding="utf-8")
            assert "ed.insert_text('zz')" in content
            other = make_window("ab", cursor_pos=2)
            try:
                assert other.load_macro(path) == []
                assert other._current_python_source == content
                assert other.play_macro()
                assert other.text() == "abzz"
            finally:
                discard_close(other)
            # テキスト形式の往復は従来どおり。
            txt_path = os.path.join(tmp, "m.txt")
            assert window.save_macro(txt_path, "m")
            assert window.load_macro(txt_path) == window._current_macro
            assert window._current_python_source is None
    finally:
        discard_close(window)


def test_python_current_macro_saves_only_as_py():
    window = make_window("ab", cursor_pos=0)
    try:
        window._current_python_source = "ed.delete_line()\n"
        with tempfile.TemporaryDirectory() as tmp:
            assert not window.save_macro(os.path.join(tmp, "m.json"))
            path = os.path.join(tmp, "m.py")
            assert window.save_macro(path)
            assert Path(path).read_text(encoding="utf-8") == \
                "ed.delete_line()\n"
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# 子プロセス化の固定(レビュー req-20261114-08 の指摘3件)
# ---------------------------------------------------------------
def test_ipc_carries_state_queries_and_print():
    # 変数や繰り返しの状態は子の中で保たれ、ed の呼び出しと
    # print は IPC 越しに親へ届く。
    ed = _FakeEd()
    stepper = macro_py.PythonStepper(
        "n = 0\n"
        "for i in range(5):\n"
        "    n += i\n"
        "ed.insert_text(str(n))\n"
        "print('合計', n, ed.line_count())\n", ed)
    try:
        stepper.run_all()
        assert stepper.done and stepper.error is None
        assert ed.calls == [("insert_text", "10")]
        assert stepper.output == ["合計 10 3"]
    finally:
        stepper.close()


def test_frame_walk_escape_cannot_harm_parent():
    # レビューの再現手順: ジェネレータの gi_frame → f_back を
    # たどって本物の sys / os に届き、プロセスを落とす。子プロ
    # セスで実行する今は、落ちるのは子だけで、本体の文書と
    # プロセスは無事(このテスト自体が続くことが証拠)。
    window = make_window("ab", cursor_pos=0)
    try:
        source = (
            "def gen():\n"
            "    frame = g.gi_frame\n"
            "    found = None\n"
            "    while frame is not None:\n"
            "        if 'sys' in frame.f_globals:\n"
            "            found = frame.f_globals['sys']\n"
            "            break\n"
            "        frame = frame.f_back\n"
            "    if found is not None:\n"
            "        found.modules['os'].abort()\n"
            "    yield 1\n"
            "g = gen()\n"
            "for x in g:\n"
            "    pass\n"
            "ed.insert_text('X')\n"
        )
        assert macro_py.validate_python(source) is None
        assert not window.play_python_macro(source)
        assert window.text() == "ab"
        assert "エラー" in window.statusBar().currentMessage()
        # 本体は無事で、続けて普通のマクロも実行できる。
        assert window.play_python_macro("ed.insert_text('Y')\n")
        assert window.text() == "Yab"
    finally:
        discard_close(window)


def test_infinite_loop_stopped_by_session_stop():
    # 純粋な Python の無限ループは、stop() で子ごと止まる。
    ed = _FakeEd()
    stepper = macro_py.PythonStepper("while True:\n    pass\n", ed)
    try:
        worker = threading.Thread(target=stepper.run_all, daemon=True)
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()
        stepper.stop()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert stepper.stopped and stepper.done
        assert stepper.error is None
        assert stepper._proc.poll() is not None
    finally:
        stepper.close()


def test_dialog_stop_button_stops_infinite_loop():
    # 画面側でも、無限ループの最中に停止が効き、固まらない。
    window = make_window("ab", cursor_pos=0)
    dialog = MacroPythonDialog(
        window,
        source="while True:\n    pass\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    try:
        dialog.show()
        QTimer.singleShot(300, dialog.stop_run)
        started = time.monotonic()
        assert not dialog.run_all()
        assert time.monotonic() - started < 5
        session = dialog.session()
        assert session is not None and session.stopped
        assert "止めました" in dialog._status_label.text()
        assert window.text() == "ab"
    finally:
        dialog.close()
        discard_close(window)


def test_python_dialog_does_not_run_until_pressed():
    # 別窓を開いただけでは、セッションも作らず実行もしない。
    window = make_window("ab", cursor_pos=0)
    dialog = MacroPythonDialog(
        window,
        source="ed.insert_text('X')\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    try:
        dialog.show()
        app().processEvents()
        assert dialog.session() is None
        assert window.text() == "ab"
        assert dialog.step_once()
        assert window.text() == "Xab"
    finally:
        dialog.close()
        discard_close(window)


def test_load_macro_dialog_does_not_run():
    # 「マクロを開く」は読み込むだけ。再生は利用者が「再生」を
    # 押したときだけ(Python 形式・テキスト形式の両方)。
    window = make_window("ab", cursor_pos=2)
    original = QFileDialog.getOpenFileName
    try:
        with tempfile.TemporaryDirectory() as tmp:
            py_path = os.path.join(tmp, "m.py")
            Path(py_path).write_text(
                "ed.insert_text('X')\n", encoding="utf-8")
            QFileDialog.getOpenFileName = staticmethod(
                lambda *a, **k: (py_path, ""))
            window.load_macro_dialog()
            assert window._current_python_source is not None
            assert window.text() == "ab"
            # 明示の再生ではじめて動く。
            assert window.play_macro()
            assert window.text() == "abX"
            # テキスト形式も、開いただけでは動かない。
            txt_path = os.path.join(tmp, "m.txt")
            Path(txt_path).write_text(
                'insert_text "Z"\n', encoding="utf-8")
            QFileDialog.getOpenFileName = staticmethod(
                lambda *a, **k: (txt_path, ""))
            window.load_macro_dialog()
            assert window._current_python_source is None
            assert window.text() == "abX"
            assert window.play_macro()
            assert window.text() == "abXZ"
    finally:
        QFileDialog.getOpenFileName = original
        discard_close(window)


# ---------------------------------------------------------------
# 再確認の残り2件(req-20261114-09)
# ---------------------------------------------------------------
def _wait_proc_exit(proc, timeout=5.0):
    """子プロセスが終わるまで、イベントを回しながら待つ。"""
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app().processEvents()
        time.sleep(0.02)
    return proc.poll() is not None


def test_dialog_close_kills_child_session():
    # 別窓を done を通らない経路(close)で閉じても、抱えている
    # セッションの子が終わる。
    window = make_window("ab", cursor_pos=0)
    dialog = MacroPythonDialog(
        window,
        source="ed.insert_text('X')\ned.insert_text('Y')\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    try:
        dialog.show()
        assert dialog.step_once()
        session = dialog.session()
        assert session is not None
        proc = session._proc
        assert proc.poll() is None
        dialog.close()
        assert _wait_proc_exit(proc)
        assert dialog.session() is None
    finally:
        dialog.close()
        discard_close(window)


def test_parent_window_destroy_kills_dialog_child():
    # レビューの再現: 別窓を開いたまま EDDy の窓ごと壊れても、
    # 子プロセスが残らない(destroyed での後始末)。
    window = make_window("ab", cursor_pos=0)
    dialog = MacroPythonDialog(
        window,
        source="ed.insert_text('X')\ned.insert_text('Y')\n",
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run)
    dialog.show()
    assert dialog.step_once()
    session = dialog.session()
    assert session is not None
    proc = session._proc
    assert proc.poll() is None
    window.deleteLater()
    app().sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert _wait_proc_exit(proc)
    # 窓も別窓も壊れたあとなので、後始末はイベントを回すだけ。
    app().processEvents()


class _EdWithExtra:
    """API の表面に無い名前も持つ ed の実体(親の制限の検証用)。"""

    def __init__(self):
        self.calls = []

    def insert_text(self, text):
        self.calls.append(("insert_text", text))
        return True

    def wipe_all(self):
        self.calls.append(("wipe_all",))
        return True


def test_parent_rejects_names_outside_api_spec():
    # 親の受け口は API_SPEC と QUERY_NAMES 以外の名前を通さない
    # (実体にその名前があっても、実体へは届かない)。
    ed = _EdWithExtra()
    stepper = macro_py.PythonStepper("ed.wipe_all()\n", ed)
    try:
        try:
            stepper.step()
        except macro_py.PythonMacroError as e:
            assert "wipe_all" in str(e) and "ありません" in str(e)
        else:
            raise AssertionError("API_SPEC 外の名前が通ってしまった")
        assert ed.calls == []
        # 正規の名前は従来どおり通る。
        ok = macro_py.PythonStepper("ed.insert_text('a')\n", ed)
        try:
            ok.run_all()
            assert ed.calls == [("insert_text", "a")]
        finally:
            ok.close()
    finally:
        stepper.close()


def test_parent_side_call_limit_stops_session():
    # 子の上限は大きいままにして、親の数えだけで止まることを
    # 確かめる(上限の値は子と同じ。親側の受け口でも数える)。
    ed = _FakeEd()
    stepper = macro_py.PythonStepper(
        "for i in range(100):\n    ed.move_char_left()\n", ed)
    try:
        stepper._api_limit = 5    # 子へ渡した上限だけを差し替える
        try:
            stepper.run_all()
        except macro_py.PythonMacroError as e:
            assert "上限" in str(e)
        else:
            raise AssertionError("親側の上限で止まらない")
        # 上限までの呼び出しだけが実体へ届き、セッションは止まる。
        assert len(ed.calls) == 5
        assert stepper.error is not None and stepper.done
        assert _wait_proc_exit(stepper._proc)
    finally:
        stepper.close()


# ---------------------------------------------------------------
# 登録マクロの段 (管理画面との統合)
# ---------------------------------------------------------------
class _FakeLibrary:
    """登録マクロの口の偽物。検証は、管理画面・窓側と同じ規則の
    最小限 (名前の重複と予約キー) を写す。"""

    def __init__(self):
        self.macros = {}
        self.reserved = {"Ctrl+F": "検索"}

    def names(self):
        return list(self.macros)

    def load(self, name):
        entry = self.macros.get(name)
        if entry is None:
            return None
        if "python" in entry:
            source = entry["python"]
        else:
            source = macro_py.commands_to_python(
                entry["commands"], name)
        return {"source": source, "key": entry.get("key") or ""}

    def delete(self, name):
        if name not in self.macros:
            raise ValueError(f"マクロがありません（{name}）")
        del self.macros[name]

    def rename(self, old, new):
        new = macmod.validate_macro_name(new)
        if old not in self.macros:
            raise ValueError(f"マクロがありません（{old}）")
        if new != old and new in self.macros:
            raise ValueError(f"同じ名前のマクロが既にあります（{new}）")
        if new != old:
            renamed = {}
            for key_name, entry in self.macros.items():
                renamed[new if key_name == old else key_name] = entry
            self.macros = renamed
        return new

    def key_of(self, name):
        entry = self.macros.get(name)
        return (entry.get("key") or "") if entry is not None else ""

    def set_key(self, name, text):
        if name not in self.macros:
            return (False, f"マクロがありません（{name}）")
        if text and text in self.reserved:
            return (False,
                    f"『{text}』は組み込みの命令"
                    f"（{self.reserved[text]}）が使っているので、"
                    "マクロには付けられません。")
        displaced = []
        if text:
            for other, entry in self.macros.items():
                if other != name and entry.get("key") == text:
                    entry["key"] = ""
                    displaced.append(other)
        self.macros[name]["key"] = text
        if displaced:
            names = "・".join(displaced)
            return (True,
                    f"『{text}』は『{names}』から外して"
                    f"『{name}』へ付けました。")
        return (True, "")


def _library_with_two():
    library = _FakeLibrary()
    library.macros["甲"] = {"python": "ed.insert_text('A')\n",
                            "key": ""}
    library.macros["乙"] = {
        "commands": [{"cmd": "insert_text", "args": {"text": "B"}}],
        "key": ""}
    return library


def _make_library_pane(window, library, source="", name="",
                       loaded_name=None, confirm_discard=None,
                       on_save=None):
    """登録マクロの段つきのペインを作る。"""
    return MacroPythonDialog(
        window,
        source=source,
        name=name,
        session_factory=window._make_python_session,
        begin_run=window._begin_python_run,
        end_run=window._end_python_run,
        flush_run=window._flush_python_run,
        on_save=on_save,
        library=library,
        loaded_name=loaded_name,
        confirm_discard=confirm_discard)


def _combo_texts(pane):
    combo = pane._library_combo
    return [combo.itemText(i) for i in range(combo.count())]


def test_pane_library_lists_and_loads():
    # プルダウンに登録済みが出て、選ぶと読み込まれる。テキスト
    # 形式は、管理画面の「Python で開く」と同じ変換で開く。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()
    pane = _make_library_pane(window, library)
    try:
        pane.show()
        assert _combo_texts(pane) == ["", "甲", "乙"]
        assert pane._library_key_row.isHidden()
        assert pane.select_macro("甲")
        assert pane.source() == "ed.insert_text('A')\n"
        assert pane.loaded_name() == "甲"
        assert pane._title_label.text() == "甲"
        assert not pane._library_key_row.isHidden()
        assert pane.select_macro("乙")
        expected = macro_py.commands_to_python(
            [{"cmd": "insert_text", "args": {"text": "B"}}], "乙")
        assert pane.source() == expected
        assert "ed.insert_text('B')" in pane.source()
        assert pane.loaded_name() == "乙"
    finally:
        pane.close()
        discard_close(window)


def test_pane_library_delete_loaded_returns_to_unnamed():
    # 表示中のマクロを消したら、名前なし (本文は残す) に戻り、
    # プルダウンからも消える。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()
    pane = _make_library_pane(
        window, library, source="ed.insert_text('A')\n",
        name="甲", loaded_name="甲")
    try:
        pane.show()
        assert pane.delete_macro("甲")
        assert library.names() == ["乙"]
        assert _combo_texts(pane) == ["", "乙"]
        assert pane.loaded_name() is None
        assert pane._title_label.text() == "新規マクロ"
        assert pane.source() == "ed.insert_text('A')\n"
        assert pane._library_key_row.isHidden()
    finally:
        pane.close()
        discard_close(window)


def test_pane_library_rename():
    # 名前の変更が、登録とプルダウンと見出しに反映される。
    # 空・重複は管理画面と同じに断られる。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()
    pane = _make_library_pane(
        window, library, source="ed.insert_text('A')\n",
        name="甲", loaded_name="甲")
    try:
        pane.show()
        assert pane.rename_macro("甲", "甲改")
        assert library.names() == ["甲改", "乙"]
        assert pane.loaded_name() == "甲改"
        assert pane._title_label.text() == "甲改"
        assert pane._library_combo.currentText() == "甲改"
        assert not pane.rename_macro("甲改", "乙")
        assert "既にあります" in pane._library_note.text()
        assert not pane.rename_macro("甲改", "")
        assert library.names() == ["甲改", "乙"]
    finally:
        pane.close()
        discard_close(window)


def test_pane_library_key_assignment():
    # キー割り当てが見え、その場で変えられる。予約キーは付け
    # られず、登録どうしの重なりは後勝ちで外れる (管理画面と
    # 同じ)。保存先は呼び側の口 (＝管理画面と同じ経路)。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()
    library.macros["乙"]["key"] = "F10"
    pane = _make_library_pane(
        window, library, source="ed.insert_text('A')\n",
        name="甲", loaded_name="甲")
    try:
        pane.show()
        assert pane._library_key_edit.keySequence().toString() == ""
        # 予約キー (組み込みの検索) は付けられない。
        assert not pane.set_macro_key("Ctrl+F")
        assert library.key_of("甲") == ""
        assert "組み込み" in pane._library_key_note.text()
        # 乙の F10 を後勝ちで取る。
        assert pane.set_macro_key("F10")
        assert library.key_of("甲") == "F10"
        assert library.key_of("乙") == ""
        assert pane._library_key_edit.keySequence().toString() == "F10"
    finally:
        pane.close()
        discard_close(window)


def test_pane_library_switch_confirm():
    # 本文が編集されていたら、切り替えのときに捨ててよいか
    # 確認の口が呼ばれる。断られたら切り替わらない。編集され
    # ていなければ、確認は呼ばれない。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()
    calls = []
    answers = {"ok": False}

    def confirm():
        calls.append(1)
        return answers["ok"]

    pane = _make_library_pane(
        window, library, source="ed.insert_text('X')\n",
        confirm_discard=confirm)
    try:
        pane.show()
        # 未編集なら、確認なしで切り替わる。
        assert pane.select_macro("甲")
        assert calls == []
        # 編集してから切り替えると、確認が呼ばれる。
        pane._edit.setPlainText("ed.insert_text('Y')\n")
        assert not pane.select_macro("乙")
        assert calls == [1]
        assert pane.loaded_name() == "甲"
        assert pane.source() == "ed.insert_text('Y')\n"
        assert pane._library_combo.currentText() == "甲"
        # よければ切り替わる。
        answers["ok"] = True
        assert pane.select_macro("乙")
        assert calls == [1, 1]
        assert pane.loaded_name() == "乙"
    finally:
        pane.close()
        discard_close(window)


def test_pane_library_save_selects_saved_name():
    # 「登録として保存」が成功したら、プルダウンが更新されて
    # その名前が選ばれた状態になる。
    window = make_window("ab", cursor_pos=2)
    library = _library_with_two()

    def save(name, source):
        library.macros[name] = {"python": source, "key": ""}

    pane = _make_library_pane(
        window, library, source="ed.insert_text('N')\n",
        on_save=save)
    try:
        pane.show()
        pane._name_edit.setText("新規")
        pane._on_save_clicked()
        assert "新規" in library.names()
        assert _combo_texts(pane) == ["", "甲", "乙", "新規"]
        assert pane._library_combo.currentText() == "新規"
        assert pane.loaded_name() == "新規"
    finally:
        pane.close()
        discard_close(window)


def test_window_pane_library_uses_registered_store():
    # 窓のペインの段は、管理画面と同じ保存先 (登録マクロの
    # ストア) を読み書きする。
    window = make_window("ab", cursor_pos=2)
    try:
        window.register_python_macro(
            "窓甲", "ed.insert_text('W')\n", key="F10")
        window.register_macro(
            "窓乙", [{"cmd": "insert_text", "args": {"text": "V"}}])
        pane = window.show_python_macro_dialog()
        assert pane is not None
        texts = _combo_texts(pane)
        assert "窓甲" in texts and "窓乙" in texts
        assert pane.select_macro("窓甲")
        assert pane.source() == "ed.insert_text('W')\n"
        assert pane.set_macro_key("F11")
        assert window.registered_macros()["窓甲"]["key"] == "F11"
        assert pane.rename_macro("窓甲", "窓甲改")
        assert "窓甲改" in window.registered_macros()
        assert "窓甲" not in window.registered_macros()
        # テキスト形式の登録も、変換して開ける。
        assert pane.select_macro("窓乙")
        assert "ed.insert_text('V')" in pane.source()
        assert pane.delete_macro("窓乙")
        assert "窓乙" not in window.registered_macros()
    finally:
        if window._python_dialog is not None:
            window._python_dialog.close()
        discard_close(window)


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", name, type(e).__name__, e)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
