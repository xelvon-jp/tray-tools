# -*- coding: utf-8 -*-
import unittest

import numpy as np

import _common as C
from mic_check import MicMonitor, diagnose, measure
from silence import SilenceWatcher


def codes(issues):
    return {i.code for i in issues}


class MicDiagnoseTest(unittest.TestCase):
    def test_good_signal_has_no_issue(self):
        x = C.speechlike(4, -12)
        self.assertEqual(diagnose(measure(x)), [])

    def test_too_quiet(self):
        x = np.concatenate([C.sine(2, -40), C.noise(1, -75)])
        self.assertIn("low", codes(diagnose(measure(x))))

    def test_quiet_boundary(self):
        self.assertIn("low", codes(diagnose(measure(C.sine(3, -31)))))
        self.assertNotIn("low", codes(diagnose(measure(C.sine(3, -29)))))

    def test_clipping(self):
        x = np.clip(C.sine(2, -3, 150) * 4.0, -32768, 32767).astype(np.int16)  # 大きく歪ませる
        iss = diagnose(measure(x))
        self.assertIn("clipping", codes(iss))
        self.assertNotIn("low", codes(iss))

    def test_few_clipped_samples_not_flagged(self):
        x = C.sine(3, -12)
        x[100] = 32767
        x[200] = -32768
        self.assertNotIn("clipping", codes(diagnose(measure(x))))

    def test_noisy_floor(self):
        x = np.concatenate([C.noise(3, -38, 1), C.sine(1, -10)])
        self.assertIn("noisy", codes(diagnose(measure(x))))

    def test_dead_mic_only_silent(self):
        iss = diagnose(measure(np.zeros(C.SR * 3, dtype=np.int16)))
        self.assertEqual(codes(iss), {"silent"})
        self.assertEqual(iss[0].severity, "error")
        iss = diagnose(measure(C.noise(3, -90)))
        self.assertEqual(codes(iss), {"silent"})

    def test_custom_thresholds(self):
        x = C.sine(3, -25)
        self.assertEqual(diagnose(measure(x), {"mic_low_peak_dbfs": -20.0}, True)[0].code, "low")
        self.assertNotIn("low", codes(diagnose(measure(x), {"mic_low_peak_dbfs": -30.0})))

    def test_messages_have_advice(self):
        for i in diagnose(measure(C.sine(3, -45))):
            self.assertTrue(i.message and i.advice)

    def test_monitor_thinking_pause_is_not_low(self):
        # 考え中の静かな区間（発話の動きがない）で誤警告しない
        m = MicMonitor(window_sec=8)
        m.feed(C.noise(9, -65))
        self.assertEqual(m.issues(), [])

    def test_monitor_quiet_speech_is_low(self):
        m = MicMonitor(window_sec=8)
        m.feed(np.concatenate([C.noise(4, -75), C.sine(4, -38)]))
        self.assertIn("low", codes(m.issues()))

    def test_monitor_needs_one_second(self):
        m = MicMonitor()
        m.feed(C.sine(0.5, -50))
        self.assertIsNone(m.stats())

    def test_monitor_window_rolls(self):
        m = MicMonitor(window_sec=3)
        m.feed(np.zeros(C.SR * 5, dtype=np.int16))
        self.assertIn("silent", codes(m.issues()))
        m.feed(C.speechlike(4, -10))  # 窓が良い音で入れ替わる
        self.assertEqual(m.issues(), [])

    def test_monitor_odd_block_sizes(self):
        m = MicMonitor(window_sec=5)
        x = C.speechlike(5, -10)
        for i in range(0, len(x), 1234):
            m.feed(x[i:i + 1234])
        self.assertEqual(m.issues(), [])


class SilenceWatcherTest(unittest.TestCase):
    def test_stops_after_limit(self):
        w = SilenceWatcher(limit_sec=10)
        w.feed(C.speechlike(3, -15))
        w.feed(C.noise(9, -70))
        self.assertFalse(w.should_stop())
        w.feed(C.noise(2, -70))
        self.assertTrue(w.should_stop())

    def test_speech_resets(self):
        w = SilenceWatcher(limit_sec=10)
        w.feed(C.noise(8, -70))
        w.feed(C.sine(1, -15))
        w.feed(C.noise(8, -70))
        self.assertFalse(w.should_stop())

    def test_disabled(self):
        w = SilenceWatcher(limit_sec=0)
        w.feed(C.noise(60, -70))
        self.assertFalse(w.should_stop())

    def test_steady_noise_counts_as_silence(self):
        # 一定のノイズ(-40dBFS)だけでも「発話なし」。床との差で判定する
        w = SilenceWatcher(limit_sec=10)
        w.feed(C.noise(15, -40))
        self.assertTrue(w.should_stop())


if __name__ == "__main__":
    unittest.main()
