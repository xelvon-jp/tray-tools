# -*- coding: utf-8 -*-
"""舌打ち2回で確定する経路（実験）。マイクは開かない・録音しない。検出器は合成音と既存の WAV（stage0 の実測録音）を流して確かめ、
配線は偽の検出器で確かめる。"""
import unittest
from pathlib import Path

import numpy as np

import _common as C  # noqa: F401
import dialogue_click as K
import ui_dialogue_logic as L
from dialogue_segmenter import UtteranceSegmenter
from test_dialogue_controller import Rig
from test_dialogue_session import wait_until

SR = 16000
WAV = Path(__file__).resolve().parent.parent / "stage0" / "click_probe" / "clicks_20261123.wav"


def quiet(sec, db=-70.0, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(sec * SR)) * (10 ** (db / 20) * 32768)).astype(np.int16)


def click(dur_ms=40, peak_db=-8.0, seed=1):
    """高域の多い短い破裂音（高域通過した雑音×すぐ減衰する包絡）。"""
    n = int(dur_ms / 1000 * SR)
    rng = np.random.default_rng(seed)
    w = np.diff(rng.standard_normal(n + 1))
    env = np.hanning(n + 2)[1:-1] ** 0.5
    x = w * env
    x = x / np.abs(x).max() * (10 ** (peak_db / 20) * 32768)
    return x.astype(np.int16)


def tone(sec, hz=300, db=-10.0):
    t = np.arange(int(sec * SR)) / SR
    return (np.sin(2 * np.pi * hz * t) * (10 ** (db / 20) * 32768)).astype(np.int16)


def timeline(*parts):
    """数値は無音の長さ（秒）、配列はそのまま並べる。"""
    return np.concatenate([quiet(p, seed=i) if isinstance(p, (int, float)) else p for i, p in enumerate(parts)])


def run(x, bs=1024):
    hits = []
    det = K.ClickDetector(lambda: hits.append(1))
    for i in range(0, len(x), bs):
        det.feed(x[i:i + bs])
    return hits, det


