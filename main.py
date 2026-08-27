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

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

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


def _is_already_running() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    connected = socket.waitForConnected(300)
    if connected:
        socket.disconnectFromServer()
    return connected


def _hold_single_instance_lock():
    """先着プロセスの目印となるサーバを立てる。戻り値は呼び出し側で参照を保持すること。
    listenに失敗しても起動は止めない(誰も待ち受けていないことは確認済みで、
    ロック機構の不調でアプリ自体が使えなくなる方が困る)。"""
    # 前回クラッシュで終わるとソケットが残り、以後ずっと起動できなくなる。
    # 接続できなかった＝誰も待ち受けていないので、残骸を消してからlistenする。
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


def _handle_connection(server, command_handlers):
    socket = server.nextPendingConnection()
    if socket is None:
        return
    try:
        command, args = _read_command(socket)
    finally:
        socket.close()

    if command is None:
        # 何も書かずに切れた＝コマンドではなく二重起動。2つ目の起動は黙って終わるだけ
        # なので、「クリックしたのに何も起きない」と見えないようこちらから知らせる。
        show_toast("tray-tools\nすでに起動しています")
        return

    handler = command_handlers.get(command)
    if handler is None:
        show_toast(f"tray-tools\n知らないコマンドです\n{command}")
        return

    # ここは外部(あふｗ等)から叩かれる入口。Qtのスロット内で例外を投げ切ると
    # 常駐アプリごと落ちるため、必ず受け止める。pythonw起動では標準エラーが
    # どこにも出ないので、内容はトーストと error.log の両方に残す。
    try:
        handler(args)
    except Exception:
        summary = log_exception(f"command={command} args={args}")
        show_toast(f"tray-tools\nコマンドの実行に失敗しました\n{summary}")


def _build_command_handlers(features) -> dict:
    """外部から叩けるコマンドの表を作る。値は args(リスト)を受け取る関数。

    ウインドウの参照は main() ではなく ScreenFeature に持たせている。同じピッカーを
    トレイメニューとホットキーからも開けるので、「開いていたら前面に呼び戻す」判定も
    含めて開閉の管理は1か所(ScreenFeature)に置きたい。main() 側にリストを持つと
    同じ窓を二重に開けてしまう。"""
    screen = next((f for f in features if isinstance(f, ScreenFeature)), None)
    if screen is None:
        return {}
    return {
        # あふｗから $P(カレントパス)を渡して呼ぶ。パスが無ければ登録なしで一覧だけ出す。
        "bookmark": lambda args: screen.start_launcher(args[0] if args else None),
    }


def _restart(instance_lock) -> bool:
    """自分を起動し直す。新しい方を起こせたら True(呼んだ側がこのプロセスを終わらせる)。

    先に待ち受けを手放すのは、新しい方が起動直後に「すでに起動しています」と判断して
    引き返してしまうため。逆に、起こすのに失敗したときは待ち受けを張り直して生き残る。
    再起動できないうえ常駐まで消えると、手で起動し直すしかなくなるため。"""
    instance_lock.close()
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    try:
        subprocess.Popen(
            [pythonw_executable(), str(Path(__file__).resolve())],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError as e:
        print(f"[tray-tools] 再起動できません: {e}", file=sys.stderr)
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

    if _is_already_running():
        # 常駐中の側が「すでに起動しています」と知らせるので、こちらは黙って終わる。
        return
    instance_lock = _hold_single_instance_lock()

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
