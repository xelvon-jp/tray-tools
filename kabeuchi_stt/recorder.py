# -*- coding: utf-8 -*-
"""録音コア（GUI・Qt・COM に依存しない）。

- start(wav_path) / stop() だけで操作できる。pause() / resume() で一時停止（同じ WAV に続ける）。
- 入力ストリームは AudioInput が持つ。Recorder に source（待機中モニターと共有する AudioInput）を渡すと、
  デバイスを開き直さず同じストリームを引き継ぐ。渡さなければ自前の AudioInput を開く（v1 互換）。
- 音声コールバックは「キューに積むだけ」。ディスク書き込み・リサンプル・外部リスナー呼び出しは
  すべて別スレッド（writer）で行う。
- WAV は 1 秒ごとにヘッダのサイズ欄を更新するので、プロセスが落ちてもそれまでの録音は WAV として開ける。
- 認識側（on_audio リスナー）が遅い・例外を投げても録音は止めない。
- 一時停止中は WAV に書かない（elapsed は一時停止分を含まない）。一時停止/再開の位置は writer のキューに
  印として流し、音声ブロックとの順序を保ったまま on_marker(kind, WAV上の秒) を呼ぶ。
"""
from __future__ import annotations

import queue
import struct
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from audio_input import (SAMPLE_RATE, AudioInput, level_from_rms, list_input_devices,  # noqa: F401  (再公開)
                         resolve_device, to_16k)

_HEADER_SIZE = 44


class WavWriter:
    """16kHz mono 16bit の WAV を追記しながら、定期的にヘッダを確定させる。"""

    def __init__(self, path: Path, sample_rate: int = SAMPLE_RATE):
        self.path = Path(path)
        self.sample_rate = sample_rate
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "wb")
        self._data_bytes = 0
        self._last_patch = time.monotonic()
        self._f.write(self._header(0))
        self._f.flush()

    def _header(self, data_bytes: int) -> bytes:
        sr = self.sample_rate
        return (
            b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16)
            + b"data" + struct.pack("<I", data_bytes)
        )

    def write(self, samples: np.ndarray) -> None:
        b = samples.astype("<i2", copy=False).tobytes()
        self._f.write(b)
        self._data_bytes += len(b)
        now = time.monotonic()
        if now - self._last_patch >= 1.0:
            self._patch()
            self._last_patch = now

    def _patch(self) -> None:
        self._f.flush()
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(self._header(self._data_bytes))
        self._f.seek(pos)
        self._f.flush()

    @property
    def seconds(self) -> float:
        return self._data_bytes / 2 / self.sample_rate

    def close(self) -> None:
        try:
            self._patch()
        finally:
            self._f.close()


class Recorder:
    def __init__(
        self,
        device=None,
        on_audio: Callable[[np.ndarray], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        sd=None,
        source: AudioInput | None = None,
        on_marker: Callable[[str, float], None] | None = None,
    ):
        self._device_setting = device
        self.on_audio = on_audio
        self.on_error = on_error
        self.on_marker = on_marker
        self._sd = sd
        self._source = source
        self._own_source = False
        self._q: queue.Queue = queue.Queue()
        self._writer_thread: threading.Thread | None = None
        self._wav: WavWriter | None = None
        self._recording = False
        self._paused = False
        self._src_rate = SAMPLE_RATE
        self._final_seconds = 0.0
        self.pause_marks: list[tuple[str, float]] = []   # ("pause"|"resume", WAV上の秒)
        self.wav_path: Path | None = None
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()  # 音声スレッドの sink と pause/resume/stop の順序を保つ

    # ---- 状態
    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def is_paused(self) -> bool:
        return self._recording and self._paused

    @property
    def elapsed(self) -> float:
        """ディスクに書けた音声の長さ（秒）。実時間ではなく実際に録れた量（一時停止分は含まない）。"""
        w = self._wav
        return w.seconds if w else self._final_seconds

    @property
    def level(self) -> float:
        s = self._source
        return s.level if s is not None else 0.0

    @property
    def seconds_since_audio(self) -> float:
        """最後に音声ブロックが届いてからの秒数。大きければデバイスが止まっている。"""
        s = self._source
        return s.seconds_since_audio if s is not None else 0.0

    @property
    def backlog_blocks(self) -> int:
        return self._q.qsize()

    # ---- 操作
    def start(self, wav_path: Path) -> Path:
        with self._lock:
            if self._recording:
                raise RuntimeError("すでに録音中です")
            self.last_error = None
            self.wav_path = Path(wav_path)
            self._final_seconds = 0.0
            self.pause_marks = []
            self._paused = False
            self._wav = WavWriter(self.wav_path)
            self._q = queue.Queue()
            if self._source is None:
                self._source = AudioInput(self._device_setting, self._sd, on_error=self._report)
                self._own_source = True
            src = self._source
            try:
                src.open()  # すでに開いている（待機中モニター）ならそのまま引き継ぐ
            except Exception:
                if self._own_source:
                    self._source, self._own_source = None, False
                self._wav.close()
                self._wav = None
                raise
            self._src_rate = src.src_rate
            self._recording = True
            self._writer_thread = threading.Thread(
                target=self._writer_loop, name="kabeuchi-writer", daemon=True)
            self._writer_thread.start()
            src.attach(self._sink)
            return self.wav_path

    def pause(self) -> bool:
        """一時停止（WAV に書かない）。すでに止まっている/録音中でなければ False。"""
        with self._state_lock:
            if not self._recording or self._paused:
                return False
            self._paused = True
            self._q.put(("pause",))
            return True

    def resume(self) -> bool:
        with self._state_lock:
            if not self._recording or not self._paused:
                return False
            self._paused = False
            self._q.put(("resume",))
            return True

    def stop(self) -> Path | None:
        with self._lock:
            if not self._recording:
                return self.wav_path
            with self._state_lock:
                self._recording = False
                self._paused = False
            src = self._source
            if src is not None:
                src.detach()
                if self._own_source:
                    src.close()
                    self._source, self._own_source = None, False
            self._q.put(None)  # 終端
            t = self._writer_thread
        if t is not None:
            t.join(timeout=30)
        with self._lock:
            if self._wav is not None:
                self._final_seconds = self._wav.seconds
                self._wav.close()
                self._wav = None
        return self.wav_path

    # ---- 内部
    def _sink(self, x: np.ndarray) -> bool:  # 音声スレッドから。キューに積むだけ
        with self._state_lock:
            if not self._recording or self._paused:
                return False
            self._q.put_nowait(x)
            return True

    def _writer_loop(self):
        q = self._q
        while True:
            x = q.get()
            if x is None:
                break
            if isinstance(x, tuple):  # 一時停止/再開の印（音声ブロックとの順序は保たれる）
                try:
                    sec = self._wav.seconds if self._wav else 0.0
                    self.pause_marks.append((x[0], sec))
                    if self.on_marker is not None:
                        self.on_marker(x[0], sec)
                except Exception as e:
                    self._report(f"一時停止の印で例外（録音は継続）: {e}")
                continue
            try:
                x = to_16k(x, self._src_rate)
                self._wav.write(x)  # ディスクが最優先
            except Exception as e:
                self._report(f"WAV 書き込み失敗: {e}")
                continue
            if self.on_audio is not None:
                try:
                    self.on_audio(x)  # 認識側はキューに積むだけの実装であること
                except Exception as e:
                    self._report(f"on_audio 例外（録音は継続）: {e}")
                    self.on_audio = None

    def _report(self, msg: str):
        self.last_error = msg
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass
