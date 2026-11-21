# -*- coding: utf-8 -*-
"""フェーズ1.5 のコア: 待機中の発話検出・すき間による改行・タイムライン（一時停止/無音の畳み込み）・
待機中モニターと録音のストリーム引き継ぎ・一時停止/再開・窓位置のクランプ・再生デバイス選択。
マイクは開かない（sounddevice は差し替え）。音は出さない。
"""
import sys
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from geometry_util import clamp_geometry, parse_geometry
from idle_speech import (IdleSpeechWatcher, frame_voiced, should_prompt, speech_run_seconds)
from paragraph import ends_sentence, format_for_output, format_paragraphs, plain_text
from playback_devices import (DEFAULT_LABEL, OutputDevice, combo_entries, default_device, resolve_output)
from recorder import Recorder
from replacements import Replacer
from session import MIC_BUSY_MESSAGE, Session
from timeline import AUDIO, DRAFT, FINAL, PAUSE, SILENCE, WAVE, Timeline

T, F = True, False


# ------------------------------------------------------------------ 待機中の発話検出
class IdleSpeechPureTest(unittest.TestCase):
    def test_run_basic(self):
        self.assertEqual(speech_run_seconds([]), 0.0)
        self.assertEqual(speech_run_seconds([F] * 30), 0.0)
        self.assertAlmostEqual(speech_run_seconds([T] * 20), 2.0)
        self.assertAlmostEqual(speech_run_seconds([F] * 50 + [T] * 20), 2.0)

    def test_short_breath_gap_is_bridged_long_gap_cuts(self):
        self.assertAlmostEqual(speech_run_seconds([T] * 5 + [F] * 4 + [T] * 5), 1.4)
        # 0.7 秒（7 フレーム）の沈黙で連続は切れる → 末尾の 0.5 秒だけ
        self.assertAlmostEqual(speech_run_seconds([T] * 20 + [F] * 7 + [T] * 5), 0.5)
        # ちょうど 0.6 秒（6 フレーム）は息継ぎとして許す
        self.assertAlmostEqual(speech_run_seconds([T] * 10 + [F] * 6 + [T] * 10), 2.6)

    def test_tail_silence_ends_the_run(self):
        self.assertAlmostEqual(speech_run_seconds([T] * 20 + [F] * 6), 2.0)   # 0.6 秒まではまだ話している扱い
        self.assertEqual(speech_run_seconds([T] * 20 + [F] * 7), 0.0)          # 0.7 秒の沈黙: 話し終わった

    def test_low_density_is_not_speech(self):
        sparse = [T, F, F, F, T, F, F, F, T, F, F, F, T]
        self.assertEqual(speech_run_seconds(sparse), 0.0)

    def test_frame_voiced(self):
        self.assertTrue(frame_voiced(-25, -12, -70))        # はっきりした声
        self.assertFalse(frame_voiced(-60, -50, -70))       # 絶対値が小さすぎる
        self.assertFalse(frame_voiced(-35, -3, -70))        # 尖った音（クリック）
        self.assertFalse(frame_voiced(-35, -28, -33))       # 床とほぼ同じ（一定の雑音）
        self.assertTrue(frame_voiced(-20, -10, -20))        # 十分大きい音は床が未確定でも発話とみなす

    def test_should_prompt_table(self):
        kw = dict(enabled=True, min_speech_sec=1.5, cooldown_sec=60.0, window_active=True, recording=False)
        self.assertTrue(should_prompt(1.5, 100.0, None, **kw))
        self.assertFalse(should_prompt(1.49, 100.0, None, **kw))
        self.assertFalse(should_prompt(3.0, 100.0, None, **{**kw, "enabled": False}))
        self.assertFalse(should_prompt(3.0, 100.0, None, **{**kw, "recording": True}))
        self.assertFalse(should_prompt(3.0, 100.0, None, **{**kw, "window_active": False}))
        # クールダウンの境界
        self.assertFalse(should_prompt(3.0, 159.9, 100.0, **kw))
        self.assertTrue(should_prompt(3.0, 160.0, 100.0, **kw))
        self.assertTrue(should_prompt(3.0, 100.0, 0.0, **{**kw, "cooldown_sec": 5.0}))


