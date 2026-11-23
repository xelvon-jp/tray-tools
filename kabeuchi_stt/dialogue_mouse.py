# -*- coding: utf-8 -*-
"""AI対話モード: マウスのサイドボタン（戻る=XBUTTON1／進む=XBUTTON2）で確定する。Qt 非依存。

マウス用の追加ソフトが入れられない会社 PC でも使えるよう、アプリ自身が WH_MOUSE_LL（低レベルフック）で
マウス入力を拾う。ヘッドセット（dialogue_headset）とは独立の経路・独立の設定（dialogue.mouse_button: "" / back / forward）。

- **セッション中だけ**フックを入れる。stop()・例外・プロセス終了（atexit）で必ず外す（フックを入れたスレッドで外す）。
- 対象ボタンの down で on_press を呼び、**down と up の両方を握りつぶす**（他のアプリへ「戻る/進む」を渡さない）。
  対象外のボタン・動き・ホイール、および注入されたイベント（LLMHF_INJECTED）は必ず素通し。
- コールバックは超軽量（Windows は遅いフックを外す）。押下はキューへ入れるだけで、別スレッドが on_press を呼ぶ。
- 何を握りつぶすか／どのボタンかの判定は純関数 decide_mouse_event（Win32 なしでテストできる）。
- import しただけでは何も起きない。導入に失敗しても例外は外へ出さず MouseResult.reason で返す。
"""
from __future__ import annotations

import atexit
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from dialogue_headset import ACCEPT, STOPPED, ACTIVE, FAILED, classify_press, next_state
from dialogue_log import dlog

WH_MOUSE_LL = 14
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02
XBUTTON1, XBUTTON2 = 0x0001, 0x0002

PATH_MOUSE = "mouse"
BUTTONS = ("", "back", "forward")
BUTTON_LABEL = {"back": "戻るボタン", "forward": "進むボタン"}
_BUTTON_XCODE = {"back": XBUTTON1, "forward": XBUTTON2}


def normalize_button(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in BUTTONS else ""


def decide_mouse_event(message: int, mouse_data: int, flags: int, button: str) -> tuple[bool, bool]:
    """低レベルフックが受け取ったイベントの判定（純関数）。(握りつぶす, 押下として通知する) を返す。
    button: "back" / "forward" / ""（なし＝常に素通し）。注入イベントは常に素通し。"""
    code = _BUTTON_XCODE.get(normalize_button(button))
    if code is None:
        return False, False
    if message not in (WM_XBUTTONDOWN, WM_XBUTTONUP):
        return False, False
    if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
        return False, False
    if ((int(mouse_data) >> 16) & 0xFFFF) != code:
        return False, False
    return True, message == WM_XBUTTONDOWN


@dataclass
class MouseResult:
    ok: bool
    reason: str = ""
    code: int = 0


class _Win32MouseBackend:
    """専用スレッドで WH_MOUSE_LL を入れ、メッセージループを回す。stop() は WM_QUIT を投げてフックを外させる。"""

    def __init__(self, button: str):
        self.button = normalize_button(button)
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._tid = 0
        self._ready = threading.Event()
        self._result = MouseResult(False, "未開始")
        self.exit_reason = ""
        self._on_press: Callable[[], None] = lambda: None
        self._on_exit: Callable[[], None] = lambda: None
        self._proc = None   # ctypes のコールバックを GC させない

    def start(self, on_press: Callable[[], None], on_exit: Callable[[], None], timeout: float = 3.0) -> MouseResult:
        if sys.platform != "win32":
            return MouseResult(False, "Windows ではありません")
        self._on_press, self._on_exit = on_press, on_exit
        self._ready.clear()
        self._q = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._drain, name="kabeuchi-mouse-press", daemon=True)
        self._worker.start()
        self._thread = threading.Thread(target=self._run, name="kabeuchi-mouse", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            return MouseResult(False, "フックの応答がありません（タイムアウト）")
        if not self._result.ok:
            self.stop()
        return self._result

    def thread_alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def stop(self) -> None:
        t, tid = self._thread, self._tid
        if t is not None and tid:
            try:
                import ctypes
                from ctypes import wintypes
                u32 = ctypes.WinDLL("user32", use_last_error=True)
                u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                u32.PostThreadMessageW.restype = wintypes.BOOL
                u32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:
                pass
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread, self._tid = None, 0
        w, self._worker = self._worker, None
        self._q.put(None)
        if w is not None and w is not threading.current_thread():
            w.join(timeout=1)

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self._on_press()
            except Exception:
                pass

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        u32.SetWindowsHookExW.restype = ctypes.c_void_p
        u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u32.UnhookWindowsHookEx.restype = wintypes.BOOL
        u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u32.CallNextHookEx.restype = LRESULT
        u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u32.GetMessageW.restype = ctypes.c_int
        u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u32.PeekMessageW.restype = wintypes.BOOL
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wintypes.DWORD
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = wintypes.HMODULE
        button = self.button
        q = self._q
        hook_holder = [None]

        def proc(n_code, w_param, l_param):
            try:
                if n_code >= 0:
                    st = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    swallow, press = decide_mouse_event(int(w_param), int(st.mouseData), int(st.flags), button)
                    if press:
                        q.put(1)              # 軽く戻る。on_press は別スレッド
                    if swallow:
                        return 1
            except Exception:
                pass
            return u32.CallNextHookEx(hook_holder[0], n_code, w_param, l_param)

        self._proc = HOOKPROC(proc)
        hook = None
        msg = wintypes.MSG()
        try:
            u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
            self._tid = int(k32.GetCurrentThreadId())
            hook = u32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, k32.GetModuleHandleW(None), 0)
            if hook:
                hook_holder[0] = hook
                self._result = MouseResult(True)
            else:
                code = int(ctypes.get_last_error())
                self._result = MouseResult(False, f"マウスのフックを入れられませんでした（Win32 エラー {code}）", code)
        except Exception as e:
            self._result = MouseResult(False, f"フック導入で例外: {type(e).__name__}: {e}")
        finally:
            self._ready.set()
        if not hook:
            return
        try:
            while True:
                r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0:
                    self.exit_reason = "WM_QUIT"
                    break
                if r < 0:
                    self.exit_reason = f"GetMessageW が -1（Win32 エラー {ctypes.get_last_error()}）"
                    break
        except Exception as e:
            self.exit_reason = f"例外: {type(e).__name__}: {e}"
        finally:
            try:
                u32.UnhookWindowsHookEx(hook)     # 必ず外す（入れたスレッドで）
            finally:
                try:
                    self._on_exit()
                except Exception:
                    pass


