# -*- coding: utf-8 -*-
"""AI対話モードの結線（DialogueController）の統合テスト。フェイクの録音・認識器・WorkerClient・ヘッドセット。
マイク・ワーカー・前面化・実モデルは使わない。"""
import tempfile
import threading
import time
import unittest

import numpy as np

import _common as C
import dialogue_profile as dp
import ui_dialogue_logic as L
from dialogue_controller import DialogueController
from dialogue_headset import HeadsetResult
from test_dialogue_session import FakeSession, Recorder, quiet, speech, wait_until

PROFILE = {"schema": "dialogue-v1", "name": "相談窓", "process_name": "app.exe", "window_class": "Cls",
           "window_title_contains": "相談", "captured_title": "相談 - app"}


class FakeClient:
    """WorkerClient の代わり。deliver の結果は results から順に返す（尽きたら delivered）。"""

    def __init__(self, results=None, pick_result=None, resolve_result=None):
        self.results = list(results or [])
        self.pick_result = pick_result or {"status": "picked", "profile": {**PROFILE, "name": ""}, "warnings": []}
        self.resolve_result = resolve_result or {"status": "ok", "hwnd": 77, "candidates": []}
        self.held_hwnd = 0
        self.delivered, self.options, self.picks = [], [], []
        self.shutdowns = 0
        self.pick_gate = None

    def resolve(self, profile, hwnd=0):
        self.held_hwnd = int(self.resolve_result.get("hwnd") or 0) if self.resolve_result.get("status") == "ok" else 0
        return self.resolve_result

    def pick(self, name="", timeout=30.0, exclude_roots=()):
        self.picks.append((name, timeout, tuple(exclude_roots)))
        if self.pick_gate is not None:
            self.pick_gate.wait(5)
        return self.pick_result

    def deliver(self, profile, text, hwnd=None, options=None, grant_foreground=True):
        self.delivered.append((profile.get("name"), text))
        self.options.append(options)
        r = self.results.pop(0) if self.results else {"status": "delivered", "stage": "uia"}
        if isinstance(r, Exception):
            raise r
        return r

    def shutdown(self):
        self.shutdowns += 1


class FakeHeadset:
    def __init__(self, callback, ok=True):
        self.callback, self.ok = callback, ok
        self.state, self.starts, self.stops = "stopped", 0, 0

    def start(self):
        self.starts += 1
        self.state = "active" if self.ok else "failed"
        return HeadsetResult(True) if self.ok else HeadsetResult(False, "他のアプリが使用中")

    def stop(self):
        self.stops += 1
        self.state = "stopped"

    def press(self):
        self.callback()


class Rig:
    def __init__(self, dialogue=None, results=None, texts=("こんにちは。", "元気ですか。"), preset=True,
                 headset_ok=True, **client_kw):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = self.tmp.name
        self.settings = {"silence_stop_peak_dbfs": -50.0, "paragraph_enabled": False,
                         "dialogue": {"headset_button": True, "punctuate_on_commit": False, **(dialogue or {})}}
        if preset:
            dp.save_preset(dict(PROFILE), self.base)
            self.settings["dialogue"]["last_target"] = "相談窓"
        self.client = FakeClient(results, **client_kw)
        self.headset = None
        self.headset_ok = headset_ok
        self.fs = FakeSession()
        self.events = []
        self.lock = threading.Lock()
        self.rec = Recorder(texts)

        def hs_factory(cb):
            self.headset = FakeHeadset(cb, self.headset_ok)
            return self.headset

        self.ctl = DialogueController(
            self.settings, self.fs, self.rec, client_factory=lambda: self.client, headset_factory=hs_factory,
            base_dir=self.base, on_event=self._ev, grant=lambda: None)

    def _ev(self, n, i):
        with self.lock:
            self.events.append(n)

    def names(self):
        with self.lock:
            return list(self.events)

    def say(self):
        self.fs.feed(np.concatenate([quiet(1), speech(1.0), quiet(2, 1), speech(1.0, 300), quiet(2, 2)]))
        assert wait_until(lambda: self.ctl.ds.buffer.draft_text == "こんにちは。元気ですか。"), self.ctl.ds.buffer.draft_text

    def close(self):
        self.ctl.shutdown()
        self.tmp.cleanup()


