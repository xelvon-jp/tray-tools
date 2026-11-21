# -*- coding: utf-8 -*-
"""無音が長く続いたら自動停止するための監視（止め忘れ対策。GUI/Qt/COM 非依存）。"""
from __future__ import annotations

import threading
from collections import deque

import numpy as np

from mic_check import FRAME, SAMPLE_RATE, to_dbfs


class SilenceWatcher:
    """0.1 秒フレームごとに「発話あり」を判定する:
      (a) フレームのピークが abs_dbfs 以上、かつ RMS が直近30秒の床（RMS下位10%点）より 10dB 以上大きい、または
      (b) RMS が abs_dbfs+20 dBFS 以上（録音の頭から話し続けていて床が未確定でも発話と判定）。
    最後に発話ありのフレームを見てからの無音の秒数が limit_sec 以上になったら should_stop() が真。
    limit_sec=0 なら無効。
    """

    def __init__(self, limit_sec: float = 180.0, abs_dbfs: float = -50.0):
        self.limit_sec = float(limit_sec)
        self.abs_dbfs = float(abs_dbfs)
        self._lock = threading.Lock()
        self._carry = np.zeros(0, dtype=np.int16)
        self._rms: deque[float] = deque(maxlen=300)
        self._floor_db = -120.0
        self._n = 0
        self.silent_sec = 0.0

    def feed(self, x: np.ndarray) -> None:
        x = np.asarray(x)
        if x.size == 0:
            return
        with self._lock:
            buf = np.concatenate([self._carry, x]) if self._carry.size else x
            n = len(buf) // FRAME
            for i in range(n):
                fr = buf[i * FRAME:(i + 1) * FRAME]
                peak_db = to_dbfs(float(np.abs(fr.astype(np.int32)).max()))
                rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
                self._rms.append(rms)
                self._n += 1
                if self._n % 10 == 1:
                    self._floor_db = to_dbfs(float(np.percentile(list(self._rms), 10)))
                rms_db = to_dbfs(rms)
                voiced = ((peak_db >= self.abs_dbfs and (rms_db - self._floor_db) >= 10.0)
                          or rms_db >= self.abs_dbfs + 20.0)
                if voiced:
                    self.silent_sec = 0.0
                else:
                    self.silent_sec += FRAME / SAMPLE_RATE
            self._carry = buf[n * FRAME:].copy()

    def should_stop(self) -> bool:
        return self.limit_sec > 0 and self.silent_sec >= self.limit_sec

    def reset(self) -> None:
        with self._lock:
            self.silent_sec = 0.0
            self._rms.clear()
            self._carry = np.zeros(0, dtype=np.int16)
            self._n = 0
            self._floor_db = -120.0
