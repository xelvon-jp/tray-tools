# -*- coding: utf-8 -*-
"""フェーズ1.6 のコア: 音声から測るすき間（改行が効かなかった原因の修正）・一時停止中の話し声の指摘・録音中の逐次認識の切替。
マイクは開かない（sounddevice は差し替え）。音は出さない。
"""
import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from gaps import segment_gaps, silence_at, silence_gaps, silence_runs, voiced_frames
from idle_speech import IDLE, PAUSED, prompt_kind
from paragraph import format_for_output, format_paragraphs
from replacements import Replacer
from session import Session
from test_core_v15 import FakeClock, LoopSD, RampSD, jumps, read_wav, wait_for
from timeline import Timeline

SR = 16000


def audio_of(*parts):
    """("tone", 秒) / ("sil", 秒) を並べた音声。tone は発話のつもり。"""
    out = []
    for kind, sec in parts:
        out.append(C.sine(sec, -12) if kind == "tone" else C.noise(sec, -70, 7))
    return np.concatenate(out)


class GapMeasureTest(unittest.TestCase):
    def test_voiced_and_silence_runs(self):
        a = audio_of(("tone", 2), ("sil", 3), ("tone", 2))
        v = voiced_frames(a)
        runs = silence_runs(v)
        self.assertEqual(len(runs), 1)
        self.assertAlmostEqual(runs[0][0], 2.0, delta=0.1)
        self.assertAlmostEqual(runs[0][1], 5.0, delta=0.1)

    def test_silence_at_boundary(self):
        v = voiced_frames(audio_of(("tone", 2), ("sil", 3), ("tone", 2)))
        self.assertAlmostEqual(silence_at(v, 5.0), 3.0, delta=0.15)     # 無音の終わりの境目
        self.assertAlmostEqual(silence_at(v, 3.5), 3.0, delta=0.15)     # 無音の途中の境目
        self.assertLess(silence_at(v, 1.0), 0.15)                       # 発話の途中

    def test_whisper_style_contiguous_segments_still_show_the_gap(self):
        """Whisper は end==次の start で返すことがある。時刻だけだとすき間 0。音声から測れば見える。"""
        a = audio_of(("tone", 3), ("sil", 5), ("tone", 3), ("sil", 2.5), ("tone", 3))
        segs = [(0.0, 8.0, "A"), (8.0, 13.5, "B"), (13.5, 16.5, "C")]
        g = silence_gaps(a, segs)
        self.assertEqual(g[0], 0.0)
        self.assertAlmostEqual(g[1], 5.0, delta=0.2)
        self.assertAlmostEqual(g[2], 2.5, delta=0.2)

    def test_one_silence_goes_to_one_boundary_only(self):
        a = audio_of(("tone", 2), ("sil", 4), ("tone", 2))
        segs = [(0, 2.0, "a"), (2.0, 4.0, "b"), (4.2, 6.0, "c"), (6.0, 8.0, "d")]
        g = segment_gaps(segs, voiced_frames(a))
        self.assertEqual(sum(1 for x in g if x >= 2.0), 1)   # 沈黙 1 つ → 改行になる境目は 1 つだけ

    def test_short_pauses_are_not_gaps(self):
        a = audio_of(("tone", 2), ("sil", 0.6), ("tone", 2))
        g = silence_gaps(a, [(0, 2.0, "a"), (2.0, 4.6, "b")])
        self.assertEqual(g, [0.0, 0.0])    # 1 秒未満の無音は無視（改行の対象にならない）

    def test_robust_to_garbage(self):
        self.assertEqual(silence_gaps(np.zeros(0, dtype=np.int16), []), [])
        self.assertEqual(silence_gaps(np.zeros(100, dtype=np.int16), [(0, 1, "a"), ("x", 2, "b")]), [0.0, 0.0])
        self.assertEqual(silence_at(np.zeros(0, dtype=bool), 3.0), 0.0)


