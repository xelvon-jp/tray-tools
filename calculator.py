# calculator.py
# 旧 Mac 風の電卓。縦ランチャ(taskbar_launcher.py)の項目から出す小窓。
#
# 初代 Macintosh の Calculator DA の見た目(白黒・太い枠・縞模様のタイトルバー)を
# Qt で寄せたもの。中身は四則演算だけの素直な電卓で、演算は押した順に左から
# 計算する(当時の電卓と同じ。優先順位は付けない)。
#
# 計算の本体は CalculatorCore(画面を持たない純粋な状態機械)に分けてあり、
# テストはそちらを中心に回す。窓は表示と入力の受け付けだけ。
#
# カレンダーと違って、フォーカスが外れても閉じない。計算の途中で別の窓の数字を
# 見に行くのが普通の使い方で、そのたびに消えると困るため。閉じ方は、項目を
# もう一度押す(トグルは feature_screen.show_calculator 側)/ Esc / タイトルバー
# 左の閉じ箱、の3つ。
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import (
    QGridLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

_MARGIN = 8
_MAX_ENTRY_CHARS = 12

_OPS = {"+", "-", "*", "/"}


def _format_number(value: float) -> str:
    """結果の表示形。整数は整数らしく、小数は有効数字10桁までに丸める
    (0.1 + 0.2 = 0.30000000000000004 のような浮動小数の尻尾を出さない)。"""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


class CalculatorCore:
    """電卓の状態機械。press(キー)で操作し、display に表示文字列を持つ。

    キーは "0".."9" / "." / "+" / "-" / "*" / "/" / "=" / "C" / "E" /
    "backspace"。C は全部消し、E は入力中の数だけ消す(当時の並びのまま)。
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._entry = "0"      # 入力中の数(文字列のまま持つ)
        self._acc = None       # 演算の左側の値
        self._op = None        # 保留中の演算子
        self._typing = False   # 数字を入力中か(演算子を押した直後は False)
        self._error = False
        self.display = "0"

    # ---------------------------------------------------------------
    def press(self, key: str) -> None:
        if key == "C":
            self._reset()
            return
        if self._error:
            if key.isdigit():
                self._reset()
            else:
                return
        if key.isdigit():
            self._press_digit(key)
        elif key == ".":
            self._press_dot()
        elif key in _OPS:
            self._press_operator(key)
        elif key == "=":
            self._press_equals()
        elif key == "E":
            self._entry = "0"
            self._typing = True
            self.display = "0"
        elif key == "backspace":
            self._press_backspace()

    def _press_digit(self, digit: str) -> None:
        if not self._typing:
            self._entry = digit
            self._typing = True
        elif self._entry == "0":
            self._entry = digit
        elif len(self._entry) < _MAX_ENTRY_CHARS:
            self._entry += digit
        self.display = self._entry

    def _press_dot(self) -> None:
        if not self._typing:
            self._entry = "0."
            self._typing = True
        elif "." not in self._entry:
            self._entry += "."
        self.display = self._entry

    def _press_backspace(self) -> None:
        if not self._typing:
            return
        self._entry = self._entry[:-1]
        if self._entry in ("", "-"):
            self._entry = "0"
        self.display = self._entry

    def _apply(self, a: float, b: float, op: str) -> float:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if b == 0:
            raise ZeroDivisionError
        return a / b

    def _fail(self) -> None:
        self._reset()
        self._error = True
        self.display = "Error"

    def _press_operator(self, op: str) -> None:
        if self._typing:
            current = float(self._entry)
            if self._op is not None and self._acc is not None:
                try:
                    self._acc = self._apply(self._acc, current, self._op)
                except ZeroDivisionError:
                    self._fail()
                    return
            else:
                self._acc = current
        elif self._acc is None:
            # = の直後など。表示中の数が左側になる。
            self._acc = float(self._entry)
        # 入力中でないときは、保留中の演算子の差し替えだけ。
        self._op = op
        self._typing = False
        self.display = _format_number(self._acc)

    def _press_equals(self) -> None:
        if self._op is None or self._acc is None:
            return
        # 演算子を押した直後の = は、左側をそのまま右側にも使う(5 + = → 10)。
        operand = float(self._entry) if self._typing else self._acc
        try:
            result = self._apply(self._acc, operand, self._op)
        except ZeroDivisionError:
            self._fail()
            return
        self._entry = _format_number(result)
        self._acc = None
        self._op = None
        self._typing = False
        self.display = self._entry


# -------------------------------------------------------------------
# 窓(旧 Mac 風の外観)
# -------------------------------------------------------------------
class _TitleBar(QWidget):
    """縞模様＋中央に題名＋左に閉じ箱、の古典 Mac 風タイトルバー。

    ドラッグで窓を動かす。閉じ箱を押したら持ち主の窓を閉じる。"""

    def __init__(self, parent: QWidget, title: str):
        super().__init__(parent)
        self._title = title
        self.setFixedHeight(22)
        self._drag_offset = None
        self._close_armed = False

    def _close_rect(self) -> QRect:
        return QRect(7, 5, 12, 12)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("white"))
        for y in (4, 7, 10, 13, 16, 19):
            painter.setPen(QColor("#8a8a8a"))
            painter.drawLine(3, y, self.width() - 4, y)
        # 題名は縞の上に白い帯を置いて読ませる(本物と同じ手つき)。
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSize(10)
        painter.setFont(font)
        text_w = painter.fontMetrics().horizontalAdvance(self._title)
        pad = 8
        band = QRect((self.width() - text_w) // 2 - pad, 0,
                     text_w + pad * 2, self.height())
        painter.fillRect(band, QColor("white"))
        painter.setPen(QColor("black"))
        painter.drawText(self.rect(), Qt.AlignCenter, self._title)
        # 閉じ箱
        painter.setPen(QColor("black"))
        painter.fillRect(self._close_rect(), QColor("white"))
        painter.drawRect(self._close_rect())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._close_rect().contains(event.position().toPoint()):
                self._close_armed = True
            else:
                self._drag_offset = (
                    event.globalPosition().toPoint()
                    - self.window().frameGeometry().topLeft()
                )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._close_armed and self._close_rect().contains(event.position().toPoint()):
            self.window().close()
        self._close_armed = False
        self._drag_offset = None
        super().mouseReleaseEvent(event)


# ボタンの並び。(表示, キー, 行, 列, 行幅, 列幅)
_BUTTONS = [
    ("C", "C", 0, 0, 1, 1), ("E", "E", 0, 1, 1, 1),
    ("÷", "/", 0, 2, 1, 1), ("×", "*", 0, 3, 1, 1),
    ("7", "7", 1, 0, 1, 1), ("8", "8", 1, 1, 1, 1),
    ("9", "9", 1, 2, 1, 1), ("−", "-", 1, 3, 1, 1),
    ("4", "4", 2, 0, 1, 1), ("5", "5", 2, 1, 1, 1),
    ("6", "6", 2, 2, 1, 1), ("+", "+", 2, 3, 1, 1),
    ("1", "1", 3, 0, 1, 1), ("2", "2", 3, 1, 1, 1),
    ("3", "3", 3, 2, 1, 1), ("=", "=", 3, 3, 2, 1),
    ("0", "0", 4, 0, 1, 2), (".", ".", 4, 2, 1, 1),
]

_STYLE = """
QWidget#calcRoot { background: white; border: 2px solid black; }
QLineEdit#calcDisplay {
    background: white; border: 2px solid black; color: black;
    padding: 2px 6px; font-size: 17px; font-weight: bold;
}
QPushButton {
    background: white; border: 2px solid black; color: black;
    font-size: 15px; font-weight: bold; padding: 4px 0;
}
QPushButton:pressed { background: black; color: white; }
"""

_KEY_MAP = {
    Qt.Key_Period: ".", Qt.Key_Comma: ".",
    Qt.Key_Plus: "+", Qt.Key_Minus: "-",
    Qt.Key_Asterisk: "*", Qt.Key_Slash: "/",
    Qt.Key_Enter: "=", Qt.Key_Return: "=", Qt.Key_Equal: "=",
    Qt.Key_Backspace: "backspace",
}


class CalculatorWindow(QWidget):
    """電卓の小窓。閉じたら closed が出る(参照の始末は持ち主の役目)。"""

    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("calcRoot")
        self.setWindowTitle("Calculator")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setStyleSheet(_STYLE)
        self.core = CalculatorCore()

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(6)
        root.addWidget(_TitleBar(self, "Calculator"))

        self._display = QLineEdit(self.core.display, self)
        self._display.setObjectName("calcDisplay")
        self._display.setReadOnly(True)
        self._display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._display.setFocusPolicy(Qt.NoFocus)
        root.addWidget(self._display)

        grid = QGridLayout()
        grid.setSpacing(5)
        for label, key, row, col, rowspan, colspan in _BUTTONS:
            button = QPushButton(label, self)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda _checked=False, k=key: self._press(k))
            grid.addWidget(button, row, col, rowspan, colspan)
        root.addLayout(grid)
        self.setFixedSize(self.sizeHint())

    # ---------------------------------------------------------------
    def _press(self, key: str) -> None:
        self.core.press(key)
        self._display.setText(self.core.display)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        if event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return super().keyPressEvent(event)
        key = None
        if Qt.Key_0 <= event.key() <= Qt.Key_9:
            key = chr(event.key())
        elif event.key() in _KEY_MAP:
            key = _KEY_MAP[event.key()]
        elif event.key() == Qt.Key_C:
            key = "C"
        elif event.key() == Qt.Key_E:
            key = "E"
        if key is not None:
            self._press(key)
            return
        super().keyPressEvent(event)

    # ---------------------------------------------------------------
    # 表示
    # ---------------------------------------------------------------
    def show_at_cursor_screen(self) -> None:
        """カーソルのある画面の右下(タスクバーを除いた領域)に出す。
        カレンダーと同じ場所感。"""
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = avail.right() + 1 - self.width() - _MARGIN
            y = avail.bottom() + 1 - self.height() - _MARGIN
            self.move(max(x, avail.left()), max(y, avail.top()))
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


def create_window() -> CalculatorWindow:
    return CalculatorWindow()
