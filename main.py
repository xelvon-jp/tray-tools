# main.py
# tray-tools エントリポイント。QApplication起動・設定読込・Feature登録・ホットキー登録を行う。
#
# Featureの定義(ダックタイピングで十分なので抽象基底クラスは作らない):
#   Feature = トレイアイコンを1つ所有するもの。
#   - コンストラクタ __init__(self, app_settings, settings_path) で QSystemTrayIcon を
#     1つ構築して self.tray_icon に保持する
#   - hotkeys() メソッドで {"設定キー名": 呼び出す関数} の辞書を返す(不要なら空辞書)
#
# 機能を足すときにFeatureを増やすのは「アイコンの見た目を占有する状態」を持つ場合だけに限る。
# 単発の動作はアイコンを増やさず、既存Featureのメニュー項目にする。アイコンを持たない能力は
# 普通のモジュール(color_picker.py / keep_awake.py など)として書き、Featureがそれを呼ぶ。
import ctypes
import faulthandler
import gc
import json
import os
import subprocess
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

import action_log
import beep
import capture_grab
import pushover
import settings as settings_module
from feature_audio import AudioFeature
from feature_screen import ScreenFeature
from hotkeys import init_keyboard, setup_hotkeys
from toast import show_toast
# 窓の出ない実行ファイルの割り出しと、親から切り離して起動するフラグを再起動でも使う。
from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

# トレイアイコンは音声用と画面用の2つだけ。増やさない方針(上のFeatureの定義を参照)。
FEATURE_CLASSES = [AudioFeature, ScreenFeature]

# 多重起動の検出に使うローカルソケット名。スタートアップからの自動起動に加えて
# ショートカットを手動で叩くと二重に立ち上がり、トレイアイコンが2組並ぶうえ
# グローバルホットキーが二重登録されて競合するため防ぐ。
#
# この待ち受けは外部からのコマンド受付も兼ねる(Windowsでは \\.\pipe\<この名前> という
# 名前付きパイプになる)。接続してきた側が {"command": ..., "args": [...]} のUTF-8 JSONを
# 1行書けば、常駐中のこちらがその機能を開く。あふｗから呼ぶ traytools_send.py がその客。
# 何も書かずに切れた場合は従来どおり「二重起動しようとした」とみなす。
SINGLE_INSTANCE_KEY = "traytools.single-instance"

# コマンドの読み取り待ち時間(ms)。相手は接続直後に1行書いて終わる作りなので短くてよい。
# ここで待ちすぎると、ただの二重起動のときにトーストが遅れる。
COMMAND_READ_TIMEOUT_MS = 300

# ---------------------------------------------------------------
# 応答(この口は双方向)
# ---------------------------------------------------------------
# 以前この口は一方向で、送った側は結果を受け取れなかった。予約が入ったのか引数を
# 間違えて無視されたのかが分からず、「エラーが出ていないから多分成功」という確かめ方しか
# できなかった(action_log.py 冒頭を参照)。いまは1接続につき1つ応答を返す。
#
# 【応答の形式】UTF-8 のテキスト。
#   1行目 : "OK" で始まれば成功、"ERR" で始まれば失敗。後ろに1行の要約が付くことがある
#   2行目〜: 本文(status / log のように中身を返すコマンドだけ)
#   最後まで書いたら切断するので、受け取る側はEOFまで読めばよい
#
# 応答を読まずに切る客(この機能を足す前の traytools_send.py、あふｗからの呼び出し)が
# 居ても壊れないこと。QLocalSocket への書き込みは相手が居なくても例外にならず -1 を
# 返すだけなので、こちらは何事もなく次へ進む。
REPLY_ENCODING = "utf-8"
# 応答を書き切るまで待つ上限(ms)。名前付きパイプ相手なので普通は一瞬で終わる。
REPLY_WRITE_TIMEOUT_MS = 1000
# ハンドラが応答を返さないまま放置した接続を回収するまでの時間(ms)。
# Pushover の送信(最長10秒)より長くしておくこと。
REPLY_DEADLINE_MS = 20000
# 応答を書いたあと、切断が完了しなかった場合に後片付けするまでの猶予(ms)。
REPLY_CLOSE_GRACE_MS = 2000

# 応答待ちの接続。ローカル変数だけで持つとGCで消え、応答が返らなくなる。
_pending_replies = set()

# log コマンドが一度に返す行数の上限。ここを無制限にすると、うっかり大きな数を
# 渡したときに応答が数百KBになる(パイプの向こうで詰まる)。
MAX_LOG_LINES = 500
DEFAULT_LOG_LINES = 20

# notify で出すトーストの上限。長い文字列がそのまま来ると画面いっぱいの板になる。
MAX_NOTIFY_CHARS = 300