class ParagraphWithGapsTest(unittest.TestCase):
    def test_gaps_override_zero_timestamp_gap(self):
        segs = [(0, 8.0, "最初の文"), (8.0, 13.5, "二番目です。"), (13.5, 16.5, "三番目")]
        self.assertEqual(format_paragraphs(segs), "最初の文二番目です。三番目")                    # 時刻だけでは連結（旧挙動）
        self.assertEqual(format_paragraphs(segs, gaps=[0, 5.0, 2.5]), "最初の文。\n二番目です。\n三番目")

    def test_rules_apply_to_measured_gaps(self):
        segs = [(0, 1, "え"), (1, 2, "い。"), (2, 3, "う")]
        self.assertEqual(format_paragraphs(segs, gaps=[0, 2.5, 2.5]), "えい。\nう")     # 句点なし 2.5 は改行しない／あり 2.5 は改行
        self.assertEqual(format_paragraphs(segs, gaps=[0, 3.0, 0.2]), "え。\nい。う")   # 3 秒以上は句点を補って改行
        self.assertEqual(format_paragraphs(segs, gaps=None), "えい。う")
        self.assertEqual(format_paragraphs(segs, gaps=[0]), "えい。う")                  # gaps が短くても落ちない

    def test_timestamp_gap_still_counts_when_larger(self):
        segs = [(0, 1, "あ。"), (6, 7, "い")]
        self.assertEqual(format_paragraphs(segs, gaps=[0, 0.0]), "あ。\nい")

    def test_for_output_passes_gaps(self):
        segs = [(0, 5, "あ"), (5, 9, "い")]
        self.assertEqual(format_for_output(segs, {"paragraph_enabled": True}, [0, 4.0]), "あ。\nい")
        self.assertEqual(format_for_output(segs, {"paragraph_enabled": False}, [0, 4.0]), "あ\nい")


class PipelineGapsTest(unittest.TestCase):
    """GUI と同じ経路（make_run_job → transcribe_wav → txt/md/履歴/タイムライン）で、Whisper 風の連続区間を通す。"""

    def run_case(self, audio, segs_spec, settings_extra=None):
        from finalize_queue import Job
        from history import History
        from model_cache import ModelCache
        from pipeline import make_run_job
        from transcriber import transcribe_wav

        class Seg:
            def __init__(self, a, b, t):
                self.start, self.end, self.text = a, b, t

        class FakeModel:
            def transcribe(self, audio_, **kw):
                return iter([Seg(*s) for s in segs_spec]), None

        d = Path(tempfile.mkdtemp(prefix="kabeuchi_gap_"))
        wavp = d / "a.wav"
        with wave.open(str(wavp), "wb") as f:
            f.setnchannels(1), f.setsampwidth(2), f.setframerate(SR)
            f.writeframes(audio.tobytes())
        settings = {"out_dir": str(d / "out"), "paragraph_enabled": True, "paragraph_gap_sec": 2.0,
                    "paragraph_force_gap_sec": 3.0, "final_model": "x", "final_cpu_threads": 0}
        settings.update(settings_extra or {})
        hist = History(d / "h.db")
        run = make_run_job(lambda: settings, Replacer(), hist, ModelCache(lambda n, t: FakeModel()),
                           transcribe=transcribe_wav)
        tl = Timeline(30)
        job = Job(1, wavp, timeline=tl)
        r = run(job)
        out = (r, Path(r.txt_path).read_text(encoding="utf-8"), Path(r.md_path).read_text(encoding="utf-8"),
               hist.get(job.history_id), tl)
        hist.close()
        return out

    def test_long_gap_and_2_to_3s_gap_with_contiguous_whisper_segments(self):
        a = audio_of(("tone", 3), ("sil", 5), ("tone", 3), ("sil", 2.5), ("tone", 3))
        r, txt, md, rec, tl = self.run_case(a, [(0.0, 8.0, "最初の文"), (8.0, 13.5, "二番目です。"), (13.5, 16.5, "三番目")])
        want = "最初の文。\n二番目です。\n三番目"
        self.assertEqual(r.text, want)                                  # クリップボード（auto_copy）に載る文字
        self.assertIn(want, txt)                                        # txt
        self.assertIn("## 全文\n\n" + want + "\n", md.replace("\r\n", "\n"))   # md の全文
        self.assertEqual(rec.text, "最初の文\n二番目です。\n三番目")              # 履歴は無加工
        self.assertEqual([tuple(s) for s in rec.segments], [(0.0, 8.0, "最初の文"), (8.0, 13.5, "二番目です。"), (13.5, 16.5, "三番目")])
        # タイムラインにも測ったすき間が渡り、「送る」も同じ整形になる
        self.assertIsNotNone(tl.final_gaps())
        self.assertEqual(format_for_output(tl.final_segments(), {"paragraph_enabled": True}, tl.final_gaps()), want)

    def test_2_to_3s_gap_without_period_does_not_break(self):
        a = audio_of(("tone", 3), ("sil", 2.5), ("tone", 3))
        r, *_ = self.run_case(a, [(0.0, 5.5, "えーと"), (5.5, 8.5, "次に")])
        self.assertEqual(r.text, "えーと次に")          # 句点なしの 2〜3 秒は考え中かもしれないので改行しない

    def test_short_pause_stays_joined_and_off_switch(self):
        a = audio_of(("tone", 3), ("sil", 0.8), ("tone", 3))
        r, *_ = self.run_case(a, [(0.0, 3.8, "あ。"), (3.8, 6.8, "い。")])
        self.assertEqual(r.text, "あ。い。")
        a2 = audio_of(("tone", 3), ("sil", 6), ("tone", 3))
        r2, *_ = self.run_case(a2, [(0.0, 9.0, "あ。"), (9.0, 12.0, "い。")], {"paragraph_enabled": False})
        self.assertEqual(r2.text, "あ。\nい。")

    def test_real_recording_pattern(self):
        """実録音 20261122_010803 と同じ形: 区間は連続（end==次の start）で、無音は区間の中にある。"""
        a = audio_of(("tone", 4), ("sil", 2.7), ("tone", 3), ("sil", 22), ("tone", 2))
        segs = [(0.0, 6.7, "一つ目"), (6.7, 9.7, "二つ目です"), (9.7, 31.7, "三つ目の前"), (31.7, 33.7, "四つ目")]
        r, *_ = self.run_case(a, segs)
        self.assertIn("\n", r.text)
        self.assertTrue(r.text.startswith("一つ目"))
        self.assertGreaterEqual(r.text.count("\n"), 1)

    def test_measure_failure_falls_back_to_timestamps(self):
        import gaps as G
        orig = G.silence_gaps
        G.silence_gaps = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            a = audio_of(("tone", 3), ("sil", 6), ("tone", 3))
            r, *_ = self.run_case(a, [(0.0, 3.0, "あ。"), (9.0, 12.0, "い。")])
        finally:
            G.silence_gaps = orig
        self.assertEqual(r.text, "あ。\nい。")      # 時刻のすき間 6 秒だけで整形（落ちない）


