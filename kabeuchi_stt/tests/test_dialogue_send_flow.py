# -*- coding: utf-8 -*-
"""送信・仮認識まわりの表示ロジックと、controller の結線（フェイク）。Qt は使わない。"""
import threading
import unittest

import numpy as np

import _common as C  # noqa: F401
import ui_dialogue_logic as L
from dialogue_buffer import DialogueBuffer
from dialogue_session import DialogueSession
from test_dialogue_controller import Rig, wait_until
from test_dialogue_session import Deliver, FakeSession


class RecognitionViewTest(unittest.TestCase):
    def snap(self, **kw):
        base = {"active": True, "held": False, "speaking": False, "pending_recognitions": 0}
        base.update(kw)
        return base

    def test_states(self):
        self.assertEqual(L.recognition_view(None)[1], "muted")
        self.assertIn("始まっていません", L.recognition_view({"active": False})[0])
        self.assertIn("待機", L.recognition_view(self.snap())[0])
        t, tone = L.recognition_view(self.snap(speaking=True))
        self.assertIn("聞き取り中", t)
        self.assertEqual(tone, "rec")
        t, tone = L.recognition_view(self.snap(pending_recognitions=2))
        self.assertIn("清書待ち（2件）", t)
        self.assertEqual(tone, "accent")
        t, _ = L.recognition_view(self.snap(speaking=True, pending_recognitions=3))
        for w in ("聞き取り中", "清書待ち（3件）"):
            self.assertIn(w, t)
        self.assertNotIn("仮認識", t)
        self.assertIn("保留", L.recognition_view(self.snap(held=True))[0])


class PlanDraftSyncTest(unittest.TestCase):
    def test_ops(self):
        self.assertEqual(L.plan_draft_sync("あ", "あ"), ("none", 0, ""))
        self.assertEqual(L.plan_draft_sync("", "あい"), ("append", 0, "あい"))
        self.assertEqual(L.plan_draft_sync("あ", "あい"), ("append", 0, "い"))
        self.assertEqual(L.plan_draft_sync("えっと、", "えっと。\nつぎ"), ("patch", 1, "。\nつぎ"))     # 「、」→「。」
        self.assertEqual(L.plan_draft_sync("えっと", "えっと。\nつぎ"), ("append", 0, "。\nつぎ"))
        self.assertEqual(L.plan_draft_sync("あいう", ""), ("replace", 0, ""))                        # 確定・破棄
        self.assertEqual(L.plan_draft_sync("あいう", "ぜんぜん別"), ("replace", 0, "ぜんぜん別"))


class NoticeTest(unittest.TestCase):
    def test_delivered_notice(self):
        self.assertEqual(L.delivered_notice({"sent": ""}), ("入力先へ届けました（送信はしていません）", "ok"))
        self.assertEqual(L.delivered_notice({"sent": "sent"})[1], "ok")
        t, tone = L.delivered_notice({"sent": "not_sent", "send_reason": "busy_timeout"})
        self.assertEqual(tone, "warn")
        self.assertIn("送信できませんでした", t)
        self.assertIn("Enter／送信ボタンを押してください", t)
        self.assertIn("回答中", t)
        t, tone = L.delivered_notice({"sent": "unknown"})
        self.assertEqual(tone, "warn")
        self.assertIn("確認できませんでした", t)

    def test_history_rows_show_sent_state_and_edited(self):
        b = DialogueBuffer()
        for t, res in (("a", {"status": "delivered", "sent": True}),
                       ("b", {"status": "delivered", "sent": False, "send_reason": "send_not_sent"}),
                       ("c", {"status": "delivered", "sent": "unknown"}),
                       ("d", {"status": "delivered"})):
            b.add_utterance(t)
            b.report(b.commit(), res)
        st = [r["status"] for r in L.history_rows(b.history)]
        self.assertEqual(st, ["配達済み・送信済み", "配達済み・未送信", "配達済み・送信は未確認", "配達済み"])

    def test_send_reason_texts(self):
        for code in ("busy_timeout", "send_foreground_failed", "send_focus_not_confirmed", "send_not_sent",
                     "send_unconfirmed", "send_foreground_lost"):
            self.assertNotEqual(L.reason_text(code), code)


