# tests/test_editor_keymap.py
# キー割り当ての純粋部分(editor/keymap.py の上書きの整え・差分)と、
# キー割り当ての設定画面(editor/keymap_dialog.py)を確かめる。
# 窓との結合(保存・読み戻し・開いている文書への反映)は
# tests/test_editor_window.py 側。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_keymap.py
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from editor import keymap as keymapmod  # noqa: E402
from editor.keymap_dialog import KeymapSettingsDialog, normalize_keys  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_dialog(**kwargs):
    app()
    return KeymapSettingsDialog(**kwargs)


# ---------------------------------------------------------------
# keymap.py(純粋部分)
# ---------------------------------------------------------------
def test_labels_cover_all_commands():
    names = [name for name, _label in keymapmod.COMMAND_LABELS]
    assert names == list(keymapmod.DEFAULT_KEYMAP)
    for name in names:
        assert keymapmod.command_label(name)


def test_merge_keymap_empty_string_unassigns():
    table = keymapmod.merge_keymap({"open": ""})
    assert table["open"] == ""                      # 割り当てなしの明示
    assert table["save"] == keymapmod.DEFAULT_KEYMAP["save"]
    # 知らない命令・文字列でない値は従来どおり捨てる。
    table = keymapmod.merge_keymap({"unknown": "F9", "save": 42})
    assert "unknown" not in table
    assert table["save"] == keymapmod.DEFAULT_KEYMAP["save"]


def test_sanitize_and_diff_overrides():
    clean = keymapmod.sanitize_overrides(
        {"save": "Ctrl+Alt+S", "open": "", "unknown": "F9", "find": 3})
    assert clean == {"save": "Ctrl+Alt+S", "open": ""}
    assert keymapmod.sanitize_overrides(None) == {}
    # 実効の表から差分を取り直すと、上書き分(空も含む)に戻る。
    table = keymapmod.merge_keymap(clean)
    assert keymapmod.diff_overrides(table) == clean
    assert keymapmod.diff_overrides(keymapmod.DEFAULT_KEYMAP) == {}
    assert keymapmod.diff_overrides(None) == {}


def test_normalize_keys():
    assert normalize_keys("Ctrl+S") == "Ctrl+S"
    assert normalize_keys(" shift+f3 ") == "Shift+F3"
    assert normalize_keys("") == ""
    assert normalize_keys(None) is None
    # 複数の並びは最初の1つだけ。
    assert normalize_keys("Ctrl+X, Ctrl+Y") == "Ctrl+X"


def test_hidemaru_aligned_defaults():
    # 2026-11-14 に Nagato の秀丸の割り当てへ寄せた既定。正本は
    # workspace/user/hidemaru-keymap-2026-11-14.txt。
    table = keymapmod.DEFAULT_KEYMAP
    assert table["find"] == "Ctrl+F"           # 検索
    assert table["replace"] == "Ctrl+R"        # 置換
    assert table["find_next"] == "F3"          # 下候補
    assert table["find_prev"] == "Shift+F3"    # 上候補
    assert table["repeat_last"] == "F4"
    assert table["copy_word"] == "F5"          # 単語をコピー
    assert table["copy_line"] == "Shift+F5"    # 行をコピー
    assert table["focus_next_pane"] == "F6"    # 分割ウィンドウ切り替え
    assert table["cut"] == "F7"                # 切り抜き
    assert table["copy"] == "F8"               # コピー
    assert table["paste"] == "F9"              # 貼り付け
    assert table["cut_append"] == "Shift+F7"   # 追加切り抜き
    assert table["copy_append"] == "Shift+F8"  # 追加コピー
    assert table["macro_record"] == "Shift+F1"  # 記録開始／終了
    assert table["macro_run_all"] == "Shift+F2"   # 通し実行
    assert table["delete_line"] == "Ctrl+Return"  # 行削除
    assert table["backspace"] == "Ctrl+H"      # バックスペース
    assert table["goto_line"] == "Ctrl+G"      # 指定行