# 通常起動(TrayTools.lnk)は pythonw.exe なので、コンソールに出した例外は誰も見られない。
# 落ちた理由を後から追えるようにファイルへ残す。
ERROR_LOG_PATH = Path(__file__).resolve().parent / "error.log"
# ctypes/COM の先で落ちた瞬間のスタック。error.log と分けるのは、こちらが
# シグナルハンドラから書かれるため(通常のログと混ざると読みにくい)。
CRASH_LOG_PATH = Path(__file__).resolve().parent / "crash.log"
_crash_log_file = None


def log_exception(where: str) -> str:
    """直前の例外をログに追記し、トースト用の短い1行を返す。

    ログを書けない状況(ディスク不調など)でも、ここで新たな例外を投げないこと。
    そもそも例外処理の途中で呼ばれる関数なので、ここで落ちると元の原因が消える。"""
    text = traceback.format_exc()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {where} =====\n{text}")
    except OSError:
        pass
    print(text, file=sys.stderr)
    exc_type, exc_value = sys.exc_info()[:2]
    return f"{exc_type.__name__}: {exc_value}" if exc_type else "unknown error"


# 先着を決める名前付きミューテックス。Local\\ 配下はログオンセッションごとに分かれるので、
# 別ユーザーが同時に使っても互いを弾かない。
SINGLE_INSTANCE_MUTEX = r"Local\traytools.single-instance"
ERROR_ALREADY_EXISTS = 183

# 握っているハンドル。プロセスが終わればOSが必ず手放すので、後始末を書き忘れても
# ロックが残り続けることはない(前回クラッシュ後に起動できなくなる、が起きない)。
_instance_mutex = None


def _acquire_single_instance() -> bool:
    """先着なら True。すでに誰か居るなら False。

    【なぜ QLocalServer で判定しないのか】
    元は「待ち受けに接続できるか調べる → 誰も居なければ自分が listen する」だった。
    これには2つ穴がある。

    1. **検査と確保の間に隙間がある。** 同時に起動した2つが両方「誰も居ない」と
       判断して、両方とも起動してしまう。実際 17ms 差で2つ立ち上がっていた
       (トレイの再起動と、待ち受けが消えている隙に traytools_send が本体を
        起こす経路が重なると起きる)。
    2. **Windows では listen が排他にならない。** 名前付きパイプは同じ名前で
       複数のサーバインスタンスを作れる。実測でも、別プロセスが待ち受けている
       名前に対して listen() が True を返した。つまり「立てられたから自分が
       先着」という判定自体が成り立たない。

    CreateMutexW は作成と「既にあったか」の判定が1回のシステムコールで済むので、
    隙間が無い。判定に使えるのはこちら。"""
    global _instance_mutex
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except OSError:
        # 判定できないなら起動を通す。ロック機構の不調でアプリが使えなくなる方が困る。
        return True
    if not handle:
        return True
    if already:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _instance_mutex = handle
    return True


def _release_single_instance() -> None:
    """先着の権利を手放す。再起動で自分の後継を起こす直前に呼ぶ。"""
    global _instance_mutex
    if _instance_mutex:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_instance_mutex))
        except OSError:
            pass
        _instance_mutex = None


def _start_command_listener():
    """外部コマンドを受ける待ち受けを立てる。戻り値は呼び出し側で参照を保持すること。

    **これは単一起動の判定には使わない**(上の理由で判定にならない)。あふｗ・フック・
    MCP から来るコマンドを受けるためだけの口。listen に失敗しても起動は止めない
    (外部コマンドが使えなくなるだけで、常駐そのものは使えるため)。"""
    # 前回クラッシュで終わるとソケットが残ることがある(Unix系)。先着なのは
    # ミューテックスで確認済みなので、残骸は消してから立ててよい。
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)
    return server


def _read_command(socket):
    """接続してきた相手が書いた1行のJSONを (command, args) で返す。読めなければ (None, [])。

    素の名前付きパイプから書かれるため、行の途中で届くことも複数行来ることもありうる。
    最初の1行だけを見て、残りは捨てる(コマンドは1接続1つと決めている)。"""
    if not socket.waitForReadyRead(COMMAND_READ_TIMEOUT_MS):
        return None, []
    raw = bytes(socket.readAll().data())
    try:
        payload = json.loads(raw.decode("utf-8").splitlines()[0])
        command = payload["command"]
    except (UnicodeDecodeError, ValueError, IndexError, TypeError, KeyError) as e:
        print(f"[tray-tools] コマンドを解釈できません: {e}", file=sys.stderr)
        return None, []
    args = payload.get("args") or []
    return command, args if isinstance(args, list) else []


