# copilot_status_process.py
# Copilot の「いま誰の手番か」を、Copilot の窓の下端に常時オーバーレイで出す係。
# **常駐(main.py)とは別のプロセスとして動く。**
#
# なぜ別プロセスなのか（この機能の設計で最も重要な点）
# ----------------------------------------------------
# **UI Automation(comtypes) と pycaw(comtypes) を同じプロセスに同居させると、
# GC が走った拍子にプロセスごと即死する。** 実測したクラッシュ率:
#
#   Qt + UIA（pycaw なし）      0/10
#   Qt + pycaw + UIA            5/10
#   UIA を先に読む              4/10
#   pycaw を先に読む            3/10
#
# 読み込む順番では避けられない。落ちるのは GC が comtypes の __del__ → Release を
# 呼んだ瞬間で、落ちる場所はトレイアイコンの描画・ピッカーの採寸・設定の保存と
# 毎回ばらばら。「たまたま GC が走ったところ」でしかないので、症状から原因に
# 辿り着けない。しかも 0xC0000005 なので Python の例外にならず error.log に何も残らない。
#
# 常駐は音声出力の切り替え(pycaw)を持っている。あちらは外せない。だから
# **UIA を使う側を常駐から追い出す**。付箋(Rapture)を capture_process.py へ
# 追い出したのと同じ判断で、あのときの理由(本体の寿命に巻き込まれない)に加えて、
# ここでは「同居させると落ちる」というもっと強い理由がある。
#
# このプロセスが持つもの / 持たないもの
# ------------------------------------
#   持つ:     UIA(copilot_loop)、オーバーレイの窓、状態の判定
#   持たない: pycaw、トレイアイコン、ホットキー、設定の書き込み
# 設定は起動時に読むだけ。書くのは常駐側(トレイのチェック)の仕事。
#
# 4つの状態
# ---------
#   ✏ ユーザ入力中        入力欄に文字がある(送信前)
#   ⏳ 応答待ち            送信済みで、Copilot がまだ喋り始めていない
#   💬 応答中              Copilot が生成中(停止ボタンが出ている)
#   🙂 応答済（入力待ち）  Copilot が喋り終わって、こちらの番
#
# copilot_loop.state() が返すのは busy/ready/idle の3つで、「送信直後の idle」と
# 「応答が終わった後の idle」が区別できない。直前に見た状態を覚えておいて、
# 入力中→idle なら応答待ち、応答中→idle なら入力待ち、と手番を決めている。
#
# 点滅は「応答待ち」と「応答済(入力待ち)」の両方で出す。どちらも人が待っている
# 状態で、放置されて困るのは同じだから。入力中と応答中は待っている人が居ないので出さない。
#
# 描画をスタイルシートでやらない理由
# ----------------------------------
# QWidget を継承した窓は setStyleSheet の背景と枠が描かれず、中の QLabel の文字だけが
# 宙に浮く。実機でこれを踏んで「白い Copilot 画面に白文字」で読めなかった。
# WA_StyledBackground を足しても半透明属性と噛み合わないので、paintEvent で自分で描く。
#
# 位置を窓の下端基準にする理由
# ----------------------------
# 入力欄の UIA 矩形は高さ22pxしかなく、見た目の入力ボックスよりずっと小さい。そこを
# 基準に「すぐ上」へ置くと入力ボックスに重なる。窓の下端から一定量だけ浮かせれば、
# 入力ボックスがどれだけ大きくても必ずその下に出る。横位置だけ入力欄の右端に合わせる。
import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from PySide6.QtCore import Qt, QPointF, QRect, QRectF, QTimer  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QFont, QGuiApplication, QPainter, QPen, QPolygonF,
)
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import capture_grab  # noqa: E402
import copilot_loop  # noqa: E402

# 状態を取り直す間隔(秒)。UIA の走査が要るので控えめに。
POLL_INTERVAL_SECONDS = 1.5
# 窓を追う間隔(ms)。GetWindowRect だけなので短くてよい。
FOLLOW_INTERVAL_MS = 150

DEFAULT_THRESHOLD_SECONDS = 30

