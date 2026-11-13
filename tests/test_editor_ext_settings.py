# tests/test_editor_ext_settings.py
# 拡張子別の設定(editor/ext_settings.py の解決、ext_settings_dialog.py
# の画面、窓との結合)を確かめる。
#
#   - 上書きの無い項目は全体の設定に従い、上書きはタブごとに効く
#   - 設定への保存は extension_settings キーへの差分だけ
#   - 無題は全体に従い、名前を付けて保存すると拡張子の設定へ切り替わる
#   - Ctrl+ホイールの大きさは、効いている大きさの出どころへ書き戻る
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_ext_settings.py
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

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

import editor  # noqa: E402
from editor import ext_settings as extmod  # noqa: E402
from editor.ext_settings_dialog import ExtensionSettingsDialog  # noqa: E402
from editor.text_edit import TextEdit  # noqa: E402

APP = None

GLOBAL_VALUES = {
    "wrap_mode": "window",
    "wrap_column": 80,
    "markdown_highlight": True,
    "font_family": "MS Gothic",
    "font_size": 10,
    "tab_width": None,
}

NO_WRAP = QPlainTextEdit.LineWrapMode.NoWrap
WIDGET_WIDTH = QPlainTextEdit.LineWrapMode.WidgetWidth


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(editor_settings=None, saved=None):
    app()
    window = editor.create_window(
        editor_settings=editor_settings,
        on_settings_change=(saved.append if saved is not None else None))
    window.show()
    return window


