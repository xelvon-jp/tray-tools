# -*- coding: utf-8 -*-
"""待機中（録音していない）に「話しているようだ」を検出する（純関数中心・GUI/Qt/COM 非依存）。

- frame_voiced(...)      : 0.1 秒フレーム1つが「発話らしい」か（床より十分大きい・クリック音ではない）
- speech_run_seconds(...) : 発話らしいフレーム列の、末尾の連続した長さ（短い息継ぎは許す）
- should_prompt(...)      : 指摘を出すべきか（有効・窓が見えている・録音していない・長さ・クールダウン）
- IdleSpeechWatcher       : 音声ブロックを渡すと上の判定を回す。音声は保持しない（数値だけ）
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Sequence

import numpy as np

from mic_check import FRAME, SAMPLE_RATE, to_dbfs

FRAME_SEC = FRAME / SAMPLE_RATE  # 0.1 秒

# 既定のしきい値
ABS_MIN_RMS_DBFS = -45.0   # RMS がこれ未満は発話とみなさない（極小ノイズ対策）
OVER_FLOOR_DB = 10.0       # 直近の床（RMS 下位10%点）よりこれ以上大きいこと
MAX_CREST_DB = 20.0        # ピーク−RMS がこれを超える（キーボードのクリック等の尖った音）は発話ではない
MAX_GAP_SEC = 0.6          # 発話の連続とみなす、息継ぎの最大長
MIN_DENSITY = 0.5          # 連続区間のうち「発話らしい」フレームが占める割合の下限


def frame_voiced(rms_dbfs: float, peak_dbfs: float, floor_dbfs: float,
                 abs_min_dbfs: float = ABS_MIN_RMS_DBFS, over_floor_db: float = OVER_FLOOR_DB,
                 max_crest_db: float = MAX_CREST_DB) -> bool:
    """フレーム1つが発話らしいか。床より十分大きく、絶対値も小さすぎず、尖った音でもない。"""
    if rms_dbfs < abs_min_dbfs:
        return False
    # 床より十分大きい。ただし十分に大きい音（abs_min+20dB 以上）は、床が未確定でも発話とみなす
    if rms_dbfs - floor_dbfs < over_floor_db and rms_dbfs < abs_min_dbfs + 20.0:
        return False
    if peak_dbfs - rms_dbfs > max_crest_db:
        return False
    return True


def speech_run_seconds(voiced: Sequence[bool], frame_sec: float = FRAME_SEC,
                       max_gap_sec: float = MAX_GAP_SEC, min_density: float = MIN_DENSITY) -> float:
    """列の**末尾**で続いている発話の長さ（秒）。

    末尾から遡り、発話らしくないフレームが max_gap_sec より長く続いた所で区切る。
    区間（最初の発話フレーム〜最後の発話フレーム）の中で発話らしいフレームが min_density 未満なら 0。
    末尾が発話でも、最後の発話フレームから max_gap_sec を超えて静かなら 0（もう話していない）。
    """
    n = len(voiced)
    if n == 0:
        return 0.0
    max_gap = int(round(max_gap_sec / frame_sec))
    # 末尾の静かな長さ
    tail = 0
    while tail < n and not voiced[n - 1 - tail]:
        tail += 1
    if tail >= n or tail > max_gap:
        return 0.0
    end = n - tail          # 最後の発話フレームの次
    i, gap = end - 1, 0
    start = end - 1
    while i >= 0:
        if voiced[i]:
            gap = 0
            start = i
        else:
            gap += 1
            if gap > max_gap:
                break
        i -= 1
    length = end - start
    count = sum(1 for k in range(start, end) if voiced[k])
    if length <= 0 or count / length < min_density:
        return 0.0
    return length * frame_sec


def should_prompt(run_sec: float, now: float, last_prompt_at: float | None, *, enabled: bool = True,
                  min_speech_sec: float = 1.5, cooldown_sec: float = 60.0, window_active: bool = True,
                  recording: bool = False) -> bool:
    """「録音していません」を出すべきか（純関数）。窓が隠れている・録音中・無効のときは出さない。"""
    if not enabled or recording or not window_active:
        return False
    if run_sec < min_speech_sec:
        return False
    if last_prompt_at is not None and (now - last_prompt_at) < cooldown_sec:
        return False
    return True


IDLE, PAUSED = "idle", "paused"


def prompt_kind(is_recording: bool, is_paused: bool) -> str | None:
    """どの指摘の対象か（純関数）。録音していない → "idle"（録音していません）、一時停止中 → "paused"
    （一時停止中です）、録音中（一時停止でない）→ None（話していて当然なので指摘しない）。"""
    if not is_recording:
        return IDLE
    return PAUSED if is_paused else None


class IdleSpeechWatcher:
    """待機中モニターの音声ブロックから発話らしさを追う。feed() は軽い（0.1 秒ごとに数値を積むだけ）。"""

    def __init__(self, abs_min_dbfs: float = ABS_MIN_RMS_DBFS, over_floor_db: float = OVER_FLOOR_DB,
                 max_crest_db: float = MAX_CREST_DB, max_gap_sec: float = MAX_GAP_SEC):
        self.abs_min_dbfs, self.over_floor_db = abs_min_dbfs, over_floor_db
        self.max_crest_db, self.max_gap_sec = max_crest_db, max_gap_sec
        self._lock = threading.Lock()
        self._carry = np.zeros(0, dtype=np.int16)
        self._rms: deque[float] = deque(maxlen=300)     # 直近30秒の RMS（床の推定）
        self._voiced: deque[bool] = deque(maxlen=200)   # 直近20秒
        self._floor_db = -120.0
        self._n = 0

    def feed(self, x: np.ndarray) -> None:
        x = np.asarray(x)
        if x.size == 0:
            return
        with self._lock:
            buf = np.concatenate([self._carry, x]) if self._carry.size else x
            n = len(buf) // FRAME
            for i in range(n):
                fr = buf[i * FRAME:(i + 1) * FRAME]
                peak = float(np.abs(fr.astype(np.int32)).max())
                rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
                self._rms.append(rms)
                self._n += 1
                if self._n % 5 == 1:
                    self._floor_db = to_dbfs(float(np.percentile(list(self._rms), 10)))
                self._voiced.append(frame_voiced(to_dbfs(rms), to_dbfs(peak), self._floor_db,
                                                 self.abs_min_dbfs, self.over_floor_db, self.max_crest_db))
            self._carry = buf[n * FRAME:].copy()

    @property
    def run_sec(self) -> float:
        with self._lock:
            v = list(self._voiced)
        return speech_run_seconds(v, FRAME_SEC, self.max_gap_sec)

    def speaking_now(self, frames: int = 4) -> bool:
        """直近 frames フレーム（既定 0.4 秒）に発話らしい音があったか（メーターを赤にする即時判定用）。"""
        with self._lock:
            return any(list(self._voiced)[-frames:]) if self._voiced else False

    def reset_run(self) -> None:
        """指摘を出したあとなど、発話の連続だけを捨てる（床の推定は残す）。"""
        with self._lock:
            self._voiced.clear()

    def reset(self) -> None:
        with self._lock:
            self._carry = np.zeros(0, dtype=np.int16)
            self._rms.clear()
            self._voiced.clear()
            self._floor_db = -120.0
            self._n = 0
