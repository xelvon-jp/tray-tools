# editor/text_edit.py
# エディタの本文ウィジェット。QPlainTextEdit に、行番号エリアと
# Ctrl+ホイールでのフォントサイズ変更を足したもの。
#
# 行番号エリアは Qt の定番の作り(CodeEditor の例と同じ): エディタの
# 左端に細い帯を置き、viewport の余白で本文をずらす。スクロールと
# 再描画は updateRequest に追随する。
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QPlainTextEdit, QWidget

FONT_SIZE_MIN = 7
FONT_SIZE_MAX = 32
# フォントの大きさが取れないとき(Pixel 指定等)の代わりの起点。
FALLBACK_FONT_SIZE = 10

_AREA_BACKGROUND = QColor(242, 242, 242)
_AREA_FOREGROUND = QColor(144, 144, 144)


class LineNumberArea(QWidget):
    """行番号を描く帯。描画そのものはエディタ側が持つ(本文のブロック
    形状が要るため)。"""

    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit

    def sizeHint(self):  # noqa: N802  Qt の上書き
        return QSize(self._edit.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: N802  Qt の上書き
        self._edit.paint_line_number_area(event)


class TextEdit(QPlainTextEdit):
    """エディタの本文。font_size_changed には、Ctrl+ホイールで変わった
    あとの大きさ(pt)が流れる(窓側が設定へ保存する)。"""

    font_size_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_numbers = True
        self._wheel_remainder = 0.0
        self._area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._on_update_request)
        self._update_area_width()

    # ---------------------------------------------------------------
    # 行番号
    # ---------------------------------------------------------------
    def line_numbers_visible(self) -> bool:
        return self._line_numbers

    def set_line_numbers_visible(self, visible):
        visible = bool(visible)
        if visible == self._line_numbers:
            return
        self._line_numbers = visible
        self._area.setVisible(visible)
        self._update_area_width()
        self._area.update()

    def line_number_area_width(self) -> int:
        if not self._line_numbers:
            return 0
        digits = len(str(max(1, self.blockCount())))
        return self.fontMetrics().horizontalAdvance("9") * digits + 10

    def _update_area_width(self, *_args):
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _on_update_request(self, rect, dy):
        if dy:
            self._area.scroll(0, dy)
        else:
            self._area.update(0, rect.y(), self._area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_area_width()

    def resizeEvent(self, event):  # noqa: N802  Qt の上書き
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._area.setGeometry(QRect(
            contents.left(), contents.top(),
            self.line_number_area_width(), contents.height()))

    def changeEvent(self, event):  # noqa: N802  Qt の上書き
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # 字の大きさが変わると番号の桁幅も変わる。
            self._update_area_width()
            self._area.update()

    def paint_line_number_area(self, event):
        painter = QPainter(self._area)
        painter.fillRect(event.rect(), _AREA_BACKGROUND)
        painter.setPen(_AREA_FOREGROUND)
        height = self.fontMetrics().height()
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        geometry = self.blockBoundingGeometry(block)
        top = geometry.translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0, int(top), self._area.width() - 4, height,
                    Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            if not block.isValid():
                break
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    # ---------------------------------------------------------------
    # フォントサイズ(Ctrl+ホイール)
    # ---------------------------------------------------------------
    def font_size(self) -> int:
        """今のフォントの大きさ(pt)。取れなければ代わりの値。"""
        size = self.font().pointSizeF()
        if size <= 0:
            return FALLBACK_FONT_SIZE
        return int(round(size))

    def set_font_size(self, size):
        """大きさを範囲内に丸めて設定する。変わったら Signal を出す。"""
        size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(size)))
        if size == self.font_size():
            return
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)
        self.font_size_changed.emit(size)

    def wheelEvent(self, event):  # noqa: N802  Qt の上書き
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                # 1ノッチ(120)で1pt。細かい刻みのホイールでも
                # 合計が1ノッチに届いた分だけ動くよう、端数を貯める。
                self._wheel_remainder += delta / 120.0
                steps = int(self._wheel_remainder)
                if steps:
                    self._wheel_remainder -= steps
                    self.set_font_size(self.font_size() + steps)
            event.accept()
            return
        super().wheelEvent(event)
