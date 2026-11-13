# editor/text_edit.py
# エディタの本文ウィジェット。QPlainTextEdit に、行番号エリアと
# Ctrl+ホイールでのフォントサイズ変更、矩形選択(rect_edit の共用
# 部品。こちらの矩形は行を足せる)を足したもの。
#
# 行番号エリアは Qt の定番の作り(CodeEditor の例と同じ): エディタの
# 左端に細い帯を置き、viewport の余白で本文をずらす。スクロールと
# 再描画は updateRequest に追随する。
#
# キーの通知(窓側がマクロ記録と F4 のために受ける):
#   move_key(命令名, 選択を広げるか) … Home / End / 矢印などの素の
#     移動キー。本文の動き自体は Qt の標準のまま(こちらでは実行
#     しない)。記録側は「起きたこと」として控えるだけ。
#   undo_redo_started / undo_redo_finished … Ctrl+Z / Ctrl+Y の
#     前後。文書の変化を記録から外すために、窓側が印を立てる。
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter
from PySide6.QtWidgets import QPlainTextEdit, QWidget

import rect_edit

# 移動キー → 命令名(keymap・マクロと共通の命令層の名前)。
_MOVE_KEYS = {
    Qt.Key.Key_Home: "move_line_start",
    Qt.Key.Key_End: "move_line_end",
    Qt.Key.Key_Up: "move_line_up",
    Qt.Key.Key_Down: "move_line_down",
    Qt.Key.Key_Left: "move_char_left",
    Qt.Key.Key_Right: "move_char_right",
}

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


class TextEdit(rect_edit.RectEditMixin, QPlainTextEdit):
    """エディタの本文。font_size_changed には、Ctrl+ホイールで変わった
    あとの大きさ(pt)が流れる(窓側が設定へ保存する)。move_key には
    移動キーの命令名と「選択を広げるか」が流れる(窓側がマクロ記録の
    材料にする。本文は標準のまま動く)。"""

    font_size_changed = Signal(int)
    move_key = Signal(str, bool)
    undo_redo_started = Signal()
    undo_redo_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_numbers = True
        self._wheel_remainder = 0.0
        self._area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._on_update_request)
        self._update_area_width()
        # エディタの矩形は、貼り付けで行が足りなければ足せる。
        self.init_rect_selection(fixed_rows=False)

    def _on_rect_changed(self) -> None:
        QPlainTextEdit.setExtraSelections(
            self, self.rect_extra_selections())

    def keyPressEvent(self, event):  # noqa: N802  Qt の上書き
        if (event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            # 元に戻す・やり直しの最中の文書変化は、記録の材料に
            # しない(窓側が印で受ける)。
            self.undo_redo_started.emit()
            try:
                super().keyPressEvent(event)
            finally:
                self.undo_redo_finished.emit()
            return
        if self.has_block_selection() and self.block_key(event):
            return
        name = _MOVE_KEYS.get(event.key())
        if name is not None:
            mods = event.modifiers()
            plain = not (mods & ~Qt.KeyboardModifier.ShiftModifier
                         & ~Qt.KeyboardModifier.KeypadModifier)
            if plain:
                self.move_key.emit(
                    name, bool(mods & Qt.KeyboardModifier.ShiftModifier))
        super().keyPressEvent(event)

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