class PromptKindTest(unittest.TestCase):
    def test_kind_table(self):
        self.assertEqual(prompt_kind(False, False), IDLE)
        self.assertEqual(prompt_kind(False, True), IDLE)      # 録音していなければ「録音していません」
        self.assertEqual(prompt_kind(True, True), PAUSED)
        self.assertIsNone(prompt_kind(True, False))


class PausedPromptSessionTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.s = {"out_dir": self._d.name, "row_sec": 30, "silence_stop_sec": 0, "live_enabled": False}
        self.clock = FakeClock()
        self.sess = Session(self.s, Replacer(), sd=LoopSD(C.speechlike(12, -12)), clock=self.clock)

    def tearDown(self):
        self.sess.close()
        self._d.cleanup()

    def test_paused_prompt_only_while_paused_and_shares_cooldown(self):
        s = self.sess
        s.start(Path(self._d.name) / "p.wav", live_enabled=False)
        time.sleep(0.6)
        self.assertIsNone(s.poll_idle_prompt(True), "録音中（一時停止でない）は話していて当然なので出さない")
        s.pause()
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8), "一時停止中の話し声を検出する")
        self.assertIsNone(s.poll_idle_prompt(False), "窓が隠れているときは出さない")
        self.assertEqual(s.poll_idle_prompt(True), PAUSED)
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8))
        self.clock.t += 59
        self.assertIsNone(s.poll_idle_prompt(True), "クールダウン中")
        self.clock.t += 2
        self.assertEqual(s.poll_idle_prompt(True), PAUSED)
        s.resume()
        time.sleep(0.5)
        self.assertIsNone(s.poll_idle_prompt(True))
        s.stop()

    def test_setting_off_and_idle_kind_when_not_recording(self):
        s = self.sess
        s.s["idle_prompt_enabled"] = False
        s.start(Path(self._d.name) / "q.wav", live_enabled=False)
        s.pause()
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8))
        self.assertIsNone(s.poll_idle_prompt(True), "設定 OFF は一時停止中も出さない")
        s.stop()
        s.s["idle_prompt_enabled"] = True
        s.apply_monitor_now(True)
        self.assertTrue(wait_for(lambda: s.idle_speech.run_sec >= 1.5, 8))
        self.assertEqual(s.poll_idle_prompt(True), IDLE)


