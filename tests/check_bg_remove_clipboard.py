# tests/check_bg_remove_clipboard.py
# bg_remove.set_clipboard_image() が、実際の Windows のクリップボードに
# 「PNG」形式(透過PNGを理解する貼り先が読むもの)と DIB 系を載せることを確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\check_bg_remove_clipboard.py
#
# offscreen では確かめられない(offscreen にはクリップボードの実装が無く、しかも
# setMimeData() を使うと終了時に 139 で落ちる。README の「検証時の注意」参照)ので、
# 実画面のプラットフォームで動かす。窓は出さない。
#
# **本物のクリップボードを一瞬書き換える。** 走らせる前の中身は clipboard_format の
# 退避(「元に戻す」と同じ仕組み)で取っておき、確認が済んだら書き戻す。退避できない
# (16MB を超える等)ときは、何も書き換えずに中止する。Windows のクリップボード履歴
# (Win+V)を有効にしていると、試験用の画像が履歴に1件残る。
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage  # noqa: E402

import bg_remove  # noqa: E402
import clipboard_format  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ctypes は argtypes / restype を必ず指定する(64bit でハンドルが切り詰められて落ちる)。
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
user32.GetClipboardFormatNameW.restype = ctypes.c_int
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

STANDARD = {2: "CF_BITMAP", 8: "CF_DIB", 17: "CF_DIBV5", 1: "CF_TEXT", 13: "CF_UNICODETEXT",
            7: "CF_OEMTEXT", 16: "CF_LOCALE"}


def _open():
    for _ in range(20):
        if user32.OpenClipboard(None):
            return True
        pump(50)
    return False


def list_formats():
    names = []
    if not _open():
        raise OSError("クリップボードを開けません")
    try:
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            buf = ctypes.create_unicode_buffer(256)
            if user32.GetClipboardFormatNameW(fmt, buf, 256) > 0:
                names.append((fmt, buf.value))
            else:
                names.append((fmt, STANDARD.get(fmt, f"#{fmt}")))
    finally:
        user32.CloseClipboard()
    return names


def read_format_bytes(fmt: int) -> bytes:
    if not _open():
        raise OSError("クリップボードを開けません")
    try:
        handle = user32.GetClipboardData(fmt)
        if not handle:
            return b""
        size = kernel32.GlobalSize(handle)
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return b""
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def pump(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    app = QGuiApplication([sys.argv[0]])  # noqa: F841  実画面のプラットフォーム(windows)
    if not clipboard_format.take_snapshot():
        print("いまのクリップボードを退避できないので、書き換えずに中止します")
        return 2

    failures = []
    try:
        rgba = np.zeros((40, 60, 4), dtype=np.uint8)
        rgba[..., 0] = 220
        rgba[10:30, 15:45, 3] = 255     # 中央だけ不透明、周りは完全に透明
        rgba[5, 5, 3] = 128             # 半透明の画素も1つ
        bg_remove.set_clipboard_image(bg_remove.array_to_qimage(rgba))
        pump(200)

        formats = list_formats()
        print("載った形式:", ", ".join(f"{name}({fmt})" for fmt, name in formats))
        names = {name for _fmt, name in formats}
        for required in ("PNG", "CF_DIB"):
            if required not in names:
                failures.append(f"{required} が載っていない")
        if "CF_DIBV5" not in names:
            print("注意: CF_DIBV5 は載っていない(DIB のアルファは貼り先が無視しがち)")

        png_fmt = user32.RegisterClipboardFormatW("PNG")
        data = read_format_bytes(png_fmt)
        image = QImage.fromData(data, "PNG")
        if image.isNull():
            failures.append(f"PNG 形式を画像として読めない({len(data)} バイト)")
        else:
            back = bg_remove.qimage_to_array(image)
            ok = (back.shape == rgba.shape and back[0, 0, 3] == 0 and back[20, 30, 3] == 255
                  and back[5, 5, 3] == 128)
            print(f"PNG 形式: {len(data)} バイト {image.width()}x{image.height()} "
                  f"アルファ(角/中央/半透明) = {back[0, 0, 3]}/{back[20, 30, 3]}/{back[5, 5, 3]}")
            if not ok:
                failures.append("PNG 形式のアルファが保たれていない")
    finally:
        ok, message = clipboard_format.restore_snapshot()
        pump(200)
        print(f"クリップボードを元に戻す: {message}")
        if not ok:
            failures.append("クリップボードを元に戻せなかった")

    if failures:
        print("FAIL: " + " / ".join(failures))
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
