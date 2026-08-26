# capture_process.py
# Rapture の付箋(capture_window.CaptureWindow)を1枚だけ表示する、本体とは別のプロセス。
#
# なぜ別プロセスなのか
#   付箋を本体(main.py)と同じプロセスに置くと、本体が落ちたときはもちろん、機能追加の
#   たびの再起動でも開いている付箋が全部消える。手順書を作るために連番キャプチャで
#   何枚も並べている最中にそれをやられると、撮り直しからやり直しになる。開発中は
#   再起動が頻繁なので、付箋の寿命を本体から切り離してある。
#
# 2つの顔を持つモジュール
#   1. 本体(feature_screen.ScreenFeature)から import して使う道具:
#        spawn()            付箋を1枚起こす
#        send_to_latest()   最後に作られた付箋へコマンドを1つ送る
#   2. pythonw.exe で直接実行されるエントリポイント(main())。
#   本体側は Qt をすでに読み込み済みなので、import を分けたりはしていない。
#
# 画像の渡し方
#   QImage はプロセス境界を越えられないので、一時ディレクトリへ PNG を書いて
#   パスだけを引数で渡す。付箋は読み終えた時点でその PNG を消す(残すとキャプチャの
#   たびにゴミが溜まる)。PNG は devicePixelRatio を保存しないため、高DPI環境で
#   2倍の大きさの付箋が開かないよう --dpr で別に渡して復元する。
#
# 連番キャプチャ(本体の Ctrl+Alt+S)の届け方
#   付箋は起動時に自分専用の名前付きパイプ traytools.rapture.<作成時刻ms>.<pid> を
#   開いて待ち受ける(中身の約束事は main.py の待ち受けと同じ、1行の UTF-8 JSON)。
#   本体は \\.\pipe\ の一覧からこの名前を拾って送る。本体が再起動して手元の参照を
#   失っても、一覧を見れば生きている付箋を見つけ直せる。
#
# keyboard は絶対に使わないこと。初回の add_hotkey が COM を触り、GC と衝突すると
# プロセスごと落ちる(hotkeys.init_keyboard のコメント参照)。ホットキーは本体が
# 持ち、ここへは上記の IPC で伝える、という役割分担にしてある。
import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtGui import QIcon, QImage
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication

import settings as settings_module
from capture_window import CaptureWindow
from toast import show_toast
# 窓の出ない実行ファイルの割り出しと、親から切り離して起動するフラグ。本体の再起動
# (main._restart)と同じものを使う。DETACHED_PROCESS を付けるのがこの機能の肝で、
# これが無いと本体を終了したときに付箋も道連れになる。
from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve()
ERROR_LOG_PATH = SCRIPT_PATH.parent / "error.log"
ICON_PATH = SCRIPT_PATH.parent / "icons" / "rapture.ico"

# 本体と同じ AppUserModelID にしておく。付箋はタイトルバーを持つ普通のウインドウなので
# タスクバーに並ぶが、別プロセスになったせいで本体と別グループに割れると見た目が変わる。
APP_USER_MODEL_ID = "traytools.app.1"

# Windows の名前付きパイプは \\.\pipe\ 配下に並ぶ。os.listdir() で一覧が取れる
# (実測 0.6ms 程度なので、ホットキーの入口で毎回舐めても体感には出ない)。
PIPE_DIR = "\\\\.\\pipe"
PIPE_PREFIX = "traytools.rapture."

# 本体の待ち受け(main.SINGLE_INSTANCE_KEY)とは名前空間を分けてある。付箋はいくつでも
# 立ち上がるものなので、二重起動ロックとは無関係でなければならない。

# コマンドの読み取り待ち時間(ms)。相手は接続直後に1行書いて終わる作りなので短くてよい。
COMMAND_READ_TIMEOUT_MS = 300

