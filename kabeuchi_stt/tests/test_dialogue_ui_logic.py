# -*- coding: utf-8 -*-
"""AI対話モードのコンパクト窓の純ロジック（状態→表示・確定/再試行の分岐・宛先表示・保存名・未配達の操作）。
Qt は使わない。"""
import unittest

import _common as C  # noqa: F401
import ui_dialogue_logic as L
from dialogue_buffer import DialogueBuffer


class StateViewTest(unittest.TestCase):
    def snap(self, **kw):
        base = {"active": True, "recording": True, "held": False, "speaking": False, "delivering": False,
                "undelivered_count": 0, "pending_recognitions": 0, "next_commit_is_retry": False}
        base.update(kw)
        return base

    def test_states(self):
        self.assertEqual(L.view_state(None).key, L.ST_STOPPED)
        self.assertEqual(L.view_state({"active": False}).key, L.ST_STOPPED)
        self.assertEqual(L.view_state(self.snap()).key, L.ST_RECORDING)
        self.assertEqual(L.view_state(self.snap(recording=False)).key, L.ST_IDLE)
        self.assertEqual(L.view_state(self.snap(held=True)).key, L.ST_HELD)
        v = L.view_state(self.snap(undelivered_count=2))
        self.assertEqual((v.key, v.tone), (L.ST_UNDELIVERED, "warn"))
        self.assertIn("2", v.label)

    def test_priority_delivering_over_undelivered_over_held(self):
        self.assertEqual(L.view_state(self.snap(delivering=True, undelivered_count=1, held=True)).key, L.ST_DELIVERING)
        self.assertEqual(L.view_state(self.snap(undelivered_count=1, held=True)).key, L.ST_UNDELIVERED)
        self.assertEqual(L.view_state(self.snap(held=True)).key, L.ST_HELD)

    def test_recording_text_is_always_shown(self):
        self.assertEqual(L.recording_text({"active": False})[1], "muted")
        t, tone = L.recording_text(self.snap())
        self.assertEqual(tone, "rec")
        self.assertIn("録音中", t)
        self.assertIn("話して", L.recording_text(self.snap(speaking=True))[0])
        self.assertIn("認識待ち 3", L.recording_text(self.snap(pending_recognitions=3))[0])
        self.assertEqual(L.recording_text(self.snap(held=True))[1], "warn")
        self.assertEqual(L.recording_text(self.snap(recording=False))[1], "warn")   # セッション中なのに録音が止まっている

    def test_headset_view(self):
        self.assertEqual(L.headset_view("active", True)[1], "ok")
        self.assertIn("受付中", L.headset_view("active", True)[0])
        t, tone = L.headset_view("failed", True, "他のアプリが使用中")
        self.assertEqual(tone, "warn")
        self.assertIn("他のアプリ", t)
        self.assertEqual(L.headset_view("stopped", True)[1], "muted")
        self.assertIn("使わない", L.headset_view("active", False)[0])


class PressActionTest(unittest.TestCase):
    def test_branches(self):
        a = {"active": True, "delivering": False, "undelivered_count": 0, "next_commit_is_retry": False}
        self.assertEqual(L.press_action(None), L.PRESS_IGNORE)
        self.assertEqual(L.press_action({"active": False}), L.PRESS_IGNORE)
        self.assertEqual(L.press_action(a), L.PRESS_COMMIT)
        self.assertEqual(L.press_action({**a, "undelivered_count": 1}), L.PRESS_RETRY)
        self.assertEqual(L.press_action({**a, "next_commit_is_retry": True}), L.PRESS_RETRY)
        self.assertEqual(L.press_action({**a, "delivering": True}), L.PRESS_BUSY)
        self.assertEqual(L.press_action({**a, "delivering": True, "undelivered_count": 1}), L.PRESS_BUSY)

    def test_matches_buffer_rule(self):
        """窓の判定とバッファの規則（未配達があれば次は再試行）が食い違わない。"""
        b = DialogueBuffer()
        b.add_utterance("こんにちは。")
        snap = lambda: {"active": True, "delivering": b.delivering, "undelivered_count": b.undelivered_count,
                        "next_commit_is_retry": b.next_commit_is_retry}
        self.assertEqual(L.press_action(snap()), L.PRESS_COMMIT)
        req = b.commit()
        self.assertEqual(L.press_action(snap()), L.PRESS_BUSY)
        b.report(req, {"status": "undelivered", "reason": "window_not_found"})
        self.assertEqual(L.press_action(snap()), L.PRESS_RETRY)
        self.assertEqual(b.commit().mode, "retry")


