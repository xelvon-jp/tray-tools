# tests/test_editor_macros.py
# エディタのマクロ(命令の列の記録・再生)と F4 の繰り返し、命令層を
# offscreen で確かめる。記録は実キー入力(QTest)で作り、別の窓へ
# 再生して結果が同じになることを見る。JSON の保存・読み戻しと、
# 手で書いたマクロの再生も確かめる。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_macros.py
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
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
from editor import macros as macmod  # noqa: E402
from editor.keymap import DEFAULT_KEYMAP  # noqa: E402

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
    window.ask_save_changes = lambda: "discard"
    window.close()
    window.deleteLater()
    app().processEvents()


def set_cursor(window, pos):
    cursor = window._edit.textCursor()
    cursor.setPosition(pos)
    window._edit.setTextCursor(cursor)


def wait_rx(window, timeout_ms=15000):
    """正規表現の非同期処理が終わるまで待つ。"""
    waited = 0
    while window._rx_pending is not None and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    assert window._rx_pending is None, "正規表現の処理が終わらない"
    QTest.qWait(10)


# ---------------------------------------------------------------
# 記録係(純粋な切り分け)
# ---------------------------------------------------------------
def test_recorder_insert_coalesces_and_last_command():
    rec = macmod.MacroRecorder()
    rec.start()
    rec.feed_change(0, 0, 1, "a", 0, 0, 0)
    rec.feed_change(1, 0, 1, "b", 1, 1, 1)
    rec.feed_change(2, 0, 2, "cd", 2, 2, 2)
    assert rec.commands == [
        {"cmd": "insert_text", "args": {"text": "abcd"}}]
    assert rec.last_command == ("insert_text", {"text": "cd"})


def test_recorder_insert_not_coalesced_across_gap():
    rec = macmod.MacroRecorder()
    rec.start()
    rec.feed_change(0, 0, 1, "a", 0, 0, 0)
    rec.feed_change(5, 0, 1, "b", 5, 5, 5)
    assert [c["args"]["text"] for c in rec.commands] == ["a", "b"]


def test_recorder_delete_classification():
    rec = macmod.MacroRecorder()
    rec.start()
    # BS: カーソルが削除範囲の後ろ
    rec.feed_change(2, 1, 0, "", 3, 3, 3)
    # Del: カーソルが削除範囲の頭
    rec.feed_change(2, 1, 0, "", 2, 2, 2)
    # 選択範囲の削除
    rec.feed_change(0, 3, 0, "", 0, 3, 3)
    assert rec.commands == [
        {"cmd": "delete_backward", "args": {"count": 1}},
        {"cmd": "delete_forward", "args": {"count": 1}},
        {"cmd": "delete_selection", "args": {}},
    ]


def test_recorder_replacement_is_not_recorded():
    rec = macmod.MacroRecorder()
    rec.start()
    rec.feed_change(0, 2, 1, "x", 0, 2, 2)   # 置き換えは記録しない
    assert rec.commands == []
    assert rec.last_command is None


def test_recorder_move_coalesces_counts():
    rec = macmod.MacroRecorder()
    rec.start()
    for _ in range(3):
        rec.note_command("move_char_left", {"count": 1, "extend": False})
    rec.note_command("move_char_left", {"count": 1, "extend": True})
    assert rec.commands == [
        {"cmd": "move_char_left", "args": {"count": 3, "extend": False}},
        {"cmd": "move_char_left", "args": {"count": 1, "extend": True}},
    ]
    # last_command は合算でなく最後の1回分(F4 は1回分を繰り返す)
    assert rec.last_command == (
        "move_char_left", {"count": 1, "extend": True})


def test_recorder_not_recording_keeps_only_last():
    rec = macmod.MacroRecorder()
    rec.note_command("cut_to_end", {})
    assert rec.commands == []
    assert rec.last_command == ("cut_to_end", {})


# ---------------------------------------------------------------
# JSON の読み書き
# ---------------------------------------------------------------
def test_macro_json_roundtrip():
    commands = [
        {"cmd": "insert_text", "args": {"text": "あい"}},
        {"cmd": "move_line_up", "args": {"count": 2, "extend": False}},
        {"cmd": "find_next", "args": {"text": "x", "case": True}},
    ]
    name, back = macmod.macro_from_json(macmod.macro_to_json(commands, "試し"))
    assert name == "試し"
    assert back == commands