class MouseButton:
    """callback() を、対象のマウスボタン1押しにつき1回呼ぶ（別スレッドから。Qt へはシグナル経由で渡すこと）。
    button が空なら start() は何も起動しない（ok=False・reason 空＝使わない設定）。
    backend: start(on_press, on_exit) -> MouseResult / stop()（テストで差し替え）。"""

    def __init__(self, callback: Callable[[], None], button: str = "", min_interval: float = 0.3,
                 clock: Callable[[], float] = time.monotonic, backend=None,
                 log: Callable[..., None] = dlog, wall: Callable[[], float] = time.time):
        self.callback = callback
        self.button = normalize_button(button)
        self.min_interval = float(min_interval)
        self._clock = clock
        self._wall = wall
        self._log = log
        self._backend = backend
        self._state = STOPPED
        self._last_accepted: float | None = None
        self._lock = threading.Lock()
        self.last_reason = ""
        self.presses = self.dropped = self.received = 0
        self.last_press = ""

    @property
    def enabled(self) -> bool:
        return bool(self.button)

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == ACTIVE

    def diagnostics(self) -> dict:
        t = getattr(self._backend, "thread_alive", None)
        try:
            alive = bool(t()) if callable(t) else None
        except Exception:
            alive = None
        return {"state": self._state, "reason": self.last_reason, "button": self.button, "enabled": self.enabled,
                "presses": self.presses, "dropped": self.dropped, "received": self.received,
                "last_press": self.last_press, "thread_alive": alive,
                "exit_reason": getattr(self._backend, "exit_reason", "")}

    def start(self) -> MouseResult:
        if not self.enabled:
            return MouseResult(False, "")
        with self._lock:
            if self._state == ACTIVE:
                return MouseResult(True)
        try:
            if self._backend is None:
                self._backend = _Win32MouseBackend(self.button)
            r = self._backend.start(self._on_press, self._on_exit)
        except Exception as e:
            r = MouseResult(False, f"フック導入で例外: {type(e).__name__}: {e}")
        with self._lock:
            self._state = next_state(self._state, "start_ok" if r.ok else "start_fail")
            self.last_reason = "" if r.ok else r.reason
            self._last_accepted = None
        self._log("mouse_register", via=PATH_MOUSE, ok=bool(r.ok), button=self.button, code=int(getattr(r, "code", 0) or 0),
                  reason=("" if r.ok else r.reason), state=self._state)
        if r.ok:
            atexit.register(self.stop)
        return r

    def stop(self) -> None:
        with self._lock:
            was = self._state
            self._state = next_state(self._state, "stop")
        b = self._backend
        if b is not None:
            try:
                b.stop()
            except Exception:
                pass
        if was != STOPPED:
            self._log("mouse_stop", via=PATH_MOUSE, was=was, presses=self.presses, dropped=self.dropped)
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass

    def _on_press(self) -> None:
        now = self._clock()
        with self._lock:
            self.received += 1
            self.last_press = time.strftime("%H:%M:%S", time.localtime(self._wall()))
            if self._state != ACTIVE:
                self._log("mouse_press", via=PATH_MOUSE, accepted=False, why=f"state={self._state}")
                return
            # マウスは独立の物理ボタン: 別経路の重複扱いはしない（同じ経路としての短すぎる連続だけ間引く）
            if classify_press(now, self._last_accepted, PATH_MOUSE, PATH_MOUSE, self.min_interval) != ACCEPT:
                self.dropped += 1
                self._log("mouse_press", via=PATH_MOUSE, accepted=False, why="間引き（直前の押下から短すぎる）")
                return
            self._last_accepted = now
            self.presses += 1
        self._log("mouse_press", via=PATH_MOUSE, accepted=True, n=self.presses)
        try:
            self.callback()
        except Exception as e:
            self._log("mouse_callback_error", via=PATH_MOUSE, error=f"{type(e).__name__}: {e}")

    def _on_exit(self) -> None:
        with self._lock:
            err = self._state == ACTIVE
            if err:
                self._state = next_state(self._state, "error")
                self.last_reason = "マウスの受信が止まりました"
        self._log("mouse_loop_exit", via=PATH_MOUSE, unexpected=err, why=getattr(self._backend, "exit_reason", ""))
