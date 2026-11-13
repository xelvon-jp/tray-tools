# editor/text_edit.py
# エディタの本文ウィジェット。QPlainTextEdit に、行番号エリアと
# Ctrl+ホイールでのフォントサイズ変更、矩形選択(rect_edit の共用
# 部品。こちらの矩形は行を足せる)を足したもの。
#
# 行番号エリアは Qt の定番の作り(CodeEditor の例と同じ): エディタの
# 左端に細い帯を置き、viewport の余白で本文をずらす。スクロールと
# 再描画は updateRequest に追随する。
#
# 第3段で足した表示:
#   - 現在行の強調(薄い背景)。矩形のハイライトと同じ extra
#     selections の口に相乗りする(矩形側の差し替えに消されないよう、
#     まとめて出し直す _refresh_extra_selections が唯一の出口)
#   - 不可視文字の表示(タブ・スペース・全角スペース・改行を記号で
#     描く)。描画だけの話で、文書の内容は変えない
#   - 配色(editor/colors.py の組)をパレットと各部の色へ反映する
#
# キーの通知(窓側がマクロ記録と F4 のために受ける):
#   move_key(命令名, 選択を広げるか) … Home / End / 矢印などの素の
#     移動キー。本文の動き自体は Qt の標準のまま(こちらでは実行
#     しない)。記録側は「起きたこと」として控えるだけ。
#   undo_redo_started / undo_redo_finished … Ctrl+Z / Ctrl+Y の
#     前後。文書の変化を記録から外すために、窓側が印を立てる。
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QKeySequence,
    QPainter,
    QPalette,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import QPlainTextEdit, QStyle, QTextEdit, QWidget

import rect_edit
from editor import colors as colormod

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

# 行番号エリアの色は配色(editor/colors.py)が持つ。標準の組の値が
# 従来の (242,242,242) / (144,144,144) と同じにしてある。