def test_macro_json_accepts_bare_list():
    name, commands = macmod.macro_from_json(
        '[{"cmd": "delete_line", "args": {}}]')
    assert name == ""
    assert commands == [{"cmd": "delete_line", "args": {}}]


def test_macro_json_rejects_unknown_command():
    try:
        macmod.macro_from_json('[{"cmd": "rm_rf", "args": {}}]')
    except ValueError:
        return
    raise AssertionError("知らない命令を受け付けてしまった")


# ---------------------------------------------------------------
# 窓での記録 → 再生
# ---------------------------------------------------------------
def test_record_and_replay_same_result():
    window = make_window("alpha\nbeta\ngamma", cursor_pos=5)
    try:
        window.set_macro_recording(True)
        # 行末で改行+入力、BS、行頭へ、Ctrl+K で行末まで切り取り
        QTest.keyClick(window._edit, Qt.Key.Key_Return)
        QTest.keyClicks(window._edit, "ZZ")
        QTest.keyClick(window._edit, Qt.Key.Key_Backspace)
        QTest.keyClick(window._edit, Qt.Key.Key_Home)
        window._act_cut_eol.trigger()
        window.set_macro_recording(False)
        expected = window.text()
        recorded = window._current_macro
        assert recorded, "記録が空です"
        kinds = [c["cmd"] for c in recorded]
        assert "insert_text" in kinds and "cut_to_end" in kinds, kinds
    finally:
        discard_close(window)

    other = make_window("alpha\nbeta\ngamma", cursor_pos=5)
    try:
        assert other.play_macro(recorded)
        assert other.text() == expected, (other.text(), expected)
    finally:
        discard_close(other)


def test_recorded_find_replays_with_recorded_word():
    window = make_window("one two one two", cursor_pos=0)
    try:
        window._find_edit.setText("two")
        window.set_macro_recording(True)
        window.execute_command("find_next")
        window.execute_command("find_next")
        window.set_macro_recording(False)
        pos_after = window._edit.textCursor().position()
        recorded = window._current_macro
        assert [c["cmd"] for c in recorded] == ["find_next", "find_next"]
        assert recorded[0]["args"]["text"] == "two"
    finally:
        discard_close(window)

    other = make_window("one two one two", cursor_pos=0)
    try:
        # 検索欄は空のまま。再生は記録した語で進むはず。
        assert other.play_macro(recorded)
        assert other._edit.textCursor().position() == pos_after
    finally:
        discard_close(other)


def test_play_macro_is_one_undo_unit():
    window = make_window("ab", cursor_pos=2)
    try:
        commands = [
            {"cmd": "insert_text", "args": {"text": "XYZ"}},
            {"cmd": "move_line_start", "args": {"extend": False}},
            {"cmd": "insert_text", "args": {"text": ">"}},
        ]
        assert window.play_macro(commands)
        assert window.text() == ">abXYZ"
        window._edit.undo()
        assert window.text() == "ab"
    finally:
        discard_close(window)


def test_handwritten_macro_plays():
    window = make_window("foo bar\nbaz", cursor_pos=0)
    try:
        text = json.dumps({"name": "手書き", "commands": [
            {"cmd": "move_line_end", "args": {"extend": False}},
            {"cmd": "insert_text", "args": {"text": "!"}},
            {"cmd": "delete_line", "args": {}},
        ]})
        _name, commands = macmod.macro_from_json(text)
        assert window.play_macro(commands)
        assert window.text() == "baz"
    finally:
        discard_close(window)


def test_goto_line_handwritten_macro():
    window = make_window("a\nb\nc\nd", cursor_pos=0)
    try:
        text = json.dumps({"name": "手書き", "commands": [
            {"cmd": "goto_line", "args": {"line": 3}},
            {"cmd": "goto_line", "args": {"line": 99}},
        ]})
        _name, commands = macmod.macro_from_json(text)
        assert window.play_macro(commands)
        # 範囲外の行は最終行に収まる。
        assert window._edit.textCursor().blockNumber() == 3
    finally:
        discard_close(window)


