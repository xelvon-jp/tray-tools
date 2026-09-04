# status_overlay_demo.py
# Copilot ステータス札の見た目・位置・点滅を、tray-tools に実装する前に確かめるデモ。
#
# 何をするか
# ----------
# 実物の Copilot の窓を探して、そこに出るはずの場所へ札を出す。中身は本物の状態では
# なく、4状態 + 30秒超過の警告(点滅)を順番に回すだけ。窓をドラッグすれば札も追う。
#
# 使い方
#   python tools\status_overlay_demo.py            既定(1状態あたり3秒)
#   python tools\status_overlay_demo.py --hold 5   ゆっくり回す
#   python tools\status_overlay_demo.py --state waiting_user   1つに固定して見る
#   終わるときは Ctrl+C か、コンソールの窓を閉じる
#
# ここで確かめたいこと
#   1. 札の背景がちゃんと塗られるか(スタイルシートに頼らず paintEvent で描く)
#   2. 入力ボックスに重ならない位置か(窓の下端基準にしてある)
#   3. 点滅が遠目で分かるか、かつ本文が読めるままか
#
# 位置の決め方
# ------------
# 縦は窓の下端から一定量だけ浮かせる。入力欄の UIA 矩形(userInput)は高さ22pxしか
# 無く、見た目の入力ボックスよりずっと小さいので、そこを基準にすると重なる。
# 横は入力欄の右端に揃える。窓の右端に貼り付けるより、入力ボックスと縦線が揃って
# 落ち着いて見えるため。入力欄の位置は窓に対して一定なので、窓の右端からの差
# として覚えておき、窓が動いたらそのまま使い回す。
#
# 追従のしかた
# ------------
# 窓の矩形は Win32 の GetWindowRect で取る(UIA を経由しない)。呼び出しは軽いので
# 150ms ごとに叩いても平気で、ドラッグにぬるりと付いてくる。入力欄の位置だけは
# UIA が要る(重い)ので、そちらは数秒に1度だけ取り直す。
import argparse
import ctypes
import os
import sys
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt, QRectF, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import capture_grab  # noqa: E402
import copilot_loop  # noqa: E402

PILL_WIDTH = 290
PILL_HEIGHT = 40
# 窓の下端から札の下端までの浮かせ量(論理px)。
PILL_GAP_BOTTOM = 12

FLASH_INTERVAL_MS = 550
FLASH_BORDER_PX = 16

# 窓を追う周期。GetWindowRect だけなので短くてよい。
FOLLOW_INTERVAL_MS = 150
# 入力欄の位置を取り直す周期。UIA の走査が要るので控えめに。
ANCHOR_INTERVAL_MS = 3000

# 状態キー → (絵文字, 文言, 背景色, 経過秒を出すか)
# 背景色は白文字とのコントラスト比が 4.5 以上になるものを選んである
# (Copilot がライトモードでも札の中は必ずこの色なので、白文字が読める)。
STATES = {
    "typing":       ("✏",        "ユーザ入力中",       QColor(168, 95, 0),   False),
    "waiting_ai":   ("⏳",        "応答待ち",           QColor(179, 40, 40),  True),
    "responding":   ("\U0001f4ac",    "応答中",             QColor(31, 107, 176), False),
    "waiting_user": ("\U0001f642",    "応答済（入力待ち）", QColor(31, 138, 76),  True),
}
# 閾値を超えたときの見た目。札もこれに変わるので、点滅を見逃しても札で分かる。
ALERT = ("\U0001f514", "応答済（入力待ち）", QColor(211, 32, 32), True)

# デモで回す順番。最後の alert だけ窓全体の点滅が付く。
DEMO_ORDER = ["typing", "waiting_ai", "responding", "waiting_user", "alert"]

user32 = ctypes.windll.user32
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = ctypes.c_bool
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.IsIconic.restype = ctypes.c_bool
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = ctypes.c_bool
user32.IsWindow.argtypes = [ctypes.c_void_p]
user32.IsWindow.restype = ctypes.c_bool


def window_rect(hwnd):
    """窓の外接矩形を Qt の論理 QRect で返す。最小化・非表示なら None。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        return None
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    if r.right <= r.left or r.bottom <= r.top:
        return None
    return capture_grab.device_bounds_to_logical((r.left, r.top, r.right, r.bottom))


class StatusPill(QWidget):
    """ステータス札。中身は全部 paintEvent で描く。

    スタイルシート(setStyleSheet)は使わない。QWidget を継承した窓には背景と枠が
    描かれず、中の QLabel の文字だけが宙に浮く(実機でこれを踏んだ)。自分で描けば
    その差が原理的に無くなるうえ、角丸も点滅も同じ場所で面倒を見られる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot ステータス(デモ)")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.resize(PILL_WIDTH, PILL_HEIGHT)

        self._emoji = ""
        self._label = ""
        self._elapsed = ""
        self._fill = QColor(90, 90, 90)

        self._emoji_font = QFont("Segoe UI Emoji", 11)
        self._label_font = QFont("Meiryo", 11, QFont.Bold)
        self._elapsed_font = QFont("Meiryo", 9)

    def apply_state(self, key, elapsed_seconds=None):
        emoji, label, color, wants_elapsed = ALERT if key == "alert" else STATES[key]
        self._emoji = emoji
        self._label = label
        self._fill = color
        self._elapsed = (
            f"{int(elapsed_seconds)}秒"
            if wants_elapsed and elapsed_seconds is not None else ""
        )
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(self._fill)
        p.setPen(QPen(QColor(255, 255, 255, 110), 1.0))
        p.drawRoundedRect(box, 9, 9)

        p.setPen(QColor(255, 255, 255))
        p.setFont(self._emoji_font)
        p.drawText(box.adjusted(13, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self._emoji)
        p.setFont(self._label_font)
        p.drawText(box.adjusted(40, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self._label)
        if self._elapsed:
            p.setPen(QColor(255, 255, 255, 210))
            p.setFont(self._elapsed_font)
            p.drawText(box.adjusted(0, 0, -13, 0),
                       Qt.AlignVCenter | Qt.AlignRight, self._elapsed)


class FlashFrame(QWidget):
    """窓全体に重ねる点滅枠。塗り潰さないので本文は読めたまま。

    マウスもキーも透過するので、点滅している最中も Copilot をそのまま触れる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot 点滅(デモ)")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
            | Qt.WindowTransparentForInput
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._bright = True
        self._timer = QTimer()
        self._timer.setInterval(FLASH_INTERVAL_MS)
        self._timer.timeout.connect(self._blink)

    def _blink(self):
        self._bright = not self._bright
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        if self._bright:
            edge, fill = QColor(255, 45, 45, 240), QColor(255, 45, 45, 45)
        else:
            edge, fill = QColor(255, 45, 45, 40), QColor(255, 45, 45, 0)
        half = FLASH_BORDER_PX / 2.0
        box = QRectF(self.rect()).adjusted(half, half, -half, -half)
        p.setPen(QPen(edge, FLASH_BORDER_PX))
        p.setBrush(fill)
        p.drawRect(box)

    def start(self):
        if not self._timer.isActive():
            self._bright = True
            self._timer.start()
        if not self.isVisible():
            self.show()
            self.raise_()

    def stop(self):
        self._timer.stop()
        self.hide()


class Demo:
    def __init__(self, hold_seconds, fixed_state):
        self.hold_seconds = hold_seconds
        self.fixed_state = fixed_state
        self.pill = StatusPill()
        self.flash = FlashFrame()

        self.copilot = None
        self.hwnd = None
        # 入力欄の右端が、窓の右端から何px内側にあるか。窓が動いても変わらない。
        self.right_inset = 170
        self.index = 0
        self.elapsed = 0

        self.follow_timer = QTimer()
        self.follow_timer.setInterval(FOLLOW_INTERVAL_MS)
        self.follow_timer.timeout.connect(self._guarded(self._follow))
        self.anchor_timer = QTimer()
        self.anchor_timer.setInterval(ANCHOR_INTERVAL_MS)
        self.anchor_timer.timeout.connect(self._guarded(self._refresh_anchor))
        self.step_timer = QTimer()
        self.step_timer.setInterval(int(hold_seconds * 1000))
        self.step_timer.timeout.connect(self._guarded(self._next_state))
        self.tick_timer = QTimer()
        self.tick_timer.setInterval(1000)
        self.tick_timer.timeout.connect(self._guarded(self._tick_elapsed))

    @staticmethod
    def _guarded(fn):
        """PySide6 はスロットで例外を投げ切るとプロセスごと落ちる。全部ここで受ける。"""
        def wrapped():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                print(f"[demo] {fn.__name__} 失敗: {e}")
        return wrapped

    def start(self):
        if not self._attach():
            print("Copilot の窓が見つかりません。Copilot を起動してから実行してください。")
            return False
        self._refresh_anchor()
        self._apply(self._current_key())
        self.follow_timer.start()
        self.anchor_timer.start()
        self.tick_timer.start()
        if self.fixed_state is None:
            self.step_timer.start()
            order = " → ".join(DEMO_ORDER)
            print(f"デモ開始: {order} を {self.hold_seconds} 秒ずつ繰り返します。")
        else:
            print(f"デモ開始: {self.fixed_state} に固定して表示します。")
        print("Copilot の窓をドラッグすると札が追いかけます。Ctrl+C で終了。")
        return True

    def _attach(self):
        try:
            self.copilot = copilot_loop.Copilot()
        except RuntimeError:
            return False
        self.hwnd = self.copilot.hwnd_main
        return True

    def _current_key(self):
        return self.fixed_state or DEMO_ORDER[self.index]

    def _refresh_anchor(self):
        """入力欄の右端が窓の右端からどれだけ内側かを測り直す。

        取れなくても既定値のまま続ける。ここで失敗するのは Copilot が描き替えている
        最中などの一時的なもので、次の周期で取れる。"""
        if self.copilot is None:
            return
        try:
            snap = self.copilot.status_snapshot()
        except Exception:  # noqa: BLE001
            # 窓を掴み直せば直ることが多い(Copilot の再起動など)。
            self.copilot = None
            self._attach()
            return
        win = snap.get("window_rect")
        inp = snap.get("input_rect")
        if not win or not inp:
            return
        win_q = capture_grab.device_bounds_to_logical(win)
        inp_q = capture_grab.device_bounds_to_logical(inp)
        inset = win_q.right() - inp_q.right()
        # 桁が明らかにおかしい値は捨てる(描き替え中に潰れた矩形を拾うことがある)。
        if 0 <= inset < win_q.width() // 2:
            self.right_inset = inset

    def _follow(self):
        rect = window_rect(self.hwnd)
        if rect is None:
            # 最小化・窓が消えた。掴み直しを試みつつ、いまは畳む。
            self.pill.hide()
            self.flash.stop()
            if self.hwnd and not user32.IsWindow(self.hwnd):
                self.copilot = None
                self._attach()
            return

        x = rect.right() - self.right_inset - PILL_WIDTH
        y = rect.bottom() - PILL_HEIGHT - PILL_GAP_BOTTOM
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.geometry()
            x = max(area.left(), min(x, area.right() - PILL_WIDTH))
            y = max(area.top(), min(y, area.bottom() - PILL_HEIGHT))
        self.pill.move(int(x), int(y))
        # show / raise_ は出ていないときだけ。150ms ごとに叩くとドラッグ中に
        # 札がちらつき、他の最前面の窓とも押し上げ合いになる。
        if not self.pill.isVisible():
            self.pill.show()
            self.pill.raise_()

        if self._current_key() == "alert":
            self.flash.setGeometry(rect)
            self.flash.start()
        else:
            self.flash.stop()

    def _apply(self, key):
        self.elapsed = 45 if key == "alert" else 0
        self.pill.apply_state(key, self.elapsed)
        print(f"  → {key}")

    def _next_state(self):
        self.index = (self.index + 1) % len(DEMO_ORDER)
        self._apply(DEMO_ORDER[self.index])

    def _tick_elapsed(self):
        """経過秒が動いて見えるように、1秒ごとに数字を進める。"""
        self.elapsed += 1
        self.pill.apply_state(self._current_key(), self.elapsed)


def main():
    parser = argparse.ArgumentParser(
        description="Copilot ステータス札のデモ(実装前の見た目確認用)")
    parser.add_argument("--hold", type=float, default=3.0,
                        help="1状態あたりの表示秒数(既定 3)")
    parser.add_argument("--state", choices=DEMO_ORDER, default=None,
                        help="1つの状態に固定して表示する")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    app = QApplication(sys.argv)
    # 札を閉じてもアプリが終わらないように(札は show/hide を繰り返す)。
    app.setQuitOnLastWindowClosed(False)

    demo = Demo(args.hold, args.state)
    if not demo.start():
        return 1
    # Ctrl+C を Qt のイベントループ中に効かせる。これが無いと反応しない。
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    wake = QTimer()
    wake.start(200)
    wake.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