class _Reply(QObject):
    """1接続ぶんの応答を書いて切る係。1回だけ書けて、2回目以降は黙って捨てる。

    呼び出しはシグナルを経由する。Pushover の送信のようにネットワークを待つ処理は
    別スレッドで走らせる(Qtのメインスレッドを塞ぐと常駐全体が固まる)ので、応答を
    出すのが別スレッドになりうる。ソケットに触ってよいのは作ったスレッドだけなので、
    ここで必ずメインスレッドへ渡し直す(この QObject はメインスレッドで作られるため、
    別スレッドからの emit はキュー接続になる)。"""

    ready = Signal(str)

    def __init__(self, socket):
        super().__init__()
        self._socket = socket
        self._finished = False
        self._sent = False
        self.ready.connect(self._write)
        # ハンドラが応答を返さないまま忘れても、接続を開いたままにしない。
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._on_deadline)
        self._deadline.start(REPLY_DEADLINE_MS)
        _pending_replies.add(self)

    def __call__(self, text: str) -> None:
        """応答を出す。別スレッドから呼んでよい唯一の入口。"""
        self.ready.emit(text)

    def _on_deadline(self) -> None:
        self._write("ERR 応答が返りませんでした（タイムアウト）")

    def _write(self, text: str) -> None:
        if self._sent:
            return  # 1接続1応答。重ねて呼ばれても最初の1つだけ
        self._sent = True
        self._deadline.stop()
        try:
            data = ((text or "OK").rstrip("\r\n") + "\n").encode(REPLY_ENCODING, "replace")
            # 応答を読まずに切る客が居る。相手が既に居なくても write は例外を投げず
            # -1 を返すだけなので、戻り値は見ずにそのまま切断へ進む。
            self._socket.write(data)
            self._socket.flush()
            self._socket.waitForBytesWritten(REPLY_WRITE_TIMEOUT_MS)
            self._socket.disconnectFromServer()
        except Exception:
            log_exception("応答の書き込み")
        if self._socket.state() == QLocalSocket.UnconnectedState:
            self._finish()
        else:
            # 書き終わるまで待ってから切れる(disconnectFromServer の作法)。
            # それでも切れないときのために猶予付きで後片付けする。
            self._socket.disconnected.connect(self._finish)
            QTimer.singleShot(REPLY_CLOSE_GRACE_MS, self._finish)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self._socket.close()
        except Exception:
            pass
        _pending_replies.discard(self)
        self.deleteLater()


def _handle_connection(server, command_handlers):
    socket = server.nextPendingConnection()
    if socket is None:
        return
    # ソケットはここでは閉じない。応答を書いてから切るのは _Reply の仕事で、
    # ハンドラが別スレッドを使う場合はこの関数を抜けたあとに書かれる。
    reply = _Reply(socket)
    try:
        command, args = _read_command(socket)
    except Exception:
        summary = log_exception("コマンドの読み取り")
        reply(f"ERR {summary}")
        return

    if command is None:
        # 何も書かずに切れた＝コマンドではなく二重起動。2つ目の起動は黙って終わるだけ
        # なので、「クリックしたのに何も起きない」と見えないようこちらから知らせる。
        show_toast("tray-tools\nすでに起動しています")
        reply("ERR コマンドが書かれませんでした（二重起動として扱いました）")
        return

    handler = command_handlers.get(command)
    if handler is None:
        show_toast(f"tray-tools\n知らないコマンドです\n{command}")
        reply(f"ERR 知らないコマンドです: {command}\n使えるのは: "
              + " / ".join(sorted(command_handlers)))
        return

    # ここは外部(あふｗ等)から叩かれる入口。Qtのスロット内で例外を投げ切ると
    # 常駐アプリごと落ちるため、必ず受け止める。pythonw起動では標準エラーが
    # どこにも出ないので、内容はトーストと error.log の両方に残す。
    try:
        handler(args, reply)
    except Exception:
        summary = log_exception(f"command={command} args={args}")
        show_toast(f"tray-tools\nコマンドの実行に失敗しました\n{summary}")
        reply(f"ERR {summary}")


