# com_gc_probe.py
# 「GC → comtypes の __del__ → Release で access violation」の犯人を捕まえるための調査モード。
#
# 【既定は無効。有効にしたときだけ働く】
#   settings.json の "debug": {"com_gc_probe": true}、または環境変数 TRAYTOOLS_COM_GC_PROBE=1。
#   無効のときは import しても何もしない(enable() を呼ばない限り何も書き換えない)。
#
# 【何をするか】
#   1. comtypes の COM ポインタ(_compointer_base)が作られたときに、作ったスレッドと
#      呼び出し元の数フレームを、そのポインタ自身の __dict__ に書いておく。
#   2. __del__ を差し替え、循環 GC の最中に呼ばれた __del__ では Release しない
#      (=落ちない)。代わりに「誰が・どのスレッドで作ったものが GC に回ってきたか」を記録する。
#      GC 以外(参照カウントが0になった普通の解放)は今までどおり Release する。作った
#      スレッドと違うスレッドで解放されたときだけ、Release の直前に記録を残す。
#   3. gc.set_debug(gc.DEBUG_SAVEALL) で、循環 GC が回収したものを gc.garbage に残す。
#      scan() が gc.garbage から COM ポインタを探し、何に抱えられていたか
#      (gc.get_referrers を数段)を com_gc_probe.log に書く。
#
# 【注意】DEBUG_SAVEALL だけでは落ちるのは止まらない。CPython は gc.garbage に残す前に
#   tp_finalize(__del__)を呼ぶので、Release は普通に走る(2026-10-21 実測)。だから 2. で
#   GC 中の Release を止めている。止めた分の COM 参照と gc.garbage は解放しない。
#   調査のための意図的なリーク。調査が済んだら必ず無効に戻すこと。
import gc
import json
import os
import sys
import threading
import time
from pathlib import Path

ENV_NAME = "TRAYTOOLS_COM_GC_PROBE"
# 記録先。テストが別の場所へ向けられるよう環境変数で差し替えられる。
LOG_PATH = Path(os.environ.get("TRAYTOOLS_COM_GC_PROBE_LOG")
                or Path(__file__).resolve().parent / "com_gc_probe.log")

# 作成時に覚えておく呼び出し元のフレーム数
_STACK_DEPTH = 10
# get_referrers をたどる深さ
_REFERRER_DEPTH = 5

_enabled = False
_lock = threading.Lock()
_gc_thread = None          # いま循環 GC を回しているスレッドの ident
_pending = []              # GC 中に溜めた記録(GC の外で書き出す)
_scanned_upto = 0          # gc.garbage のどこまで見たか
_seen_ids = set()          # 報告済みの COM ポインタ(gc.garbage に残るので id は再利用されない)
_counts = {"created": 0, "gc_finalized": 0, "cross_thread_release": 0}


def is_requested(settings_path=None) -> bool:
    """環境変数か settings.json の debug.com_gc_probe で有効にされているか。

    settings_module.load_settings を通さないのは、起動のいちばん早い段階(COM を使う
    Feature を作る前)に判断したいため。読めなければ無効。"""
    if os.environ.get(ENV_NAME, "").strip() in ("1", "true", "yes", "on"):
        return True
    path = Path(settings_path) if settings_path else Path(__file__).resolve().parent / "settings.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    debug = data.get("debug") if isinstance(data, dict) else None
    return bool(isinstance(debug, dict) and debug.get("com_gc_probe"))


def enabled() -> bool:
    return _enabled


def _write(lines) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
    except OSError:
        pass


def _thread_label(ident=None) -> str:
    ident = threading.get_ident() if ident is None else ident
    for t in threading.enumerate():
        if t.ident == ident:
            return f"{ident:#x}[{t.name}]"
    return f"{ident:#x}[?]"


def _short_stack(start_frame) -> list:
    """フレームそのものは持たない(持つと新しい循環を作る)。文字列にして捨てる。"""
    out = []
    f = start_frame
    while f is not None and len(out) < _STACK_DEPTH:
        code = f.f_code
        out.append(f"{code.co_filename}:{f.f_lineno} {code.co_name}")
        f = f.f_back
    del f
    return out