def test_save_and_load_macro_file():
    window = make_window("ab", cursor_pos=0)
    try:
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "Q")
        window.set_macro_recording(False)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            assert window.save_macro(path, "名前")
            other = make_window("ab", cursor_pos=0)
            try:
                commands = other.load_macro(path)
                assert other.play_macro(commands)
                assert other.text() == "Qab"
            finally:
                discard_close(other)
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            assert saved["name"] == "名前"
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# F4(直前の命令の繰り返し)と delete_line
# ---------------------------------------------------------------
def test_keymap_defaults_have_repeat_and_delete_line():
    assert DEFAULT_KEYMAP["repeat_last"] == "F4"
    assert DEFAULT_KEYMAP["delete_line"] == "Ctrl+Shift+K"


def test_f4_repeats_insert_text_once():
    window = make_window("", cursor_pos=0)
    try:
        QTest.keyClicks(window._edit, "ab")
        # F4 は「最後の1回分」= b だけを繰り返す
        window.repeat_last()
        assert window.text() == "abb"
    finally:
        discard_close(window)


def test_f4_repeats_cut_to_end():
    window = make_window("hello world\nnext line", cursor_pos=2)
    try:
        window._act_cut_eol.trigger()
        assert window.text() == "he\nnext line"
        set_cursor(window, 6)   # 2行目の "nex" の後ろへ
        window.repeat_last()
        assert window.text() == "he\nnex"
    finally:
        discard_close(window)


def test_f4_without_last_command_does_nothing():
    window = make_window("abc", cursor_pos=1)
    try:
        window.repeat_last()
        assert window.text() == "abc"
    finally:
        discard_close(window)


def test_f4_cleared_by_mouse_click():
    # 記録できない操作(マウスでの移動)が起きたら、F4 はその前の
    # 命令を繰り返さない。記録中でなくても同じ(第2段レビュー残件)。
    window = make_window("hello world\nnext line", cursor_pos=2)
    try:
        window._act_cut_eol.trigger()
        assert window.text() == "he\nnext line"
        assert window._recorder.last_command is not None
        QTest.mouseClick(window._edit.viewport(),
                         Qt.MouseButton.LeftButton)
        assert window._recorder.last_command is None
        window.repeat_last()
        assert window.text() == "he\nnext line"
        assert window._macro_skips == 0   # 件数を数えるのは記録中だけ
    finally:
        discard_close(window)


def test_f4_cleared_by_unrecorded_move():
    # Ctrl+矢印(単語移動)も記録できない操作。F4 の控えは消える。
    window = make_window("one two three", cursor_pos=0)
    try:
        QTest.keyClicks(window._edit, "x")
        assert window._recorder.last_command is not None
        QTest.keyClick(window._edit, Qt.Key.Key_Right,
                       Qt.KeyboardModifier.ControlModifier)
        assert window._recorder.last_command is None
        before = window.text()
        window.repeat_last()
        assert window.text() == before
    finally:
        discard_close(window)


def test_f4_cleared_by_typing_over_selection():
    # 選択範囲を打って上書きする入力も、F4 の控えを消す。
    window = make_window("hello world", cursor_pos=2)
    try:
        window._act_cut_eol.trigger()
        assert window.text() == "he"
        assert window._recorder.last_command is not None
        window._edit.selectAll()
        QTest.keyClicks(window._edit, "x")
        assert window._recorder.last_command is None
        window.repeat_last()
        assert window.text() == "x"
    finally:
        discard_close(window)


def test_f4_cleared_while_recording_and_counted():
    # 記録中なら、控えが消えることに加えて「記録できない操作」と
    # して数えられる(知らせる仕組みと矛盾しない)。
    window = make_window("hello world", cursor_pos=2)
    try:
        window.set_macro_recording(True)
        window._act_cut_eol.trigger()
        assert window.text() == "he"
        assert window._recorder.last_command is not None
        QTest.mouseClick(window._edit.viewport(),
                         Qt.MouseButton.LeftButton)
        assert window._macro_skips == 1
        assert window._recorder.last_command is None
        window.repeat_last()
        assert window.text() == "he"
        window.set_macro_recording(False)
    finally:
        discard_close(window)