def _build_command_handlers(features) -> dict:
    """外部から叩けるコマンドの表を作る。値は (args, reply) を受け取る関数。

    reply は1接続につき1回だけ呼べる応答の口(_Reply)。同期で済むコマンドはその場で
    呼び、Pushover のように待ちの入るものは別スレッドの終わりに呼ぶ。

    【この表を増やすときの線引き】
    ここは外から叩ける口で、増やすほど誤操作の被害が広がる。足してよいのは
    「常駐しているこのプロセスでなければできないこと」だけ。アプリの起動・ファイル操作の
    たぐいは足さない——呼ぶ側が直接できるので、わざわざ常駐を経由させる利点が無く、
    口を通る危険だけが増える。

    ウインドウの参照は main() ではなく ScreenFeature に持たせている。同じピッカーを
    トレイメニューとホットキーからも開けるので、「開いていたら前面に呼び戻す」判定も
    含めて開閉の管理は1か所(ScreenFeature)に置きたい。main() 側にリストを持つと
    同じ窓を二重に開けてしまう。"""
    screen = next((f for f in features if isinstance(f, ScreenFeature)), None)
    if screen is None:
        return {}
    return {
        # あふｗから $P(カレントパス)を渡して呼ぶ。パスが無ければ登録なしで一覧だけ出す。
        "bookmark": lambda args, reply: _bookmark_command(screen, args, reply),
        # スリープ抑止の入切。長い処理を外から回すとき、始める前に掛けて終わったら
        # 外す、という使い方ができる(席を外している間に寝られると処理が止まるため)。
        #
        #   traytools_send.py keep-awake on        無期限
        #   traytools_send.py keep-awake on 90     90分だけ
        #   traytools_send.py keep-awake off       解除
        "keep-awake": lambda args, reply: reply(_keep_awake_command(screen, args)),
        # PCをスリープさせる。長い処理を回し終えたあとに寝かせる用。
        #
        #   traytools_send.py sleep                    すぐ(猶予のあと)
        #   traytools_send.py sleep 1800               30分後
        #   traytools_send.py sleep 1800 hibernate     30分後に休止状態
        #   traytools_send.py sleep cancel             予約を取り消す
        #
        # 予約の時刻になってもすぐには寝ず、声を掛けてから少し待つ。予約を忘れて
        # 作業している最中に落ちると、開いているものが道連れになるため。
        "sleep": lambda args, reply: reply(_sleep_command(screen, args)),
        # いま何が起きているかを返す。抑止・予約・ミラー・付箋の枚数。
        #
        #   traytools_send.py status
        "status": lambda args, reply: reply("OK\n" + screen.status_text()),
        # 操作ログの末尾を読む。既定20行、引数で行数を指定できる。
        #
        #   traytools_send.py log 50
        "log": lambda args, reply: reply(_log_command(args)),
        # トーストを1枚出す。長い処理の節目を知らせる用。
        #
        #   traytools_send.py notify "変換が終わりました"
        "notify": lambda args, reply: reply(_notify_command(args)),
        # 音を1つ鳴らす。トーストより強い合図(画面を見ていなくても気づける)。
        #
        #   traytools_send.py beep done
        "beep": lambda args, reply: reply(_beep_command(args)),
        # スマホへプッシュ通知。席を外していても届く。
        #
        #   traytools_send.py pushover "変換が終わりました" --title 変換 --priority 1
        "pushover": lambda args, reply: _pushover_command(args, reply),
        # 画面を1枚撮ってファイルに保存し、そのパスを返す。
        #
        #   traytools_send.py capture        画面全体(仮想デスクトップ)
        #   traytools_send.py capture 2      2番目の画面だけ
        #
        # 「常駐でなければできないこと」に当たるので載せている。mss のインスタンスを
        # 都度作ると COM/GC 衝突で落ちるため(2026-08-28 に8回)、必ず常駐が抱えている
        # capture_grab._sct() を通す必要がある。呼ぶ側が自前で撮る道は無い。
        #
        # 画像そのものは返さない。この口の応答は1行のテキストで、画像を載せるなら
        # base64 で数MBを流すことになる。保存してパスを返せば、読む側は普通に
        # ファイルを開けばよい(保存先は capture.save_folder と同じ)。
        "capture": lambda args, reply: reply(_capture_command(screen, args)),
        # Copilot アプリを相手にした疑似エージェントループ。
        #
        #   traytools_send.py agent-loop start <お題ファイル> [--auto] [--max N]
        #   traytools_send.py agent-loop status
        #   traytools_send.py agent-loop cancel
        #   traytools_send.py agent-loop stop-if-cancelled  （内部用）
        #
        # 【なぜ常駐に載せるのか】
        # - 1周ぶん十数秒 × 数周 = 数分の非同期処理を持ち回るのに、外の呼び出し側で
        #   待つ形にすると使い勝手が悪い。常駐に投げっぱなしにできると便利。
        # - status で「回っているか」を人にもエージェントにも見せられる。
        # - cancel は「置きっぱなしのフラグ」だけで済むので、実行スレッドの応答性に
        #   関係なく効く(実行スレッドが PowerShell の完了待ちで詰まっていても、
        #   次の周の頭で拾って止まる)。
        "agent-loop": lambda args, reply: _agent_loop_command(args, reply),
    }


def _bookmark_command(screen, args, reply) -> None:
    screen.start_launcher(args[0] if args else None)
    reply("OK フォルダブックマークを開きました")


def _sleep_command(screen, args) -> str:
    """外から来た sleep の引数を解いて呼び分ける。想定外は「何もしない」に倒す。

    戻り値は応答の1行。「無視された」のか「効いた」のかを送った側が区別できるよう、
    何もしなかったときは ERR で返す(動作は従来どおり何もしない)。"""
    first = (args[0] if args else "").strip().lower()
    if first in ("cancel", "off", "stop", "abort"):
        screen.cancel_sleep(source="external")
        return "OK " + screen.sleep_status()
    # 2つめに hibernate と書けば休止状態。書かなければスリープ。
    hibernate = any(str(a).strip().lower() in ("hibernate", "hiber") for a in args[1:])
    seconds = 0
    if first and first not in ("hibernate", "hiber"):
        try:
            seconds = max(0, int(first))
        except (TypeError, ValueError):
            return f"ERR 秒数を解釈できません: {first}（何もしませんでした）"
    elif first in ("hibernate", "hiber"):
        hibernate = True
    screen.schedule_sleep(seconds, hibernate, source="external")
    return "OK " + screen.sleep_status()


