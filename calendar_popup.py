# calendar_popup.py
# タスクバーウィジェットの縦ランチャ(taskbar_launcher.py)から出す、小さなカレンダー。
# 見る専用で、予定の連携や祝日は持たない。
#
# 欲しいのは「今月は何日が何曜日か」を一目で確かめることだけなので、中身は Qt の
# QCalendarWidget をそのまま使う。色付けも土曜=青・日曜=赤だけ足す。
#
# Windows 標準の時計フライアウトを開く手は採らなかった。外のプロセスから開く公式の
# 手段が無いし、そもそもサブ画面ではウィジェット自身が時計に化けていて、本物の時計が
# そこに無い。出す画面ごとに確実に開ける、自前の小窓にする。
#
# 閉じ方は3つ。Esc / 別の窓へフォーカスが移る / ランチャの項目をもう一度押す
# (トグルは feature_screen.show_calendar 側)。日付を押しても何も起きない(選択が動く
# だけ)。窓1つにつき1モジュール、の慣例でここに分けてある。
from PySide6.QtCore import QDate, QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QCursor, QGuiApplication, QTextCharFormat
from PySide6.QtWidgets import QApplication, QCalendarWidget, QVBoxLayout, QWidget

# フォーカスを失ってから閉じるまでの猶予(ms)。月を選ぶメニューなど、自分のポップアップが
# 出ている間も非アクティブ化は来るので、即閉じにするとそれらが使えなくなる。
DEACTIVATE_CLOSE_MS = 150

_MARGIN = 8


class CalendarPopup(QWidget):
    """カレンダーの小窓。閉じたら closed が出る(参照の始末は持ち主の役目)。"""

    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("カレンダー")
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self._calendar = QCalendarWidget(self)
        self._calendar.setFirstDayOfWeek(Qt.Sunday)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self._calendar.setSelectedDate(QDate.currentDate())
        saturday = QTextCharFormat()
        saturday.setForeground(QBrush(QColor("#2563eb")))
        sunday = QTextCharFormat()
        sunday.setForeground(QBrush(QColor("#dc2626")))
        self._calendar.setWeekdayTextFormat(Qt.Saturday, saturday)
        self._calendar.setWeekdayTextFormat(Qt.Sunday, sunday)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._calendar)

        self._deactivate_timer = QTimer(self)
        self._deactivate_timer.setSingleShot(True)
        self._deactivate_timer.timeout.connect(self._on_deactivate_timeout)
        # 無効化されたままかどうかは、イベントの来た順で自分で持つ。Qt の有効化状態は
        # 表示直後の対のイベントや環境差で実態とずれることがあるため、そちらは見ない。
        self._deactivated = False

    # ---------------------------------------------------------------
    # 表示
    # ---------------------------------------------------------------
    def show_at_cursor_screen(self) -> None:
        """カーソルのある画面の右下(タスクバーを除いた領域)に出す。

        時計のフライアウトと同じ場所感にする。タスクバーが下以外の辺にあっても、
        availableGeometry がタスクバーを除いた矩形を返すので、その右下で合う。"""
        self.adjustSize()
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

    # ---------------------------------------------------------------
    # 閉じる条件
    # ---------------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self._deactivated = True
            self._deactivate_timer.start(DEACTIVATE_CLOSE_MS)
        elif e.type() == QEvent.WindowActivate:
            self._deactivated = False
        return super().event(e)

    def _on_deactivate_timeout(self) -> None:
        # 猶予の間にフォーカスが戻っていたら閉じない。表示直後は有効化と無効化が
        # 対で来ることがあり、無効化の瞬間に即閉じにすると表示自体が成立しない。
        # 月・年のメニューなど自分のポップアップが出ている間も閉じない(選んだ後に
        # フォーカスが戻らなければ、次の無効化で閉じる)。
        if not self._deactivated:
            return
        if QApplication.activePopupWidget() is not None:
            return
        self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


def create_popup() -> CalendarPopup:
    """カレンダーの小窓を作る。出す位置まで決めるのは呼び出し側(show_at_cursor_screen)。"""
    return CalendarPopup()
