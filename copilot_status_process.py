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
import ctypes
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
from PySide6.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

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

# 親(常駐)がまだ居るかを見る間隔(ms)。落ちたときに札が貼り付いたままになるのを防ぐ。
PARENT_CHECK_MS = 3000

# 点滅を何往復させたら止めるか。ずっと点滅していると視界の端でうるさく、
# しばらくすると脳が慣れて逆に気づかなくなる。数回で気を引いたら枠は消して、
# あとは札の色(警告色)だけで「まだ待っている」ことを示す。
FLASH_MAX_CYCLES = 5

# 最後に人がキーやマウスを触ってから、この秒数以内なら「操作中」とみなす。
# 操作中は入力待ちの経過を数えない(目の前で作業している人を急かさない)。
# 短すぎるとタイプの合間で解除され、長すぎると席を立ってもしばらく数え始めない。
INPUT_ACTIVE_SECONDS = 3.0

# 「監視を再開しますか？」の小窓。札のすぐ上に出す。
PROMPT_WIDTH = 250
PROMPT_HEIGHT = 92
PROMPT_BUTTON_W = 62
PROMPT_BUTTON_H = 26

# Ctrl+ドラッグで札を動かせる。Ctrl を要ることにしたのは、掴むつもりが無いのに
# 動かしてしまう事故を避けるため(札は入力欄のすぐ近くに置いてある)。
DRAG_MODIFIER = Qt.ControlModifier

ALERTABLE_STATES = ("waiting_ai", "waiting_user")


def _to_logical(bounds):
    """Win32/UIA の (left, top, right, bottom)(物理px)を Qt の論理 QRect へ。"""
    if bounds is None:
        return None
    return capture_grab.device_bounds_to_logical(bounds)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


_user32 = ctypes.windll.user32
_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = ctypes.c_bool
_kernel32 = ctypes.windll.kernel32
_kernel32.GetTickCount.restype = ctypes.c_uint


