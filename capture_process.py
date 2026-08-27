# capture_process.py
# Rapture の付箋(capture_window.CaptureWindow)を1枚だけ表示する、本体とは別のプロセス。
#
# なぜ別プロセスなのか
#   付箋を本体(main.py)と同じプロセスに置くと、本体が落ちたときはもちろん、機能追加の
#   たびの再起動でも開いている付箋が全部消える。手順書を作るために連番キャプチャで
#   何枚も並べている最中にそれをやられると、撮り直しからやり直しになる。開発中は
#   再起動が頻繁なので、付箋の寿命を本体から切り離してある。
#
# 待機プロセス方式(既定)
#   付箋を1枚出すたびに Python と Qt を読み込むと、範囲を選んでから付箋が出るまで
#   実測355msかかる(遅いPCでは3秒以上という報告もある)。その7割強は「Qtを読み込んで
#   最初のウインドウを出す」固定費で、コードの書き方では縮まない。そこでその費用を、
#   待たされない時間帯へ前払いする。
#
#     本体の起動から少し経ったら、付箋プロセスを1つ「待機役」として起こしておく
#     (--prewarm)。待機役は Qt を読み込み、CaptureWindow まで作り、見えない場所で
#     一度描画してから隠れて待つ。キャプチャのときは、その待機役へ画像と表示位置を
#     送るだけ。送られた待機役はウインドウを出して普通の付箋になり、本体は次の待機役を
#     裏で起こす。
#
#   待機役が居ないときは、必ず従来どおりの spawn() に落ちる(355msかかるが確実に出る)。
#   待機役が死んでいてキャプチャが失敗する、という事態を作らないこと。
#   常時1プロセスぶん(専有27MB程度)のメモリを使うため、settings.json の
#   capture.prewarm を false にすると従来の毎回起動へ戻せる。
#
# 2つの顔を持つモジュール
#   1. 本体(feature_screen.ScreenFeature)から import して使う道具:
#        spawn()            付箋を1枚起こす(待機役が居ないときのフォールバック)
#        prewarm()          待機役を1人起こす
#        ensure_prewarmed() 待機役が居なければ1人起こす
#        show_via_warm()    待機役へ画像と表示位置を送って付箋にしてもらう
#        shutdown_warm()    まだ何も表示していない待機役に店じまいしてもらう
#        send_to_latest()   最後に作られた付箋へコマンドを1つ送る
#   2. pythonw.exe で直接実行されるエントリポイント(main())。付箋1枚として出るか、
#      --prewarm なら待機役として待つ。
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

from PySide6.QtCore import QPoint, Qt, QTimer
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

# 待機役(--prewarm)の待ち受け名。付箋とは別の名前空間にしてある。
# "traytools.rapture-warm." は "traytools.rapture." では始まらない(4文字目以降が
# "-" と "." で分かれる)ので、list_sticky_pipes() が待機役を付箋と取り違えることはない。
# ここを取り違えると、連番キャプチャ(Ctrl+Alt+S)が画面に出ていない待機役へ飛び、
# 撮ったつもりの絵がどこにも現れないという分かりにくい壊れ方をする。
WARM_PIPE_PREFIX = "traytools.rapture-warm."

# 本体の待ち受け(main.SINGLE_INSTANCE_KEY)とは名前空間を分けてある。付箋はいくつでも
# 立ち上がるものなので、二重起動ロックとは無関係でなければならない。
# 待機役だけはこの名前を「本体がまだ居るか」の目印として見る(_WarmHost._check_parent)。
MAIN_PIPE_NAME = "traytools.single-instance"

# 待機役へ送るコマンド。
SHOW_STICKY_COMMAND = "show_sticky"   # この画像・この位置で付箋になれ
QUIT_WARM_COMMAND = "quit_warm"       # まだ何も出していないなら終わってよい

