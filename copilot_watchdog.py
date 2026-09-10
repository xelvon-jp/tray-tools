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
#   ※ 閾値も位置もこちらは持たない。子が settings.json と自分の状態ファイルから
#     直接読む(渡し口を2つ持つと、どちらが効いているのか追えなくなる)。
#   - 子が自分から終わったら(札の右クリック)、メニューのチェックを外す
#   ※ 以前は「agent-loop が動いている間は止めておく」もここの仕事だったが、やめた。
#     ループ中こそ様子が知りたいのに、その間だけ札が消えていた(理由は下の
#     _watch_timer のところ)。
#
# エージェントループとの関係
# --------------------------
# ループもこの札も、常駐の子プロセスで、どちらも UIA で Copilot を読む。互いに直接の
# 連絡口は持たない。ループの周回数だけが LOOP_STATUS_FILE を通って札に届く。
# 【1プロセスにまとめようと考える前に、このファイル冒頭の実測値を読むこと。】
import os
import subprocess
import sys

from PySide6.QtCore import QTimer

SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "copilot_status_process.py")

# 子の生死を見る間隔(ms)。すぐ起こし直すと、起動直後に落ち続ける状況で無限に
# 再起動を繰り返す。少し置いてから見る。
RESPAWN_CHECK_MS = 5000

# 子が「自分の意思で終わった」ときの終了コード。札の右クリックで終了したとき、
# 常駐が親切に起こし直してしまわないよう、事故で落ちた場合と区別する。
EXIT_BY_USER = 0

# エージェントループの周回数を状態監視バーに知らせるファイル。
#
# 【なぜここに置くか】
# 書くのは常駐(feature_screen)、読むのは札の子プロセス(copilot_status_process)。
# 常駐から copilot_status_process を import すると UIA(comtypes)が常駐に入り込み、
# pycaw と衝突して 0xC0000005 で即死する(このファイルの冒頭の実測値)。だから
# 双方が安全に import できるこの監督役に、場所の取り決めだけを置く。
#
# 【なぜファイルか】
# 札とループはどちらも常駐の子で、互いに直接の連絡口を持たない。札は150msごとに
# 追従処理を回しているので、そこで1つファイルを見るのがいちばん安い。
LOOP_STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "copilot_loop_status.json")


def _pythonw():
    """コンソール窓を出さないインタプリタ。capture_process と同じ流儀。"""
    from capture_process import pythonw_executable
    return pythonw_executable()


class CopilotWatchdog:
    """状態監視バーの入切。実処理は子プロセス。

    公開している名前(set_enabled / is_enabled / close など)は、1プロセスで
    動かしていた頃と同じにしてある。feature_screen 側を書き換えずに済ませるため。"""

    def __init__(self, app_settings=None, settings_path=None,
                 on_child_exit=None):
        self._app_settings = app_settings
        self._settings_path = settings_path
        # 子が自分から終わったときに、メニューのチェックを外してもらう連絡口。
        self._on_child_exit = on_child_exit

        # **起動時は必ず OFF。** 前回 ON のまま終わっても、次に立ち上げたときに
        # 勝手に札が出ないようにする。常駐の起動は「PC を使い始めるとき」なので、
        # そこで前回の続きを再開されても困ることのほうが多い。
        self._enabled = False
        self._proc = None

        # 子が落ちていたら起こし直す。
        #
        # 【以前はここで agent-loop の出入りも見ていた】
        # ループが回っている間は札を止める作りだった。「そのときの手番は
        # tray-tools の番で、表示している4状態のどれでもない」という理屈だったが、
        # 実際に困るのは逆だった。ループは何分も回ることがあり、その間ずっと札が
        # 消えるので、**進んでいるのか固まったのかが分からない時間が一番長い**。
        # いまは札に周回数(🤖 3/10)を出すので、手番が tray-tools にあることは
        # その姿で分かる。止める理由が無くなった。
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
        # 閾値は子が settings.json から直接読む。渡し口を2つ持つと、どちらが
        # 効いているのか追えなくなる。
        argv = [_pythonw(), SCRIPT_PATH,
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
        """子が落ちていたら起こし直す。

        ただし子が終了コード0で終わったときは起こし直さない。それは札の右クリックで
        「終了」を選んだ場合で、起こし直すと消したものが即座に戻ってきてしまう。"""
        try:
            if not self._enabled:
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