class LiveFake:
    """逐次認識の偽物（実モデルなし）。feed の回数と stop を記録し、5 ブロック目に区間を 1 つ返す。"""

    def __init__(self, on_segment):
        self.on_segment, self.fed, self.started, self.stopped = on_segment, 0, False, False
        self.processed_segments = self.pending_segments = 0
        self.error = None

    def start(self):
        self.started = True

    def stop(self, wait=0):
        self.stopped = True

    def feed(self, x):
        self.fed += 1
        if self.fed == 5:
            self.on_segment(0.2, 0.9, "途中から")


class LiveToggleTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.out = Path(self._d.name)
        self.s = {"out_dir": self._d.name, "row_sec": 30, "silence_stop_sec": 0, "live_enabled": False}
        self.lives = []
        self.sess = Session(self.s, Replacer(), sd=RampSD(),
                            live_factory=lambda seg, st: self.lives.append(LiveFake(seg)) or self.lives[-1])

    def tearDown(self):
        self.sess.close()
        self._d.cleanup()

    def test_turn_on_and_off_during_recording_never_breaks_the_wav(self):
        s = self.sess
        wav = s.start(self.out / "l.wav", live_enabled=False)
        self.assertIsNone(s.live)
        self.assertTrue(wait_for(lambda: s.elapsed > 0.5))
        at = s.elapsed
        s.set_live(True)                                   # 録音中に ON
        self.assertEqual(len(self.lives), 1)
        live = self.lives[0]
        self.assertTrue(live.started)
        self.assertTrue(wait_for(lambda: live.fed >= 6), "その時点からの音声が渡る")
        drafts = [r for r in s.timeline.rows() if r.state == "draft"]
        self.assertTrue(drafts, "途中から始めた逐次認識の区間が画面に出る")
        # 区間の時刻は、始めた位置（録音の秒）を足して WAV 上の時刻に合う
        self.assertGreaterEqual(s.timeline._drafts[0][0], at - 0.3)
        s.set_live(False)                                  # 録音中に OFF
        self.assertIsNone(s.live)
        self.assertTrue(wait_for(lambda: live.stopped), "認識は別スレッドで止まる")
        n = live.fed
        e = s.elapsed
        time.sleep(0.4)
        self.assertEqual(live.fed, n, "OFF のあとは音声を渡さない")
        self.assertGreater(s.elapsed, e, "録音は続いている")
        s.set_live(True)                                   # もう一度 ON（新しい認識）
        self.assertEqual(len(self.lives), 2)
        s.stop()
        x = read_wav(wav)
        self.assertEqual(jumps(x), [], "切り替えても WAV に欠け・重複がない")

    def test_noop_when_not_recording_and_idempotent(self):
        s = self.sess
        s.set_live(True)
        self.assertEqual(self.lives, [])
        s.start(self.out / "m.wav", live_enabled=True)
        self.assertEqual(len(self.lives), 1)
        s.set_live(True)                                   # すでに ON
        self.assertEqual(len(self.lives), 1)
        s.set_live(False)
        s.set_live(False)
        s.stop()

    def test_live_error_does_not_stop_recording(self):
        s = self.sess
        wav = s.start(self.out / "n.wav", live_enabled=False)
        s.set_live(True)
        self.lives[0].feed = lambda x: (_ for _ in ()).throw(RuntimeError("live broken"))
        s._tap.parts[-1] = self.lives[0].feed
        time.sleep(0.4)
        self.assertTrue(s.is_recording)
        s.stop()
        self.assertEqual(jumps(read_wav(wav)), [])


# ------------------------------------------------------------------ F: 未録音の赤メーター（純関数）
from meter_state import HoldLatch, meter_unrecorded  # noqa: E402
from idle_speech import IdleSpeechWatcher  # noqa: E402