def _keep_awake_command(screen, args) -> str:
    """外から来た keep-awake の引数を解いて呼び分ける。

    引数を間違えても落とさない。外から叩かれる口なので、想定外が来ても
    「何もしない」に倒すほうがよい。戻り値は応答の1行で、掛け終わった実際の状態を返す
    (頼んだとおりになったかは、送った側がこれで確かめられる)。"""
    mode = (args[0] if args else "on").strip().lower()
    if mode in ("off", "0", "false", "stop", "disable"):
        screen.disable_keep_awake(source="external")
        return "OK " + screen.keep_awake_status()
    minutes = None
    if len(args) > 1:
        try:
            minutes = max(1, int(args[1]))
        except (TypeError, ValueError):
            minutes = None
    screen.enable_keep_awake(minutes, source="external")
    return "OK " + screen.keep_awake_status()


def _capture_command(screen, args) -> str:
    """画面を1枚撮って保存し、そのパスを返す。args[0] があれば画面番号(1始まり)。"""
    screens = QApplication.screens()
    if not screens:
        return "ERR 画面が見つかりません"

    if args:
        try:
            index = int(str(args[0]).strip())
        except (TypeError, ValueError):
            return f"ERR 画面番号を解釈できません: {args[0]}"
        if not 1 <= index <= len(screens):
            return f"ERR 画面番号は 1〜{len(screens)} です（指定: {index}）"
        rect = screens[index - 1].geometry()
        label = f"画面{index}"
    else:
        rect = capture_grab.virtual_geometry()
        label = f"画面全体({len(screens)}面)"

    try:
        # include_layered=True で半透明のウィンドウも写す(通知やメニューを撮りたい)。
        image = capture_grab.grab_region(rect, include_layered=True)
    except Exception as e:  # noqa: BLE001  外部から叩かれる口。落とさない
        return f"ERR 撮影できません: {e}"
    if image.isNull():
        return "ERR 撮影できませんでした（空の画像）"

    path = capture_grab.save_image(
        image, screen.app_settings.get("capture", {}), stem=capture_grab.new_session_stem()
    )
    if not path:
        return "ERR 保存できませんでした（保存先を確認してください）"

    action_log.record("画面キャプチャ", f"{label} {image.width()}x{image.height()}", "external")
    return f"OK {label} {image.width()}x{image.height()}\n{path}"


def _log_command(args) -> str:
    """操作ログの末尾を返す。行数は args[0](省略時20行)。"""
    lines = DEFAULT_LOG_LINES
    if args:
        try:
            lines = int(str(args[0]).strip())
        except (TypeError, ValueError):
            return f"ERR 行数を解釈できません: {args[0]}"
        if lines <= 0:
            return f"ERR 行数は1以上にしてください: {lines}"
        lines = min(lines, MAX_LOG_LINES)
    body = action_log.tail(lines)
    if not body.strip():
        return "OK 操作ログはまだ空です"
    return f"OK 直近{lines}行\n" + body.rstrip("\n")


def _notify_command(args) -> str:
    """トーストを1枚出す。引数は繋いで1つの本文にする。

    引数の中の \\n(円記号とn の2文字)は改行に直す。あふｗやバッチから渡すときに
    本物の改行を通すのは面倒なため。"""
    text = " ".join(str(a) for a in args).strip()
    if not text:
        return "ERR 本文がありません（何も出しませんでした）"
    text = text.replace("\\n", "\n")[:MAX_NOTIFY_CHARS]
    show_toast(text)
    action_log.record("通知", text.splitlines()[0][:40], "external")
    return "OK 通知を出しました"


def _beep_command(args) -> str:
    """音を1つ鳴らす。知らない名前なら鳴らさずに ERR。"""
    kind = (str(args[0]).strip() if args else beep.DEFAULT_KIND)
    if not beep.play(kind):
        return f"ERR 知らない音です: {kind}（{beep.kind_names()} のどれか）"
    return f"OK 鳴らしました: {kind}"


def _parse_pushover_args(args):
    """pushover の引数を (本文, オプション辞書) に解く。解けなければ (None, 理由)。

    --title / --priority / --sound を抜いた残りを繋いで本文にする。位置引数で
    タイトルを受けると「本文の続き」と見分けが付かないため、明示の旗にしている。"""
    options = {"title": None, "priority": None, "sound": None}
    words = []
    rest = list(args)
    while rest:
        word = str(rest.pop(0))
        key = word[2:].lower() if word.startswith("--") else None
        if key in options:
            if not rest:
                return None, f"{word} の値がありません"
            options[key] = str(rest.pop(0))
        elif word.startswith("--"):
            return None, f"知らないオプションです: {word}"
        else:
            words.append(word)
    message = " ".join(words).strip().replace("\\n", "\n")
    if not message:
        return None, "本文がありません"
    return message, options


