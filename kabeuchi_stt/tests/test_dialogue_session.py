# -*- coding: utf-8 -*-
"""AI対話モードのセッション（フェイクの録音・認識・配達）。マイクも実モデルも使わない。"""
import json
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from dialogue_session import DialogueSession
from replacements import Replacer
from settings import DEFAULTS, DIALOGUE_DEFAULTS, dialogue_config, load_settings

SR = C.SR


def quiet(sec, seed=0):
    return C.noise(sec, -70, seed)


def speech(sec=1.0, freq=220.0):
    return C.sine(sec, -12, freq)


def wait_until(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class FakeSession:
    """session.Session の代わり。feed() が録音スレッドの AudioTap 相当。"""

    def __init__(self):
        self.parts = []
        self.recording = False
        self.starts = self.stops = 0

    @property
    def is_recording(self):
        return self.recording

    def start(self, wav_path=None, live_enabled=None, extra_parts=()):
        self.parts = list(extra_parts)
        self.recording, self.starts = True, self.starts + 1
        self.live_enabled = live_enabled
        return Path("fake.wav")

    def stop(self):
        self.recording, self.stops = False, self.stops + 1
        return Path("fake.wav")

    def feed(self, x, block=1600):
        for i in range(0, len(x), block):
            for p in self.parts:
                p(x[i:i + block])


class Recorder:
    """フェイクの認識器。呼ばれるたびに texts から順に返す。"""

    def __init__(self, texts, delay=0.0):
        self.texts, self.delay, self.calls = list(texts), delay, []
        self.lock = threading.Lock()

    def __call__(self, audio, context):
        with self.lock:
            self.calls.append((len(audio), context))
            i = len(self.calls) - 1
        if self.delay:
            time.sleep(self.delay)
        t = self.texts[i] if i < len(self.texts) else f"発話{i}"
        if isinstance(t, Exception):
            raise t
        return t


class Deliver:
    def __init__(self, results=None, gate=None):
        self.results, self.gate, self.texts = list(results or []), gate, []

    def __call__(self, text):
        self.texts.append(text)
        if self.gate is not None:
            self.gate.wait(5)
        r = self.results.pop(0) if self.results else {"status": "delivered", "stage": "uia"}
        if isinstance(r, Exception):
            raise r
        return r


def make(texts=("こんにちは。", "元気ですか。"), deliver=None, dialogue=None, replacer=None, recognizer=None, **kw):
    # 既存の流れのテストは、句点・改行の規則と確定時の句点補いを切って、連結の素朴な結果を見る（規則は専用のテストで確かめる）
    s = {"silence_stop_peak_dbfs": -50.0, "paragraph_enabled": False,
         "dialogue": {"punctuate_on_commit": False, **(dialogue or {})}}
    fs = FakeSession()
    rec = recognizer or Recorder(texts)
    dl = deliver or Deliver()
    events = []
    ds = DialogueSession(s, fs, rec, dl, replacer=replacer, on_event=lambda n, i: events.append(n), **kw)
    return ds, fs, rec, dl, events


class FlowTest(unittest.TestCase):
    def test_utterances_become_draft_then_commit_delivers(self):
        ds, fs, rec, dl, ev = make(replacer=Replacer([{"from": "元気", "to": "げんき"}]))
        ds.start()
        self.assertTrue(ds.is_recording)
        self.assertFalse(fs.live_enabled)                       # 既存の逐次認識は切る
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2)]))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "こんにちは。げんきですか。"))   # 置換辞書つき
        self.assertEqual(rec.calls[1][1], "こんにちは。")        # 直前の発話が文脈で渡る
        self.assertEqual(dl.texts, [])                          # 確定するまで入力先へは出さない
        self.assertTrue(ds.commit())
        self.assertTrue(wait_until(lambda: dl.texts and not ds.buffer.delivering))
        self.assertEqual(dl.texts, ["こんにちは。げんきですか。"])
        self.assertEqual(ds.buffer.history[0].status, "delivered")
        self.assertTrue(ds.is_recording)                        # 確定のあとも録音は続く
        ds.stop()
        self.assertFalse(ds.is_recording)

    def test_commit_includes_utterance_in_progress(self):
        ds, fs, rec, dl, _ = make(texts=["話している途中"])
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0)]))       # まだ区切られていない
        self.assertEqual(rec.calls, [])
        ds.commit()
        self.assertTrue(wait_until(lambda: dl.texts))
        self.assertEqual(dl.texts, ["話している途中"])
        ds.stop()

    def test_failure_keeps_text_and_next_commit_is_retry(self):
        dl = Deliver([{"status": "undelivered", "reason": "窓が見つかりません"}])
        ds, fs, rec, dl, ev = make(texts=["伝えたい内容"], deliver=dl)
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text)
        ds.commit()
        self.assertTrue(wait_until(lambda: ds.buffer.undelivered_count == 1))
        self.assertEqual(ds.buffer.undelivered[0].reason, "窓が見つかりません")
        self.assertEqual(ds.buffer.draft_text, "")
        self.assertTrue(ds.is_recording)                        # 録音は続く
        self.assertTrue(ds.snapshot()["next_commit_is_retry"])
        ds.commit()                                             # 再試行
        self.assertTrue(wait_until(lambda: len(dl.texts) == 2 and not ds.buffer.delivering))
        self.assertEqual(ds.buffer.undelivered_count, 0)
        self.assertEqual(dl.texts, ["伝えたい内容", "伝えたい内容"])
        self.assertIn("undelivered", ev)
        ds.stop()

    def test_deliver_exception_is_contained(self):
        dl = Deliver([RuntimeError("worker died")])
        ds, fs, rec, dl, _ = make(texts=["守る", "続き"], deliver=dl)
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text)
        ds.commit()
        self.assertTrue(wait_until(lambda: ds.buffer.undelivered_count == 1))
        self.assertIn("worker died", ds.buffer.undelivered[0].reason)
        fs.feed(np.concatenate([speech(1.0, 300), quiet(2, 3)]))    # 認識は止まらない
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "続き"))
        ds.stop()

    def test_retry_delivers_oldest_first_one_by_one(self):
        dl = Deliver([{"status": "undelivered", "reason": "x"}])
        ds, fs, rec, dl, _ = make(texts=["一", "二", "三"], deliver=dl)
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text == "一")
        ds.commit()
        wait_until(lambda: ds.buffer.undelivered_count == 1)
        # 続けて話した分は、確定のたびに列へ並ぶ（自動確定相当）
        for i, f in enumerate((300, 400)):
            fs.feed(np.concatenate([speech(1.0, f), quiet(2, 5 + i)]))
            wait_until(lambda: ds.buffer.draft_text)
            ds.buffer.enqueue_draft()
        self.assertEqual([m.text for m in ds.buffer.undelivered], ["一", "二", "三"])
        ds.commit()    # 再試行 → 成功したら次へ進む
        self.assertTrue(wait_until(lambda: ds.buffer.undelivered_count == 0))
        self.assertEqual(dl.texts, ["一", "一", "二", "三"])
        ds.stop()

    def test_merge_undelivered_setting(self):
        dl = Deliver([{"status": "undelivered", "reason": "x"}])
        ds, fs, rec, dl, _ = make(texts=["あ", "い"], deliver=dl, dialogue={"merge_undelivered": True})
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text == "あ")
        ds.commit()
        wait_until(lambda: ds.buffer.undelivered_count == 1)
        fs.feed(np.concatenate([speech(1.0, 300), quiet(2, 2)]))
        wait_until(lambda: ds.buffer.draft_text == "い")
        ds.buffer.enqueue_draft()
        ds.commit()
        self.assertTrue(wait_until(lambda: len(dl.texts) == 2 and not ds.buffer.delivering))
        self.assertEqual(dl.texts, ["あ", "あ\nい"])
        ds.stop()


