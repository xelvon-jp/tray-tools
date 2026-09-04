# copilot_watchdog.py
# Copilot の「いま誰の手番か」を、Copilot の入力欄のすぐ上に常時オーバーレイで出す。
# 応答待ちが閾値秒(既定30秒)を超えたら、Copilot の窓全体を点滅させて遠目でも気づかせる。
#
# なぜ「1回だけ出す通知」から「常時表示」に変えたか
# --------------------------------------------------
# 前の版は「idle が30秒続いたら画面中央に大きな窓を1回出す」だった。実機で使うと
# 二つ困った。(1) 出ていない間は何も分からないので、いま応答中なのか止まっているのか
# 見て判断できない。(2) 画面中央に出るので Copilot と視線が離れ、しかも本文を隠す。
# ステータスを常に入力欄の脇に置いておけば、視線をその場から動かさずに手番が分かる。
#
# 4つの状態
# ---------
#   ✏ ユーザ入力中        入力欄に文字がある(送信前)
#   ⏳ 応答待ち            送信済みで、Copilot がまだ喋り始めていない ← ここが閾値超えで点滅
#   💬 応答中              Copilot が生成中(割り込みボタンが出ている)
#   🙂 応答済（入力待ち）  Copilot が喋り終わって、こちらの番
#
# copilot_loop.state() が返すのは busy/ready/idle の3つで、「送信直後の idle」と
# 「応答が終わった後の idle」が区別できない。直前に見た状態を覚えておいて、
# 入力中→idle なら応答待ち、応答中→idle なら入力待ち、と手番を決めている。
#
# なぜ Pushover ではなく画面通知か
# --------------------------------
# 業務PC ではスマホ通知を使えない場面が多い(Pushover を入れられない、Wi-Fi 分離、
# セキュリティ規約など)。手元でPCの画面を見れば分かる形が必要。
#
# 実装のかたち
# ------------
# QTimer で数秒おきに poll するだけ。Copilot が起動していないときは
# copilot_loop.Copilot() が RuntimeError を投げるので、そこで畳む。
# CLAUDE.md にある「スロット内で例外が投げ切られると常駐ごと終了する」対策として、
# QTimer のスロットで呼ぶ tick は必ず try で受ける。
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

import capture_grab
import copilot_loop
import settings as settings_module

# poll する間隔(秒)。常時表示なので、状態が変わってから札に反映されるまでの遅れが
# そのまま「反応が鈍い」という印象になる。1.5秒だと体感で追随して見える。
POLL_INTERVAL_SECONDS = 1.5

# 既定の閾値。陽太さんの要件「30秒以上」。
DEFAULT_THRESHOLD_SECONDS = 30

# ステータス札の大きさ。入力欄の上に載せるので、本文を隠さない範囲で。
STATUS_WIDTH = 290
STATUS_HEIGHT = 40

# 札を入力欄の上端からどれだけ浮かせるか(論理px)。
STATUS_GAP = 6

# 入力欄の矩形が取れなかったときの逃げ道。窓の下端からこれだけ上に置く
# (copilot_loop._bottom_buttons が入力欄まわりとみなしている 170px に合わせてある)。
INPUT_BAND_HEIGHT = 170

# 点滅の周期(ms)。速すぎると視界の端で不快、遅いと点滅と気づかない。
FLASH_INTERVAL_MS = 550

# 点滅の枠の太さ(論理px)。遠目で気づかせるのが目的なので太くする。
FLASH_BORDER_PX = 16

SETTINGS_SECTION = "copilot_watchdog"
SETTINGS_ENABLED = "enabled"
SETTINGS_THRESHOLD = "threshold_seconds"

# 状態キー → (札の文言, 背景色)。色は状態を色だけで見分けられるように離してある。
STATE_STYLES = {
    "typing": ("✏ ユーザ入力中", "rgba(230, 145, 20, 235)"),
    "waiting_ai": ("⏳ 応答待ち", "rgba(190, 45, 45, 235)"),
    "responding": ("💬 応答中", "rgba(40, 115, 190, 235)"),
    "waiting_user": ("🙂 応答済（入力待ち）", "rgba(35, 145, 80, 235)"),
}

# 経過秒を出す状態。入力中と応答中は「何秒経ったか」に意味が薄く、
# 数字が動き続けると視界の端でちらついて邪魔になる。
ELAPSED_STATES = ("waiting_ai", "waiting_user")


def _to_logical(bounds):
    """Win32/UIA の (left, top, right, bottom)(物理px)を Qt の論理 QRect へ。

    最小化された窓は -32000 付近を返すので、そこは呼び側で弾く。"""
    if bounds is None:
        return None
    return capture_grab.device_bounds_to_logical(bounds)


def _looks_offscreen(rect) -> bool:
    """最小化された窓かどうか。Windows は最小化中の窓に -32000 付近の座標を返す。"""
    return rect is None or rect.left() < -20000 or rect.top() < -20000