def test_delete_line_middle_and_last():
    window = make_window("one\ntwo\nthree", cursor_pos=5)
    try:
        assert window.execute_command("delete_line")
        assert window.text() == "one\nthree"
        set_cursor(window, 4)
        assert window.execute_command("delete_line")
        assert window.text() == "one"
    finally:
        discard_close(window)


def test_macro_menu_and_record_action_exist():
    window = make_window("")
    try:
        titles = [a.text() for a in window.menuBar().actions()]
        assert any("マクロ" in t for t in titles), titles
        assert window._act_macro_record.isCheckable()
    finally:
        discard_close(window)


# ---------------------------------------------------------------
# レビュー req-20261113-105 の3件(絵文字・単語移動・知らせ)
# ---------------------------------------------------------------
def test_recording_with_emoji_replays_same():
    # 絵文字(サロゲートペア)のある文書でも記録が空にならず、再生
    # 結果が一致する。contentsChange の位置は UTF-16 単位なので、
    # 挿入文字列を Python の文字列の位置で切ると壊れる。
    window = make_window("😀ab", cursor_pos=4)  # 末尾は UTF-16 で 4
    try:
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "x")
        window.set_macro_recording(False)
        recorded = window._current_macro
        assert recorded == [
            {"cmd": "insert_text", "args": {"text": "x"}}], recorded
        assert window._macro_skips == 0
    finally:
        discard_close(window)
    other = make_window("😀ab", cursor_pos=4)
    try:
        assert other.play_macro(recorded)
        assert other.text() == "😀abx"
    finally:
        discard_close(other)


def test_recording_mid_document_after_emoji():
    # 絵文字の直後への入力も、挿入した文字そのものが記録される
    # (ずれた位置の文字を拾わない)。
    window = make_window("😀ab", cursor_pos=2)  # 絵文字の直後
    try:
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "x")
        window.set_macro_recording(False)
        recorded = window._current_macro
        assert recorded == [
            {"cmd": "insert_text", "args": {"text": "x"}}], recorded
        assert window.text() == "😀xab"
    finally:
        discard_close(window)
    other = make_window("😀ab", cursor_pos=2)
    try:
        assert other.play_macro(recorded)
        assert other.text() == "😀xab"
    finally:
        discard_close(other)


def test_ctrl_arrow_not_recorded_as_char_move():
    # Ctrl+矢印の単語移動は、1文字移動として記録されない。記録から
    # 外れたことは「記録できない操作」として数える。
    window = make_window("one two three", cursor_pos=0)
    try:
        window.set_macro_recording(True)
        QTest.keyClick(window._edit, Qt.Key.Key_Right,
                       Qt.KeyboardModifier.ControlModifier)
        assert window._edit.textCursor().position() > 1  # 単語移動した
        assert window.recorded_commands() == []
        assert window._macro_skips == 1
        # 素の矢印は従来どおり記録される。
        QTest.keyClick(window._edit, Qt.Key.Key_Left)
        kinds = [c["cmd"] for c in window.recorded_commands()]
        assert kinds == ["move_char_left"], kinds
        assert window._macro_skips == 1
        window.set_macro_recording(False)
    finally:
        discard_close(window)


def test_typing_over_selection_flagged_unrecorded():
    # 選択範囲を打って上書きする入力は記録に残らず、黙って外され
    # ずに「記録できない操作」として知らされる。
    window = make_window("hello", cursor_pos=0)
    try:
        window._edit.selectAll()
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "x")
        assert window.text() == "x"
        assert window.recorded_commands() == []
        assert window._macro_skips >= 1
        window.set_macro_recording(False)
        message = window.statusBar().currentMessage()
        assert "記録できない操作" in message, message
    finally:
        discard_close(window)


