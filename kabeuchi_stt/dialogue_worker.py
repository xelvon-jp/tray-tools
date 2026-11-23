# -*- coding: utf-8 -*-
"""AI対話モード: 常駐ワーカー（別プロセス）。Win32 と UI Automation の実作業を担う。

起動: <worker_python> dialogue_worker.py（標準入出力で JSON 1行 = 1リクエスト／1レスポンス、UTF-8）。
  リクエスト  {"id": n, "op": "ping|pick|resolve|deliver|cleanup|shutdown", ...引数}
  レスポンス  {"id": n, "ok": true, "result": {...}} / {"id": n, "ok": false, "error": "..."}
stdout は応答 JSON 以外を書かない（起動時に print の行き先を stderr へ付け替える）。診断は stderr。

【約束】
- import しただけでは何も起きない（DLL の読み込み・DPI・COM は RealOps を作って start() するまでしない）。
- PySide6・numpy・faster-whisper と、本体直下の settings.py / hotkeys.py 等は import しない。
  import してよいのは dialogue_profile / dialogue_deliver と標準ライブラリ・ctypes・comtypes（遅延）だけ。
- ctypes は argtypes / restype を必ず指定する（64bit でハンドルが切り詰められるため）。
- COM ポインタは関数の外へ出さない（辞書にして返す）。型変換は QueryInterface で行い、ctypes の型変換関数は使わない。
  COMError は OSError ではないので except Exception で受ける。
- キー送信・クリックは、関数の中で「いま前面（または点の下）の窓が expected_root か」を再確認し、違えば
  例外を上げて何も送らない。呼び出し側の確認を信用しない作りにしてある。
- グローバルフックは使わない。クリックの検出は GetAsyncKeyState のポーリング。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

import dialogue_deliver as dd
import dialogue_profile as dp

WORKER_VERSION = "20261123-02"
BUTTON_PROXIMITY_PX = 300       # 送信・回答中ボタンは、入力欄の四方この距離以内にあるものだけを数える

GA_ROOT = 2
VK_LBUTTON, VK_RBUTTON, VK_ESCAPE = 0x01, 0x02, 0x1B
VK_CONTROL, VK_END, VK_A, VK_V = 0x11, 0x23, 0x41, 0x56
VK_RETURN = 0x0D
SM_SWAPBUTTON = 23
SW_RESTORE = 9
INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
WM_GETOBJECT, OBJID_CLIENT, SMTO_ABORTIFHUNG = 0x003D, 0xFFFFFFFC, 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GMEM_MOVEABLE = 0x0002
FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC


class ForegroundMismatch(Exception):
    """送る直前の前面の窓が、想定の窓ではなかった（何も送っていない）。"""


class PointMismatch(Exception):
    """クリックしようとした点の下の最上位の窓が、想定の窓ではなかった（何も送っていない）。"""


# --- 構造体（定義だけ。import で副作用は無い）---
ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD),
                ("uCount", wt.UINT), ("dwTimeout", wt.DWORD)]


def log(msg):
    """診断は stderr へ。"""
    try:
        sys.stderr.write(f"[dialogue_worker] {msg}\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# クリップボードの形式の分類（純関数。単体テストできる）
# ---------------------------------------------------------------------------

CF_BITMAP, CF_METAFILEPICT, CF_DIB, CF_PALETTE, CF_ENHMETAFILE, CF_DIBV5 = 2, 3, 8, 9, 14, 17
CF_OWNERDISPLAY = 0x0080
_DSP_HANDLE_FORMATS = (0x0082, 0x0083, 0x008E)   # CF_DSPBITMAP / CF_DSPMETAFILEPICT / CF_DSPENHMETAFILE


def classify_clipboard_format(fmt, all_fmts):
    """形式 1 つの扱い。戻りは "save"（メモリのバイト列として保存できる）/
    "skip"（ハンドル系だが、保存できる別形式があれば復元時に自動で作られるので保存しなくてよい）/
    "unrestorable"（保存できない）。"""
    have_dib = CF_DIB in all_fmts or CF_DIBV5 in all_fmts
    if fmt in (CF_BITMAP, CF_PALETTE):
        return "skip" if have_dib else "unrestorable"
    if fmt in (CF_METAFILEPICT, CF_ENHMETAFILE, CF_OWNERDISPLAY) or fmt in _DSP_HANDLE_FORMATS:
        return "unrestorable"
    if 0x0200 <= fmt <= 0x02FF or 0x0300 <= fmt <= 0x03FF:   # 私的形式（中身はアプリ次第）・GDI オブジェクト
        return "unrestorable"
    return "save"


def classify_clipboard(fmts):
    """形式の番号リストを分類する。{"save": [...], "skip": [...], "unrestorable": [...]}。"""
    fmts = list(fmts)
    out = {"save": [], "skip": [], "unrestorable": []}
    for f in fmts:
        out[classify_clipboard_format(f, fmts)].append(f)
    return out


# ---------------------------------------------------------------------------
# Win32（ctypes）。インスタンスを作ったときに初めて DLL を読み、API を宣言する
# ---------------------------------------------------------------------------

class Win32:
    def __init__(self):
        u = ctypes.WinDLL("user32", use_last_error=True)
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self._u, self._k = u, k

        def decl(fn, argtypes, restype):
            fn.argtypes = argtypes
            fn.restype = restype
            return fn

        self.GetForegroundWindow = decl(u.GetForegroundWindow, [], wt.HWND)
        self.GetAncestor = decl(u.GetAncestor, [wt.HWND, wt.UINT], wt.HWND)
        self.GetWindowThreadProcessId = decl(u.GetWindowThreadProcessId,
                                             [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD)
        self.GetClassNameW = decl(u.GetClassNameW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
        self.GetWindowTextLengthW = decl(u.GetWindowTextLengthW, [wt.HWND], ctypes.c_int)
        self.GetWindowTextW = decl(u.GetWindowTextW, [wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
        self.GetWindowRect = decl(u.GetWindowRect, [wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL)
        self.IsIconic = decl(u.IsIconic, [wt.HWND], wt.BOOL)
        self.IsWindowVisible = decl(u.IsWindowVisible, [wt.HWND], wt.BOOL)
        self.IsWindow = decl(u.IsWindow, [wt.HWND], wt.BOOL)
        self.WindowFromPoint = decl(u.WindowFromPoint, [wt.POINT], wt.HWND)
        self.GetCursorPos = decl(u.GetCursorPos, [ctypes.POINTER(wt.POINT)], wt.BOOL)
        self.SetCursorPos = decl(u.SetCursorPos, [ctypes.c_int, ctypes.c_int], wt.BOOL)
        self.GetAsyncKeyState = decl(u.GetAsyncKeyState, [ctypes.c_int], ctypes.c_short)
        self.GetSystemMetrics = decl(u.GetSystemMetrics, [ctypes.c_int], ctypes.c_int)
        self.SetForegroundWindow = decl(u.SetForegroundWindow, [wt.HWND], wt.BOOL)
        self.ShowWindow = decl(u.ShowWindow, [wt.HWND, ctypes.c_int], wt.BOOL)
        self.AttachThreadInput = decl(u.AttachThreadInput, [wt.DWORD, wt.DWORD, wt.BOOL], wt.BOOL)
        self.SendInput = decl(u.SendInput, [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int], wt.UINT)
        self.EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        self.EnumWindows = decl(u.EnumWindows, [self.EnumProc, wt.LPARAM], wt.BOOL)
        self.EnumChildWindows = decl(u.EnumChildWindows, [wt.HWND, self.EnumProc, wt.LPARAM], wt.BOOL)
        self.SendMessageTimeoutW = decl(u.SendMessageTimeoutW,
                                        [wt.HWND, wt.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                                         wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)],
                                        ctypes.c_ssize_t)
        self.FlashWindowEx = decl(u.FlashWindowEx, [ctypes.POINTER(_FLASHWINFO)], wt.BOOL)
        self.GetLastInputInfo = decl(u.GetLastInputInfo, [ctypes.POINTER(_LASTINPUTINFO)], wt.BOOL)
        # クリップボード
        self.OpenClipboard = decl(u.OpenClipboard, [wt.HWND], wt.BOOL)
        self.CloseClipboard = decl(u.CloseClipboard, [], wt.BOOL)
        self.EmptyClipboard = decl(u.EmptyClipboard, [], wt.BOOL)
        self.GetClipboardData = decl(u.GetClipboardData, [wt.UINT], ctypes.c_void_p)
        self.SetClipboardData = decl(u.SetClipboardData, [wt.UINT, ctypes.c_void_p], ctypes.c_void_p)
        self.EnumClipboardFormats = decl(u.EnumClipboardFormats, [wt.UINT], wt.UINT)
        self.GetClipboardFormatNameW = decl(u.GetClipboardFormatNameW,
                                            [wt.UINT, wt.LPWSTR, ctypes.c_int], ctypes.c_int)
        self.GlobalAlloc = decl(k.GlobalAlloc, [wt.UINT, ctypes.c_size_t], ctypes.c_void_p)
        self.GlobalFree = decl(k.GlobalFree, [ctypes.c_void_p], ctypes.c_void_p)
        self.GlobalLock = decl(k.GlobalLock, [ctypes.c_void_p], ctypes.c_void_p)
        self.GlobalUnlock = decl(k.GlobalUnlock, [ctypes.c_void_p], wt.BOOL)
        self.GlobalSize = decl(k.GlobalSize, [ctypes.c_void_p], ctypes.c_size_t)
        self.GetCurrentThreadId = decl(k.GetCurrentThreadId, [], wt.DWORD)
        self.GetTickCount = decl(k.GetTickCount, [], wt.DWORD)
        self.OpenProcess = decl(k.OpenProcess, [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE)
        self.CloseHandle = decl(k.CloseHandle, [wt.HANDLE], wt.BOOL)
        self.QueryFullProcessImageNameW = decl(k.QueryFullProcessImageNameW,
                                               [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)],
                                               wt.BOOL)

    # --- DPI（起動時に 1 回）---
    def init_dpi(self):
        """Per-Monitor DPI aware v2 にする。座標を UIA・WindowFromPoint・SetCursorPos で揃えるため。"""
        try:
            f = self._u.SetProcessDpiAwarenessContext
            f.argtypes = [ctypes.c_void_p]
            f.restype = wt.BOOL
            if f(ctypes.c_void_p(-4)):
                return "per_monitor_v2"
        except Exception:  # noqa: BLE001
            pass
        try:
            shcore = ctypes.WinDLL("shcore")
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            if shcore.SetProcessDpiAwareness(2) == 0:
                return "per_monitor(shcore)"
        except Exception:  # noqa: BLE001
            pass
        try:
            self._u.SetProcessDPIAware.argtypes = []
            self._u.SetProcessDPIAware.restype = wt.BOOL
            if self._u.SetProcessDPIAware():
                return "system_aware"
        except Exception:  # noqa: BLE001
            pass
        return "none"

    # --- 窓の情報 ---
    @staticmethod
    def hv(h):
        return int(h) if h else 0

    def root_of(self, hwnd):
        if not hwnd:
            return 0
        return self.hv(self.GetAncestor(hwnd, GA_ROOT)) or self.hv(hwnd)

    def class_of(self, hwnd):
        buf = ctypes.create_unicode_buffer(256)
        self.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def title_of(self, hwnd):
        n = self.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(max(n, 0) + 2)
        self.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def pid_of(self, hwnd):
        pid = wt.DWORD(0)
        self.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def exe_of_pid(self, pid):
        h = self.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if self.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            self.CloseHandle(h)

    def rect_of(self, hwnd):
        r = wt.RECT()
        if not self.GetWindowRect(hwnd, ctypes.byref(r)):
            return (0, 0, 0, 0)
        return (r.left, r.top, r.right, r.bottom)

    def window_info(self, hwnd):
        hwnd = self.hv(hwnd)
        pid = self.pid_of(hwnd) if hwnd else 0
        return {
            "hwnd": hwnd, "root": self.root_of(hwnd), "pid": pid,
            "exe": self.exe_of_pid(pid) if pid else "",
            "class": self.class_of(hwnd) if hwnd else "",
            "title": self.title_of(hwnd) if hwnd else "",
            "rect": self.rect_of(hwnd) if hwnd else (0, 0, 0, 0),
            "is_iconic": bool(self.IsIconic(hwnd)) if hwnd else False,
            "is_visible": bool(self.IsWindowVisible(hwnd)) if hwnd else False,
        }

    def is_window(self, hwnd):
        return bool(self.IsWindow(hwnd))

    def enum_top_windows(self):
        """タイトルのある可視（または最小化）のトップレベル窓の window_info を並べる。"""
        hits = []

        def on(hwnd, _l):
            try:
                if not (self.IsWindowVisible(hwnd) or self.IsIconic(hwnd)):
                    return True
                if not self.title_of(hwnd).strip():
                    return True
                hits.append(self.window_info(hwnd))
            except Exception:  # noqa: BLE001
                pass
            return True

        cb = self.EnumProc(on)      # 呼び出しの間、コールバックを生かしておく
        self.EnumWindows(cb, 0)
        return hits

    def find_render_child(self, root_hwnd):
        """Chromium のレンダラ HWND（Chrome_RenderWidgetHostHWND）。無ければ 0。"""
        found = {"h": 0}

        def walk(parent):
            def on(child, _l):
                if found["h"]:
                    return False
                if self.class_of(child) == "Chrome_RenderWidgetHostHWND":
                    found["h"] = self.hv(child)
                    return False
                walk(child)
                return not found["h"]
            cb = self.EnumProc(on)
            self.EnumChildWindows(parent, cb, 0)

        walk(root_hwnd)
        return found["h"]

    def wake_message(self, hwnd):
        """WM_GETOBJECT を 1 回投げる（Chromium にアクセシビリティツリーを作らせる）。"""
        out = ctypes.c_size_t(0)
        self.SendMessageTimeoutW(hwnd, WM_GETOBJECT, 0, ctypes.c_ssize_t(OBJID_CLIENT),
                                 SMTO_ABORTIFHUNG, 1000, ctypes.byref(out))

    def window_from_point(self, x, y):
        return self.hv(self.WindowFromPoint(wt.POINT(int(x), int(y))))

    def root_of_point(self, x, y):
        return self.root_of(self.window_from_point(x, y))

    def cursor_pos(self):
        p = wt.POINT()
        self.GetCursorPos(ctypes.byref(p))
        return (p.x, p.y)

    # --- 前面 ---
    def foreground_root(self):
        return self.root_of(self.GetForegroundWindow())

    def foreground_is(self, hwnd_root):
        fg = self.foreground_root()
        return bool(fg) and fg == self.root_of(hwnd_root)

    def wait_foreground(self, hwnd_root, timeout=2.0, poll=0.05):
        t0 = time.perf_counter()
        while True:
            if self.foreground_is(hwnd_root):
                return True
            if time.perf_counter() - t0 >= timeout:
                return False
            time.sleep(poll)

    def set_foreground(self, hwnd):
        ok = bool(self.SetForegroundWindow(hwnd))
        return {"api_ok": ok, "last_error": ctypes.get_last_error()}

    def show_restore_then_set(self, hwnd):
        """最小化されていれば SW_RESTORE してから SetForegroundWindow。
        最小化していないときは SW_RESTORE しない（最大化の窓を通常サイズへ戻してしまうため）。"""
        restored = False
        if self.IsIconic(hwnd):
            self.ShowWindow(hwnd, SW_RESTORE)
            restored = True
        ok = bool(self.SetForegroundWindow(hwnd))
        return {"api_ok": ok, "restored": restored, "last_error": ctypes.get_last_error()}

    def attach_thread_input_foreground(self, hwnd):
        """いまの前面窓のスレッドに自スレッドの入力を結びつけてから SetForegroundWindow（結びつけは必ず外す）。"""
        fg = self.GetForegroundWindow()
        fg_tid = self.GetWindowThreadProcessId(fg, None) if fg else 0
        me = self.GetCurrentThreadId()
        attached = False
        try:
            if fg_tid and fg_tid != me:
                attached = bool(self.AttachThreadInput(me, fg_tid, True))
            return {"api_ok": bool(self.SetForegroundWindow(hwnd)), "attached": attached}
        finally:
            if attached:
                self.AttachThreadInput(me, fg_tid, False)

    def flash(self, hwnd):
        """タスクバーの点滅（前面にならなかったことを知らせる）。キーもマウスも送らない。"""
        info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, FLASHW_ALL | FLASHW_TIMERNOFG, 3, 0)
        return bool(self.FlashWindowEx(ctypes.byref(info)))

    def last_input_ms(self):
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not self.GetLastInputInfo(ctypes.byref(info)):
            return None
        return (int(self.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF

    # --- クリック検出（グローバルフック無し）---
    def _down(self, vk):
        return bool(self.GetAsyncKeyState(vk) & 0x8000)

    def _logical_left_vk(self):
        return VK_RBUTTON if self.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON

    def wait_for_click(self, timeout=30.0, poll=0.02):
        """左ボタンの押下エッジを待つ（クリックは横取りしない）。Esc でキャンセル。
        戻りは {"status": "pressed"|"esc"|"timeout", "x", "y", "hwnd", "root"}。"""
        vk = self._logical_left_vk()
        t0 = time.perf_counter()
        while (self._down(vk) or self._down(VK_ESCAPE)) and time.perf_counter() - t0 < timeout:
            time.sleep(poll)       # 始めに押されている状態は離すまで待つ
        while True:
            if time.perf_counter() - t0 >= timeout:
                return {"status": "timeout"}
            if self._down(VK_ESCAPE):
                return {"status": "esc"}
            if self._down(vk):
                x, y = self.cursor_pos()
                h = self.window_from_point(x, y)
                return {"status": "pressed", "x": x, "y": y, "hwnd": h, "root": self.root_of(h)}
            time.sleep(poll)

    # --- キー送信・クリック（送る直前に必ず再確認）---
    @staticmethod
    def _key_input(vk, up=False):
        i = _INPUT(type=INPUT_KEYBOARD)
        i.ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=0)
        return i

    def _send(self, inputs):
        arr = (_INPUT * len(inputs))(*inputs)
        n = self.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))
        if n != len(inputs):
            raise OSError(f"SendInput が {n}/{len(inputs)} 件しか通らなかった (last_error={ctypes.get_last_error()})")
        return n

    def _require_foreground(self, expected_root):
        if not expected_root:
            raise ForegroundMismatch("expected_root が空")
        if not self.foreground_is(expected_root):
            raise ForegroundMismatch(
                f"前面が想定の窓ではない (前面={self.foreground_root():#x}, 想定={expected_root:#x})")

    def _combo(self, expected_root, vk):
        self._require_foreground(expected_root)
        return self._send([self._key_input(VK_CONTROL), self._key_input(vk),
                           self._key_input(vk, True), self._key_input(VK_CONTROL, True)])

    def send_ctrl_v(self, expected_root):
        """Ctrl+V。送る直前に前面が expected_root か再確認し、違えば送らず例外。"""
        return self._combo(expected_root, VK_V)

    def send_ctrl_a(self, expected_root):
        return self._combo(expected_root, VK_A)

    def send_ctrl_end(self, expected_root):
        return self._combo(expected_root, VK_END)

    def send_enter(self, expected_root):
        """Enter キー。送る直前に前面が expected_root か再確認し、違えば送らず例外。"""
        self._require_foreground(expected_root)
        return self._send([self._key_input(VK_RETURN), self._key_input(VK_RETURN, True)])

    def real_click(self, x, y, expected_root, restore_cursor=True):
        """(x, y) を本物のマウスで左クリック。事前に点の下の最上位の窓が expected_root か確認し、
        違えば例外（別の窓へのクリック事故防止）。カーソルを動かした後にも再確認し、終わったら元へ戻す。"""
        if not expected_root or self.root_of_point(x, y) != self.root_of(expected_root):
            raise PointMismatch(f"点 ({x},{y}) の下の最上位の窓が想定の窓ではない")
        orig = self.cursor_pos()
        moved = False
        try:
            self.SetCursorPos(int(x), int(y))
            moved = True
            time.sleep(0.05)
            if self.root_of_point(x, y) != self.root_of(expected_root):
                raise PointMismatch(f"カーソル移動後、点 ({x},{y}) の下の窓が変わった")
            down = _INPUT(type=INPUT_MOUSE)
            down.mi = _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)
            up = _INPUT(type=INPUT_MOUSE)
            up.mi = _MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)
            self._send([down])
            time.sleep(0.04)
            self._send([up])
            time.sleep(0.05)
        finally:
            if moved and restore_cursor:
                self.SetCursorPos(orig[0], orig[1])
        return {"clicked": True, "point": (int(x), int(y)), "cursor_restored": self.cursor_pos() == orig}

    # --- クリップボード（全形式の退避・復元）---
    def _open_clipboard(self, retries=20, wait=0.05):
        for _ in range(retries):
            if self.OpenClipboard(None):
                return True
            time.sleep(wait)
        return False

    def _format_list(self):
        """開いた状態で、いまの形式の番号を列挙する。"""
        out, fmt = [], 0
        while True:
            fmt = self.EnumClipboardFormats(fmt)
            if not fmt:
                break
            out.append(int(fmt))
        return out

    def _format_name(self, fmt):
        buf = ctypes.create_unicode_buffer(128)
        n = self.GetClipboardFormatNameW(fmt, buf, 128)
        return buf.value if n > 0 else str(fmt)

    def _read_format_bytes(self, fmt):
        """開いた状態で、形式 fmt のメモリをバイト列にする。読めなければ None。"""
        h = self.GetClipboardData(fmt)
        if not h:
            return None
        size = self.GlobalSize(h)
        if not size:
            return None
        p = self.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.string_at(p, size)
        finally:
            self.GlobalUnlock(h)

    def clipboard_snapshot(self):
        """元のクリップボードの控え（全形式）。
        戻りは {"kind": "empty"|"data"|"unavailable", "items": [(形式番号, バイト列)], "formats": [名前],
                "restorable": bool, "unrestorable": [名前]}。
        restorable は「保存できない形式が無い」ときだけ True。ハンドル系（画像のハンドル等）・私的形式・
        遅延レンダリングで読めない形式があれば False（貼り付けを諦める判断材料にする）。"""
        if not self._open_clipboard():
            return {"kind": "unavailable", "items": [], "formats": [], "restorable": False,
                    "unrestorable": ["(開けない)"]}
        try:
            fmts = self._format_list()
            names = [self._format_name(f) for f in fmts]
            if not fmts:
                return {"kind": "empty", "items": [], "formats": [], "restorable": True, "unrestorable": []}
            plan = classify_clipboard(fmts)
            items, bad = [], [self._format_name(f) for f in plan["unrestorable"]]
            for f in plan["save"]:
                data = self._read_format_bytes(f)
                if data is None:
                    bad.append(self._format_name(f))
                else:
                    items.append((f, data))
            return {"kind": "data", "items": items, "formats": names, "restorable": not bad,
                    "unrestorable": bad}
        finally:
            self.CloseClipboard()

    def _set_open(self, fmt, data):
        h = self.GlobalAlloc(GMEM_MOVEABLE, max(len(data), 1))
        if not h:
            return False
        p = self.GlobalLock(h)
        if not p:
            self.GlobalFree(h)
            return False
        ctypes.memmove(p, data, len(data))
        self.GlobalUnlock(h)
        if not self.SetClipboardData(fmt, h):
            self.GlobalFree(h)
            return False
        return True           # 成功したら所有権はシステムへ移る（解放しない）

    def clipboard_set_text(self, text):
        if not self._open_clipboard():
            return False
        try:
            if not self.EmptyClipboard():
                return False
            return self._set_open(13, (text + "\0").encode("utf-16-le"))   # CF_UNICODETEXT
        finally:
            self.CloseClipboard()

    def clipboard_restore(self, snap):
        """控えを戻す。全部戻せたら True。restorable でない控えは何もせず False。"""
        if not snap or not snap.get("restorable"):
            return False
        if not self._open_clipboard():
            return False
        try:
            if not self.EmptyClipboard():
                return False
            ok = True
            for fmt, data in snap.get("items", []):
                ok = self._set_open(fmt, data) and ok
            return ok
        finally:
            self.CloseClipboard()


# ---------------------------------------------------------------------------
# UI Automation（comtypes。import は init() まで遅延）
# ---------------------------------------------------------------------------

TYPE_NAMES = {
    50000: "Button", 50002: "CheckBox", 50003: "Edit", 50004: "ComboBox",
    50005: "Link", 50006: "Image", 50007: "ListItem", 50008: "List",
    50011: "MenuItem", 50013: "RadioButton", 50020: "Text", 50021: "ToolBar",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header",
}
PATTERN_IDS = [(10002, "Value"), (10014, "Text"), (10000, "Invoke"), (10018, "LegacyIAccessible"),
               (10005, "ExpandCollapse"), (10004, "Scroll"), (10010, "SelectionItem")]
VALUE_PATTERN, TEXT_PATTERN = 10002, 10014


def _exc(e):
    return f"{type(e).__name__}: {e}"


class Uia:
    """UIA 操作。COM ポインタは辞書にして返し、関数の外へ出さない。入力欄に対する操作は
    _run_on_input の中（root・FindAll の結果を保持したまま）で行う。"""

    def __init__(self, win):
        self.win = win
        self._UIA = None
        self._auto = None

    def init(self):
        if self._auto is not None:
            return True
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as UIA
        self._UIA = UIA
        self._auto = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        return True

    def _ensure(self):
        self.init()
        return self._UIA, self._auto

    @staticmethod
    def _hwnd_ptr(h):
        return ctypes.c_void_p(h)

    # --- 要素 → 辞書 ---
    def _patterns_of(self, el):
        names = []
        for pid, label in PATTERN_IDS:
            try:
                if el.GetCurrentPattern(pid):
                    names.append(label)
            except Exception:  # noqa: BLE001
                continue
        return names

    @staticmethod
    def _rect_of(el):
        try:
            r = el.CurrentBoundingRectangle
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:  # noqa: BLE001
            return (0, 0, 0, 0)

    def _describe(self, el, with_patterns=False):
        d = {"control_type": "", "name": "", "automation_id": "", "class_name": "",
             "rect": (0, 0, 0, 0), "native_hwnd": 0}
        try:
            t = int(el.CurrentControlType)
            d["control_type"] = TYPE_NAMES.get(t, str(t))
        except Exception:  # noqa: BLE001
            pass
        for key, attr in (("name", "CurrentName"), ("automation_id", "CurrentAutomationId"),
                          ("class_name", "CurrentClassName")):
            try:
                d[key] = (getattr(el, attr) or "").strip()
            except Exception:  # noqa: BLE001
                pass
        d["rect"] = self._rect_of(el)
        try:
            d["native_hwnd"] = int(el.CurrentNativeWindowHandle or 0)
        except Exception:  # noqa: BLE001
            pass
        if with_patterns:
            d["patterns"] = self._patterns_of(el)
        return d

    def _value_pattern(self, el):
        try:
            p = el.GetCurrentPattern(VALUE_PATTERN)
            if not p:
                return None
            return p.QueryInterface(self._UIA.IUIAutomationValuePattern)
        except Exception:  # noqa: BLE001
            return None

    def _text_pattern(self, el):
        try:
            p = el.GetCurrentPattern(TEXT_PATTERN)
            if not p:
                return None
            return p.QueryInterface(self._UIA.IUIAutomationTextPattern)
        except Exception:  # noqa: BLE001
            return None

    # --- 起こす ---
    def wake(self, hwnd_root, max_wait=4.0, ready_count=30):
        """Chromium 系のアクセシビリティツリーを起こし、子孫が ready_count 以上になるまで最大 max_wait 秒待つ。"""
        u, auto = self._ensure()
        t0 = time.perf_counter()
        render = self.win.find_render_child(hwnd_root)
        root = auto.ElementFromHandle(self._hwnd_ptr(hwnd_root))
        cond = auto.CreateTrueCondition()

        def count():
            try:
                return int(root.FindAll(u.TreeScope_Descendants, cond).Length)
            except Exception:  # noqa: BLE001
                return -1

        before = count()
        now = before
        if before < ready_count:
            while True:
                self.win.wake_message(hwnd_root)
                if render:
                    self.win.wake_message(render)
                time.sleep(0.4)
                now = count()
                if now >= ready_count or time.perf_counter() - t0 >= max_wait:
                    break
        return {"count_before": before, "count_after": now, "woke": now > before,
                "waited_ms": int((time.perf_counter() - t0) * 1000), "render_hwnd": render}

    # --- 点の下の要素 ---
    def _chain_of(self, el, hwnd_root):
        """要素の親の連なり（近い順、根まで）。辞書のリスト。"""
        _u, auto = self._ensure()
        walker = auto.ControlViewWalker
        chain = []
        node = el
        for _ in range(40):
            if node is None:
                break
            try:
                if int(node.CurrentNativeWindowHandle or 0) == hwnd_root:
                    break
            except Exception:  # noqa: BLE001
                pass
            try:
                parent = walker.GetParentElement(node)
            except Exception:  # noqa: BLE001
                break
            if parent is None:
                break
            pd = self._describe(parent)
            chain.append({k: pd[k] for k in ("control_type", "name", "automation_id", "class_name")})
            if pd["native_hwnd"] == hwnd_root:
                break
            node = parent
        return chain

    def _grab_at_point(self, x, y, hwnd_root):
        u, auto = self._ensure()
        pt = u.tagPOINT()
        pt.x, pt.y = int(x), int(y)
        el = auto.ElementFromPoint(pt)
        if el is None:
            return None
        info = self._describe(el, with_patterns=True)
        info["has_value"] = self._value_pattern(el) is not None
        info["chain"] = self._chain_of(el, hwnd_root)
        info["is_container_only"] = (
            info["native_hwnd"] == hwnd_root
            or info["class_name"].startswith(("Chrome_RenderWidgetHostHWND", "Chrome_WidgetWin")))
        return info

    def element_at_point(self, x, y, hwnd_root):
        """(x, y) の下の UIA 要素を辞書で返す。取れなければ {"error": 理由}。
        先に wake し、窓枠だけが取れた・例外だったときは 1.5 秒待って 1 回だけ再試行。"""
        wake_info = self.wake(hwnd_root)
        info, err = None, ""
        try:
            info = self._grab_at_point(x, y, hwnd_root)
        except Exception as e:  # noqa: BLE001
            err = _exc(e)
        retried = False
        if info is None or info.get("is_container_only"):
            retried = True
            time.sleep(1.5)
            try:
                info2 = self._grab_at_point(x, y, hwnd_root)
                if info2 is not None:
                    info = info2
            except Exception as e:  # noqa: BLE001
                err = _exc(e)
        if info is None:
            return {"error": err or "要素が取れなかった", "wake": wake_info, "retried": retried}
        info["wake"] = wake_info
        info["retried"] = retried
        return info

    # --- 窓の中の要素を集める ---
    def _gather(self, auto, u, root, pt):
        """root の全子孫を辞書にする。coll は呼び出し側が保持すること。戻りは (coll, infos, win_rect)。
        has_value は入力欄っぽい種類か、pt を含む要素だけ調べる（他は None）。"""
        win_rect = self._rect_of(root)
        coll = root.FindAll(u.TreeScope_Descendants, auto.CreateTrueCondition())
        infos = []
        for i in range(int(coll.Length)):
            el = coll.GetElement(i)
            d = self._describe(el)
            r = d["rect"]
            near = pt is not None and r[0] <= pt[0] < r[2] and r[1] <= pt[1] < r[3]
            d["has_value"] = (self._value_pattern(el) is not None) \
                if (d["control_type"] in ("Edit", "ComboBox", "Document") or near) else None
            d["index"] = i
            infos.append(d)
        return coll, infos, win_rect

    def descend_at(self, hwnd_root, rect, point=None):
        """pick 用: 値パターンの無い要素が当たったとき、その矩形内の入力欄（Edit/ComboBox/Document、
        値パターンあり）の要素情報を返す。無ければ None。"""
        u, auto = self._ensure()
        try:
            root = auto.ElementFromHandle(self._hwnd_ptr(hwnd_root))
            coll, infos, _wr = self._gather(auto, u, root, None)
            idx = dp.descend_input(infos, rect, point)
            if idx is None:
                return None
            el = coll.GetElement(idx)
            info = self._describe(el, with_patterns=True)
            info["has_value"] = True
            info["chain"] = self._chain_of(el, hwnd_root)
            return info
        except Exception as e:  # noqa: BLE001
            log(f"descend_at: {_exc(e)}")
            return None

    # --- 入力欄を探して操作する ---
    INPUT_CACHE_SEC = 30.0

    def _profile_key(self, profile):
        try:
            return json.dumps(profile, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return repr(profile)

    def _run_on_cached_input(self, hwnd_root, profile, action):
        """直前の find_input で見つけた入力欄の要素を使い回す（窓の全子孫を調べ直すと 2〜3 秒かかるため）。
        同じ窓・同じプロファイル・INPUT_CACHE_SEC 以内で、要素の位置が変わっていないときだけ。使えなければ None。"""
        c = getattr(self, "_input_cache", None)
        if not c or c["hwnd"] != hwnd_root or c["pkey"] != self._profile_key(profile):
            return None
        if time.monotonic() - c["t"] > self.INPUT_CACHE_SEC:
            return None
        el = c["el"]
        try:
            if self._rect_of(el) != c["rect"] or c["rect"] == (0, 0, 0, 0):
                return None
            out = {"found": True, "how": c["how"], "descended": c["descended"], "element": c["element"],
                   "result": None, "candidates": [], "error": "", "cached": True}
            out["result"] = action(el)
            return out
        except Exception:  # noqa: BLE001  要素が古くなっていた等は、通常の探し直しへ
            self._input_cache = None
            return None

    def _run_on_input(self, hwnd_root, profile, action, use_cache=False):
        """入力欄を見つけ、action(el) を実行する（要素は関数内に閉じる）。use_cache のときは直前に見つけた要素を使い回す。
        戻りは {found, how, descended, element, result, candidates, error}。"""
        if use_cache:
            hit = self._run_on_cached_input(hwnd_root, profile, action)
            if hit is not None:
                return hit
        u, auto = self._ensure()
        out = {"found": False, "how": "", "descended": False, "element": None, "result": None,
               "candidates": [], "error": ""}
        try:
            root = auto.ElementFromHandle(self._hwnd_ptr(hwnd_root))
            win_rect = self._rect_of(root)
            pt = None
            if profile.get("click_rel"):
                pt, _chg = dp.expected_point(profile, win_rect)
            coll, infos, win_rect = self._gather(auto, u, root, pt)
            res = dp.resolve_input(infos, profile, win_rect)
            out["descendants"] = len(infos)
            if res["index"] is None:
                out["candidates"] = dp.input_candidates(infos)
                out["error"] = res["note"]
                return out
            el = coll.GetElement(res["index"])   # root / coll はこの関数の間ずっと保持している
            out["found"] = True
            out["how"] = res["how"]
            out["descended"] = res["descended"]
            out["element"] = self._describe(el, with_patterns=True)
            self._input_cache = {"hwnd": hwnd_root, "pkey": self._profile_key(profile), "el": el,
                                 "rect": self._rect_of(el), "how": out["how"], "descended": out["descended"],
                                 "element": out["element"], "t": time.monotonic()}
            out["result"] = action(el)
        except Exception as e:  # noqa: BLE001  COMError は OSError ではない
            out["error"] = _exc(e)
        return out

    def find_input(self, hwnd_root, profile):
        return self._run_on_input(hwnd_root, profile, lambda el: {})

    def write_value(self, hwnd_root, profile, text):
        """ValuePattern.SetValue だけで書く（フォーカス操作もキー送信もしない）。"""
        def act(el):
            vp = self._value_pattern(el)
            if vp is None:
                return {"ok": False, "reason": "unwritable: ValuePattern が無い"}
            try:
                if vp.CurrentIsReadOnly:
                    return {"ok": False, "reason": "unwritable: ReadOnly"}
            except Exception:  # noqa: BLE001
                pass
            try:
                vp.SetValue(text)
                return {"ok": True, "reason": ""}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "reason": "SetValue が例外: " + _exc(e)}
        r = self._run_on_input(hwnd_root, profile, act, use_cache=True)
        if r.get("cached") and not (r["result"] or {}).get("ok"):      # 使い回した要素で書けなかった: 探し直して1回だけやり直す
            r = self._run_on_input(hwnd_root, profile, act)
        res = r["result"] or {"ok": False, "reason": r["error"] or "入力欄が見つからない"}
        return {**res, "found": r["found"], "how": r["how"]}

    def read_value(self, hwnd_root, profile):
        """ValuePattern.CurrentValue → TextPattern の順に読む。text が None なら読めなかった。"""
        def act(el):
            value_text = None
            vp = self._value_pattern(el)
            if vp is not None:
                try:
                    value_text = vp.CurrentValue or ""
                    if value_text.strip():
                        return {"text": value_text, "via": "value"}
                except Exception:  # noqa: BLE001
                    pass
            tp = self._text_pattern(el)
            if tp is not None:
                try:
                    t = tp.DocumentRange.GetText(-1) or ""
                    if t.strip() or value_text is None:
                        return {"text": t, "via": "text"}
                except Exception:  # noqa: BLE001
                    pass
            if value_text is not None:
                return {"text": value_text, "via": "value"}
            return {"text": None, "via": ""}
        r = self._run_on_input(hwnd_root, profile, act, use_cache=True)
        if r.get("cached") and (r["result"] or {}).get("text") is None:   # 使い回した要素で読めなかった: 探し直して1回だけやり直す
            r = self._run_on_input(hwnd_root, profile, act)
        res = r["result"] or {"text": None, "via": ""}
        return {"ok": res["text"] is not None, **res, "found": r["found"], "how": r["how"],
                "error": r["error"]}

    def _run_on_button(self, hwnd_root, names, near_rect, action):
        """窓の中の Button のうち、名前が names のどれかに（大文字小文字を無視して）完全一致するものを探す。
        near_rect（入力欄の矩形）があれば、その中心にいちばん近いものを選ぶ。action(el, info) の結果を返す。
        戻りは {found, enabled, name, rect, result, error}。"""
        u, auto = self._ensure()
        out = {"found": False, "enabled": False, "name": "", "rect": None, "result": None, "error": ""}
        want = [str(n).strip().casefold() for n in names if str(n).strip()]
        try:
            root = auto.ElementFromHandle(self._hwnd_ptr(hwnd_root))
            # Button だけを列挙する（全子孫を調べるより速い。回答中の待ちで何度も呼ばれるため）
            coll = root.FindAll(u.TreeScope_Descendants, auto.CreatePropertyCondition(30003, 50000))
            cands = []
            for i in range(int(coll.Length)):
                d = self._describe(coll.GetElement(i))
                d["index"] = i
                if d["name"].casefold() in want:
                    # 画面外（スクロールで隠れた・別ペインの）ボタンは数えない。同名のボタンが
                    # 同じ窓の別の場所（ブラウザペインのページ等）にあっても誤検出しないため
                    try:
                        if bool(coll.GetElement(i).CurrentIsOffscreen):
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    cands.append(d)
            if near_rect:
                # 入力欄のまわり（四方 PROXIMITY_PX 以内）にあるものだけに絞る
                pad = BUTTON_PROXIMITY_PX
                cands = [d for d in cands
                         if d["rect"][2] >= near_rect[0] - pad and d["rect"][0] <= near_rect[2] + pad
                         and d["rect"][3] >= near_rect[1] - pad and d["rect"][1] <= near_rect[3] + pad]
            if not cands:
                return out
            if near_rect:
                nx, ny = (near_rect[0] + near_rect[2]) / 2.0, (near_rect[1] + near_rect[3]) / 2.0

                def dist(d):
                    r = d["rect"]
                    return ((r[0] + r[2]) / 2.0 - nx) ** 2 + ((r[1] + r[3]) / 2.0 - ny) ** 2
                cands.sort(key=dist)
            d = cands[0]
            el = coll.GetElement(d["index"])
            out.update(found=True, name=d["name"], rect=d["rect"])
            try:
                out["enabled"] = bool(el.CurrentIsEnabled)
            except Exception:  # noqa: BLE001
                out["enabled"] = True
            out["result"] = action(el)
        except Exception as e:  # noqa: BLE001
            out["error"] = _exc(e)
        return out

    def find_button(self, hwnd_root, names, near_rect=None):
        r = self._run_on_button(hwnd_root, names, near_rect, lambda el: {})
        return {k: r[k] for k in ("found", "enabled", "name", "rect", "error")}

    def invoke_button(self, hwnd_root, names, near_rect=None):
        """UIA の Invoke（前面化・フォーカス不要）。無効なボタン・Invoke できないものは押さない。"""
        def act(el):
            try:
                if not bool(el.CurrentIsEnabled):
                    return {"invoked": False, "reason": "ボタンが無効"}
            except Exception:  # noqa: BLE001
                pass
            try:
                p = el.GetCurrentPattern(10000)
                if not p:
                    return {"invoked": False, "reason": "InvokePattern が無い"}
                p.QueryInterface(self._UIA.IUIAutomationInvokePattern).Invoke()
                return {"invoked": True, "reason": ""}
            except Exception as e:  # noqa: BLE001
                return {"invoked": False, "reason": "Invoke が例外: " + _exc(e)}
        r = self._run_on_button(hwnd_root, names, near_rect, act)
        res = r["result"] or {"invoked": False, "reason": r["error"] or "ボタンが見つからない"}
        return {**res, "found": r["found"], "name": r["name"]}

    def set_focus(self, hwnd_root, profile):
        """入力欄に UIA の SetFocus を呼び、その後のフォーカス要素が入力欄と一致したかを返す。"""
        def act(el):
            _u, auto = self._ensure()
            res = {"ok": False, "focused_matches": False}
            try:
                el.SetFocus()
                res["ok"] = True
            except Exception as e:  # noqa: BLE001
                res["error"] = _exc(e)
                return res
            time.sleep(0.15)
            try:
                res["focused_matches"] = bool(auto.CompareElements(el, auto.GetFocusedElement()))
            except Exception as e:  # noqa: BLE001
                res["error"] = _exc(e)
            return res
        r = self._run_on_input(hwnd_root, profile, act)
        res = r["result"] or {"ok": False, "focused_matches": False, "error": r["error"]}
        return {**res, "found": r["found"], "how": r["how"]}


# ---------------------------------------------------------------------------
# ops の本物の実装（dialogue_deliver が使う）
# ---------------------------------------------------------------------------

class RealOps:
    def __init__(self):
        self.win = None
        self.uia = None
        self.held = 0              # 握っている宛先窓（最上位 HWND）
        self.dpi = ""

    def start(self):
        """DLL・DPI・COM を初期化する（import では何もしない）。"""
        self.win = Win32()
        self.dpi = self.win.init_dpi()
        self.uia = Uia(self.win)
        self.uia.init()
        return self

    # --- 窓 ---
    def find_window(self, profile, hwnd=0):
        infos = self.win.enum_top_windows()
        sel = dp.select_window(infos, profile, hwnd or self.held)
        if sel["status"] == "ok":
            info = next((i for i in infos if int(i.get("root") or 0) == sel["hwnd"]), {})
            sel = {**sel, "is_iconic": info.get("is_iconic", False), "rect": info.get("rect")}
            self.held = sel["hwnd"]
        return sel

    def wake(self, hwnd):
        return self.uia.wake(hwnd)

    def find_input(self, hwnd, profile):
        return self.uia.find_input(hwnd, profile)

    def read_value(self, hwnd, profile):
        return self.uia.read_value(hwnd, profile)

    def write_value(self, hwnd, profile, text):
        return self.uia.write_value(hwnd, profile, text)

    def set_focus(self, hwnd, profile):
        return self.uia.set_focus(hwnd, profile)

    def foreground_root(self):
        return self.win.foreground_root()

    def wait_foreground(self, hwnd, timeout):
        return self.win.wait_foreground(hwnd, float(timeout))

    def bring_to_front(self, hwnd, profile, method):
        """前面化の手段を 1 つだけ実行する（他の手段へフォールバックしない）。"""
        if method == "uia_set_focus" and profile:
            return self.uia.set_focus(hwnd, profile)
        if method == "show_restore_then_set":
            return self.win.show_restore_then_set(hwnd)
        if method == "attach_thread_input":
            return self.win.attach_thread_input_foreground(hwnd)
        return self.win.set_foreground(hwnd)

    def root_of_point(self, x, y):
        return self.win.root_of_point(x, y)

    def click(self, x, y, expected_root):
        try:
            return self.win.real_click(x, y, expected_root)
        except PointMismatch as e:
            return {"clicked": False, "reason": str(e)}

    def paste(self, expected_root, mode):
        """Ctrl+V（replace なら先に Ctrl+A、append なら先に Ctrl+End）。各キーの直前に前面を再確認する。"""
        try:
            if mode == "replace":
                self.win.send_ctrl_a(expected_root)
                time.sleep(0.05)
            elif mode == "append":
                self.win.send_ctrl_end(expected_root)
                time.sleep(0.05)
            self.win.send_ctrl_v(expected_root)
            return {"sent": True}
        except ForegroundMismatch as e:
            return {"sent": False, "reason": str(e)}

    def find_button(self, hwnd, profile, names, near_rect):
        return self.uia.find_button(hwnd, names, near_rect)

    def invoke_button(self, hwnd, profile, names, near_rect):
        return self.uia.invoke_button(hwnd, names, near_rect)

    def send_enter(self, expected_root):
        try:
            self.win.send_enter(expected_root)
            return {"sent": True}
        except ForegroundMismatch as e:
            return {"sent": False, "reason": str(e)}

    def clipboard_snapshot(self):
        return self.win.clipboard_snapshot()

    def clipboard_set_text(self, text):
        return self.win.clipboard_set_text(text)

    def clipboard_restore(self, snap):
        return self.win.clipboard_restore(snap)

    def last_input_ms(self):
        return self.win.last_input_ms()

    def flash(self, hwnd):
        return self.win.flash(hwnd)

    # --- ワーカーの op ---
    def pick(self, args):
        """宛先の入力欄を 1 回クリックしてもらい、プロファイルを返す（何も書き込まない・送らない）。"""
        timeout = float(args.get("timeout") or 30.0)
        name = args.get("name") or ""
        exclude = {int(x) for x in (args.get("exclude_roots") or [])}
        click = self.win.wait_for_click(timeout)
        if click["status"] == "esc":
            return {"status": "cancelled"}
        if click["status"] != "pressed":
            return {"status": "timeout"}
        root = click["root"]
        if not root or root in exclude:
            return {"status": "own_window"}
        win = self.win.window_info(root)
        r = self.win.find_render_child(root)
        win["render_class"] = self.win.class_of(r) if r else ""
        el = self.uia.element_at_point(click["x"], click["y"], root)
        if "error" in el:
            return {"status": "no_element", "error": el["error"]}
        warnings, descended = [], False
        if not el.get("has_value"):
            inner = self.uia.descend_at(root, el.get("rect"), (click["x"], click["y"]))
            if inner is not None:
                el, descended = {**inner, "wake": el.get("wake"), "retried": el.get("retried")}, True
            else:
                warnings.append("no_value_pattern")
        if el.get("is_container_only"):
            warnings.append("container_only")
        profile = dp.build_profile(win, el, (click["x"], click["y"]), name)
        return {"status": "picked", "profile": profile, "descended": descended, "warnings": warnings,
                "window": {k: win[k] for k in ("hwnd", "root", "exe", "class", "title", "rect")},
                "element": {k: el.get(k) for k in ("control_type", "name", "automation_id", "class_name",
                                                   "rect", "patterns")}}

    def resolve(self, args):
        """プロファイルから窓を探して握る。見つからない・曖昧なら握らない。"""
        profile = args.get("profile") or {}
        sel = self.find_window(profile, int(args.get("hwnd") or 0))
        if sel["status"] != "ok":
            self.held = 0
        return sel

    def cleanup(self):
        self.held = 0
        return {"released": True}


# ---------------------------------------------------------------------------
# サービス（JSON のリクエスト → レスポンス）。ops を差し替えればフェイクでも動く
# ---------------------------------------------------------------------------

class WorkerService:
    def __init__(self, ops):
        self.ops = ops
        self.stop = False

    def handle(self, req):
        rid = req.get("id")
        op = req.get("op")
        try:
            if op not in dd.OPS:
                return {"id": rid, "ok": False, "error": f"unknown_op: {op}"}
            return {"id": rid, "ok": True, "result": self._dispatch(op, req)}
        except Exception as e:  # noqa: BLE001  COMError も含めて、ワーカーは落とさず応答する
            log(f"{op} が例外: {_exc(e)}")
            return {"id": rid, "ok": False, "error": _exc(e)}

    def _dispatch(self, op, req):
        ops = self.ops
        if op == "ping":
            return {"pong": True, "version": WORKER_VERSION, "pid": os.getpid(),
                    "dpi": getattr(ops, "dpi", "")}
        if op == "pick":
            return ops.pick(req)
        if op == "resolve":
            return ops.resolve(req)
        if op == "deliver":
            return dd.deliver(ops, req.get("profile") or {}, req.get("text") or "",
                              hwnd=int(req.get("hwnd") or 0), options=req.get("options") or {})
        if op == "cleanup":
            return ops.cleanup()
        self.stop = True            # shutdown
        return {"bye": True}


def serve(service, stdin, stdout):
    """stdin（バイナリ）を 1 行ずつ読み、応答を stdout（バイナリ）へ 1 行ずつ書く。shutdown か EOF で終わる。"""
    while not service.stop:
        raw = stdin.readline()
        if not raw:
            return 0               # 親が閉じた
        if not raw.strip():
            continue
        try:
            req = dd.decode_line(raw)
        except ValueError as e:
            resp = {"id": None, "ok": False, "error": f"bad_request: {e}"}
        else:
            resp = service.handle(req)
        stdout.write(dd.encode_line(resp))
        stdout.flush()
    return 0


def main(argv=None):
    real_out = sys.stdout.buffer
    sys.stdout = sys.stderr         # うっかりの print が応答を壊さないように
    ops = RealOps().start()
    log(f"start version={WORKER_VERSION} dpi={ops.dpi} pid={os.getpid()}")
    return serve(WorkerService(ops), sys.stdin.buffer, real_out)


if __name__ == "__main__":
    sys.exit(main())