class ControllerSendTest(unittest.TestCase):
    def test_options_follow_settings_each_delivery(self):
        r = Rig(texts=("こんにちは。", "元気ですか。"))
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit("window")
            self.assertTrue(wait_until(lambda: r.client.options and not r.ctl.ds.buffer.delivering))
            self.assertFalse(r.client.options[0]["send"])                        # 既定は送信しない
            r.ctl.set_option("send_after_deliver", True)                          # パネルのチェック
            r.ctl.ds.buffer.add_utterance("つぎ。")
            r.headset.press()                                                     # ヘッドセット単押し = 確定して届ける（同じ操作）
            self.assertTrue(wait_until(lambda: len(r.client.options) == 2 and not r.ctl.ds.buffer.delivering))
            o = r.client.options[1]
            self.assertTrue(o["send"] and o["wait_while_busy"])
            self.assertEqual(o["busy_wait_sec"], 120.0)
        finally:
            r.close()

    def test_all_entry_points_use_same_commit_with_send(self):
        for source in ("window", "headset", "hotkey"):
            r = Rig(dialogue={"send_after_deliver": True},
                    results=[{"status": "delivered", "stage": "uia", "sent": True}])
            try:
                r.ctl.start()
                r.say()
                if source == "headset":
                    r.headset.press()
                else:
                    r.ctl.commit(source)
                self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
                self.assertTrue(r.client.options[0]["send"], source)
                self.assertEqual(r.ctl.ds.buffer.history[0].sent, "sent")
            finally:
                r.close()

    def test_retry_with_send_keeps_the_send_option_and_marks_sent(self):
        r = Rig(dialogue={"send_after_deliver": True},
                results=[{"status": "undelivered", "reason": "foreground_failed"},
                         {"status": "delivered", "stage": "uia", "sent": True}])
        try:
            r.ctl.start()
            r.say()
            r.headset.press()
            self.assertTrue(wait_until(lambda: r.ctl.ds.buffer.undelivered_count == 1))
            r.headset.press()                                                     # 再試行
            self.assertTrue(wait_until(lambda: len(r.client.delivered) == 2 and not r.ctl.ds.buffer.delivering))
            self.assertTrue(all(o["send"] for o in r.client.options))
            self.assertEqual(r.ctl.ds.buffer.history[0].sent, "sent")
        finally:
            r.close()

    def test_delivered_but_not_sent_is_not_retried_again(self):
        r = Rig(dialogue={"send_after_deliver": True},
                results=[{"status": "delivered", "stage": "uia", "sent": False, "send_reason": "busy_timeout"}])
        try:
            r.ctl.start()
            r.say()
            r.ctl.commit("window")
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.ctl.ds.buffer.history[0].sent, "not_sent")
            self.assertEqual(r.ctl.ds.buffer.undelivered_count, 0)
            # 再試行にならない（二重入力しない）。下書きは空なので「空」（2026-11-22 以降。何も届けず、押したことは画面に出す）
            self.assertEqual(L.press_action(r.ctl.snapshot()), L.PRESS_EMPTY)
            self.assertIn("delivered", r.names())
        finally:
            r.close()

    def test_apply_headset_setting_only_during_session(self):
        r = Rig(dialogue={"headset_button": False})
        try:
            r.ctl.apply_headset_setting()
            self.assertIsNone(r.headset)                                          # セッション外では何もしない
            r.ctl.start()
            self.assertIsNone(r.headset)
            r.ctl.set_option("headset_button", True)
            r.ctl.apply_headset_setting()                                         # セッション中なら今すぐ独占を登録
            self.assertEqual(r.headset.state, "active")
            r.ctl.set_option("headset_button", False)
            r.ctl.apply_headset_setting()
            self.assertEqual(r.headset.state, "stopped")                          # 今すぐ解除
        finally:
            r.close()

    def test_set_option_saves_only_that_key(self):
        saved = []
        r = Rig(dialogue={"last_target_marker": 1})
        try:
            r.ctl._on_settings_changed = lambda: saved.append(dict(r.settings["dialogue"]))
            before = dict(r.settings["dialogue"])
            r.ctl.set_option("send_after_deliver", True)
            after = r.settings["dialogue"]
            self.assertEqual({k: v for k, v in after.items() if k != "send_after_deliver"}, before)
            self.assertTrue(after["send_after_deliver"])
            self.assertEqual(len(saved), 1)
        finally:
            r.close()


if __name__ == "__main__":
    unittest.main()