class TargetViewTest(unittest.TestCase):
    PROF = {"name": "相談窓", "process_name": "app.exe", "window_title_contains": "相談", "captured_title": "相談 - app"}

    def test_none(self):
        v = L.view_target(None)
        self.assertEqual((v.state, v.tone), (L.TG_NONE, "warn"))

    def test_held_shows_window_title_from_candidates(self):
        res = {"status": "ok", "hwnd": 7, "candidates": [{"hwnd": 7, "title": "相談 - いまのタイトル"}]}
        v = L.view_target(self.PROF, L.target_state_from_resolve(res), res)
        self.assertEqual(v.state, L.TG_HELD)
        self.assertEqual((v.name, v.process, v.title), ("相談窓", "app.exe", "相談 - いまのタイトル"))
        self.assertEqual(v.tone, "ok")

    def test_not_found_and_ambiguous(self):
        for st, want in (("none", L.TG_NOT_FOUND), ("ambiguous", L.TG_AMBIGUOUS), ("weird", L.TG_ERROR)):
            self.assertEqual(L.target_state_from_resolve({"status": st}), want)
        v = L.view_target(self.PROF, L.TG_NOT_FOUND)
        self.assertIn("見つかりません", v.status_text)
        self.assertIn("別の窓へは送りません", v.status_text)
        self.assertEqual(v.tone, "warn")
        self.assertEqual(L.view_target(self.PROF, L.TG_UNCHECKED).tone, "muted")
        self.assertIn("boom", L.view_target(self.PROF, L.TG_ERROR, error="boom").status_text)


class NameValidationTest(unittest.TestCase):
    def test_ok(self):
        c = L.validate_preset_name("  相談窓  ", ["別"])
        self.assertTrue(c.ok)
        self.assertEqual((c.name, c.exists), ("相談窓", False))

    def test_empty_and_too_long(self):
        self.assertFalse(L.validate_preset_name("").ok)
        self.assertFalse(L.validate_preset_name("   ").ok)
        self.assertFalse(L.validate_preset_name("あ" * (L.MAX_NAME_LEN + 1)).ok)
        self.assertTrue(L.validate_preset_name("あ" * L.MAX_NAME_LEN).ok)

    def test_illegal_chars_and_reserved_are_rejected_not_rewritten(self):
        for bad in ("a/b", "a\\b", 'a:"b', "a*b", "a?b", "a<b>", "a|b", "CON", "nul", "com1", "名前.", "x\x01y"):
            c = L.validate_preset_name(bad)
            self.assertFalse(c.ok, bad)
            self.assertTrue(c.message)

    def test_existing_is_flagged_case_insensitive(self):
        c = L.validate_preset_name("Work", ["work", "x"])
        self.assertTrue(c.ok)
        self.assertTrue(c.exists)


class RowsTest(unittest.TestCase):
    def test_reason_text(self):
        self.assertEqual(L.reason_text("window_not_found"), "宛先の窓が見つかりません")
        self.assertEqual(L.reason_text("独自の理由"), "独自の理由")
        self.assertEqual(L.reason_text(""), "理由不明")

    def test_undelivered_and_history_rows(self):
        b = DialogueBuffer(now=lambda: "2026-11-22T09:15:00")
        b.add_utterance("あ" * 80)
        req = b.commit()
        b.report(req, {"status": "undelivered", "reason": "readback_mismatch"})
        rows = L.undelivered_rows(b.undelivered)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["time"], rows[0]["reason"]), ("09:15", "入力後の読み戻しが一致しませんでした"))
        self.assertTrue(rows[0]["snippet"].endswith("…"))
        self.assertEqual(rows[0]["text"], "あ" * 80)          # 全文は消えない
        h = L.history_rows(b.history)
        self.assertEqual(h[0]["status"], "未配達")

    def test_pick_texts(self):
        self.assertIn("あと 30 秒", L.pick_guide_text(30))
        self.assertIn("あと 1 秒", L.pick_guide_text(0.2))
        self.assertIn("0 秒", L.pick_guide_text(-5))
        self.assertIn("入力先の窓", L.pick_failure_text({"status": "own_window"}))
        self.assertIn("やめました", L.pick_failure_text({"status": "cancelled"}))


