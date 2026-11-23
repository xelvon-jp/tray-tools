# -*- coding: utf-8 -*-
"""下書きの手修正・句点と改行の規則・送信の状態・記録だけ（バッファ側の純ロジック）。"""
import json
import unittest

import _common as C  # noqa: F401
from dialogue_buffer import DELIVERED, DialogueBuffer

OK = {"status": "delivered", "stage": "uia"}


def NG(reason="前面化できませんでした"):
    return {"status": "undelivered", "reason": reason}


def make(**kw):
    n = [0]

    def now():
        n[0] += 1
        return f"2026-01-01T00:00:{n[0]:02d}"
    return DialogueBuffer(now=now, **kw)


class DraftEditTest(unittest.TestCase):
    def test_set_draft_text_replaces_and_appends_to_the_end(self):
        b = make()
        b.add_utterance("こんにちは。")
        rev = b.draft_rev
        self.assertTrue(b.set_draft_text("  こんばんは。  "))
        self.assertEqual(b.draft_text, "こんばんは。")
        self.assertGreater(b.draft_rev, rev)
        self.assertTrue(b.draft_edited)
        b.add_utterance("元気ですか。")                      # 以後の追記は、手修正後の末尾へ連結
        self.assertEqual(b.draft_text, "こんばんは。元気ですか。")
        req = b.commit()
        self.assertEqual(req.text, "こんばんは。元気ですか。")     # 確定は手修正後の内容
        self.assertTrue(b.history[0].edited)                     # 履歴に「手修正あり」の印
        self.assertFalse(b.draft_edited)

    def test_no_change_does_not_bump_rev_or_mark_edited(self):
        b = make()
        b.add_utterance("同じ")
        rev = b.draft_rev
        self.assertTrue(b.set_draft_text("同じ "))
        self.assertEqual(b.draft_rev, rev)
        self.assertFalse(b.draft_edited)

    def test_base_rev_conflict_never_overwrites_a_recognition_that_arrived(self):
        b = make()
        b.add_utterance("一つ目")
        base = b.draft_rev
        b.add_utterance("二つ目")                             # 画面が見ていない間に届いた認識結果
        self.assertFalse(b.set_draft_text("一つ目を直した", base_rev=base))
        self.assertEqual(b.draft_text, "一つ目二つ目")        # 失われない
        self.assertTrue(b.set_draft_text("一つ目二つ目を直した", base_rev=b.draft_rev))
        self.assertEqual(b.draft_text, "一つ目二つ目を直した")

    def test_empty_edit_clears_and_commit_of_empty_returns_none(self):
        b = make()
        b.add_utterance("消す")
        self.assertTrue(b.set_draft_text(""))
        self.assertFalse(b.has_draft)
        self.assertIsNone(b.commit())

    def test_typing_into_an_empty_draft_can_be_committed(self):
        b = make()
        self.assertTrue(b.set_draft_text("手で書いた"))
        self.assertEqual(b.commit().text, "手で書いた")

    def test_edit_keeps_text_never_lost_on_failure(self):
        b = make()
        b.add_utterance("あ")
        b.set_draft_text("あの手修正")
        b.report(b.commit(), NG())
        self.assertEqual(b.undelivered[0].text, "あの手修正")
        self.assertTrue(b.undelivered[0].edited)

    def test_gap_rules_and_commit_period(self):
        b = make(gap_sec=2.0, force_gap_sec=3.0, punctuate_on_commit=True)
        b.add_utterance("今日は", 0.0, 1.0)
        b.add_utterance("晴れです。", 1.2, 2.0)           # 間 0.8 秒: そのまま連結
        b.add_utterance("あしたは", 9.0, 10.0)            # 間 7.6 秒: 句点系で終わっている → 改行
        self.assertEqual(b.draft_text, "今日は晴れです。\nあしたは")
        self.assertEqual(b.commit().text, "今日は晴れです。\nあしたは。")      # 確定時に末尾へ句点

    def test_edit_then_append_applies_rules_only_to_new_part(self):
        b = make(gap_sec=2.0, force_gap_sec=3.0)
        b.add_utterance("まえ", 0.0, 1.0)
        b.set_draft_text("手で直した")
        b.add_utterance("つぎ。", 20.0, 21.0)
        self.assertEqual(b.draft_text, "手で直した\nつぎ。")           # 手修正の文に「。」を足さない

    def test_to_dict_roundtrip_keeps_new_fields(self):
        b = make()
        b.add_utterance("あ")
        b.set_draft_text("い")
        b.report(b.commit(), {"status": "delivered", "stage": "uia", "sent": False, "send_reason": "busy_timeout"})
        d = json.loads(json.dumps(b.to_dict()))
        b2 = DialogueBuffer.from_dict(d)
        self.assertTrue(b2.history[0].edited)
        self.assertEqual((b2.history[0].sent, b2.history[0].send_reason), ("not_sent", "busy_timeout"))


class SendStateTest(unittest.TestCase):
    def deliver(self, **extra):
        b = make()
        b.add_utterance("送る")
        out = b.report(b.commit(), {"status": "delivered", "stage": "uia", **extra})
        return b, out

    def test_states(self):
        self.assertEqual(self.deliver()[0].history[0].sent, "")                      # 送信まではしない設定
        self.assertEqual(self.deliver(sent=True)[0].history[0].sent, "sent")
        self.assertEqual(self.deliver(sent=False, send_reason="send_not_sent")[0].history[0].sent, "not_sent")
        self.assertEqual(self.deliver(sent="unknown")[0].history[0].sent, "unknown")

    def test_delivered_but_not_sent_is_delivered_not_undelivered(self):
        """入力は済んでいる（入力欄に残っている）ので未配達にしない = 再試行で二重入力しない。"""
        b, out = self.deliver(sent=False, send_reason="busy_timeout")
        self.assertTrue(out.delivered)
        self.assertEqual(b.history[0].status, DELIVERED)
        self.assertEqual(b.undelivered_count, 0)
        self.assertIsNone(b.begin_retry())
        self.assertEqual(b.history[0].text, "送る")                                  # テキストは残る


class RecordOnlyBufferTest(unittest.TestCase):
    """宛先「なし（記録だけ）」: 配達の結果が stage=record の「配達済み」。確定すると下書きから消え、履歴に残る。"""

    def test_record_stage_delivery_clears_the_draft_and_keeps_history(self):
        b = make()
        b.add_utterance("記録だけ")
        out = b.report(b.commit(), {"status": "delivered", "stage": "record", "sent": ""})
        self.assertTrue(out.delivered)
        self.assertEqual(b.draft_text, "")
        self.assertEqual((b.history[0].status, b.history[0].stage, b.history[0].text), (DELIVERED, "record", "記録だけ"))
        self.assertEqual(b.undelivered_count, 0)


if __name__ == "__main__":
    unittest.main()
