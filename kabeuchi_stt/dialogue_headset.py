# -*- coding: utf-8 -*-
"""AI対話モード: ヘッドセットの多機能ボタン（メディア再生/一時停止キー）の検出。Qt/COM 非依存。

ヘッドセットの短押しは PC には VK_MEDIA_PLAY_PAUSE として届く（押す/離すは同時刻のパルス）。
検出経路は2つを併用できる（設定 dialogue.headset_mode: auto / hid / hotkey）:
  1. HID 直接読み（dialogue_hid。読み取り専用）  2. RegisterHotKey（独占して動画・音楽への副作用を抑える役目＋予備）。
どちらか一方でも成功すれば「受付中」。両方が同じ押下を拾ったときは短い時間内の重複を1回に間引く。
RegisterHotKey で独占すると、背景の動画・音楽が反応しなくなり、押下を1回ずつ受け取れる。

- **対話モードが有効な間だけ**登録する。stop()・例外・プロセス終了（atexit）で必ず解除する。
- 本体プロセス内（ctypes）。低レベルフックは使わない（hotkeys.py と同じ流儀: 専用スレッド＋メッセージループ）。
- import しただけでは何も起きない（Win32 は start() の中で初めて触る）。
- 登録中はキーボードの再生/一時停止キーも効かなくなる。窓に「受付中」を出すこと（state / active で分かる）。
- 取得失敗（他のアプリが先に登録している等）は HeadsetResult.reason で返す。副作用ありで動かすかは呼び出し側の設定。
- 押下の間引き・状態遷移は純関数（should_accept_press / next_state）で、Win32 なしでテストできる。
"""
from __future__ import annotations

import atexit
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from dialogue_log import dlog

VK_MEDIA_PLAY_PAUSE = 0xB3
MOD_NOREPEAT = 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
PM_NOREMOVE = 0x0000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# 状態
STOPPED, ACTIVE, FAILED = "stopped", "active", "failed"
_TRANSITIONS = {
    (STOPPED, "start_ok"): ACTIVE, (STOPPED, "start_fail"): FAILED,
    (FAILED, "start_ok"): ACTIVE, (FAILED, "start_fail"): FAILED,
    (ACTIVE, "stop"): STOPPED, (FAILED, "stop"): STOPPED, (STOPPED, "stop"): STOPPED,
    (ACTIVE, "error"): FAILED,   # 受信ループが異常終了した
    (ACTIVE, "start_ok"): ACTIVE, (ACTIVE, "start_fail"): ACTIVE,   # 二重の start は無視（状態を壊さない）
    (STOPPED, "error"): STOPPED, (FAILED, "error"): FAILED,
}


def next_state(state: str, event: str) -> str:
    """状態遷移（純関数）。event: start_ok / start_fail / stop / error。未知の組は現状維持。"""
    return _TRANSITIONS.get((state, event), state)


def should_accept_press(now: float, last_accepted: float | None, min_interval: float = 0.3) -> bool:
    """押下の間引き（純関数）。直前に受け付けてから min_interval 秒未満の押下は捨てる（接点ばたつき・二重配信対策）。
    ヘッドセットは素早い連続押下を内部処理して PC に届かない（実測）ので、あまり長くしない。"""
    return last_accepted is None or (now - last_accepted) >= min_interval


def reason_for_error(code: int) -> str:
    if code == ERROR_HOTKEY_ALREADY_REGISTERED:
        return "他のアプリがすでにこのボタン（再生/一時停止キー）を使っています"
    return f"ボタンを独占できませんでした（Win32 エラー {code}）"


MODES = ("auto", "hid", "hotkey")
PATH_HID, PATH_HOTKEY = "hid", "hotkey"
PATH_LABEL = {PATH_HID: "HID直接読み", PATH_HOTKEY: "ホットキー"}
ACCEPT, DROP, DUP = "accept", "drop", "dup"


def normalize_mode(mode) -> str:
    m = str(mode or "auto").strip().lower()
    return m if m in MODES else "auto"