class RobustnessTest(unittest.TestCase):
    def test_recognizer_error_does_not_kill_session(self):
        ds, fs, rec, dl, ev = make(recognizer=Recorder([RuntimeError("model broke"), "二つ目"]))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2)]))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "二つ目"))
        self.assertEqual(ds.recognition_errors, 1)
        self.assertIn("recognition_error", ev)
        ds.stop()

    def test_audio_callback_is_fast_even_if_recognition_is_slow(self):
        ds, fs, rec, dl, _ = make(recognizer=Recorder(["遅い"] * 5, delay=0.5))
        ds.start()
        worst = 0.0
        x = np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2), speech(1, 400), quiet(2, 3)])
        for i in range(0, len(x), 1600):
            t0 = time.perf_counter()
            fs.parts[0](x[i:i + 1600])
            worst = max(worst, time.perf_counter() - t0)
        self.assertLess(worst, 0.1)
        self.assertGreater(ds.pending_recognitions, 0)          # 認識は遅れて溜まっている
        ds.stop(wait=10)
        self.assertEqual(len(rec.calls), 3)                     # 停止時に残りも認識される
        self.assertEqual(ds.buffer.draft_text, "遅い遅い遅い")

    def test_audio_callback_never_raises(self):
        ds, fs, rec, dl, _ = make()
        ds.start()
        for bad in (None, np.zeros(0, dtype=np.int16), "x", np.array([1.5])):
            try:
                fs.parts[0](bad)
            except Exception as e:   # noqa: BLE001
                self.fail(f"録音スレッドへ例外が出た: {e}")
        ds.stop()

    def test_delivery_slowness_does_not_block_recognition(self):
        gate = threading.Event()
        dl = Deliver(gate=gate)
        ds, fs, rec, dl, _ = make(texts=["送る", "次の話"], deliver=dl)
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text == "送る")
        ds.commit()
        wait_until(lambda: dl.texts)
        self.assertTrue(ds.buffer.delivering)
        fs.feed(np.concatenate([speech(1.0, 300), quiet(2, 2)]))   # 配達中でも次の発話は認識される
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "次の話"))
        ds.commit()                                                 # 配達中の確定は二重配達しない
        time.sleep(0.2)
        self.assertEqual(dl.texts, ["送る"])
        gate.set()
        wait_until(lambda: not ds.buffer.delivering)
        ds.stop()
        self.assertEqual(ds.buffer.history[0].status, "delivered")
        self.assertEqual(ds.buffer.draft_text, "次の話")           # 終了後も下書きが残り、end_report で分かる
        self.assertEqual(ds.end_report()["draft_text"], "次の話")

    def test_start_twice_and_stop_idempotent(self):
        ds, fs, *_ = make()
        ds.start()
        with self.assertRaises(RuntimeError):
            ds.start()
        r1 = ds.stop()
        r2 = ds.stop()
        self.assertEqual(fs.stops, 1)
        self.assertEqual(r1["undelivered_count"], r2["undelivered_count"])
        self.assertFalse(ds.commit())      # 終了後の確定は受け付けない

    def test_start_failure_leaves_clean_state(self):
        ds, fs, *_ = make()

        def boom(*a, **k):
            raise OSError("mic busy")
        fs.start = boom
        with self.assertRaises(OSError):
            ds.start()
        self.assertFalse(ds.is_active)


