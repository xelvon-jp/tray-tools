# -*- coding: utf-8 -*-
"""清書キュー・モデル常駐キャッシュ・ホットキー・録音セッション（モック）のテスト。マイクは開かない。"""
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from finalize_queue import CANCELLED, DONE, ERROR, PENDING, FinalizeQueue
from hotkeys import (MOD_ALT, MOD_CONTROL, MOD_SHIFT, HotkeyManager, parse_hotkey)
from model_cache import ModelCache
from replacements import Replacer
from session import Session, run_mic_test


class QueueTest(unittest.TestCase):
    def test_runs_in_order_one_at_a_time(self):
        order, active, maxactive = [], [0], [0]
        lock = threading.Lock()

        def run(job):
            with lock:
                active[0] += 1
                maxactive[0] = max(maxactive[0], active[0])
            time.sleep(0.05)
            order.append(job.id)
            with lock:
                active[0] -= 1
            return "ok"
        events = []
        q = FinalizeQueue(run, on_event=lambda j: events.append((j.id, j.status)))
        jobs = [q.submit(Path(f"{i}.wav")) for i in range(4)]
        self.assertTrue(q.wait_idle(5))
        self.assertEqual(order, [1, 2, 3, 4])
        self.assertEqual(maxactive[0], 1)
        self.assertEqual([j.status for j in jobs], [DONE] * 4)
        self.assertIn((1, "running"), events)
        q.close()

    def test_waiting_count_and_submit_during_run(self):
        gate = threading.Event()

        def run(job):
            gate.wait(5)
            return 1
        q = FinalizeQueue(run)
        q.submit(Path("a.wav"))
        time.sleep(0.1)
        q.submit(Path("b.wav"))
        q.submit(Path("c.wav"))
        self.assertEqual(q.waiting, 2)          # 清書中でも次が積める
        self.assertEqual(q.current.title, "a")
        gate.set()
        self.assertTrue(q.wait_idle(5))
        q.close()

    def test_error_does_not_stop_queue(self):
        def run(job):
            if job.wav.stem == "bad":
                raise RuntimeError("boom")
            return 1
        q = FinalizeQueue(run)
        a, b = q.submit(Path("bad.wav")), q.submit(Path("good.wav"))
        self.assertTrue(q.wait_idle(5))
        self.assertEqual((a.status, a.error, b.status), (ERROR, "boom", DONE))
        q.close()

    def test_cancel_pending_and_running(self):
        started = threading.Event()

        def run(job):
            started.set()
            while not job.cancel.is_set():
                time.sleep(0.01)
            return None
        q = FinalizeQueue(run)
        a, b = q.submit(Path("a.wav")), q.submit(Path("b.wav"))
        started.wait(2)
        q.cancel(b)
        self.assertEqual(b.status, CANCELLED)
        q.cancel(a)
        self.assertTrue(q.wait_idle(5))
        self.assertEqual(a.status, CANCELLED)
        q.close()

    def test_event_callback_exception_is_swallowed(self):
        def bad(job):
            raise ValueError("ui")
        q = FinalizeQueue(lambda j: 1, on_event=bad)
        j = q.submit(Path("a.wav"))
        self.assertTrue(q.wait_idle(5))
        self.assertEqual(j.status, DONE)
        q.close()