class DetectorTest(unittest.TestCase):
    def test_pair_detected_once(self):
        hits, det = run(timeline(1.0, click(), 0.35, click(seed=2), 1.0))
        self.assertEqual(len(hits), 1)
        self.assertEqual(det.errors, 0)

    def test_block_size_does_not_matter(self):
        x = timeline(1.0, click(), 0.35, click(seed=2), 1.0)
        for bs in (80, 333, 1024, 5000):
            self.assertEqual(len(run(x, bs)[0]), 1, bs)

    def test_single_click_nothing(self):
        self.assertEqual(len(run(timeline(1.0, click(), 2.0))[0]), 0)

    def test_three_clicks_nothing(self):
        self.assertEqual(len(run(timeline(1.0, click(), 0.3, click(seed=2), 0.15, click(seed=3), 1.5))[0]), 0)

    def test_pair_spacing_limits(self):
        self.assertEqual(len(run(timeline(1.0, click(), 0.08, click(seed=2), 1.0))[0]), 0)   # 近すぎる（1回の余韻）
        self.assertEqual(len(run(timeline(1.0, click(), 0.2, click(seed=2), 1.0))[0]), 1)    # 速い舌打ち（陽太さんの実際の間隔）
        self.assertEqual(len(run(timeline(1.0, click(), 0.9, click(seed=2), 1.0))[0]), 0)    # 遠すぎる
        self.assertEqual(len(run(timeline(1.0, click(), 0.25, click(seed=2), 1.0))[0]), 1)
        self.assertEqual(len(run(timeline(1.0, click(), 0.55, click(seed=2), 1.0))[0]), 1)

    def test_click_conditions(self):
        self.assertEqual(len(run(timeline(1.0, click(peak_db=-20), 0.3, click(peak_db=-20, seed=2), 1.0))[0]), 0)  # 小さすぎる
        self.assertEqual(len(run(timeline(1.0, click(dur_ms=200), 0.3, click(dur_ms=200, seed=2), 1.0))[0]), 0)     # 長すぎる
        low = tone(0.04, 200)                                                                                     # 低域だけ
        self.assertEqual(len(run(timeline(1.0, low, 0.3, low, 1.0))[0]), 0)

    def test_speech_right_before_or_after_blocks(self):
        self.assertEqual(len(run(timeline(1.0, tone(0.4, 300, -25), 0.1, click(), 0.3, click(seed=2), 1.0))[0]), 0)
        self.assertEqual(len(run(timeline(1.0, click(), 0.3, click(seed=2), 0.1, tone(0.4, 300, -25), 1.0))[0]), 0)

    def test_quiet_gaps_pass_even_with_speech_far_away(self):
        x = timeline(0.5, tone(0.4, 300, -25), 0.6, click(), 0.3, click(seed=2), 0.5, tone(0.4, 300, -25), 0.5)
        self.assertEqual(len(run(x)[0]), 1)

    def test_start_of_stream_needs_full_quiet_window(self):
        self.assertEqual(len(run(timeline(0.1, click(), 0.3, click(seed=2), 1.0))[0]), 0)

    def test_refractory_two_seconds(self):
        pair = [click(), 0.3, click(seed=2)]
        x = timeline(1.0, *pair, 1.0, *pair, 3.0, *pair, 1.0)     # 2組目は約1.4秒後（不応期の中）、3組目は十分あと
        self.assertEqual(len(run(x)[0]), 2)

    def test_silence_and_noise_nothing(self):
        self.assertEqual(len(run(quiet(30, -60))[0]), 0)
        self.assertEqual(len(run(tone(10, 800, -20))[0]), 0)

    def test_garbage_input_does_not_raise(self):
        det = K.ClickDetector(lambda: 1 / 0)
        for x in (np.zeros(0), [1, 2, 3], np.zeros(10, np.float32), "x", None):
            det.feed(x)
        x = timeline(1.0, click(), 0.3, click(seed=2), 1.0)       # callback が例外でも止まらない
        for i in range(0, len(x), 1024):
            det.feed(x[i:i + 1024])
        self.assertGreaterEqual(det.errors, 1)

    def test_long_stream_buffer_stays_bounded(self):
        det = K.ClickDetector(lambda: None)
        for _ in range(60):
            det.feed(quiet(5, -70))
        self.assertLessEqual(det._n, det._cap)

    def test_samples_since_pair_start(self):
        det = K.ClickDetector(lambda: None)
        self.assertIsNone(det.samples_since_pair_start())
        x = timeline(1.0, click(), 0.35, click(seed=2), 1.0)
        for i in range(0, len(x), 1024):
            det.feed(x[i:i + 1024])
        ago = det.samples_since_pair_start()
        self.assertTrue(0.6 * SR < ago < 1.2 * SR + 16000, ago)


@unittest.skipUnless(WAV.exists(), "実測録音が無い")
class RealRecordingTest(unittest.TestCase):
    """陽太さんの実録音（0〜10秒 静か／10〜45秒 1回ずつ／45〜80秒 2回続け×10組／80〜110秒 普通の発話／110〜125秒 バラバラ）。"""

    @classmethod
    def setUpClass(cls):
        import av
        c = av.open(str(WAV))
        r = av.AudioResampler(format="flt", layout="mono", rate=SR)
        xs = []
        for f in c.decode(audio=0):
            for g in r.resample(f):
                xs.append(g.to_ndarray().reshape(-1))
        c.close()
        a = np.concatenate(xs)
        cls.x = (np.clip(a, -1, 1) * 32767).astype(np.int16)
        cls.times = []
        n = [0]
        det = K.ClickDetector(lambda: cls.times.append(n[0] / SR))
        for i in range(0, len(cls.x), 1024):
            n[0] = min(len(cls.x), i + 1024)
            det.feed(cls.x[i:i + 1024])

    def count(self, lo, hi):
        return sum(1 for t in self.times if lo <= t < hi)

    def test_pairs_detected(self):
        self.assertGreaterEqual(self.count(45, 82), 8)       # 10組中（実測は10組）
        self.assertLessEqual(self.count(45, 82), 10)

    def test_no_false_detections_elsewhere(self):
        self.assertEqual(self.count(0, 45), 0)
        self.assertEqual(self.count(82, 200), 0)