def close_window(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def write_file(path, text="本文です\n"):
    Path(path).write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------
# ext_settings.py(純粋部分)
# ---------------------------------------------------------------
def test_sanitize_drops_garbage():
    assert extmod.sanitize(None) == {}
    assert extmod.sanitize("x") == {}
    assert extmod.sanitize({}) == {}
    clean = extmod.sanitize({
        "PY": {"wrap_mode": "none", "unknown": 1},
        ".Md": {"markdown_highlight": False},
        "bad ext!": {"wrap_mode": "none"},
        "txt": {"wrap_mode": "wide", "wrap_column": 3,
                "font_size": "12", "markdown_highlight": "yes",
                "tab_width": 99, "font_family": "  "},
        "log": {},
    })
    # 拡張子は小文字・ドット無しに揃い、不正な項目・値は落ちる。
    # 上書きが残らなかった拡張子は対応ごと落ちる。
    assert clean == {
        "py": {"wrap_mode": "none"},
        "md": {"markdown_highlight": False},
    }


def test_resolve_follows_global_and_keeps_overrides():
    settings = {"py": {"wrap_mode": "none", "font_size": 14}}
    resolved = extmod.resolve(GLOBAL_VALUES, settings, "py")
    assert resolved["wrap_mode"] == "none"        # 上書きが勝つ
    assert resolved["font_size"] == 14
    assert resolved["wrap_column"] == 80          # 全体に従う
    assert resolved["font_family"] == "MS Gothic"
    assert resolved["tab_width"] is None
    # 上書きの無い拡張子は全部全体に従う。大文字でも同じ拡張子。
    assert extmod.resolve(GLOBAL_VALUES, settings, "txt") == GLOBAL_VALUES
    assert extmod.resolve(GLOBAL_VALUES, settings, "PY")["wrap_mode"] \
        == "none"
    # 解決の時点でも、不正な上書きは全体を壊さない。
    dirty = {"py": {"wrap_mode": "wide", "font_size": -1}}
    resolved = extmod.resolve(GLOBAL_VALUES, dirty, "py")
    assert resolved["wrap_mode"] == "window"
    assert resolved["font_size"] == 10


def test_wrap_modes_match_window():
    from editor import window as windowmod
    assert tuple(windowmod.WRAP_MODES) == extmod.WRAP_MODES


def test_extension_of():
    assert extmod.extension_of("a/b/C.PY") == "py"
    assert extmod.extension_of("note.md") == "md"
    assert extmod.extension_of("noext") == ""
    assert extmod.extension_of(None) == ""
    assert extmod.extension_of("") == ""


# ---------------------------------------------------------------
# text_edit.py のタブ幅
# ---------------------------------------------------------------
def test_text_edit_tab_width():
    app()
    edit = TextEdit()
    try:
        assert edit.tab_width() is None
        edit.set_tab_width(4)
        assert edit.tab_width() == 4
        char = edit._char_width()
        assert char > 0
        assert abs(edit.tabStopDistance() - char * 4) < 0.6
        # フォントが変わると、距離は新しい文字幅で計算し直される。
        before = edit.tabStopDistance()
        edit.set_font_size(edit.font_size() + 6)
        assert edit.tabStopDistance() > before
        # 解除すると Qt の既定の距離に戻る。
        edit.set_tab_width(None)
        assert edit.tab_width() is None
        assert edit.tabStopDistance() == 80.0
    finally:
        edit.deleteLater()


# ---------------------------------------------------------------
# 窓との結合
# ---------------------------------------------------------------
def test_extension_wrap_applies_per_tab():
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        txt = os.path.join(tmp, "b.txt")
        write_file(py)
        write_file(txt)
        window = make_window(editor_settings={
            "extension_settings": {"py": {"wrap_mode": "none"}}})
        try:
            assert window.open_path(py)
            assert window._edit.lineWrapMode() == NO_WRAP
            assert window.open_path(txt)
            assert window._edit.lineWrapMode() == WIDGET_WIDTH
            # タブを戻しても、py のタブは上書きのまま(混ざらない)。
            assert window.activate_path(py)
            assert window._edit.lineWrapMode() == NO_WRAP
            assert window.activate_path(txt)
            assert window._edit.lineWrapMode() == WIDGET_WIDTH
        finally:
            close_window(window)


def test_extension_markdown_override():
    with tempfile.TemporaryDirectory() as tmp:
        md = os.path.join(tmp, "note.md")
        txt = os.path.join(tmp, "note.txt")
        write_file(md, "# 見出し\n")
        write_file(txt, "# 見出し\n")
        window = make_window(editor_settings={
            "extension_settings": {
                "md": {"markdown_highlight": False},
                "txt": {"markdown_highlight": True},
            }})
        try:
            assert window.open_path(md)
            assert not window._highlighter.markdown_enabled()
            assert window.open_path(txt)
            # ふだんは効かない .txt でも、上書きで入る。
            assert window._highlighter.markdown_enabled()
        finally:
            close_window(window)


def test_extension_font_and_tab_width():
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        txt = os.path.join(tmp, "b.txt")
        write_file(py)
        write_file(txt)
        window = make_window(editor_settings={
            "extension_settings": {
                "py": {"font_family": "Consolas", "font_size": 14,
                       "tab_width": 2},
            }})
        try:
            assert window.open_path(py)
            assert window._edit.font().family() == "Consolas"
            assert window._edit.font_size() == 14
            assert window._edit.tab_width() == 2
            py_distance = window._edit.tabStopDistance()
            assert window.open_path(txt)
            assert window._edit.font().family() == "MS Gothic"
            assert window._edit.font_size() == 10
            assert window._edit.tab_width() is None
            # 全体(Qt の既定 80px)より、2文字ぶんのほうが狭い。
            assert py_distance < window._edit.tabStopDistance()
        finally:
            close_window(window)


def test_column_follows_global_unless_overridden():
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        write_file(py)
        saved = []
        window = make_window(editor_settings={
            "wrap_mode": "column", "wrap_column": 50,
            "extension_settings": {"py": {"wrap_mode": "column"}}},
            saved=saved)
        try:
            assert window.open_path(py)
            # 方式だけ上書きし、桁数は全体に従う。
            assert window._edit.wrap_column() == 50
            window.set_wrap_column(60)
            assert window._edit.wrap_column() == 60
            assert {"wrap_column": 60} in saved
            # 桁数も上書きしたら、全体を変えても付いてこない。
            window.apply_extension_settings(
                {"py": {"wrap_mode": "column", "wrap_column": 30}})
            assert window._edit.wrap_column() == 30
            window.set_wrap_column(70)
            assert window._edit.wrap_column() == 30
        finally:
            close_window(window)


def test_global_wrap_change_keeps_override():
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        txt = os.path.join(tmp, "b.txt")
        write_file(py)
        write_file(txt)
        window = make_window(editor_settings={
            "extension_settings": {"py": {"wrap_mode": "none"}}})
        try:
            assert window.open_path(py)
            assert window.open_path(txt)
            py_view, txt_view = window._views[0], window._views[1]
            window.set_wrap_mode("none")
            assert txt_view.edit.lineWrapMode() == NO_WRAP
            window.set_wrap_mode("window")
            # 全体は戻るが、py のタブは上書きのまま。
            assert txt_view.edit.lineWrapMode() == WIDGET_WIDTH
            assert py_view.edit.lineWrapMode() == NO_WRAP
        finally:
            close_window(window)


def test_settings_persist_and_readback():
    saved = []
    window = make_window(editor_settings={}, saved=saved)
    try:
        window.apply_extension_settings({
            "py": {"wrap_mode": "none", "tab_width": 4},
            "md": {"wrap_mode": "bogus"},
            "bad ext!": {"wrap_mode": "none"},
        })
        # 保存されるのは extension_settings への差分だけで、
        # 不正な分は落ちている。
        assert saved[-1] == {"extension_settings": {
            "py": {"wrap_mode": "none", "tab_width": 4}}}
        assert window.extension_settings() == {
            "py": {"wrap_mode": "none", "tab_width": 4}}
    finally:
        close_window(window)
    # 読み戻し: 保存された値で新しい窓を開くと、そのまま効く。
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        write_file(py)
        window = make_window(editor_settings={
            "extension_settings": {
                "py": {"wrap_mode": "none", "tab_width": 4}}})
        try:
            assert window.open_path(py)
            assert window._edit.lineWrapMode() == NO_WRAP
            assert window._edit.tab_width() == 4
        finally:
            close_window(window)


def test_untitled_then_save_switches():
    with tempfile.TemporaryDirectory() as tmp:
        saved = []
        window = make_window(editor_settings={
            "extension_settings": {"py": {"wrap_mode": "none"}}},
            saved=saved)
        try:
            # 無題のうちは全体(ウィンドウ幅)に従う。
            assert window._edit.lineWrapMode() == WIDGET_WIDTH
            window._edit.textCursor().insertText("print(1)\n")
            target = os.path.join(tmp, "new.py")
            assert window.save_to(target)
            # 名前が付いて拡張子が決まったら、その設定へ切り替わる。
            assert window._doc.path == target
            assert window._edit.lineWrapMode() == NO_WRAP
        finally:
            close_window(window)


def test_wheel_zoom_writes_back_to_override():
    with tempfile.TemporaryDirectory() as tmp:
        py = os.path.join(tmp, "a.py")
        txt = os.path.join(tmp, "b.txt")
        write_file(py)
        write_file(txt)
        saved = []
        window = make_window(editor_settings={
            "extension_settings": {"py": {"font_size": 12}}}, saved=saved)
        try:
            assert window.open_path(py)
            assert window._edit.font_size() == 12
            assert window.open_path(txt)
            assert window._edit.font_size() == 10
            assert window.activate_path(py)
            # py のタブで大きさを変えると、上書きへ書き戻る。
            window._edit.set_font_size(14)
            assert window.extension_settings() == {"py": {"font_size": 14}}
            window._flush_pending_font_size()
            assert saved[-1] == {"extension_settings": {
                "py": {"font_size": 14}}}
            assert all("font_size" not in entry for entry in saved
                       if set(entry) == {"font_size"})
            # txt のタブの大きさは全体のまま(10)で変わらない。
            assert window.activate_path(txt)
            assert window._edit.font_size() == 10
        finally:
            close_window(window)


def test_menu_has_extension_settings():
    window = make_window()
    try:
        texts = []
        for menu_action in window.menuBar().actions():
            menu = menu_action.menu()
            if menu is None:
                continue
            for action in menu.actions():
                texts.append(action.text())
                submenu = action.menu()
                if submenu is not None:
                    texts.extend(a.text() for a in submenu.actions())
        assert any("拡張子別の設定" in text for text in texts)
    finally:
        close_window(window)


# ---------------------------------------------------------------
# ext_settings_dialog.py
# ---------------------------------------------------------------
def make_dialog(**kwargs):
    app()
    kwargs.setdefault("global_values", dict(GLOBAL_VALUES))
    return ExtensionSettingsDialog(**kwargs)


def test_dialog_set_and_reset_item():
    calls = []
    dialog = make_dialog(
        settings_map={"py": {"wrap_mode": "column"}},
        current_extension="py",
        on_change=lambda m: calls.append(m))
    try:
        assert dialog.extension() == "py"
        assert dialog.is_overridden("wrap_mode")
        assert dialog.item_value("wrap_mode") == "column"
        assert dialog.item_value("wrap_column") == 80  # 全体に従う
        assert not dialog._reset_buttons["wrap_column"].isEnabled()

        dialog.set_item("wrap_column", 40)
        assert dialog.is_overridden("wrap_column")
        assert dialog.item_value("wrap_column") == 40
        assert calls[-1] == {"py": {"wrap_mode": "column",
                                    "wrap_column": 40}}
        # 同じ値・不正な値では通知しない。
        before = len(calls)
        dialog.set_item("wrap_column", 40)
        dialog.set_item("wrap_column", 3)
        dialog.set_item("unknown", 1)
        assert len(calls) == before

        dialog.reset_item("wrap_column")
        assert not dialog.is_overridden("wrap_column")
        assert dialog.item_value("wrap_column") == 80
        assert calls[-1] == {"py": {"wrap_mode": "column"}}
    finally:
        dialog.close()


def test_dialog_switch_extension_and_reset_all():
    calls = []
    dialog = make_dialog(
        settings_map={"py": {"wrap_mode": "none", "tab_width": 2}},
        current_extension="py",
        on_change=lambda m: calls.append(m))
    try:
        # 大文字で指定しても小文字に揃う。md には上書きが無い。
        dialog.set_extension("MD")
        assert dialog.extension() == "md"
        assert not dialog.is_overridden("wrap_mode")
        assert dialog.item_value("wrap_mode") == "window"
        # 全体のタブ幅は「Qt の既定」(None)。上書きで初めて値になる。
        assert dialog.item_value("tab_width") is None
        dialog.set_extension("py")
        assert dialog.is_overridden("tab_width")
        assert dialog.item_value("tab_width") == 2
        dialog.reset_extension()
        assert dialog.values() == {}
        assert calls[-1] == {}
    finally:
        dialog.close()


def test_dialog_starts_from_given_state():
    dialog = make_dialog(
        settings_map={"py": {"wrap_mode": "none", "bad": 1},
                      "xx yy": {"wrap_mode": "none"}},
        extensions=["py", "log"], current_extension="log")
    try:
        assert dialog.extension() == "log"
        # 不正な項目・拡張子は開いた時点で落ちている。
        assert dialog.values() == {"py": {"wrap_mode": "none"}}
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