class FlowTest(unittest.TestCase):
    def test_start_say_commit_deliver_stop(self):
        r = Rig()
        try:
            self.assertEqual(r.ctl.profile["name"], "相談窓")          # last_target を復元
            r.ctl.start()
            self.assertTrue(r.ctl.active)
            self.assertEqual(r.headset.state, "active")             # ヘッドセット受付開始
            self.assertTrue(wait_until(lambda: r.ctl.target_state == L.TG_HELD))
            self.assertEqual(r.client.held_hwnd, 77)
            r.say()
            self.assertEqual(r.client.delivered, [])                 # 確定するまで入力先へは出さない
            self.assertEqual(r.ctl.commit("window"), L.PRESS_COMMIT)
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.client.delivered[0], ("相談窓", "こんにちは。元気ですか。"))
            self.assertEqual(r.client.options[0]["existing_text"], "append")
            self.assertEqual(r.ctl.ds.buffer.undelivered_count, 0)
            self.assertIn("delivered", r.names())
            rep = r.ctl.stop()
            self.assertFalse(rep["needs_attention"])
            self.assertEqual((r.headset.stops >= 1, r.client.shutdowns), (True, 1))   # 必ず止める
            self.assertFalse(r.ctl.active)
            self.assertIn("stopped", r.names())
        finally:
            r.close()

    def test_undelivered_then_headset_press_retries_then_delivers(self):
        r = Rig(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        try:
            r.ctl.start()
            r.say()
            r.headset.press()                                         # 1回目の押下 = 確定
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            self.assertEqual(L.view_state(r.ctl.snapshot()).key, L.ST_UNDELIVERED)
            self.assertEqual(L.press_action(r.ctl.snapshot()), L.PRESS_RETRY)
            r.headset.press()                                         # 2回目の押下 = 再試行（新規の確定ではない）
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 0 and not r.ctl.ds.buffer.delivering))
            self.assertEqual([t for _n, t in r.client.delivered], ["こんにちは。元気ですか。"] * 2)
            self.assertEqual(r.ctl.ds.buffer.history[0].status, "delivered")
            self.assertEqual(len(r.ctl.ds.buffer.history), 1)
            self.assertFalse(r.ctl.stop()["needs_attention"])
        finally:
            r.close()

    def test_worker_exception_becomes_undelivered_not_lost(self):
        r = Rig(results=[RuntimeError("pipe")])
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit("hotkey")
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            self.assertIn("pipe", r.ctl.ds.buffer.undelivered[0].reason)
            rep = r.ctl.stop()
            self.assertTrue(rep["needs_attention"])                 # 終了時に未配達の確認へ
            self.assertEqual(rep["undelivered_count"], 1)
            ops = r.ctl.ops()
            self.assertIn("こんにちは。", r.ctl.ds.buffer.copy_text())     # セッション後も、未配達の文は選択してコピーできる（自動ではコピーしない）
            self.assertEqual(ops.resolve_end("discard")["discarded"], 1)
        finally:
            r.close()

    def test_no_target_keeps_text_as_undelivered(self):
        r = Rig(preset=False)
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            self.assertEqual(r.ctl.ds.buffer.undelivered[0].reason, "no_target")
            self.assertEqual(L.reason_text("no_target"), "宛先が未設定です")
            self.assertEqual(r.client.delivered, [])
        finally:
            r.close()

    def test_headset_not_started_when_disabled_and_failure_reported(self):
        r = Rig(dialogue={"headset_button": False})
        try:
            r.ctl.start()
            self.assertIsNone(r.headset)
            self.assertEqual(r.ctl.headset_state(), "stopped")
        finally:
            r.close()
        r = Rig(headset_ok=False)
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.headset_state(), "failed")
            self.assertIn("他のアプリ", r.ctl.headset_reason)
            self.assertIn("headset_failed", r.names())
            self.assertTrue(r.ctl.active)                            # 取得失敗でもセッションは動く
        finally:
            r.close()

    def test_commit_busy_and_ignore(self):
        r = Rig()
        try:
            self.assertEqual(r.ctl.commit("window"), L.PRESS_IGNORE)   # セッション外
            gate = threading.Event()
            orig = r.client.deliver

            def slow(*a, **k):
                gate.wait(5)
                return orig(*a, **k)
            r.client.deliver = slow
            r.ctl.start()
            r.say()
            r.ctl.commit()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.delivering))
            self.assertEqual(r.ctl.commit("headset"), L.PRESS_BUSY)    # 二重配達しない
            gate.set()
            self.assertTrue(wait_until(lambda: not r.ctl.ds.buffer.delivering))
            self.assertEqual(len(r.client.delivered), 1)
        finally:
            r.close()

    def test_undelivered_never_copies_to_the_clipboard_by_itself(self):
        """クリップボードへ入れるのは、利用者が明示的にコピーしたときだけ（自動コピーは廃止）。"""
        r = Rig(results=[{"status": "undelivered", "reason": "x"}], dialogue={"copy_undelivered_to_clipboard": True})
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit()
            self.assertTrue(wait_until(lambda: "undelivered" in r.names()))
            self.assertNotIn("copy_request", r.names())
        finally:
            r.close()

    def test_second_session_carries_undelivered_over(self):
        r = Rig(results=[{"status": "undelivered", "reason": "x"}])
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            r.ctl.stop()
            r.ctl.start()                                              # 「あとで（残す）」の持ち越し
            self.assertEqual(r.ctl.ds.buffer.undelivered_count, 1)
            self.assertEqual(r.client.shutdowns, 1)
            r.ctl.stop()
            self.assertEqual(r.client.shutdowns, 2)
        finally:
            r.close()

    def test_shutdown_is_safe_to_repeat_and_stops_everything(self):
        r = Rig()
        try:
            r.ctl.start()
            r.ctl.shutdown()
            r.ctl.shutdown()
            self.assertFalse(r.ctl.active)
            self.assertGreaterEqual(r.headset.stops, 1)
            self.assertGreaterEqual(r.client.shutdowns, 1)
            self.assertEqual(r.fs.stops, 1)
            with self.assertRaises(RuntimeError):
                r.ctl.start()
        finally:
            r.close()