class SegmenterDropTest(unittest.TestCase):
    def seg(self):
        self.utts = []
        return UtteranceSegmenter(gap_sec=1.2, abs_dbfs=-50, min_voiced_sec=0.3, loud_dbfs=-30, on_utterance=self.utts.append)

    def feed(self, seg, x):
        for i in range(0, len(x), 1024):
            seg.feed(x[i:i + 1024])

    def test_drop_recent_removes_trigger_only_utterance(self):
        seg = self.seg()
        burst = tone(0.4, 2500, -6)
        self.feed(seg, timeline(2.0, burst, 0.1, burst, 0.3))
        self.assertTrue(seg.in_speech)
        self.assertTrue(seg.drop_recent(int(1.2 * SR)))
        self.assertFalse(seg.in_speech)
        seg.flush()
        self.feed(seg, quiet(3.0))
        self.assertEqual(self.utts, [])

    def test_drop_recent_keeps_earlier_speech(self):
        seg = self.seg()
        self.feed(seg, timeline(2.0, tone(1.0, 300, -12), 0.4, tone(0.3, 2500, -6), 0.35))
        self.assertTrue(seg.drop_recent(int(0.9 * SR)))
        self.assertTrue(seg.in_speech)
        seg.flush()
        self.assertEqual(len(self.utts), 1)
        self.assertLess(self.utts[0].end_sec, 4.3)

    def test_drop_recent_when_idle_is_harmless(self):
        seg = self.seg()
        self.feed(seg, quiet(3.0))
        seg.drop_recent(SR)
        self.feed(seg, tone(1.0, 300, -12))
        seg.flush()
        self.assertEqual(len(self.utts), 1)


class FakeDetector:
    def __init__(self, cb):
        self.cb, self.fed, self.ago = cb, 0, 12000

    def feed(self, x):
        self.fed += 1
        if self.fed == 3:
            self.cb()

    def samples_since_pair_start(self):
        return self.ago


class ClickCommitTest(unittest.TestCase):
    def make(self, enabled=True, factory=None):
        self.logs, self.calls = [], []
        self.now = [100.0]
        return K.ClickCommit(lambda: self.calls.append(1), enabled, min_interval=0.3, clock=lambda: self.now[0],
                             detector_factory=factory or (lambda cb: FakeDetector(cb)),
                             log=lambda ev, **f: self.logs.append((ev, f)))

    def test_disabled_starts_nothing(self):
        made = []
        c = self.make(False, lambda cb: made.append(1))
        self.assertFalse(c.start().ok)
        self.assertEqual((made, c.state, c.enabled), ([], "stopped", False))

    def test_detection_notifies_after_drop(self):
        c = self.make()
        order = []
        self.assertTrue(c.start().ok)
        self.assertTrue(c.active)
        c.callback = lambda: order.append("press")
        for _ in range(3):
            c.feed(np.zeros(100, np.int16), lambda ago: order.append(("drop", ago)))
        self.assertTrue(wait_until(lambda: "press" in order))
        self.assertEqual(order, [("drop", 12000), "press"])
        c.stop()
        self.assertEqual(c.state, "stopped")
        c.feed(np.zeros(100, np.int16))           # 停止後は何もしない

    def test_rapid_detections_thinned(self):
        c = self.make()
        c.start()
        c._on_press()
        self.now[0] += 0.1
        c._on_press()
        self.now[0] += 0.5
        c._on_press()
        self.assertEqual((len(self.calls), c.dropped), (2, 1))
        c.stop()

    def test_factory_exception_contained(self):
        def bad(cb):
            raise RuntimeError("boom")
        c = self.make(True, bad)
        r = c.start()
        self.assertFalse(r.ok)
        self.assertIn("boom", r.reason)
        self.assertEqual(c.state, "failed")

    def test_detector_and_drop_exceptions_contained(self):
        class Bad(FakeDetector):
            def feed(self, x):
                raise RuntimeError("x")
        c = self.make(True, lambda cb: Bad(cb))
        c.start()
        c.feed(np.zeros(10, np.int16))
        c.stop()
        c = self.make()
        c.start()
        for _ in range(3):
            c.feed(np.zeros(10, np.int16), lambda ago: 1 / 0)
        self.assertTrue(wait_until(lambda: len(self.calls) == 1))
        c.stop()

    def test_callback_error_does_not_propagate(self):
        c = K.ClickCommit(lambda: 1 / 0, True, detector_factory=lambda cb: FakeDetector(cb), log=lambda *a, **k: None)
        c.start()
        c._on_press()
        c.stop()

    def test_status_line_and_notice(self):
        self.assertIn("無効", L.click_status_line({}, False))
        self.assertIn("受付中", L.click_status_line({"state": "active", "presses": 1}, True))
        self.assertIn("なぜか", L.click_status_line({"state": "failed", "reason": "なぜか"}, True))
        text, _ = L.press_notice("click", L.PRESS_COMMIT)
        self.assertIn("舌打ちを検出しました", text)

    def test_default_setting_is_off(self):
        import settings as S
        self.assertIs(S.dialogue_config({})["click_commit"], False)