def _raw_value(ptr):
    """ポインタの値。obj.value は COM のプロパティ(ITypeLib など)に化けることがあるので、
    c_void_p の value を直接読む。"""
    import ctypes
    try:
        return ctypes.c_void_p.value.__get__(ptr)
    except Exception:
        return None


def _on_gc(phase, info) -> None:
    global _gc_thread
    if phase == "start":
        _gc_thread = threading.get_ident()
    else:
        _gc_thread = None


def enable() -> bool:
    """調査モードを入れる。2回目以降は何もしない。comtypes が読めなければ False。"""
    global _enabled
    if _enabled:
        return True
    try:
        from comtypes._post_coinit.unknwn import _compointer_base
    except Exception:
        return False

    original_init = _compointer_base.__init__
    original_del = _compointer_base.__del__

    def probe_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            frame = sys._getframe(1)
            self.__dict__["_probe_tid"] = threading.get_ident()
            self.__dict__["_probe_stack"] = _short_stack(frame)
            del frame
            _counts["created"] += 1
        except Exception:
            pass

    def probe_del(self):
        try:
            if not self:
                return
            here = threading.get_ident()
            info = self.__dict__
            tid = info.get("_probe_tid")
            if _gc_thread == here:
                # 循環 GC の最中。ここで Release すると落ちる(のが今回の調査対象)ので、
                # 解放せずに記録だけ残す。中身は gc.garbage に残るので scan() が詳しく見る。
                _counts["gc_finalized"] += 1
                _pending.append(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} GC_FINALIZE(Releaseを止めた) "
                    f"type={type(self).__name__} value={(_raw_value(self) or 0):#x} "
                    f"作成スレッド={_thread_label(tid) if tid else '不明(__init__を通らない作り方)'} "
                    f"GCのスレッド={_thread_label(here)}"
                )
                return
            if tid is not None and tid != here:
                _counts["cross_thread_release"] += 1
                _write([
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} CROSS_THREAD_RELEASE "
                    f"type={type(self).__name__} 作成={_thread_label(tid)} 解放={_thread_label(here)}",
                    *("    created at " + s for s in info.get("_probe_stack", [])[:6]),
                ])
        except Exception:
            pass
        try:
            original_del(self)
        except Exception as e:
            # ctypes は Release の中のアクセス違反を OSError に変えて返すことがある。
            # 返ってきたときだけでも「どれが解放済みのものを Release したか」を残す。
            info = self.__dict__
            tid = info.get("_probe_tid")
            own = getattr(self, "_objects", None)
            _counts["release_failed"] = _counts.get("release_failed", 0) + 1
            _write([
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} RELEASE_FAILED(GC外) {e!r} "
                f"type={type(self).__name__} 作成={_thread_label(tid) if tid else '不明(__init__を通らない=castなど)'} "
                f"解放={_thread_label()} _objects={type(own).__name__}"
                f"{'(他のCOMポインタを抱えている)' if isinstance(own, dict) and own else ''}",
                *("    created at " + s for s in info.get("_probe_stack", [])[:8]),
                *("    released from " + s for s in _short_stack(sys._getframe(1))[:6]),
            ])
            e = None

    _compointer_base.__init__ = probe_init
    _compointer_base.__del__ = probe_del
    gc.callbacks.append(_on_gc)
    gc.set_debug(gc.DEBUG_SAVEALL)
    _enabled = True
    _write([f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 調査モード開始 pid={os.getpid()} "
            f"main={_thread_label()} ====="])
    return True


# ---------------------------------------------------------------- 抱えていたものの説明