# 相手が取り込み中(ERROR_PIPE_BUSY)だったときに1度だけ出し直すまでの待ち。
# QLocalServer は1接続を受けた直後に次の待ち受けを張り直すので、当たっても一瞬。
BUSY_RETRY_SECONDS = 0.05

# 画像の受け渡しに使う一時ディレクトリ。付箋が読んだら消すので普段は空。
HANDOFF_DIR = Path(tempfile.gettempdir()) / "traytools-rapture"
# 付箋を起こせなかった場合の PNG は誰も消さないため、次の起動のついでに掃除する。
HANDOFF_STALE_SECONDS = 300


def _log_exception(where: str) -> str:
    """直前の例外を error.log に追記し、通知用の短い1行を返す。

    ここで新たな例外を投げないこと(例外処理の途中で呼ばれるので、落ちると原因が消える)。
    通常起動は pythonw.exe で標準エラーがどこにも出ないため、ファイルに残すのが頼り。"""
    text = traceback.format_exc()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} rapture {where} =====\n{text}")
    except OSError:
        pass
    print(text, file=sys.stderr)
    exc_type, exc_value = sys.exc_info()[:2]
    return f"{exc_type.__name__}: {exc_value}" if exc_type else "unknown error"


# ---------------------------------------------------------------
# 付箋の見つけ方(本体・付箋の両方から使う)
# ---------------------------------------------------------------
def pipe_basename(created_ms: int, pid: int) -> str:
    """付箋1枚ぶんの待ち受け名。作成時刻(ms)を先頭に入れるのは、本体が一覧から
    「最後に作られた付箋」を選び直せるようにするため。pid だけでは新旧が分からない
    (pid は使い回されるうえ、大小が起動順とは限らない)。"""
    return f"{PIPE_PREFIX}{created_ms}.{pid}"


def _parse_pipe_name(name: str):
    """待ち受け名から (作成時刻ms, pid) を取り出す。うちの付箋でなければ None。"""
    if not name.startswith(PIPE_PREFIX):
        return None
    parts = name[len(PIPE_PREFIX):].split(".")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def list_sticky_pipes() -> list:
    """生きている付箋の待ち受け名を、新しく作られた順に並べて返す。

    パイプはプロセスが終われば消えるので、一覧に出るものは生きている付箋。本体が
    再起動して手元の参照を失っても、これで見つけ直せる(それが別プロセス化の目的)。"""
    try:
        names = os.listdir(PIPE_DIR)
    except OSError as e:
        print(f"[rapture] 名前付きパイプの一覧を取れません: {e}", file=sys.stderr)
        return []
    found = []
    for name in names:
        parsed = _parse_pipe_name(name)
        if parsed is not None:
            found.append((parsed[0], parsed[1], name))
    found.sort(reverse=True)  # 作成時刻の新しい順。同時刻なら pid の大きい方を先に
    return [name for _, _, name in found]


def send(pipe_name: str, command: str, args=None) -> None:
    """付箋1枚へコマンドを送る。中身は main.py の待ち受けと同じ1行の UTF-8 JSON。

    QLocalSocket ではなく素の open() で書くのは traytools_send.py と同じ理由
    (Windows では QLocalServer の待ち受けはただの名前付きパイプなので、これで届く)。"""
    payload = json.dumps({"command": command, "args": args or []}, ensure_ascii=False)
    with open(f"{PIPE_DIR}\\{pipe_name}", "wb") as pipe:
        pipe.write((payload + "\n").encode("utf-8"))


def _send_with_retry(pipe_name: str, command: str, args=None) -> None:
    try:
        send(pipe_name, command, args)
    except FileNotFoundError:
        raise  # 付箋が消えていた。呼び出し側が次の候補へ回す
    except OSError:
        # 取り込み中。少しだけ待って同じ相手に1度だけ出し直す。ここで別の付箋へ
        # 回してはいけない(狙っていない付箋を撮ってしまう)。
        time.sleep(BUSY_RETRY_SECONDS)
        send(pipe_name, command, args)


