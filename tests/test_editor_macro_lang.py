# tests/test_editor_macro_lang.py
# マクロの言語化(テキスト形式との相互変換)と、登録マクロ(名前を
# 付けて設定に残し、メニューとキーで呼ぶ)を offscreen で確かめる。
# テキスト形式の文法は editor/macros.py の冒頭コメントが正本。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_macro_lang.py
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

from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
from editor import macros as macmod  # noqa: E402
from editor.macro_dialog import MacroLibraryDialog  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(text="", **kwargs):
    app()
    window = editor.create_window(**kwargs)
    window.show()
    if text:
        window._edit.setPlainText(text)
    return window


def discard_close(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def make_dialog(**kwargs):
    app()
    return MacroLibraryDialog(**kwargs)


SAMPLE = [
    {"cmd": "insert_text", "args": {"text": "abc"}},
    {"cmd": "move_line_start", "args": {"extend": False}},
    {"cmd": "delete_backward", "args": {"count": 2}},
    {"cmd": "find_next", "args": {"text": "foo", "case": True,
                                  "regex": False}},
    {"cmd": "replace_all", "args": {"text": "a b", "repl": "c",
                                    "case": False, "regex": True}},
    {"cmd": "goto_line", "args": {"line": 12}},
    {"cmd": "sort_lines", "args": {"order": "desc"}},
    {"cmd": "delete_line", "args": {}},
]


# ---------------------------------------------------------------
# テキスト形式(純粋な相互変換)
# ---------------------------------------------------------------
def test_text_roundtrip_all_command_kinds():
    text = macmod.commands_to_text(SAMPLE, "サンプル")
    assert text.startswith("# name: サンプル\n")
    assert 'insert_text "abc"' in text
    assert "move_line_start extend=false" in text
    assert "delete_backward count=2" in text
    assert 'find_next text="foo" case=true regex=false' in text
    assert "delete_line" in text.splitlines()
    name, commands = macmod.commands_from_text(text)
    assert name == "サンプル"
    assert commands == SAMPLE


def test_text_roundtrip_escapes_and_numbers():
    commands = [
        {"cmd": "insert_text", "args": {"text": "a\"b\nc\\d"}},
        {"cmd": "move_char_right", "args": {"count": 3, "extend": True}},
    ]
    text = macmod.commands_to_text(commands)
    # 改行を含む文字列も、エスケープされて1行に収まる。
    assert len([ln for ln in text.splitlines() if ln.strip()]) == 2
    _name, back = macmod.commands_from_text(text)
    assert back == commands


def test_text_accepts_key_value_form_for_insert_text():
    _name, commands = macmod.commands_from_text(
        'insert_text text="x y"\n')
    assert commands == [{"cmd": "insert_text", "args": {"text": "x y"}}]


def test_text_ignores_comments_and_blank_lines():
    name, commands = macmod.commands_from_text(
        "# ただのコメント\n"
        "\n"
        "delete_line\n"
        "   \n"
        "# name: あと勝ちではない\n"
        "insert_text \"z\"\n")
    # # name: は最初の1つだけを拾う(上のコメント行のあとでもよい)。
    assert name == "あと勝ちではない"
    assert [c["cmd"] for c in commands] == ["delete_line", "insert_text"]


def test_text_errors_carry_line_numbers():
    cases = [
        ("delete_line\nunknown_cmd\n", "2 行目"),          # 知らない命令
        ("insert_text \"a\"\nfind_next text=foo\n", "2 行目"),  # 読めない値
        ("goto_line line=\"x\" 残り\n", "1 行目"),          # 後ろのゴミ
        ("move_line_up 3\n", "1 行目"),                    # key= でない引数
        ("delete_backward count=[1]\n", "1 行目"),         # 値が列
        ("insert_text abc\n", "1 行目"),                   # 引用符なし
        ("insert_text \"a\" \"b\"\n", "1 行目"),            # 文字列が2つ
    ]
    for text, marker in cases:
        try:
            macmod.commands_from_text(text)
        except ValueError as e:
            assert marker in str(e), (text, e)
        else:
            raise AssertionError(f"ValueError にならない: {text!r}")


def test_text_empty_input():
    assert macmod.commands_from_text("") == ("", [])
    assert macmod.commands_from_text("# name: x\n") == ("x", [])
    assert macmod.commands_to_text([]) == ""


# ---------------------------------------------------------------
# 登録マクロの整え(純粋部分)
# ---------------------------------------------------------------
def test_sanitize_registered_forms():
    raw = {
        "通常": {"commands": [{"cmd": "delete_line", "args": {}}],
                 "key": "Ctrl+Alt+1"},
        "列だけ": [{"cmd": "insert_text", "args": {"text": "a"}}],
        "壊れた命令": {"commands": [{"cmd": "nope", "args": {}}]},
        "空": {"commands": []},
        "形違い": 42,
        "": {"commands": [{"cmd": "delete_line", "args": {}}]},
    }
    clean = macmod.sanitize_registered(raw)
    assert set(clean) == {"通常", "列だけ"}
    assert clean["通常"]["key"] == "Ctrl+Alt+1"
    assert clean["列だけ"] == {
        "commands": [{"cmd": "insert_text", "args": {"text": "a"}}],
        "key": ""}
    assert macmod.sanitize_registered(None) == {}
    assert macmod.sanitize_registered([1, 2]) == {}


# ---------------------------------------------------------------
# 管理画面(ダイアログ)
# ---------------------------------------------------------------
def test_dialog_register_rename_delete_and_notify():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        dialog.register_macro("掃除", SAMPLE[:1])
        assert dialog.names() == ["掃除"]
        assert calls[-1]["掃除"]["commands"] == SAMPLE[:1]
        dialog.rename_macro("掃除", "片付け")
        assert dialog.names() == ["片付け"]
        assert list(calls[-1]) == ["片付け"]
        dialog.delete_macro("片付け")
        assert dialog.names() == []
        assert calls[-1] == {}
        for fn in (lambda: dialog.register_macro("  ", SAMPLE[:1]),
                   lambda: dialog.rename_macro("無い", "x"),
                   lambda: dialog.delete_macro("無い")):
            try:
                fn()
            except ValueError:
                pass
            else:
                raise AssertionError("ValueError にならない")
        dialog.register_macro("重複", SAMPLE[:1])
        try:
            dialog.register_macro("重複", SAMPLE[:1])
        except ValueError:
            pass
        else:
            raise AssertionError("同じ名前の登録が通ってしまった")
    finally:
        dialog.close()


def test_dialog_key_assignment_rules():
    reserved = {"Ctrl+S": "上書き保存"}
    dialog = make_dialog(reserved_keys=reserved)
    try:
        dialog.register_macro("甲", SAMPLE[:1])
        dialog.register_macro("乙", SAMPLE[:1])
        # 組み込みと重なるキーは付けられない。
        assert not dialog.set_macro_key("甲", "Ctrl+S")
        assert dialog.macro_key("甲") == ""
        assert "上書き保存" in dialog._note_label.text()
        # 登録どうしは後勝ち。
        assert dialog.set_macro_key("甲", "Ctrl+Alt+1")
        assert dialog.set_macro_key("乙", "Ctrl+Alt+1")
        assert dialog.macro_key("甲") == ""
        assert dialog.macro_key("乙") == "Ctrl+Alt+1"
        # 外すこともできる。
        assert dialog.set_macro_key("乙", "")
        assert dialog.macro_key("乙") == ""
    finally:
        dialog.close()


def test_dialog_text_edit_roundtrip_and_error():
    dialog = make_dialog()
    try:
        dialog.register_macro("対象", SAMPLE[:1])
        text = dialog.macro_text("対象")
        assert text.startswith("# name: 対象\n")
        dialog.apply_macro_text(
            "対象", "delete_line\ninsert_text \"q\"\n")
        assert dialog.values()["対象"]["commands"] == [
            {"cmd": "delete_line", "args": {}},
            {"cmd": "insert_text", "args": {"text": "q"}}]
        before = dialog.values()
        try:
            dialog.apply_macro_text("対象", "delete_line\nbogus_cmd\n")
        except ValueError as e:
            assert "2 行目" in str(e)
        else:
            raise AssertionError("壊れたテキストが通ってしまった")
        assert dialog.values() == before  # 失敗では内容は変わらない
        try:
            dialog.apply_macro_text("対象", "# コメントだけ\n")
        except ValueError:
            pass
        else:
            raise AssertionError("空のマクロが通ってしまった")
    finally:
        dialog.close()


# ---------------------------------------------------------------
# 窓との結合(登録・保存・読み戻し・再生・メニュー)
# ---------------------------------------------------------------
def test_window_register_persists_and_plays():
    saved = []
    window = make_window("ab\ncd", on_settings_change=saved.append)
    try:
        window.register_macro(
            "行削除", [{"cmd": "delete_line", "args": {}}],
            key="Ctrl+Alt+1")
        assert saved[-1] == {"macros": {
            "行削除": {"commands": [{"cmd": "delete_line", "args": {}}],
                       "key": "Ctrl+Alt+1"}}}
        assert window.registered_macros()["行削除"]["key"] == "Ctrl+Alt+1"
        # メニューに項目があり、ショートカットが付いている。
        action = window._registered_macro_actions["行削除"]
        assert action.shortcut().toString() == "Ctrl+Alt+1"
        assert window.play_registered_macro("行削除")
        assert window.text() == "cd"
        assert not window.play_registered_macro("無い")
    finally:
        discard_close(window)


def test_window_reads_registered_from_settings():
    section = {"macros": {
        "挿入": {"commands": [{"cmd": "insert_text",
                               "args": {"text": "★"}}],
                 "key": "Ctrl+S"},   # 組み込み(保存)と重なる
        "壊れた": {"commands": [{"cmd": "nope", "args": {}}]},
    }}
    window = make_window("", editor_settings=section)
    try:
        macros = window.registered_macros()
        assert set(macros) == {"挿入"}  # 壊れた項目は読みで落ちる
        action = window._registered_macro_actions["挿入"]
        # 組み込みと重なるキーは付けない(メニューからは実行できる)。
        assert action.shortcut().isEmpty()
        assert window.play_registered_macro("挿入")
        assert window.text() == "★"
    finally:
        discard_close(window)


def test_window_macro_key_follows_keymap_changes():
    saved = []
    window = make_window("", on_settings_change=saved.append)
    try:
        # F10 は既定で使われていないキー(F9 は貼り付けの既定に
        # なったので、登録マクロには最初から付けられない)。
        window.register_macro(
            "印", [{"cmd": "toggle_bookmark", "args": {}}], key="F10")
        action = window._registered_macro_actions["印"]
        assert action.shortcut().toString() == "F10"
        # 組み込みに F10 を付けたら、マクロ側のショートカットは外れる。
        window.apply_keymap_settings({"goto_line": "F10"})
        action = window._registered_macro_actions["印"]
        assert action.shortcut().isEmpty()
    finally:
        discard_close(window)


def test_window_library_dialog_drives_registration():
    saved = []
    window = make_window("x", on_settings_change=saved.append)
    try:
        # マクロメニューに入口がある。
        labels = [a.text() for a in window._macro_menu.actions()]
        assert any("マクロの管理" in label for label in labels)
        dialog = MacroLibraryDialog(
            window, macros=window.registered_macros(),
            reserved_keys={"Ctrl+S": "上書き保存"},
            current_commands=lambda: [
                {"cmd": "insert_text", "args": {"text": "y"}}],
            on_change=window.apply_registered_macros)
        try:
            dialog.register_macro("追加", dialog._current_commands())
            assert "追加" in window.registered_macros()
            assert saved[-1]["macros"]["追加"]["commands"] == [
                {"cmd": "insert_text", "args": {"text": "y"}}]
            dialog.rename_macro("追加", "改名")
            assert "改名" in window.registered_macros()
            dialog.delete_macro("改名")
            assert window.registered_macros() == {}
            assert saved[-1] == {"macros": {}}
        finally:
            dialog.close()
    finally:
        discard_close(window)


def test_macro_text_file_roundtrip_via_window():
    window = make_window("")
    other = make_window("zz")
    try:
        commands = [{"cmd": "insert_text", "args": {"text": "T"}},
                    {"cmd": "move_line_start", "args": {"extend": False}}]
        window._current_macro = macmod.copy_commands(commands)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.txt")
            assert window.save_macro(path, "手書き")
            raw = Path(path).read_text(encoding="utf-8")
            assert raw.startswith("# name: 手書き\n")
            loaded = other.load_macro(path)
            assert loaded == commands
            assert other._current_macro_name == "手書き"
            assert other.play_macro()
            assert other.text() == "Tzz"  # カーソルは先頭から再生される
            # .json は従来どおり JSON で書かれ、読みも中身で見分ける。
            jpath = os.path.join(tmp, "m.json")
            assert window.save_macro(jpath, "j")
            assert Path(jpath).read_text(encoding="utf-8").lstrip() \
                .startswith("{")
            assert other.load_macro(jpath) == commands
    finally:
        discard_close(window)
        discard_close(other)


def test_recorded_macro_to_text_and_back_plays_same():
    window = make_window("")
    other = make_window("")
    try:
        window.set_macro_recording(True)
        window.execute_command("insert_text", {"text": "hi"})
        window.execute_command("move_line_start", {})
        window.set_macro_recording(False)
        recorded = window.recorded_commands()
        text = macmod.commands_to_text(recorded, "記録")
        name, parsed = macmod.commands_from_text(text)
        assert name == "記録"
        assert parsed == recorded
        assert other.play_macro(parsed)
        assert other.text() == "hi"
    finally:
        discard_close(window)
        discard_close(other)


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