class IdleSpeechWaveformTest(unittest.TestCase):
    def feed(self, w, x, block=1600):
        for i in range(0, len(x), block):
            w.feed(x[i:i + block])

    def test_speechlike_detected(self):
        w = IdleSpeechWatcher()
        self.feed(w, C.noise(3, -70, 1))
        self.assertEqual(w.run_sec, 0.0)
        self.feed(w, C.speechlike(4, -12))
        self.assertGreaterEqual(w.run_sec, 1.5)

    def test_steady_noise_and_quiet_are_not_speech(self):
        for db in (-80, -60, -50):
            w = IdleSpeechWatcher()
            self.feed(w, C.noise(10, db, 2))
            self.assertEqual(w.run_sec, 0.0, msg=f"{db}dB")
        w = IdleSpeechWatcher()
        self.feed(w, C.noise(15, -35, 3))   # 大きめだが一定の雑音（床が追従する）
        self.assertLess(w.run_sec, 1.5)

    def test_short_burst_is_not_enough_long_burst_is(self):
        w = IdleSpeechWatcher()
        self.feed(w, np.concatenate([C.noise(3, -70, 4), C.sine(1.0, -15), C.noise(1, -70, 5)]))
        self.assertLess(w.run_sec, 1.5)
        w2 = IdleSpeechWatcher()
        self.feed(w2, np.concatenate([C.noise(3, -70, 4), C.sine(2.2, -15)]))
        self.assertGreaterEqual(w2.run_sec, 1.5)

    def test_stops_counting_after_speech_ends(self):
        w = IdleSpeechWatcher()
        self.feed(w, np.concatenate([C.noise(3, -70, 4), C.sine(2.2, -15), C.noise(2, -70, 6)]))
        self.assertEqual(w.run_sec, 0.0)

    def test_reset_run_keeps_floor(self):
        w = IdleSpeechWatcher()
        self.feed(w, np.concatenate([C.noise(3, -70, 4), C.sine(2.2, -15)]))
        self.assertGreater(w.run_sec, 0)
        w.reset_run()
        self.assertEqual(w.run_sec, 0.0)


# ------------------------------------------------------------------ 清書の改行
def S(*rows):
    return [(a, b, t) for a, b, t in rows]


class ParagraphTest(unittest.TestCase):
    def test_small_gap_does_nothing(self):
        self.assertEqual(format_paragraphs(S((0, 2, "今日は晴れ。"), (2.5, 4, "散歩した。"))), "今日は晴れ。散歩した。")
        self.assertEqual(format_paragraphs(S((0, 2, "今日は晴れ。"), (3.99, 5, "散歩した。"))), "今日は晴れ。散歩した。")

    def test_gap_2_to_3_breaks_only_after_sentence_end(self):
        # ちょうど 2.0 は改行（以上）、1.99 は改行しない
        self.assertEqual(format_paragraphs(S((0, 2, "晴れ。"), (4.0, 5, "歩いた。"))), "晴れ。\n歩いた。")
        self.assertEqual(format_paragraphs(S((0, 2, "晴れ。"), (3.99, 5, "歩いた。"))), "晴れ。歩いた。")
        # 2.99 でも句点があれば改行、句点を補わない
        self.assertEqual(format_paragraphs(S((0, 2, "晴れ。"), (4.99, 6, "歩いた"))), "晴れ。\n歩いた")
        # 句点がなければ改行しない（考え中の沈黙の可能性）
        self.assertEqual(format_paragraphs(S((0, 2, "えーと"), (4.5, 6, "次に"))), "えーと次に")
        self.assertEqual(format_paragraphs(S((0, 2, "えーと、"), (4.9, 6, "次に"))), "えーと、次に")

    def test_gap_3_or_more_always_breaks_and_adds_period(self):
        self.assertEqual(format_paragraphs(S((0, 2, "えーと"), (5.0, 6, "次に"))), "えーと。\n次に")   # ちょうど 3.0
        self.assertEqual(format_paragraphs(S((0, 2, "えーと"), (4.99, 6, "次に"))), "えーと次に")      # 2.99 は補わない
        self.assertEqual(format_paragraphs(S((0, 2, "晴れ。"), (10, 11, "次"))), "晴れ。\n次")          # 句点があれば補わない
        self.assertEqual(format_paragraphs(S((0, 2, "本当？"), (10, 11, "次"))), "本当？\n次")
        self.assertEqual(format_paragraphs(S((0, 2, "すごい！"), (10, 11, "次"))), "すごい！\n次")
        self.assertEqual(format_paragraphs(S((0, 2, "will?"), (10, 11, "次"))), "will?\n次")
        self.assertEqual(format_paragraphs(S((0, 2, "えーと、"), (10, 11, "次"))), "えーと。\n次")      # 末尾の読点は句点に

    def test_closing_brackets_count_as_sentence_end(self):
        self.assertTrue(ends_sentence("「はい。」"))
        self.assertTrue(ends_sentence("（できた！） "))
        self.assertFalse(ends_sentence("「はい」"))
        self.assertEqual(format_paragraphs(S((0, 2, "「はい。」"), (5, 6, "次"))), "「はい。」\n次")
        self.assertEqual(format_paragraphs(S((0, 2, "「はい」"), (6, 7, "次"))), "「はい」。\n次")

    def test_empty_segments_ignored_and_gap_measured_from_last_real_one(self):
        segs = S((0, 1, "あ。"), (1, 2, ""), (2, 3, "   "), (6, 7, "い"))
        self.assertEqual(format_paragraphs(segs), "あ。\nい")           # 1→6 の 5 秒で改行。最後の行は補わない
        self.assertEqual(format_paragraphs([]), "")
        self.assertEqual(format_paragraphs(S((0, 1, ""), (2, 3, " "))), "")
        self.assertEqual(format_paragraphs(S((0, 1, ""), (2, 3, "あ"))), "あ")

    def test_ascii_neighbours_get_a_space(self):
        self.assertEqual(format_paragraphs(S((0, 1, "API"), (1.2, 2, "is good"))), "API is good")
        self.assertEqual(format_paragraphs(S((0, 1, "API"), (1.2, 2, "は良い"))), "APIは良い")

    def test_overlap_and_garbage_do_not_crash(self):
        self.assertEqual(format_paragraphs(S((0, 5, "あ。"), (3, 6, "い。"))), "あ。い。")  # 重なり: すき間は負
        self.assertEqual(format_paragraphs([(0, 1, "あ。"), ("x", 2, "?"), (None, None, None), (5, 6, "い")]), "あ。\nい")
        self.assertEqual(format_paragraphs([(0, 1, None), (2, 3, "あ")]), "あ")

    def test_swapped_thresholds_are_repaired(self):
        # 強制のほうが小さい設定でも、強制を通常のしきい値まで引き上げて矛盾させない
        self.assertEqual(format_paragraphs(S((0, 1, "あ"), (2.5, 3, "い")), gap_sec=3.0, force_gap_sec=2.0), "あい")
        self.assertEqual(format_paragraphs(S((0, 1, "あ"), (4.5, 5, "い")), gap_sec=3.0, force_gap_sec=2.0), "あ。\nい")

    def test_settings_switch(self):
        segs = S((0, 1, "あ。"), (1.5, 2, "い。"), (6, 7, "う"))
        self.assertEqual(format_for_output(segs, {"paragraph_enabled": True}), "あ。い。\nう")
        self.assertEqual(format_for_output(segs, {"paragraph_enabled": False}), "あ。\nい。\nう")
        self.assertEqual(format_for_output(segs, {"paragraph_gap_sec": 0.5, "paragraph_force_gap_sec": 3.0}),
                         "あ。\nい。\nう")
        self.assertEqual(plain_text(segs), "あ。\nい。\nう")

    def test_original_segments_untouched(self):
        segs = S((0, 1, "えーと"), (9, 10, "次"))
        before = [tuple(s) for s in segs]
        format_paragraphs(segs)
        self.assertEqual([tuple(s) for s in segs], before)


