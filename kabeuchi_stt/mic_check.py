# -*- coding: utf-8 -*-
"""マイク音量の判定（純関数・GUI/Qt/COM 非依存）。

- measure(x)          : int16 配列から統計（ピーク/RMS/クリップ率/床）を出す
- diagnose(stats,...) : 統計としきい値から、指摘（Issue）のリストを返す
- MicMonitor          : 録音中の常時監視。直近 window 秒のブロック統計を保持して診断する
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
FRAME = 1600  # 0.1 秒
CLIP_LEVEL = 32000  # |x| がこれ以上なら張り付きとみなす（約 -0.2 dBFS）

THRESHOLD_DEFAULTS = {
    "mic_low_peak_dbfs": -30.0,
    "mic_clip_ratio": 0.001,
    "mic_noise_floor_dbfs": -45.0,
    "mic_dead_peak_dbfs": -70.0,
}


def to_dbfs(v: float) -> float:
    """int16 振幅 → dBFS。0 は -120 とする。"""
    if v <= 0:
        return -120.0
    return max(-120.0, 20 * math.log10(v / 32768.0))


@dataclass
class Stats:
    peak_dbfs: float
    rms_dbfs: float
    floor_dbfs: float      # 0.1秒フレームRMSの下位10%点（無音時の床）
    clip_ratio: float      # |x| >= CLIP_LEVEL の割合
    activity_db: float     # 最大フレームRMSと床の差（発話があったか）
    seconds: float


def measure(x: np.ndarray) -> Stats:
    x = np.asarray(x)
    if x.size == 0:
        return Stats(-120.0, -120.0, -120.0, 0.0, 0.0, 0.0)
    a = np.abs(x.astype(np.int32))
    peak = float(a.max())
    xf = x.astype(np.float32)
    rms = float(np.sqrt(np.mean(xf ** 2)))
    n = len(x) // FRAME
    if n >= 2:
        fr = xf[: n * FRAME].reshape(n, FRAME)
        fr_rms = np.sqrt(np.mean(fr ** 2, axis=1))
        floor = float(np.percentile(fr_rms, 10))
        top = float(fr_rms.max())
    else:
        floor = top = rms
    clip = float(np.mean(a >= CLIP_LEVEL))
    pk, fl = to_dbfs(peak), to_dbfs(floor)
    # 発話の動き = 最も大きいフレームの RMS と床の差（雑音のピークの揺れは含めない）
    return Stats(pk, to_dbfs(rms), fl, clip, to_dbfs(top) - fl, len(x) / SAMPLE_RATE)


@dataclass
class Issue:
    code: str       # silent / low / clipping / noisy
    severity: str   # warn / error
    message: str
    advice: str


def diagnose(st: Stats, th: dict | None = None, expect_speech: bool = True) -> list[Issue]:
    """統計から指摘を返す。

    expect_speech=True（マイクテスト: 話してもらう）なら、ピークが低いだけで「小さすぎ」。
    False（録音中の常時監視）は、発話らしい動き（最大フレームRMSと床の差が 6dB 以上）が
    あった窓だけを「小さすぎ」と判定する（考え中の無音で誤警告しないため）。
    """
    t = dict(THRESHOLD_DEFAULTS)
    if th:
        t.update({k: v for k, v in th.items() if k in t})
    if st.seconds <= 0:
        return []
    issues: list[Issue] = []
    if st.peak_dbfs < t["mic_dead_peak_dbfs"]:
        issues.append(Issue(
            "silent", "error", "音が入っていません",
            "マイクがミュート、または別のデバイスが選ばれているかもしれません。"
            "Windows のサウンド設定で入力デバイスとミュートを確認してください。"))
        return issues  # 他の判定は意味がない
    if st.clip_ratio > t["mic_clip_ratio"]:
        issues.append(Issue(
            "clipping", "warn", "音割れしています",
            "声が大きすぎるか入力音量が高すぎます。Windows の入力音量を下げるか、マイクから少し離れてください。"))
    elif st.peak_dbfs < t["mic_low_peak_dbfs"] and (expect_speech or st.activity_db >= 6.0):
        issues.append(Issue(
            "low", "warn", "音が小さすぎます",
            "マイクが遠いかもしれません。マイクに近づくか、Windows の入力音量を上げてください。"))
    if st.floor_dbfs > t["mic_noise_floor_dbfs"]:
        issues.append(Issue(
            "noisy", "warn", "ノイズが大きいです",
            "話していない間も雑音が入っています。ファンやエアコンの音、マイクの入力ブーストを確認してください。"))
    return issues


class MicMonitor:
    """録音中の常時監視。feed() は軽い（ブロックごとに統計を1つ積むだけ）。"""

    def __init__(self, thresholds: dict | None = None, window_sec: float = 8.0):
        self.th = thresholds or {}
        self.window_sec = max(1.0, float(window_sec))
        self._lock = threading.Lock()
        self._blocks: deque[tuple[float, float, float, int, int]] = deque()
        # (seconds, peak, sumsq, clipped, n)  ※床の推定用に0.1秒フレームRMSも別に持つ
        self._carry = np.zeros(0, dtype=np.int16)

    def feed(self, x: np.ndarray) -> None:
        x = np.asarray(x)
        if x.size == 0:
            return
        with self._lock:
            buf = np.concatenate([self._carry, x]) if self._carry.size else x
            n = len(buf) // FRAME
            for i in range(n):
                fr = buf[i * FRAME:(i + 1) * FRAME]
                a = np.abs(fr.astype(np.int32))
                rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
                self._blocks.append((FRAME / SAMPLE_RATE, float(a.max()), rms,
                                     int(np.sum(a >= CLIP_LEVEL)), FRAME))
            self._carry = buf[n * FRAME:].copy()
            maxn = int(self.window_sec / (FRAME / SAMPLE_RATE))
            while len(self._blocks) > maxn:
                self._blocks.popleft()

    def stats(self) -> Stats | None:
        with self._lock:
            blocks = list(self._blocks)
        if len(blocks) < 10:  # 1秒未満では判定しない
            return None
        peaks = np.array([b[1] for b in blocks])
        rmss = np.array([b[2] for b in blocks])
        clip = sum(b[3] for b in blocks) / sum(b[4] for b in blocks)
        pk, fl = to_dbfs(float(peaks.max())), to_dbfs(float(np.percentile(rmss, 10)))
        rms = to_dbfs(float(np.sqrt(np.mean(rmss ** 2))))
        return Stats(pk, rms, fl, clip, to_dbfs(float(rmss.max())) - fl, len(blocks) * FRAME / SAMPLE_RATE)

    def issues(self) -> list[Issue]:
        st = self.stats()
        return diagnose(st, self.th, expect_speech=False) if st else []

    def reset(self) -> None:
        with self._lock:
            self._blocks.clear()
            self._carry = np.zeros(0, dtype=np.int16)
