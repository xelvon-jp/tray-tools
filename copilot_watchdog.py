# copilot_watchdog.py
# Copilot の「いま誰の手番か」を、Copilot の窓の下端に常時オーバーレイで出す。
# 待ちが閾値秒(既定30秒)を超えたら窓全体を点滅させて、遠目でも気づけるようにする。
#
# なぜ「1回だけ出す通知」から「常時表示」に変えたか
# --------------------------------------------------
# 前の版は「idle が30秒続いたら画面中央に大きな窓を1回出す」だった。実機で使うと
# 二つ困った。(1) 出ていない間は何も分からないので、いま応答中なのか止まっているのか
# 見て判断できない。(2) 画面中央に出るので Copilot と視線が離れ、しかも本文を隠す。
# ステータスを常に入力欄の近くに置いておけば、視線を動かさずに手番が分かる。
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
# 点滅は「応答待ち」と「応答済(入力待ち)」の両方で出す。どちらも誰かが待っている
# 状態で、放置されると困るのは同じだから。入力中と応答中は、待っている人が居ないので
# 点滅させない。
#
# 描画をスタイルシートでやらない理由
# ----------------------------------
# QWidget を継承した窓は setStyleSheet の背景と枠が描かれず、中の QLabel の文字だけが
# 宙に浮く。実機でこれを踏んで「白い Copilot 画面に白文字」で読めなかった。
# WA_StyledBackground を足しても半透明属性と噛み合わなかったので、paintEvent で
# 自分で描く形にした。こうすると背景・枠・文字・点滅を1か所で面倒を見られる。
#
# 位置を窓の下端基準にする理由
# ----------------------------
# 入力欄の UIA 矩形(userInput)は高さ22pxしかなく、見た目の入力ボックスよりずっと
# 小さい。そこを基準に「すぐ上」へ置くと入力ボックスに重なる。窓の下端から一定量だけ
# 浮かせれば、入力ボックスがどれだけ大きくても必ずその下に出る。
# 横位置だけは入力欄の右端に合わせる(縦線が揃って落ち着く)。
#
# 業務PC(M365 Copilot)対応
# ------------------------
# 窓もボタン名も copilot_loop のプロファイルが面倒を見る。この機能が要るのは
# 「窓の矩形」「入力欄の矩形」「busy/ready/idle」の3つだけで、発言マーカーは使わない。
# M365 のマーカーは未特定のままだが、この機能はそれで動く。
#
# 実装のかたち
# ------------
# タイマーが2本ある。状態は UIA の走査が要るので重く、1.5秒ごと。窓の位置は
# GetWindowRect だけなので軽く、150msごと(ドラッグにぬるりと付いてくる)。
# CLAUDE.md にある「スロット内で例外が投げ切られると常駐ごと終了する」対策として、
# QTimer のスロットで呼ぶものは必ず try で受ける。
import time

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

import capture_grab
import copilot_loop
import settings as settings_module

# 状態を取り直す間隔(秒)。UIA の走査が要るので控えめに。
POLL_INTERVAL_SECONDS = 1.5
# 窓を追う間隔(ms)。GetWindowRect だけなので短くてよい。
FOLLOW_INTERVAL_MS = 150

# 既定の閾値。陽太さんの要件「30秒以上」。
DEFAULT_THRESHOLD_SECONDS = 30

PILL_WIDTH = 290
PILL_HEIGHT = 40
# 窓の下端から札の下端までの浮かせ量(論理px)。
PILL_GAP_BOTTOM = 12
# 入力欄の右端が窓の右端から何px内側にあるか。実測できるまでの初期値
# (手元PCの Copilot で168px、M365 でも同じ桁と見込む)。
DEFAULT_RIGHT_INSET = 168

FLASH_INTERVAL_MS = 550
FLASH_BORDER_PX = 16

SETTINGS_SECTION = "copilot_watchdog"
SETTINGS_ENABLED = "enabled"
SETTINGS_THRESHOLD = "threshold_seconds"

# 状態キー → (絵文字, 文言, 背景色, 経過秒を出すか)
# 背景色は白文字とのコントラスト比が 4.5 以上あるものを選んである。札の中は必ず
# この色で塗るので、Copilot がライトモードでも白文字が読める。
STATE_STYLES = {
    "typing": ("✏", "ユーザ入力中", QColor(168, 95, 0), False),
    "waiting_ai": ("⏳", "応答待ち", QColor(179, 40, 40), True),
    "responding": ("💬", "応答中", QColor(31, 107, 176), False),
    "waiting_user": ("🙂", "応答済（入力待ち）", QColor(31, 138, 76), True),
}
# 閾値を超えたときの見た目。札もこれに変わるので、点滅を見逃しても札で分かる。
ALERT_STYLE = ("🔔", None, QColor(211, 32, 32), True)

# 待ちが続いたら点滅させる状態。入力中と応答中は待っている人が居ないので入れない。
ALERTABLE_STATES = ("waiting_ai", "waiting_user")