class OutputIntegrationTest(unittest.TestCase):
    """transcribe_wav → txt/md は整形、履歴は元の区間（無加工）。"""

    def test_formatter_applies_to_outputs_but_history_keeps_raw(self):
        from finalize_queue import Job
        from history import History
        from model_cache import ModelCache
        from pipeline import make_run_job
        from transcriber import transcribe_wav

        class Seg:
            def __init__(self, a, b, t):
                self.start, self.end, self.text = a, b, t

        class FakeModel:
            def transcribe(self, audio, **kw):
                return iter([Seg(0.0, 2.0, "今日は晴れです。"), Seg(2.5, 4.0, "散歩に行きます"),
                             Seg(8.0, 9.0, "帰りました")]), None

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            d = Path(d)
            wavp = d / "a.wav"
            with wave.open(str(wavp), "wb") as f:
                f.setnchannels(1), f.setsampwidth(2), f.setframerate(16000)
                f.writeframes(C.sine(1, -20).tobytes())
            settings = {"out_dir": str(d / "out"), "paragraph_enabled": True, "paragraph_gap_sec": 2.0,
                        "paragraph_force_gap_sec": 3.0, "final_model": "x", "final_cpu_threads": 0}
            hist = History(d / "h.db")
            cache = ModelCache(lambda n, t: FakeModel())
            run = make_run_job(lambda: settings, Replacer(), hist, cache, transcribe=transcribe_wav)
            job = Job(1, wavp)
            r = run(job)
            # 2.0→2.5 は 0.5 秒（つなげる）、4.0→8.0 は 4 秒（句点がないので「。」を補って改行）
            self.assertEqual(r.text, "今日は晴れです。散歩に行きます。\n帰りました")
            txt = Path(r.txt_path).read_text(encoding="utf-8")
            self.assertIn("今日は晴れです。散歩に行きます。", txt)
            md = Path(r.md_path).read_text(encoding="utf-8")
            self.assertIn("[00:02] 散歩に行きます", md)      # タイムスタンプ付きは区間ごとのまま
            rec = hist.get(job.history_id)
            self.assertEqual(rec.text, "今日は晴れです。\n散歩に行きます\n帰りました")   # 履歴は整形しない
            self.assertEqual([tuple(s) for s in rec.segments],
                             [(0.0, 2.0, "今日は晴れです。"), (2.5, 4.0, "散歩に行きます"), (8.0, 9.0, "帰りました")])
            # (履歴は最後に閉じる)
            # OFF なら従来どおり 1 区間 1 行
            settings["paragraph_enabled"] = False
            r2 = run(Job(2, wavp))
            self.assertEqual(r2.text, "今日は晴れです。\n散歩に行きます\n帰りました")
            hist.close()


