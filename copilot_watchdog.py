# copilot_watchdog.py
# Copilot が「応答済みで、こちらの入力を待っている」状態が閾値秒(既定30秒)を超えたら
# 画面に大きな通知を出す。業務PC で Copilot に聞いた後、返事を返し忘れて放置している
# のを、遠目でも気づけるようにするための機能。
#
# なぜ Pushover ではなく画面通知か
# --------------------------------
# 業務PC ではスマホ通知を使えない場面が多い(Pushover が入れられない、Wi-Fi 分離、
# セキュリティ規約など)。手元でPCの画面を見れば分かる形が必要。トレイ通知の「トースト」
# は小さくて隅に出るだけなので、遠目でも見える大きな窓を画面中央に半透明で被せる。
#
# 判定ロジック
# ------------
# - Copilot が idle(state == "idle") かつ入力欄が空、その状態が threshold_seconds
#   連続で続いたら通知
# - busy になったら、その回のカウントは捨てる(応答生成中の 30 秒は普通のこと)
# - 陽太さんが入力欄に何か書き始めたら、その回のカウントは捨てる(入力中は「待ち」ではない)
# - 通知は 1 回だけ。次の busy を経由するまで再通知しない(鳴らしすぎで無視される)
# - **agent-loop が動いている間は完全に休む。** ループのなかの idle は「次周を送る直前」で、
#   人の入力を待っているわけではない。ここに通知を出したら agent-loop の妨害になる。
#
# 実装のかたち
# ------------
# QTimer で数秒おきに poll するだけ。Copilot が起動していないときは state() を呼ぶ前に
# 「窓が無い = idle 判定できない」と分かるので、フォルスポジ通知にはならない
# (copilot_loop.Copilot() は窓が無ければ RuntimeError を投げるので try で受ける)。
#
# CLAUDE.md にある「スロット内で例外が投げ切られると常駐ごと終了する」対策として、
# QTimer のスロットで呼ぶ tick は必ず try で受ける。
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QFont, QGuiApplication
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

import copilot_loop
import settings as settings_module

# poll する間隔(秒)。閾値の 1/6 くらいを目安に。5秒だと 30秒閾値で最大10秒の遅延がある
# (state を取った瞬間から数え始めるため)。3秒にする。
POLL_INTERVAL_SECONDS = 3.0

# 既定の閾値。陽太さんの要件「30秒以上」。
DEFAULT_THRESHOLD_SECONDS = 30

# 通知窓の大きさ。「遠目でも見える」ために大きく取る。
NOTIFY_WIDTH = 720
NOTIFY_HEIGHT = 220

SETTINGS_SECTION = "copilot_watchdog"
SETTINGS_ENABLED = "enabled"
SETTINGS_THRESHOLD = "threshold_seconds"


