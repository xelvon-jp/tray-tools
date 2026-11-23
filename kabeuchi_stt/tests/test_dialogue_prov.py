# -*- coding: utf-8 -*-
"""確定は常に清書の結果を待つ・自動配達しない・清書待ちの発話の波形（認識中）と、無音で波形を出さない条件
（フェイクの録音・認識・配達）。仮認識（黄色い暫定の文字）は廃止したので、そのテストは無い（無いことを確かめる）。"""
import threading
import time
import unittest

import numpy as np

import _common as C  # noqa: F401
from dialogue_session import DialogueSession
from test_dialogue_session import Deliver, FakeSession, quiet, speech, wait_until


class Gated:
    """清書（small）の偽物。gate が開くまで返らない。"""

    def __init__(self, text="せいしょ", gate=None):
        self.text, self.gate, self.calls = text, gate, 0

    def __call__(self, audio, context):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        return self.text if self.calls == 1 else self.text + "です"      # 同じ文の連続は捨てられるので、2回目は少し変える


def make(small=None, dialogue=None, deliver=None):
    s = {"silence_stop_peak_dbfs": -50.0, "paragraph_enabled": False,
         "dialogue": {"punctuate_on_commit": False, "idle_stop_sec": 0, **(dialogue or {})}}
    fs = FakeSession()
    dl = deliver or Deliver()
    events = []
    ds = DialogueSession(s, fs, small or Gated(), dl, on_event=lambda n, i: events.append((n, i)))
    return ds, fs, dl, events


def say_and_cut(fs):
    fs.feed(np.concatenate([quiet(1), speech(2.0), quiet(2, 1)]))      # 発話を1つ区切る


class NoProvisionalTest(unittest.TestCase):
    def test_provisional_is_gone(self):
        ds, fs, dl, _ = make()
        self.assertNotIn("provisional_recognizer", DialogueSession.__init__.__code__.co_varnames)
        for name in ("provisional_enabled", "adopt_provisional", "_prov_loop", "_make_provisional"):
            self.assertFalse(hasattr(ds, name), name)
        self.assertNotIn("provisional_count", ds.snapshot())
        ds.start()
        say_and_cut(fs)
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "せいしょ"))      # 下書きに入るのは清書の結果だけ
        ds.stop()


class CommitFlowTest(unittest.TestCase):
    def test_nothing_is_ever_delivered_without_commit(self):
        """絶対条件: 清書は下書きに溜まるだけ。確定操作なしに deliver_callback は呼ばれない。"""
        ds, fs, dl, _ = make(dialogue={"auto_commit": True, "message_gap_sec": 0.5})
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(2.0), quiet(10, 1), speech(2.0, 300), quiet(10, 2)]))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text.count("せいしょ") == 2))
        time.sleep(0.5)
        self.assertEqual(dl.texts, [])
        self.assertEqual(ds.buffer.history, [])
        ds.stop()
        self.assertEqual(dl.texts, [])

    def test_commit_always_waits_for_final(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        say_and_cut(fs)
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 1))
        t0 = time.time()
        ds.commit()
        time.sleep(0.6)
        self.assertEqual(dl.texts, [])                                    # 清書が揃うまで待っている
        gate.set()
        self.assertTrue(wait_until(lambda: dl.texts))
        self.assertEqual(dl.texts, ["せいしょ"])                          # 清書の内容が送られる
        self.assertLess(time.time() - t0, 4.0)
        ds.stop()

    def test_commit_flushes_the_utterance_in_progress_and_waits_for_it(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("途中の発話", gate))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(2.0)]))                  # まだ話している途中
        ds.commit()
        time.sleep(0.3)
        self.assertEqual(dl.texts, [])
        gate.set()
        self.assertTrue(wait_until(lambda: dl.texts))
        self.assertEqual(dl.texts, ["途中の発話"])
        ds.stop()

    def test_hold_and_discard_with_waves(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        say_and_cut(fs)
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 1))
        ds.hold(True)
        self.assertEqual(ds.pending_waves(), [])
        ds.hold(False)
        gate.set()
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "せいしょ"))
        self.assertEqual(ds.discard_draft(), "せいしょ")
        self.assertEqual(ds.buffer.draft_text, "")
        ds.stop()