class HoldTest(unittest.TestCase):
    def test_hold_stops_intake(self):
        ds, fs, rec, dl, _ = make(texts=["入る", "入らない", "再開後"])
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        wait_until(lambda: ds.buffer.draft_text == "入る")
        ds.hold(True)
        self.assertTrue(ds.is_held)
        fs.feed(np.concatenate([speech(1.0, 300), quiet(2, 2)]))
        time.sleep(0.2)
        self.assertEqual(len(rec.calls), 1)                     # 保留中は認識もしない
        ds.hold(False)
        fs.feed(np.concatenate([speech(1.0, 400), quiet(2, 3)]))
        self.assertTrue(wait_until(lambda: len(rec.calls) == 2))
        self.assertEqual(ds.toggle_hold(), True)
        ds.stop()

    def test_hold_flushes_utterance_in_progress_to_recognition(self):
        ds, fs, rec, dl, _ = make(texts=["途中の発話"])
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0)]))
        ds.hold(True)                                           # 話している途中: 捨てずに区切って認識へ
        self.assertTrue(wait_until(lambda: len(rec.calls) == 1))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "途中の発話"))   # 保留中でも、保留前に話した分は入る
        ds.hold(False)
        fs.feed(quiet(3, 1))
        time.sleep(0.2)
        self.assertEqual(len(rec.calls), 1)
        ds.stop()

    def test_duplicate_sentence_dropped_within_window(self):
        ds, fs, rec, dl, _ = make(texts=["同じ文です。", "同じ文です", "別の文です。"])
        ds.start()
        for i, seed in enumerate((0, 1, 2)):
            fs.feed(np.concatenate([quiet(1, seed), speech(1.0, 200 + 50 * i), quiet(2, seed + 5)]))
            self.assertTrue(wait_until(lambda n=i + 1: len(rec.calls) == n))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text.endswith("別の文です。")))
        self.assertEqual(ds.buffer.draft_text.count("同じ文です"), 1)
        self.assertEqual(ds.dropped_dup, 1)
        ds.stop()

    def test_repeated_sentence_inside_one_result_collapsed(self):
        ds, fs, rec, dl, _ = make(texts=["ありがとう。ありがとう。ありがとう。ありがとう。次です。"])
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text.replace("\n", "") == "ありがとう。次です。"))
        self.assertEqual(ds.collapsed, 3)
        ds.stop()


