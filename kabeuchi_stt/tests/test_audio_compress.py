# -*- coding: utf-8 -*-
"""録音の Opus 圧縮: 変換・各設定（off/keep/trash/delete）・失敗時に WAV が残る・履歴/清書キュー/再生が Opus を扱える。
ごみ箱は実物を使わない（trasher を差し替え）。"""
import contextlib
import tempfile
import unittest
import wave
from pathlib import Path

import _common as C
import audio_compress as AC
from finalize_queue import FinalizeQueue
from history import History
from pipeline import make_run_job
from replacements import Replacer
from transcriber import load_wav_float

SR = 16000


def write_wav(path: Path, sec: float = 3.0) -> Path:
    x = C.speechlike(sec, -12)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.tobytes())
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = Path(self._d.name)
        self.wav = write_wav(self.d / "kabeuchi_20261123_000000.wav")
        self.trashed = []

    def tearDown(self):
        self._d.cleanup()

    def trasher(self, p):
        self.trashed.append(Path(p).name)
        Path(p).unlink()


class ConvertTest(Base):
    def test_encode_decode_length(self):
        out = AC.encode_opus(self.wav, self.d / "a.opus", 24)
        self.assertTrue(out.exists() and out.stat().st_size > 0)
        self.assertLess(out.stat().st_size, self.wav.stat().st_size / 5)
        self.assertAlmostEqual(len(AC.decode_audio_float(out)) / SR, 3.0, delta=0.2)
        self.assertAlmostEqual(len(load_wav_float(out)) / SR, 3.0, delta=0.2)   # 清書の入力として読める

    def test_modes(self):
        for mode, wav_left, trashed in (("keep", True, 0), ("trash", False, 1), ("delete", False, 0)):
            with self.subTest(mode=mode):
                w = write_wav(self.d / f"{mode}.wav")
                self.trashed.clear()
                r = AC.compress_after_final(w, {"audio_compress": mode}, trasher=self.trasher)
                self.assertEqual(r, w.with_suffix(".opus"))
                self.assertTrue(r.exists())
                self.assertEqual(w.exists(), wav_left)
                self.assertEqual(len(self.trashed), trashed)

    def test_off_and_default_and_bad_value(self):
        self.assertIsNone(AC.compress_after_final(self.wav, {"audio_compress": "off"}, trasher=self.trasher))
        self.assertTrue(self.wav.exists())
        self.assertFalse(self.wav.with_suffix(".opus").exists())
        self.assertEqual(AC.normalize_mode(None), "delete")     # 既定は delete（Opus に圧縮して、元の WAV を完全に削除）
        self.assertEqual(AC.normalize_mode("へん"), "delete")
        r = AC.compress_after_final(self.wav, {}, trasher=self.trasher)   # 設定なし=既定 delete
        self.assertIsNotNone(r)
        self.assertFalse(self.wav.exists())
        self.assertEqual(self.trashed, [])

    def test_failure_keeps_wav_even_for_delete(self):
        bad = self.d / "bad.wav"
        bad.write_bytes(b"RIFFxxxxWAVEfmt garbage")
        for mode in ("trash", "delete"):
            r = AC.compress_after_final(bad, {"audio_compress": mode}, trasher=self.trasher)
            self.assertIsNone(r)
            self.assertTrue(bad.exists())
            self.assertFalse(bad.with_suffix(".opus").exists())
            self.assertFalse(list(self.d.glob("bad*.tmp")))
        self.assertEqual(self.trashed, [])

    def test_zero_length_keeps_wav(self):
        z = self.d / "zero.wav"
        with wave.open(str(z), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
        r = AC.compress_after_final(z, {"audio_compress": "delete"}, trasher=self.trasher)
        self.assertIsNone(r)
        self.assertTrue(z.exists())

    def test_trash_failure_keeps_wav_but_opus_is_made(self):
        def boom(p):
            raise OSError("no")
        r = AC.compress_after_final(self.wav, {"audio_compress": "trash"}, trasher=boom)
        self.assertTrue(r.exists() and self.wav.exists())

    def test_existing_opus_not_overwritten(self):
        other = self.wav.with_suffix(".opus")
        other.write_bytes(b"mine")
        r = AC.compress_after_final(self.wav, {"audio_compress": "keep"}, trasher=self.trasher)
        self.assertNotEqual(r, other)
        self.assertEqual(other.read_bytes(), b"mine")

    def test_non_wav_or_missing_is_skipped(self):
        self.assertIsNone(AC.compress_after_final(self.d / "none.wav", {}, trasher=self.trasher))
        o = AC.encode_opus(self.wav, self.d / "x.opus")
        self.assertIsNone(AC.compress_after_final(o, {}, trasher=self.trasher))

    def test_playback_source(self):
        self.assertEqual(AC.playback_source(self.wav), str(self.wav))
        o = AC.encode_opus(self.wav, self.d / "p.opus")
        p = Path(AC.playback_source(o, cache_dir=self.d / "cache"))
        self.assertEqual(p.suffix, ".wav")
        self.assertAlmostEqual(len(load_wav_float(p)) / SR, 3.0, delta=0.2)
        self.assertEqual(AC.playback_source(""), "")

    def test_settings_defaults(self):
        from settings import DEFAULTS
        self.assertEqual(DEFAULTS["audio_compress"], "delete")
        self.assertEqual(DEFAULTS["audio_compress_kbps"], 24)
        self.assertEqual(AC.kbps_of({"audio_compress_kbps": "x"}), 24)
        self.assertEqual(AC.kbps_of({"audio_compress_kbps": 1}), 6)


class _FakeResult:
    def __init__(self):
        self.segments = [(0.0, 1.0, "あ。")]
        self.audio_seconds = 3.0
        self.txt_path = self.md_path = None
        self.text = "あ。"
        self.elapsed = 0.1


class PipelineTest(Base):
    """清書キュー経由: 清書が WAV を読み終えたあとに圧縮・履歴の更新。やり直しは Opus からできる。"""

    def setUp(self):
        super().setUp()
        import history
        self.h = History(self.d / "h.db")
        self.seen = []
        self.settings = {"audio_compress": "trash", "final_model": "x"}

        def transcribe(wav, s, **kw):
            self.seen.append((Path(wav), Path(wav).exists()))   # 清書の時点で入力がある
            load_wav_float(Path(wav))                             # 実際に読める
            return _FakeResult()

        class Cache:
            def use(self_, name, th):
                return contextlib.nullcontext(object())
        run = make_run_job(lambda: self.settings, Replacer([]), self.h, Cache(), transcribe=transcribe)
        # trash は実ごみ箱を使わないよう、モジュールの関数を差し替える
        self._orig = history.send_to_recycle_bin
        history.send_to_recycle_bin = self.trasher
        self.q = FinalizeQueue(run)

    def tearDown(self):
        import history
        history.send_to_recycle_bin = self._orig
        self.q.close()
        self.h.close()
        super().tearDown()

    def test_record_job_compresses_after_final_and_updates_history(self):
        job = self.q.submit(self.wav, compress=True)
        self.assertTrue(self.q.wait_idle(30))
        self.assertEqual(job.status, "done")
        self.assertEqual(self.seen, [(self.wav, True)])
        rec = self.h.get(job.history_id)
        self.assertEqual(Path(rec.wav_path), self.wav.with_suffix(".opus"))
        self.assertTrue(Path(rec.wav_path).exists())
        self.assertFalse(self.wav.exists())
        self.assertEqual(self.trashed, [self.wav.name])
        # やり直し: Opus を入力にできる（圧縮はしない・パスは変わらない）
        j2 = self.q.submit(Path(rec.wav_path), history_id=rec.id)
        self.assertTrue(self.q.wait_idle(30))
        self.assertEqual(j2.status, "done")
        self.assertEqual(self.h.get(rec.id).wav_path, rec.wav_path)
        # 削除: Opus もごみ箱へ（実ごみ箱は使わない）
        self.trashed.clear()
        failed = self.h.delete(rec.id, trasher=self.trasher)
        self.assertEqual(failed, [])
        self.assertEqual(self.trashed, [Path(rec.wav_path).name])

    def test_manual_job_does_not_compress(self):
        job = self.q.submit(self.wav)   # WAV清書（任意のファイル）は触らない
        self.assertTrue(self.q.wait_idle(30))
        self.assertTrue(self.wav.exists())
        self.assertEqual(self.h.get(job.history_id).wav_path, str(self.wav))

    def test_off_keeps_wav_in_history(self):
        self.settings["audio_compress"] = "off"
        job = self.q.submit(self.wav, compress=True)
        self.assertTrue(self.q.wait_idle(30))
        self.assertEqual(self.h.get(job.history_id).wav_path, str(self.wav))
        self.assertTrue(self.wav.exists())

    def test_set_audio_path_only_touches_that_row(self):
        a = self.h.add(text="a", segments=[], duration=1, model="m", wav_path="a.wav")
        b = self.h.add(text="b", segments=[], duration=1, model="m", wav_path="b.wav")
        self.h.set_audio_path(a, "a.opus")
        self.assertEqual((self.h.get(a).wav_path, self.h.get(b).wav_path), ("a.opus", "b.wav"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