def test_partial_replacement_and_block_edit_flagged():
    # 部分的な上書きと、矩形選択での編集も、記録できない操作として
    # 数えられる(記録の列だけ見ると起きなかったことに見える操作)。
    window = make_window("hello world", cursor_pos=0)
    try:
        cursor = window._edit.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
        window._edit.setTextCursor(cursor)
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "HELLO")
        assert window.text() == "HELLO world"
        assert window._macro_skips >= 1
        window.set_macro_recording(False)
    finally:
        discard_close(window)

    window = make_window("ab\ncd", cursor_pos=0)
    try:
        window._edit.set_block_selection((0, 1), (1, 1))
        window.set_macro_recording(True)
        QTest.keyClicks(window._edit, "x")
        assert window.text() == "axb\ncxd"
        assert window._macro_skips >= 1
        window.set_macro_recording(False)
    finally:
        discard_close(window)


def test_mouse_click_during_recording_flagged():
    # マウスでのカーソル移動は記録できない。記録中にクリックしたら
    # 知らせる対象になる(記録中でなければ数えない)。
    window = make_window("abcdef", cursor_pos=0)
    try:
        QTest.mouseClick(window._edit.viewport(),
                         Qt.MouseButton.LeftButton)
        assert window._macro_skips == 0
        window.set_macro_recording(True)
        QTest.mouseClick(window._edit.viewport(),
                         Qt.MouseButton.LeftButton)
        assert window._macro_skips == 1
        window.set_macro_recording(False)
    finally:
        discard_close(window)


def test_replace_macro_save_load_roundtrip():
    # 置換を含むマクロは、保存(JSON 化)→読み直し→再生が往復できる
    # (KNOWN_COMMANDS に replace_next / replace_all が要る。
    # レビュー req-20261113-107 の指摘)。
    window = make_window("a1 b2 a3", cursor_pos=0)
    try:
        window.set_macro_recording(True)
        assert window.execute_command(
            "replace_all",
            {"text": "([a-z])(\\d)", "repl": "\\2\\1",
             "case": False, "regex": True})
        wait_rx(window)   # 正規表現の置換は非同期(裏スレッド)
        window.set_macro_recording(False)
        assert window.text() == "1a 2b 3a"
        recorded = window._current_macro
        assert [c["cmd"] for c in recorded] == ["replace_all"], recorded
        text = macmod.macro_to_json(recorded, "置換の往復")
        name, loaded = macmod.macro_from_json(text)
        assert name == "置換の往復"
        assert loaded == recorded
    finally:
        discard_close(window)
    other = make_window("x9 y8", cursor_pos=0)
    try:
        assert other.play_macro(loaded)
        assert other.text() == "9x 8y"
        # replace_next も同じく往復できる。
        one = [{"cmd": "replace_next",
                "args": {"text": "9", "repl": "N",
                         "case": False, "regex": False}}]
        _name, loaded_one = macmod.macro_from_json(macmod.macro_to_json(one))
        assert loaded_one == one
        set_cursor(other, 0)   # 次置換はカーソル位置から探す
        assert other.play_macro(loaded_one)
        assert other.text() == "Nx 8y"
    finally:
        discard_close(other)


def test_replace_commands_in_handwritten_macro():
    # 手書きマクロ(JSON)でも置換の命令が使える。regex の有無や
    # 置換文字列が引数で渡り、再生で同じ結果になる。
    window = make_window("a1 b2 a3", cursor_pos=0)
    try:
        name, commands = macmod.macro_from_json(
            '{"commands": [{"cmd": "replace_all", "args": '
            '{"text": "([a-z])(\\\\d)", "repl": "\\\\2\\\\1", '
            '"case": false, "regex": true}}]}')
        assert name == ""
        assert window.play_macro(commands)
        assert window.text() == "1a 2b 3a"
    finally:
        discard_close(window)


def test_replace_recorded_and_replayed():
    # 検索バー経由の置換が記録され、別の内容へ再生できる。
    window = make_window("foo foo", cursor_pos=0)
    try:
        window._find_edit.setText("foo")
        window._replace_edit.setText("bar")
        window.set_macro_recording(True)
        assert window.execute_command("replace_all")
        window.set_macro_recording(False)
        assert window.text() == "bar bar"
        recorded = window.recorded_commands()
        assert [c["cmd"] for c in recorded] == ["replace_all"]
        assert recorded[0]["args"]["repl"] == "bar"
        window._edit.setPlainText("foo")
        assert window.play_macro()
        assert window.text() == "bar"
    finally:
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