def _to_logical(bounds):
    """Win32/UIA の (left, top, right, bottom)(物理px)を Qt の論理 QRect へ。"""
    if bounds is None:
        return None
    return capture_grab.device_bounds_to_logical(bounds)


class StatusPill(QWidget):
    """窓の下端に貼り付ける、常時表示のステータス札。

    フォーカスもマウスも透過する。Copilot のすぐそばに置くので、透過しないと
    札を掴んでしまって下にあるものが押せなくなる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot ステータス")
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

    def apply_state(self, state_key, elapsed_seconds=None, alert=False):
        emoji, label, color, wants_elapsed = STATE_STYLES[state_key]
        if alert:
            a_emoji, _a_label, a_color, a_elapsed = ALERT_STYLE
            # 文言は状態のものを残す。何を待っているのかが分からなくなるため。
            emoji, color, wants_elapsed = a_emoji, a_color, a_elapsed
        changed = (emoji, label, color.rgb()) != (
            self._emoji, self._label, self._fill.rgb())
        self._emoji, self._label, self._fill = emoji, label, color
        elapsed_text = (
            f"{int(elapsed_seconds)}秒"
            if wants_elapsed and elapsed_seconds is not None else ""
        )
        if elapsed_text != self._elapsed:
            self._elapsed = elapsed_text
            changed = True
        if changed:
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
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると常駐ごと落ちる
            print(f"[copilot_watchdog] 点滅の描き替えに失敗: {e}")

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


class CopilotWatchdog:
    """Copilot の手番を常時オーバーレイで出す監視係。

    トレイメニューから ON/OFF できる。ON の間だけタイマーが回る。"""

    def __init__(self, app_settings=None, settings_path=None,
                 is_agent_loop_running=lambda: False):
        self._app_settings = app_settings
        self._settings_path = settings_path
        # agent-loop が回っている間は札も点滅も出さない。あれが回っているときの
        # 手番は「tray-tools の番」であって、この4状態のどれでもない。
        self._is_agent_loop_running = is_agent_loop_running

        self._enabled = self._load_bool(SETTINGS_ENABLED, False)
        self._threshold = self._load_int(SETTINGS_THRESHOLD, DEFAULT_THRESHOLD_SECONDS)

        self._state = None        # 4状態のキー。None は未判定
        self._state_since = None  # その状態になった時刻
        # 直近に見た「手番がはっきりしている状態」。idle の意味を決めるのに使う。
        self._last_decisive = None

        self._copilot = None
        self._right_inset = DEFAULT_RIGHT_INSET

        self._pill = None
        self._flash = None

        self._poll_timer = QTimer()
        self._poll_timer.setInterval(int(POLL_INTERVAL_SECONDS * 1000))
        self._poll_timer.timeout.connect(self._on_poll)
        self._follow_timer = QTimer()
        self._follow_timer.setInterval(FOLLOW_INTERVAL_MS)
        self._follow_timer.timeout.connect(self._on_follow)

        if self._enabled:
            self._start_timers()

    # -- 外部から呼ばれる操作 --------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._save_bool(SETTINGS_ENABLED, self._enabled)
        if self._enabled:
            self._reset()
            self._start_timers()
            # 最初の poll を待つと、押してから札が出るまで1.5秒黙る。すぐ1回叩く。
            QTimer.singleShot(0, self._on_poll)
        else:
            self._poll_timer.stop()
            self._follow_timer.stop()
            self._hide_all()
            self._copilot = None

    def is_enabled(self) -> bool:
        return self._enabled

    def threshold_seconds(self) -> int:
        return self._threshold

    def set_threshold(self, seconds: int) -> None:
        seconds = max(5, int(seconds))
        self._threshold = seconds
        self._save_int(SETTINGS_THRESHOLD, seconds)

    def close(self) -> None:
        """常駐終了時に呼ぶ。タイマー停止 + 窓を畳む。"""
        for timer in (self._poll_timer, self._follow_timer):
            try:
                timer.stop()
            except Exception:  # noqa: BLE001
                pass
        self._hide_all()
        # UIA クライアントを握ったままにしない。
        self._copilot = None

    def _start_timers(self) -> None:
        self._poll_timer.start()
        self._follow_timer.start()

    # -- 状態を取り直す(重い。1.5秒ごと) ---------------------------------
    def _on_poll(self) -> None:
        try:
            self._poll()
        except Exception as e:  # noqa: BLE001
            # スロット内で例外が抜けると常駐ごと落ちる(CLAUDE.md)。ここで受ける。
            print(f"[copilot_watchdog] 状態の取得に失敗: {e}")

    def _poll(self) -> None:
        if self._is_agent_loop_running():
            self._hide_all()
            self._reset()
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

        # 入力欄の右端が窓の右端から何px内側か。窓が動いても変わらないので、
        # ここで測って覚えておき、追従側ではこれを使い回す。
        window = _to_logical(snapshot.get("window_rect"))
        inp = _to_logical(snapshot.get("input_rect"))
        if window is not None and inp is not None:
            inset = window.right() - inp.right()
            # 桁が明らかにおかしい値は捨てる(描き替え中に潰れた矩形を拾うことがある)。
            if 0 <= inset < window.width() // 2:
                self._right_inset = inset

    # -- 窓を追う(軽い。150msごと) ---------------------------------------
    def _on_follow(self) -> None:
        try:
            self._follow()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot_watchdog] 追従に失敗: {e}")

    def _follow(self) -> None:
        if self._state is None or self._is_agent_loop_running():
            self._hide_all()
            return
        copilot = self._copilot
        rect = _to_logical(
            copilot_loop.window_bounds(copilot.hwnd_main) if copilot else None)
        if rect is None:
            # 最小化・窓が消えた。次の poll で掴み直す。
            self._hide_all()
            return

        elapsed = time.time() - (self._state_since or time.time())
        alert = self._state in ALERTABLE_STATES and elapsed >= self._threshold

        if self._pill is None:
            self._pill = StatusPill()
        self._pill.apply_state(self._state, elapsed, alert=alert)
        self._pill.place(rect, self._right_inset)
        if not self._pill.isVisible():
            self._pill.show()
            self._pill.raise_()

        if alert:
            if self._flash is None:
                self._flash = FlashFrame()
            self._flash.start(rect)
        elif self._flash is not None:
            self._flash.stop()

    # -- 判定 ------------------------------------------------------------
    def _classify(self, snapshot: dict) -> str:
        """busy/ready/idle の3値と入力欄の中身から、4つの手番のどれかを決める。

        入力欄に文字があれば、送信ボタンが出ていようがいまいが「入力中」。
        state が 'ready' でも入力欄が空のことがあるので、文字の有無を先に見る。

        空の idle は、直前に何を見たかで意味が変わる:
          入力中 → 空idle : 送信した直後 → 応答待ち
          応答中 → 空idle : 喋り終わった → 入力待ち
        判断がつかない(常駐を起動した直後など)ときは入力待ち扱いにする。
        どちらも点滅する状態なので実害は小さく、「応答待ち」と言い切って
        Copilot が遅いように見せるより、こちらのほうが誤解が少ない。"""
        if snapshot.get("state") == "busy":
            self._last_decisive = "responding"
            return "responding"
        if (snapshot.get("input_text") or "").strip():
            self._last_decisive = "typing"
            return "typing"
        return "waiting_ai" if self._last_decisive == "typing" else "waiting_user"

    def _read_snapshot(self):
        """Copilot の状態一式を取る。Copilot が居ない・読めないなら None。

        掴んだ Copilot は使い回す。Copilot() の生成は窓の全列挙 + アクセシビリティ
        ツリーの起こし + UIA クライアントの生成で、毎回やるには重い(実測 140ms、
        使い回すと 74ms)。窓が閉じた・アプリを起動し直したときは hwnd が無効に
        なるので、そこで捨てて作り直す。

        COM オブジェクトは Copilot インスタンスの属性として生き続けるので、
        こちらがインスタンスを持ち続けている限り「関数の外に出して即解放」の罠
        (CLAUDE.md)は踏まない。"""
        cp = self._copilot
        if cp is not None and not copilot_loop.user32.IsWindow(cp.hwnd_main):
            cp = self._copilot = None
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
            self._copilot = None
            return None

    def _reset(self) -> None:
        self._state = None
        self._state_since = None
        self._last_decisive = None

    def _hide_all(self) -> None:
        if self._flash is not None:
            try:
                self._flash.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._pill is not None:
            try:
                self._pill.hide()
            except Exception:  # noqa: BLE001
                pass

    # -- 設定の永続化(snippets.push_recent と同じ流儀) ------------------
    def _load_bool(self, key: str, default: bool) -> bool:
        section = (self._app_settings or {}).get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            return default
        return bool(section.get(key, default))

    def _load_int(self, key: str, default: int) -> int:
        section = (self._app_settings or {}).get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            return default
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError):
            return default

    def _save_bool(self, key: str, value: bool) -> None:
        self._save_scalar(key, bool(value))

    def _save_int(self, key: str, value: int) -> None:
        self._save_scalar(key, int(value))

    def _save_scalar(self, key: str, value) -> None:
        if isinstance(self._app_settings, dict):
            section = self._app_settings.get(SETTINGS_SECTION)
            if not isinstance(section, dict):
                section = self._app_settings[SETTINGS_SECTION] = {}
            section[key] = value
        if not self._settings_path:
            return
        import json
        import os
        try:
            stored = {}
            if os.path.exists(self._settings_path):
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
            section = stored.get(SETTINGS_SECTION)
            if not isinstance(section, dict):
                section = stored[SETTINGS_SECTION] = {}
            section[key] = value
            settings_module.save_settings(stored, self._settings_path)
        except OSError as e:
            print(f"[copilot_watchdog] 設定保存に失敗: {e}")