# ------------------------------------------------------------------ タイムライン
def loud(sec):
    return C.sine(sec, -10)


class TimelineV15Test(unittest.TestCase):
    def test_without_pause_rows_are_unchanged(self):
        tl = Timeline(30)
        tl.feed(loud(70))
        rows = tl.rows()
        self.assertEqual([r.label for r in rows], ["00:00–00:30", "00:30–01:00", "01:00–01:10"])
        self.assertTrue(all(r.kind == AUDIO and r.slots == 0 for r in rows))
        self.assertEqual(len(rows[0].peaks), 60)

    def test_pause_splits_rows_and_adds_one_marker_row(self):
        tl = Timeline(30)
        tl.feed(loud(20))
        i = tl.add_pause(20.0)
        tl.feed(loud(30))      # 再開後に 30 秒（WAV 上は 20〜50 秒）
        rows = tl.rows()
        kinds = [r.kind for r in rows]
        self.assertEqual(kinds, [AUDIO, PAUSE, AUDIO])
        self.assertEqual(rows[1].label, "一時停止 00:20")
        self.assertEqual(rows[1].text, "休止中")
        self.assertEqual((rows[0].start, rows[0].end), (0.0, 20.0))
        self.assertEqual((rows[2].start, rows[2].end), (20.0, 50.0))
        # 短く閉じた行は「未録音の点線」を出さないため slots が実長
        self.assertEqual(rows[0].slots, 40)
        self.assertEqual(rows[2].slots, 0)
        tl.set_pause_length(i, 90.0)
        self.assertEqual(tl.rows()[1].text, "休止 1:30")
        self.assertEqual([r.index for r in tl.rows()], [0, 1, 2])

    def test_drafts_and_finals_go_to_the_right_side_of_the_pause(self):
        tl = Timeline(30)
        tl.feed(loud(20))
        tl.add_pause(20.0)
        tl.feed(loud(20))
        tl.add_draft(5, 8, "前半")
        tl.add_draft(25, 28, "後半")
        rows = tl.rows()
        self.assertEqual((rows[0].text, rows[0].state), ("前半", DRAFT))
        self.assertEqual((rows[2].text, rows[2].state), ("後半", DRAFT))
        tl.set_final([(5, 8, "前半です。"), (25, 28, "後半です。")], 40)
        rows = tl.rows()
        self.assertEqual([r.kind for r in rows], [AUDIO, PAUSE, AUDIO])
        self.assertEqual((rows[0].text, rows[2].text), ("前半です。", "後半です。"))

    def test_paused_at_the_end_has_no_live_row(self):
        tl = Timeline(30)
        tl.set_recording(True)
        tl.feed(loud(40))
        self.assertTrue(tl.rows()[-1].live)
        tl.add_pause(40.0)
        rows = tl.rows()
        self.assertEqual([r.kind for r in rows], [AUDIO, AUDIO, PAUSE])
        self.assertFalse(any(r.live for r in rows))
        tl.feed(loud(5))   # 再開
        rows = tl.rows()
        self.assertEqual([r.kind for r in rows], [AUDIO, AUDIO, PAUSE, AUDIO])
        self.assertTrue(rows[-1].live)

    def test_two_pauses_in_a_row_and_pause_at_zero(self):
        tl = Timeline(30)
        tl.add_pause(0.0)
        tl.feed(loud(10))
        tl.add_pause(10.0)
        tl.add_pause(10.0)
        rows = tl.rows()
        self.assertEqual([r.kind for r in rows], [PAUSE, AUDIO, PAUSE, PAUSE])

    def test_silence_rows_are_collapsed(self):
        tl = Timeline(30)
        tl.feed(loud(150))
        tl.set_final([(0.5, 3.0, "こんにちは。")], 150)
        rows = tl.rows()
        self.assertEqual([r.kind for r in rows], [AUDIO, SILENCE])
        self.assertEqual(rows[1].label, "無音 2:00")
        self.assertEqual((rows[1].start, rows[1].end), (30.0, 150.0))
        # 発話・無音・発話・無音
        tl2 = Timeline(30)
        tl2.feed(loud(150))
        tl2.set_final([(1, 2, "あ"), (95, 96, "い")], 150)
        rows = tl2.rows()
        self.assertEqual([r.kind for r in rows], [AUDIO, SILENCE, AUDIO, SILENCE])
        self.assertEqual([r.label for r in rows if r.kind == SILENCE], ["無音 1:00", "無音 0:30"])

    def test_silence_only_applies_to_final_empty_rows(self):
        tl = Timeline(30)
        tl.feed(loud(90))
        self.assertTrue(all(r.kind == AUDIO and r.state == WAVE for r in tl.rows()))   # 仮認識前は波形のまま
        tl.add_draft(1, 2, "あ")
        self.assertEqual([r.kind for r in tl.rows()], [AUDIO] * 3)

    def test_pause_between_silences_keeps_them_separate(self):
        tl = Timeline(30)
        tl.feed(loud(60))
        tl.add_pause(60.0)
        tl.feed(loud(60))
        tl.set_final([], 120)
        self.assertEqual([r.kind for r in tl.rows()], [SILENCE, PAUSE, SILENCE])