# 待機役が本体の生存を確かめる間隔と、何回続けて見つからなければ終わるか。
# 待機役は DETACHED_PROCESS で起こしてあるので本体の道連れにならない。付箋にとっては
# それが目的だが、まだ何も表示していない待機役にとっては、本体が落ちた/終わったあとに
# 誰にも使われないまま27MB居座るだけの置き土産になる。本体の再起動(main._restart)では
# 待ち受けが一瞬消えるので、1回の空振りでは終わらせない。
WARM_PARENT_CHECK_MS = 15000
WARM_PARENT_MISS_LIMIT = 2

# 待機役を起こしてから待ち受けが立つまでの猶予。この間は list_warm_pipes() に出ないので、
# 「居ないならもう1人」を素直にやると待機役が増え続ける。直前に頼んだ時刻を覚えておいて
# 二重に起こさないようにする(ensure_prewarmed)。
WARM_SPAWN_GRACE_SECONDS = 5.0
_last_prewarm_request = 0.0

# 画像を渡し終えた待機役の名前。渡された側は受け取った時点で名乗りを下ろすが、その処理は
# 向こうのイベントループで動くので、こちらが「補充が要るか」を見に行くほうが先に着く。
# 覚えておかないと「まだ待機役が居る」と読み違えて補充が空振りし、次の1枚が355msに戻る。
_consumed_warm = set()

# 待機中のウインドウを置いておく座標。仮想画面の外なら実際にはどこでもよい。
# ここに置いたうえで不透明度0・WS_EX_TOOLWINDOW も重ねるので、三重に見えない。
WARM_OFFSCREEN_POS = (-4000, -4000)

# 待機中のウインドウの ex-style をいじるための Win32 定数。Qt の setWindowFlags で
# Qt.Tool を足すと native window が作り直され、前払いした初回描画が無駄になるため、
# ex-style だけを直接触る。
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080

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


def warm_pipe_basename(created_ms: int, pid: int) -> str:
    """待機役1人ぶんの待ち受け名。作りは付箋と同じで、頭の目印だけが違う。"""
    return f"{WARM_PIPE_PREFIX}{created_ms}.{pid}"


def _parse_pipe_name(name: str, prefix: str = PIPE_PREFIX):
    """待ち受け名から (作成時刻ms, pid) を取り出す。その種類でなければ None。"""
    if not name.startswith(prefix):
        return None
    parts = name[len(prefix):].split(".")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _list_pipes(prefix: str) -> list:
    """\\\\.\\pipe\\ から、その種類の待ち受け名を新しく作られた順に並べて返す。

    パイプはプロセスが終われば消えるので、一覧に出るものは生きているプロセス。本体が
    再起動して手元の参照を失っても、これで見つけ直せる(それが別プロセス化の目的)。"""
    try:
        names = os.listdir(PIPE_DIR)
    except OSError as e:
        print(f"[rapture] 名前付きパイプの一覧を取れません: {e}", file=sys.stderr)
        return []
    found = []
    for name in names:
        parsed = _parse_pipe_name(name, prefix)
        if parsed is not None:
            found.append((parsed[0], parsed[1], name))
    found.sort(reverse=True)  # 作成時刻の新しい順。同時刻なら pid の大きい方を先に
    return [name for _, _, name in found]


def list_sticky_pipes() -> list:
    """生きている付箋の待ち受け名を、新しく作られた順に並べて返す。

    待機役はここに出ない(名前の目印が違う)。画面に出ていない待機役へ連番キャプチャが
    飛ばないようにするため。"""
    return _list_pipes(PIPE_PREFIX)


