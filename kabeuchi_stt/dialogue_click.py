# -*- coding: utf-8 -*-
"""AI対話モード（実験・既定OFF）: 舌打ちを2回続けて鳴らすと「確定して届ける」になる。マイクの音だけで判定する。

追加ソフト・特別な権限は要らない。判定は純 NumPy の ClickDetector（Qt・COM・マイク・時計に依存せず、サンプル数だけで進む）。

検出の仕様（実測で決めた値。stage0\\click_probe の試作と実録音で確認）:
 1. 5ms（80サンプル）ごとのフレームのピークを dBFS にし、> -30dBFS を「大きい」とする。10ms 以内の谷は同じ塊につなぐ。
 2. 塊が「舌打ち1回」: 継続 25〜110ms・塊の最大ピーク >= -14dBFS・高域比 >= 0.5
    （高域比 = 塊の開始の80サンプル前から max(塊の長さ,1024) サンプルを Hann 窓＋rfft したパワーのうち 2kHz 以上の割合）。
 3. 「2回続けた舌打ち」= 条件を満たす舌打ち1回が、開始から開始まで 0.2〜0.7 秒で2つ並ぶ。
 4. 静けさの条件: 1つ目の開始の前 0.3 秒（直前 50ms は除く）と、2つ目の終わりのあと 0.3 秒の RMS がどちらも < -48dBFS
    （発話の語尾の破裂音の誤検出を排除する）。確認が済むのは2つ目の終わりから 0.3 秒後で、その時点で1回だけ通知する。
 5. 通知後 2 秒は再検出しない（不応期）。3回目以降・1回だけの舌打ちでは何も起きない。
"""
from __future__ import annotations

import atexit
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from dialogue_headset import ACCEPT, STOPPED, ACTIVE, FAILED, classify_press, next_state
from dialogue_log import dlog

PATH_CLICK = "click"

SR = 16000
FRAME = 80                      # 5ms
FRAME_MS = 5
LOUD_DBFS = -30.0
BRIDGE_FRAMES = 3               # 最後の「大きい」フレームのあと、この数だけ連続で小さければ塊を閉じる（谷 10ms まで同じ塊）
DUR_MIN_MS, DUR_MAX_MS = 25, 110
PEAK_MIN_DBFS = -14.0
HF_MIN = 0.5
HF_HZ = 2000.0
SPEC_BACK = 80
SPEC_MIN = 1024
PAIR_MIN_SEC, PAIR_MAX_SEC = 0.15, 0.7      # 陽太さんの実際の舌打ちは、2回の間隔が約0.2秒（0.2未満に測れることもある）。0.1秒前後は1回の余韻
QUIET_SEC = 0.3
QUIET_GUARD_SEC = 0.05          # 1つ目の直前のこの長さは静けさの判定から除く
QUIET_DBFS = -48.0
REFRACTORY_SEC = 2.0
_KEEP_SEC = 4.0
_CAP_SEC = 12.0


def _db(x: np.ndarray) -> float:
    """int16 配列の RMS を dBFS で。"""
    if x.size == 0:
        return 0.0
    f = x.astype(np.float64) / 32768.0
    return float(20.0 * np.log10(np.sqrt(np.mean(f * f)) + 1e-9))