class AutoTest(unittest.TestCase):
    def test_never_commits_or_delivers_without_user_operation(self):
        """絶対条件: 下書きは、利用者の確定操作なしに宛先へ届かない（auto_commit が設定に書かれていても無視）。"""
        ds, fs, rec, dl, _ = make(texts=["確定しない", "これも"], dialogue={"auto_commit": True, "message_gap_sec": 1.0})
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(8, 1), speech(1.0, 300), quiet(8, 2)]))
        self.assertTrue(wait_until(lambda: len(rec.calls) == 2 and ds.buffer.draft_text))
        time.sleep(0.3)
        self.assertEqual(dl.texts, [])                       # deliver_callback は一度も呼ばれない
        self.assertEqual(ds.buffer.history, [])
        self.assertTrue(ds.buffer.has_draft)                 # 下書きに溜まるだけ
        ds.stop()
        self.assertEqual(dl.texts, [])                       # 終了しても自動では届けない

    def test_auto_commit_setting_is_not_a_default(self):
        self.assertNotIn("auto_commit", DEFAULTS["dialogue"])
        self.assertNotIn("message_gap_sec", DEFAULTS["dialogue"])

    def test_idle_auto_stop(self):
        ds, fs, rec, dl, ev = make(dialogue={"idle_stop_sec": 3})
        ds.start()
        fs.feed(quiet(5.0))
        self.assertTrue(wait_until(lambda: not ds.is_active))
        self.assertIn("idle_stop", ev)
        self.assertTrue(wait_until(lambda: "ended" in ev))
        self.assertEqual(fs.stops, 1)
        self.assertFalse(ds.is_recording)
        ds.stop()
        self.assertEqual(fs.stops, 1)

    def test_idle_stop_zero_disables(self):
        ds, fs, *_ = make(dialogue={"idle_stop_sec": 0})
        ds.start()
        fs.feed(quiet(30.0))
        time.sleep(0.2)
        self.assertTrue(ds.is_active)
        ds.stop()


