"""Windows のクリップボード（テキストの読み書き・画像の書き込み）。ctypes で Win32 API を直接呼ぶ。"""

from __future__ import annotations

import ctypes
import io
import time
from ctypes import wintypes
from pathlib import Path

CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# 64bit でハンドルやポインタが切り詰められないよう、型を明示する
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
_user32.RegisterClipboardFormatW.restype = wintypes.UINT
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL


class ClipboardError(OSError):
    pass


class _Opened:
    """OpenClipboard は他のアプリが開いていると失敗するので、少し待ってやり直す。"""

    def __enter__(self) -> None:
        for _ in range(20):
            if _user32.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise ClipboardError("クリップボードを開けませんでした（他のアプリが使用中）")

    def __exit__(self, *exc) -> None:
        _user32.CloseClipboard()


def _put(fmt: int, data: bytes) -> None:
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise ClipboardError("GlobalAlloc に失敗しました")
    ptr = _kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    _kernel32.GlobalUnlock(handle)
    if not _user32.SetClipboardData(fmt, handle):
        _kernel32.GlobalFree(handle)  # 成功時の所有権はシステムに移るので、失敗時だけ解放する
        raise ClipboardError(f"SetClipboardData に失敗しました (format={fmt})")


def get_text() -> str:
    with _Opened():
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)


def set_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    with _Opened():
        _user32.EmptyClipboard()
        _put(CF_UNICODETEXT, data)


def set_image(path: Path) -> None:
    """画像ファイルをクリップボードへ。

    CF_DIB（どのアプリでも貼れる・透明は失われる）と、PNG 形式（Office やブラウザは
    こちらを優先して透明も保たれる）の両方を載せる。
    """
    from PIL import Image

    with Image.open(path) as im:
        im.load()
        rgba = im.convert("RGBA")
    png = io.BytesIO()
    rgba.save(png, "PNG")
    bmp = io.BytesIO()
    rgba.convert("RGB").save(bmp, "BMP")
    dib = bmp.getvalue()[14:]  # BITMAPFILEHEADER(14バイト)を除いたものが DIB

    png_format = _user32.RegisterClipboardFormatW("PNG")
    with _Opened():
        _user32.EmptyClipboard()
        _put(CF_DIB, dib)
        if png_format:
            _put(png_format, png.getvalue())
