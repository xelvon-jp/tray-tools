# -*- coding: utf-8 -*-
"""AI対話モードのバッファと未配達キュー（純ロジック）。"""
import json
import random
import unittest

import _common as C  # noqa: F401  (import パスの設定)
from dialogue_buffer import (DELIVERED, DELIVERING, DISCARDED, UNDELIVERED, DeliveryOutcome, DialogueBuffer,
                             join_texts, normalize_result)

OK = {"status": "delivered", "stage": "uia"}


def NG(reason="前面化できませんでした"):
    return {"status": "undelivered", "reason": reason}


def make(**kw):
    n = [0]

    def now():
        n[0] += 1
        return f"2026-01-01T00:00:{n[0]:02d}"
    return DialogueBuffer(now=now, **kw)


def queue_undelivered(b, texts):
    """未配達を複数つくる（確定→失敗が1件、残りは未配達の列へ並べる）。"""
    for i, t in enumerate(texts):
        b.add_utterance(t)
        if i == 0:
            b.report(b.commit(), NG())
        else:
            b.enqueue_draft()


class JoinTest(unittest.TestCase):
    def test_join(self):
        self.assertEqual(join_texts(["こんにちは。", "元気です。"]), "こんにちは。元気です。")
        self.assertEqual(join_texts(["use", "python", " 3 "]), "use python 3")
        self.assertEqual(join_texts(["", "  ", "あ"]), "あ")
        self.assertEqual(join_texts(["日本語", "abc"]), "日本語abc")


class DraftTest(unittest.TestCase):
    def test_add_and_empty(self):
        b = make()
        self.assertFalse(b.add_utterance("  "))
        self.assertTrue(b.add_utterance("一つ目。"))
        self.assertTrue(b.add_utterance("二つ目。", start_sec=3.0))
        self.assertEqual(b.draft_text, "一つ目。二つ目。")
        self.assertEqual(len(b.draft_utterances), 2)

    def test_hold_blocks_intake_but_not_committed(self):
        b = make()
        b.add_utterance("確定する分")
        req = b.commit()
        b.hold(True)
        self.assertFalse(b.add_utterance("保留中の発話"))
        self.assertEqual(b.draft_text, "")
        b.report(req, OK)
        self.assertEqual(b.history[0].status, DELIVERED)
        b.hold(False)
        self.assertTrue(b.add_utterance("再開後"))

    def test_discard_draft(self):
        b = make()
        b.add_utterance("やめる")
        self.assertEqual(b.discard_draft(), "やめる")
        self.assertIsNone(b.commit())
        self.assertEqual(b.history, [])


