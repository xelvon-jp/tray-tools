# capture_overlay.py
# 「範囲選択オーバーレイ」(素のものと、画面を凍結してから選ばせるもの)と、
# タイマー起動時の「カウントダウン表示」を担当する。
# (rapture-py/overlay.py から移設)
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QImage, QPen, QFont, QGuiApplication
from PySide6.QtWidgets import QWidget

from capture_grab import grab_region, virtual_geometry


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
        self._draw_background(painter)

        if self._dragging and self._start is not None and self._current is not None:
            rect_local = QRect(self._start, self._current).normalized()

            self._draw_selection_background(painter, rect_local)

            pen = QPen(QColor(0, 200, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect_local)

            self._draw_info(painter, rect_local)
        else:
            self._draw_hover_info(painter)

    def _draw_background(self, painter):
        """全面の減光。情報表示と同様、サブクラス(FrozenSelectionOverlay)が
        背景の作り方だけ差し替えられるよう切り出してある。"""
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

    def _draw_selection_background(self, painter, rect_local):
        """選択範囲部分だけ塗りを消して見やすくする。"""
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(rect_local, Qt.transparent)
        painter.restore()

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


class FrozenSelectionOverlay(SelectionOverlay):
    """画面を凍結してから範囲を選ばせるオーバーレイ。

    開いた時点で全モニタ分を1枚のQImageとして撮り、その静止画を全画面に表示する。選択が
    確定しても撮り直さず crop() でこの画像から切り出すので、選択中に見えていた絵と保存される
    絵が必ず一致する(色を吸い取るオーバーレイと同じ発想)。カウントダウン中に開いたメニューや
    ツールチップは、選択操作で消えても撮れる。"""

    def __init__(self):
        super().__init__()
        geometry = virtual_geometry()
        self._image = grab_region(geometry)
        self._dpr = self._image.devicePixelRatio() or 1.0

    def _draw_background(self, painter):
        painter.drawImage(0, 0, self._image)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

    def _draw_selection_background(self, painter, rect_local):
        # 素のCompositionMode_Clearで抜くと下の静止画ごと消えて穴が空く。
        # 選択範囲だけ静止画を描き直して、減光していない状態に戻す。
        painter.drawImage(rect_local, self._image, self._to_device(rect_local))

    def _to_device(self, rect_local: QRect) -> QRect:
        """オーバーレイ内のローカル座標(Qt論理座標)を、凍結画像の物理ピクセル座標へ直す。"""
        return QRect(
            int(rect_local.x() * self._dpr),
            int(rect_local.y() * self._dpr),
            int(rect_local.width() * self._dpr),
            int(rect_local.height() * self._dpr),
        )

    def crop(self, rect_global: QRect) -> QImage:
        """凍結画像から rect_global(Qt論理座標のグローバル座標)の範囲を切り出す。
        オーバーレイの左上は仮想デスクトップの左上なので、原点を引いてから物理ピクセルへ直す。"""
        rect_device = self._to_device(rect_global.translated(-self._origin))
        # 画面構成の端数などで画像からはみ出しうる。はみ出した領域は未定義になるためクランプする。
        rect_device = rect_device.intersected(self._image.rect())
        if rect_device.isEmpty():
            rect_device = QRect(0, 0, 1, 1)

        cropped = self._image.copy(rect_device)
        # 付箋ウインドウは論理サイズの算出にdevicePixelRatioを使う。設定し直さないと
        # 高DPI環境で2倍の大きさのウインドウが開いてしまう。
        cropped.setDevicePixelRatio(self._dpr)
        return cropped


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
