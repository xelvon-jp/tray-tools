# -*- coding: utf-8 -*-
"""AI対話モードの区切り器（合成波形）。マイクは開かない。"""
import unittest

import numpy as np

import _common as C
from dialogue_segmenter import FRAME_SEC, UtteranceSegmenter

SR = C.SR


def quiet(sec, seed=0):
    return C.noise(sec, -70, seed)


def feed_chunks(seg, x, chunk=1234):   # フレーム境界と揃わない大きさで流す
    out = []
    for i in range(0, len(x), chunk):
        out += seg.feed(x[i:i + chunk])
    return out


class SegmenterTest(unittest.TestCase):
    def test_silence_tone_silence_gives_one_utterance(self):
        seg = UtteranceSegmenter(gap_sec=1.2)
        x = np.concatenate([quiet(2.0), C.sine(1.5, -12), quiet(3.0, 1)])
        out = feed_chunks(seg, x)
        self.assertEqual(len(out), 1)
        u = out[0]
        self.assertAlmostEqual(u.voiced_sec, 1.5, delta=0.15)
        self.assertFalse(u.forced)
        # 先頭に preroll（0.3 秒）、末尾に tail（0.3 秒）だけ付く
        self.assertAlmostEqual(u.seconds, 0.3 + 1.5 + 0.3, delta=0.2)
        self.assertAlmostEqual(u.start_sec, 2.0 - 0.3, delta=0.15)
        self.assertEqual(u.audio.dtype, np.int16)

    def test_split_only_after_gap(self):
        seg = UtteranceSegmenter(gap_sec=1.2)
        # 1.0 秒の間（ギャップ未満）は続きとして1発話、1.5 秒の間は2発話
        x = np.concatenate([quiet(1.0), C.sine(1, -12), quiet(1.0, 1), C.sine(1, -12, 300), quiet(1.5, 2),
                            C.sine(1, -12, 400), quiet(2.0, 3)])
        out = feed_chunks(seg, x)
        self.assertEqual(len(out), 2)
        self.assertGreater(out[0].voiced_sec, 1.8)
        self.assertAlmostEqual(out[1].voiced_sec, 1.0, delta=0.15)
        self.assertLess(out[0].end_sec, out[1].start_sec + 0.5)

    def test_short_blip_is_dropped(self):
        seg = UtteranceSegmenter(gap_sec=1.2, min_voiced_sec=0.3)
        out = feed_chunks(seg, np.concatenate([quiet(1), C.sine(0.1, -12), quiet(3, 1)]))
        self.assertEqual(out, [])
        out = feed_chunks(seg, np.concatenate([C.sine(0.4, -12), quiet(3, 2)]))
        self.assertEqual(len(out), 1)

    def test_pure_quiet_gives_nothing_and_counts_silence(self):
        seg = UtteranceSegmenter()
        self.assertEqual(feed_chunks(seg, quiet(10)), [])
        self.assertFalse(seg.in_speech)
        self.assertAlmostEqual(seg.silence_sec, 10.0, delta=0.3)

    def test_preroll_keeps_the_onset(self):
        seg = UtteranceSegmenter(gap_sec=1.2, preroll_sec=0.3)
        pre = C.sine(0.3, -58, 500)    # 発話前の小さい音（声とは判定されない小ささ）
        x = np.concatenate([quiet(2), pre, C.sine(1, -12), quiet(2, 1)])
        u = feed_chunks(seg, x)[0]
        self.assertAlmostEqual(u.start_sec, 2.0, delta=0.2)   # 声の始まり(2.3秒)より 0.3 秒前から入っている
        n_pre = int(0.2 * SR)
        self.assertGreater(float(np.abs(u.audio[:n_pre].astype(np.int32)).max()), 0)

    def test_no_preroll(self):
        seg = UtteranceSegmenter(gap_sec=1.2, preroll_sec=0.0, tail_sec=0.0)
        out = feed_chunks(seg, np.concatenate([quiet(2), C.sine(1, -12), quiet(2, 1)]))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].seconds, 1.0, delta=0.15)

    def test_forced_split_at_max_length(self):
        seg = UtteranceSegmenter(gap_sec=1.2, max_utt_sec=5.0)
        # 12 秒しゃべり続ける（途中 3 秒目付近に短い弱い区間がある）
        x = np.concatenate([quiet(1), C.sine(4.0, -12), C.sine(0.3, -40, 300), C.sine(8, -12), quiet(3, 1)])
        out = feed_chunks(seg, x)
        self.assertGreaterEqual(len(out), 3)
        self.assertTrue(out[0].forced)
        for u in out:
            self.assertLessEqual(u.seconds, 5.0 + 2 * FRAME_SEC)
        self.assertFalse(out[-1].forced)
        # 発話は重ならず、順序どおり
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a.end_sec, b.start_sec + 1e-6)
        # 強制で区切っても、声のある時間はほぼ失われない
        self.assertGreater(sum(u.voiced_sec for u in out), 12.0 - 0.8)

    def test_forced_split_picks_quiet_frame(self):
        seg = UtteranceSegmenter(gap_sec=1.2, max_utt_sec=5.0)
        # 4.2〜4.3 秒目（最長の手前 0.7 秒）にだけ極小の音の谷
        x = np.concatenate([C.sine(4.2, -12), C.sine(0.1, -60), C.sine(5.0, -12), quiet(2, 1)])
        out = feed_chunks(seg, x)
        self.assertTrue(out[0].forced)
        self.assertAlmostEqual(out[0].end_sec, 4.3, delta=0.15)

    def test_flush_emits_current_speech(self):
        seg = UtteranceSegmenter(gap_sec=1.2)
        self.assertEqual(feed_chunks(seg, np.concatenate([quiet(1), C.sine(1.0, -12)])), [])
        self.assertTrue(seg.in_speech)
        out = seg.flush()
        self.assertEqual(len(out), 1)
        self.assertFalse(seg.in_speech)
        self.assertEqual(seg.flush(), [])

    def test_callback_and_exception_safety(self):
        got = []

        def cb(u):
            got.append(u)
            raise RuntimeError("boom")
        seg = UtteranceSegmenter(on_utterance=cb)
        out = feed_chunks(seg, np.concatenate([quiet(1), C.sine(1, -12), quiet(2, 1)]))
        self.assertEqual(len(out), 1)
        self.assertEqual(len(got), 1)
        self.assertEqual(seg.errors, 1)
        # 変な入力でも例外を出さない
        self.assertEqual(seg.feed(np.zeros(0, dtype=np.int16)), [])
        self.assertEqual(seg.feed(np.array([0.1, 0.2], dtype=np.float32)), [])

    def test_discard_pending_keeps_position(self):
        seg = UtteranceSegmenter()
        feed_chunks(seg, np.concatenate([quiet(1), C.sine(1, -12)]))
        pos = seg.position_sec
        seg.discard_pending()
        self.assertFalse(seg.in_speech)
        self.assertEqual(seg.position_sec, pos)
        out = feed_chunks(seg, np.concatenate([quiet(2, 3), C.sine(1, -12), quiet(2, 4)]))
        self.assertEqual(len(out), 1)
        self.assertGreater(out[0].start_sec, pos)

    def test_total_audio_positions_match_stream(self):
        seg = UtteranceSegmenter(gap_sec=1.2)
        x = np.concatenate([quiet(1), C.sine(1, -12), quiet(2, 1), C.sine(1, -12, 300), quiet(2, 2)])
        out = feed_chunks(seg, x)
        for u in out:   # 区切った音声は、元の音声の同じ位置の切り出しと一致する
            a, b = int(round(u.start_sec * SR)), int(round(u.start_sec * SR)) + len(u.audio)
            self.assertTrue(np.array_equal(x[a:b], u.audio))

    def test_set_loud_dbfs_takes_effect_for_next_utterance(self):
        seg = UtteranceSegmenter(gap_sec=1.2, loud_dbfs=-30.0)
        # -40 dB の声は -30 のラインでは捨てられる
        out = feed_chunks(seg, np.concatenate([quiet(1), C.sine(1.5, -40), quiet(3, 1)]))
        self.assertEqual(len(out), 0)
        seg.set_loud_dbfs(-50.0)
        self.assertEqual(seg.loud_dbfs, -50.0)
        out = feed_chunks(seg, np.concatenate([C.sine(1.5, -40, 200), quiet(3, 2)]))
        self.assertEqual(len(out), 1)
        seg.set_loud_dbfs(None)
        self.assertIsNone(seg.loud_dbfs)


if __name__ == "__main__":
    unittest.main()