def test_displaced_defaults_moved_to_free_keys():
    # 秀丸の割り当てに押し出された EDDy 側の命令の移し先。
    table = keymapmod.DEFAULT_KEYMAP
    assert table["toggle_bookmark"] == "Ctrl+F7"      # 旧 F7
    assert table["next_bookmark"] == "Ctrl+F8"        # 旧 F8
    assert table["prev_bookmark"] == "Ctrl+Shift+F8"  # 旧 Shift+F8
    assert table["prev_edited_line"] == "Ctrl+F2"     # 旧 Shift+F2
    # F2 は秀丸で未割り当てなので、次の編集行は据え置き。
    assert table["next_edited_line"] == "F2"


def test_defaults_have_no_key_collisions():
    by_key = {}
    for name, keys in keymapmod.DEFAULT_KEYMAP.items():
        if keys:
            assert keys not in by_key, (
                f"{keys} が {by_key[keys]} と {name} で重なっています")
            by_key[keys] = name
    dialog = make_dialog()
    try:
        assert dialog.conflicts() == {}
    finally:
        dialog.close()


def test_user_override_still_wins_over_new_defaults():
    # 利用者が保存した差分は、既定の変更より優先される。
    table = keymapmod.merge_keymap(
        {"cut": "Ctrl+Alt+X", "toggle_bookmark": ""})
    assert table["cut"] == "Ctrl+Alt+X"
    assert table["toggle_bookmark"] == ""  # 外したまま既定に戻らない
    assert table["copy"] == "F8"
    assert keymapmod.diff_overrides(table) == {
        "cut": "Ctrl+Alt+X", "toggle_bookmark": ""}


# ---------------------------------------------------------------
# keymap_dialog.py
# ---------------------------------------------------------------
def test_dialog_lists_all_commands():
    dialog = make_dialog()
    try:
        assert set(dialog._edits) == set(keymapmod.DEFAULT_KEYMAP)
        assert dialog.values() == dict(keymapmod.DEFAULT_KEYMAP)
        assert dialog.overrides() == {}
        assert dialog.conflicts() == {}
        for name, keys in keymapmod.DEFAULT_KEYMAP.items():
            assert dialog._edits[name].keySequence().toString() == keys
            assert dialog.is_default(name)
            # 既定のままの行は「既定に戻す」が押せない。
            assert not dialog._reset_buttons[name].isEnabled()
    finally:
        dialog.close()


def test_dialog_starts_from_given_keymap():
    table = keymapmod.merge_keymap({"save": "Ctrl+Alt+S", "open": ""})
    dialog = make_dialog(keymap=table)
    try:
        assert dialog.command_key("save") == "Ctrl+Alt+S"
        assert dialog.command_key("open") == ""
        assert dialog.overrides() == {"save": "Ctrl+Alt+S", "open": ""}
        assert not dialog.is_default("save")
        assert dialog._reset_buttons["save"].isEnabled()
    finally:
        dialog.close()


def test_dialog_change_reports_diff():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        dialog.set_command_key("save", "Ctrl+Alt+S")
        assert dialog.command_key("save") == "Ctrl+Alt+S"
        assert dialog._edits["save"].keySequence().toString() == \
            "Ctrl+Alt+S"
        assert calls == [{"save": "Ctrl+Alt+S"}]
        # 同じ値・読めない値・知らない命令では通知しない。
        dialog.set_command_key("save", "Ctrl+Alt+S")
        dialog.set_command_key("save", None)
        dialog.set_command_key("unknown", "F9")
        assert len(calls) == 1
    finally:
        dialog.close()


def test_dialog_edit_widget_drives_change():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        # F10 は既定の割り当てで使われていないキー(既定で F6 には
        # focus_next_pane が付いたので、そちらを使うと押し出しが
        # 一緒に流れてしまう)。
        dialog._edits["find_next"].setKeySequence(QKeySequence("F10"))
        assert dialog.command_key("find_next") == "F10"
        assert calls[-1] == {"find_next": "F10"}
    finally:
        dialog.close()