class ModelCacheTest(unittest.TestCase):
    def test_load_once_and_reuse(self):
        n = []
        c = ModelCache(lambda name, th: (n.append(name), object())[1], idle_sec=100)
        with c.use("small", 0) as m1:
            pass
        with c.use("small", 0) as m2:
            pass
        self.assertIs(m1, m2)
        self.assertEqual(n, ["small"])
        with c.use("base", 2):
            pass
        self.assertEqual(sorted(c.loaded), ["base", "small"])

    def test_idle_release(self):
        now = [0.0]
        c = ModelCache(lambda n, t: object(), idle_sec=60, clock=lambda: now[0])
        with c.use("small", 0):
            pass
        now[0] = 59
        self.assertEqual(c.release_idle(), [])
        now[0] = 61
        self.assertEqual(c.release_idle(), ["small"])
        self.assertEqual(c.loaded, [])
        with c.use("small", 0):
            pass
        self.assertEqual(c.loads, 2)  # 解放後に使えば再ロード

    def test_in_use_not_released(self):
        now = [0.0]
        c = ModelCache(lambda n, t: object(), idle_sec=10, clock=lambda: now[0])
        with c.use("small", 0):
            now[0] = 1000
            self.assertEqual(c.release_idle(), [])
            c.release_all()
            self.assertEqual(c.loaded, ["small"])
        self.assertEqual(c.release_idle(), [])  # 使い終わった時刻から数え直し
        now[0] = 2000
        self.assertEqual(c.release_idle(), ["small"])

    def test_concurrent_use_loads_once(self):
        n = []

        def loader(name, th):
            time.sleep(0.1)
            n.append(1)
            return object()
        c = ModelCache(loader)
        ts = [threading.Thread(target=lambda: c.use("m", 0).__enter__()) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(n), 1)

    def test_loader_failure_propagates_and_counts_released(self):
        def loader(name, th):
            raise RuntimeError("no model")
        c = ModelCache(loader)
        with self.assertRaises(RuntimeError):
            with c.use("x", 0):
                pass
        self.assertEqual(c.release_idle(), [])


class HotkeyTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_hotkey("Ctrl+Alt+F9"), (MOD_CONTROL | MOD_ALT, 0x78))
        self.assertEqual(parse_hotkey("ctrl + shift + k"), (MOD_CONTROL | MOD_SHIFT, ord("K")))
        self.assertEqual(parse_hotkey("Alt+1"), (MOD_ALT, ord("1")))
        self.assertEqual(parse_hotkey("Ctrl+Space"), (MOD_CONTROL, 0x20))

    def test_parse_invalid(self):
        for bad in ("", "F9", "Ctrl", "Ctrl+Alt", "Ctrl+A+B", "Ctrl+Foo", "Ctrl+F25"):
            with self.assertRaises(ValueError, msg=bad):
                parse_hotkey(bad)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_register_and_unregister_without_pressing_keys(self):
        # キー押下は合成しない。登録→解除が成功するかだけ確認（常用されにくい Ctrl+Alt+Shift+F23/F24）
        m = HotkeyManager({"record": "Ctrl+Alt+Shift+F24", "show": "Ctrl+Alt+Shift+F23", "none": ""},
                          callback=lambda n: None)
        res = m.start()
        self.assertEqual(res, {"record": None, "show": None})
        # 二重登録（別マネージャ）は失敗として報告される＝衝突検出
        m2 = HotkeyManager({"x": "Ctrl+Alt+Shift+F24"}, callback=lambda n: None)
        res2 = m2.start()
        self.assertIsNotNone(res2["x"])
        m2.stop()
        m.stop()
        # 解除後は再登録できる
        m3 = HotkeyManager({"x": "Ctrl+Alt+Shift+F24"}, callback=lambda n: None)
        self.assertEqual(m3.start(), {"x": None})
        m3.stop()

    def test_bad_spec_reported_not_raised(self):
        m = HotkeyManager({"a": "F9"}, callback=lambda n: None)
        res = m.start()
        self.assertIn("a", res)
        self.assertIsNotNone(res["a"])
        m.stop()


class FakeStream:
    def __init__(self, samplerate, channels, dtype, device, blocksize, callback, src, speed, loop=False, owner=None):
        self.cb, self.src, self.speed, self.loop, self.owner = callback, src, speed, loop, owner
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        n = 1600
        while True:
            for i in range(0, len(self.src), n):
                if self._stop.is_set():
                    return
                blk = self.src[i:i + n].reshape(-1, 1)
                self.cb(blk, len(blk), None, None)
                time.sleep(0.1 / self.speed)
            if not self.loop:
                break
        while not self._stop.is_set():
            time.sleep(0.02)

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
        if self.owner is not None:
            self.owner.closed += 1


class FakeSD:
    """sounddevice の代わり。src を speed 倍速で流す（loop=True なら繰り返す）。実マイクは開かない。"""

    def __init__(self, src, speed=20.0, loop=False, fail=None):
        self.src, self.speed, self.loop, self.fail = src, speed, loop, fail
        self.opened = 0   # InputStream を作った回数
        self.closed = 0

    def InputStream(self, **kw):
        if self.fail is not None:
            raise self.fail
        self.opened += 1
        return FakeStream(src=self.src, speed=self.speed, loop=self.loop, owner=self, **kw)

    def query_devices(self, *a, **k):
        return []