class ClickControllerTest(unittest.TestCase):
    def rig(self, click=True, fail=False, **kw):
        r = Rig(dialogue={"click_commit": click, **kw})
        made = []

        def factory(cb):
            def det(cbk):
                if fail:
                    raise RuntimeError("検出器が作れない")
                return FakeDetector(cbk)
            c = K.ClickCommit(cb, True, min_interval=0.0, detector_factory=det, log=lambda *a, **k: None)
            made.append(c)
            return c

        r.ctl._click_factory = factory
        r.made = made
        return r

    def test_start_stop_follow_session(self):
        r = self.rig()
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.click_state(), "active")
            self.assertIsNotNone(r.ctl.ds.audio_tap)
            r.ctl.stop()
            self.assertEqual(r.ctl.click_state(), "stopped")
            self.assertIsNone(r.ctl.ds.audio_tap)
        finally:
            r.close()

    def test_empty_setting_creates_nothing(self):
        r = self.rig(click=False)
        try:
            r.ctl.start()
            self.assertEqual(r.made, [])
            self.assertIsNone(r.ctl.click)
            self.assertIsNone(r.ctl.ds.audio_tap)
            self.assertEqual(r.ctl.click_diagnostics()["state"], "stopped")
            r.ctl.stop()
        finally:
            r.close()

    def test_detection_commits_and_delivers(self):
        r = self.rig()
        try:
            r.ctl.start()
            r.say()
            r.made[0]._det.fed = 2           # 次の音声ブロックで検出
            r.fs.feed(quiet(0.2))
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.client.delivered[0], ("相談窓", "こんにちは。元気ですか。"))
            self.assertIn("pressed", r.names())
            r.ctl.stop()
        finally:
            r.close()

    def test_failure_does_not_affect_other_paths(self):
        r = self.rig(fail=True)
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.click_state(), "failed")
            self.assertIn("検出器", r.ctl.click_reason)
            self.assertIn("click_failed", r.names())
            self.assertEqual(r.headset.state, "active")
            self.assertTrue(r.ctl.active)
            r.say()
            r.headset.press()
            self.assertTrue(wait_until(lambda: r.client.delivered))
            r.ctl.stop()
        finally:
            r.close()

    def test_real_detector_end_to_end(self):
        """本物の検出器を、セッションの音声の入口（偽の録音）から流す: 発話→舌打ち2回→届く。マイクは使わない。"""
        r = Rig(dialogue={"click_commit": True})
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.click_state(), "active")
            r.say()
            r.fs.feed(timeline(1.0, click(), 0.35, click(seed=2), 1.0))
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.client.delivered[0], ("相談窓", "こんにちは。元気ですか。"))
            r.ctl.stop()
        finally:
            r.close()

    def test_shutdown_stops_click(self):
        r = self.rig()
        try:
            r.ctl.start()
            c = r.made[0]
            r.ctl.shutdown()
            self.assertEqual(c.state, "stopped")
        finally:
            r.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