def _pushover_command(args, reply) -> None:
    """スマホへプッシュ通知を送る。送信は別スレッドで行う。

    ネットワークは数秒かかりうる。Qtのメインスレッドで待つと、その間トレイも付箋も
    ホットキーも全部固まる。応答は送信が終わってから reply() で返す(_Reply が
    メインスレッドへ渡し直してくれるので、ワーカーから呼んで安全)。"""
    message, options = _parse_pushover_args(args)
    if message is None:
        reply(f"ERR {options}（送信しませんでした）")
        return
    if not pushover.is_registered():
        # 未登録のまま送りにいっても弾かれるだけ。通信する前に断る。
        reply("ERR Pushover のトークンが登録されていません"
              "（トレイメニューの「スマホ通知（Pushover）」から登録してください）")
        return

    def work():
        # ここはワーカースレッド。Qtのウィジェットには絶対に触らないこと。
        ok, detail = pushover.send(
            message,
            title=options["title"],
            priority=options["priority"],
            sound=options["sound"],
        )
        reply(("OK " if ok else "ERR ") + detail)

    threading.Thread(target=work, name="pushover-send", daemon=True).start()


# ---------------------------------------------------------------------------
# 疑似エージェントループ(Copilot アプリを相手に回す)
# ---------------------------------------------------------------------------
# 走っているスレッドを1本だけ持つ。2本同時に走らせない(Copilot は1つしか無い)。
# 完了・失敗時のサマリはここに載せて status で読める状態にする。
_agent_loop_state = {"thread": None, "started": None, "prompt": None,
                     "auto": False, "summary": None}


def _agent_loop_running() -> bool:
    thread = _agent_loop_state.get("thread")
    return thread is not None and thread.is_alive()


def _agent_loop_status_text() -> str:
    if _agent_loop_running():
        started = _agent_loop_state["started"] or ""
        return (f"agent-loop 実行中 (開始 {started}, "
                f"auto={_agent_loop_state['auto']}, "
                f"prompt={_agent_loop_state['prompt']})")
    summary = _agent_loop_state.get("summary")
    if summary:
        return (f"直近: 停止理由={summary.get('stopped_by')} "
                f"周回={summary.get('rounds')} "
                f"経過={summary.get('elapsed')}秒 "
                f"詳細={summary.get('detail', '')}")
    return "agent-loop 未実行"