def _describe(obj) -> str:
    import types
    try:
        if isinstance(obj, types.FrameType):
            return f"frame {obj.f_code.co_filename}:{obj.f_lineno} in {obj.f_code.co_name}"
        if isinstance(obj, types.TracebackType):
            f = obj.tb_frame
            return f"traceback {f.f_code.co_filename}:{obj.tb_lineno} in {f.f_code.co_name}"
        if isinstance(obj, BaseException):
            return f"exception {type(obj).__name__}: {str(obj)[:80]}"
        if isinstance(obj, types.FunctionType):
            return f"function {obj.__module__}.{obj.__qualname__}"
        if isinstance(obj, types.MethodType):
            return f"bound method {getattr(obj.__func__, '__qualname__', '?')}"
        if isinstance(obj, types.CellType):
            return "cell(クロージャの変数)"
        if isinstance(obj, dict):
            keys = list(obj.keys())[:6]
            int_keys = keys and all(isinstance(k, int) for k in keys)
            kind = "dict(ctypesの_objects=キープアライブ。ctypes.cast などで共有される)" if int_keys else "dict"
            return f"{kind} keys={keys!r}"[:200]
        if isinstance(obj, (list, tuple, set)):
            return f"{type(obj).__name__} len={len(obj)}"
        mod = type(obj).__module__
        return f"{mod}.{type(obj).__qualname__} instance"
    except Exception as e:  # 説明に失敗しても調査は続ける
        return f"<{type(obj).__name__} 説明失敗 {e!r}>"


def _referrer_chain(target, garbage_ids, depth=_REFERRER_DEPTH) -> list:
    """target を抱えていたものを、gc.garbage の中だけで数段たどる。

    gc.garbage 自体・このモジュールのフレームなど、調査のために生まれた参照は除く。"""
    lines = []
    frontier = [target]
    visited = {id(target)}
    for level in range(1, depth + 1):
        nxt = []
        for obj in frontier:
            for ref in gc.get_referrers(obj):
                rid = id(ref)
                if rid in visited or rid not in garbage_ids:
                    continue
                visited.add(rid)
                lines.append(f"    {'  ' * level}<- {_describe(ref)}")
                nxt.append(ref)
                if len(lines) > 40:
                    return lines
        frontier = nxt
        if not frontier:
            break
    return lines


def scan() -> int:
    """gc.garbage を見て、新しく見つかった COM ポインタを記録する。見つけた数を返す。

    メインスレッドの QTimer やハーネスのループから呼ぶ。GC の外で呼ぶこと。"""
    global _scanned_upto
    if not _enabled:
        return 0
    try:
        from comtypes._post_coinit.unknwn import _compointer_base
    except Exception:
        return 0
    lines = []
    with _lock:
        pending, _pending[:] = list(_pending), []
        lines.extend(pending)
        garbage = gc.garbage
        new = garbage[_scanned_upto:]
        _scanned_upto = len(garbage)
        if not new:
            if lines:
                _write(lines)
            return 0
        garbage_ids = {id(o) for o in garbage}
        found = 0
        for obj in new:
            if not isinstance(obj, _compointer_base) or id(obj) in _seen_ids:
                continue
            _seen_ids.add(id(obj))
            found += 1
            info = obj.__dict__
            tid = info.get("_probe_tid")
            lines.append(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} GARBAGE_COM type={type(obj).__name__} "
                f"value={(_raw_value(obj) or 0):#x} 作成スレッド="
                f"{_thread_label(tid) if tid else '不明'}"
            )
            for s in info.get("_probe_stack", []):
                lines.append(f"    created at {s}")
            own = getattr(obj, "_objects", None)
            if isinstance(own, dict) and any(v is obj for v in own.values()):
                lines.append("    !! 自分の _objects(dict) が自分自身を抱えている = ctypes.cast の元にされた形")
            lines.append("    抱えていたもの:")
            lines.extend(_referrer_chain(obj, garbage_ids))
        del new, garbage
    lines.append(f"    (累計 created={_counts['created']} gc_finalized={_counts['gc_finalized']} "
                 f"cross_thread_release={_counts['cross_thread_release']} garbage={len(gc.garbage)})")
    _write(lines)
    return found


def counts() -> dict:
    return dict(_counts)


def start_timer(interval_ms: int = 5000):
    """メインスレッドで定期的に scan() する QTimer を返す(呼ぶ側が参照を持つこと)。"""
    if not _enabled:
        return None
    from PySide6.QtCore import QTimer
    timer = QTimer()
    timer.setInterval(interval_ms)
    timer.timeout.connect(lambda: _safe_scan())
    timer.start()
    return timer


def _safe_scan():
    try:
        scan()
    except Exception as e:  # スロットで例外を投げ切るとプロセスごと落ちる
        _write([f"scan 失敗: {e!r}"])
