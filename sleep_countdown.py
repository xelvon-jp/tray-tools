# sleep_countdown.py
# スリープ・休止の予約が近づいたときに、画面の右下へ出すカウントダウンの窓。
#
# トースト(toast.py)ではなく専用の窓にしたのは、押せるボタンが要るため。あちらは
# 数秒で自分から消える読み物で、掴む場所を持たない。こちらは「予約を忘れて作業して
# いる最中に落ちる」のを防ぐのが仕事なので、気づいたその場で止められる必要がある。
#
# 出す場所を右下に固定するのは、通知の出る場所としてWindowsが使っている隅で、
# 作業中の視線からいちばん遠いため。中央に出すと、それ自体が作業の邪魔になる。
import sys

from PySide6.QtCore import Qt, QRect, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter
from PySide6.QtWidgets import QPushButton, QWidget

# 窓の大きさと、画面の隅からの余白(論理px)。
WIDTH = 300
HEIGHT = 108
SCREEN_MARGIN = 24
PADDING = 14
BUTTON_WIDTH = 96
BUTTON_HEIGHT = 30

# 残りがこれを切ったら数字を赤くする(秒)。押すなら今、を目で分かるようにする。
URGENT_SECONDS = 30

BACKGROUND = "#1f1f1f"
FOREGROUND = "#ffffff"
BORDER = "#5a5a5a"
URGENT = "#ff5252"


def _guard(where: str) -> None:
    """スロットの中で起きた例外をここで止める。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。1秒ごとに走る
    タイマーなので、1回の失敗で常駐アプリごと消えては割に合わない。"""
    print(f"[tray-tools] スリープの予告: {where}に失敗しました", file=sys.stderr)


class SleepCountdown(QWidget):
    """あと何秒で寝るかを数えて見せ、その場で取り消せる窓。

    on_cancel は「取り消す」を押したときに呼ばれる。窓を閉じるのは呼ばれた側の
    責任にしてある(予約の状態を持っているのはあちらなので、そちらから閉じるほうが
    順序が狂わない)。"""

    def __init__(self, on_cancel, label: str = "スリープ"):
        super().__init__()
        self._on_cancel = on_cancel
        self._label = label
        self._seconds = 0
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus  # 作業中の入力を奪わない
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.resize(WIDTH, HEIGHT)

        self._button = QPushButton("取り消す", self)
        self._button.setCursor(QCursor(Qt.PointingHandCursor))
        self._button.setFocusPolicy(Qt.NoFocus)
        self._button.setGeometry(
            WIDTH - PADDING - BUTTON_WIDTH,
            HEIGHT - PADDING - BUTTON_HEIGHT,
            BUTTON_WIDTH,
            BUTTON_HEIGHT,
        )
        self._button.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: 1px solid %s;"
            " border-radius: 4px; }"
            "QPushButton:hover { background: %s; }"
            % (BACKGROUND, FOREGROUND, BORDER, "#333333")
        )
        self._button.clicked.connect(self._cancel)

        # 1秒ごとに数字を書き換える。残り時間そのものは呼ぶ側が持っていて、
        # こちらは表示するだけ(2か所で数えると必ずズレる)。
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(1000)

    def _cancel(self):
        try:
            if self._on_cancel is not None:
                self._on_cancel()
        except Exception:
            _guard("取り消し")

    def show_for(self, seconds: int, label: str) -> None:
        """残り seconds 秒として、画面の右下に出す。"""
        try:
            self._seconds = max(0, int(seconds))
            self._label = label
            self.setGeometry(self._place())
            self.show()
            self.raise_()
        except Exception:
            _guard("表示")

    def set_seconds(self, seconds: int) -> None:
        """残り時間を更新する。数えているのは呼ぶ側。"""
        self._seconds = max(0, int(seconds))
        self.update()

    def _place(self) -> QRect:
        """カーソルのある画面の右下。作業領域の中に置く(タスクバーに潜らせない)。"""
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        return QRect(
            area.right() + 1 - WIDTH - SCREEN_MARGIN,
            area.bottom() + 1 - HEIGHT - SCREEN_MARGIN,
            WIDTH,
            HEIGHT,
        )

    def paintEvent(self, event):
        try:
            painter = QPainter(self)
            painter.fillRect(self.rect(), QColor(BACKGROUND))
            painter.setPen(QColor(BORDER))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

            font = QFont(self.font())
            font.setPixelSize(13)
            painter.setFont(font)
            painter.setPen(QColor(FOREGROUND))
            painter.drawText(
                QRect(PADDING, PADDING, WIDTH - PADDING * 2, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"まもなく{self._label}にします",
            )

            # 残り時間。押すなら今、が分かるよう最後の30秒は赤くする。
            font.setPixelSize(30)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(URGENT if self._seconds <= URGENT_SECONDS else FOREGROUND))
            painter.drawText(
                QRect(PADDING, PADDING + 22, WIDTH - PADDING * 2, 40),
                Qt.AlignLeft | Qt.AlignVCenter,
                _format(self._seconds),
            )
        except Exception:
            _guard("描画")


def _format(seconds: int) -> str:
    """残り秒を「2:05」「0:18」のように出す。"""
    seconds = max(0, int(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)
