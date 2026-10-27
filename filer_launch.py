# filer_launch.py
# あっふ(filer.py)を常駐側から起こすための入口。bg_remove_launch.py と同じ作法で、
# 標準ライブラリと、呼ぶ側がすでに読み込んでいる PySide6 だけで書く。
#
# 既に開いていれば新しく起こさず、開いているものを前面に出す(ホットキーを2回押して
# 窓が2枚になると、どちらで操作したか分からなくなり、閉じたときの保存も後勝ちになる)。
#
# 「開いているか」の判定は名前付きミューテックスで行う(CLAUDE.md「単一起動の判定」)。
# QLocalServer の listen は Windows では排他にならないので判定には使えない。待ち受けは
# 「前面に出して」を届ける口としてだけ使う。
#
# 起こし方は bg_remove_launch.spawn() と同じ(pythonw.exe + DETACHED_PROCESS)。本体を
# 再起動しても開いているファイラが道連れにならない。
import ctypes
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "filer.py"

# filer.py が握るミューテックスと、前面化の依頼を受ける待ち受けの名前。
# Local\ はログオンセッション内だけで効く名前(別ユーザーのファイラとは干渉しない)。
MUTEX_NAME = "Local\\traytools.filer"
SERVER_NAME = "traytools.filer"
SHOW_COMMAND = b"show\n"

SYNCHRONIZE = 0x00100000
ASFW_ANY = 0xFFFFFFFF

try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _OpenMutexW = _kernel32.OpenMutexW
    _OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _OpenMutexW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
except (AttributeError, OSError):
    _OpenMutexW = None

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _AllowSetForegroundWindow = _user32.AllowSetForegroundWindow
    _AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _AllowSetForegroundWindow.restype = wintypes.BOOL
except (AttributeError, OSError):
    _AllowSetForegroundWindow = None


def _allow_foreground() -> None:
    """前面化の権利を譲る(bg_remove_launch と同じ理由。DETACHED の子は前面化ロックに掛かる)。
    既に開いているファイラを前面に出すときも、相手は別プロセスなので同じく要る。"""
    if _AllowSetForegroundWindow is None:
        return
    try:
        _AllowSetForegroundWindow(ASFW_ANY)
    except OSError:
        pass


def is_running() -> bool:
    """ファイラが既に動いているか(ミューテックスが既にあるか)。

    判定できなければ False(＝起こしに行く)。二重に起きても、後から起きた側が
    自分でミューテックスを見て、先客を前面に出して引き返すので害は無い。"""
    if _OpenMutexW is None:
        return False
    try:
        handle = _OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
    except OSError:
        return False
    if not handle:
        return False
    _CloseHandle(handle)
    return True


def send_show(timeout_ms: int = 1000) -> bool:
    """動いているファイラに「前面に出して」を送る。届いたら True。

    書いたあと相手の返事("ok")を少し待ってから切る。すぐ切ると、相手が読む前に
    接続ごと片付けられて依頼が消えることがある。返事が来なくても書けていれば True
    (相手は受け取っていて、返事だけ遅れている見込みが高い)。"""
    from PySide6.QtNetwork import QLocalSocket

    _allow_foreground()
    sock = QLocalSocket()
    try:
        sock.connectToServer(SERVER_NAME)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(SHOW_COMMAND)
        sock.flush()
        # flush で書き切れていると waitForBytesWritten は待つものが無く False を返す。
        # 結果では判断せず、書き残しが無いかで見る。Windows のパイプは書き込みが非同期で、
        # 1回の waitForBytesWritten では書き終わらないことがある(テストで5回に1回ほど
        # 書き残しが出た)ので、期限まで繰り返す。
        deadline = time.monotonic() + timeout_ms / 1000
        while sock.bytesToWrite() and time.monotonic() < deadline:
            sock.waitForBytesWritten(50)
        if sock.bytesToWrite():
            return False
        sock.waitForReadyRead(timeout_ms)
        sock.disconnectFromServer()
        return True
    finally:
        sock.abort()


def spawn() -> int:
    """filer.py を別プロセスで起こす。起こせなければ OSError(呼ぶ側で通知する)。"""
    _allow_foreground()
    proc = subprocess.Popen(
        [pythonw_executable(), str(SCRIPT_PATH)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def open_filer() -> str:
    """開いていれば前面へ、無ければ起こす。何をしたかを "shown" / "spawned" / "starting" で返す。

    "starting" は、ミューテックスはあるのに待ち受けに繋がらなかったとき(起動の途中で、
    窓はまもなく出る)。ここで重ねて起こすと、起動中のものと2つになるだけなので何もしない。"""
    if is_running():
        return "shown" if send_show() else "starting"
    spawn()
    return "spawned"