def send_to_latest(command: str, args=None):
    """最後に作られた付箋へコマンドを送り、送れた付箋の名前を返す。1枚も無ければ None。

    対象を「最後に作られたもの」にするのは別プロセス化の前と同じ決め方
    (撮りたいのは今出した付箋なので、複数枚並んでいても直近の1枚に向ければ迷わない)。
    違うのは対象の覚え方で、以前は生成時に掴んだオブジェクトを持ち続けていたのに対し、
    今は毎回パイプの一覧から選び直す。本体を再起動しても対象を見失わないのが利点。

    届かなかったときは OSError をそのまま投げる。「付箋がありません」と告げると、
    目の前に付箋があるユーザーに嘘をつくことになるため、無いのと区別する。

    既知の穴: 付箋の起動には数百msかかるので、キャプチャ直後に間髪入れず送ると、
    まだ待ち受けが立っておらず1つ前の付箋が選ばれうる。付箋が1枚のときは
    「付箋がありません」になるだけで実害が無く、2枚以上並べる使い方では
    撮る前に付箋を見るぶんの間が空くため、待ち合わせは入れていない。"""
    for name in list_sticky_pipes():
        try:
            _send_with_retry(name, command, args)
            return name
        except FileNotFoundError:
            continue  # 一覧を取った直後に閉じられた付箋。次に新しいものへ回す
    return None


# ---------------------------------------------------------------
# 付箋を起こす(本体から使う)
# ---------------------------------------------------------------
def _sweep_stale_handoffs() -> None:
    """読み込まれないまま取り残された受け渡し用PNGを片付ける。

    通常は付箋自身が読み終えた時点で消すので何も残らない。起動に失敗した場合だけ
    誰も消さないファイルが残るため、次に付箋を開くついでに掃除する。"""
    try:
        stale = list(HANDOFF_DIR.glob("handoff_*.png"))
    except OSError:
        return
    cutoff = time.time() - HANDOFF_STALE_SECONDS
    for path in stale:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass  # 使用中などで消せないものは次の機会に