def _agent_loop_command(args, reply) -> None:
    """外部から agent-loop を操作する。start/status/cancel。

    実処理は別スレッドで走らせる。Qtメインスレッドを塞ぐと常駐全体が固まるうえ、
    ループ本体は Copilot への送信・PowerShell 実行で数分ブロックしうる。"""
    action = (args[0].strip().lower() if args else "status")

    if action == "status":
        reply("OK " + _agent_loop_status_text())
        return

    if action == "cancel":
        # ファイルを置くだけ。実行スレッドが次の周の頭で拾って止まる。
        # 実行スレッドが PowerShell で詰まっていても、これは即座に効く。
        try:
            import agent_loop as al
            al.request_cancel()
            reply("OK agent-loop にキャンセルを要求しました（次の周の頭で止まります）")
        except Exception as e:  # noqa: BLE001
            reply(f"ERR キャンセル要求できませんでした: {e}")
        return

    if action != "start":
        reply(f"ERR 不明な agent-loop 指示: {action}（start / status / cancel）")
        return

    if _agent_loop_running():
        reply("ERR agent-loop は既に走っています（先に cancel してください）")
        return

    # --- start の引数解析 ---
    # traytools_send.py agent-loop start <お題ファイル> [--auto] [--max N]
    #                                                  [--ps-timeout N] [--response-timeout N]
    #                                                  [--paste-limit N] [--finish-word W]
    if len(args) < 2:
        reply("ERR お題ファイルのパスを指定してください")
        return
    prompt_path = args[1]
    if not os.path.exists(prompt_path):
        reply(f"ERR お題ファイルが見つかりません: {prompt_path}")
        return

    opts = {"auto": False, "max_rounds": None, "ps_timeout": None,
            "response_timeout": None, "paste_limit": None,
            "finish_word": "", "loop_timeout": None}
    i = 2
    tail = args[2:]
    for i, tok in enumerate(tail):
        if tok == "--auto":
            opts["auto"] = True
        elif tok.startswith("--max=") or tok.startswith("--max-rounds="):
            opts["max_rounds"] = int(tok.split("=", 1)[1])
        elif tok.startswith("--ps-timeout="):
            opts["ps_timeout"] = int(tok.split("=", 1)[1])
        elif tok.startswith("--response-timeout="):
            opts["response_timeout"] = int(tok.split("=", 1)[1])
        elif tok.startswith("--paste-limit="):
            opts["paste_limit"] = int(tok.split("=", 1)[1])
        elif tok.startswith("--finish-word="):
            opts["finish_word"] = tok.split("=", 1)[1]
        elif tok.startswith("--loop-timeout="):
            opts["loop_timeout"] = int(tok.split("=", 1)[1])
        # 未知のオプションは黙って飛ばす(古い呼び出しが将来の版で落ちないため)

    def work():
        # ここはワーカースレッド。Qtのウィジェットには絶対に触らないこと。
        try:
            import agent_loop as al  # 遅延 import。常駐の起動時間を伸ばさないため
            prompt = open(prompt_path, encoding="utf-8-sig").read().strip()
            kwargs = {"initial_prompt": prompt, "auto_run": opts["auto"]}
            for key in ("max_rounds", "ps_timeout", "response_timeout",
                        "paste_limit", "loop_timeout"):
                if opts[key] is not None:
                    kwargs[key] = opts[key]
            if opts["finish_word"]:
                kwargs["finish_word"] = opts["finish_word"]
            _agent_loop_state["summary"] = None
            summary = al.run_loop(**kwargs)
            _agent_loop_state["summary"] = summary
            action_log.record("agent-loop 終了",
                              f"{summary.get('stopped_by')} 周回{summary.get('rounds')} "
                              f"経過{summary.get('elapsed')}秒",
                              "external")
        except Exception as e:  # noqa: BLE001
            _agent_loop_state["summary"] = {
                "stopped_by": "error", "detail": str(e),
                "rounds": 0, "elapsed": 0,
            }
            action_log.record("agent-loop 失敗", str(e)[:80], "external")

    _agent_loop_state["thread"] = threading.Thread(
        target=work, name="agent-loop", daemon=True)
    _agent_loop_state["started"] = datetime.now().strftime("%H:%M:%S")
    _agent_loop_state["prompt"] = os.path.basename(prompt_path)
    _agent_loop_state["auto"] = opts["auto"]
    _agent_loop_state["thread"].start()

    action_log.record("agent-loop 開始",
                      f"{os.path.basename(prompt_path)} auto={opts['auto']}",
                      "external")
    reply(f"OK agent-loop を開始しました "
          f"(auto={opts['auto']}, prompt={os.path.basename(prompt_path)})")