def seconds_since_input():
    """最後にキーかマウスが操作されてからの秒数。取れなければ None。

    【なぜフックを張らないか】
    tray-tools の作法として、キー/マウスをグローバルに捕捉する仕組みは事故の
    もとなので入れない(CLAUDE.md)。GetLastInputInfo は**捕捉ではなく照会**で、
    何が押されたかは一切分からず、最後に入力があった時刻だけが返る。見張りたい
    のは「人が operating しているか」だけなので、これで足りる。

    GetTickCount は約49日でひと回りする。回った直後は負になりうるので、
    負なら「たった今」として扱う(0を返す)。"""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    delta_ms = _kernel32.GetTickCount() - info.dwTime
    if delta_ms < 0:
        return 0.0
    return delta_ms / 1000.0


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

    def __init__(self, on_toggle_pause=None, on_quit=None,
                 on_toggle_pin=None, on_moved=None):
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
        self._on_quit = on_quit
        self._on_toggle_pin = on_toggle_pin
        self._on_moved = on_moved
        self._emoji = ""
        self._label = ""
        self._elapsed = ""
        self._fill = QColor(90, 90, 90)
        self._paused = False
        self._pinned = False
        # ホバー中のボタン名('pause' / 'pin' / None)。
        self._hover_button = None
        # Ctrl+ドラッグ中に、掴んだ点と窓の左上との差を覚えておく。
        self._drag_origin = None

        self._emoji_font = QFont("Segoe UI Emoji", 11)
        # ボタンの中に収める都合で、状態の絵文字より小さくする。
        self._pin_font = QFont("Segoe UI Emoji", 9)
        self._label_font = QFont("Meiryo", 11, QFont.Bold)
        self._elapsed_font = QFont("Meiryo", 9)

    # -- ボタンの当たり判定 ----------------------------------------------
    # 右端から「一時停止」「ピン留め」の順に並べる。よく押すほうを端に置く。
    def _button_rect(self, index=0):
        size = BUTTON_SIZE
        right = PILL_WIDTH - BUTTON_MARGIN - index * (size + 4)
        return QRect(right - size, (PILL_HEIGHT - size) // 2, size, size)

    def _pause_button_rect(self):
        return self._button_rect(0)

    def _pin_button_rect(self):
        return self._button_rect(1)

    def _hit_button(self, pos):
        """押された場所がどのボタンか。'pause' / 'pin' / None。"""
        if self._pause_button_rect().contains(pos):
            return "pause"
        if self._pin_button_rect().contains(pos):
            return "pin"
        return None

    def set_pinned(self, pinned: bool) -> None:
        if bool(pinned) != self._pinned:
            self._pinned = bool(pinned)
            self.update()

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
        if self._drag_origin is not None:
            # Ctrl+ドラッグ中。掴んだ点との差だけ動かす。
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            return
        hovering = self._hit_button(event.position().toPoint())
        if hovering != self._hover_button:
            self._hover_button = hovering
            self.update()

    def leaveEvent(self, _event):
        if self._hover_button:
            self._hover_button = None
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        # Ctrl を押しながらなら、どこを掴んでも移動。ボタンより先に見るので、
        # Ctrl+ボタンの位置で掴んでも一時停止が誤爆しない。
        if event.modifiers() & DRAG_MODIFIER:
            self._drag_origin = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            self.setCursor(Qt.SizeAllCursor)
            return
        hit = self._hit_button(pos)
        # ボタン以外を押したときは何もしない(閉じたり動かしたりできると事故る)。
        if hit == "pause" and self._on_toggle_pause is not None:
            self._on_toggle_pause()
        elif hit == "pin" and self._on_toggle_pin is not None:
            self._on_toggle_pin()

    def mouseReleaseEvent(self, _event):
        if self._drag_origin is None:
            return
        self._drag_origin = None
        self.setCursor(Qt.PointingHandCursor)
        # 自動配置に戻されないよう、置かれた場所を覚えてもらう。
        if self._on_moved is not None:
            self._on_moved(self.pos())

    def contextMenuEvent(self, event):
        """右クリックで、一時停止と終了を選ばせる。

        右クリックで即終了にしなかったのは、消してしまうとトレイのメニューまで
        戻らないと出し直せないため。誤クリック1回で消えるのは代償が大きい。"""
        menu = QMenu(self)
        pause = menu.addAction("▶ 監視を再開" if self._paused else "⏸ 監視を一時停止")
        menu.addSeparator()
        quit_action = menu.addAction("⏹ 状態監視バーを終了")
        chosen = menu.exec(event.globalPos())
        if chosen is pause and self._on_toggle_pause is not None:
            self._on_toggle_pause()
        elif chosen is quit_action and self._on_quit is not None:
            self._on_quit()

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
            # ボタンが2つ並ぶので、そのぶん左へ寄せる。寄せ足りないと数字が
            # ボタンの下に潜って読めなくなる。
            right_pad = BUTTON_MARGIN + BUTTON_SIZE * 2 + 4 + 8
            p.drawText(box.adjusted(0, 0, -right_pad, 0),
                       Qt.AlignVCenter | Qt.AlignRight, self._elapsed)

        self._paint_button(p)

    def _paint_button(self, p):
        self._paint_pause_button(p)
        self._paint_pin_button(p)

    def _button_face(self, p, rect, name, active=False):
        """ボタンの土台。押せることが分かる程度に明るくする。

        ピン留めのように「入っている/入っていない」があるものは、入っているときを
        さらに明るくして、ホバーと区別がつくようにする。"""
        p.setPen(Qt.NoPen)
        if active:
            alpha = 120 if self._hover_button == name else 95
        else:
            alpha = 70 if self._hover_button == name else 38
        p.setBrush(QColor(255, 255, 255, alpha))
        p.drawRoundedRect(rect, 6, 6)

    def _paint_pause_button(self, p):
        rect = QRectF(self._pause_button_rect())
        self._button_face(p, rect, "pause")
        cx, cy = rect.center().x(), rect.center().y()
        if self._paused:
            # ▶（再開）。線ではなく塗りの三角にする(細い線だと小さくて潰れる)。
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawPolygon(QPolygonF([
                QPointF(cx - 3.5, cy - 5.5),
                QPointF(cx - 3.5, cy + 5.5),
                QPointF(cx + 5.5, cy),
            ]))
        else:
            # ⏸（一時停止）。縦棒2本。
            p.setPen(QPen(QColor(255, 255, 255, 235), 2.0))
            p.drawLine(QPointF(cx - 3.0, cy - 5.0), QPointF(cx - 3.0, cy + 5.0))
            p.drawLine(QPointF(cx + 3.0, cy - 5.0), QPointF(cx + 3.0, cy + 5.0))

    def _paint_pin_button(self, p):
        """📌 ピン留め。入っていると、Copilot が引っ込んでも札を出したままにする。

        図形で画鋲を描いてみたが、26px では漏斗にしか見えなかった。札の他の記号
        (✏⏳💬🙂)と同じく絵文字に揃える。入/切は下地の明るさと、絵文字自体の
        濃さで示す。"""
        rect = QRectF(self._pin_button_rect())
        self._button_face(p, rect, "pin", active=self._pinned)
        p.save()
        p.setOpacity(1.0 if self._pinned else 0.5)
        p.setPen(QColor(255, 255, 255))
        p.setFont(self._pin_font)
        p.drawText(rect, Qt.AlignCenter, "📌")
        p.restore()

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
        self._cycles = 0
        # この待ちについて光り終えたか。待ちが解けるまで立てたままにする。
        self._done = False
        self._timer = QTimer()
        self._timer.setInterval(FLASH_INTERVAL_MS)
        self._timer.timeout.connect(self._on_blink)

    def _on_blink(self):
        try:
            self._bright = not self._bright
            if self._bright:
                # 暗→明で1往復ぶん数える。規定回数まで来たら枠ごと引っ込める。
                #
                # 【薄い赤を残さない理由】
                # 以前は明るい赤のまま置いていたが、画面の縁がずっと赤いのは
                # 単純に邪魔で、しかも見慣れると意味を持たなくなる。数回光って
                # 気を引くところまでが枠の仕事で、「まだ待っている」ことは札の
                # 色と経過秒が示し続ける。
                self._cycles += 1
                if self._cycles >= FLASH_MAX_CYCLES:
                    self.stop()
                    return
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
        # この待ちについては光り終えた。もう出さない。
        #
        # この印が無いと、消えた直後の tick で「出ていない」と判断して光り直し、
        # 永久に点滅し続けることになる(消す変更を入れた時点で必ず踏む)。
        # 印が消えるのは、待ちが解けて reset() が呼ばれたとき。
        if self._done:
            return
        self.setGeometry(window_rect)
        # 既に光っている最中なら、そのまま続ける。ここで数え直すと、窓を動かす
        # たびに点滅が最初からになってしまう。
        if not self.isVisible() and not self._timer.isActive():
            self._bright = True
            self._cycles = 0
            self._timer.start()
        # show / raise_ は出ていないときだけ。毎回叩くと他の最前面の窓と
        # 押し上げ合いになってちらつく。
        if not self.isVisible():
            self.show()
            self.raise_()

    def stop(self):
        """光るのをやめて引っ込める。光り終えた印は残す。"""
        self._timer.stop()
        self._done = True
        self.hide()

    def reset(self):
        """待ちが解けたときに呼ぶ。次の待ちでまた光れるように印を消す。"""
        self._timer.stop()
        self._cycles = 0
        self._done = False
        self.hide()


class ResumePrompt(QWidget):
    """「監視を再開しますか？」を尋ねる小さな窓。札のすぐ上に出す。

    【なぜ QMessageBox を使わないか】
    あれは前面に出てフォーカスを取る。Copilot に何か打っている最中に出たら、
    続きの文字が吸われる。この道具の全体方針(フォーカスを奪わない)に反するので、
    札と同じ作りの、フォーカスを取らない窓を自前で出す。"""

    def __init__(self, on_yes=None, on_no=None):
        super().__init__()
        self.setWindowTitle("Copilot 監視の再開")
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.resize(PROMPT_WIDTH, PROMPT_HEIGHT)

        self._on_yes = on_yes
        self._on_no = on_no
        self._hover = None
        self._label_font = QFont("Meiryo", 10, QFont.Bold)
        self._button_font = QFont("Meiryo", 9, QFont.Bold)

    def _yes_rect(self):
        return QRect(PROMPT_WIDTH - 8 - PROMPT_BUTTON_W * 2 - 6,
                     PROMPT_HEIGHT - 8 - PROMPT_BUTTON_H,
                     PROMPT_BUTTON_W, PROMPT_BUTTON_H)

    def _no_rect(self):
        return QRect(PROMPT_WIDTH - 8 - PROMPT_BUTTON_W,
                     PROMPT_HEIGHT - 8 - PROMPT_BUTTON_H,
                     PROMPT_BUTTON_W, PROMPT_BUTTON_H)

    def _hit(self, pos):
        if self._yes_rect().contains(pos):
            return "yes"
        if self._no_rect().contains(pos):
            return "no"
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.update()

    def leaveEvent(self, _event):
        if self._hover:
            self._hover = None
            self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        hit = self._hit(event.position().toPoint())
        if hit == "yes" and self._on_yes is not None:
            self._on_yes()
        elif hit == "no" and self._on_no is not None:
            self._on_no()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        box = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(31, 107, 176))
        p.setPen(QPen(QColor(255, 255, 255, 130), 1.0))
        p.drawRoundedRect(box, 9, 9)

        p.setPen(QColor(255, 255, 255))
        p.setFont(self._label_font)
        p.drawText(box.adjusted(13, 8, -13, 0),
                   Qt.AlignTop | Qt.AlignLeft,
                   "Copilot が応答中です。\n監視を再開しますか？")
        self._paint_button(p, self._yes_rect(), "yes", "はい")
        self._paint_button(p, self._no_rect(), "no", "いいえ")

    def _paint_button(self, p, rect, name, text):
        r = QRectF(rect)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 95 if self._hover == name else 55))
        p.drawRoundedRect(r, 5, 5)
        p.setPen(QColor(255, 255, 255, 245))
        p.setFont(self._button_font)
        p.drawText(r, Qt.AlignCenter, text)

    def show_above(self, pill_rect):
        """札のすぐ上に出す。画面から食み出さないよう寄せる。"""
        x = pill_rect.right() - self.width()
        y = pill_rect.top() - self.height() - 8
        screen = (QGuiApplication.screenAt(pill_rect.center())
                  or QGuiApplication.primaryScreen())
        if screen is not None:
            area = screen.geometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(int(x), int(y))
        if not self.isVisible():
            self.show()
            self.raise_()


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
        # ピン留め。Copilot が引っ込んでも札を出したままにする。
        self._pinned = False
        # Ctrl+ドラッグで置かれた場所。None なら自動配置。
        self._manual_pos = None
        # 最後に札が居た場所。Copilot が最小化されたときの置き場所に使う。
        self._last_pos = None
        # 待ちの経過秒。操作していない時間だけを積み上げる(_advance_elapsed)。
        self._elapsed_accum = 0.0
        self._elapsed_ticked_at = None
        # 一時停止中に Copilot が動き出したとき、再開するか尋ねる窓。
        self._resume_prompt = None
        self._resume_declined = False

        self._pill = StatusPill(on_toggle_pause=self.toggle_pause,
                                on_quit=self.quit_by_user,
                                on_toggle_pin=self.toggle_pin,
                                on_moved=self._on_pill_moved)
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
            self._close_prompt()
            if self._paused:
                # 止めている間は光らせない。再開したときにまだ待っていれば、
                # そのとき改めて光ってよいので reset で印も消す。
                self._flash.reset()
            else:
                self._state = None
                self._state_since = None
                self._last_decisive = None
                self._on_poll()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            print(f"[copilot-status] 一時停止の切り替えに失敗: {e}")

    def _check_resume_while_paused(self):
        """一時停止中に Copilot が応答中になったら、再開するか一度だけ尋ねる。

        止めている間も UIA を1回だけ触ることになるが、状態を見るだけで
        (status_snapshot ではなく state)、走査は軽い。ここを見ないと、
        止めたまま忘れて放置する事故が起きる——それを防ぐのが元々この機能の目的。

        断られたら二度と聞かない(_resume_declined)。しつこいと邪魔なだけで、
        手番が変わればフラグは消える。"""
        if self._resume_declined or self._resume_prompt is not None:
            return
        cp = self._copilot
        if cp is None:
            return
        try:
            if cp.state() != "busy":
                return
        except Exception:  # noqa: BLE001  読めないだけなら次の周期で
            return
        self._resume_prompt = ResumePrompt(on_yes=self._resume_yes,
                                           on_no=self._resume_no)
        self._resume_prompt.show_above(self._pill.geometry())

    def _close_prompt(self):
        prompt, self._resume_prompt = self._resume_prompt, None
        if prompt is not None:
            try:
                prompt.close()
            except Exception:  # noqa: BLE001
                pass

    def _resume_yes(self):
        try:
            self._close_prompt()
            if self._paused:
                self.toggle_pause()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-status] 再開に失敗: {e}")

    def _resume_no(self):
        try:
            self._close_prompt()
            # 同じ待ちについては、もう聞かない。手番が変われば聞き直す。
            self._resume_declined = True
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-status] 応答の処理に失敗: {e}")

    def toggle_pin(self):
        """札のピン留めを入切する。

        留めるのは**この札だけ**で、Copilot の窓には一切手を出さない。前面化や
        最小化の復元はフォーカスを奪う恐れがあり、tray-tools では禁じている
        (打ちかけの文章が別の窓へ飛ぶ事故を過去に踏んだ)。相手を引き出す代わりに、
        こちらが残る。"""
        try:
            self._pinned = not self._pinned
            self._pill.set_pinned(self._pinned)
            if not self._pinned:
                # 留めるのをやめた。Copilot が見えないなら素直に引っ込む。
                self._on_follow()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            print(f"[copilot-status] ピン留めの切り替えに失敗: {e}")

    def _on_pill_moved(self, pos):
        """Ctrl+ドラッグで置かれた場所を覚える。以後そこに置き続ける。

        覚えないと、次の追従(150ms後)で自動配置に引き戻されて動かせない。"""
        self._manual_pos = pos
        self._last_pos = pos

    def reset_position(self):
        """手で動かした位置を忘れて、自動配置に戻す。"""
        self._manual_pos = None

    def quit_by_user(self):
        """札の右クリックから「終了」を選ばれたときの出口。

        終了コード0で終わる。常駐はこれを見て「事故で落ちたのではなく、人が
        消した」と判断し、起こし直さずにメニューのチェックを外す
        (copilot_watchdog._on_watch)。区別が無いと、消した札が数秒後に
        勝手に戻ってきてしまう。"""
        try:
            self._poll_timer.stop()
            self._follow_timer.stop()
            self._hide_all()
            self._release()
        except Exception:  # noqa: BLE001
            pass
        QApplication.instance().exit(0)

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
            # 止めている間も、Copilot が動き出したかどうかだけは見る。
            # 一時停止は「いまは邪魔しないで」であって「もう知らない」ではない。
            # 相手が喋り始めたなら、見張りに戻すか一度だけ尋ねる。
            self._check_resume_while_paused()
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
            # 手番が変わったら経過秒は数え直す。積み上げ方式なので、
            # ここで戻さないと前の待ちの秒数を引き継いでしまう。
            self._elapsed_accum = 0.0
            self._elapsed_ticked_at = None
            # 前の待ちで断った「再開しますか」も、状況が変わったので忘れる。
            self._resume_declined = False

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
        if self._state is None and not self._paused and not self._pinned:
            self._hide_all()
            return
        cp = self._copilot
        rect = _to_logical(
            copilot_loop.window_bounds(cp.hwnd_main) if cp else None)
        if rect is None:
            # 最小化・窓が消えた。ピン留めしてあるなら、最後に居た場所へ札だけ
            # 残す(Copilot を前面に引き出したりはしない。触るのは自分の窓だけ)。
            if not self._pinned or self._last_pos is None:
                self._hide_all()
                return
            self._flash.reset()   # 相手が見えないのに窓を光らせても意味が無い
            self._show_pill_at(self._last_pos)
            return

        elapsed = self._advance_elapsed()
        alert = (not self._paused
                 and self._state in ALERTABLE_STATES
                 and elapsed >= self._threshold)

        # 停止中は state が None のこともある(掴み直す前など)。札は灰色の
        # 「一時停止中」になるので、状態のキーは何でもよい。
        self._pill.apply_state(self._state or "waiting_user", elapsed,
                               alert=alert, paused=self._paused)
        # 手で動かされていたら、その場所を尊重する。自動配置に戻すと、直した
        # そばから元へ引き戻されて動かせなくなる。
        if self._manual_pos is not None:
            self._show_pill_at(self._manual_pos)
        else:
            self._pill.place(rect, self._right_inset)
            self._show_pill_at(None)
        self._last_pos = self._pill.pos()

        if alert:
            self._flash.start(rect)
        else:
            # 待ちが解けた。次の待ちでまた光れるように印を消す。
            self._flash.reset()

    def _show_pill_at(self, pos):
        """札を出す。pos を渡せばそこへ置く。既に出ていれば raise しない。"""
        if pos is not None:
            self._pill.move(pos)
        if not self._pill.isVisible():
            self._pill.show()
            self._pill.raise_()

    def _advance_elapsed(self):
        """待ちの経過秒を進めて返す。**操作中は進めない。**

        壁時計の差(now - 開始時刻)をやめたのは、陽太さんが目の前で作業している
        間まで数えてしまうため。キーやマウスを触っている人は画面を見ているので、
        点滅で急かす相手ではない。数えるのは「触っていない時間」だけにする。

        追従のタイマー(150ms)ごとに呼ばれるので、その刻みで積み上げる。"""
        now = time.time()
        last = self._elapsed_ticked_at
        self._elapsed_ticked_at = now
        if self._state_since is None:
            return self._elapsed_accum
        if last is None:
            return self._elapsed_accum
        idle = seconds_since_input()
        if idle is not None and idle < INPUT_ACTIVE_SECONDS:
            # 操作中。時間は進めるが、待ちとしては数えない。
            return self._elapsed_accum
        self._elapsed_accum += now - last
        return self._elapsed_accum

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
            self._close_prompt()
            self._flash.reset()
            self._pill.hide()
        except Exception:  # noqa: BLE001
            pass