# ------------------------------------------------------------------ 入力ストリームの引き継ぎ・一時停止
class RampStream:
    """1 サンプルずつ増える波形（0..29999 を繰り返す）を流し続ける。欠け・重複が差分で分かる。"""
    MOD = 30000

    def __init__(self, owner, callback, speed, zeros=False):
        self.owner, self.cb, self.speed, self.zeros = owner, callback, speed, zeros
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        n = 1600
        pos = 0
        while not self._stop.is_set():
            if self.zeros:
                blk = np.zeros((n, 1), dtype=np.int16)
            else:
                blk = ((np.arange(pos, pos + n) % self.MOD)).astype(np.int16).reshape(-1, 1)
            pos += n
            self.cb(blk, n, None, None)
            time.sleep(0.1 / self.speed)

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()
        self.owner.closed += 1


class RampSD:
    default = types.SimpleNamespace(device=(0, 0))

    def __init__(self, speed=40.0, zeros=False, fail=None):
        self.speed, self.zeros, self.fail = speed, zeros, fail
        self.opened = self.closed = 0

    def InputStream(self, **kw):
        if self.fail is not None:
            raise self.fail
        self.opened += 1
        return RampStream(self, kw["callback"], self.speed, self.zeros)

    def query_devices(self, *a, **k):
        return {"default_samplerate": 16000}


class LoopStream:
    """src を繰り返し流す（実マイクは開かない）。"""

    def __init__(self, src, cb, speed=40.0):
        self.src, self.cb, self.speed = src, cb, speed
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._t.start()

    def _run(self):
        while not self._stop.is_set():
            for i in range(0, len(self.src), 1600):
                if self._stop.is_set():
                    return
                blk = self.src[i:i + 1600].reshape(-1, 1)
                self.cb(blk, len(blk), None, None)
                time.sleep(0.1 / self.speed)

    def stop(self):
        self._stop.set()

    def close(self):
        self._stop.set()


class LoopSD:
    default = types.SimpleNamespace(device=(0, 0))

    def __init__(self, src):
        self.src = src
        self.opened = 0

    def InputStream(self, **kw):
        self.opened += 1
        return LoopStream(self.src, kw["callback"])

    def query_devices(self, *a, **k):
        return {"default_samplerate": 16000}


def read_wav(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.int64)


def jumps(x, mod=RampStream.MOD):
    """連続（差が +1 mod）でない位置のリスト。"""
    d = (np.diff(x) % mod)
    return [int(i) + 1 for i in np.nonzero(d != 1)[0]]