class TargetTest(unittest.TestCase):
    def test_pick_and_save_with_validation(self):
        r = Rig(preset=False)
        try:
            ev = []
            r.ctl.set_event_sink(lambda n, i: ev.append((n, i)))
            self.assertTrue(r.ctl.pick_async("", exclude_roots=[5]))
            self.assertTrue(wait_until(lambda: any(n == "picked" for n, _ in ev)))
            self.assertEqual(r.client.picks[0][2], (5,))
            self.assertEqual(r.client.shutdowns, 1)                   # セッション外で起こしたワーカーは止める
            for bad in ("", "a/b", "CON"):
                with self.assertRaises(ValueError):
                    r.ctl.save_picked(bad)
            prof = r.ctl.save_picked("相談窓")
            self.assertEqual(prof["name"], "相談窓")
            self.assertEqual(r.ctl.preset_names(), ["相談窓"])
            self.assertEqual(r.settings["dialogue"]["last_target"], "相談窓")
            # 同名は黙って上書きしない
            r.ctl.picked = {"profile": {**PROFILE, "name": ""}}
            with self.assertRaises(FileExistsError):
                r.ctl.save_picked("相談窓")
            r.ctl.save_picked("相談窓", overwrite=True)
            with self.assertRaises(ValueError):                       # 選んでいない
                r.ctl.save_picked("別")
        finally:
            r.close()

    def test_pick_failure_event_and_no_double_pick(self):
        gate = threading.Event()
        r = Rig(preset=False, pick_result={"status": "timeout"})
        r.client.pick_gate = gate
        try:
            ev = []
            r.ctl.set_event_sink(lambda n, i: ev.append((n, i)))
            self.assertTrue(r.ctl.pick_async(""))
            self.assertTrue(r.ctl.picking)
            self.assertGreater(r.ctl.pick_remaining(), 0)
            self.assertFalse(r.ctl.pick_async(""))                    # 選択中は二重に始めない
            gate.set()
            self.assertTrue(wait_until(lambda: any(n == "pick_failed" for n, _ in ev)))
            self.assertIn("クリックされません", [i for n, i in ev if n == "pick_failed"][0]["text"])
            self.assertTrue(wait_until(lambda: not r.ctl.picking))
        finally:
            r.close()

    def test_select_and_delete_preset_and_resolve_states(self):
        r = Rig(resolve_result={"status": "none", "hwnd": 0, "candidates": []})
        try:
            r.ctl.start()
            self.assertTrue(wait_until(lambda: r.ctl.target_state == L.TG_NOT_FOUND))
            self.assertIn("見つかりません", r.ctl.target_view().status_text)
            self.assertEqual(r.client.held_hwnd, 0)
            self.assertFalse(r.ctl.select_preset("存在しない"))
            self.assertTrue(r.ctl.delete_preset("相談窓"))
            self.assertIsNone(r.ctl.profile)
            self.assertEqual(r.ctl.target_view().state, L.TG_NONE)
            self.assertEqual(r.settings["dialogue"]["last_target"], "")
        finally:
            r.close()


class RecordOnlyTest(unittest.TestCase):
    """宛先「なし（記録だけ）」: 何も届けず・クリップボードにも入れず、確定済みとして記録に残して下書きから消す。"""

    def test_record_only_keeps_history_and_never_touches_worker_or_clipboard(self):
        r = Rig(dialogue={"dest_record_only": True})
        try:
            self.assertTrue(r.ctl.record_only)
            self.assertFalse(hasattr(r.ctl, "copy_only"))
            r.ctl.start()
            r.say()
            self.assertEqual(r.ctl.commit("window"), L.PRESS_COMMIT)
            self.assertTrue(wait_until(lambda: "delivered" in r.names()))
            buf = r.ctl.ds.buffer
            self.assertEqual(r.client.delivered, [])                       # 入力欄へは何も書かない
            self.assertEqual(buf.draft_text, "")                           # 下書きからは消える
            m = buf.history[0]
            self.assertEqual((m.status, m.stage, m.text), ("delivered", "record", "こんにちは。元気ですか。"))
            self.assertEqual(buf.undelivered_count, 0)
            self.assertEqual(r.client.held_hwnd, 0)                        # 窓も探さない
            names = r.names()
            self.assertIn("delivered", names)
            self.assertNotIn("clipboard_copy", names)                      # クリップボードへのコピー要求も出さない
            self.assertFalse(r.ctl.stop()["needs_attention"])
        finally:
            r.close()

    def test_toggle_saves_the_new_key_and_old_key_is_ignored(self):
        r = Rig(dialogue={"dest_clipboard": True})                         # 古い設定キーは無視（宛先「なし」にはならない）
        try:
            self.assertFalse(r.ctl.record_only)
            r.ctl.set_record_only(True)
            self.assertTrue(r.settings["dialogue"]["dest_record_only"])
            r.ctl.set_record_only(False)
            self.assertFalse(r.ctl.record_only)
        finally:
            r.close()

    def test_text_transform_is_not_applied_when_recording_only(self):
        r = Rig(dialogue={"dest_record_only": True})
        try:
            r.ctl.text_transform = lambda t: "前置き：" + t
            r.ctl.start()
            r.say()
            r.ctl.commit("window")
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.history and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.ctl.ds.buffer.history[0].text, "こんにちは。元気ですか。")
        finally:
            r.close()