class MeterStateTest(unittest.TestCase):
    def test_table(self):
        f = meter_unrecorded
        #       録音中  一時停止 モニター開 開けない 話している
        self.assertTrue(f(False, False, True, False, True))      # 待機中に話す → 赤
        self.assertFalse(f(False, False, True, False, False))    # 待機中でも静か → 赤にしない
        self.assertTrue(f(True, True, True, False, True))        # 一時停止中に話す → 赤
        self.assertFalse(f(True, True, True, False, False))
        self.assertFalse(f(True, False, True, False, True))      # 録音中は緑のまま
        self.assertFalse(f(False, False, False, False, True))    # 待機中モニター OFF（見えていない）
        self.assertFalse(f(False, False, True, True, True))      # マイク使用中で開けない
        self.assertFalse(f(True, True, False, False, True))      # 一時停止中でもモニターが閉じていれば音は見えない

    def test_hold_latch_hysteresis(self):
        h = HoldLatch(0.5)
        self.assertFalse(h.update(0.0, False))
        self.assertTrue(h.update(1.0, True))          # 即時に赤
        self.assertTrue(h.update(1.2, False))         # 静かになっても保持
        self.assertTrue(h.update(1.49, False))
        self.assertFalse(h.update(1.5, False))        # 0.5 秒たったら戻る
        self.assertTrue(h.update(2.0, True))
        self.assertTrue(h.update(2.3, True))          # 話し続ける間はずっと赤
        self.assertTrue(h.update(2.6, False))         # 最後の True(2.3) から 0.5 秒未満
        self.assertFalse(h.update(2.8, False))
        h.update(3.0, True)
        h.reset()
        self.assertFalse(h.value)

    def test_speaking_now_is_immediate(self):
        w = IdleSpeechWatcher()
        for i in range(0, SR * 3, 1600):
            w.feed(C.noise(0.1, -70, i)[:1600])
        self.assertFalse(w.speaking_now())
        w.feed(C.sine(0.3, -15))                       # 0.3 秒の声だけで即時に True（1.5 秒待たない）
        self.assertTrue(w.speaking_now())
        self.assertLess(w.run_sec, 1.5)
        for i in range(10):
            w.feed(C.noise(0.1, -70, 100 + i))
        self.assertFalse(w.speaking_now())            # 静かになれば False


# ------------------------------------------------------------------ G: 右クリックメニューの項目（純関数）
from ui_menu import history_menu, is_text_row, rows_text_from, timeline_menu  # noqa: E402


class MenuItemsTest(unittest.TestCase):
    def rows(self):
        tl = Timeline(30)
        tl.feed(C.sine(150, -10))
        tl.add_draft(1, 5, "最初の仮認識")
        tl.add_draft(35, 40, "二つ目")
        return tl.rows()   # 0:仮認識 1:仮認識 2〜4:波形

    def test_text_row_items(self):
        rows = self.rows()
        keys = [i.key for i in timeline_menu(rows, 0)]
        self.assertEqual(keys, ["copy_part", "copy_all", "copy_from_here"])
        items = timeline_menu(rows, 1, selection="二つ")
        self.assertEqual([i.key for i in items], ["copy_selection", "copy_part", "copy_all", "copy_from_here"])
        self.assertEqual(items[0].payload, "二つ")
        self.assertEqual(items[1].payload, "二つ目")          # その行だけ（改行は付けない）

    def test_wave_silence_pause_rows_have_no_text_items(self):
        rows = self.rows()
        self.assertEqual([i.key for i in timeline_menu(rows, 3)], ["copy_all"])      # 波形だけの行: 全文コピーのみ
        tl = Timeline(30)
        tl.feed(C.sine(90, -10))
        tl.add_pause(30.0)
        tl.set_final([], 90)
        r2 = tl.rows()
        self.assertEqual(timeline_menu(r2, 0), [])                                    # 文字が一切ない → メニューは空（出さない）
        self.assertEqual([r.kind for r in r2][:2], ["silence", "pause"])
        self.assertEqual(timeline_menu(r2, 1), [])
        self.assertEqual(timeline_menu(rows, -1), [MenuItem_copy_all()])             # 行の外を右クリック
        self.assertEqual(timeline_menu([], 0), [])

    def test_text_from_here(self):
        rows = self.rows()
        self.assertEqual(rows_text_from(rows, 0), "最初の仮認識\n二つ目")
        self.assertEqual(rows_text_from(rows, 1), "二つ目")
        self.assertFalse(is_text_row(rows[3]))

    def test_history_items(self):
        blocks = ["一行目", "", "三行目"]
        keys = [i.key for i in history_menu(blocks, 0, can_play=True)]
        self.assertEqual(keys, ["copy_part", "copy_all", "copy_from_here", "-", "play_from"])
        self.assertEqual([i.key for i in history_menu(blocks, 0, can_play=False)], ["copy_part", "copy_all", "copy_from_here"])
        self.assertEqual([i.key for i in history_menu(blocks, 1, selection="x")], ["copy_selection", "copy_all"])  # 空行
        self.assertEqual(history_menu(["", " "], 0), [])


def MenuItem_copy_all():
    from ui_menu import MenuItem
    return MenuItem("copy_all", "全文コピー")



if __name__ == "__main__":
    unittest.main()