class CopilotStatusOverlay(QWidget):
    """入力欄のすぐ上に貼り付ける、常時表示のステータス札。

    フォーカスを奪わない(WindowDoesNotAcceptFocus)うえ、マウスも透過する
    (WA_TransparentForMouseEvents)。Copilot の入力欄の直上に置くので、透過しないと
    札を掴んでしまって入力欄が押せなくなる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot ステータス")
        self.setObjectName("copilotStatus")
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
        # QWidget を継承した窓は、これが無いとスタイルシートの背景と枠が描かれない
        # (Qt の仕様。中の QLabel には効くので「文字だけ宙に浮く」という出方になる)。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.resize(STATUS_WIDTH, STATUS_HEIGHT)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 4, 14, 4)
        layout.setSpacing(10)

        self.label = QLabel("")
        self.label.setFont(QFont("Meiryo", 11, QFont.Bold))
        self.label.setStyleSheet("color: #fff; background: transparent;")
        layout.addWidget(self.label)

        layout.addStretch(1)

        self.elapsed = QLabel("")
        self.elapsed.setFont(QFont("Meiryo", 10))
        self.elapsed.setStyleSheet("color: rgba(255,255,255,200); background: transparent;")
        layout.addWidget(self.elapsed)

        self._background = None
        self._apply_background("rgba(90, 90, 90, 235)")

    def _apply_background(self, color: str) -> None:
        # setStyleSheet は毎回スタイルを作り直すので、変わっていないなら触らない
        # (1.5秒ごとに再適用すると札が微妙にちらつく)。
        if color == self._background:
            return
        self._background = color
        self.setStyleSheet(
            f"#copilotStatus {{ background-color: {color};"
            " border-radius: 8px; border: 1px solid rgba(255,255,255,90); }}"
        )

    def apply_state(self, state_key: str, elapsed_seconds) -> None:
        text, color = STATE_STYLES[state_key]
        self.label.setText(text)
        self.elapsed.setText(
            f"{int(elapsed_seconds)}秒" if elapsed_seconds is not None else ""
        )
        self._apply_background(color)

    def place_above(self, anchor_rect, window_rect) -> bool:
        """入力欄(anchor_rect)の直上に置く。取れないときは窓の下端から逆算する。

        置けたら True。窓が最小化されていて置き場所が決まらないなら False。"""
        rect = anchor_rect
        if _looks_offscreen(rect):
            if _looks_offscreen(window_rect):
                return False
            # 入力欄の矩形だけ取れなかった場合。窓の下端の帯を入力欄とみなす。
            rect = window_rect.adjusted(0, window_rect.height() - INPUT_BAND_HEIGHT, 0, 0)

        # 入力欄の右端に寄せる。左寄せだと本文の書き出しに重なりやすいのと、
        # Copilot の送信ボタンが右下にあるので視線の動線とも合う。
        x = rect.right() - self.width()
        y = rect.top() - self.height() - STATUS_GAP

        # 画面の外へ出すと Windows は表示するが見えない。乗っている画面へ押し戻す。
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.geometry()
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(int(x), int(y))
        return True


class CopilotFlashOverlay(QWidget):
    """Copilot の窓全体に重ねて点滅させる枠。応答待ちが閾値を超えたときだけ出る。

    塗り潰さず「太い枠 + ごく薄い塗り」で点滅させる。全面を濃く塗ると本文が読めず、
    気づいた瞬間に中身を確認できない。マウスもキーも透過するので、点滅したまま
    Copilot をそのまま操作できる。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot 応答待ち")
        self.setObjectName("copilotFlash")
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
        # 枠だけの窓なので、これが無いと本当に何も描かれない(上と同じ理由)。
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._bright = False
        self._timer = QTimer()
        self._timer.setInterval(FLASH_INTERVAL_MS)
        self._timer.timeout.connect(self._on_flash_tick)
        self._paint(False)

    def _on_flash_tick(self) -> None:
        try:
            self._paint(not self._bright)
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると常駐ごと落ちる
            print(f"[copilot_watchdog] 点滅の描き替えに失敗: {e}")

    def _paint(self, bright: bool) -> None:
        self._bright = bright
        if bright:
            border, fill = "rgba(255, 45, 45, 240)", "rgba(255, 45, 45, 45)"
        else:
            border, fill = "rgba(255, 45, 45, 40)", "rgba(255, 45, 45, 0)"
        self.setStyleSheet(
            f"#copilotFlash {{ background-color: {fill};"
            f" border: {FLASH_BORDER_PX}px solid {border}; }}"
        )

    def start(self, window_rect) -> None:
        """window_rect(論理 QRect)に重ねて点滅を始める。既に出ていれば位置だけ追う。"""
        if _looks_offscreen(window_rect):
            self.stop()
            return
        self.setGeometry(window_rect)
        if not self._timer.isActive():
            self._paint(True)
            self._timer.start()
        # WA_ShowWithoutActivating が効くので show でよい(activateWindow は呼ばない)。
        self.show()
        self.raise_()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()