def test_dialog_conflict_displaces_other():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        # 「開く」の Ctrl+O を「上書き保存」へ付けると、開くは外れる。
        dialog.set_command_key("save", "Ctrl+O")
        assert dialog.command_key("save") == "Ctrl+O"
        assert dialog.command_key("open") == ""
        assert dialog._edits["open"].keySequence().isEmpty()
        assert dialog.overrides() == {"save": "Ctrl+O", "open": ""}
        # 重なりは残らず、知らせに外した命令が出る。
        assert dialog.conflicts() == {}
        note = dialog._note_label.text()
        assert "Ctrl+O" in note and "開く" in note
        assert calls[-1] == {"save": "Ctrl+O", "open": ""}
    finally:
        dialog.close()


def test_dialog_conflicts_detected_at_open():
    table = dict(keymapmod.DEFAULT_KEYMAP)
    table["save"] = "Ctrl+X"
    table["open"] = "Ctrl+X"
    dialog = make_dialog(keymap=table)
    try:
        conflicts = dialog.conflicts()
        assert set(conflicts) == {"Ctrl+X"}
        assert set(conflicts["Ctrl+X"]) == {"save", "open"}
        assert "重なり" in dialog._note_label.text()
        # 片方を変えれば、後勝ちで重なりが解消する。
        dialog.set_command_key("save", "Ctrl+Alt+S")
        assert dialog.conflicts() == {}
    finally:
        dialog.close()


def test_dialog_clear_and_reset_command():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        dialog.clear_command_key("save")
        assert dialog.command_key("save") == ""
        assert dialog.overrides() == {"save": ""}
        dialog.reset_command("save")
        assert dialog.command_key("save") == \
            keymapmod.DEFAULT_KEYMAP["save"]
        assert dialog.overrides() == {}
        assert calls[-1] == {}
        # 既定のままのリセットは通知しない。
        before = len(calls)
        dialog.reset_command("save")
        assert len(calls) == before
    finally:
        dialog.close()


def test_dialog_reset_all():
    calls = []
    dialog = make_dialog(on_change=calls.append)
    try:
        dialog.set_command_key("save", "Ctrl+Alt+S")
        dialog.clear_command_key("open")
        assert dialog.overrides() == {"save": "Ctrl+Alt+S", "open": ""}
        dialog.reset_all()
        assert dialog.values() == dict(keymapmod.DEFAULT_KEYMAP)
        assert dialog.overrides() == {}
        assert calls[-1] == {}
        # 既定のままなら通知しない。
        before = len(calls)
        dialog.reset_all()
        assert len(calls) == before
    finally:
        dialog.close()


def test_dialog_conflict_display_persists_until_resolved():
    # 重なりの表示は専用の欄と行の印で持ち、別の編集で知らせが
    # 書き換わっても消えない(件C)。付け直せばその場で消える。
    table = dict(keymapmod.DEFAULT_KEYMAP)
    table["save"] = "Ctrl+X"
    table["open"] = "Ctrl+X"
    dialog = make_dialog(keymap=table)
    try:
        assert "重なり" in dialog._conflict_label.text()
        assert dialog._name_labels["save"].text().endswith("（重なり）")
        assert dialog._name_labels["open"].text().endswith("（重なり）")
        # 関係ない編集をしても(知らせは書き換わる)、重なりの表示は
        # 残る。
        dialog.set_command_key("find", "Ctrl+Alt+F")
        assert "重なり" in dialog._conflict_label.text()
        assert dialog._name_labels["save"].text().endswith("（重なり）")
        # 片方を付け直せば、欄も印も消える。
        dialog.set_command_key("save", "Ctrl+Alt+S")
        assert dialog.conflicts() == {}
        assert dialog._conflict_label.text() == ""
        assert dialog._name_labels["save"].text() == \
            keymapmod.command_label("save")
        assert dialog._name_labels["open"].text() == \
            keymapmod.command_label("open")
    finally:
        dialog.close()


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