def wait_for(cond, sec=5.0):
    t0 = time.time()
    while time.time() - t0 < sec:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class MonitorHandoffTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self._d.name) / "out"
        self.s = {"out_dir": str(self.out), "row_sec": 30, "silence_stop_sec": 0, "live_enabled": False}
        self.sessions = []

    def tearDown(self):
        for s in self.sessions:
            s.close()
        self._d.cleanup()

    def session(self, sd, **kw):
        s = Session(self.s, Replacer(), sd=sd, **kw)
        self.sessions.append(s)
        return s

    def test_idle_monitor_runs_without_saving_anything(self):
        sd = RampSD()
        s = self.session(sd)
        s.apply_monitor_now(True)
        self.assertTrue(s.monitor_active)
        self.assertTrue(wait_for(lambda: s.idle_monitor.stats() is not None))   # 1 秒ぶん貯まった
        self.assertGreater(s.level, 0.0)                                         # メーターが動く
        self.assertFalse(self.out.exists() and any(self.out.iterdir()))          # WAV は作られない
        self.assertEqual(sd.opened, 1)
        s.apply_monitor_now(False)
        self.assertFalse(s.monitor_active)
        self.assertEqual(sd.closed, 1)
        self.assertEqual(s.level, 0.0)

    def test_recording_takes_over_the_same_stream_without_gaps(self):
        sd = RampSD()
        s = self.session(sd)
        s.apply_monitor_now(True)
        self.assertTrue(wait_for(lambda: s.idle_monitor.stats() is not None))
        wav = s.start(self.out / "t.wav", live_enabled=False)
        self.assertEqual(sd.opened, 1, "デバイスを開き直してはいけない")
        self.assertTrue(wait_for(lambda: s.elapsed > 1.0))
        s.stop()
        self.assertEqual(sd.opened, 1)
        self.assertTrue(s.monitor_active, "窓が見えている間はモニターのストリームを開いたまま")
        x = read_wav(wav)
        self.assertGreater(len(x), 16000)
        self.assertEqual(jumps(x), [], "録音の中に欠け・重複がある")
        # 録音が終わったあとも待機中モニターが動き続ける
        s.idle_monitor.reset()
        self.assertTrue(wait_for(lambda: s.idle_monitor.stats() is not None))
        # 録音中は待機中の経路に流れない（二重に溜めない）
        s.apply_monitor_now(False)
        self.assertEqual(sd.closed, 1)

    def test_stop_closes_stream_when_monitor_not_wanted(self):
        sd = RampSD()
        s = self.session(sd)
        s.start(self.out / "t.wav", live_enabled=False)       # モニターなしで録音開始（ここで開く）
        self.assertEqual(sd.opened, 1)
        time.sleep(0.2)
        s.stop()
        self.assertFalse(s.monitor_active)
        self.assertEqual(sd.closed, 1)

    def test_pause_resume_writes_nothing_while_paused(self):
        sd = RampSD()
        s = self.session(sd)
        s.apply_monitor_now(True)
        wav = s.start(self.out / "p.wav", live_enabled=False)
        self.assertTrue(wait_for(lambda: s.elapsed > 0.5))
        self.assertTrue(s.pause())
        self.assertTrue(s.is_paused)
        self.assertFalse(s.pause())                      # 二重の一時停止は無視
        time.sleep(0.15)
        e1 = s.elapsed
        time.sleep(0.5)
        self.assertAlmostEqual(s.elapsed, e1, delta=0.01, msg="一時停止中に経過時間が進んだ")
        self.assertGreater(s.level, 0.0, "一時停止中もメーターは動く")
        self.assertTrue(s.resume())
        self.assertFalse(s.resume())
        self.assertTrue(wait_for(lambda: s.elapsed > e1 + 0.5))
        s.stop()
        x = read_wav(wav)
        j = jumps(x)
        self.assertEqual(len(j), 1, "一時停止のところで 1 回だけ飛ぶはず")
        marks = s.rec.pause_marks
        self.assertEqual([m[0] for m in marks], ["pause", "resume"])
        self.assertAlmostEqual(marks[0][1], j[0] / 16000.0, places=6)      # 印の位置 = 飛んだ位置
        self.assertAlmostEqual(marks[1][1], j[0] / 16000.0, places=6)
        # タイムラインに印の行が 1 行
        pr = [r for r in s.timeline.rows() if r.kind == PAUSE]
        self.assertEqual(len(pr), 1)
        self.assertEqual(pr[0].start, marks[0][1])
        self.assertTrue(pr[0].text.startswith("休止 "), pr[0].text)
        self.assertGreater(s.paused_total, 0.4)

    def test_stop_while_paused_closes_the_pause_mark(self):
        sd = RampSD()
        s = self.session(sd)
        s.start(self.out / "q.wav", live_enabled=False)
        self.assertTrue(wait_for(lambda: s.elapsed > 0.3))
        s.pause()
        time.sleep(0.2)
        wav = s.stop()
        rows = s.timeline.rows()
        self.assertEqual([r.kind for r in rows if r.kind == PAUSE], [PAUSE])
        self.assertTrue(rows[-1].text.startswith("休止 "))
        self.assertEqual(jumps(read_wav(wav)), [])     # 最後まで一時停止なら WAV に欠けはない

    def test_toggle_pause_and_not_recording(self):
        s = self.session(RampSD())
        self.assertIsNone(s.toggle_pause())
        self.assertFalse(s.pause())
        s.start(self.out / "r.wav", live_enabled=False)
        self.assertTrue(s.toggle_pause())
        self.assertFalse(s.toggle_pause())

    def test_silence_autostop_does_not_advance_while_paused(self):
        s = self.session(RampSD(zeros=True))
        s.s = dict(self.s, silence_stop_sec=0)
        s.start(self.out / "z.wav", live_enabled=False)
        self.assertTrue(wait_for(lambda: s.silence.silent_sec > 0.5))
        s.pause()
        time.sleep(0.2)
        v = s.silence.silent_sec
        time.sleep(0.6)
        self.assertAlmostEqual(s.silence.silent_sec, v, delta=0.01)
        s.stop()

    def test_open_failure_is_not_an_error_but_a_message(self):
        sd = RampSD(fail=OSError("Device unavailable (exclusive)"))
        clock = FakeClock()
        s = self.session(sd, clock=clock)
        s.apply_monitor_now(True)                        # 例外を出さない
        self.assertFalse(s.monitor_active)
        self.assertEqual(s.monitor_error, MIC_BUSY_MESSAGE)
        self.assertIn("exclusive", s.monitor_error_detail)
        # 15 秒たつまでは開き直さない
        sd.fail = None
        s.apply_monitor_now(True)
        self.assertFalse(s.monitor_active)
        clock.t += 16
        s.apply_monitor_now(True)
        self.assertTrue(s.monitor_active)
        self.assertEqual(s.monitor_error, "")
        # 録音開始の失敗は従来どおり例外（画面で警告する）
        s.apply_monitor_now(False)
        sd.fail = OSError("busy")
        with self.assertRaises(OSError):
            s.start(self.out / "f.wav", live_enabled=False)
        self.assertFalse(s.is_recording)

    def test_request_monitor_is_asynchronous_and_idempotent(self):
        sd = RampSD()
        s = self.session(sd)
        for _ in range(5):
            s.request_monitor(True)
        self.assertTrue(wait_for(lambda: s.monitor_active))
        for _ in range(5):
            s.request_monitor(True)
        time.sleep(0.1)
        self.assertEqual(sd.opened, 1)
        s.request_monitor(False)
        self.assertTrue(wait_for(lambda: not s.monitor_active))
        self.assertEqual(sd.closed, 1)

    def test_request_monitor_does_not_touch_a_recording(self):
        sd = RampSD()
        s = self.session(sd)
        s.start(self.out / "m.wav", live_enabled=False)
        s.request_monitor(False)      # 窓を隠した等。録音中は閉じない
        time.sleep(0.2)
        self.assertTrue(s.monitor_active)
        self.assertTrue(s.is_recording)
        s.stop()
        self.assertFalse(s.monitor_active)

    def test_input_device_change_closes_idle_stream(self):
        sd = RampSD()
        s = self.session(sd)
        s.apply_monitor_now(True)
        s.s["input_device"] = 3
        s.input_device_changed()
        self.assertFalse(s.monitor_active)
        self.assertEqual(sd.closed, 1)

    def test_mic_test_listens_on_the_open_stream(self):
        sd = RampSD()
        s = self.session(sd)
        s.apply_monitor_now(True)
        stats, issues = s.mic_test(seconds=0.3, sleep=time.sleep)
        self.assertEqual(sd.opened, 1, "テストのためにもう一度開いてはいけない")
        self.assertGreater(stats.seconds, 0.2)

    def test_recorder_alone_still_works_with_pause(self):
        sd = RampSD()
        got = []
        rec = Recorder(on_audio=lambda x: got.append(len(x)), sd=sd)
        wav = rec.start(Path(self._d.name) / "v1.wav")
        self.assertTrue(wait_for(lambda: rec.elapsed > 0.3))
        rec.pause()
        time.sleep(0.1)
        e = rec.elapsed
        time.sleep(0.3)
        self.assertAlmostEqual(rec.elapsed, e, delta=0.01)
        rec.resume()
        time.sleep(0.3)
        rec.stop()
        self.assertEqual(sd.closed, 1)
        self.assertEqual(len(jumps(read_wav(wav))), 1)
        self.assertGreater(sum(got), 8000)


class IdlePromptSessionTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.s = {"out_dir": self._d.name, "row_sec": 30, "silence_stop_sec": 0, "live_enabled": False}
        self.clock = FakeClock()

    def tearDown(self):
        self.sess.close()
        self._d.cleanup()

    def make(self, src, **settings):
        self.s.update(settings)
        self.sess = Session(self.s, Replacer(), sd=LoopSD(src), clock=self.clock)
        return self.sess

    def test_prompt_when_speaking_not_recording(self):
        s = self.make(C.speechlike(12, -12))
        s.apply_monitor_now(True)
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8))
        self.assertFalse(s.poll_idle_prompt(window_active=False), "窓が隠れているときは出さない")
        self.assertTrue(s.poll_idle_prompt(window_active=True))
        # クールダウン中は出さない（時計を進める）
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8))
        self.clock.t += 59
        self.assertFalse(s.poll_idle_prompt(True))
        self.clock.t += 2
        self.assertTrue(s.poll_idle_prompt(True))

    def test_no_prompt_for_silence_or_when_disabled_or_recording(self):
        s = self.make(C.noise(10, -70, 1))
        s.apply_monitor_now(True)
        time.sleep(0.5)
        self.assertFalse(s.poll_idle_prompt(True))
        s.apply_monitor_now(False)
        s.close()
        s2 = self.make(C.speechlike(12, -12), idle_prompt_enabled=False)
        s2.apply_monitor_now(True)
        self.assertTrue(wait_for(lambda: s2.idle_speech.run_sec >= 1.5, 8))
        self.assertFalse(s2.poll_idle_prompt(True), "設定で OFF")
        s2.s["idle_prompt_enabled"] = True
        s2.start(Path(self._d.name) / "x.wav", live_enabled=False)
        self.assertFalse(s2.poll_idle_prompt(True), "録音中は出さない")
        s2.stop()

    def test_prompt_needs_the_monitor_to_be_open(self):
        s = self.make(C.speechlike(12, -12))
        self.assertFalse(s.poll_idle_prompt(True))

    def test_cooldown_and_threshold_come_from_settings(self):
        s = self.make(C.speechlike(12, -12), idle_prompt_cooldown_sec=5, idle_prompt_speech_sec=1.0)
        s.apply_monitor_now(True)
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.0, 8))
        self.assertTrue(s.poll_idle_prompt(True))
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.0, 8))
        self.clock.t += 5
        self.assertTrue(s.poll_idle_prompt(True))