class IdleNotifyWindow(QWidget):
    """画面中央に半透明で被せる大きな通知窓。

    半透明にするのは、後ろのアプリを完全に隠さないため(通知に気づいた瞬間、視線を
    その下の Copilot 画面に戻せる)。閉じる操作は明示的な×ボタンだけにして、
    誤クリックで消えないようにする(全画面ならクリックスルーにすべきだが、この窓は
    「私はここ」を強く主張するのが目的なのでクリックスルーにはしない)。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Copilot 入力待ち")
        self.setObjectName("copilotIdleNotify")
        # 最前面固定。フォーカスを奪わない(WindowDoesNotAcceptFocus)ので、
        # 陽太さんが Copilot にすぐ入力を戻せる。
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(NOTIFY_WIDTH, NOTIFY_HEIGHT)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(8)

        # 上段: 大きな見出し
        self.head = QLabel("🤖 Copilot が応答を返しています")
        self.head.setFont(QFont("Meiryo", 22, QFont.Bold))
        self.head.setStyleSheet("color: #fff;")
        layout.addWidget(self.head)

        # 中段: 経過秒数
        self.body = QLabel("入力待ちのまま 30 秒経過")
        self.body.setFont(QFont("Meiryo", 14))
        self.body.setStyleSheet("color: #eee;")
        layout.addWidget(self.body)

        layout.addStretch(1)

        # 右下: 閉じるボタン
        self.close_btn = QPushButton("閉じる")
        self.close_btn.setFont(QFont("Meiryo", 10))
        self.close_btn.setStyleSheet(
            "QPushButton { background-color: #333; color: #fff;"
            " border: 1px solid #666; border-radius: 4px; padding: 6px 18px; }"
            "QPushButton:hover { background-color: #444; }"
        )
        self.close_btn.clicked.connect(self.hide)
        layout.addWidget(self.close_btn, 0, Qt.AlignRight)

        self.setStyleSheet(
            "#copilotIdleNotify { background-color: rgba(180, 40, 40, 220);"
            " border-radius: 12px; border: 2px solid #f0a0a0; }"
        )

    def _center_on_screen(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.top() + area.height() // 3
        self.move(max(area.left(), x), max(area.top(), y))

    def show_with_elapsed(self, elapsed_seconds: int) -> None:
        self.body.setText(f"入力待ちのまま {elapsed_seconds} 秒経過")
        self._center_on_screen()
        # WA_ShowWithoutActivating が効くので show でよい(activateWindow は呼ばない)。
        self.show()
        self.raise_()


class CopilotWatchdog:
    """Copilot が idle のまま閾値を超えたら通知窓を出す監視係。

    トレイメニューから ON/OFF できる。ON の間だけ QTimer が回る。"""

    def __init__(self, app_settings=None, settings_path=None,
                 is_agent_loop_running=lambda: False):
        self._app_settings = app_settings
        self._settings_path = settings_path
        # agent-loop が回っている間は通知を出したくない。判定を外から差し込めるように
        # コールバックで受ける(feature_screen.ScreenFeature が持っている状態を渡す)。
        self._is_agent_loop_running = is_agent_loop_running

        self._enabled = self._load_bool(SETTINGS_ENABLED, False)
        self._threshold = self._load_int(SETTINGS_THRESHOLD, DEFAULT_THRESHOLD_SECONDS)

        # 状態: idle が続き始めた時刻。None なら「連続していない」。
        self._idle_since = None
        # 直近の通知時刻(1回鳴らしたら次の busy を経由するまで再通知しない)。
        self._notified_since = None
        # 直近に見た state(遷移検知用)。
        self._last_state = None

        self._timer = QTimer()
        self._timer.setInterval(int(POLL_INTERVAL_SECONDS * 1000))
        self._timer.timeout.connect(self._on_tick)

        self._notify = None  # IdleNotifyWindow(遅延生成)

        if self._enabled:
            self._timer.start()

    # -- 外部から呼ばれる操作 --------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._save_bool(SETTINGS_ENABLED, self._enabled)
        if self._enabled:
            # 立ち上げ直後は state 未知。次の tick から普通に数え始める。
            self._idle_since = None
            self._notified_since = None
            self._last_state = None
            self._timer.start()
        else:
            self._timer.stop()
            self._hide_notify()

    def is_enabled(self) -> bool:
        return self._enabled

    def threshold_seconds(self) -> int:
        return self._threshold

    def set_threshold(self, seconds: int) -> None:
        seconds = max(5, int(seconds))
        self._threshold = seconds
        self._save_int(SETTINGS_THRESHOLD, seconds)

    def close(self) -> None:
        """常駐終了時に呼ぶ。タイマー停止 + 窓破棄。"""
        try:
            self._timer.stop()
        except Exception:  # noqa: BLE001
            pass
        self._hide_notify()

    # -- タイマー本体 ---------------------------------------------------
    def _on_tick(self) -> None:
        try:
            self._tick()
        except Exception as e:  # noqa: BLE001
            # スロット内で例外が抜けると常駐ごと落ちる(CLAUDE.md)。ここで受ける。
            print(f"[copilot_watchdog] tick 失敗: {e}")

    def _tick(self) -> None:
        # agent-loop が走っているときは休む(応答→次周送信の間の idle を拾わない)。
        if self._is_agent_loop_running():
            self._idle_since = None
            self._notified_since = None
            return

        state, input_value = self._read_copilot_state()
        if state is None:
            # Copilot 窓が無い or 一時的に取れなかった。無音で終わる(false positive しない)。
            self._idle_since = None
            return

        # 「入力待ち」= idle かつ入力欄が空。入力中は待ちではないので数え直す。
        is_idle = state == "idle" and not (input_value or "").strip()

        # busy → idle の遷移で「通知済みフラグ」を外す(次の応答に対しては再通知したい)。
        if self._last_state == "busy" and state != "busy":
            self._notified_since = None
        self._last_state = state

        if not is_idle:
            self._idle_since = None
            return

        now = time.time()
        if self._idle_since is None:
            self._idle_since = now
            return

        elapsed = int(now - self._idle_since)
        if elapsed < self._threshold:
            return
        if self._notified_since is not None:
            # 既に通知済み。busy を挟むまで再通知しない。
            return

        # 通知を出す。窓は遅延生成(常駐起動を軽く保つ)。
        if self._notify is None:
            self._notify = IdleNotifyWindow()
        self._notify.show_with_elapsed(elapsed)
        self._notified_since = now

    def _read_copilot_state(self):
        """Copilot の状態と入力欄の中身を取る。Copilot が居ないなら (None, None)。"""
        try:
            cp = copilot_loop.Copilot()
        except RuntimeError:
            return None, None
        try:
            return cp.state(), cp.read_input()
        except Exception:  # noqa: BLE001
            return None, None

    def _hide_notify(self) -> None:
        if self._notify is not None:
            try:
                self._notify.hide()
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