PILL_WIDTH = 290
PILL_HEIGHT = 40
PILL_GAP_BOTTOM = 12
DEFAULT_RIGHT_INSET = 168

FLASH_INTERVAL_MS = 550
FLASH_BORDER_PX = 16

# 状態キー → (絵文字, 文言, 背景色, 経過秒を出すか)
# 背景色は白文字とのコントラスト比が 4.5 以上あるものを選んである。札の中は必ず
# この色で塗るので、Copilot がライトモードでも白文字が読める。
STATE_STYLES = {
    "typing": ("✏", "ユーザ入力中", QColor(168, 95, 0), False),
    "waiting_ai": ("⏳", "応答待ち", QColor(179, 40, 40), True),
    "responding": ("💬", "応答中", QColor(31, 107, 176), False),
    "waiting_user": ("🙂", "応答済（入力待ち）", QColor(31, 138, 76), True),
}
ALERT_EMOJI = "🔔"
ALERT_COLOR = QColor(211, 32, 32)

# 一時停止中の見た目。状態の色(橙/赤/青/緑)のどれとも違う灰色にして、
# 「いま見張っていない」ことが色だけで分かるようにする。
PAUSED_EMOJI = "⏸"
PAUSED_LABEL = "一時停止中"
PAUSED_COLOR = QColor(88, 94, 104)

# 札の右端に置く一時停止/再開ボタン。
BUTTON_SIZE = 26
BUTTON_MARGIN = 8

ALERTABLE_STATES = ("waiting_ai", "waiting_user")


def _to_logical(bounds):
    """Win32/UIA の (left, top, right, bottom)(物理px)を Qt の論理 QRect へ。"""
    if bounds is None:
        return None
    return capture_grab.device_bounds_to_logical(bounds)