class CommitTest(unittest.TestCase):
    def test_commit_delivered(self):
        b = make()
        b.add_utterance("こんにちは")
        req = b.commit()
        self.assertEqual((req.mode, req.text), ("commit", "こんにちは"))
        self.assertEqual(b.draft_text, "")
        self.assertTrue(b.delivering)
        out = b.report(req, OK)
        self.assertTrue(out.delivered)
        m = b.history[0]
        self.assertEqual((m.status, m.stage, m.attempts), (DELIVERED, "uia", 1))
        self.assertTrue(m.created_at and m.delivered_at)
        self.assertEqual(b.undelivered_count, 0)

    def test_empty_commit_is_none(self):
        self.assertIsNone(make().commit())

    def test_no_double_delivery_while_delivering(self):
        b = make()
        b.add_utterance("a")
        req = b.commit()
        b.add_utterance("b")
        self.assertIsNone(b.commit())      # 配達中は受け付けない
        self.assertEqual(b.draft_text, "b")   # 下書きは残る
        b.report(req, OK)
        self.assertEqual(b.commit().text, "b")

    def test_failure_keeps_text_as_undelivered(self):
        b = make()
        b.add_utterance("大事な文")
        req = b.commit()
        b.report(req, NG("窓が見つかりません"))
        u = b.undelivered
        self.assertEqual([(m.text, m.status, m.reason) for m in u], [("大事な文", UNDELIVERED, "窓が見つかりません")])
        self.assertTrue(b.next_commit_is_retry)

    def test_next_commit_is_retry_and_draft_stays(self):
        b = make()
        b.add_utterance("一通目")
        b.report(b.commit(), NG())
        b.add_utterance("二通目")
        req = b.commit()
        self.assertEqual((req.mode, req.text), ("retry", "一通目"))   # 新規の確定ではない
        self.assertEqual(b.draft_text, "二通目")
        b.report(req, OK)
        self.assertEqual(b.undelivered_count, 0)
        req2 = b.commit()
        self.assertEqual((req2.mode, req2.text), ("commit", "二通目"))

    def test_auto_commit_never_retries_but_queues_in_order(self):
        b = make()
        b.add_utterance("x")
        b.report(b.commit(), NG())
        b.add_utterance("y")
        self.assertIsNone(b.commit(auto=True))      # 配達も再試行も試さない
        self.assertEqual([m.text for m in b.undelivered], ["x", "y"])   # 下書きは確定して列の末尾へ
        self.assertEqual(b.draft_text, "")
        self.assertEqual(b.undelivered[1].attempts, 0)
        self.assertIsNone(b.commit(auto=True))      # 下書きが空なら何もしない

    def test_oldest_first_one_by_one(self):
        b = make()
        queue_undelivered(b, ["古い", "中くらい", "新しい"])
        self.assertEqual([m.text for m in b.undelivered], ["古い", "中くらい", "新しい"])
        r = b.begin_retry()
        self.assertEqual(r.text, "古い")
        b.report(r, OK)
        r = b.begin_retry()
        self.assertEqual(r.text, "中くらい")
        b.report(r, NG("また失敗"))                 # 失敗したら、順序を保ったまま残る
        self.assertEqual([m.text for m in b.undelivered], ["中くらい", "新しい"])
        self.assertEqual(b.undelivered[0].attempts, 1)   # 列に並べたあと、再試行で1回試した
        self.assertEqual(b.undelivered[0].reason, "また失敗")

    def test_merge_setting(self):
        b = make(merge_undelivered=True)
        queue_undelivered(b, ["あ", "い", "う"])
        r = b.begin_retry()
        self.assertTrue(r.merged)
        self.assertEqual(r.text, "あ\nい\nう")
        self.assertEqual(len(r.ids), 3)
        b.report(r, NG("だめ"))
        self.assertEqual(b.undelivered_count, 3)    # 失敗しても元の3件が残る
        r = b.begin_retry()
        b.report(r, OK)
        self.assertEqual(b.undelivered_count, 0)
        self.assertTrue(all(m.status == DELIVERED for m in b.history))

    def test_exception_and_garbage_results_are_undelivered(self):
        for bad in (RuntimeError("boom"), None, "ok", 123, {"status": "ok"}, {}, False):
            b = make()
            b.add_utterance("守る")
            out = b.report(b.commit(), bad)
            self.assertFalse(out.delivered, bad)
            self.assertEqual(b.undelivered[0].text, "守る")
            self.assertTrue(out.reason)

    def test_result_forms(self):
        self.assertTrue(normalize_result(True).delivered)
        self.assertTrue(normalize_result({"status": "delivered"}).delivered)
        self.assertTrue(normalize_result(DeliveryOutcome(True)).delivered)

        class R:
            status = "delivered"
            stage = "paste"
        o = normalize_result(R())
        self.assertTrue(o.delivered)
        self.assertEqual(o.stage, "paste")

    def test_abort(self):
        b = make()
        b.add_utterance("x")
        req = b.commit()
        b.abort(req, "起動できません")
        self.assertEqual(b.undelivered[0].reason, "起動できません")

    def test_report_is_idempotent(self):
        b = make()
        b.add_utterance("x")
        req = b.commit()
        b.report(req, OK)
        b.report(req, NG())     # 二重に報告されても、配達済みを未配達に戻さない
        self.assertEqual(b.history[0].status, DELIVERED)