class CopilotWatchdog:
    """Copilot の手番を常時オーバーレイで出す監視係。

    トレイメニューから ON/OFF できる。ON の間だけ QTimer が回る。"""

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

        self._timer = QTimer()
        self._timer.setInterval(int(POLL_INTERVAL_SECONDS * 1000))
        self._timer.timeout.connect(self._on_tick)

        self._status = None  # CopilotStatusOverlay(遅延生成)
        self._flash = None   # CopilotFlashOverlay(遅延生成)
        # 掴んだ Copilot(使い回す。_read_snapshot を参照)。
        self._copilot = None

        if self._enabled:
            self._timer.start()

    # -- 外部から呼ばれる操作 --------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._save_bool(SETTINGS_ENABLED, self._enabled)
        if self._enabled:
            self._reset()
            self._timer.start()
            # 最初の tick を待つと、押してから札が出るまで1.5秒黙る。すぐ1回叩く。
            QTimer.singleShot(0, self._on_tick)
        else:
            self._timer.stop()
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
        try:
            self._timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self._hide_all()
        # UIA クライアントを握ったままにしない。
        self._copilot = None

    # -- タイマー本体 ---------------------------------------------------
    def _on_tick(self) -> None:
        try:
            self._tick()
        except Exception as e:  # noqa: BLE001
            # スロット内で例外が抜けると常駐ごと落ちる(CLAUDE.md)。ここで受ける。
            print(f"[copilot_watchdog] tick 失敗: {e}")

    def _tick(self) -> None:
        if self._is_agent_loop_running():
            self._hide_all()
            self._reset()
            return

        snapshot = self._read_snapshot()
        if snapshot is None:
            # Copilot が居ない or 一時的に読めなかった。畳むが、手番の記憶は残す
            # (Copilot が一瞬読めなかっただけで応答待ちが入力待ちに化けると困る)。
            self._hide_all()
            return

        state = self._classify(snapshot)
        if state != self._state:
            self._state = state
            self._state_since = time.time()
        elapsed = time.time() - (self._state_since or time.time())

        window_rect = _to_logical(snapshot.get("window_rect"))
        input_rect = _to_logical(snapshot.get("input_rect"))

        if self._status is None:
            self._status = CopilotStatusOverlay()
        if not self._status.place_above(input_rect, window_rect):
            # 最小化されている。札の置き場所が無いので何も出さない。
            self._hide_all()
            return
        self._status.apply_state(
            state, elapsed if state in ELAPSED_STATES else None
        )
        self._status.show()
        self._status.raise_()

        # 応答待ちが続きすぎたら窓全体を点滅させる。他の状態に移れば即やめる。
        if state == "waiting_ai" and elapsed >= self._threshold:
            if self._flash is None:
                self._flash = CopilotFlashOverlay()
            self._flash.start(window_rect)
        elif self._flash is not None:
            self._flash.stop()

    def _classify(self, snapshot: dict) -> str:
        """busy/ready/idle の3値と入力欄の中身から、4つの手番のどれかを決める。

        入力欄に文字があれば、送信ボタンが出ていようがいまいが「入力中」。
        state が 'ready'(送信ボタンあり)でも入力欄が空のことがあるので、
        文字の有無を先に見る。

        空の idle は、直前に何を見たかで意味が変わる:
          入力中 → 空idle : 送信した直後 → 応答待ち
          応答中 → 空idle : 喋り終わった → 入力待ち
        判断がつかない(常駐を起動した直後など)ときは入力待ち扱いにする。
        こちらに寄せるのは、点滅するのが応答待ちのほうだけだから
        (分からないときに点滅を始めるより、黙っているほうが害が小さい)。"""
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
        ツリーの起こし(最大1秒待つ) + UIA クライアントの生成で、常時ポーリングの
        たびにやるには重い。窓が閉じた・Copilot を起動し直したときは hwnd が
        無効になるので、そこで捨てて作り直す。

        COM オブジェクトは Copilot インスタンスの属性として生き続けるので、
        こちらがインスタンスを持ち続けている限り「関数の外に出して即解放」の罠
        (CLAUDE.md)は踏まない。"""
        cp = self._copilot
        if cp is not None and not copilot_loop.user32.IsWindow(cp.hwnd_main):
            cp = self._copilot = None
        if cp is None:
            try:
                cp = self._copilot = copilot_loop.Copilot()
            except RuntimeError:
                return None
        try:
            return cp.status_snapshot()
        except Exception:  # noqa: BLE001
            # 窓を掴み直せば直ることが多い(Copilot の再起動・タブの入れ替えなど)。
            # 次の tick で作り直す。
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
        if self._status is not None:
            try:
                self._status.hide()
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