def classify_press(now: float, last_accepted: float | None, last_via: str | None, via: str,
                   min_interval: float = 0.3, dup_window: float = 0.5) -> str:
    """押下の分類（純関数）。ACCEPT=受け付ける / DROP=同じ経路の短すぎる連続 / DUP=別経路が同じ押下を拾った（重複）。
    別経路は dup_window 秒以内なら重複、同じ経路は min_interval 秒未満なら間引き。"""
    if last_accepted is None:
        return ACCEPT
    dt = now - last_accepted
    if via != last_via:
        return DUP if dt < dup_window else ACCEPT
    return ACCEPT if dt >= min_interval else DROP


@dataclass
class HeadsetResult:
    ok: bool
    reason: str = ""
    code: int = 0              # ホットキー登録に失敗したときの Win32 エラーコード（診断用。無ければ 0）
    paths: tuple = ()          # 有効になった検出経路（"hid" / "hotkey"）
    exclusive: bool = False    # ホットキーで再生/一時停止キーを独占できているか（False だと動画等も同時に反応しうる）


class _Win32Backend:
    """RegisterHotKey(NULL, id, 0, VK_MEDIA_PLAY_PAUSE) を専用スレッドで登録し、WM_HOTKEY を待つ。"""

    HOTKEY_ID = 0x4B53  # 任意の識別子（スレッド内で一意なら良い）

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._ready = threading.Event()
        self._result = HeadsetResult(False, "未開始")
        self.exit_reason = ""          # 受信ループが止まった理由（診断用）
        self._on_press: Callable[[], None] = lambda: None
        self._on_exit: Callable[[], None] = lambda: None

    def start(self, on_press: Callable[[], None], on_exit: Callable[[], None], timeout: float = 3.0) -> HeadsetResult:
        if sys.platform != "win32":
            return HeadsetResult(False, "Windows ではありません")
        self._on_press, self._on_exit = on_press, on_exit
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="kabeuchi-headset", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            return HeadsetResult(False, "登録の応答がありません（タイムアウト）")
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
            if t is not threading.current_thread():
                t.join(timeout=3)
        self._thread, self._tid = None, 0

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)   # 共有の windll の argtypes を書き換えない
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u32.RegisterHotKey.restype = wintypes.BOOL
        u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.UnregisterHotKey.restype = wintypes.BOOL
        u32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        u32.GetMessageW.restype = ctypes.c_int
        u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        u32.PeekMessageW.restype = wintypes.BOOL
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wintypes.DWORD
        registered = False
        msg = wintypes.MSG()
        try:
            u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)   # このスレッドのメッセージキューを作る
            self._tid = int(k32.GetCurrentThreadId())
            if u32.RegisterHotKey(None, self.HOTKEY_ID, MOD_NOREPEAT, VK_MEDIA_PLAY_PAUSE):
                registered = True
                self._result = HeadsetResult(True)
            else:
                code = int(ctypes.get_last_error())
                self._result = HeadsetResult(False, reason_for_error(code), code)
        except Exception as e:
            self._result = HeadsetResult(False, f"登録で例外: {type(e).__name__}: {e}")
        finally:
            self._ready.set()
        if not registered:
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
                if msg.message == WM_HOTKEY and msg.wParam == self.HOTKEY_ID:
                    try:
                        self._on_press()
                    except Exception:
                        pass
        except Exception as e:
            self.exit_reason = f"例外: {type(e).__name__}: {e}"
        finally:
            try:
                u32.UnregisterHotKey(None, self.HOTKEY_ID)   # 必ず解除（このスレッドで登録したのでこのスレッドで）
            finally:
                try:
                    self._on_exit()
                except Exception:
                    pass


class _NoBackend:
    """無効な経路（テストで HID を使わない等）の目印。start しない。"""