class WaveTest(unittest.TestCase):
    def test_pending_waves_are_the_cut_utterances_only(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        self.assertEqual(ds.pending_waves(), [])
        fs.feed(np.concatenate([quiet(1), speech(2.5)]))                 # 話している途中: 認識中の行ではない（live_view が担当）
        self.assertEqual(ds.pending_waves(), [])
        self.assertTrue(ds.live_view()["speaking"])
        fs.feed(quiet(2, 1))                                              # 区切れて清書待ち（gate で止めている）
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 1))
        w = ds.pending_waves()
        self.assertEqual(len(w), 1)
        self.assertTrue(any(v > 0 for v in w[0]["env"]))
        self.assertLessEqual(len(w[0]["env"]), 24)                        # 小さな波形
        gate.set()
        self.assertTrue(wait_until(lambda: ds.pending_waves() == [] and ds.buffer.draft_text == "せいしょ"))
        ds.stop()


class NoWaveForSilenceTest(unittest.TestCase):
    """声のある発話にだけ波形を出す。無音・小さな物音・空の認識結果では出さない／消す。"""

    def test_silence_and_low_noise_never_produce_a_wave(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        fs.feed(quiet(5))
        self.assertEqual(ds.pending_waves(), [])
        self.assertFalse(ds.live_view()["speaking"])
        gate.set()
        ds.stop()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        fs.feed(C.noise(8, -55, 3))                                       # 最初から続く環境ノイズ（立ち上がりも「声」にしない）
        self.assertEqual(ds.pending_waves(), [])
        self.assertFalse(ds.live_view()["speaking"])
        self.assertEqual(ds.snapshot()["pending_recognitions"], 0)
        gate.set()
        ds.stop()

    def test_short_click_is_not_shown_and_not_recognized(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(0.15), quiet(0.5, 1)]))   # 最短長（0.3 秒）に満たない塊
        self.assertFalse(ds.live_view()["speaking"])                       # 話している途中としても出さない
        fs.feed(quiet(3, 2))
        self.assertEqual(ds.pending_waves(), [])
        self.assertEqual(ds.snapshot()["pending_recognitions"], 0)
        gate.set()
        ds.stop()

    def test_in_progress_live_only_while_voice_is_detected(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("せいしょ", gate))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.5)]))
        self.assertTrue(ds.live_view()["speaking"])                        # 声が出ている間
        fs.feed(quiet(0.8, 1))                                             # 無音が 0.6 秒を超えた（まだ区切る前）
        self.assertFalse(ds.live_view()["speaking"])
        fs.feed(speech(0.5, 300))                                          # また話し始めた
        self.assertTrue(ds.live_view()["speaking"])
        gate.set()
        ds.stop()

    def test_wave_disappears_when_recognition_is_empty(self):
        gate = threading.Event()
        ds, fs, dl, _ = make(small=Gated("", gate))                        # 清書が空文字
        ds.start()
        say_and_cut(fs)
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 1))
        self.assertEqual(len(ds.pending_waves()), 1)                       # 認識待ちの間は声のある発話として出る
        gate.set()
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 0))
        self.assertEqual(ds.pending_waves(), [])                           # 認識が空で終わったら消える
        self.assertEqual(ds.buffer.draft_text, "")
        ds.stop()

    def test_hallucination_final_is_dropped(self):
        ds, fs, dl, _ = make(small=Gated("ご視聴ありがとうございました"))
        ds.start()
        say_and_cut(fs)
        self.assertTrue(wait_until(lambda: ds.snapshot()["pending_recognitions"] == 0 and ds.utterances_total == 1))
        self.assertEqual(ds.buffer.draft_text, "")
        ds.stop()


if __name__ == "__main__":
    unittest.main()
