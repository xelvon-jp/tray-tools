# -*- coding: utf-8 -*-
"""AI対話モード: 本体側のワーカー管理（Qt 非依存）。

本体（PySide6 の kabeuchi-stt venv）は comtypes を import しない。UIA を使う常駐ワーカー
dialogue_worker.py を別プロセスで起こし、標準入出力の JSON 1行で頼む。
- 1 本のワーカーに、同時には 1 件だけ頼む（ロックで直列化）。
- 各 op にタイムアウト。タイムアウトしたら、ワーカーの状態が分からないので殺して、次の呼び出しで起こし直す。
- ワーカーが死んだら、次の呼び出しで自動で起こし直す。ping / resolve / cleanup は 1 回だけ自動でやり直す。
  deliver / pick は二重実行になりかねないので自動ではやり直さない（呼び出し側が判断する）。
- 起動の仕方（spawn）は注入できる。単体テストではサブプロセスを起こさずに、プロトコルを確かめる。
"""
from __future__ import annotations

import collections
import os
import queue
import subprocess
import threading
import time

import dialogue_deliver as dd

DEFAULT_TIMEOUTS = {"ping": 5.0, "resolve": 15.0, "deliver": 40.0, "cleanup": 10.0,
                    "shutdown": 3.0, "pick": 30.0}
PICK_MARGIN_SEC = 10.0            # pick は「クリックを待つ秒数 + この余裕」で打ち切る
_RETRYABLE = ("ping", "resolve", "cleanup")


class WorkerError(Exception):
    """ワーカーとのやりとりの失敗の親。"""


class WorkerDied(WorkerError):
    """ワーカーが終了した（または起こせなかった・書き込めなかった）。"""


class WorkerTimeout(WorkerError):
    """応答が時間内に来なかった（ワーカーは殺した）。"""


class WorkerOpError(WorkerError):
    """ワーカーが ok=false を返した（例外・未知の op 等）。"""


# ---------------------------------------------------------------------------
# 起動まわり
# ---------------------------------------------------------------------------

def default_worker_python(home=None):
    """既定のワーカー用 python（tray-tools の venv。comtypes あり）。"""
    return os.path.join(home or os.path.expanduser("~"), ".venvs", "tray-tools", "Scripts", "python.exe")


def resolve_worker_python(configured="", exists=os.path.exists, home=None):
    """設定 dialogue.worker_python（空なら既定）から python のパスを決める。戻りは (パス, 存在するか)。"""
    c = (configured or "").strip()
    path = os.path.expandvars(os.path.expanduser(c)) if c else default_worker_python(home)
    return path, bool(exists(path))


def worker_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dialogue_worker.py")


def build_command(python, script=None):
    # -u: 出力のバッファリングなし。-X utf8: 診断（stderr）の文字化け防止。-I は使わない（同階層の import ができなくなる）
    return [python, "-X", "utf8", "-u", script or worker_script()]


def subprocess_spawn(cmd, cwd=None):
    """本物の起動（テストでは使わない）。窓は出さない。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd or os.path.dirname(worker_script()), creationflags=flags)


def allow_foreground():
    """AllowSetForegroundWindow(ASFW_ANY)。ヘッドセット／ホットキーの入力を受けた直後（本体が
    直近の入力を受けた状態）に呼んで、ワーカーに前面化の権利を渡す。成功なら True。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        f = ctypes.WinDLL("user32", use_last_error=True).AllowSetForegroundWindow
        f.argtypes = [wt.DWORD]
        f.restype = wt.BOOL
        return bool(f(wt.DWORD(0xFFFFFFFF)))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# クライアント
# ---------------------------------------------------------------------------

