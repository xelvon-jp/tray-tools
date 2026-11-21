# -*- coding: utf-8 -*-
"""音声そのものから、区間の境目の「実際の無音の長さ」を測る（純関数・GUI/Qt/COM 非依存）。

Whisper（faster-whisper）の区間は、前の区間の終了＝次の区間の開始、のように**無音を区間の中に含めて**
連続して返すことがある（実録音 20261122_010803 で確認: 30 秒の沈黙があっても end==次の start）。
そのため、区間の時刻だけからすき間を求めると、すき間が常に 0 に見えて改行が効かない。
ここでは WAV のエネルギーから発話らしい区間を求め、各境目の前後に連続する無音の長さを返す。
"""
from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
FRAME_SEC = 0.05
MIN_THRESHOLD_DBFS = -55.0   # 発話の下限（これより小さい音は常に無音）
OVER_FLOOR_DB = 10.0         # 床（フレーム RMS の下位 10% 点）よりこれ以上大きければ発話


def voiced_frames(audio: np.ndarray, sample_rate: int = SAMPLE_RATE, frame_sec: float = FRAME_SEC) -> np.ndarray:
    """フレームごとの「発話らしいか」（bool 配列）。audio は float32(-1..1) か int16。"""
    x = np.asarray(audio)
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    else:
        x = x.astype(np.float32, copy=False)
    n = int(sample_rate * frame_sec)
    k = len(x) // n
    if k <= 0:
        return np.zeros(0, dtype=bool)
    fr = x[: k * n].reshape(k, n)
    rms = np.sqrt(np.mean(fr * fr, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    floor = float(np.percentile(db, 10))
    thr = max(MIN_THRESHOLD_DBFS, floor + OVER_FLOOR_DB)
    return db >= thr


def silence_at(voiced: np.ndarray, t: float, frame_sec: float = FRAME_SEC) -> float:
    """時刻 t をまたぐ、連続した無音の長さ（秒）。t の直前から遡る無音 + t から先へ続く無音。"""
    n = len(voiced)
    if n == 0:
        return 0.0
    i = min(max(int(round(t / frame_sec)), 0), n)
    back = 0
    j = i - 1
    while j >= 0 and not voiced[j]:
        back += 1
        j -= 1
    fwd = 0
    j = i
    while j < n and not voiced[j]:
        fwd += 1
        j += 1
    return (back + fwd) * frame_sec


def silence_runs(voiced: np.ndarray, frame_sec: float = FRAME_SEC, min_sec: float = 1.0) -> list[tuple[float, float]]:
    """連続した無音の (開始秒, 終了秒)。min_sec 未満の短い無音は含めない。"""
    runs, start = [], None
    for i, v in enumerate(voiced):
        if not v and start is None:
            start = i
        elif v and start is not None:
            if (i - start) * frame_sec >= min_sec:
                runs.append((start * frame_sec, i * frame_sec))
            start = None
    if start is not None and (len(voiced) - start) * frame_sec >= min_sec:
        runs.append((start * frame_sec, len(voiced) * frame_sec))
    return runs


def segment_gaps(segments: list, voiced: np.ndarray, frame_sec: float = FRAME_SEC,
                 tolerance: float = 1.5) -> list[float]:
    """各区間 i の**直前**の無音の長さ（i=0 は 0）。

    Whisper の区間の境目は、実際の発話の切れ目から少しずれる（発話の頭が境目の少し前に入る等）。
    そこで、各無音の連続区間を「いちばん近い境目」1 つだけに割り当てる（境目が無音の中にあれば距離 0、
    外なら無音の端までの距離。tolerance 秒以内のときだけ）。1 つの沈黙で複数の境目が改行にならない。
    """
    starts = []
    for seg in segments:
        try:
            starts.append(float(seg[0]))
        except (TypeError, ValueError, IndexError):
            starts.append(None)
    out = [0.0] * len(segments)
    for a, b in silence_runs(voiced, frame_sec):
        best, best_d = None, None
        for i in range(1, len(starts)):
            t = starts[i]
            if t is None:
                continue
            d = 0.0 if a <= t <= b else min(abs(t - a), abs(t - b))
            if d <= tolerance and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            out[best] = max(out[best], b - a)
    return out


def silence_gaps(audio: np.ndarray, segments: list, sample_rate: int = SAMPLE_RATE) -> list[float]:
    """音声と区間から、区間ごとの直前の無音の長さを返す。"""
    return segment_gaps(segments, voiced_frames(audio, sample_rate))
