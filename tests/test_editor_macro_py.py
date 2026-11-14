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
from editor.macro_py_dialog import MacroPythonDialog  # noqa: E402

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
        assert dialog.session().done
        assert not dialog.step_once()
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
        assert dialog.session() is None
        assert dialog.run_all()
        assert window.text() == "ab112"
        assert dialog.session().done
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