class _FakeSession:
    def __init__(self, buffer, active=True):
        self.buffer, self.is_active, self.commits = buffer, active, 0

    def commit(self):
        self.commits += 1
        return True


class UndeliveredOpsTest(unittest.TestCase):
    def setUp(self):
        self.b = DialogueBuffer()
        for t in ("一つ目。", "二つ目。"):
            self.b.add_utterance(t)
            req = self.b.commit() or self.b.begin_retry()
            self.b.report(req, {"status": "undelivered", "reason": "foreground_failed"})
            # 二通目は、一通目が未配達だと commit が再試行になるので下書きを enqueue して並べる
        self.b.add_utterance("三つ目。")
        self.b.enqueue_draft()
        self.clip = []
        self.s = _FakeSession(self.b)
        self.ops = L.UndeliveredOps(self.s)

    def test_retry_goes_through_session_commit(self):
        self.assertTrue(self.ops.retry())
        self.assertEqual(self.s.commits, 1)

    def test_retry_refused_when_inactive_nothing_or_delivering(self):
        self.s.is_active = False
        self.assertFalse(self.ops.retry())
        self.s.is_active = True
        self.b.discard_undelivered()
        self.assertFalse(self.ops.retry())          # 未配達なし
        self.assertEqual(self.s.commits, 0)
        b2 = DialogueBuffer()
        b2.add_utterance("x")
        b2.commit()                                  # 配達中
        b2.enqueue_draft()
        s2 = _FakeSession(b2)
        self.assertFalse(L.UndeliveredOps(s2).retry())

    def test_no_clipboard_in_the_ops(self):
        """クリップボードへは何も入れない（入れてよいのは、利用者が明示的に Ctrl+C／右クリックのコピーをしたときだけ）。"""
        self.assertFalse(hasattr(self.ops, "clipboard"))
        self.assertFalse(hasattr(self.ops, "copy"))
        self.assertTrue(self.b.copy_text().startswith("一つ目。"))        # 選択可能なテキストで見せるための文（コピーはしない）

    def test_discard_selected_and_all(self):
        before = self.b.undelivered
        first = before[0].id
        self.assertEqual(self.ops.discard([first]), 1)
        self.assertEqual(self.b.undelivered_count, len(before) - 1)
        self.assertEqual(self.ops.discard(None), len(before) - 1)
        self.assertEqual(self.b.undelivered_count, 0)
        self.assertTrue(any(m.text.startswith("一つ目") for m in self.b.history))   # 履歴にテキストは残る

    def test_resolve_end(self):
        self.b.add_utterance("下書き")
        out = self.ops.resolve_end("keep")
        self.assertEqual(out["discarded"], 0)
        self.assertGreaterEqual(self.b.undelivered_count, 1)
        self.assertNotIn("copied", out)
        out = self.ops.resolve_end("copy")                            # コピーの選択肢は無い（何もしない）
        self.assertEqual(out["discarded"], 0)
        self.assertGreaterEqual(self.b.undelivered_count, 1)
        out = self.ops.resolve_end("discard")
        self.assertGreaterEqual(out["discarded"], 1)
        self.assertEqual(out["draft_discarded"], "下書き")
        self.assertEqual((self.b.undelivered_count, self.b.has_draft), (0, False))


