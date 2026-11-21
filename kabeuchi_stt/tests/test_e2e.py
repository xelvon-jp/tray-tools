# -*- coding: utf-8 -*-
"""エンドツーエンド: 合成音声(SAPI) → 録音(モックのマイク) → 仮認識 → 停止 → キュー清書 → 置換 → 履歴 → 検索 → プロンプト。

実マイクは開かない（sounddevice を差し替え）。実モデルで認識するので 1〜2 分かかる。
Windows の日本語音声（Microsoft Haruka）が無ければスキップ。
"""
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from finalize_queue import DONE, FinalizeQueue
from history import History
from model_cache import ModelCache
from pipeline import make_run_job
from presets import build_prompt
from replacements import Replacer
from session import Session
from settings import load_settings
from transcriber import load_model

TEXT = "今日は予算の確認作業を、AIで自動化する方法を考えています。まず一次集計から始めます。"


def make_tts(path: Path) -> bool:
    ps = path.with_suffix(".ps1")
    ps.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1\n"
        "if (-not $v) { exit 3 }\n"
        "$s.SelectVoice($v.VoiceInfo.Name)\n"
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)\n"
        f"$s.SetOutputToWaveFile('{path}', $fmt)\n"
        f"$s.Speak('{TEXT}')\n"
        "$s.Dispose()\n", encoding="utf-8-sig")
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps)],
                       capture_output=True, timeout=120)
    return r.returncode == 0 and path.exists()


class FakeStream:
    def __init__(self, callback, src, speed, **kw):
        self.cb, self.src, self.speed = callback, src, speed
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        n = 1600
        for i in range(0, len(self.src), n):
            if self._stop.is_set():
                return
            blk = self.src[i:i + n].reshape(-1, 1)
            self.cb(blk, len(blk), None, None)
            time.sleep(0.1 / self.speed)
        while not self._stop.is_set():
            time.sleep(0.02)

    def stop(self):
        self._stop.set()

    def close(self):
        pass


class FakeSD:
    def __init__(self, src, speed):
        self.src, self.speed = src, speed

    def InputStream(self, **kw):
        return FakeStream(src=self.src, speed=self.speed, **kw)

    def query_devices(self, *a, **k):
        return []


def model_present(name: str) -> bool:
    """モデルが手元（model_dir）にあるか。無いときに勝手にダウンロードを始めないため、テストはスキップする。"""
    from settings import resolve_dir
    mdir = resolve_dir(load_settings(create=False).get("model_dir", "models"))
    return (mdir / name / "model.bin").exists() or any(mdir.glob(f"models--*faster-whisper-{name}"))


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class E2ETest(unittest.TestCase):
    def test_full_flow(self):
        s0 = load_settings(create=False)
        for need in (s0["final_model"], s0["live_model"]):
            if not model_present(need):
                self.skipTest(f"モデル {need} が model_dir にありません（SKIP。README の「モデルのオフライン手置き」）")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            tts = d / "tts.wav"
            if not make_tts(tts):
                self.skipTest("日本語の SAPI 音声がありません")
            with wave.open(str(tts)) as w:
                self.assertEqual(w.getframerate(), 16000)
                speech = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
            src = np.concatenate([np.zeros(C.SR, dtype=np.int16), speech, np.zeros(C.SR * 2, dtype=np.int16)])

            s = load_settings(create=False)
            s["out_dir"] = str(d / "out")
            s["hotwords"] = "予算, 一次集計, 自動化"
            replacer = Replacer([{"from": "予算", "to": "予算(NEW)"}])  # 必ず出る語で置換を確認
            hist = History(d / "h.db")
            cache = ModelCache(lambda n, th: load_model(n, s, cpu_threads=th), idle_sec=600)
            events = []
            q = FinalizeQueue(make_run_job(lambda: s, replacer, hist, cache), on_event=lambda j: events.append(j.status))
            sess = Session(s, replacer, model_cache=cache, sd=FakeSD(src, speed=8.0))

            t0 = time.time()
            wav = sess.start(d / "out" / "e2e.wav", live_enabled=True)
            while time.time() - t0 < len(src) / C.SR / 8.0 + 1.5:
                time.sleep(0.2)
            tl = sess.timeline
            wav = sess.stop()
            self.assertAlmostEqual(sess.elapsed, len(src) / C.SR, delta=0.5)
            n_rows_before = len(tl.rows())
            self.assertGreaterEqual(n_rows_before, 1)
            drafts = list(tl._drafts)  # 逐次認識の仮認識（開始・終了秒つき）
            print("[e2e] 仮認識:", drafts)
            self.assertGreaterEqual(len(drafts), 1, "逐次認識の仮認識が1つも出ていません")
            self.assertTrue(all(0 <= a_ < b_ <= len(src) / C.SR + 1 for a_, b_, _ in drafts))
            self.assertEqual(tl.rows()[0].state, "draft")

            job = q.submit(wav, timeline=tl)
            # 清書中でも次の録音を始められる（別 Timeline が作られ、古い方は影響を受けない）
            sess2 = Session(s, replacer, sd=FakeSD(src, speed=8.0))
            sess2.start(d / "out" / "e2e_second.wav", live_enabled=False)
            sess2.stop()
            self.assertTrue(q.wait_idle(600), "清書が終わりません")
            self.assertEqual(job.status, DONE, job.error)

            r = job.result
            # 認識結果の基本（TTS なので専門語まで拾えることを期待するが、ゆるく見る）
            self.assertIn("自動化", r.text)
            # 置換: 清書の出力に辞書が適用され、txt/md にも入っている
            self.assertIn("予算(NEW)", r.text)
            self.assertNotIn("予算(NEW)(NEW)", r.text)
            self.assertIn("予算(NEW)", Path(r.txt_path).read_text(encoding="utf-8"))
            self.assertIn("予算(NEW)", Path(r.md_path).read_text(encoding="utf-8"))
            # タイムライン: 仮認識の行が清書に置き換わった
            self.assertTrue(tl.is_final)
            self.assertTrue(all(r_.state == "final" for r_ in tl.rows()))
            self.assertEqual(tl.final_text(), r.text)
            # 履歴に登録され、検索できる
            self.assertEqual(len(hist.list()), 1)
            self.assertEqual([x.id for x in hist.search("NEW 自動化")], [job.history_id])
            self.assertEqual(hist.search("存在しない語"), [])
            rec = hist.get(job.history_id)
            self.assertEqual(rec.wav_path, str(wav))
            self.assertGreater(rec.duration, 3)
            self.assertEqual(rec.model, s["final_model"])
            # プロンプト組み立て
            out = build_prompt("要約3行とToDoにしてください。", rec.current_text)
            self.assertTrue(out.startswith("要約3行とToDoにしてください。"))
            self.assertIn("予算(NEW)", out)
            # モデルは常駐（2回目の清書で再ロードされない）
            loads_before = cache.loads
            job2 = q.submit(wav, history_id=job.history_id)
            self.assertTrue(q.wait_idle(600))
            self.assertEqual(job2.status, DONE, job2.error)
            self.assertEqual(cache.loads, loads_before)
            self.assertEqual(len(hist.list()), 1)  # やり直しは履歴を更新（増えない）
            q.close()
            hist.close()
            print("\n[e2e] 清書:", r.text, f"({r.elapsed:.1f}s / {r.audio_seconds:.1f}s)")
            print("[e2e] 行:", [(x.label, x.state) for x in tl.rows()])


if __name__ == "__main__":
    unittest.main()
