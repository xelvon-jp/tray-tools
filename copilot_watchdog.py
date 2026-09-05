# copilot_watchdog.py
# 状態監視バーを「起こす・止める」だけの監督役。
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
# 【このファイルで COM に触れるものを使わないこと。psutil も含む。】
# 取り残しを掃除するために psutil.process_iter(['cmdline']) を呼んだら、それ自体が
# 内部で COM を使っていて常駐が即死した(2026-09-05 13:17)。避けようとしていた当の
# ものを、避けるためのコードで持ち込んでいた。**掃除は子プロセス側の仕事**にしてある
# (copilot_status_process の sweep_other_instances)。
#
# 常駐に残す仕事
# --------------
#   - トレイメニューのチェック状態
#   - 子プロセスの起動と停止(常駐が終わるときは道連れにする)
#   - agent-loop が動いている間は止めておく
#   - 子が自分から終わったら(札の右クリック)、メニューのチェックを外す
import os
import subprocess
import sys

from PySide6.QtCore import QTimer

import settings as settings_module

SETTINGS_SECTION = "copilot_watchdog"
SETTINGS_THRESHOLD = "threshold_seconds"

DEFAULT_THRESHOLD_SECONDS = 30

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "copilot_status_process.py")

# 子の生死を見る間隔(ms)。すぐ起こし直すと、起動直後に落ち続ける状況で無限に
# 再起動を繰り返す。少し置いてから見る。
RESPAWN_CHECK_MS = 5000

# 子が「自分の意思で終わった」ときの終了コード。札の右クリックで終了したとき、
# 常駐が親切に起こし直してしまわないよう、事故で落ちた場合と区別する。
EXIT_BY_USER = 0


def _pythonw():
    """コンソール窓を出さないインタプリタ。capture_process と同じ流儀。"""
    from capture_process import pythonw_executable
    return pythonw_executable()


class CopilotWatchdog:
    """状態監視バーの入切。実処理は子プロセス。

    公開している名前(set_enabled / is_enabled / close など)は、1プロセスで
    動かしていた頃と同じにしてある。feature_screen 側を書き換えずに済ませるため。"""

    def __init__(self, app_settings=None, settings_path=None,
                 is_agent_loop_running=lambda: False, on_child_exit=None):
        self._app_settings = app_settings
        self._settings_path = settings_path
        # agent-loop が回っている間は止めておく。あれが回っているときの手番は
        # 「tray-tools の番」であって、表示している4状態のどれでもない。
        self._is_agent_loop_running = is_agent_loop_running
        # 子が自分から終わったときに、メニューのチェックを外してもらう連絡口。
        self._on_child_exit = on_child_exit

        # **起動時は必ず OFF。** 前回 ON のまま終わっても、次に立ち上げたときに
        # 勝手に札が出ないようにする。常駐の起動は「PC を使い始めるとき」なので、
        # そこで前回の続きを再開されても困ることのほうが多い。
        # 閾値だけは設定から読む(こちらは好みの値で、毎回入れ直したくない)。
        self._enabled = False
        self._threshold = self._load_int(SETTINGS_THRESHOLD, DEFAULT_THRESHOLD_SECONDS)
        self._proc = None

        # agent-loop の出入りに合わせて子を止める/起こす。ついでに、子が落ちて
        # いたら起こし直す。
        self._watch_timer = QTimer()
        self._watch_timer.setInterval(RESPAWN_CHECK_MS)
        self._watch_timer.timeout.connect(self._on_watch)

    # -- 外部から呼ばれる操作 --------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
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
        無くなる(トレイのメニューが消えるため)。

        常駐が「終了」を通らずに落ちた場合の取り残しは、子が自分で親を見張って
        始末する(--parent-pid)。ここで psutil を使って掃除してはいけない。"""
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
        argv = [_pythonw(), SCRIPT_PATH,
                "--threshold", str(self._threshold),
                # 常駐が落ちても札が残らないよう、子に見張らせる。
                "--parent-pid", str(os.getpid())]
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
        """子を木ごと終わらせる。

        【木ごとにする理由】
        venv の Scripts/pythonw.exe は本体のインタプリタを子として起こす中継役で、
        Popen が返すのは中継役の pid。terminate() は中継役しか殺さないので、
        札を出している実体はそのまま残る(消したはずの札が消えない)。

        taskkill を使うのは、木をたどるのに psutil を使いたくないため。あれは
        内部で COM を使っていて、常駐に持ち込むと即死する(このファイル冒頭)。
        taskkill は外部コマンドなので、この常駐に COM を持ち込まない。"""
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[copilot-status] 止められませんでした: {e}", file=sys.stderr)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def _on_watch(self) -> None:
        """agent-loop の出入りに追従し、子が落ちていたら起こし直す。

        ただし子が終了コード0で終わったときは起こし直さない。それは札の右クリックで
        「終了」を選んだ場合で、起こし直すと消したものが即座に戻ってきてしまう。"""
        try:
            if not self._enabled:
                return
            if self._is_agent_loop_running():
                self._stop_child()
                return
            if self._child_alive():
                return
            code = self._proc.poll() if self._proc is not None else None
            if code == EXIT_BY_USER:
                # 札の上で終了された。機能ごと OFF にして、メニューのチェックも外す。
                self._proc = None
                self._enabled = False
                self._watch_timer.stop()
                if self._on_child_exit is not None:
                    self._on_child_exit()
                return
            self._start_child()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると常駐ごと落ちる
            print(f"[copilot-status] 面倒見に失敗: {e}", file=sys.stderr)

    # -- 設定の永続化(snippets.push_recent と同じ流儀) ------------------
    # 保存するのは閾値だけ。入切は保存しない(起動時は必ず OFF)。
    def _load_int(self, key: str, default: int) -> int:
        section = (self._app_settings or {}).get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            return default
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError):
            return default

    def _save_int(self, key: str, value: int) -> None:
        value = int(value)
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