class StatusPill(QWidget):
    """窓の下端に貼り付ける、常時表示のステータス札。右端に一時停止ボタンを持つ。

    【クリックを透過させない理由】
    札そのものは邪魔をしないよう透過させたいが、それだと右端のボタンも押せない。
    透過は窓ごとの設定で、一部だけ通すことができないため、札全体を「クリックを
    受け取る」側に倒してある。札は小さく(290x40)、入力ボックスの下の余白に置いて
    いるので、下にある Copilot の操作を奪う場面はほぼ無い。

    【それでもフォーカスは奪わない】
    WindowDoesNotAcceptFocus と WA_ShowWithoutActivating は残す。押しても Copilot の
    入力位置(キャレット)が飛ばないので、書きかけの文章を触っても続きから打てる。
    tray-tools が SetForegroundWindow を禁じているのと同じ考え方。"""

    def __init__(self, on_toggle_pause=None):
        super().__init__()
        self.setWindowTitle("Copilot ステータス")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(PILL_WIDTH, PILL_HEIGHT)
        # ホバーの明るさを出すために、押していなくてもマウスの位置を受け取る。
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

        self._on_toggle_pause = on_toggle_pause
        self._emoji = ""
        self._label = ""
        self._elapsed = ""
        self._fill = QColor(90, 90, 90)
        self._paused = False
        self._hover_button = False

        self._emoji_font = QFont("Segoe UI Emoji", 11)
        self._label_font = QFont("Meiryo", 11, QFont.Bold)
        self._elapsed_font = QFont("Meiryo", 9)

    # -- ボタンの当たり判定 ----------------------------------------------
    def _button_rect(self):
        size = BUTTON_SIZE
        return QRect(PILL_WIDTH - BUTTON_MARGIN - size,
                     (PILL_HEIGHT - size) // 2, size, size)

    def apply_state(self, state_key, elapsed_seconds=None, alert=False,
                    paused=False):
        if paused:
            emoji, label, color = PAUSED_EMOJI, PAUSED_LABEL, PAUSED_COLOR
            wants_elapsed = False
        else:
            emoji, label, color, wants_elapsed = STATE_STYLES[state_key]
            if alert:
                # 文言は状態のものを残す。何を待っているのか分からなくなるため。
                emoji, color, wants_elapsed = ALERT_EMOJI, ALERT_COLOR, True
        elapsed_text = (
            f"{int(elapsed_seconds)}秒"
            if wants_elapsed and elapsed_seconds is not None else ""
        )
        if (emoji, label, color.rgb(), elapsed_text, paused) == (
                self._emoji, self._label, self._fill.rgb(), self._elapsed,
                self._paused):
            return
        self._emoji, self._label, self._fill = emoji, label, color
        self._elapsed, self._paused = elapsed_text, paused
        self.update()

    # -- マウス ------------------------------------------------------------
    def mouseMoveEvent(self, event):
        hovering = self._button_rect().contains(event.position().toPoint())
        if hovering != self._hover_button:
            self._hover_button = hovering
            self.update()

    def leaveEvent(self, _event):
        if self._hover_button:
            self._hover_button = False
            self.update()

    def mousePressEvent(self, event):
        # ボタン以外を押したときは何もしない(閉じたり動かしたりできると事故る)。
        if (event.button() == Qt.LeftButton
                and self._button_rect().contains(event.position().toPoint())
                and self._on_toggle_pause is not None):
            self._on_toggle_pause()

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
            # 経過秒はボタンの左に寄せる。右端まで書くとボタンと重なる。
            right_pad = BUTTON_MARGIN + BUTTON_SIZE + 8
            p.drawText(box.adjusted(0, 0, -right_pad, 0),
                       Qt.AlignVCenter | Qt.AlignRight, self._elapsed)

        self._paint_button(p)

    def _paint_button(self, p):
        rect = QRectF(self._button_rect())
        p.setPen(Qt.NoPen)
        # 札の色に対して少し明るい面を置く。ホバーでさらに明るくして、押せることを示す。
        p.setBrush(QColor(255, 255, 255, 70 if self._hover_button else 38))
        p.drawRoundedRect(rect, 6, 6)
        p.setPen(QPen(QColor(255, 255, 255, 235), 2.0))
        cx, cy = rect.center().x(), rect.center().y()
        if self._paused:
            # ▶（再開）。線ではなく塗りの三角にする(細い線だと小さくて潰れる)。
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            tri = QPolygonF([
                QPointF(cx - 3.5, cy - 5.5),
                QPointF(cx - 3.5, cy + 5.5),
                QPointF(cx + 5.5, cy),
            ])
            p.drawPolygon(tri)
        else:
            # ⏸（一時停止）。縦棒2本。
            p.drawLine(QPointF(cx - 3.0, cy - 5.0), QPointF(cx - 3.0, cy + 5.0))
            p.drawLine(QPointF(cx + 3.0, cy - 5.0), QPointF(cx + 3.0, cy + 5.0))

    def place(self, window_rect, right_inset):
        """窓の下端に沿って置く。入力ボックスの大きさに関係なく必ずその下に来る。"""
        x = window_rect.right() - right_inset - self.width()
        y = window_rect.bottom() - self.height() - PILL_GAP_BOTTOM
        screen = (QGuiApplication.screenAt(window_rect.center())
                  or QGuiApplication.primaryScreen())
        if screen is not None:
            area = screen.geometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(int(x), int(y))


class FlashFrame(QWidget):
    """窓全体に重ねる点滅枠。塗り潰さないので本文は読めたまま。

    全面を濃く塗ると、気づいた瞬間に中身を確認できない。太い枠とごく薄い塗りに
    してある。マウスもキーも透過するので、点滅中も Copilot をそのまま触れる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot 待ち")
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
        self._timer.timeout.connect(self._on_blink)

    def _on_blink(self):
        try:
            self._bright = not self._bright
            self.update()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            print(f"[copilot-status] 点滅の描き替えに失敗: {e}")

    def paintEvent(self, _event):
        p = QPainter(self)
        if self._bright:
            edge, fill = QColor(255, 45, 45, 240), QColor(255, 45, 45, 45)
        else:
            edge, fill = QColor(255, 45, 45, 40), QColor(255, 45, 45, 0)
        half = FLASH_BORDER_PX / 2.0
        p.setPen(QPen(edge, FLASH_BORDER_PX))
        p.setBrush(fill)
        p.drawRect(QRectF(self.rect()).adjusted(half, half, -half, -half))

    def start(self, window_rect):
        self.setGeometry(window_rect)
        if not self._timer.isActive():
            self._bright = True
            self._timer.start()
        # show / raise_ は出ていないときだけ。毎回叩くと他の最前面の窓と
        # 押し上げ合いになってちらつく。
        if not self.isVisible():
            self.show()
            self.raise_()

    def stop(self):
        self._timer.stop()
        self.hide()


class StatusWatcher:
    """Copilot を見張って、札と点滅を出し入れする本体。"""

    def __init__(self, threshold_seconds, app_settings=None):
        self._threshold = threshold_seconds
        self._app_settings = app_settings

        self._state = None
        self._state_since = None
        # 直近に見た「手番がはっきりしている状態」。idle の意味を決めるのに使う。
        self._last_decisive = None

        self._copilot = None
        self._right_inset = DEFAULT_RIGHT_INSET
        # 一時停止は、そのとき限りの操作。設定に保存しない(次に開いたら見張っている
        # のが当たり前で、黙って止まったままのほうが事故になる)。
        self._paused = False

        self._pill = StatusPill(on_toggle_pause=self.toggle_pause)
        self._flash = FlashFrame()

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(int(POLL_INTERVAL_SECONDS * 1000))
        self._poll_timer.timeout.connect(self._on_poll)
        self._follow_timer = QTimer()
        self._follow_timer.setInterval(FOLLOW_INTERVAL_MS)
        self._follow_timer.timeout.connect(self._on_follow)

    def toggle_pause(self):
        """札のボタンから呼ばれる。見張りを止める/再開する。

        止めている間は UIA の走査をしない(点滅もしない)。窓の追従だけは続けるので、
        Copilot を動かしても札は付いてきて、そのまま再開ボタンを押せる。
        再開したときは、直前の手番の記憶を捨てて数え直す。止めている間に何往復か
        進んでいることがあり、古い記憶から続けると経過秒が嘘になるため。"""
        try:
            self._paused = not self._paused
            if self._paused:
                self._flash.stop()
            else:
                self._state = None
                self._state_since = None
                self._last_decisive = None
                self._on_poll()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            print(f"[copilot-status] 一時停止の切り替えに失敗: {e}")

    def start(self):
        self._poll_timer.start()
        self._follow_timer.start()
        self._on_poll()

    # -- 状態を取り直す(重い。1.5秒ごと) ---------------------------------
    def _on_poll(self):
        try:
            self._poll()
        except Exception as e:  # noqa: BLE001
            # スロット内で例外が抜けるとプロセスごと落ちる。ここで受ける。
            print(f"[copilot-status] 状態の取得に失敗: {e}")

    def _poll(self):
        if self._paused:
            # 掴んだ窓が無効になっていたら捨てるだけ。ここで掴み直すと UIA を
            # 触ることになるので、再開するまで何もしない。
            cp = self._copilot
            if cp is not None and not copilot_loop.user32.IsWindow(cp.hwnd_main):
                self._release()
            return

        snapshot = self._read_snapshot()
        if snapshot is None:
            # Copilot が居ない or 一時的に読めなかった。手番の記憶は残す
            # (一瞬読めなかっただけで応答待ちが入力待ちに化けると困る)。
            return

        state = self._classify(snapshot)
        if state != self._state:
            self._state = state
            self._state_since = time.time()

        window = _to_logical(snapshot.get("window_rect"))
        inp = _to_logical(snapshot.get("input_rect"))
        if window is not None and inp is not None:
            inset = window.right() - inp.right()
            # 桁が明らかにおかしい値は捨てる(描き替え中に潰れた矩形を拾うことがある)。
            if 0 <= inset < window.width() // 2:
                self._right_inset = inset

    # -- 窓を追う(軽い。150msごと) ---------------------------------------
    def _on_follow(self):
        try:
            self._follow()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-status] 追従に失敗: {e}")

    def _follow(self):
        if self._state is None and not self._paused:
            self._hide_all()
            return
        cp = self._copilot
        rect = _to_logical(
            copilot_loop.window_bounds(cp.hwnd_main) if cp else None)
        if rect is None:
            # 最小化・窓が消えた。次の poll で掴み直す。
            self._hide_all()
            return

        elapsed = time.time() - (self._state_since or time.time())
        alert = (not self._paused
                 and self._state in ALERTABLE_STATES
                 and elapsed >= self._threshold)

        # 停止中は state が None のこともある(掴み直す前など)。札は灰色の
        # 「一時停止中」になるので、状態のキーは何でもよい。
        self._pill.apply_state(self._state or "waiting_user", elapsed,
                               alert=alert, paused=self._paused)
        self._pill.place(rect, self._right_inset)
        if not self._pill.isVisible():
            self._pill.show()
            self._pill.raise_()

        if alert:
            self._flash.start(rect)
        else:
            self._flash.stop()

    # -- 判定 ------------------------------------------------------------
    def _classify(self, snapshot):
        """busy/ready/idle の3値と入力欄の中身から、4つの手番のどれかを決める。

        入力欄に文字があれば、送信ボタンが出ていようがいまいが「入力中」。
        state が 'ready' でも入力欄が空のことがあるので、文字の有無を先に見る。

        空の idle は、直前に何を見たかで意味が変わる:
          入力中 → 空idle : 送信した直後 → 応答待ち
          応答中 → 空idle : 喋り終わった → 入力待ち
        判断がつかない(起動した直後など)ときは入力待ち扱いにする。どちらも点滅する
        状態なので実害は小さく、「応答待ち」と言い切って Copilot が遅いように
        見せるより誤解が少ない。"""
        if snapshot.get("state") == "busy":
            self._last_decisive = "responding"
            return "responding"
        if (snapshot.get("input_text") or "").strip():
            self._last_decisive = "typing"
            return "typing"
        return "waiting_ai" if self._last_decisive == "typing" else "waiting_user"

    def _read_snapshot(self):
        """Copilot の状態一式を取る。Copilot が居ない・読めないなら None。

        掴んだ Copilot は使い回す。生成は窓の全列挙 + ツリー起こし + UIA クライアントの
        生成で、毎回やるには重い(実測 140ms、使い回すと 74ms)。窓が閉じた・アプリを
        起動し直したときは hwnd が無効になるので、そこで捨てて作り直す。"""
        cp = self._copilot
        if cp is not None and not copilot_loop.user32.IsWindow(cp.hwnd_main):
            self._release()
            cp = None
        if cp is None:
            try:
                cp = self._copilot = copilot_loop.Copilot(
                    app_settings=self._app_settings)
            except RuntimeError:
                return None
        try:
            return cp.status_snapshot()
        except Exception:  # noqa: BLE001
            # 窓を掴み直せば直ることが多い(アプリの再起動など)。次の poll で作り直す。
            self._release()
            return None

    def _release(self):
        cp, self._copilot = self._copilot, None
        if cp is not None:
            try:
                cp.close()
            except Exception:  # noqa: BLE001
                pass

    def _hide_all(self):
        try:
            self._flash.stop()
            self._pill.hide()
        except Exception:  # noqa: BLE001
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copilot の手番を常時表示する（常駐とは別プロセス）")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_SECONDS,
                        help="この秒数を超えて待ちが続いたら窓を点滅させる")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # pythonw.exe では stdout が無い

    app = QApplication(sys.argv)
    # 札は show/hide を繰り返すので、最後の窓が閉じても終わらせない。
    app.setQuitOnLastWindowClosed(False)

    # settings.json はプロファイルの上書きを読むためだけに使う。書き戻さない
    # (設定を書くのは常駐側の仕事。両方から書くと取り合いになる)。
    app_settings = {}
    try:
        import settings as settings_module
        app_settings = settings_module.load_settings(settings_module.SETTINGS_PATH)
    except Exception:  # noqa: BLE001
        pass

    watcher = StatusWatcher(max(5, args.threshold), app_settings)
    watcher.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
