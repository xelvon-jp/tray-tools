# capture_overlay.py
# 起動直後の「範囲選択オーバーレイ」と、タイマー起動時の「カウントダウン表示」を担当する。
# (rapture-py/overlay.py から移設。ロジックは変更なし)
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QGuiApplication
from PySide6.QtWidgets import QWidget


class SelectionOverlay(QWidget):
    """全モニタを覆う半透明ウインドウ。ドラッグで矩形を選択し、離した位置で選択確定を通知する。"""

    # rect_global はQtの論理座標系(スクリーン全体, マルチモニタ考慮済み)でのQRect
    selection_made = Signal(QRect)
    canceled = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 全モニタ分のQRect(論理座標)を合算し、マルチモニタでも1枚のオーバーレイでカバーする
        virtual_geometry = QRect()
        for screen in QGuiApplication.screens():
            virtual_geometry = virtual_geometry.united(screen.geometry())
        self._origin = virtual_geometry.topLeft()
        self.setGeometry(virtual_geometry)

        self._start = None
        self._current = None
        self._dragging = False
        # ドラッグ前でもカーソル座標を出すため、ドラッグ中かどうかに関わらず追う
        self._hover = None

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._current = self._start
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event):
        self._hover = event.position().toPoint()
        if self._dragging:
            self._current = self._hover
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            rect_local = QRect(self._start, self._current).normalized()

            # 小さすぎる選択(クリックだけ等)は無視して選択継続する
            if rect_local.width() < 4 or rect_local.height() < 4:
                self._start = None
                self._current = None
                self.update()
                return

            rect_global = rect_local.translated(self._origin)
            self.selection_made.emit(rect_global)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.canceled.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

        if self._dragging and self._start is not None and self._current is not None:
            rect_local = QRect(self._start, self._current).normalized()

            # 選択範囲部分だけ塗りを消して見やすくする
            painter.save()
            painter.setCompositionMode(QPainter.CompositionMode_Clear)
            painter.fillRect(rect_local, Qt.transparent)
            painter.restore()

            pen = QPen(QColor(0, 200, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect_local)

            self._draw_info(painter, rect_local)
        else:
            self._draw_hover_info(painter)

    def _to_global(self, point: QPoint) -> QPoint:
        """オーバーレイ内のローカル座標を実際の画面座標へ直す。
        オーバーレイの左上は仮想デスクトップの左上であり、プライマリの左や上にモニタが
        あると原点が(0,0)にならない。ローカル座標のまま表示すると実際の画面座標とずれる。"""
        return point + self._origin

    def _draw_info(self, painter, rect_local):
        """選択中の情報表示。減光や選択枠の描画を共有したまま表示内容だけ差し替えられるよう、
        サブクラス(screen_ruler.RulerOverlay)のために切り出してある。"""
        origin = self._to_global(rect_local.topLeft())
        info_text = f"{rect_local.width()} x {rect_local.height()}  ({origin.x()}, {origin.y()})"
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        text_pos = QPoint(rect_local.x(), max(rect_local.y() - 6, 12))
        painter.drawText(text_pos, info_text)

    def _draw_hover_info(self, painter):
        """まだドラッグしていないときのカーソル座標表示。範囲を決める前に
        「今どこを指しているか」が分かるようにする。"""
        if self._hover is None:
            return
        point = self._to_global(self._hover)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        painter.drawText(
            QPoint(self._hover.x() + 14, max(self._hover.y() - 8, 12)),
            f"({point.x()}, {point.y()})",
        )


class CountdownOverlay(QWidget):
    """タイマー起動時、画面隅に小さくカウントダウンを表示するウインドウ。"""

    finished = Signal()

    def __init__(self, seconds: int):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._remaining = seconds
        self.resize(120, 80)

        screen_geo = QGuiApplication.primaryScreen().geometry()
        self.move(screen_geo.right() - 140, screen_geo.top() + 20)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.finished.emit()
        else:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 28, QFont.Bold))
        painter.drawText(self.rect(), Qt.AlignCenter, str(max(self._remaining, 0)))