def spawn(image: QImage, global_pos: QPoint, settings_path=None,
          close_on_escape: bool = False, session_stem=None, session_index: int = 0,
          capture_hotkey=None) -> int:
    """付箋を1枚、別プロセスとして起こす。起こせなければ OSError を投げる。

    pythonw.exe で起こすのはコンソール窓を出さないため、DETACHED_PROCESS を付けるのは
    本体を終了しても付箋が道連れにならないようにするため(この機能の肝)。

    戻り値は subprocess が返した pid だが、これは付箋自身の pid とは限らない。
    venv の Scripts\\pythonw.exe は本体のインタプリタを子として起こす中継役なので、
    ここで見えるのは中継役の pid になる(実測でも両者は別の値だった)。付箋を名指し
    したいときは pid ではなく待ち受けの名前(list_sticky_pipes の戻り値)を使うこと。

    実測(このPC): 呼んだ側が止まるのは 11〜15ms、付箋が画面に出て待ち受けが立つまでが
    365〜377ms。同じプロセスで作っていた頃は 40〜76ms だったので +0.3秒ほど遅い。
    範囲を選び終えてから付箋へ目を移すまでの間に収まるので、素直な作りのままにしてある。
    これが気になるようなら、付箋プロセスを1つ先に立ち上げて画像だけ送り込む
    (待機役を常に1つ飼っておく)のが次の手。ただし待機中の1プロセスぶんメモリを
    常に食うのと、待機役が死んだときの立て直しが要る。"""
    _sweep_stale_handoffs()
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    # 同じミリ秒に2枚開いても衝突しないよう pid と ns を混ぜる。
    handoff = HANDOFF_DIR / f"handoff_{os.getpid()}_{time.time_ns()}.png"
    if not image.save(str(handoff), "PNG"):
        raise OSError(f"受け渡し用の画像を書き出せませんでした: {handoff}")

    argv = [
        pythonw_executable(),
        str(SCRIPT_PATH),
        str(handoff),
        "--pos", str(global_pos.x()), str(global_pos.y()),
        # PNG は devicePixelRatio を保存しないので別に渡す(無いと高DPIで倍の大きさになる)
        "--dpr", str(image.devicePixelRatio() or 1.0),
        "--session-index", str(int(session_index or 0)),
    ]
    if settings_path:
        argv += ["--settings-path", str(settings_path)]
    if session_stem:
        argv += ["--session-stem", str(session_stem)]
    if capture_hotkey:
        argv += ["--capture-hotkey", str(capture_hotkey)]
    if close_on_escape:
        argv += ["--close-on-escape"]

    try:
        proc = subprocess.Popen(
            argv,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except OSError:
        # 起こせなかったなら受け渡し用PNGを読む相手が居ない。その場で消す。
        try:
            handoff.unlink()
        except OSError:
            pass
        raise
    return proc.pid


# ---------------------------------------------------------------
# ここから下は付箋プロセス側(pythonw.exe で直接実行されたとき)
# ---------------------------------------------------------------
def _install_excepthook() -> None:
    """どこにも捕まらなかった例外を error.log に残す。

    PySide6 はスロットから例外が抜けるとプロセスを終わらせるので、これで落ちるのを
    防げるわけではない。落ちた「理由」を後から読めるようにするための保険
    (main._install_excepthook と同じ考え方。付箋はスレッドを持たないので
     sys.excepthook だけでよい)。"""
    original = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(
                    f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} rapture uncaught =====\n"
                    + "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
                )
        except OSError:
            pass
        original(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _parse_args(argv: list):
    parser = argparse.ArgumentParser(
        description="Rapture の付箋を1枚表示する(本体とは別プロセス)",
    )
    parser.add_argument("image", help="表示する画像(PNG)。読み込んだら消す")
    parser.add_argument("--pos", nargs=2, type=int, required=True, metavar=("X", "Y"),
                        help="付箋の中身の左上を置くグローバル座標(Qt論理座標)")
    parser.add_argument("--dpr", type=float, default=1.0,
                        help="画像の devicePixelRatio(PNGには保存されないため別に受け取る)")
    parser.add_argument("--settings-path", default=None, help="settings.json のパス")
    parser.add_argument("--session-stem", default=None, help="連番セッションのファイル名の頭")
    parser.add_argument("--session-index", type=int, default=0,
                        help="このセッションで保存済みの枚数")
    parser.add_argument("--capture-hotkey", default=None,
                        help="タイトルバーに出す連番キャプチャのキー")
    parser.add_argument("--close-on-escape", action="store_true",
                        help="Esc で閉じられるようにする")
    return parser.parse_args(argv)


def _take_handoff_image(path: str, dpr: float):
    """受け渡し用PNGを読み込み、読み終えたファイルを消す。読めなければ None。

    消すのは成功しても失敗しても必ず行う。残すとキャプチャのたびにゴミが溜まる。"""
    image = QImage(path)
    try:
        os.remove(path)
    except OSError:
        pass
    if image.isNull():
        print(f"[rapture] 画像を読み込めません: {path}", file=sys.stderr)
        return None
    image.setDevicePixelRatio(dpr or 1.0)
    return image


def _read_command(socket):
    """接続してきた相手が書いた1行のJSONを (command, args) で返す。読めなければ (None, [])。

    素の名前付きパイプから書かれるため、行の途中で届くことも複数行来ることもありうる。
    最初の1行だけを見て、残りは捨てる(main._read_command と同じ)。"""
    if not socket.waitForReadyRead(COMMAND_READ_TIMEOUT_MS):
        return None, []
    raw = bytes(socket.readAll().data())
    try:
        payload = json.loads(raw.decode("utf-8").splitlines()[0])
        command = payload["command"]
    except (UnicodeDecodeError, ValueError, IndexError, TypeError, KeyError) as e:
        print(f"[rapture] コマンドを解釈できません: {e}", file=sys.stderr)
        return None, []
    args = payload.get("args") or []
    return command, args if isinstance(args, list) else []


def _dispatch(command: str, args: list, window) -> None:
    if command == "capture_sequence":
        # 本体のホットキー(既定 Ctrl+Alt+S)から届く。右クリックメニューの
        # 「キャプチャ＆保存」と同じ実処理。
        window.capture_and_save()
    elif command == "ping":
        pass  # 生存確認だけ。届いた時点で目的は果たしている
    else:
        show_toast(f"Rapture\n知らないコマンドです\n{command}")


def _handle_connection(server, window) -> None:
    socket = server.nextPendingConnection()
    if socket is None:
        return
    try:
        command, args = _read_command(socket)
    finally:
        socket.close()

    if command is None:
        return  # 何も書かずに切れた。生存を確かめただけの接続なので黙って捨てる

    # ここは外部から叩かれる入口。Qtのスロット内で例外を投げ切ると付箋プロセスごと
    # 落ちるため、必ず受け止める。閉じられた直後の付箋を触った RuntimeError も含む。
    try:
        _dispatch(command, args, window)
    except Exception:
        summary = _log_exception(f"command={command} args={args}")
        show_toast(f"Rapture\nコマンドの実行に失敗しました\n{summary}")


def _listen(window):
    """この付箋専用の待ち受けを立てる。戻り値は呼び出し側で参照を保持すること。

    listen に失敗しても付箋は出す(連番キャプチャが効かなくなるだけで、付箋そのものは
    使える)。前回クラッシュの残骸があると listen できないので removeServer してから
    張る(main._hold_single_instance_lock と同じ作法)。"""
    name = pipe_basename(int(time.time() * 1000), os.getpid())
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if not server.listen(name):
        print(f"[rapture] 待ち受けを開けません: {name} ({server.errorString()})", file=sys.stderr)
    server.newConnection.connect(lambda: _handle_connection(server, window))
    return server


def main(argv=None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _install_excepthook()

    # これを設定しないと、他のPythonツールとタスクバーで同一アプリ扱いされてアイコンが
    # 混線することがある。QApplication生成前に呼ぶ必要がある(main.py と同じ)。
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass

    # QApplication に自前の引数を渡さない(Qtが解釈しようとするのを避ける)。
    app = QApplication([sys.argv[0]])
    # 終了の合図は lastWindowClosed に任せない。ダブルクリックの一時非表示や
    # キャプチャ＆保存の最中は付箋を hide() しており、そこで畳まれると撮り直しの
    # 途中でプロセスが消えてしまう。閉じられて実体が壊れたときだけ終わらせる。
    app.setQuitOnLastWindowClosed(False)

    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    image = _take_handoff_image(args.image, args.dpr)
    if image is None:
        return 1

    app_settings = settings_module.load_settings(args.settings_path)
    settings_path = args.settings_path or str(settings_module.SETTINGS_PATH)

    window = CaptureWindow(
        image,
        QPoint(args.pos[0], args.pos[1]),
        app_settings.get("capture", {}),
        settings_path=settings_path,
        close_on_escape=args.close_on_escape,
        session_stem=args.session_stem,
        session_index=args.session_index,
        capture_hotkey=args.capture_hotkey,
    )
    # WA_DeleteOnClose が付いているので、閉じられると destroyed が飛ぶ。付箋1枚が
    # このプロセスの存在理由なので、そこで終わる。
    window.destroyed.connect(lambda *_: app.quit())

    # 待ち受けはウインドウを出す直前に張る。本体から見た「付箋が使えるようになった時刻」を
    # 表示とほぼ揃えたい(起動待ちの計測もこの時刻を目印にしている)。
    # 参照を捨てるとGCで待ち受けごと消えるため、ローカル変数で持ち続ける。
    server = _listen(window)  # noqa: F841
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