class WorkerClient:
    """spawn(cmd) は、stdin（write/flush/close）・stdout（readline）・stderr（readline、省略可）・
    poll()・kill()・wait(timeout) を持つプロセスもどきを返す関数。"""

    def __init__(self, worker_python="", spawn=None, script=None, timeouts=None,
                 grant=allow_foreground, exists=os.path.exists, home=None, clock=time.monotonic):
        self._configured_python = worker_python
        self._spawn = spawn or subprocess_spawn
        self._script = script or worker_script()
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}
        self._grant = grant
        self._exists = exists
        self._home = home
        self._clock = clock
        self._lock = threading.RLock()
        self._proc = None
        self._q = None
        self._next_id = 0
        self._closed = False
        self.held_hwnd = 0
        self.starts = 0               # 起こした回数（2 以上なら再起動した）
        self.bad_lines = collections.deque(maxlen=20)      # JSON でなかった行（診断用）
        self.stderr_tail = collections.deque(maxlen=50)

    # --- 死活 ---
    def is_alive(self):
        p = self._proc
        return p is not None and p.poll() is None

    def start(self):
        """ワーカーを起こす（起きていれば何もしない）。起こせなければ WorkerDied。"""
        with self._lock:
            self._closed = False
            self._ensure_started()

    def _ensure_started(self):
        if self.is_alive():
            return
        if self._closed:
            raise WorkerError("closed")
        self._discard()
        python, ok = resolve_worker_python(self._configured_python, self._exists, self._home)
        if not ok and self._spawn is subprocess_spawn:
            raise WorkerDied(f"ワーカー用の python が見つからない: {python}")
        try:
            proc = self._spawn(build_command(python, self._script))
        except Exception as e:  # noqa: BLE001
            raise WorkerDied(f"ワーカーを起こせない: {type(e).__name__}: {e}") from e
        self._proc = proc
        self.starts += 1
        q = queue.Queue()
        self._q = q
        threading.Thread(target=self._pump, args=(proc.stdout, q, True), daemon=True).start()
        if getattr(proc, "stderr", None) is not None:
            threading.Thread(target=self._pump, args=(proc.stderr, None, False), daemon=True).start()

    def _pump(self, stream, q, is_stdout):
        """読み取りスレッド。stdout は行ごとにキューへ（EOF で None）。stderr は直近だけ覚える。"""
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                if is_stdout:
                    q.put(line)
                else:
                    self.stderr_tail.append(line.decode("utf-8", "replace").rstrip()
                                            if isinstance(line, (bytes, bytearray)) else str(line).rstrip())
        except Exception:  # noqa: BLE001
            pass
        finally:
            if is_stdout:
                q.put(None)

    def _discard(self):
        """いまのプロセスを捨てる（生きていれば殺す）。"""
        p, self._proc, self._q = self._proc, None, None
        if p is None:
            return
        try:
            if p.poll() is None:
                p.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            p.wait(1.0)
        except Exception:  # noqa: BLE001
            pass

    # --- 呼び出し ---
    def call(self, op, args=None, timeout=None):
        """op を頼んで result の辞書を返す。失敗は WorkerDied / WorkerTimeout / WorkerOpError。"""
        if op not in dd.OPS:
            raise ValueError(f"未知の op: {op}")
        t = float(timeout if timeout is not None else self.timeouts.get(op, 10.0))
        with self._lock:
            tries = 2 if op in _RETRYABLE else 1
            for n in range(tries):
                try:
                    return self._call_once(op, args or {}, t)
                except WorkerDied:
                    if n == tries - 1:
                        raise
            raise WorkerDied("unreachable")

    def _call_once(self, op, args, timeout):
        self._ensure_started()
        self._next_id += 1
        rid = self._next_id
        q, proc = self._q, self._proc
        try:
            proc.stdin.write(dd.encode_line({**args, "id": rid, "op": op}))
            proc.stdin.flush()
        except Exception as e:  # noqa: BLE001  BrokenPipe・閉じたファイル等
            self._discard()
            raise WorkerDied(f"ワーカーへ書けない: {type(e).__name__}: {e}") from e
        deadline = self._clock() + timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                self._discard()
                raise WorkerTimeout(f"{op} が {timeout:g} 秒で返らなかった")
            try:
                line = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                if proc.poll() is not None:        # 終了したが EOF がまだ届いていない
                    self._discard()
                    raise WorkerDied(f"{op} の応答前にワーカーが終了した")
                continue
            if line is None:
                self._discard()
                raise WorkerDied(f"{op} の応答前にワーカーが終了した")
            try:
                msg = dd.decode_line(line)
            except ValueError as e:
                self.bad_lines.append(f"{e}: {bytes(line)[:80]!r}")
                continue                            # 診断出力が紛れても待ち続ける
            if msg.get("id") != rid:
                continue                            # 前の（タイムアウトした）頼みの遅れた応答は捨てる
            if not msg.get("ok"):
                raise WorkerOpError(str(msg.get("error") or "unknown_error"))
            res = msg.get("result")
            return res if isinstance(res, dict) else {"value": res}

    # --- op ごとの入口 ---
    def ping(self):
        return self.call("ping")

    def ensure_alive(self):
        """死活監視用。応答があれば True。死んでいれば起こし直して試し、だめなら False。"""
        try:
            self.ping()
            return True
        except WorkerError:
            return False

    def pick(self, name="", timeout=30.0, exclude_roots=()):
        """宛先の入力欄のクリックを待ち、プロファイルを受け取る。クリック待ちの間は他の頼みは待たされる。"""
        return self.call("pick", {"name": name, "timeout": float(timeout),
                                  "exclude_roots": [int(x) for x in exclude_roots]},
                         timeout=float(timeout) + PICK_MARGIN_SEC)

    def resolve(self, profile, hwnd=0):
        res = self.call("resolve", {"profile": profile, "hwnd": int(hwnd or 0)})
        self.held_hwnd = int(res.get("hwnd") or 0) if res.get("status") == "ok" else 0
        return res

    def deliver(self, profile, text, hwnd=None, options=None, grant_foreground=True):
        """配達を頼む。直前に AllowSetForegroundWindow(ASFW_ANY) を呼ぶ（前面化ロック対策）。
        窓のボタン・無入力のタイマーからの呼び出しは前面化できないことがある（その場合は未配達で返る）。"""
        if grant_foreground:
            try:
                self._grant()
            except Exception:  # noqa: BLE001
                pass
        o = options or {}
        timeout = None
        if o.get("send"):    # 送信まで行うときは、回答中の待ち（busy_wait_sec）と送信の確認の分だけ長く待つ
            timeout = self.timeouts["deliver"] + (float(o.get("busy_wait_sec") or 0) if o.get("wait_while_busy") else 0.0) + 15.0
        return self.call("deliver", {"profile": profile, "text": text,
                                     "hwnd": int(self.held_hwnd if hwnd is None else hwnd or 0),
                                     "options": o}, timeout=timeout)

    def cleanup(self):
        self.held_hwnd = 0
        if not self.is_alive():
            return {"released": True}
        return self.call("cleanup")

    def shutdown(self):
        """ワーカーに終了を頼み、終わらなければ殺す。何度呼んでも安全。"""
        with self._lock:
            self._closed = True
            if self.is_alive():
                try:
                    self._call_once("shutdown", {}, self.timeouts["shutdown"])
                except WorkerError:
                    pass
                p = self._proc
                try:
                    p.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    p.wait(2.0)
                except Exception:  # noqa: BLE001
                    pass
            self._discard()
            self.held_hwnd = 0
