# tests/test_editor_colors.py
# 配色の「プリセット+上書き」(editor/colors.py)と、色の設定画面
# (editor/color_dialog.py)を確かめる。窓との結合(保存・読み戻し・
# 本文への反映)は tests/test_editor_window.py 側。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_colors.py
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

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QColorDialog  # noqa: E402

from editor import colors as colormod  # noqa: E402
from editor.color_dialog import ColorSettingsDialog  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_dialog(**kwargs):
    app()
    kwargs.setdefault("preset_name", "標準")
    return ColorSettingsDialog(**kwargs)


# ---------------------------------------------------------------
# colors.py(純粋部分)
# ---------------------------------------------------------------
def test_schemes_cover_all_items():
    # 設定画面に出す項目は、どのプリセットにも値があること。
    # ハイライタ側の定数とも、標準の組で一致すること(取りこぼし防止)。
    from editor import highlighter
    for name in colormod.scheme_names():
        scheme = colormod.get_scheme(name)
        for key, _label in colormod.COLOR_ITEMS:
            assert scheme.get(key), (name, key)
    standard = colormod.get_scheme("標準")
    for level, color in enumerate(highlighter.HEADING_BACKGROUNDS, start=1):
        assert standard[f"heading{level}_background"] == color.name()
    assert standard["code_block_background"] == \
        highlighter.CODE_BLOCK_BACKGROUND.name()
    assert standard["inline_code_background"] == \
        highlighter.INLINE_CODE_BACKGROUND.name()
    assert standard["inline_code_foreground"] == \
        highlighter.INLINE_CODE_FOREGROUND.name()
    assert standard["link_foreground"] == highlighter.LINK_COLOR.name()
    assert standard["quote_foreground"] == highlighter.QUOTE_COLOR.name()
    assert standard["bullet_foreground"] == highlighter.BULLET_COLOR.name()
    assert standard["star_background"] == highlighter.STAR_BACKGROUND.name()


def test_sanitize_overrides():
    assert colormod.sanitize_overrides(None) == {}
    assert colormod.sanitize_overrides("x") == {}
    assert colormod.sanitize_overrides({}) == {}
    # 知らない項目・不正な色・色でない値は捨てる。大文字は小文字に揃う。
    clean = colormod.sanitize_overrides({
        "background": "#AABBCC",
        "unknown_key": "#000000",
        "foreground": "red",
        "current_line": "#12345",
        "invisible": 123,
    })
    assert clean == {"background": "#aabbcc"}


def test_resolve_scheme_follows_preset_and_keeps_overrides():
    overrides = {"current_line": "#010203"}
    standard = colormod.resolve_scheme("標準", overrides)
    assert standard["current_line"] == "#010203"       # 上書きが勝つ
    assert standard["invisible"] == "#c0c0c0"          # 標準に追随
    hidemaru = colormod.resolve_scheme("秀丸風", overrides)
    assert hidemaru["current_line"] == "#010203"       # 切り替えても残る
    assert hidemaru["invisible"] == "#7fb3d5"          # 秀丸風に追随
    # 不正な上書きは解決の時点で落ちる。知らないプリセット名は既定に倒す。
    assert colormod.resolve_scheme("無い名前")["background"] == "#ffffff"
    assert colormod.resolve_scheme(
        "標準", {"background": "red"})["background"] == "#ffffff"


# ---------------------------------------------------------------
# color_dialog.py
# ---------------------------------------------------------------
def test_dialog_item_color_and_reset():
    calls = []
    dialog = make_dialog(
        on_change=lambda name, ov: calls.append((name, dict(ov))))
    try:
        assert dialog.values() == ("標準", {})
        assert dialog.item_color("background") == "#ffffff"
        assert not dialog.is_overridden("background")
        assert not dialog._reset_buttons["background"].isEnabled()

        dialog.set_item_color("background", "#112233")
        assert dialog.is_overridden("background")
        assert dialog.item_color("background") == "#112233"
        assert dialog._swatches["background"].text() == "#112233"
        assert dialog._reset_buttons["background"].isEnabled()
        assert calls[-1] == ("標準", {"background": "#112233"})

        # 同じ色・不正な色では通知しない。
        before = len(calls)
        dialog.set_item_color("background", "#112233")
        dialog.set_item_color("background", "red")
        dialog.set_item_color("unknown", "#000000")
        assert len(calls) == before

        dialog.reset_item("background")
        assert not dialog.is_overridden("background")
        assert dialog.item_color("background") == "#ffffff"
        assert not dialog._reset_buttons["background"].isEnabled()
        assert calls[-1] == ("標準", {})
        # 上書きが無い項目のリセットは通知しない。
        before = len(calls)
        dialog.reset_item("background")
        assert len(calls) == before
    finally:
        dialog.close()


def test_dialog_preset_switch_keeps_overrides():
    calls = []
    dialog = make_dialog(
        overrides={"current_line": "#010203"},
        on_change=lambda name, ov: calls.append((name, dict(ov))))
    try:
        assert dialog.item_color("current_line") == "#010203"
        dialog.set_preset("秀丸風")
        assert dialog.values() == ("秀丸風", {"current_line": "#010203"})
        # 上書きの無い項目は新しいプリセットに追随する。
        assert dialog.item_color("invisible") == "#7fb3d5"
        assert dialog.item_color("current_line") == "#010203"
        assert calls[-1] == ("秀丸風", {"current_line": "#010203"})
        # コンボからでも同じ(信号経由)。
        dialog._preset_combo.setCurrentText("標準")
        assert dialog.preset_name() == "標準"
        assert dialog.item_color("invisible") == "#c0c0c0"
    finally:
        dialog.close()


def test_dialog_pick_color_uses_color_dialog():
    dialog = make_dialog()
    original = QColorDialog.getColor
    try:
        QColorDialog.getColor = staticmethod(
            lambda *args, **kwargs: QColor("#445566"))
        dialog._pick_color("foreground")
        assert dialog.overrides() == {"foreground": "#445566"}
        # キャンセル(無効な色)では変わらない。
        QColorDialog.getColor = staticmethod(
            lambda *args, **kwargs: QColor())
        dialog._pick_color("foreground")
        assert dialog.overrides() == {"foreground": "#445566"}
    finally:
        QColorDialog.getColor = original
        dialog.close()


def test_dialog_starts_from_given_state():
    dialog = make_dialog(preset_name="秀丸風",
                         overrides={"background": "#0a0b0c", "bad": "x"})
    try:
        assert dialog.preset_name() == "秀丸風"
        assert dialog._preset_combo.currentText() == "秀丸風"
        # 不正な項目は開いた時点で落ちている。
        assert dialog.overrides() == {"background": "#0a0b0c"}
        assert dialog.item_color("background") == "#0a0b0c"
        assert dialog.item_color("invisible") == "#7fb3d5"
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