class UndeliveredOpsTest(unittest.TestCase):
    def setUp(self):
        self.b = make()
        queue_undelivered(self.b, ["いち", "に", "さん"])
        self.b.add_utterance("下書き")

    def test_copy_text(self):
        self.assertEqual(self.b.copy_text(), "いち\nに\nさん")
        self.assertEqual(self.b.copy_text("draft"), "下書き")
        self.assertEqual(self.b.copy_text("all", sep=" / "), "いち / に / さん / 下書き")

    def test_discard_some_and_all_keeps_history_text(self):
        first = self.b.undelivered[0].id
        self.assertEqual(self.b.discard_undelivered([first]), 1)
        self.assertEqual([m.text for m in self.b.undelivered], ["に", "さん"])
        self.assertEqual(self.b.discard_undelivered(), 2)
        self.assertFalse(self.b.has_undelivered)
        self.assertEqual([m.status for m in self.b.history], [DISCARDED] * 3)
        self.assertEqual([m.text for m in self.b.history], ["いち", "に", "さん"])   # 履歴には残る

    def test_end_report(self):
        r = self.b.end_report()
        self.assertEqual(r["undelivered_count"], 3)
        self.assertEqual(r["draft_text"], "下書き")
        self.assertTrue(r["needs_attention"])
        self.b.discard_undelivered()
        self.b.discard_draft()
        self.assertFalse(self.b.end_report()["needs_attention"])

    def test_end_report_inflight(self):
        b = make()
        b.add_utterance("x")
        b.commit()
        r = b.end_report()
        self.assertEqual(len(r["delivering_ids"]), 1)
        self.assertTrue(r["needs_attention"])

    def test_not_dropped_by_age(self):
        # 期限で捨てない: 時計がどれだけ進んでも残る
        b = DialogueBuffer(now=lambda: "2999-12-31T00:00:00")
        b.add_utterance("ずっと")
        b.report(b.commit(), NG())
        for _ in range(100):
            b.add_utterance("追加")
            self.assertEqual(b.undelivered[0].text, "ずっと")
        self.assertEqual(b.undelivered_count, 1)


class PersistTest(unittest.TestCase):
    def test_roundtrip_json(self):
        b = make()
        b.add_utterance("配達済み")
        b.report(b.commit(), OK)
        b.add_utterance("未配達")
        b.report(b.commit(), NG("理由"))
        b.add_utterance("下書き", start_sec=1.5)
        d = json.loads(json.dumps(b.to_dict(), ensure_ascii=False))
        b2 = DialogueBuffer.from_dict(d)
        self.assertEqual([(m.text, m.status, m.created_at) for m in b2.history],
                         [(m.text, m.status, m.created_at) for m in b.history])
        self.assertEqual(b2.undelivered[0].reason, "理由")
        self.assertEqual(b2.draft_text, "下書き")
        b2.add_utterance("続き")
        req = b2.commit()           # 未配達があるので再試行
        self.assertEqual(req.text, "未配達")
        b2.report(req, OK)
        self.assertEqual(b2.commit().mode, "commit")
        self.assertEqual(len({m.id for m in b2.history}), 3)   # id が衝突しない

    def test_inflight_at_save_comes_back_undelivered(self):
        b = make()
        b.add_utterance("配達中に保存された")
        b.commit()
        b2 = DialogueBuffer.from_dict(b.to_dict())
        self.assertEqual(b2.undelivered[0].text, "配達中に保存された")
        self.assertFalse(b2.delivering)

    def test_ids_after_restore_unique(self):
        b = make()
        for t in "abc":
            b.add_utterance(t)
            b.report(b.commit(), OK)
        b2 = DialogueBuffer.from_dict(b.to_dict())
        b2.add_utterance("d")
        b2.report(b2.commit(), OK)
        self.assertEqual([m.id for m in b2.history], [1, 2, 3, 4])