class FinalizeOnStopTest(unittest.TestCase):
    """D1: 終了のとき、清書が終わっていない発話は、清書が済んでから下書きに残す（最大 wait 秒待つ。仮の表示は出さない）。"""

    def test_stop_waits_for_pending_recognition_then_keeps_it_in_the_draft(self):
        ds, fs, rec, dl, _ = make(recognizer=Recorder(["遅い一つ目", "遅い二つ目"], delay=0.4))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2)]))
        self.assertGreater(ds.pending_recognitions, 0)
        rep = ds.stop(wait=10)
        self.assertEqual(rep["pending_recognitions"], 0)                    # 清書が済んでから戻る
        self.assertEqual(ds.buffer.draft_text, "遅い一つ目遅い二つ目")
        self.assertEqual(rep["draft_text"], "遅い一つ目遅い二つ目")
        self.assertTrue(rep["needs_attention"])

    def test_stop_gives_up_after_wait_and_reports_the_pending_count(self):
        ds, fs, rec, dl, _ = make(recognizer=Recorder(["とても遅い"], delay=1.5))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        rep = ds.stop(wait=0.3)
        self.assertGreater(rep["pending_recognitions"], 0)                  # 終わっていない旨が分かる
        self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "とても遅い", 5))   # 終わり次第、下書きへ入る（捨てない）

    def test_next_session_starts_with_the_leftover_draft_as_final_text(self):
        ds, fs, rec, dl, _ = make(texts=["持ち越す"])
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
        ds.stop()
        carried = ds.buffer
        ds2 = DialogueSession({"silence_stop_peak_dbfs": -50.0, "paragraph_enabled": False,
                               "dialogue": {"punctuate_on_commit": False}}, FakeSession(), Recorder([]), Deliver(), buffer=carried)
        ds2.start()
        self.assertEqual(ds2.buffer.draft_final_text, "持ち越す")             # 白い確定前の下書きとして出る（仮の表示は無い）
        self.assertFalse(hasattr(ds2.buffer, "provisional_items"))
        ds2.stop()


class LiveViewTest(unittest.TestCase):
    """喋っているあいだだけ動く波形と長さ（live_view）。無音のあいだは止まる。"""

    def test_speaking_then_silence(self):
        ds, fs, rec, dl, _ = make()
        ds.start()
        self.assertFalse(ds.live_view()["speaking"])
        fs.feed(quiet(1))
        self.assertFalse(ds.live_view()["speaking"])
        fs.feed(speech(1.5))
        v = ds.live_view()
        self.assertTrue(v["speaking"])
        self.assertGreater(v["seconds"], 1.0)
        self.assertEqual(len(v["env"]), ds.LIVE_BARS)
        self.assertGreater(max(v["env"]), 0)
        fs.feed(quiet(0.8, 3))                                              # 無音が続くと、止まる（長さも出さない）
        self.assertFalse(ds.live_view()["speaking"])
        ds.stop()

    def test_pending_waves_has_only_utterances_that_were_cut(self):
        ds, fs, rec, dl, _ = make(recognizer=Recorder(["a"], delay=0.8))
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0)]))                    # 話している途中: 波形の行は出ない（live_view の役目）
        self.assertEqual(ds.pending_waves(), [])
        self.assertTrue(ds.live_view()["speaking"])
        fs.feed(quiet(2, 1))                                                # 区切れた: 「認識中」の行になる
        w = ds.pending_waves()
        self.assertEqual(len(w), 1)
        self.assertTrue(w[0]["env"])
        self.assertTrue(wait_until(lambda: ds.pending_waves() == [] and ds.buffer.draft_text == "a"))
        ds.stop()

    def test_held_has_no_live_or_waves(self):
        ds, fs, rec, dl, _ = make()
        ds.start()
        fs.feed(np.concatenate([quiet(1), speech(1.0)]))
        ds.hold(True)
        self.assertFalse(ds.live_view()["speaking"])
        self.assertEqual(ds.pending_waves(), [])
        ds.stop()