class OneColumnLogicTest(unittest.TestCase):
    """1列の画面（2026-11-22）の純ロジック: 空の押下・通知・ヘッダーの警告・波形／黄色の並びの差分。"""

    def test_press_action_empty_draft_is_empty_not_commit(self):
        base = {"active": True, "draft_text": "", "speaking": False, "pending_recognitions": 0}
        self.assertEqual(L.press_action(base), L.PRESS_EMPTY)
        self.assertEqual(L.press_action({**base, "draft_text": "こんにちは"}), L.PRESS_COMMIT)
        self.assertEqual(L.press_action({**base, "speaking": True}), L.PRESS_COMMIT)            # 話している途中は確定に含める
        self.assertEqual(L.press_action({**base, "pending_recognitions": 1}), L.PRESS_COMMIT)   # 認識待ちも
        self.assertEqual(L.press_action({**base, "undelivered_count": 1}), L.PRESS_RETRY)       # 未配達があれば空でも再試行
        self.assertEqual(L.press_action({**base, "delivering": True}), L.PRESS_BUSY)
        self.assertEqual(L.press_action({"active": False, "draft_text": ""}), L.PRESS_IGNORE)
        self.assertEqual(L.press_action({"active": True}), L.PRESS_COMMIT)                      # 下書きの情報が無い辞書は従来どおり

    def test_press_notice_always_reacts_for_headset_and_hotkey(self):
        for act in (L.PRESS_COMMIT, L.PRESS_RETRY, L.PRESS_EMPTY, L.PRESS_BUSY, L.PRESS_IGNORE):
            t, tone = L.press_notice("headset", act)
            self.assertIn("ヘッドセットのボタンを受け取りました", t)
            self.assertTrue(t.split("→")[1].strip())
        self.assertIn("確定します", L.press_notice("headset", L.PRESS_COMMIT)[0])
        self.assertIn("再試行します", L.press_notice("headset", L.PRESS_RETRY)[0])
        t, tone = L.press_notice("headset", L.PRESS_EMPTY)
        self.assertIn("下書きが空です", t)
        self.assertEqual(tone, "warn")
        self.assertIn("ホットキー", L.press_notice("hotkey", L.PRESS_COMMIT)[0])

    def test_notice_line_fades_and_warn_sticks(self):
        n = L.NoticeLine(hold_sec=5.0, fade_sec=3.0)
        fb = ("Ctrl+Enter", "muted")
        self.assertEqual(n.view(0.0, fb), ("Ctrl+Enter", "muted", False))
        n.post("届けました", "ok", now=10.0)
        self.assertEqual(n.view(12.0, fb), ("届けました", "ok", False))
        self.assertEqual(n.view(16.0, fb), ("届けました", "ok", True))         # 薄くなる
        self.assertEqual(n.view(19.0, fb), ("Ctrl+Enter", "muted", False))      # 消えて常時の案内へ
        n.post("送信できませんでした", "warn", now=20.0)
        self.assertEqual(n.view(500.0, fb), ("送信できませんでした", "warn", False))   # 警告は残る
        self.assertTrue(n.sticky())
        n.post("入力欄に入れました", "ok", now=600.0)                              # 次の通知で置き換わる
        self.assertFalse(n.sticky())
        n.clear()
        self.assertEqual(n.view(601.0, fb)[0], "Ctrl+Enter")

    def test_idle_hint_shows_undelivered_count(self):
        self.assertEqual(L.idle_hint({"active": True, "undelivered_count": 2}), ("未配達 2件", "warn"))
        self.assertEqual(L.idle_hint({"active": True})[1], "muted")
        self.assertEqual(L.idle_hint({"active": False})[0], "")
    def test_idle_hint_lists_only_enabled_methods(self):
        a = {"active": True}
        self.assertEqual(L.idle_hint(a)[0], "Ctrl+Enter でも確定")
        self.assertEqual(L.idle_hint(a, methods=["ヘッドセット"])[0], "Ctrl+Enter／ヘッドセットでも確定")
        self.assertEqual(L.idle_hint(a, methods=["マウス", "舌打ち"])[0], "Ctrl+Enter／マウス／舌打ちでも確定")

    def test_delivered_short_texts(self):
        t, tone = L.delivered_short({}, "14:32")
        self.assertEqual((t, tone), ("入力欄に入れました ✓ 14:32", "ok"))
        self.assertEqual(L.delivered_short({"sent": "sent"}, "14:32")[0], "届けて送信しました ✓ 14:32")
        t, tone = L.delivered_short({"sent": "not_sent", "send_reason": "busy_timeout"}, "14:32")
        self.assertIn("Enter／送信ボタンを押してください", t)
        self.assertEqual(tone, "warn")
        self.assertEqual(L.delivered_short({"sent": "unknown"}, "14:32")[1], "warn")
        self.assertEqual(L.delivered_short({"stage": "record", "sent": ""}, "14:32"), ("記録しました ✓ 14:32", "ok"))
        self.assertIn("記録しました", L.delivered_notice({"stage": "record"})[0])

    def test_history_rows_show_record_only(self):
        b = DialogueBuffer(now=lambda: "2026-11-22T09:15:00")
        b.add_utterance("記録だけの文")
        b.report(b.commit(), {"status": "delivered", "stage": "record", "sent": ""})
        b.add_utterance("届けた文")
        b.report(b.commit(), {"status": "delivered", "stage": "uia", "sent": True})
        h = L.history_rows(b.history)
        self.assertEqual(h[0]["status"], "記録のみ")
        self.assertEqual(h[1]["status"], "配達済み・送信済み")

    def test_headset_view_weak_note_when_not_exclusive(self):
        t, tone = L.headset_view("active", True, "", False)
        self.assertEqual(tone, "ok")
        self.assertIn("動画の再生/一時停止も同時に反応", t)
        self.assertIn("独占", L.headset_view("active", True, "", True)[0])

    def test_headset_status_text_shows_each_path(self):
        diag = {"state": "active", "thread_alive": True, "presses": 2, "dropped": 1, "exclusive": False, "mode": "auto",
                "paths": {"hid": {"enabled": True, "ok": True, "alive": True, "received": 2,
                                  "detail": {"device": "mi_00&col01", "reports": 9, "last_report": "01 01"}},
                          "hotkey": {"enabled": True, "ok": False, "reason": "他のアプリがすでに使用中", "code": 1409}}}
        t = L.headset_status_text(diag, True)
        self.assertIn("同時に反応する場合", t)
        self.assertIn("HID直接読み: 開けました（mi_00&col01・押下 2 回・レポート 9 件", t)
        self.assertIn("ホットキー: 登録できません（他のアプリがすでに使用中・Win32 エラー 1409）", t)
        diag["paths"]["hid"] = {"enabled": True, "ok": False, "reason": "デバイスが見つかりません"}
        diag["paths"]["hotkey"] = {"enabled": False}
        t = L.headset_status_text(diag, True)
        self.assertIn("HID直接読み: 開けません（デバイスが見つかりません）", t)
        self.assertIn("ホットキー: 使いません（設定）", t)

    def test_header_warnings_are_short_and_only_when_needed(self):
        self.assertEqual(L.target_header_warn(L.TG_HELD), "")
        self.assertEqual(L.target_header_warn(L.TG_UNCHECKED), "")
        self.assertIn("見つかりません", L.target_header_warn(L.TG_NOT_FOUND))
        self.assertEqual(L.headset_header_warn("active", True), "")
        self.assertEqual(L.headset_header_warn("stopped", True), "")
        self.assertEqual(L.headset_header_warn("failed", False, "x"), "")
        w = L.headset_header_warn("failed", True, "他のアプリが使用中")
        self.assertTrue(w.startswith("⚠ ヘッドセット受付できません"))
        self.assertIn("他のアプリが使用中", w)

    def test_headset_status_text(self):
        self.assertIn("使わない", L.headset_status_text({}, False))
        t = L.headset_status_text({"state": "failed", "reason": "他のアプリが使用中", "register_code": 1409,
                                   "presses": 0, "dropped": 0}, True)
        self.assertIn("1409", t)
        self.assertIn("他のアプリが使用中", t)
        t = L.headset_status_text({"state": "active", "thread_alive": True, "presses": 3, "dropped": 1,
                                   "last_press": "14:32:05"}, True)
        self.assertIn("受付中", t)
        self.assertIn("押下 3 回（間引き 1 回）", t)
        self.assertIn("14:32:05", t)

    def test_build_tail_sorts_by_time(self):
        items = L.build_tail([{"uid": 20.0, "env": (2, 3)}, {"uid": 12.0, "env": (1,)}])
        self.assertEqual([(i.uid, i.kind, i.wave) for i in items], [(12.0, L.K_WAVE, (1,)), (20.0, L.K_WAVE, (2, 3))])
        self.assertEqual(L.build_tail([]), [])
        self.assertFalse(hasattr(L, "K_PROV"))                             # 仮認識（黄色）は廃止した

    def apply(self, cur, ops):
        """plan_tail の操作を素朴なリストへ適用して、結果を返す（画面の代わり）。"""
        cur = [(c.uid, c.kind, tuple(c.wave)) for c in cur]
        for op in ops:
            if op[0] == "remove":
                del cur[op[1]]
            elif op[0] == "insert":
                it = op[2]
                cur.insert(op[1], (it.uid, it.kind, tuple(it.wave)))
            else:
                it = op[2]
                cur[op[1]] = (it.uid, it.kind, tuple(it.wave))
        return cur

    def test_plan_tail_reaches_target_and_is_minimal(self):
        import random
        rng = random.Random(7)
        for _ in range(300):
            def rand_items():
                uids = sorted(rng.sample(range(1, 12), rng.randint(0, 6)))
                return [L.TailItem(float(u), L.K_WAVE, (rng.randint(0, 3),)) for u in uids]
            a, b = rand_items(), rand_items()
            cur = [L.CurBlock(i.uid, i.kind, i.wave) for i in a]
            ops = L.plan_tail(cur, b)
            self.assertEqual(self.apply(cur, ops), [(i.uid, i.kind, tuple(i.wave)) for i in b])
        # 同じなら何もしない（カーソル・選択を動かさない）
        same = [L.TailItem(5.0, L.K_WAVE, (3,)), L.TailItem(9.0, L.K_WAVE, (1, 2))]
        self.assertEqual(L.plan_tail([L.CurBlock(i.uid, i.kind, i.wave) for i in same], same), [])

    def test_recognizing_row_appears_then_goes_in_place(self):
        """区切れた発話が「認識中」の行として出て、清書が済むと（白い文字は確定済みの側へ入るので）後ろ側から消える。"""
        w = [L.TailItem(5.0, L.K_WAVE, (1, 2, 3))]
        self.assertEqual(L.plan_tail([], w), [("insert", 0, w[0])])
        self.assertEqual(L.plan_tail([L.CurBlock(5.0, L.K_WAVE, (1, 2, 3))], []), [("remove", 0)])

    def test_wave_levels(self):
        import numpy as np
        a = np.concatenate([np.zeros(1000, np.int16), (np.ones(1000) * 16000).astype(np.int16)])
        lv = L.wave_levels(a, bars=4)
        self.assertEqual(len(lv), 4)
        self.assertEqual((lv[0], lv[1]), (0, 0))
        self.assertGreater(lv[3], 4)
        self.assertEqual(L.wave_levels([], bars=3), (0, 0, 0))
        self.assertTrue(2 <= L.wave_bars_for(0.1) <= L.wave_bars_for(5) < L.wave_bars_for(15))       # バーの長さは喋った秒数に比例

    def test_live_wave_scrolls_from_the_right(self):
        import numpy as np
        loud = (np.ones(3200) * 16000).astype(np.int16)                  # 0.2 秒ぶんの音
        lv = L.live_wave(loud, bars=24)
        self.assertEqual(len(lv), 24)
        self.assertEqual(sum(1 for v in lv if v > 0), 2)                 # 0.1 秒＝1本
        self.assertTrue(all(v == 0 for v in lv[:22]) and all(v > 4 for v in lv[22:]))   # 右端が最新
        long = np.concatenate([(np.ones(1600 * 40) * 16000).astype(np.int16)])
        self.assertTrue(all(v > 4 for v in L.live_wave(long, bars=24)))   # 長い音は直近 24 本だけ
        self.assertEqual(L.live_wave([], bars=5), (0, 0, 0, 0, 0))

    def test_recognition_view_has_no_provisional(self):
        t, _ = L.recognition_view({"active": True, "pending_recognitions": 2, "provisional_count": 5})
        self.assertIn("清書待ち（2件）", t)
        self.assertNotIn("仮認識", t)


if __name__ == "__main__":
    unittest.main()
