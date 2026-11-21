# -*- coding: utf-8 -*-
"""sounddevice を差し替えたモックで録音ループ＋逐次認識＋清書を検証する（マイクは開かない）。
使い方: python tests\test_mock_record.py <16kHz mono の WAV> [倍速=4]
"""
import sys, threading, time, wave
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from recorder import Recorder, SAMPLE_RATE
from transcriber import LiveTranscriber, transcribe_wav, wav_seconds
from settings import load_settings


class FakeStream:
    def __init__(self, samplerate, channels, dtype, device, blocksize, callback, src, speed):
        self.cb, self.src, self.speed = callback, src, speed
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
    def start(self): self._t.start()
    def _run(self):
        n = 1600  # 0.1s
        for i in range(0, len(self.src), n):
            if self._stop.is_set(): return
            blk = self.src[i:i + n].reshape(-1, 1)
            self.cb(blk, len(blk), None, None)
            time.sleep(0.1 / self.speed)
        while not self._stop.is_set():
            time.sleep(0.05)
    def stop(self): self._stop.set()
    def close(self): pass


class FakeSD:
    def __init__(self, src, speed): self.src, self.speed = src, speed
    def InputStream(self, **kw): return FakeStream(src=self.src, speed=self.speed, **kw)


def main():
    wav = Path(sys.argv[1]); speed = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    with wave.open(str(wav)) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    sil = np.zeros(SAMPLE_RATE * 2, dtype=np.int16)
    src = np.concatenate([sil, x, sil, x[: SAMPLE_RATE * 5], sil])  # 無音→音声→無音→途中まで→無音
    s = load_settings(create=False)
    s["out_dir"] = str(Path(__file__).resolve().parent.parent / "out" / "_mocktest")
    lines = []
    live = LiveTranscriber(s, on_text=lambda t: lines.append((time.time(), t)), on_status=print)
    rec = Recorder(on_audio=live.feed, sd=FakeSD(src, speed))
    out = Path(s["out_dir"]) / "mock.wav"
    rec.start(out); live.start()
    t0 = time.time()
    while time.time() - t0 < len(src) / SAMPLE_RATE / speed + 1:
        time.sleep(0.5)
        print(f"  elapsed={rec.elapsed:.1f}s level={rec.level:.2f} backlog={rec.backlog_blocks} live_pending={live.pending_segments}")
    rec.stop(); time.sleep(8); live.stop()
    print("録音長(秒):", rec.elapsed if rec.wav_path else 0, "WAV推定:", wav_seconds(out), "期待:", len(src) / SAMPLE_RATE)
    print("逐次認識:", [t for _, t in lines], "processed", live.processed_segments, "dropped", live.dropped_segments)
    r = transcribe_wav(out, s)
    print("清書:", r.text, f"({r.elapsed:.1f}s / {r.audio_seconds:.1f}s)")

if __name__ == "__main__":
    main()
