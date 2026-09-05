# copilot_watchdog.py
# Copilot の手番の常時表示を「起こす・止める」だけの監督役。
# **表示と UIA の実処理は別プロセス(copilot_status_process.py)にある。**
#
# なぜ別プロセスに追い出したのか
# ------------------------------
# **UI Automation(comtypes) と pycaw(comtypes) を同じプロセスに同居させると、
# GC が走った拍子にプロセスごと即死する。** 実測したクラッシュ率:
#
#   Qt + UIA（pycaw なし）      0/10
#   Qt + pycaw + UIA            5/10
#   UIA を先に読む              4/10
#   pycaw を先に読む            3/10
#
# 読み込む順番では避けられない。落ちるのは GC が comtypes の __del__ → Release を
# 呼んだ瞬間で、落ちる場所はトレイアイコンの描画・ピッカーの採寸・設定の保存と毎回
# ばらばら。「たまたま GC が走ったところ」でしかないので症状から原因に辿り着けない。
# 0xC0000005 なので Python の例外にならず error.log にも何も残らない。
#
# 常駐は音声出力の切り替え(pycaw)を持っていて、あちらは外せない。だから UIA を使う
# 側を追い出した。付箋(Rapture)を capture_process.py へ追い出したのと同じ手だが、
# あのときの理由(本体の寿命に巻き込まれない)より強い理由がここにはある。
#
# 【この経緯を知らずに「1プロセスに戻せば単純になる」と考えないこと。】
# 2026-09-04〜05 に15回、常駐が理由不明で即死した原因がこれ。
#
# 常駐に残す仕事
# --------------
#   - トレイメニューのチェック状態と、その設定の保存
#   - 子プロセスの起動と停止(常駐が終わるときは道連れにする)
#   - agent-loop が動いている間は止めておく
# UIA には一切触らない。このファイルは comtypes を import すらしない。
import os
import subprocess
import sys

from PySide6.QtCore import QTimer

import settings as settings_module

SETTINGS_SECTION = "copilot_watchdog"
SETTINGS_ENABLED = "enabled"
SETTINGS_THRESHOLD = "threshold_seconds"

DEFAULT_THRESHOLD_SECONDS = 30

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "copilot_status_process.py")

# 子が落ちていないかを見る間隔(ms)。すぐ起こし直すと、起動直後に落ち続ける状況で
# 無限に再起動を繰り返す。少し置いてから見る。
RESPAWN_CHECK_MS = 5000


def _pythonw():
    """コンソール窓を出さないインタプリタ。capture_process と同じ流儀。"""
    from capture_process import pythonw_executable
    return pythonw_executable()


class CopilotWatchdog:
    """Copilot 状態表示の入切。実処理は子プロセス。

    公開している名前(set_enabled / is_enabled / close など)は、1プロセスで
    動かしていた頃と同じにしてある。feature_screen 側を書き換えずに済ませるため。"""

    def __init__(self, app_settings=None, settings_path=None,
                 is_agent_loop_running=lambda: False):
        self._app_settings = app_settings
        self._settings_path = settings_path
        # agent-loop が回っている間は止めておく。あれが回っているときの手番は
        # 「tray-tools の番」であって、表示している4状態のどれでもない。
        self._is_agent_loop_running = is_agent_loop_running

        self._enabled = self._load_bool(SETTINGS_ENABLED, False)
        self._threshold = self._load_int(SETTINGS_THRESHOLD, DEFAULT_THRESHOLD_SECONDS)
        self._proc = None

        # agent-loop の出入りに合わせて子を止める/起こす。ついでに、子が落ちて
        # いたら起こし直す。
        self._watch_timer = QTimer()
        self._watch_timer.setInterval(RESPAWN_CHECK_MS)
        self._watch_timer.timeout.connect(self._on_watch)

        if self._enabled:
            self._start_child()
            self._watch_timer.start()

    # -- 外部から呼ばれる操作 --------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._save_bool(SETTINGS_ENABLED, self._enabled)
        if self._enabled:
            self._start_child()
            self._watch_timer.start()
        else:
            self._watch_timer.stop()
            self._stop_child()

    def is_enabled(self) -> bool:
        return self._enabled

    def threshold_seconds(self) -> int:
        return self._threshold

    def set_threshold(self, seconds: int) -> None:
        seconds = max(5, int(seconds))
        self._threshold = seconds
        self._save_int(SETTINGS_THRESHOLD, seconds)
        if self._enabled:
            # 子は起動時に閾値を受け取るので、入れ替えるには起こし直す。
            self._stop_child()
            self._start_child()

    def close(self) -> None:
        """常駐終了時に呼ぶ。子を道連れにする。

        付箋(capture_process)は本体が落ちても残ってほしいので DETACHED_PROCESS に
        してあるが、こちらは逆。常駐が終われば札を出し続ける理由が無いし、消す手段も
        無くなる(トレイのメニューが消えるため)。"""
        self._watch_timer.stop()
        self._stop_child()

    # -- 子プロセスの世話 ------------------------------------------------
    def _child_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_child(self) -> None:
        if self._child_alive():
            return
        if self._is_agent_loop_running():
            return
        argv = [_pythonw(), SCRIPT_PATH, "--threshold", str(self._threshold)]
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=os.path.dirname(SCRIPT_PATH),
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        except OSError as e:
            self._proc = None
            print(f"[copilot-status] 起こせませんでした: {e}", file=sys.stderr)

    def _stop_child(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
        except OSError as e:
            print(f"[copilot-status] 止められませんでした: {e}", file=sys.stderr)

    def _on_watch(self) -> None:
        """agent-loop の出入りに追従し、子が落ちていたら起こし直す。"""
        try:
            if not self._enabled:
                return
            if self._is_agent_loop_running():
                self._stop_child()
            elif not self._child_alive():
                self._start_child()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると常駐ごと落ちる
            print(f"[copilot-status] 面倒見に失敗: {e}", file=sys.stderr)

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
            print(f"[copilot-status] 設定保存に失敗: {e}", file=sys.stderr)
