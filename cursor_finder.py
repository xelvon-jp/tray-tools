# cursor_finder.py
# カーソルファインダー。ホットキーや縦ランチャから呼ぶと、マウスカーソルの
# 位置に波紋の輪を広げて「カーソルはここ」と知らせる。モニタが複数あると
# 見失いがちなので、そのための目印。
#
# 窓はカーソルのある画面いっぱいの透過オーバーレイで、マウスもキーも一切
# 受け取らない(presenter_overlay.py の透過窓と同じ作法)。輪はアニメーション
# の間、今のカーソル位置に付いて回る(動かしても目印が付いてくる)。
from PySide6.QtCore import QElapsedTimer, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

DURATION_MS = 1000
RING_COUNT = 3
RING_STAGGER_MS = 140
MAX_RADIUS = 150.0
MIN_RADIUS = 8.0
RING_COLOR = QColor(255, 106, 0)
TIMER_MS = 16


def ripple_state(elapsed_ms, duration_ms=DURATION_MS):
    """経過時間から (半径, 濃さ 0..1) の並びを作る(純関数)。"""
    states = []
    for i in range(RING_COUNT):
        start = i * RING_STAGGER_MS
        span = duration_ms - start
        if span <= 0:
            continue
        t = (elapsed_ms - start) / span
        if t < 0 or t >= 1:
            continue
        eased = 1.0 - (1.0 - t) ** 2                    # easeOutQuad
        radius = MIN_RADIUS + (MAX_RADIUS - MIN_RADIUS) * eased
        alpha = (1.0 - t) ** 1.5
        states.append((radius, alpha))
    return states


def fade_alpha(elapsed_ms, duration_ms=DURATION_MS):
    """中心の点の濃さ。最後にふっと消える。"""
    t = min(max(elapsed_ms / duration_ms, 0.0), 1.0)
    return (1.0 - t) ** 2


class CursorFinderOverlay(QWidget):
    """カーソルのある画面いっぱいに広がる、透過の目印窓。"""

    def __init__(self):
        super().__init__()
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._origin = QPoint(0, 0)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        """カーソルのある画面へ置き直して、アニメーションを最初から出す。
        出ている間に呼ばれたら、そこから出し直す。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) \
            or QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = screen.geometry()
            self._origin = geometry.topLeft()
            self.setGeometry(geometry)
        self._clock.restart()
        self._timer.start(TIMER_MS)
        self.show()
        self.raise_()
        self.update()

    def _tick(self) -> None:
        if self._clock.elapsed() >= DURATION_MS:
            self._timer.stop()
            self.hide()
            return
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt の名前)
        elapsed = self._clock.elapsed() if self._clock.isValid() else DURATION_MS
        center = QCursor.pos() - self._origin
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for radius, alpha in ripple_state(elapsed):
            color = QColor(RING_COLOR)
            color.setAlphaF(max(0.0, min(alpha * 0.9, 1.0)))
            painter.setPen(QPen(color, 3))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius, radius)
        dot = QColor(RING_COLOR)
        dot.setAlphaF(fade_alpha(elapsed))
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(center, 4, 4)
        painter.end()
