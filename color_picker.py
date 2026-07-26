# color_picker.py
# 画面の色を吸い取るオーバーレイ(PowerToys Color Picker相当)。トレイアイコンは持たない部品で、
# 開閉の管理と通知は feature_screen 側が行う。
from PySide6.QtCore import Qt, QPoint, QRect, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from capture_grab import grab_region, virtual_geometry

# ルーペ: 元画像の何ピクセル四方を、何ピクセル四方に拡大して表示するか
MAG_SOURCE_PX = 15
MAG_VIEW_SIZE = 180
LABEL_HEIGHT = 34


def copy_color(hex_color: str) -> None:
    QGuiApplication.clipboard().setText(hex_color)


class ColorPickerOverlay(QWidget):
    """全モニタを覆うウインドウ。クリックした位置の色を #RRGGBB で通知する。

    開いた時点で画面全体を1回だけキャプチャし、以降はそのQImageからピクセルを読む。
    クリックのたびに撮り直すと遅いうえ、カーソル追従のルーペ表示ができない。
    (画面は静止画で覆われるため、拾える色は開いた瞬間の画面の色になる)"""

    picked = Signal(str)
    canceled = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        geometry = virtual_geometry()
        self._origin = geometry.topLeft()
        self.setGeometry(geometry)

        self._image = grab_region(geometry)
        self._dpr = self._image.devicePixelRatio() or 1.0
        self._cursor = QCursor.pos() - self._origin

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

    def _image_pos(self, local_pos: QPoint) -> QPoint:
        x = int(local_pos.x() * self._dpr)
        y = int(local_pos.y() * self._dpr)
        x = max(0, min(x, self._image.width() - 1))
        y = max(0, min(y, self._image.height() - 1))
        return QPoint(x, y)

    def _color_at(self, local_pos: QPoint) -> QColor:
        if self._image.isNull():
            return QColor(0, 0, 0)
        pos = self._image_pos(local_pos)
        return self._image.pixelColor(pos.x(), pos.y())

    def mouseMoveEvent(self, event):
        self._cursor = event.position().toPoint()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            color = self._color_at(event.position().toPoint())
            self.picked.emit(color.name().upper())

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.canceled.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.drawImage(0, 0, self._image)

        color = self._color_at(self._cursor)
        panel = self._panel_rect()

        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        source = QRect(0, 0, MAG_SOURCE_PX, MAG_SOURCE_PX)
        source.moveCenter(self._image_pos(self._cursor))
        view = QRect(panel.x(), panel.y(), MAG_VIEW_SIZE, MAG_VIEW_SIZE)
        painter.drawImage(view, self._image, source)

        # 拡大表示のどのマスを拾うのかが分かるよう、中央の1ピクセルを枠で囲う
        cell = MAG_VIEW_SIZE / MAG_SOURCE_PX
        center_cell = QRect(
            int(view.x() + cell * (MAG_SOURCE_PX // 2)),
            int(view.y() + cell * (MAG_SOURCE_PX // 2)),
            int(cell),
            int(cell),
        )
        painter.setPen(QPen(QColor(255, 255, 255), 2))
        painter.drawRect(center_cell)
        painter.setPen(QPen(QColor(0, 0, 0), 1))
        painter.drawRect(center_cell.adjusted(-1, -1, 1, 1))
        painter.setPen(QPen(QColor(20, 20, 20), 2))
        painter.drawRect(view)

        label = QRect(panel.x(), view.bottom() + 1, MAG_VIEW_SIZE, LABEL_HEIGHT)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 20, 20, 230))
        painter.drawRect(label)
        painter.setBrush(color)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawRect(label.x() + 7, label.y() + 7, 20, 20)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 11))
        painter.drawText(
            label.adjusted(36, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, color.name().upper()
        )

    def _panel_rect(self) -> QRect:
        """ルーペ+ラベルの表示位置。画面端でははみ出さないよう反対側へ寄せる。"""
        width = MAG_VIEW_SIZE
        height = MAG_VIEW_SIZE + LABEL_HEIGHT
        x = self._cursor.x() + 24
        y = self._cursor.y() + 24
        if x + width > self.width():
            x = self._cursor.x() - 24 - width
        if y + height > self.height():
            y = self._cursor.y() - 24 - height
        return QRect(max(x, 0), max(y, 0), width, height)