# ------------------------------------------------------------------ 窓位置・再生デバイス・その他
class GeometryTest(unittest.TestCase):
    SCR = [(0, 0, 1920, 1040)]

    def test_inside_is_unchanged(self):
        self.assertEqual(clamp_geometry((100, 100, 800, 600), self.SCR), (100, 100, 800, 600))

    def test_off_screen_is_pulled_in(self):
        self.assertEqual(clamp_geometry((5000, 100, 800, 600), self.SCR), (1120, 100, 800, 600))
        self.assertEqual(clamp_geometry((-3000, -500, 800, 600), self.SCR), (0, 0, 800, 600))
        self.assertEqual(clamp_geometry((1800, 1000, 800, 600), self.SCR), (1120, 440, 800, 600))

    def test_too_big_is_shrunk(self):
        self.assertEqual(clamp_geometry((0, 0, 4000, 3000), self.SCR), (0, 0, 1920, 1040))

    def test_multi_monitor_picks_overlapping_screen(self):
        scr = [(-1920, 0, 1920, 1040), (0, 0, 1920, 1040)]
        self.assertEqual(clamp_geometry((-1000, 50, 800, 600), scr), (-1000, 50, 800, 600))
        # 画面の外側（どれとも重ならない）は先頭の画面へ
        self.assertEqual(clamp_geometry((9000, 9000, 800, 600), scr), (-800, 440, 800, 600))
        # 取り外された 2 枚目の画面にいた窓: 残った画面へ寄せる
        self.assertEqual(clamp_geometry((-1000, 50, 800, 600), [(0, 0, 1920, 1040)]), (0, 50, 800, 600))

    def test_no_screens_or_garbage(self):
        self.assertEqual(clamp_geometry((1, 2, 3, 4), []), (1, 2, 3, 4))
        self.assertIsNone(parse_geometry([]))
        self.assertIsNone(parse_geometry(None))
        self.assertIsNone(parse_geometry([1, 2, "a", 4]))
        self.assertIsNone(parse_geometry([1, 2, 0, 4]))
        self.assertEqual(parse_geometry([1, 2, 300, 200]), (1, 2, 300, 200))


class PlaybackDeviceLogicTest(unittest.TestCase):
    A = OutputDevice("id-a", "スピーカー", True)
    B = OutputDevice("id-b", "ヘッドセット", False)
    C = OutputDevice("id-c", "HDMI", False)

    def test_default_follows_the_current_default(self):
        devs = [self.A, self.B]
        r = resolve_output("", devs)
        self.assertEqual((r.device.id, r.follows_default, r.notice), ("id-a", True, ""))
        # 常駐ツールで既定がヘッドセットに切り替わった
        devs2 = [OutputDevice("id-a", "スピーカー", False), OutputDevice("id-b", "ヘッドセット", True)]
        self.assertEqual(resolve_output("", devs2).device.id, "id-b")

    def test_explicit_selection_is_kept_even_if_default_changes(self):
        devs = [OutputDevice("id-a", "スピーカー", False), OutputDevice("id-b", "ヘッドセット", True)]
        r = resolve_output("id-a", devs)
        self.assertEqual((r.device.id, r.follows_default, r.reset_setting), ("id-a", False, False))

    def test_missing_selection_falls_back_with_notice(self):
        r = resolve_output("id-zzz", [self.A, self.B])
        self.assertEqual(r.device.id, "id-a")
        self.assertTrue(r.follows_default and r.reset_setting)
        self.assertIn("システム既定に戻しました", r.notice)

    def test_no_devices(self):
        r = resolve_output("", [])
        self.assertIsNone(r.device)
        self.assertIsNone(default_device([]))
        self.assertEqual(default_device([self.B, self.C]).id, "id-b")   # 既定の印がなければ先頭

    def test_combo_entries(self):
        e = combo_entries([self.A, self.B])
        self.assertEqual(e[0], (DEFAULT_LABEL, ""))
        self.assertEqual([x[1] for x in e], ["", "id-a", "id-b"])
        self.assertIn("現在の既定", e[1][0])
        self.assertNotIn("現在の既定", e[2][0])


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WinShellTest(unittest.TestCase):
    def test_app_user_model_id_roundtrip(self):
        from winshell import APP_USER_MODEL_ID, get_app_user_model_id, set_app_user_model_id
        self.assertEqual(APP_USER_MODEL_ID, "kabeuchi.stt")
        self.assertTrue(set_app_user_model_id())
        self.assertEqual(get_app_user_model_id(), "kabeuchi.stt")


if __name__ == "__main__":
    unittest.main()