def sweep_other_instances():
    """自分以外の状態監視バーを片付ける。片付けた数を返す。

    【なぜ子側でやるのか】
    最初は常駐側で掃除していたが、psutil の process_iter(['cmdline']) は内部で
    COM を使う。常駐は pycaw を持っているので、そこへ COM を持ち込むと GC のたびに
    即死する——**避けようとしていた当のものを、避けるためのコードで持ち込んでいた**
    (2026-09-05 13:17 に実際に落ちた)。こちらのプロセスには pycaw が居ないので、
    psutil を使っても安全。

    【判定を引数ごとに見る理由】
    コマンドラインを連結した文字列で判定すると、判定コード自身がスクリプト名の
    文字列を含むため自分を殺す。これも実際にやらかした。"""
    try:
        import psutil
    except ImportError:
        return 0
    # 自分と、自分の先祖は除く。venv の Scripts\pythonw.exe は本体のインタプリタを
    # 子として起こす中継役で、**その中継役のコマンドラインもこのスクリプトを指す**。
    # 自分だけ除いて掃除すると、自分を起こしてくれた中継役を殺すことになり、
    # 常駐から見ると「子が死んだ」ことになってしまう(実際そうなった)。
    keep = {os.getpid()}
    try:
        proc = psutil.Process(os.getpid())
        for _ in range(4):   # 中継役は1段だが、余裕を見て数段たどる
            proc = proc.parent()
            if proc is None:
                break
            keep.add(proc.pid)
    except Exception:  # noqa: BLE001
        pass

    victims = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid in keep:
                continue
            argv = proc.info["cmdline"] or []
            if any(a.replace("\\", "/").endswith("/copilot_status_process.py")
                   for a in argv):
                proc.terminate()
                victims.append(proc)
        except Exception:  # noqa: BLE001  消えた・権限が無いだけなら気にしない
            continue
    if victims:
        try:
            _gone, alive = psutil.wait_procs(victims, timeout=3)
            for proc in alive:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
    return len(victims)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Copilot の手番を常時表示する（常駐とは別プロセス）")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_SECONDS,
                        help="この秒数を超えて待ちが続いたら窓を点滅させる")
    parser.add_argument("--parent-pid", type=int, default=0,
                        help="この pid が消えたら自分も終わる（常駐が指定する）")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # pythonw.exe では stdout が無い

    # 二重に出さない。前の常駐が落ちて取り残された札が居ることがある。
    sweep_other_instances()

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

    # 常駐が消えたら自分も終わる。
    #
    # 【なぜ要るか】
    # 常駐が「終了」で綺麗に終わるとは限らない。落ちることもあるし、タスク
    # マネージャで殺されることもある。そのとき subprocess の子は Windows では
    # 生き残るので、札だけが画面に貼り付いたまま消せなくなる(実際そうなった。
    # 取り残しが25個溜まっていた)。親の生死を自分で見張るのが確実。
    if args.parent_pid:
        def check_parent():
            try:
                import psutil
                if not psutil.pid_exists(args.parent_pid):
                    app.quit()
            except Exception:  # noqa: BLE001  見張りのために落ちない
                pass

        parent_timer = QTimer()
        parent_timer.setInterval(PARENT_CHECK_MS)
        parent_timer.timeout.connect(check_parent)
        parent_timer.start()
        # 参照を持ち続けないと GC で消える(タイマーの持ち主が居なくなる)。
        app._parent_timer = parent_timer

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
