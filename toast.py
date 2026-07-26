# toast.py
# 自前描画の簡易通知。Windows 11 のトースト通知は通知システムを経由するため数秒遅れて出る
# ことがあり、操作し終わった後になって鳴るだけで邪魔になる。ここでは即座に出て自分で消える
# 小さなパネルを描く(見た目は color_picker の情報パネル・CountdownOverlay に揃えてある)。
from PySide6.QtCore import Qt, QPropertyAnimation, QRect, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

VISIBLE_MS = 1600
FADE_MS = 250
MAX_WIDTH = 420
PADDING_X = 16
PADDING_Y = 10
SCREEN_MARGIN = 24
RADIUS = 10

# 表示中のトースト。ローカル変数だけで持つとGCされて途中で消えるため、モジュール側で保持する。
_current = None


class Toast(QWidget):
    """枠なし・最前面・クリック透過の通知パネル。一定時間で自分をフェードアウトして閉じる。"""

    def __init__(self, text: str):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # 入力中に出ると文字が飛ぶ事故になるため、フォーカスは絶対に奪わない
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # 通知の下にあるものをクリックできないと操作の邪魔になる
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._text = text
        self._font = QFont("Meiryo", 10)
        self._fade = None

        metrics = QFontMetrics(self._font)
        bounds = metrics.boundingRect(QRect(0, 0, MAX_WIDTH, 0), Qt.TextWordWrap, text)
        self.resize(bounds.width() + PADDING_X * 2, bounds.height() + PADDING_Y * 2)
        self._move_to_corner()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self._timer.start(VISIBLE_MS)

    def _move_to_corner(self):
        """カーソルのある画面の右下に出す。primaryScreen固定だとマルチモニタで
        今見ていない画面に出てしまい、気付けないことがある。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - SCREEN_MARGIN,
            area.bottom() - self.height() - SCREEN_MARGIN,
        )

    def _fade_out(self):
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self._finish)
        self._fade.start()

    def _finish(self):
        self.close()
        _forget(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 20, 20, 230))
        painter.drawRoundedRect(self.rect(), RADIUS, RADIUS)

        painter.setPen(QColor(255, 255, 255))
        painter.setFont(self._font)
        painter.drawText(
            self.rect().adjusted(PADDING_X, PADDING_Y, -PADDING_X, -PADDING_Y),
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignVCenter,
            self._text,
        )


def _forget(toast) -> None:
    global _current
    if _current is toast:
        _current = None
        toast.deleteLater()


def dismiss() -> None:
    """表示中のトーストがあれば即座に消す。"""
    global _current
    if _current is not None:
        # deleteLaterは即座には消さない。その隙に消灯タイマーが発火して、破棄待ちの
        # ウィジェットにアニメーションを張るのを避けるため、先に止めておく。
        _current._timer.stop()
        _current.close()
        _current.deleteLater()
        _current = None


def show_toast(text: str) -> None:
    """即時表示の通知を出す。短時間に連続して呼ばれても積み上がらず、常に最新の1枚だけ残る。"""
    global _current
    dismiss()
    _current = Toast(text)
    _current.show()