class HeadsetButton:
    """callback() を、ボタン1押しにつき1回呼ぶ（専用スレッドから。Qt へはシグナル経由で渡すこと）。
    backend: ホットキー経路（start(on_press, on_exit) -> HeadsetResult / stop()。テスト用に差し替え可）。
    hid_backend: HID 経路（dialogue_hid.HidButtonReader 互換）。backend だけを渡したとき（テスト）は HID を使わない。
    mode: auto（両方試す）/ hid / hotkey。"""

    def __init__(self, callback: Callable[[], None], min_interval: float = 0.3,
                 clock: Callable[[], float] = time.monotonic, backend=None,
                 log: Callable[..., None] = dlog, wall: Callable[[], float] = time.time,
                 hid_backend=None, mode: str = "auto", vid_pid: str = "3511:2EF2",
                 dup_window: float = 0.5):
        self.callback = callback
        self._log = log
        self._wall = wall
        self.min_interval = float(min_interval)
        self.dup_window = float(dup_window)
        self._clock = clock
        self.mode = normalize_mode(mode)
        self.vid_pid = vid_pid
        if backend is not None and hid_backend is None:
            hid_backend = _NoBackend()          # 差し替えのホットキーだけ（テスト）: 実機の HID は触らない
        self._backends: dict[str, object] = {}
        if self.mode in ("auto", "hotkey"):
            self._backends[PATH_HOTKEY] = backend if backend is not None else _Win32Backend()
        if self.mode in ("auto", "hid") and not isinstance(hid_backend, _NoBackend):
            if hid_backend is None:
                from dialogue_hid import HidButtonReader   # 遅延 import（Win32 は start まで触らない）
                hid_backend = HidButtonReader(vid_pid)
            self._backends[PATH_HID] = hid_backend
        self._backend = self._backends.get(PATH_HOTKEY)    # 互換（診断用）
        self._state = STOPPED
        self._last_accepted: float | None = None
        self._last_via: str | None = None
        self._lock = threading.Lock()
        self.last_reason = ""
        self.register_code = 0     # ホットキー登録に失敗したときの Win32 エラーコード
        self.presses = 0           # 受け付けた押下の数
        self.dropped = 0           # 間引いた押下の数（重複を含む）
        self.deduped = 0           # うち、別経路の重複として間引いた数
        self.received = 0          # 受信した押下の数（間引き前）
        self.received_by = {PATH_HID: 0, PATH_HOTKEY: 0}
        self.last_press = ""       # 最後に受信した押下の時刻（hh:mm:ss。診断表示用）
        self._starting = False     # 登録の応答待ち（この間に届いた押下も受ける）
        self._paths = {n: self._blank_path(n) for n in (PATH_HID, PATH_HOTKEY)}
        self.exclusive = False

    def _blank_path(self, name: str) -> dict:
        return {"enabled": name in self._backends, "ok": None, "reason": "", "code": 0, "alive": False}

    def diagnostics(self) -> dict:
        """診断用の状態（⚙メニュー・設定の表示、テスト用）。"""
        alive_vals = []
        for b in self._backends.values():
            f = getattr(b, "thread_alive", None)
            if callable(f):
                try:
                    alive_vals.append(bool(f()))
                except Exception:
                    alive_vals.append(False)
        paths = {}
        for n, st in self._paths.items():
            d = dict(st)
            d["received"] = self.received_by.get(n, 0)
            b = self._backends.get(n)
            dg = getattr(b, "diagnostics", None)
            if callable(dg):
                try:
                    d["detail"] = dict(dg())
                except Exception:
                    pass
            d["exit_reason"] = getattr(b, "exit_reason", "") if b is not None else ""
            paths[n] = d
        return {"state": self._state, "reason": self.last_reason, "register_code": self.register_code,
                "presses": self.presses, "dropped": self.dropped, "received": self.received,
                "deduped": self.deduped, "mode": self.mode, "exclusive": self.exclusive,
                "last_press": self.last_press, "thread_alive": any(alive_vals) if alive_vals else None,
                "exit_reason": getattr(self._backend, "exit_reason", ""), "paths": paths}

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        """「ヘッドセットボタン: 受付中」の表示用。"""
        return self._state == ACTIVE

    def start(self) -> HeadsetResult:
        with self._lock:
            if self._state == ACTIVE:
                return HeadsetResult(True, paths=self._active_paths(), exclusive=self.exclusive)
            self._starting = True
        results: dict[str, object] = {}
        for name in (PATH_HID, PATH_HOTKEY):       # HID を先に（ホットキーは独占する側）
            b = self._backends.get(name)
            if b is None:
                continue
            try:
                r = b.start(self._make_press(name), self._make_exit(name))
            except Exception as e:
                r = HeadsetResult(False, f"登録で例外: {type(e).__name__}: {e}")
            results[name] = r
        ok_paths = tuple(n for n, r in results.items() if getattr(r, "ok", False))
        if ok_paths:
            reason = ""
        elif len(results) == 1:
            reason = next(iter(results.values())).reason
        elif results:
            reason = " ／ ".join(f"{PATH_LABEL[n]}: {r.reason}" for n, r in results.items())
        else:
            reason = "検出経路がありません"
        hk = results.get(PATH_HOTKEY)
        code = 0 if (hk is None or hk.ok) else int(getattr(hk, "code", 0) or 0)
        res = HeadsetResult(bool(ok_paths), reason, code, ok_paths, PATH_HOTKEY in ok_paths)
        with self._lock:
            self._starting = False
            self._state = next_state(self._state, "start_ok" if res.ok else "start_fail")
            self.last_reason = reason
            self.register_code = code
            self.exclusive = res.exclusive
            self._last_accepted = None
            self._last_via = None
            self._paths = {n: self._blank_path(n) for n in (PATH_HID, PATH_HOTKEY)}
            for n, r in results.items():
                self._paths[n].update(ok=bool(r.ok), reason=("" if r.ok else r.reason),
                                      code=int(getattr(r, "code", 0) or 0), alive=bool(r.ok))
        self._log("headset_register", ok=res.ok, code=self.register_code, reason=("" if res.ok else res.reason),
                  state=self._state, mode=self.mode, paths=",".join(ok_paths), exclusive=res.exclusive)
        for n, r in results.items():
            self._log("headset_path", via=n, ok=bool(r.ok), code=int(getattr(r, "code", 0) or 0),
                      reason=("" if r.ok else r.reason), device=getattr(r, "path", ""))
        if res.ok:
            atexit.register(self.stop)   # 異常終了でも解除する
        return res

    def _active_paths(self) -> tuple:
        return tuple(n for n, st in self._paths.items() if st.get("alive"))

    def _make_press(self, via: str) -> Callable[[], None]:
        return lambda: self._on_press(via)

    def _make_exit(self, via: str) -> Callable[[], None]:
        return lambda: self._on_exit(via)

    def stop(self) -> None:
        with self._lock:
            was = self._state
            self._state = next_state(self._state, "stop")
        for b in self._backends.values():
            try:
                b.stop()
            except Exception:
                pass
        with self._lock:
            for st in self._paths.values():
                st["alive"] = False
        if was != STOPPED:
            hid = self._backends.get(PATH_HID)
            self._log("headset_stop", was=was, presses=self.presses, dropped=self.dropped, received=self.received,
                      by=dict(self.received_by), hid_reports=getattr(hid, "reports", None),
                      hid_last=getattr(hid, "last_report", None), hid_device=getattr(hid, "device", None),
                      hid_alive=(hid.thread_alive() if hid is not None and hasattr(hid, "thread_alive") else None))
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass

    # ---- バックエンドから（専用スレッド）
    def _on_press(self, via: str = PATH_HOTKEY) -> None:
        now = self._clock()
        with self._lock:
            self.received += 1
            self.received_by[via] = self.received_by.get(via, 0) + 1
            self.last_press = time.strftime("%H:%M:%S", time.localtime(self._wall()))
            if self._state != ACTIVE and not self._starting:
                self._log("headset_press", via=via, accepted=False, why=f"state={self._state}")
                return
            verdict = classify_press(now, self._last_accepted, self._last_via, via, self.min_interval, self.dup_window)
            if verdict != ACCEPT:
                self.dropped += 1
                if verdict == DUP:
                    self.deduped += 1
                    self._log("headset_press", via=via, accepted=False, why="重複（別経路が同じ押下を受け付け済み）")
                else:
                    self._log("headset_press", via=via, accepted=False, why="間引き（直前の押下から短すぎる）")
                return
            self._last_accepted = now
            self._last_via = via
            self.presses += 1
        self._log("headset_press", via=via, accepted=True, n=self.presses)
        try:
            self.callback()
        except Exception as e:
            self._log("headset_callback_error", error=f"{type(e).__name__}: {e}")   # 受信ループは止めない

    def _on_exit(self, via: str = PATH_HOTKEY) -> None:
        """ある経路の受信ループが止まった。stop() 以外で止まり、動いている経路が1つも無くなったら失敗扱いにする。"""
        with self._lock:
            self._paths[via]["alive"] = False
            err = self._state == ACTIVE and not self._active_paths()
            if err:
                self._state = next_state(self._state, "error")
                self.last_reason = "ボタンの受信が止まりました"
        self._log("headset_loop_exit", via=via, unexpected=err, why=getattr(self._backends.get(via), "exit_reason", ""))