class _Backend:
    """HeadsetButton のバックエンド（Win32 を呼ばない）。on_press() が WM_HOTKEY の受信に相当。"""

    def __init__(self, result=None):
        self.result = result or HeadsetResult(True)
        self.on_press = None
        self.exit_reason = ""

    def start(self, on_press, on_exit):
        self.on_press = on_press
        return self.result

    def stop(self):
        pass


class HeadsetEndToEndTest(unittest.TestCase):
    """本物の HeadsetButton（フェイクのバックエンド）→ コントローラ → 確定・配達。押下の全経路を通す。"""

    def make(self, backend, **kw):
        from dialogue_headset import HeadsetButton
        r = Rig(**kw)
        self.logs = []
        r.ctl._headset_factory = lambda cb: HeadsetButton(cb, backend=backend, min_interval=0.0,
                                                          log=lambda ev, **f: self.logs.append((ev, f)))
        return r

    def test_press_commits_and_delivers_and_reports_pressed(self):
        be = _Backend()
        r = self.make(be)
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.headset_state(), "active")
            r.say()
            be.on_press()
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.client.delivered[0][1], "こんにちは。元気ですか。")
            self.assertIn("headset_press", r.names())
            self.assertIn("pressed", r.names())
        finally:
            r.close()

    def test_press_with_empty_draft_reacts_but_delivers_nothing(self):
        be = _Backend()
        r = self.make(be)
        events = []
        try:
            r.ctl.set_event_sink(lambda n, i: events.append((n, i)))
            r.ctl.start()
            be.on_press()
            self.assertTrue(wait_until(lambda: any(n == "pressed" for n, _ in events)))
            info = [i for n, i in events if n == "pressed"][0]
            self.assertEqual((info["source"], info["action"]), ("headset", L.PRESS_EMPTY))
            self.assertTrue(any(n == "headset_press" for n, _ in events))       # 受け取ったことは必ず出る
            self.assertEqual(r.client.delivered, [])
            self.assertEqual(r.ctl.ds.buffer.history, [])
        finally:
            r.close()

    def test_press_with_undelivered_retries(self):
        be = _Backend()
        r = self.make(be, results=[{"status": "undelivered", "reason": "foreground_failed"}])
        events = []
        try:
            r.ctl.start()
            r.say()
            be.on_press()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            r.ctl.set_event_sink(lambda n, i: events.append((n, i)))
            be.on_press()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 0 and not r.ctl.ds.buffer.delivering))
            info = [i for n, i in events if n == "pressed"][0]
            self.assertEqual(info["action"], L.PRESS_RETRY)
        finally:
            r.close()

    def test_registration_failure_is_reported_with_code(self):
        be = _Backend(HeadsetResult(False, "他のアプリがすでにこのボタンを使っています", 1409))
        r = self.make(be)
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.headset_state(), "failed")
            d = r.ctl.headset_diagnostics()
            self.assertEqual((d["state"], d["register_code"]), ("failed", 1409))
            self.assertIn("headset_failed", r.names())
            ev, f = self.logs[0]
            self.assertEqual((ev, f["ok"], f["code"]), ("headset_register", False, 1409))
        finally:
            r.close()

    def test_diagnostic_log_has_counts_but_never_the_spoken_text(self):
        import dialogue_log as DL
        be = _Backend()
        r = self.make(be)
        try:
            r.ctl.start()
            r.say()
            be.on_press()
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            time.sleep(0.2)
            for h in DL._logger().handlers:
                h.flush()
            text = DL.log_path().read_text(encoding="utf-8")
            self.assertIn("commit_call", text)
            self.assertIn("deliver_result", text)
            self.assertIn("session_start", text)
            self.assertNotIn("こんにちは", text)
            self.assertNotIn("元気ですか", text)
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