class ClickDetector:
    """feed(int16 ブロック) → 「舌打ち2回」を検出したら callback() を1回呼ぶ（feed を呼んだスレッドで）。
    例外は外へ出さない（errors に数える）。"""

    def __init__(self, callback: Callable[[], None], sr: int = SR):
        self.callback = callback
        self.sr = int(sr)
        cap = int(_CAP_SEC * self.sr)
        self._cap = cap
        self._keep = int(_KEEP_SEC * self.sr)
        self._buf = np.zeros(cap, dtype=np.int16)
        self._n = 0                 # 保持している末尾のサンプル数（_buf[:_n]）
        self._total = 0             # これまでに受け取った総サンプル数
        self._proc = 0              # フレーム処理済みの絶対サンプル位置（FRAME の倍数）
        self._in_blob = False
        self._b_start = 0           # 塊の開始フレーム（絶対）
        self._b_last = 0            # 塊の最後の「大きい」フレーム
        self._pending: list[tuple[int, int]] = []     # 閉じたが、周波数判定の音がまだ揃わない塊（開始フレーム, 最後のフレーム）
        self._prev_click: tuple[int, int] | None = None   # 直前の舌打ち1回 (開始サンプル, 終了サンプル)
        self._prev_quiet_ok = False
        self._wait: list[tuple[int, int, int]] = []   # 静けさ待ちの候補 (確認が済むサンプル位置, 2つ目の終わり, 1つ目の開始)
        self._refractory_until = 0
        self.errors = 0
        self.detections = 0
        self.clicks_seen = 0
        self.last_pair_start = -1

    # ---- 入力
    def samples_since_pair_start(self) -> int | None:
        """直近に検出した2回続けの1つ目の開始から、いままでのサンプル数。まだ無ければ None。"""
        return None if self.last_pair_start < 0 else int(self._total - self.last_pair_start)

    def feed(self, x) -> None:
        try:
            x = np.asarray(x)
            if x.size == 0:
                return
            if x.dtype != np.int16:
                x = x.astype(np.int16)
            x = x.reshape(-1)
            self._append(x)
            self._step()
        except Exception:
            self.errors += 1

    def _append(self, x: np.ndarray) -> None:
        m = len(x)
        if m >= self._cap:
            x = x[-self._cap:]
            m = len(x)
        if self._n + m > self._cap:             # 満杯: 古い側を捨てて詰める（直近 _keep だけ残す）
            keep = min(self._n, self._keep)
            self._buf[:keep] = self._buf[self._n - keep:self._n]
            self._n = keep
        self._buf[self._n:self._n + m] = x
        self._n += m
        self._total += m

    def _slice(self, a: int, b: int) -> np.ndarray | None:
        """絶対サンプル位置 [a, b) の音（a<0 は 0 へ丸める）。保持していない範囲・まだ無い範囲を含めば None。"""
        a = max(0, int(a))
        b = int(b)
        base = self._total - self._n
        if a < base or b > self._total:
            return None
        return self._buf[a - base:b - base]

    # ---- 進行
    def _step(self) -> None:
        nfr = (self._total - self._proc) // FRAME
        if nfr > 0:
            a = self._proc
            seg = self._slice(a, a + nfr * FRAME)
            if seg is None:                       # 取りこぼし（呼び出しが長く止まった等）: 状態を捨てて進める
                self._proc = (self._total // FRAME) * FRAME
                self._reset_blob()
                return
            pk = np.abs(seg.astype(np.int32)).reshape(nfr, FRAME).max(axis=1).astype(np.float64) / 32768.0
            on = (20.0 * np.log10(pk + 1e-9)) > LOUD_DBFS
            f0 = a // FRAME
            for k in range(nfr):
                f = f0 + k
                if self._in_blob:
                    if on[k]:
                        self._b_last = f
                    elif f - self._b_last >= BRIDGE_FRAMES:
                        self._pending.append((self._b_start, self._b_last))
                        self._in_blob = False
                elif on[k]:
                    self._in_blob, self._b_start, self._b_last = True, f, f
            self._proc = a + nfr * FRAME
        self._eval_pending()
        self._eval_waits()

    def _reset_blob(self) -> None:
        self._in_blob = False
        self._pending.clear()

    def _eval_pending(self) -> None:
        while self._pending:
            fs, fl = self._pending[0]
            s0, e0 = fs * FRAME, (fl + 1) * FRAME
            need = s0 + max(e0 - s0, SPEC_MIN)
            if self._total < need:
                return
            self._pending.pop(0)
            self._judge_blob(s0, e0)

    def _judge_blob(self, s0: int, e0: int) -> None:
        dur_ms = (e0 - s0) // FRAME * FRAME_MS
        if not (DUR_MIN_MS <= dur_ms <= DUR_MAX_MS):
            return
        x = self._slice(s0, e0)
        if x is None:
            return
        p = 20.0 * np.log10(float(np.abs(x.astype(np.int32)).max()) / 32768.0 + 1e-9)
        if p < PEAK_MIN_DBFS:
            return
        seg = self._slice(max(0, s0 - SPEC_BACK), s0 + max(e0 - s0, SPEC_MIN))
        if seg is None or len(seg) < 2:
            return
        sf = seg.astype(np.float64) / 32768.0
        sp = np.abs(np.fft.rfft(sf * np.hanning(len(sf)))) ** 2
        fr = np.fft.rfftfreq(len(sf), 1.0 / self.sr)
        hf = float(sp[fr > HF_HZ].sum() / (sp.sum() + 1e-12))
        if hf < HF_MIN:
            return
        self._on_click(s0, e0)

    def _on_click(self, s0: int, e0: int) -> None:
        self.clicks_seen += 1
        sr = self.sr
        prev, prev_ok = self._prev_click, self._prev_quiet_ok
        if prev is not None and prev_ok:
            gap = (s0 - prev[0]) / sr
            if PAIR_MIN_SEC <= gap <= PAIR_MAX_SEC:
                self._wait.append((e0 + int(QUIET_SEC * sr), e0, prev[0]))
        # この舌打ちを「1つ目」にする場合の、前の静けさ（0.3秒、直前 50ms を除く）
        a, b = s0 - int((QUIET_SEC + QUIET_GUARD_SEC) * sr), s0 - int(QUIET_GUARD_SEC * sr)
        pre = self._slice(a, b) if a >= 0 else None      # 録音の頭すぎて足りないときは成立させない
        self._prev_click = (s0, e0)
        self._prev_quiet_ok = bool(pre is not None and _db(pre) < QUIET_DBFS)

    def _eval_waits(self) -> None:
        if not self._wait:
            return
        rest = []
        for ready, e0, s1 in self._wait:
            if self._total < ready:
                rest.append((ready, e0, s1))
                continue
            if ready < self._refractory_until:
                continue
            post = self._slice(e0, ready)
            if post is None or _db(post) >= QUIET_DBFS:
                continue
            self._refractory_until = ready + int(REFRACTORY_SEC * self.sr)
            self.detections += 1
            self.last_pair_start = s1
            try:
                self.callback()
            except Exception:
                self.errors += 1
        self._wait = rest


@dataclass
class ClickResult:
    ok: bool
    reason: str = ""


class ClickCommit:
    """舌打ち2回を検出するたびに callback() を1回呼ぶ（別スレッドから。Qt へはシグナル経由で渡すこと）。
    enabled が False なら start() は何も起動しない。音声は feed(int16) で受ける（録音の音声ブロックの入口から）。
    detector_factory(callback) -> feed(x) を持つもの（テストで差し替え）。"""

    def __init__(self, callback: Callable[[], None], enabled: bool = False, min_interval: float = 0.3,
                 clock: Callable[[], float] = time.monotonic, detector_factory=None,
                 log: Callable[..., None] = dlog, wall: Callable[[], float] = time.time):
        self.callback = callback
        self._enabled = bool(enabled)
        self.min_interval = float(min_interval)
        self._clock = clock
        self._wall = wall
        self._log = log
        self._factory = detector_factory or (lambda cb: ClickDetector(cb))
        self._det = None
        self._state = STOPPED
        self._last_accepted: float | None = None
        self._lock = threading.Lock()
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._worker: threading.Thread | None = None
        self._fired = False
        self.last_reason = ""
        self.presses = self.dropped = self.received = 0
        self.last_press = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def state(self) -> str:
        return self._state

    @property
    def active(self) -> bool:
        return self._state == ACTIVE

    def diagnostics(self) -> dict:
        det = self._det
        return {"state": self._state, "reason": self.last_reason, "enabled": self.enabled,
                "presses": self.presses, "dropped": self.dropped, "received": self.received,
                "last_press": self.last_press, "clicks_seen": int(getattr(det, "clicks_seen", 0) or 0),
                "errors": int(getattr(det, "errors", 0) or 0)}

    def start(self) -> ClickResult:
        if not self.enabled:
            return ClickResult(False, "")
        with self._lock:
            if self._state == ACTIVE:
                return ClickResult(True)
        try:
            self._det = self._factory(self._notify)
            self._q = queue.SimpleQueue()
            self._worker = threading.Thread(target=self._drain, name="kabeuchi-click-press", daemon=True)
            self._worker.start()
            r = ClickResult(True)
        except Exception as e:
            r = ClickResult(False, f"舌打ち検出の開始で例外: {type(e).__name__}: {e}")
        with self._lock:
            self._state = next_state(self._state, "start_ok" if r.ok else "start_fail")
            self.last_reason = "" if r.ok else r.reason
            self._last_accepted = None
        self._log("click_register", via=PATH_CLICK, ok=bool(r.ok), reason=("" if r.ok else r.reason), state=self._state)
        if r.ok:
            atexit.register(self.stop)
        return r

    def stop(self) -> None:
        with self._lock:
            was = self._state
            self._state = next_state(self._state, "stop")
        w, self._worker = self._worker, None
        if w is not None:
            self._q.put(None)
            if w is not threading.current_thread():
                w.join(timeout=1)
        if was != STOPPED:
            self._log("click_stop", via=PATH_CLICK, was=was, presses=self.presses, dropped=self.dropped)
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass

    def feed(self, x, on_detect: Callable[[int], None] | None = None) -> None:
        """録音の音声ブロック（int16・16kHz）を検出器へ。軽く・例外を出さない。
        検出したら、確定の通知（別スレッド）より先に on_detect(合図の開始から今までのサンプル数) を呼ぶ
        （合図の音を区切り器から取り除く用。確定の flush より前に済ませるため）。"""
        if self._state != ACTIVE:
            return
        det = self._det
        if det is None:
            return
        self._fired = False
        try:
            det.feed(x)
        except Exception:
            pass
        if self._fired:
            self._fired = False
            if on_detect is not None:
                try:
                    ago = getattr(det, "samples_since_pair_start", lambda: None)()
                    if ago is not None:
                        on_detect(int(ago))
                except Exception:
                    pass
            self._q.put(1)

    def _notify(self) -> None:        # 検出器から（録音スレッド）。feed の最後にキューへ入れる
        self._fired = True

    def _drain(self) -> None:
        q = self._q
        while True:
            item = q.get()
            if item is None:
                return
            try:
                self._on_press()
            except Exception:
                pass

    def _on_press(self) -> None:
        now = self._clock()
        with self._lock:
            self.received += 1
            self.last_press = time.strftime("%H:%M:%S", time.localtime(self._wall()))
            if self._state != ACTIVE:
                self._log("click_press", via=PATH_CLICK, accepted=False, why=f"state={self._state}")
                return
            if classify_press(now, self._last_accepted, PATH_CLICK, PATH_CLICK, self.min_interval) != ACCEPT:
                self.dropped += 1
                self._log("click_press", via=PATH_CLICK, accepted=False, why="間引き（直前の検出から短すぎる）")
                return
            self._last_accepted = now
            self.presses += 1
        self._log("click_press", via=PATH_CLICK, accepted=True, n=self.presses)
        try:
            self.callback()
        except Exception as e:
            self._log("click_callback_error", via=PATH_CLICK, error=f"{type(e).__name__}: {e}")
