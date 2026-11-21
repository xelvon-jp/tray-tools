# -*- coding: utf-8 -*-
"""グローバルホットキー（RegisterHotKey, ctypes）。Qt/COM 非依存。

低レベルキーフック（keyboard ライブラリ等）は EDR に検知されうるため使わない。
RegisterHotKey はスレッドのメッセージキューに紐づくので、専用スレッドで登録し
GetMessage ループで WM_HOTKEY を待つ。コールバックは**そのスレッドから**呼ばれる
（Qt からはシグナル経由でメインスレッドへ渡すこと）。

既定は未割り当て。割り当てる組み合わせは Claude アプリ等の常用ショートカットと
衝突しないこと（過去に Ctrl+Alt+A がアーカイブと衝突した）。
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN}
_NAMED = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
          "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
          "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
          ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
          "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """"Ctrl+Alt+F9" → (修飾キー, 仮想キーコード)。修飾キーが 1 つも無い、または不正なら ValueError。"""
    parts = [p.strip() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("ホットキーが空です")
    mods, key = 0, None
    for p in parts:
        low = p.lower()
        if low in _MODS:
            mods |= _MODS[low]
        elif key is not None:
            raise ValueError(f"キーが2つ指定されています: {spec}")
        elif low in _NAMED:
            key = _NAMED[low]
        elif len(low) == 1 and (low.isalpha() or low.isdigit()):
            key = ord(low.upper())
        elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            key = 0x70 + int(low[1:]) - 1
        else:
            raise ValueError(f"解釈できないキー: {p}")
    if key is None:
        raise ValueError(f"キーがありません: {spec}")
    if mods == 0:
        raise ValueError("Ctrl / Alt / Shift / Win のいずれかを組み合わせてください（単独キーは通常の入力を奪うため不可）")
    return mods, key


class HotkeyManager:
    """登録は start() 内で専用スレッドが行う。結果は results（名前→ None=成功 / エラー文字列）。"""

    def __init__(self, bindings: dict[str, str], callback: Callable[[str], None]):
        self.bindings = {k: v for k, v in bindings.items() if (v or "").strip()}
        self.callback = callback
        self.results: dict[str, str | None] = {}
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()

    def start(self, timeout: float = 3.0) -> dict[str, str | None]:
        if sys.platform != "win32" or not self.bindings:
            return self.results
        self._thread = threading.Thread(target=self._run, name="kabeuchi-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self.results

    def stop(self) -> None:
        if self._thread and self._tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        for i, (name, spec) in enumerate(self.bindings.items(), start=1):
            try:
                mods, vk = parse_hotkey(spec)
                if not user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                    raise OSError(f"登録できません（他のアプリが使用中の可能性）: {spec} (error {ctypes.GetLastError()})")
                ids[i] = name
                self.results[name] = None
            except Exception as e:
                self.results[name] = str(e)
        self._ready.set()
        if not ids:
            return
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in ids:
                    try:
                        self.callback(ids[msg.wParam])
                    except Exception:
                        pass
        finally:
            for i in ids:
                user32.UnregisterHotKey(None, i)