def list_warm_pipes() -> list:
    """待っている待機役の待ち受け名を、新しく起きた順に並べて返す。

    使われた待機役は自分の名乗りを下ろして付箋の名前へ張り替えるので、ここに出るのは
    「まだ画像を渡されていない待機役」だけ。"""
    return _list_pipes(WARM_PIPE_PREFIX)


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

    この0.3秒を消すために、待機役へ画像を送るだけにする道(show_via_warm)を後から
    足した。既定はそちらで、この関数は「待機役が居ない/死んでいる」ときのフォールバック
    として残してある。遅いが確実に出るのがこちらの取り柄なので、消さないこと。
    懸念していた2つの代償は実装してみて次のように収まった:
      - メモリ: 待機役1人あたり専有27MB前後(ワーキングセットは56MBだが、大半は本体と
        共有しているQtのDLL)。常時1人だけ飼う作りにした。
      - 立て直し: 待機役が死んでいると show_via_warm が None を返すので、呼び出し側
        (feature_screen._open_capture_window)がここへ落ちる。付箋が出ないことは無い。"""
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
# 待機役を飼う(本体から使う)
# ---------------------------------------------------------------
def prewarm(settings_path=None) -> int:
    """待機役を1人起こす。起こせなければ OSError を投げる。

    起こし方は spawn() と同じ(pythonw.exe + DETACHED_PROCESS)。違うのは画像を渡さない
    ことだけで、渡されるまでウインドウを出さずに待つ。戻り値が中継役の pid になるのも
    spawn() と同じ事情なので、名指ししたいときは list_warm_pipes() の名前を使うこと。"""
    argv = [pythonw_executable(), str(SCRIPT_PATH), "--prewarm"]
    if settings_path:
        argv += ["--settings-path", str(settings_path)]
    proc = subprocess.Popen(
        argv,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def ensure_prewarmed(settings_path=None) -> int:
    """待機役が居なければ1人起こす。起こしたなら pid、要らなければ 0 を返す。

    待機役は1人だけ飼う。2人目からは、次の1枚が出るまでの時間を縮めないのにメモリだけ
    増える(連続してキャプチャしても、1枚出すたびに次の待機役を起こすので間に合う)。

    起きてから待ち受けが立つまで0.4秒ほどかかり、その間は list_warm_pipes() に出ない。
    そこを見ずに「居ないからもう1人」を繰り返すと待機役が増えてしまうので、直前に
    頼んだ時刻を覚えて WARM_SPAWN_GRACE_SECONDS の間は重ねて起こさない。

    逆に、画像を渡し終えた直後の待機役はまだ一覧に残っている(名乗りを下ろすのは向こうの
    イベントループなので、こちらの問い合わせのほうが先に着く)。そのまま数えると補充が
    空振りするため、_consumed_warm に控えた名前は居ないものとして扱う。"""
    global _last_prewarm_request
    _forget_dead_consumed_warm()
    if [n for n in list_warm_pipes() if n not in _consumed_warm]:
        # 頼んでおいたぶんが立ち上がっている。猶予はもう役目を終えたので忘れる。
        # ここを残すと、その待機役を使った直後の補充まで巻き添えで止まり、2枚目が
        # 従来どおりの355msに戻ってしまう(実際にそれで測定値が1枚おきに跳ねた)。
        _last_prewarm_request = 0.0
        return 0
    if time.monotonic() - _last_prewarm_request < WARM_SPAWN_GRACE_SECONDS:
        return 0  # 直前に頼んだぶんがまだ立ち上がっている途中
    _last_prewarm_request = time.monotonic()
    return prewarm(settings_path)


def _forget_dead_consumed_warm() -> None:
    """名乗りを下ろし終えた待機役の名前を _consumed_warm から落とす。
    放っておくとキャプチャのたびに1つずつ増え続ける(消えないゴミになる)。"""
    if not _consumed_warm:
        return
    _consumed_warm.intersection_update(list_warm_pipes())


def show_via_warm(image: QImage, global_pos: QPoint, settings_path=None,
                  close_on_escape: bool = False, session_stem=None,
                  session_index: int = 0, capture_hotkey=None):
    """待機役へ画像と表示位置を送り、付箋になってもらう。

    送れた待機役の待ち受け名を返す。待機役が1人も居ない/全員に届かなかったときは
    None を返す(例外にしない)。呼び出し側はそれを見て spawn() へ落ちること。

    画像の渡し方は spawn() と同じで、一時PNGを書いてパスだけを送る。プロセス境界を
    越えられないのは待機役相手でも同じで、パイプに画像を流し込むより、既にある
    受け渡しの作法をそのまま使うほうが読む側の覚えることが減る。読んだ待機役が
    そのPNGを消す(消し忘れるとキャプチャのたびにゴミが溜まる)。

    誰にも届かなかった場合は、読む相手の居ないPNGをここで消してから戻る。"""
    global _last_prewarm_request
    _forget_dead_consumed_warm()
    names = [n for n in list_warm_pipes() if n not in _consumed_warm]
    if not names:
        return None

    _sweep_stale_handoffs()
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    handoff = HANDOFF_DIR / f"handoff_{os.getpid()}_{time.time_ns()}.png"
    if not image.save(str(handoff), "PNG"):
        raise OSError(f"受け渡し用の画像を書き出せませんでした: {handoff}")

    payload = {
        "image": str(handoff),
        "pos": [global_pos.x(), global_pos.y()],
        # PNG は devicePixelRatio を保存しないので別に渡す(無いと高DPIで倍の大きさになる)
        "dpr": image.devicePixelRatio() or 1.0,
        "session_stem": session_stem,
        "session_index": int(session_index or 0),
        "capture_hotkey": capture_hotkey,
        "close_on_escape": bool(close_on_escape),
        "settings_path": str(settings_path) if settings_path else None,
    }

    for name in names:
        try:
            send(name, SHOW_STICKY_COMMAND, [payload])
            # この待機役はもう付箋。一覧から消えるまでの間、居ないものとして扱う。
            _consumed_warm.add(name)
            # 使えたということは、頼んでおいたぶんは立ち上がり切っている。すぐ後に来る
            # 補充を猶予で止めないよう忘れる(ensure_prewarmed のコメント参照)。
            _last_prewarm_request = 0.0
            return name
        except OSError:
            # 消えていた(FileNotFoundError)か、他所に取られて取り込み中。どちらでも
            # 次の候補へ回す。ここで粘らないのは、待たせるくらいなら spawn() に
            # 落ちたほうが結果が早いため。
            continue

    try:
        handoff.unlink()
    except OSError:
        pass
    return None


def shutdown_warm() -> int:
    """まだ何も表示していない待機役に店じまいしてもらう。終わらせた人数を返す。

    本体の終了時に呼ぶ。付箋と違って残す理由が無い(残すと、使われないプロセスが
    そのまま居座る)。使われた待機役はもう付箋なので、この呼び出しの対象に入らない
    ——名乗りを下ろして list_warm_pipes() から消えているため。本体が落ちて呼べなかった
    場合の受け皿は待機役側にもある(_WarmHost._check_parent)。"""
    stopped = 0
    for name in list_warm_pipes():
        try:
            send(name, QUIT_WARM_COMMAND)
            stopped += 1
        except OSError:
            pass  # もう居ない。終わらせる必要も無い
    return stopped


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
    # --prewarm のときは画像も位置もまだ決まっていない(渡されるまで待つのが仕事)。
    # そのため必須にはせず、下で組み合わせだけを見る。
    parser.add_argument("image", nargs="?", default=None,
                        help="表示する画像(PNG)。読み込んだら消す")
    parser.add_argument("--pos", nargs=2, type=int, default=None, metavar=("X", "Y"),
                        help="付箋の中身の左上を置くグローバル座標(Qt論理座標)")
    parser.add_argument("--prewarm", action="store_true",
                        help="ウインドウを出さずに待機し、画像を送られたら付箋になる")
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
    args = parser.parse_args(argv)
    if not args.prewarm and (args.image is None or args.pos is None):
        parser.error("--prewarm でないときは画像と --pos が要ります")
    return args


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


# ---------------------------------------------------------------
# 待機役の中身(--prewarm で実行されたとき)
# ---------------------------------------------------------------
def _placeholder_image() -> QImage:
    """待機中に持たせておく仮の画像。本物が来たら差し替えるので中身は何でもよい。
    小さいほど下ごしらえが軽く済むので8pxにしてある。"""
    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(0)
    return image


def _set_tool_window(window, enabled: bool) -> None:
    """ウインドウの WS_EX_TOOLWINDOW を付け外しする(タスクバーのボタンの有無)。

    Qt の setWindowFlags に Qt.Tool を足すやり方は使えない。あれは native window を
    作り直すので、待機中に前払いした初回描画がその場で無駄になる。ex-style を直接
    触るぶんには作り直されない。

    効かなくても待機中のウインドウがタスクバーに一瞬出るだけなので、失敗は握り潰す。"""
    try:
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        updated = (style | WS_EX_TOOLWINDOW) if enabled else (style & ~WS_EX_TOOLWINDOW)
        if updated != style:
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, updated)
    except (AttributeError, OSError, RuntimeError, ValueError):
        pass


def _prerender_offscreen(window) -> None:
    """待機中に「このプロセスで最初のウインドウを画面に出す」費用を前払いする。

    実測(このPC・900x600の付箋):
        何も前払いしない                    show して描き終わるまで 80〜148ms
        WA_DontShowOnScreen で1回出す       35〜43ms
        本当に出して隠す(この関数)          6〜8ms
    WA_DontShowOnScreen は native window を実際には見せないため、見せる費用が本番に
    残ってしまい前払いになっていなかった。そのため「本当に出す。ただし見えない場所へ、
    透明で、タスクバーにも出さず、フォーカスも奪わずに」という形にしてある:
        画面外に置く            仮想画面の外なので目に入らない
        不透明度0               万一位置がクランプされて画面内へ来ても見えない
        WS_EX_TOOLWINDOW        タスクバーにボタンを出さない
        WA_ShowWithoutActivating 作業中のウインドウからフォーカスを奪わない
    ここで仕込んだ細工は _restore_from_prerender と prepare_for_capture が全部外す。

    show() だけでは WM_PAINT がまだ来ていないことがあるので、repaint() で同期的に
    1回描かせる。ここを省くと肝心の初回描画が本番に残る。"""
    window.setWindowOpacity(0.0)
    window.setAttribute(Qt.WA_ShowWithoutActivating, True)
    # WS_EX_TOOLWINDOW は show() より前に付けること。タスクバーのボタンは
    # 「最初に表示された時点の ex-style」で決まるので、出してから付けても遅い。
    # winId() が native window を先に作らせる。
    window.winId()
    _set_tool_window(window, True)
    window.show()
    QApplication.processEvents()
    window.repaint()
    window.hide()
    QApplication.processEvents()


def _restore_from_prerender(window) -> None:
    """_prerender_offscreen で仕込んだ「見せないための細工」を外す。

    不透明度は prepare_for_capture が1.0へ戻す(あちらは「まっさらな付箋の状態」を
    作る役なので、そこに含めてある)。ここで外すのは表示のされ方に関わる2つだけ。"""
    _set_tool_window(window, False)
    window.setAttribute(Qt.WA_ShowWithoutActivating, False)


class _WarmHost:
    """待機役1人ぶんの持ち物と振る舞い。

    ウインドウ・待ち受け・タイマーの参照をここでまとめて持つ。ローカル変数に散らすと、
    どれか1つ取りこぼした瞬間にGCが持って行く(Qtの親子関係が無いオブジェクトなので、
    Python側が唯一の持ち主)。このインスタンス自体は _run_prewarmed が握っている。"""

    def __init__(self, app, settings_path):
        self.app = app
        self.settings_path = settings_path
        self.became_sticky = False

        app_settings = settings_module.load_settings(settings_path)
        self.window = CaptureWindow(
            _placeholder_image(),
            QPoint(*WARM_OFFSCREEN_POS),
            app_settings.get("capture", {}),
            settings_path=settings_path,
        )
        _prerender_offscreen(self.window)

        self.warm_name = warm_pipe_basename(int(time.time() * 1000), os.getpid())
        # 待ち受けは QLocalServer 1つを最後まで使い回す(_listen_as のコメント参照)。
        self.server = QLocalServer()
        self.server.newConnection.connect(self._on_connection)
        self._listen_as(self.warm_name)

        # 本体が居なくなったら店じまいする見張り。付箋になったら止める。
        self.parent_misses = 0
        self.parent_timer = QTimer()
        self.parent_timer.timeout.connect(self._check_parent)
        self.parent_timer.start(WARM_PARENT_CHECK_MS)

    # -----------------------------------------------------------
    def _listen_as(self, name: str) -> None:
        """待ち受けの名前を張り替える。QLocalServer は作り直さず使い回すこと。

        待機役と付箋では名乗る名前が違う(list_sticky_pipes が拾うのは付箋の名前だけ)ので、
        付箋になるときに張り替えが要る。そこで新しい QLocalServer を作って古いほうを
        捨てる書き方をすると、接続を受けた直後の QLocalServer が解放され、数秒後に
        Qt6Network.dll の中でアクセス違反(0xC0000005)になってプロセスごと消える。
        実際にそれで踏んだ(付箋が出てから3秒ほどで音もなく消える。error.log には
        Python の例外ではないので何も残らない)。同じオブジェクトを close() して
        listen() し直すぶんには何も壊れない。

        前回の残骸があると listen できないので removeServer してから張る
        (_listen と同じ作法)。"""
        self.server.close()
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            print(f"[rapture] 待ち受けを開けません: {name} "
                  f"({self.server.errorString()})", file=sys.stderr)

    def _on_connection(self) -> None:
        # Qtのスロット内で例外を投げ切るとプロセスごと落ちる。ここは外部から叩かれる
        # 入口なので必ず受け止める。落ちると「本体は送れたつもりなのに何も出ない」
        # という、いちばん分かりにくい壊れ方になる。
        try:
            if self.became_sticky:
                # もう普通の付箋。連番キャプチャなどの宛先として、spawn で起きた付箋と
                # まったく同じ扱いにする。
                _handle_connection(self.server, self.window)
            else:
                self._accept()
        except Exception:
            summary = _log_exception("warm connection")
            show_toast(f"Rapture\n待機役でエラーが起きました\n{summary}")

    def _accept(self) -> None:
        socket = self.server.nextPendingConnection()
        if socket is None:
            return
        # 受けた時点で名乗りを下ろす。開けたままにすると、続けてキャプチャしたときに
        # 同じ待機役がもう一度選ばれ、2枚目が黙って捨てられる。閉じておけば本体側は
        # 次の候補か spawn() のフォールバックへ回れる。
        self.server.close()
        try:
            command, args = _read_command(socket)
        finally:
            socket.close()

        if command == SHOW_STICKY_COMMAND:
            payload = args[0] if args and isinstance(args[0], dict) else {}
            self._become_sticky(payload)
            return
        if command == QUIT_WARM_COMMAND:
            # 本体が終了した。まだ何も表示していないので付き合って終わる。
            self.parent_timer.stop()
            self.app.quit()
            return
        # 生存確認や知らないコマンド。待機役のままなので名乗り直す。
        self._listen_as(self.warm_name)

    def _become_sticky(self, payload: dict) -> None:
        """画像を受け取って普通の付箋になる。ここから先は本体の生死と無関係に生きる。"""
        image = _take_handoff_image(payload.get("image") or "", payload.get("dpr") or 1.0)
        if image is None:
            # 出すものが無い。ここで終わると本体は「送れた」と思っているのに何も出ず、
            # おまけに待機役まで居なくなる。待機役のまま次を待つ。
            self._listen_as(self.warm_name)
            return

        self.parent_timer.stop()
        self.became_sticky = True

        # 設定は渡された時点で読み直す。待機役は起動しっぱなしなので、起きたときに
        # 読んだままだと settings.json の変更が次の待機役まで効かない。
        settings_path = payload.get("settings_path") or self.settings_path
        app_settings = settings_module.load_settings(settings_path)
        pos = payload.get("pos") or [0, 0]

        window = self.window
        window.prepare_for_capture(
            image,
            QPoint(int(pos[0]), int(pos[1])),
            capture_settings=app_settings.get("capture", {}),
            settings_path=settings_path,
            close_on_escape=bool(payload.get("close_on_escape")),
            session_stem=payload.get("session_stem"),
            session_index=int(payload.get("session_index") or 0),
            capture_hotkey=payload.get("capture_hotkey"),
        )
        _restore_from_prerender(window)

        # WA_DeleteOnClose が付いているので、閉じられると destroyed が飛ぶ。付箋1枚が
        # このプロセスの存在理由なので、そこで終わる(spawn 側と同じ)。
        window.destroyed.connect(lambda *_: self.app.quit())

        # 付箋としての待ち受け(連番キャプチャの宛先)へ名前を張り替える。待機役の名前は
        # list_sticky_pipes() に出ないので、これをやらないと Ctrl+Alt+S が届かない。
        # 表示の直前に張るのは spawn 側と同じ理由(本体から見た「付箋が使えるようになった
        # 時刻」を表示と揃えたい)。
        self._listen_as(pipe_basename(int(time.time() * 1000), os.getpid()))
        window.show()
        # 待機役は「古いプロセス」なので、show() だけでは前面に来ないことがある。
        # 最前面固定(WindowStaysOnTopHint)のおかげで見えなくなりはしないが、
        # Ctrl+S などが効く状態にするためフォーカスも寄せておく。
        window.raise_()
        window.activateWindow()

    def _check_parent(self) -> None:
        """本体がもう居ないなら、使われないまま居座らずに終わる。

        待機役は DETACHED_PROCESS で起こしてあるので本体の道連れにならない。付箋には
        それが必要だが、まだ何も表示していない待機役に必要なのは逆で、本体が落ちたら
        一緒に消えてほしい。本体の待ち受け(名前付きパイプ)の有無で判断する。

        付箋になったあとはこのタイマーを止めてあるので、表示中の付箋がこれで消えることは
        無い(それをやると別プロセス化の目的そのものを壊す)。"""
        try:
            alive = MAIN_PIPE_NAME in os.listdir(PIPE_DIR)
        except OSError:
            alive = True  # 判断が付かないなら残る側に倒す
        if alive:
            self.parent_misses = 0
            return
        self.parent_misses += 1
        if self.parent_misses >= WARM_PARENT_MISS_LIMIT:
            self.parent_timer.stop()
            self.app.quit()


def _run_prewarmed(app, args) -> int:
    """待機役として立ち上がる。画像を渡されるまでウインドウを出さない。"""
    settings_path = args.settings_path or str(settings_module.SETTINGS_PATH)
    # ローカル変数で持ち続ける。捨てるとウインドウごとGCで消える。
    host = _WarmHost(app, settings_path)  # noqa: F841
    return app.exec()


def _run_sticky(app, args) -> int:
    """付箋1枚として立ち上がる(従来どおりの道)。"""
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
    # 待機役にとってはこれが必須でもある(下ごしらえで一度 hide() するため)。
    app.setQuitOnLastWindowClosed(False)

    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    if args.prewarm:
        return _run_prewarmed(app, args)
    return _run_sticky(app, args)


if __name__ == "__main__":
    sys.exit(main())
