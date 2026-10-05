# bg_remove_launch.py
# 「背景を透過」(bg_remove.py)を別プロセスで起こすための入口。本体(feature_screen)と
# 付箋(capture_window)の両方から import する。
#
# なぜ bg_remove.py と分けたのか
#   bg_remove.py はトップレベルで numpy / cv2 を読み込む(純関数をそこに置いてあるので)。
#   本体からそれを import すると、常駐プロセスに使うか分からない数十MBのライブラリが
#   起動のたびに乗り、import の時間ぶん起動も遅くなる。付箋プロセスはさらに起動の速さを
#   削って作ってある(capture_process.py の待機役の話)ので、そこへ持ち込むのは論外。
#   ここは標準ライブラリと、呼ぶ側がすでに読み込んでいる PySide6 だけで書く。
#
# 起こし方は capture_process.spawn() と同じ(pythonw.exe + DETACHED_PROCESS)。
# DETACHED_PROCESS を付けるのは、本体を再起動しても開いている窓が道連れにならないため。
import ctypes
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "bg_remove.py"

# 「クリップボードから読め」の印。ファイルパスと取り違えないよう -- で始めてある。
CLIPBOARD_SOURCE = "--clipboard"

# 付箋の画像を渡すための一時PNG。bg_remove.py は --delete-after-read のときだけ、
# 読み終えた時点でこれを消す(ユーザーが指定した本物のファイルを消さないため、
# 消してよいかは呼ぶ側が明示する)。起こせなかったときの取り残しは次の起動で掃除する。
HANDOFF_DIR = Path(tempfile.gettempdir()) / "traytools-bgremove"
HANDOFF_STALE_SECONDS = 300

# 起こした側が前面を譲るための Win32。子プロセスは DETACHED で起こすので、Windows の
# 前面化ロックに掛かって「窓は出たが後ろに居る」になりやすい。呼ぶ側(ホットキーや
# メニューを受けた直後の本体)は前面化の権利を持っていることが多いので、それを譲っておく。
# 権利が無ければ失敗するだけで害は無い(窓は最前面フラグ付きで出すので見失わない)。
ASFW_ANY = wintypes.DWORD(0xFFFFFFFF)
try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _AllowSetForegroundWindow = _user32.AllowSetForegroundWindow
    _AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _AllowSetForegroundWindow.restype = wintypes.BOOL
except (AttributeError, OSError):
    _AllowSetForegroundWindow = None


def _allow_foreground() -> None:
    if _AllowSetForegroundWindow is None:
        return
    try:
        _AllowSetForegroundWindow(ASFW_ANY)
    except OSError:
        pass


def spawn(source: str, delete_after_read: bool = False) -> int:
    """bg_remove.py を別プロセスで起こす。source は画像のパスか CLIPBOARD_SOURCE。

    起こせなければ OSError を投げる(呼ぶ側で通知する)。戻り値は中継役の pid で、
    窓の持ち主とは限らない(venv の pythonw.exe はスタブ。CLAUDE.md 参照)。"""
    argv = [pythonw_executable(), str(SCRIPT_PATH), str(source)]
    if delete_after_read:
        argv.append("--delete-after-read")
    _allow_foreground()
    proc = subprocess.Popen(
        argv,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def spawn_from_clipboard() -> int:
    return spawn(CLIPBOARD_SOURCE)


def spawn_with_image(image) -> int:
    """QImage を一時PNGに書いて、それを読ませる形で起こす(付箋から使う)。

    QImage はプロセス境界を越えられないので、capture_process と同じくファイルで渡す。
    PNG はアルファを保つので、描き込み済みの絵もそのまま渡る。"""
    _sweep_stale_handoffs()
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    path = HANDOFF_DIR / f"handoff_{os.getpid()}_{time.time_ns()}.png"
    if not image.save(str(path), "PNG"):
        raise OSError(f"受け渡し用の画像を書き出せませんでした: {path}")
    try:
        return spawn(str(path), delete_after_read=True)
    except OSError:
        # 起こせなかったなら読む相手が居ない。その場で消す。
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _sweep_stale_handoffs() -> None:
    """読まれないまま残った受け渡し用PNGを片付ける(capture_process と同じ作法)。"""
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
            pass


# 読み込める画像の拡張子。エクスプローラでコピーしたファイルの中から画像を選ぶのに使う。
# QImageReader.supportedImageFormats() を毎回引くと Qt のプラグイン走査が走るので、
# よく使うものだけを決め打ちにしてある(ここに無い形式でも、パスを直接渡せば読める)。
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


def clipboard_has_image(clipboard=None) -> bool:
    """クリップボードに、背景透過へ渡せる画像があるか(本体側の事前確認)。

    無いのに子プロセスを起こすと、numpy/cv2 の読み込みを待たされた挙句に
    「画像がありません」と言われることになる。その1秒余りを省くため、本体で先に見る。
    ここは Qt の型を引くだけで、画像の中身までは読まない(大きい画像でも一瞬で済む)。"""
    try:
        if clipboard is None:
            from PySide6.QtGui import QGuiApplication

            clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None:
            return False
        if mime.hasImage():
            return True
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile() and Path(url.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES:
                    return True
        return False
    except Exception:
        # 判定できないなら起こしてみる側へ倒す。子でも同じ確認をして、無ければ知らせて終わる。
        return True