class NeverLoseCommittedTextTest(unittest.TestCase):
    """ランダムな操作列で、確定した文字列が履歴から消えないこと。"""

    def test_random_operations(self):
        for seed in range(200):
            rnd = random.Random(seed)
            b = make(merge_undelivered=rnd.random() < 0.5)
            committed: list[str] = []
            pending_req = None
            n = 0
            for _ in range(60):
                op = rnd.choice(["add", "add", "commit", "commit", "ok", "ng", "exc", "retry", "hold",
                                 "discard_u", "discard_d", "save"])
                if op == "add":
                    n += 1
                    b.add_utterance(f"発話{n}")
                elif op == "commit":
                    drafted = b.draft_text
                    was_retry = b.has_undelivered
                    req = b.commit(auto=rnd.random() < 0.2)
                    if req is not None:
                        pending_req = req
                        if req.mode == "commit":
                            self.assertFalse(was_retry)
                            committed.append(drafted)
                    elif drafted and b.draft_text == "":
                        committed.append(drafted)    # 自動確定で未配達の列へ並べられた
                elif op == "retry":
                    r = b.begin_retry()
                    if r is not None:
                        pending_req = r
                elif op in ("ok", "ng", "exc") and pending_req is not None:
                    b.report(pending_req, OK if op == "ok" else NG() if op == "ng" else RuntimeError("x"))
                    pending_req = None
                elif op == "hold":
                    b.hold(not b.held)
                elif op == "discard_u":
                    b.discard_undelivered()
                elif op == "discard_d":
                    b.discard_draft()
                elif op == "save":
                    b = DialogueBuffer.from_dict(json.loads(json.dumps(b.to_dict())),
                                                 merge_undelivered=b.merge_undelivered)
                    pending_req = None   # 保存・復元をまたいだ依頼は無効（配達中は未配達として戻る）
                # 不変条件: 確定したテキストは履歴から消えない（破棄しても履歴に残る）
                hist = [m.text for m in b.history]
                for t in committed:
                    self.assertIn(t, hist, f"seed={seed} 確定したテキストが履歴から消えた: {t}")
                for m in b.history:
                    self.assertIn(m.status, (UNDELIVERED, DELIVERING, DELIVERED, DISCARDED))
            r = b.end_report()
            inflight = [m for m in b.history if m.status == DELIVERING]
            self.assertEqual(len(r["delivering_ids"]), len(inflight))


class NoProvisionalTest(unittest.TestCase):
    """仮認識は廃止した: 下書きに入るのは清書の結果だけ。持ち越した下書きは、次のセッションでも白い確定済みの文字のまま残る。"""

    def test_no_provisional_api_and_message_has_no_clean_version(self):
        b = make()
        for name in ("set_provisional", "provisional_items", "adopt", "is_closed", "clear_provisional", "provisional_count"):
            self.assertFalse(hasattr(b, name), name)
        b.add_utterance("清書の結果", 0.0, 1.0, uid=0.0)
        b.commit()
        m = b.history[0]
        self.assertFalse(hasattr(m, "clean"))
        self.assertNotIn("provisional", m.to_dict())

    def test_old_saved_messages_with_clean_keys_still_load(self):
        old = {"version": 1, "next_id": 2, "held": False,
               "messages": [{"id": 1, "text": "古い", "created_at": "2026-11-01T00:00:00", "status": "delivered",
                             "provisional": True, "clean": ["むかしの清書版"]}],
               "draft": [{"text": "下書き", "t": "x", "start_sec": None, "end_sec": None}]}
        b = DialogueBuffer.from_dict(old)
        self.assertEqual(b.history[0].text, "古い")
        self.assertEqual(b.draft_text, "下書き")

    def test_carried_draft_stays_as_final_text_at_next_session(self):
        b = make()
        b.add_utterance("終わりぎわの発話", 1.0, 2.0, uid=1.0)
        rev = b.draft_rev
        b.begin_session()                                  # 次のセッションの開始: 文字は捨てず、そのまま確定済みの下書き
        self.assertEqual(b.draft_final_text, "終わりぎわの発話")
        self.assertEqual(b.draft_rev, rev)
        self.assertTrue(b.has_draft)

    def test_late_utterance_after_discard_is_not_special(self):
        b = make()
        b.add_utterance("あ", 0.0, 1.0, uid=0.0)
        b.discard_draft()
        b.add_utterance("い", 2.0, 3.0, uid=2.0)
        self.assertEqual(b.draft_text, "い")


class SentenceRulesInBufferTest(unittest.TestCase):
    def test_commit_text_uses_newline_after_sentence_and_blank_line(self):
        b = make(sentence_newline=True, blank_line=True, gap_sec=2.0, force_gap_sec=3.0)
        b.add_utterance("一つ目です。", 0.0, 2.0, uid=0.0)
        b.add_utterance("せいしょ。つぎ。", 5.0, 7.0, uid=5.0)
        self.assertEqual(b.draft_final_text, "一つ目です。\n\nせいしょ。\nつぎ。")
        self.assertEqual(b.draft_text, b.draft_final_text)

    def test_defaults_off_keep_plain_join(self):
        b = make()
        b.add_utterance("あ。い。")
        self.assertEqual(b.draft_final_text, "あ。い。")


if __name__ == "__main__":
    unittest.main()
