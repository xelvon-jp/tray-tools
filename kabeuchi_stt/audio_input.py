# -*- coding: utf-8 -*-
"""入力ストリーム（マイク）を 1 本だけ持ち、ブロックを「録音」か「待機中モニター」へ配る（Qt/COM 非依存）。

- 待機中モニター（窓が見えている間だけ開く）と録音は**同じストリーム**を使う。録音開始時に
  デバイスを開き直さないので、頭が欠けない。録音が終わってもストリームは開いたまま（閉じるかどうかは呼び出し側）。
- 音声コールバック（音声スレッド）は「音量の計算とキューに積むだけ」。ディスク・リサンプル・認識は別スレッド。
- 録音側の受け口（sink）は attach()/detach() で差し替える。sink が True を返したブロックは録音が受け取った
  もの。False（一時停止中など）や sink 無しのブロックは待機中の経路へ流れ、idle_consumer が数値だけ見て捨てる
  （**保存も認識もしない**）。
- ストリームは 16kHz mono int16 で開く。受け付けないデバイスは既定レートで開き、待機中の経路・録音の
  writer が 16kHz へ変換する。
"""
from __future__ import annotations

import math
import queue
import threading
import time
from typing import Callable

import numpy as np

SAMPLE_RATE = 16000
IDLE_QUEUE_MAX = 400  # 待機中の経路が詰まっても無限に溜めない（約40秒ぶん。古いものを諦める）


def level_from_rms(rms: float) -> float:
    """RMS(int16) を 0..1 のメーター値へ（-60dBFS..0dBFS）。"""
    if rms <= 0:
        return 0.0
    db = 20 * math.log10(rms / 32768.0)
    return max(0.0, min(1.0, (db + 60.0) / 60.0))


def to_16k(x: np.ndarray, src_rate: int) -> np.ndarray:
    """int16 のブロックを 16kHz へ線形補間で変換（src_rate が 16kHz ならそのまま）。"""
    if src_rate == SAMPLE_RATE or len(x) == 0:
        return x
    n = max(1, int(round(len(x) * SAMPLE_RATE / src_rate)))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float32)).astype(np.int16)


def list_input_devices(sd=None) -> list[dict]:
    """入力可能なデバイス一覧。マイクは開かない。"""
    if sd is None:
        import sounddevice as sd  # type: ignore
    out = []
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None
    apis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "hostapi": apis[d["hostapi"]]["name"],
                "default_samplerate": d.get("default_samplerate"),
                "is_default": i == default_in,
            })
    return out


def resolve_device(setting, sd=None):
    """設定値（None / 番号 / 名前の一部）を sounddevice のデバイス指定に変換。"""
    if setting is None or setting == "":
        return None
    if isinstance(setting, int):
        return setting
    s = str(setting)
    if s.isdigit():
        return int(s)
    for d in list_input_devices(sd):
        if s.lower() in d["name"].lower():
            return d["index"]
    raise ValueError(f"マイクデバイス '{setting}' が見つかりません。--list-devices で確認してください。")


class AudioInput:
    def __init__(self, device=None, sd=None, idle_consumer: Callable[[np.ndarray], None] | None = None,
                 on_error: Callable[[str], None] | None = None):
        self.device_setting = device
        self._sd = sd
        self.idle_consumer = idle_consumer
        self.on_error = on_error
        self._lock = threading.RLock()
        self._stream = None
        self._opened_device = object()     # 開いたときのデバイス指定（変更検知用）
        self._src_rate = SAMPLE_RATE
        self._level = 0.0
        self._last_cb = 0.0
        self._sink: Callable[[np.ndarray], bool] | None = None
        self._idle_q: queue.Queue = queue.Queue()
        self._idle_thread: threading.Thread | None = None
        self.opened_at = 0.0
        self.open_count = 0                # 何回デバイスを開いたか（引き継ぎで開き直していないことの確認用）

    # ---- 状態
    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def src_rate(self) -> int:
        return self._src_rate

    @property
    def level(self) -> float:
        return self._level if self._stream is not None else 0.0

    @property
    def seconds_since_audio(self) -> float:
        """最後にブロックが届いてからの秒数（開いた直後はその時点から数える）。"""
        if self._stream is None:
            return 0.0
        ref = self._last_cb or self.opened_at
        return time.monotonic() - ref if ref else 0.0

    @property
    def backlog_blocks(self) -> int:
        return self._idle_q.qsize()

    # ---- 開閉
    def open(self) -> None:
        """ストリームを開く（開いていて、デバイス指定が同じなら何もしない）。失敗したら例外。"""
        with self._lock:
            if self._stream is not None:
                if self._opened_device == self.device_setting:
                    return
                self.close()
            sd = self._sd
            if sd is None:
                import sounddevice as sd  # type: ignore
                self._sd = sd
            dev = resolve_device(self.device_setting, sd)
            self._level, self._last_cb = 0.0, 0.0
            self._src_rate = SAMPLE_RATE
            try:
                try:
                    st = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                        device=dev, blocksize=0, callback=self._callback)
                    st.start()
                except Exception:
                    # 16kHz を受け付けないデバイスは既定レートで開いて自前で変換する
                    info = sd.query_devices(dev if dev is not None else sd.default.device[0])
                    self._src_rate = int(info["default_samplerate"])
                    st = sd.InputStream(samplerate=self._src_rate, channels=1, dtype="int16",
                                        device=dev, blocksize=0, callback=self._callback)
                    st.start()
            except Exception:
                self._stream = None
                raise
            self._stream = st
            self._opened_device = self.device_setting
            self.opened_at = time.monotonic()
            self.open_count += 1
            self._idle_q = queue.Queue()
            self._idle_thread = threading.Thread(target=self._idle_loop, args=(self._idle_q,),
                                                 name="kabeuchi-idle-input", daemon=True)
            self._idle_thread.start()

    def close(self) -> None:
        with self._lock:
            st, self._stream = self._stream, None
            q, t, self._idle_thread = self._idle_q, self._idle_thread, None
            self._level = 0.0
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception as e:
                self._report(f"ストリーム停止で例外: {e}")
        if t is not None:
            q.put(None)
            t.join(timeout=5)

    # ---- 録音側の受け口
    def attach(self, sink: Callable[[np.ndarray], bool]) -> None:
        self._sink = sink

    def detach(self) -> None:
        self._sink = None

    # ---- 内部
    def _callback(self, indata, frames, time_info, status):  # 音声スレッド。軽く保つ
        try:
            x = np.array(indata[:, 0], dtype=np.int16, copy=True)
            self._last_cb = time.monotonic()
            rms = float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if len(x) else 0.0
            self._level = level_from_rms(rms)
            sink = self._sink
            if sink is not None:
                try:
                    if sink(x):
                        return
                except Exception:
                    pass
            q = self._idle_q
            if q.qsize() < IDLE_QUEUE_MAX:
                q.put_nowait(x)
        except Exception:
            pass

    def _idle_loop(self, q: queue.Queue) -> None:
        while True:
            x = q.get()
            if x is None:
                break
            cons = self.idle_consumer
            if cons is None:
                continue
            try:
                cons(to_16k(x, self._src_rate))
            except Exception as e:
                self._report(f"待機中モニターで例外（無視して続行）: {e}")

    def _report(self, msg: str) -> None:
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception:
                pass
