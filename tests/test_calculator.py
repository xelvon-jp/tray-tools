# test_calculator.py
# 旧 Mac 風の電卓(calculator.py)のテスト。計算の本体(CalculatorCore)は画面なしで、
# 窓は offscreen で作ってボタンを押すところまで確かめる。
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calculator  # noqa: E402


def _run(keys: str) -> str:
    core = calculator.CalculatorCore()
    for key in keys.split():
        core.press(key)
    return core.display


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_basic_arithmetic():
    assert _run("1 + 2 =") == "3"
    assert _run("7 * 8 =") == "56"
    assert _run("1 0 / 4 =") == "2.5"
    assert _run("9 - 1 5 =") == "-6"
    # 浮動小数の尻尾は出ない
    assert _run("0 . 1 + 0 . 2 =") == "0.3"
    assert _run("1 / 3 =") == "0.3333333333"


def test_chain_left_to_right():
    """演算子を押した時点で左から計算する(優先順位は付けない)。"""
    assert _run("2 + 3 + 4 =") == "9"
    assert _run("2 + 3 * 4 =") == "20"
    # 演算子の押し直しは差し替え
    assert _run("5 + - 3 =") == "2"
    # 演算子の直後の = は左側を右側にも使う
    assert _run("5 + =") == "10"
    # = の結果から続けて計算できる
    assert _run("2 + 3 = * 4 =") == "20"


def test_clear_and_entry():
    # C は全部消し
    assert _run("1 2 + 3 4 C") == "0"
    assert _run("1 2 + 3 4 C 5 + 5 =") == "10"
    # E は入力中の数だけ消す(保留中の演算は残る)
    assert _run("1 2 + 3 4 E 5 =") == "17"
    # 小数点は1つまで、先行ゼロは置き換わる
    assert _run("0 0 5") == "5"
    assert _run("1 . . 5") == "1.5"
    # 末尾の削除
    assert _run("1 2 3 backspace") == "12"
    assert _run("7 backspace") == "0"


def test_divide_by_zero():
    assert _run("1 / 0 =") == "Error"
    # エラーのあと数字を押せば最初から
    assert _run("1 / 0 = 5 + 5 =") == "10"
    # 演算子の連鎖の中でも同様
    assert _run("1 / 0 +") == "Error"


def test_launcher_wiring():
    """項目表が画面 Feature の show_calculator を指し、グリフの絵が作れる。"""
    import taskbar_launcher as launcher

    item = launcher.ITEMS["calculator"]
    assert item["label"] == "電卓"
    assert item["feature"] == "screen"
    assert item["method"] == "show_calculator"
    assert "calculator" in launcher._GLYPHS
    assert launcher.DEFAULT_ITEMS[-1] == "calculator"

    source = Path(launcher.__file__).with_name("feature_screen.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef) and cls.name == "ScreenFeature"
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "show_calculator" in methods

    _qapp()
    assert not launcher._item_pixmap("calculator").isNull()


def test_window_buttons_offscreen():
    """窓を作り、ボタンを押して表示が変わる。Esc で閉じる。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QPushButton

    _qapp()
    window = calculator.create_window()
    buttons = {b.text(): b for b in window.findChildren(QPushButton)}
    for label in ("7", "×", "8", "="):
        buttons[label].click()
    assert window._display.text() == "56"
    buttons["C"].click()
    assert window._display.text() == "0"

    closed = []
    window.closed.connect(lambda: closed.append(True))
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key_Escape, Qt.NoModifier)
    window.keyPressEvent(event)
    assert closed == [True]