class RealSessionTest(unittest.TestCase):
    """本物の session.Session（フェイクのマイク）に載せて、extra_parts 経由で区切り・WAV 録音が両立すること。"""

    def test_with_real_session_and_fake_mic(self):
        from session import Session
        from test_core_runtime import FakeSD
        with tempfile.TemporaryDirectory() as d:
            s = {"out_dir": d, "row_sec": 30, "silence_stop_sec": 0, "live_enabled": True,
                 "silence_stop_peak_dbfs": -50.0, "paragraph_enabled": False,
                 "dialogue": {"idle_stop_sec": 0, "punctuate_on_commit": False}}
            src = np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2)])
            sess = Session(s, Replacer(), sd=FakeSD(src, speed=20.0),
                           live_factory=lambda seg, st: (_ for _ in ()).throw(AssertionError("逐次認識は起動しない")))
            rec = Recorder(["一つ目。", "二つ目。"])
            ds = DialogueSession(s, sess, rec, Deliver())
            wav = ds.start(Path(d) / "dlg.wav")
            self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "一つ目。二つ目。", timeout=10))
            self.assertTrue(ds.is_recording)
            rep = ds.stop()
            with wave.open(str(wav)) as w:
                self.assertGreater(w.getnframes() / SR, 5.5)    # 録音（WAV）も残る（止めた時点まで）
            self.assertEqual(rep["draft_text"], "一つ目。二つ目。")
            self.assertTrue(rep["needs_attention"])
            self.assertEqual(sess.hub.is_open, False)


class SettingsTest(unittest.TestCase):
    def test_defaults_present(self):
        d = DEFAULTS["dialogue"]
        for k in ("worker_python", "utterance_gap_sec", "dest_record_only", "send_after_deliver", "punctuate_on_commit", "headset_button", "headset_mode", "headset_vid_pid", "existing_text", "keep_idle_foreground", "return_focus",
                  "quiet_input_sec", "last_target"):
            self.assertIn(k, d)
        self.assertNotIn("enabled", d)                                       # AI対話モードは常に有効（有効・無効の設定は無い）
        self.assertEqual((d["utterance_gap_sec"], d["existing_text"]), (1.2, "append"))
        self.assertTrue(d["punctuate_on_commit"])
        for gone in ("provisional", "provisional_interval_sec", "provisional_wait_sec", "dest_clipboard"):
            self.assertNotIn(gone, d)                                        # 仮認識・クリップボードコピーは廃止
        self.assertTrue(DEFAULTS["finalize_on_end"])                         # セッション終了時の録音全体の清書は既定ON
        self.assertFalse(d["send_after_deliver"])
        self.assertFalse(d["headset_button"])

    def test_partial_section_is_filled_and_not_shared(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "settings.json"
            p.write_text(json.dumps({"dialogue": {"enabled": True, "last_target": "x"}}), encoding="utf-8")
            s = load_settings(p)
            self.assertEqual(s["dialogue"]["last_target"], "x")
            self.assertEqual(s["dialogue"]["utterance_gap_sec"], 1.2)
            s["dialogue"]["utterance_gap_sec"] = 9
            self.assertEqual(DIALOGUE_DEFAULTS["utterance_gap_sec"], 1.2)       # 既定の dict を書き換えない
            self.assertEqual(load_settings(Path(t) / "none.json", create=False)["dialogue"]["utterance_gap_sec"], 1.2)
        self.assertEqual(dialogue_config({})["idle_stop_sec"], 180)
        self.assertEqual(dialogue_config({"dialogue": {"idle_stop_sec": 6}})["idle_stop_sec"], 6)

    def test_old_removed_keys_are_ignored_without_error(self):
        """古い設定ファイルに残った廃止済みのキーは、読み込みで無視する（落ちない・値も使わない）。"""
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "settings.json"
            p.write_text(json.dumps({"ui_layout": "unified", "live_model": "tiny", "auto_copy": True, "unified_mode": "wall",
                                     "dialogue": {"provisional": True, "provisional_interval_sec": 2.0, "dest_clipboard": True,
                                                  "last_target": "x"}}), encoding="utf-8")
            s = load_settings(p)
            self.assertEqual(s["dialogue"]["last_target"], "x")
            c = dialogue_config(s)
            self.assertFalse(c["dest_record_only"])                          # 古い dest_clipboard は読み替えない（無視する）
            ds, fs, rec, dl, _ = make(dialogue=s["dialogue"])                # 古いキーがあっても、セッションは普通に動く
            ds.start()
            fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1)]))
            self.assertTrue(wait_until(lambda: ds.buffer.draft_text == "こんにちは。"))
            ds.stop()


if __name__ == "__main__":
    unittest.main()