def wrap_right_margin(available_width, char_width, document_margin, column):
    """桁数で折り返すための右余白(ピクセル)を計算する純粋関数。

    QPlainTextEdit には桁数指定の折り返しが無いので、右に余白を
    取って本文の見える幅を狭める。available_width は「余白を変えても
    動かない幅」(窓全体の幅から行番号エリア・スクロールバー・枠を
    引いたもの)を渡すこと。今の viewport の幅を基準にすると、余白を
    変えるたびに基準自体が動き、resizeEvent と行ったり来たりして
    プロセスごと落ちる(スタックオーバーフロー。レビュー
    req-20261113-109 の指摘)。
    目標は「本文の幅 = column × char_width + 文書の余白(両側)」。
    桁数が無い・文字幅が取れない・幅が足りないときは 0。"""
    if not column or column <= 0 or char_width <= 0:
        return 0
    target = column * char_width + 2 * document_margin
    return max(0, int(round(available_width - target)))


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
    材料にする。本文は標準のまま動く)。unrecorded_move は、Ctrl+矢印
    など命令で表せない移動が起きた合図。mouse_pressed はマウスが
    押された合図(どちらも、記録中に起きたら窓側が利用者へ知らせる)。"""

    font_size_changed = Signal(int)
    move_key = Signal(str, bool)
    unrecorded_move = Signal()
    mouse_pressed = Signal()
    undo_redo_started = Signal()
    undo_redo_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_numbers = True
        self._wrap_column = None
        self._updating_margins = False
        self._wheel_remainder = 0.0
        self._area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._on_update_request)
        # スクロールバーの出入りで本文の幅が変わったら、桁数折り返し
        # の余白を計算し直す(計算の基準は窓全体の幅なので安定)。
        self.verticalScrollBar().rangeChanged.connect(
            lambda *_args: self._update_area_width())
        self._update_area_width()
        # エディタの矩形は、貼り付けで行が足りなければ足せる。
        self.init_rect_selection(fixed_rows=False)
        # 表示(第3段)。配色は既定(標準)で始め、窓が設定の組で上書きする。
        self._show_invisibles = False
        self._current_line_enabled = True
        self._scheme = colormod.get_scheme(colormod.DEFAULT_SCHEME)
        self._current_line_color = QColor(self._scheme["current_line"])
        self._invisible_color = QColor(self._scheme["invisible"])
        self._area_background = QColor(
            self._scheme["line_number_background"])
        self._area_foreground = QColor(
            self._scheme["line_number_foreground"])
        self.cursorPositionChanged.connect(self._refresh_extra_selections)

    def _on_rect_changed(self) -> None:
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        """extra selections の唯一の出口。矩形のハイライトと現在行の
        強調を合わせて出し直す(片方の更新でもう片方が消えないよう)。"""
        selections = list(self.rect_extra_selections())
        if self._current_line_enabled:
            # ExtraSelection の型は QTextEdit 側にある(rect_edit と同じ)。
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(self._current_line_color)
            selection.format.setProperty(
                QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            selections.append(selection)
        QPlainTextEdit.setExtraSelections(self, selections)

    # ---------------------------------------------------------------
    # 配色
    # ---------------------------------------------------------------
    def apply_colors(self, scheme):
        """editor/colors.py の配色の組を、パレットと各部の色へ反映する。"""
        self._scheme = dict(scheme)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base,
                         QColor(scheme["background"]))
        palette.setColor(QPalette.ColorRole.Text,
                         QColor(scheme["foreground"]))
        palette.setColor(QPalette.ColorRole.Highlight,
                         QColor(scheme["selection_background"]))
        palette.setColor(QPalette.ColorRole.HighlightedText,
                         QColor(scheme["foreground"]))
        self.setPalette(palette)
        self._current_line_color = QColor(scheme["current_line"])
        self._invisible_color = QColor(scheme["invisible"])
        self._area_background = QColor(scheme["line_number_background"])
        self._area_foreground = QColor(scheme["line_number_foreground"])
        self._refresh_extra_selections()
        self._area.update()
        self.viewport().update()

    def color_scheme(self) -> dict:
        return dict(self._scheme)

    # ---------------------------------------------------------------
    # 現在行の強調
    # ---------------------------------------------------------------
    def current_line_enabled(self) -> bool:
        return self._current_line_enabled

    def set_current_line_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._current_line_enabled:
            return
        self._current_line_enabled = enabled
        self._refresh_extra_selections()

    # ---------------------------------------------------------------
    # 不可視文字の表示(描画だけ。本文は変えない)
    # ---------------------------------------------------------------
    def show_invisibles(self) -> bool:
        return self._show_invisibles

    def set_show_invisibles(self, visible):
        visible = bool(visible)
        if visible == self._show_invisibles:
            return
        self._show_invisibles = visible
        self.viewport().update()

    def paintEvent(self, event):  # noqa: N802  Qt の上書き
        super().paintEvent(event)
        if self._show_invisibles:
            self._paint_invisibles()

    def _paint_invisibles(self):
        """見えている行のタブ・スペース・改行を記号で描く。

        タブは →、半角スペースは ・、全角スペースは □、改行は行末に
        ↵。位置は cursorRect で取る(折り返しやスクロールに追随する)。
        描くのは見えているブロックだけに絞る。"""
        painter = QPainter(self.viewport())
        painter.setPen(self._invisible_color)
        painter.setFont(self.font())
        scratch = QTextCursor(self.document())
        block = self.firstVisibleBlock()
        viewport_height = self.viewport().height()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(
                self.contentOffset())
            if geometry.top() > viewport_height:
                break
            if block.isVisible():
                text = block.text()
                base = block.position()
                for i, ch in enumerate(text):
                    if ch == "\t":
                        symbol = "→"
                    elif ch == " ":
                        symbol = "·"
                    elif ch == "　":
                        symbol = "□"
                    else:
                        continue
                    scratch.setPosition(base + i)
                    rect = self.cursorRect(scratch)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignLeft
                                     | Qt.AlignmentFlag.AlignVCenter, symbol)
                if block.next().isValid():
                    scratch.setPosition(base + len(text))
                    rect = self.cursorRect(scratch)
                    painter.drawText(rect, Qt.AlignmentFlag.AlignLeft
                                     | Qt.AlignmentFlag.AlignVCenter, "↵")
            block = block.next()

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
            # 素の移動か(Shift の選択拡張とキーパッドは素に含める)。
            # 修飾キーを明示的に見る。~ の反転で計算すると、PySide6
            # では Ctrl 等が付いていても素の扱いになってしまう。
            extra = mods & (Qt.KeyboardModifier.ControlModifier
                            | Qt.KeyboardModifier.AltModifier
                            | Qt.KeyboardModifier.MetaModifier)
            if extra:
                # Ctrl+矢印の単語移動・Ctrl+Home の文書頭などは、
                # 1文字・1行の移動の命令では表せない。記録には入れず、
                # 窓側へ知らせる(黙って1文字移動として記録しない)。
                self.unrecorded_move.emit()
            else:
                self.move_key.emit(
                    name, bool(mods & Qt.KeyboardModifier.ShiftModifier))
        super().keyPressEvent(event)

    def mousePressEvent(self, event):  # noqa: N802  Qt の上書き
        # マウスでのカーソル移動はマクロに記録できない。記録中なら
        # 窓側が知らせるために合図する。
        self.mouse_pressed.emit()
        super().mousePressEvent(event)

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
        # 左は行番号エリア、右は桁数折り返しの余白。両方ここで決める。
        # 入り直しの印と「同じなら呼ばない」で、setViewportMargins が
        # 起こす resizeEvent との往復を断つ(レビュー req-20261113-109)。
        if self._updating_margins:
            return
        left = self.line_number_area_width()
        right = self._wrap_right_margin()
        current = self.viewportMargins()
        if (current.left() == left and current.top() == 0
                and current.right() == right and current.bottom() == 0):
            return
        self._updating_margins = True
        try:
            self.setViewportMargins(left, 0, right, 0)
        finally:
            self._updating_margins = False

    # ---------------------------------------------------------------
    # 桁数での折り返し(右の余白で見える幅を狭める)
    # ---------------------------------------------------------------
    def set_wrap_column(self, column):
        """桁数で折り返す。column 桁(半角の文字数)で折り返して
        見えるよう、右に余白を取る。None で解除(余白を戻す)。
        折り返しそのものは窓側が WidgetWidth にして行う。"""
        if column is not None:
            try:
                column = int(column)
            except (TypeError, ValueError):
                column = None
            if column is not None and column <= 0:
                column = None
        if column == self._wrap_column:
            if column is not None:
                self._update_area_width()
            return
        self._wrap_column = column
        self._update_area_width()

    def wrap_column(self):
        return self._wrap_column

    def _char_width(self) -> float:
        """半角1文字の幅(ピクセル)。取れなければ 0。"""
        metrics = self.fontMetrics()
        width = metrics.averageCharWidth()
        if width <= 0:
            width = metrics.horizontalAdvance("0")
        return width

    def _wrap_right_margin(self) -> int:
        if not self._wrap_column:
            return 0
        # 基準は窓全体の幅から、余白で動かない分(行番号エリア・
        # スクロールバー・枠)を引いた安定した幅にする。今の viewport
        # の幅を基準にすると往復して落ちる(wrap_right_margin の説明)。
        available = (self.width() - self.line_number_area_width()
                     - self._scrollbar_extent() - 2 * self.frameWidth())
        return wrap_right_margin(
            available, self._char_width(),
            self.document().documentMargin(), self._wrap_column)

    def _scrollbar_extent(self) -> int:
        """縦スクロールバーの幅(出ていなくても一定の前提で数える)。"""
        return self.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent)

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
        # 窓の幅が変わったら、桁数折り返しの余白も計算し直す。
        self._update_area_width()

    def changeEvent(self, event):  # noqa: N802  Qt の上書き
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # 字の大きさが変わると番号の桁幅も変わる。
            self._update_area_width()
            self._area.update()

    def paint_line_number_area(self, event):
        painter = QPainter(self._area)
        painter.fillRect(event.rect(), self._area_background)
        painter.setPen(self._area_foreground)
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