def _restart(instance_lock) -> bool:
    """自分を起動し直す。新しい方を起こせたら True(呼んだ側がこのプロセスを終わらせる)。

    先に待ち受けと先着の権利を手放すのは、新しい方が起動直後に「すでに起動しています」と
    判断して引き返してしまうため。逆に、起こすのに失敗したときは張り直して生き残る。
    再起動できないうえ常駐まで消えると、手で起動し直すしかなくなるため。"""
    instance_lock.close()
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    _release_single_instance()
    try:
        subprocess.Popen(
            [pythonw_executable(), str(Path(__file__).resolve())],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        print(f"[tray-tools] 再起動できません: {e}", file=sys.stderr)
        _acquire_single_instance()
        instance_lock.listen(SINGLE_INSTANCE_KEY)
        return False
    return True


def _wire_restart(features, instance_lock) -> None:
    """再起動をトレイメニューから呼べるようにする。

    待ち受け(instance_lock)を握っているのは main() なので、Featureのコンストラクタでは
    渡せない。Featureが揃ってからここで渡す(_wire_taskbar_widget と同じ形)。"""
    screen = next((f for f in features if isinstance(f, ScreenFeature)), None)
    if screen is None:
        return
    screen.attach_restart(lambda: _restart(instance_lock))


def _wire_taskbar_widget(features) -> None:
    """各ディスプレイのタスクバーに置くウィジェットを ScreenFeature に組み立てさせる。

    あれは通知領域そのものの代わりなので、画面側と音声側の両方(アイコンの絵・デバイス
    切替・それぞれのメニュー)を呼ぶ。Featureは1つずつ構築されるため、コンストラクタの
    中では相手がまだ居ない。全部そろったここで渡す(_build_command_handlers が features を
    受け取って引き当てているのと同じ形)。

    ウィジェットの参照は main() ではなく ScreenFeature が持つ。表示のON/OFFはトレイ
    メニューからも切り替えるので、開閉の管理は1か所に置きたい。"""
    screen = next((f for f in features if isinstance(f, ScreenFeature)), None)
    audio = next((f for f in features if isinstance(f, AudioFeature)), None)
    if screen is None or audio is None:
        return
    screen.attach_audio_feature(audio)


def _install_crash_log():
    """ctypes/COM の先で落ちたときに、その瞬間のPythonスタックを残す。

    _install_excepthook が拾えるのは Python の例外だけで、ctypes を経由した先の
    アクセス違反(0xC0000005)やコールバック内の致命的例外(0xC000041D)は例外にならず
    プロセスが即死する。実際、Windowsのイベントログには _ctypes.pyd での 0xC0000005 が
    何度も記録されているのに error.log には何も残っていなかった。

    faulthandler はシグナルハンドラの中から直接書き出すので、この状況でもスタックが
    残る。ファイルは開いたまま保持する必要があるので、モジュールに掴んでおく
    (閉じるとハンドラの書き込み先が無くなる)。all_threads=True にするのは、
    keyboard のフックが専用スレッドで動くため。"""
    global _crash_log_file
    try:
        _crash_log_file = open(CRASH_LOG_PATH, "a", encoding="utf-8", buffering=1)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _crash_log_file.write(os.linesep.join(["", "===== " + stamp + " 起動 =====", ""]))
        faulthandler.enable(file=_crash_log_file, all_threads=True)
    except OSError as e:
        print(f"[tray-tools] クラッシュログを開けません: {e}", file=sys.stderr)


def _install_excepthook():
    """どこにも捕まらなかった例外を error.log に残す。

    PySide6 はスロットから例外が抜けるとプロセスを終わらせるので、これで落ちるのを
    防げるわけではない。落ちた「理由」を後から読めるようにするための保険。
    防ぎたい箇所は個別に try で受け止めること(_handle_connection がその例)。

    sys.excepthook はメインスレッドしか見ない。keyboard のフックは専用スレッドで
    動くので、threading.excepthook も同じ宛先に向けておく(そうしないと、ホットキー
    まわりで落ちたときに何も残らない)。"""
    original = sys.excepthook

    def write(kind: str, text: str) -> None:
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {kind} =====\n{text}")
        except OSError:
            pass

    def formatted(exc_type, exc_value, exc_tb) -> str:
        """例外を文字列にする。整形している間はGCを止める。

        comtypes のCOMオブジェクトは CoInitialize(STA)で作られており、作った
        スレッド以外から解放すると落ちる。PythonのGCは任意のスレッドで走るので、
        たまたま整形の最中に走ると、記録しようとしたこちらがプロセスごと死ぬ。

        実際 crash.log に、traceback.format_exception の途中でGCが動き
        comtypes の __del__ → Release で access violation になった記録が残っている。
        そのとき元の例外が何だったのかは、記録し終える前に死んだので永久に分からない。
        例外は滅多に起きないので、その間だけ止めても実害は無い。"""
        gc.disable()
        try:
            return "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        finally:
            gc.enable()

    def hook(exc_type, exc_value, exc_tb):
        write("uncaught", formatted(exc_type, exc_value, exc_tb))
        original(exc_type, exc_value, exc_tb)

    def thread_hook(args):
        write(
            f"uncaught in thread {args.thread.name if args.thread else '?'}",
            formatted(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = hook
    threading.excepthook = thread_hook


def main():
    _install_crash_log()
    _install_excepthook()
    # COMを使う機能(音声デバイス)を組み立てる前に済ませる。理由は init_keyboard を参照。
    init_keyboard()

    # このIDを設定しないと、他のPythonツールとタスクバー/通知領域で同一アプリ扱いされ
    # アイコンが混線することがある。QApplication生成前に呼ぶ必要がある。
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("traytools.app.1")
    except (AttributeError, OSError):
        pass

    app = QApplication(sys.argv)
    # 付箋ウインドウを全部閉じても常駐アプリごと終了しないようにする(必須)。
    app.setQuitOnLastWindowClosed(False)

    if not _acquire_single_instance():
        # 常駐中の側が「すでに起動しています」と知らせるので、こちらは黙って終わる。
        return
    instance_lock = _start_command_listener()

    icon_path = Path(__file__).resolve().parent / "icons" / "rapture.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    app_settings = settings_module.load_settings()
    settings_module.cleanup_old_captures(app_settings.get("capture", {}))

    features = [cls(app_settings, settings_module.SETTINGS_PATH) for cls in FEATURE_CLASSES]
    _wire_restart(features, instance_lock)
    _wire_taskbar_widget(features)

    # 二重起動の通知と、外部からのコマンド受付を兼ねる入口。
    command_handlers = _build_command_handlers(features)
    instance_lock.newConnection.connect(
        lambda: _handle_connection(instance_lock, command_handlers)
    )

    handlers = {}
    for feature in features:
        handlers.update(feature.hotkeys())
    # 戻り値のHotkeyBridgeはローカル変数として保持し続ける必要がある
    # (参照が無くなるとQObjectがGCされ、シグナル接続ごと消えてしまう)。
    def on_hotkey_error(where):
        summary = log_exception(where)
        show_toast(f"tray-tools\nホットキーの処理に失敗しました\n{summary}")

    hotkey_bridge = setup_hotkeys(app_settings, handlers, on_error=on_hotkey_error)  # noqa: F841

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