class FakeLive:
    """認識が極端に遅い/例外を投げる逐次認識（録音が止まらないことの確認用）。"""

    def __init__(self, on_segment, mode):
        self.on_segment, self.mode, self.fed = on_segment, mode, 0

    def start(self):
        pass

    def stop(self, wait=0):
        pass

    def feed(self, x):
        self.fed += 1
        if self.mode == "raise":
            raise RuntimeError("live broken")
        if self.mode == "slow":
            time.sleep(0.05)
        if self.fed == 5:
            self.on_segment(0.2, 0.9, "不線を貼る")


class SessionTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.s = {"out_dir": self._d.name, "row_sec": 30, "silence_stop_sec": 5, "live_enabled": True}

    def tearDown(self):
        self._d.cleanup()

    def run_session(self, src, live_mode="ok", settle=0.5):
        rep = Replacer([{"from": "不線", "to": "付箋"}])
        sess = Session(self.s, rep, sd=FakeSD(src),
                       live_factory=lambda seg, st: FakeLive(seg, live_mode))
        wav = sess.start(Path(self._d.name) / "t.wav")
        t0 = time.time()
        while time.time() - t0 < len(src) / C.SR / 20 + settle:
            time.sleep(0.05)
        return sess, sess.stop(), wav

    def test_records_wav_timeline_and_applies_replacement(self):
        src = C.speechlike(6, -12)
        sess, wav, _ = self.run_session(src)
        with wave.open(str(wav)) as w:
            self.assertEqual(w.getframerate(), 16000)
            self.assertAlmostEqual(w.getnframes() / 16000, 6.0, delta=0.2)
        rows = sess.timeline.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].text, "付箋を貼る")  # 仮認識にも置換が掛かる
        self.assertEqual(rows[0].state, "draft")
        self.assertFalse(sess.is_recording)

    def test_recording_survives_broken_or_slow_recognition(self):
        src = C.speechlike(6, -12)
        for mode in ("raise", "slow"):
            sess, wav, _ = self.run_session(src, live_mode=mode, settle=1.0)
            with wave.open(str(wav)) as w:
                self.assertAlmostEqual(w.getnframes() / 16000, 6.0, delta=0.2, msg=mode)

    def test_monitor_and_silence_fed_from_recording(self):
        src = np.concatenate([C.speechlike(2, -12), np.zeros(C.SR * 8, dtype=np.int16)])
        sess, wav, _ = self.run_session(src, settle=0.3)
        self.assertTrue(sess.silence.should_stop())   # 8秒無音 ≥ 5秒
        self.assertIsNotNone(sess.monitor.stats())

    def test_no_double_start_and_restart_clears_timeline(self):
        rep = Replacer()
        src = C.speechlike(2, -12)
        sess = Session(self.s, rep, sd=FakeSD(src), live_factory=None)
        sess.start(Path(self._d.name) / "a.wav", live_enabled=False)
        with self.assertRaises(RuntimeError):
            sess.start(Path(self._d.name) / "b.wav", live_enabled=False)
        time.sleep(0.4)
        sess.stop()
        self.assertGreater(len(sess.timeline.rows()), 0)
        sess.start(Path(self._d.name) / "c.wav", live_enabled=False)
        sess.stop()
        self.assertTrue(sess.timeline.rows() == [] or sess.timeline.duration < 2)

    def test_mic_test_with_fake_device(self):
        good = C.speechlike(3, -12)
        stats, issues = run_mic_test(self.s, sd=FakeSD(good, speed=50.0), seconds=0.3,
                                     sleep=lambda s: time.sleep(s))
        self.assertEqual(issues, [])
        quiet = C.sine(3, -45)
        stats, issues = run_mic_test(self.s, sd=FakeSD(quiet, speed=50.0), seconds=0.3,
                                     sleep=lambda s: time.sleep(s))
        self.assertIn("low", {i.code for i in issues})
        stats, issues = run_mic_test(self.s, sd=FakeSD(np.zeros(C.SR * 3, dtype=np.int16), speed=50.0),
                                     seconds=0.3, sleep=lambda s: time.sleep(s))
        self.assertEqual({i.code for i in issues}, {"silent"})


if __name__ == "__main__":
    unittest.main()
