"""Windows の通知を、通知1回ごとに別プロセスで出す。

第1段（〜2026-10）: win11toast（WinRT）を常駐プロセスの中で、しかも受信のたびに
別スレッドから呼んでいたら、_winrt_windows_data_xml_dom の中でアクセス違反
（0xC0000005）を起こして常駐ごと落ちた（2026-10-17。Python の例外にならないので
ログにも残らない）。対策として、通知は別プロセスに分離した（子なら、仮に落ちても
通知が1回出ないだけで済む）。

第2段（tray-tools 統合）: 子プロセスの中身を win11toast（WinRT）から、標準
ライブラリだけのバルーン通知（Shell_NotifyIcon）に差し替えた。WinRT への依存
自体を無くすのが目的で、分離の形（常駐は通知の実装を import しない）は保つ。

互換のための引数:
- on_click: クリックで開く先（file の URI か http(s) の URL かパス）。
  None ならクリックしても何もしない（バルーンが閉じるだけ）。
- buttons / xml: 旧 win11toast 用。バルーンには無いので受け取って無視する。
  （texts の「ブラウザで開く」ボタンは無くなる。本文クリックで URL が開く形に
  なるので、開く動作そのものは保たれる。）
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .config import PROJECT_ROOT

log = logging.getLogger("landrop")

Launcher = Callable[[list[str]], object]


def _default_launcher(cmd: list[str]) -> object:
    return subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,  # -m landrop.toast を解決できるように
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def toast(title: str, body: str, *, launcher: Launcher | None = None, **kwargs) -> None:
    """通知を子プロセスで出す。待たずに戻る。引数は旧 win11toast と互換。"""
    payload = json.dumps({"title": title, "body": body, **kwargs}, ensure_ascii=False)
    try:
        (launcher or _default_launcher)([sys.executable, "-m", "landrop.toast", payload])
    except Exception:
        log.exception("通知を出せませんでした")


def click_target(on_click: object) -> str | None:
    """on_click を「開ける形」に直す。file URI はローカルパスへ、URL とパスはそのまま。"""
    if not on_click or not isinstance(on_click, str):
        return None
    parsed = urlparse(on_click)
    if parsed.scheme == "file":
        return url2pathname(parsed.path)
    return on_click


def _main(argv: list[str]) -> None:
    payload = json.loads(argv[1])
    try:
        _show_balloon(
            str(payload.get("title", "")),
            str(payload.get("body", "")),
            payload.get("on_click"),
        )
    except Exception:
        log.exception("通知を表示できませんでした")


def _show_balloon(title: str, body: str, on_click: object) -> None:
    """バルーン通知を出す（Windows 専用・標準ライブラリのみ）。クリック/時間切れで戻る。"""
    import ctypes
    import os
    import time
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_USER = 0x0400
    CALLBACK_MSG = WM_USER + 20
    NIN_BALLOONUSERCLICK = WM_USER + 5
    NIN_BALLOONTIMEOUT = WM_USER + 4
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
    NIIF_INFO = 0x1
    IDI_APPLICATION = 32512
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    )

    clicked: list[bool] = []

    def _wnd_proc(hwnd, msg, wparam, lparam):
        if msg == CALLBACK_MSG and lparam in (NIN_BALLOONUSERCLICK, NIN_BALLOONTIMEOUT):
            if lparam == NIN_BALLOONUSERCLICK:
                clicked.append(True)
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    hinst = kernel32.GetModuleHandleW(None)
    proc = WNDPROC(_wnd_proc)  # GC で回収されないよう保持する
    wc = WNDCLASSW(0, proc, 0, 0, hinst, None, None, None, None, "LandropBalloon")
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(
        0, "LandropBalloon", "LandropBalloon", 0, 0, 0, 0, 0, None, None, hinst, None
    )

    icon_path = Path(__file__).resolve().parent / "static" / "icon.ico"
    if icon_path.exists():
        hicon = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
        )
    else:
        hicon = user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR))

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = CALLBACK_MSG
    nid.hIcon = hicon
    nid.szTip = title[:127]
    shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    nid.uFlags = NIF_INFO
    nid.szInfo = body[:255]
    nid.szInfoTitle = title[:63]
    nid.dwInfoFlags = NIIF_INFO
    nid.uTimeoutOrVersion = 10000
    shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    # バルーンが閉じるかクリックされるまで、最大25秒だけメッセージを回す
    msg = wintypes.MSG()
    deadline = time.time() + 25
    while time.time() < deadline:
        if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            if msg.message == 0x0012:  # WM_QUIT
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        else:
            time.sleep(0.05)

    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
    user32.DestroyWindow(hwnd)

    if clicked:
        target = click_target(on_click)
        if target:
            os.startfile(target)  # noqa: S606 - 通知クリックの既定動作（受信フォルダ/URL）


if __name__ == "__main__":
    _main(sys.argv)
