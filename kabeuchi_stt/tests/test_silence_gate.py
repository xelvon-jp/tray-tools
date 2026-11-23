# -*- coding: utf-8 -*-
"""無音カット（F2）・清書キューのゲート（A）・発話ごとの履歴（C）の部品テスト。実録音・画面は使わない。"""
import sqlite3
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
import audio_compress as AC
from finalize_queue import FinalizeQueue, DONE
from history import History
from pipeline import make_run_job
from replacements import Replacer
import test_dialogue_session as TD
from ui_unified_logic import finalize_progress_text, GATED_TEXT

SR = 16000


def f32(x):
    return x.astype(np.float32) / 32768.0


def synth():
    """3秒の声 + 10秒の無音 + 3秒の声 + 2秒の無音 + 3秒の声"""
    quiet = lambda s, seed: f32(C.noise(s, -75, seed))
    voice = lambda s: f32(C.sine(s, -12))
    return np.concatenate([voice(3), quiet(10, 1), voice(3), quiet(2, 2), voice(3)])


def write_wav(path, a):
    x = (a * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(x.tobytes())


class CutTest(unittest.TestCase):
    def test_find_and_apply(self):
        a = synth()
        cuts = AC.find_silence_cuts(a, protect=[(0, 3), (13, 16), (18, 21)])
        self.assertEqual(len(cuts), 1)                      # 2秒の無音は縮めない
        s, e, k = cuts[0]
        self.assertAlmostEqual(s, 3.3, delta=0.15)   # 発話の前後 0.3 秒は守る
        self.assertAlmostEqual(e, 12.7, delta=0.15)
        b = AC.apply_cuts(a, cuts)
        self.assertAlmostEqual(len(b) / SR, len(a) / SR - (e - s - k), delta=0.01)
        self.assertAlmostEqual(AC.removed_seconds(cuts), e - s - k, places=3)

    def test_protect_and_safety(self):
        a = synth()
        self.assertEqual(AC.find_silence_cuts(a, protect=[(3, 13)]), [])      # 発話として守られた区間は削らない
        self.assertEqual(AC.find_silence_cuts(a * 0.001), [])                 # 声が拾えていない録音は削らない
        self.assertEqual(AC.find_silence_cuts(a[:SR * 3]), [])

    def test_time_conversion_roundtrip(self):
        cuts = [[3.0, 13.0, 0.5], [20.0, 30.0, 0.5]]
        f, g = AC.orig_to_cut_time, AC.cut_to_orig_time
        self.assertAlmostEqual(f(2.0, cuts), 2.0)
        self.assertAlmostEqual(f(13.0, cuts), 3.5)
        self.assertAlmostEqual(f(14.0, cuts), 4.5)
        self.assertAlmostEqual(f(8.0, cuts), 3.25)                            # 縮めた中は境目へ
        self.assertAlmostEqual(f(31.0, cuts), 31.0 - 9.5 - 9.5)
        for t in (0.0, 2.9, 3.1, 13.2, 19.0, 30.4, 40.0):
            self.assertAlmostEqual(g(f(t, cuts), cuts), t, places=6)
        self.assertAlmostEqual(f(5.0, []), 5.0)

    def test_json(self):
        c = [[3.0, 13.0, 0.5]]
        self.assertEqual(AC.cuts_from_json(AC.cuts_to_json(c)), c)
        self.assertEqual(AC.cuts_from_json(""), [])
        self.assertEqual(AC.cuts_from_json("xx"), [])

    def test_compress_with_cuts_and_history(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            wav = d / "r.wav"
            write_wav(wav, synth())
            cuts = []
            out = AC.compress_after_final(wav, {"audio_compress": "keep"},
                                          protect=[(0, 3), (13, 16), (18, 21)], cuts_out=cuts)
            self.assertIsNotNone(out)
            self.assertEqual(len(cuts), 1)
            n = len(AC.decode_audio_float(out)) / SR
            self.assertAlmostEqual(n, len(synth()) / SR - AC.removed_seconds(cuts), delta=0.3)
            c2 = []
            AC.compress_after_final(wav, {"audio_compress": "keep", "audio_silence_cut": False}, cuts_out=c2)
            self.assertEqual(c2, [])
            # 履歴: 旧スキーマのDBが自動で列追加される・保存と読み出し
            db = d / "h.db"
            con = sqlite3.connect(str(db))
            con.executescript(
                "CREATE TABLE recordings (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
                "duration REAL NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',"
                "wav_path TEXT NOT NULL DEFAULT '', txt_path TEXT NOT NULL DEFAULT '', md_path TEXT NOT NULL DEFAULT '',"
                "text TEXT NOT NULL DEFAULT '', edited_text TEXT, edited_at TEXT, segments_json TEXT NOT NULL DEFAULT '[]');"
                "INSERT INTO recordings(created_at,text) VALUES('2026-01-01','old');")
            con.commit()
            con.close()
            h = History(db)
            self.assertEqual(h.list()[0].cuts, [])
            hid = h.add(text="a", segments=[(0, 1, "a")], duration=16, model="m", wav_path=out)
            h.set_cuts(hid, cuts)
            self.assertEqual(h.get(hid).cuts, [list(map(float, c)) for c in cuts])
            h.update_final(hid, text="b", segments=[(0, 1, "b")], duration=6, model="m")
            self.assertEqual(h.get(hid).cuts, [])
            h.close()

    def test_pipeline_compress_only_records_cuts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            wav = d / "r.wav"
            write_wav(wav, synth())
            h = History(d / "h.db")
            hid = h.add(text="a", segments=[(0, 3, "a"), (13, 16, "b"), (18, 21, "c")], duration=21,
                        model="m", wav_path=wav)
            s = {"audio_compress": "delete"}
            q = FinalizeQueue(make_run_job(lambda: s, Replacer([]), h, None))
            q.set_gate(lambda: False)                         # ゲートが閉じていても、圧縮だけの仕事は進む
            job = q.submit(wav, history_id=hid, compress_only=True)
            self.assertTrue(q.wait_idle(30))
            self.assertEqual(job.status, DONE)
            r = h.get(hid)
            self.assertTrue(r.wav_path.endswith(".opus"))
            self.assertEqual(len(r.cuts), 1)
            self.assertFalse(wav.exists())
            q.close()
            h.close()


class GateTest(unittest.TestCase):
    def test_gate_holds_pending_and_resumes(self):
        opened = {"v": False}
        ran = []
        prio = []
        q = FinalizeQueue(lambda j: ran.append(j.id) or "r", set_priority=prio.append)
        q.set_gate(lambda: opened["v"])
        self.assertEqual(prio, [True])
        j = q.submit(Path("a.wav"))
        time.sleep(0.8)
        self.assertEqual(ran, [])
        self.assertTrue(q.gated)
        self.assertEqual(finalize_progress_text(None, 0, q.waiting, running=False, gated=q.gated), GATED_TEXT)
        opened["v"] = True
        q.poke()
        self.assertTrue(q.wait_idle(5))
        self.assertEqual(ran, [j.id])
        self.assertEqual(prio, [True, False])
        self.assertFalse(q.gated)
        q.close()

    def test_running_job_continues_when_gate_closes(self):
        ev = threading.Event()
        started = threading.Event()
        state = {"open": True}
        prio = []
        q = FinalizeQueue(lambda j: (started.set(), ev.wait(5), "r")[2], set_priority=prio.append)
        q.set_gate(lambda: state["open"])
        j = q.submit(Path("a.wav"))
        self.assertTrue(started.wait(3))
        state["open"] = False
        q.poke()
        self.assertEqual(prio, [True])                      # 実行中でも優先度だけ下げる
        ev.set()
        self.assertTrue(q.wait_idle(5))
        self.assertEqual(j.status, DONE)
        q.close()


class SegLogTest(unittest.TestCase):
    def test_session_segments(self):
        ds, fs, rec, dl, ev = TD.make(texts=["こんにちは。", "ご視聴ありがとうございました", "元気ですか。"],
                                      replacer=Replacer([{"from": "元気", "to": "げんき"}]))
        ds.start()
        q, sp = TD.quiet, TD.speech
        fs.feed(np.concatenate([q(1), sp(1.0), q(2, 1), sp(1.0, 300), q(2, 2), sp(1.0, 400), q(2, 3)]))
        self.assertTrue(TD.wait_until(lambda: len(rec.calls) == 3 and ds.pending_recognitions == 0))
        segs = ds.session_segments()
        self.assertEqual([t for _a, _b, t in segs], ["こんにちは。", "げんきですか。"])   # ノイズ除外・置換辞書適用後
        self.assertTrue(all(b > a for a, b, _t in segs) and segs[0][0] < segs[1][0])
        ds.stop()


if __name__ == "__main__":
    unittest.main(verbosity=1)
