# -*- coding: utf-8 -*-
"""統合 GUI の判定ロジック（ui_unified_logic）のテスト。Qt 不要。"""
import unittest

import _common as C  # noqa: F401
import ui_dialogue_logic as L
import ui_unified_logic as U
from mic_check import Issue


def issue(code, sev="warn"):
    return Issue(code, sev, f"msg-{code}", f"adv-{code}")


class ChipStateTest(unittest.TestCase):
    def st(self, **kw):
        base = dict(recording=False, paused=False, issues=(), monitoring=True, no_audio=False, busy=False,
                    speaking_unrecorded=False, measured=True)
        base.update(kw)
        return U.chip_state(**base)

    def test_all_eight_states_plus_wait_are_reachable(self):
        got = {
            "ok": self.st(recording=True),
            "low": self.st(recording=True, issues=[issue("low")]),
            "clip": self.st(recording=True, issues=[issue("clipping")]),
            "noise": self.st(recording=True, issues=[issue("noisy")]),
            "none": self.st(recording=True, no_audio=True),
            "busy": self.st(busy=True, monitoring=False),
            "idle": self.st(speaking_unrecorded=True),
            "paused": self.st(recording=True, paused=True, speaking_unrecorded=True),
            "wait": self.st(monitoring=False),
        }
        for k, v in got.items():
            self.assertEqual(k, v)
        self.assertEqual(set(got), set(U.CHIP_STATES))
        self.assertEqual(set(U.CHIP_ORDER), set(U.CHIP_STATES))

    def test_priority(self):
        # マイク使用中 > 話し声（未録音）> 音が入っていない > 音量の指摘（音割れ > 小さい > ノイズ）
        self.assertEqual(self.st(busy=True, speaking_unrecorded=True, no_audio=True), "busy")
        self.assertEqual(self.st(speaking_unrecorded=True, no_audio=True, issues=[issue("clipping")]), "idle")
        self.assertEqual(self.st(recording=True, no_audio=True, issues=[issue("clipping")]), "none")
        self.assertEqual(self.st(recording=True, issues=[issue("noisy"), issue("low"), issue("clipping")]), "clip")
        self.assertEqual(self.st(recording=True, issues=[issue("noisy"), issue("low")]), "low")
        self.assertEqual(self.st(recording=True, issues=[issue("silent", "error")]), "none")

    def test_busy_only_matters_when_not_recording(self):
        self.assertEqual(self.st(recording=True, busy=True), "ok")

    def test_idle_waits_until_measured(self):
        self.assertEqual(self.st(measured=False), "wait")
        self.assertEqual(self.st(monitoring=False), "wait")
        self.assertEqual(self.st(), "ok")

    def test_paused_needs_recording(self):
        self.assertEqual(self.st(recording=False, paused=True, speaking_unrecorded=True), "idle")

    def test_labels_are_the_agreed_wording(self):
        want = ["入力OK", "音が小さい", "音割れ", "ノイズ大（要相談）", "音が入っていない", "マイク使用中",
                "未録音（話しています）", "一時停止中（話しています）"]
        self.assertEqual([U.CHIP_STATES[k][0] for k in U.CHIP_ORDER[:8]], want)

    def test_detail_uses_the_specific_issue_text(self):
        t, body, adv = U.chip_detail("low", [issue("low")])
        self.assertEqual((t, body, adv), ("音が小さい", "msg-low", "adv-low"))
        t, body, adv = U.chip_detail("ok", [])
        self.assertIn("良好", body)
        self.assertTrue(U.chip_detail("unknown-key", [])[0])


class ButtonsTest(unittest.TestCase):
    def test_single_rec_button_is_session_start_stop(self):
        self.assertEqual(U.rec_button_view(False)[:3], ("録音", "rec", True))
        self.assertEqual(U.rec_button_view(True)[:3], ("停止", "stop", True))
        self.assertIn("セッション", U.rec_button_view(True)[3])
        self.assertFalse(U.rec_button_view(False, dialogue_ok=False)[2])        # 無効設定
        self.assertFalse(U.rec_button_view(True, ending=True)[2])               # 終了処理中
        self.assertFalse(hasattr(U, "MODE_WALL"))                               # 壁打ちモードは廃止

    def test_commit_text(self):
        self.assertEqual(U.commit_button_text(L.PRESS_COMMIT), "確定して届ける")
        self.assertEqual(U.commit_button_text(L.PRESS_RETRY), "未配達を再試行")

    def test_pause_button_is_hold(self):
        en, on, tip = U.pause_button_view(True, True)
        self.assertEqual((en, on), (True, True))
        self.assertIn("保留を解除", tip)
        en, on, tip = U.pause_button_view(True, False)
        self.assertEqual((en, on), (True, False))
        self.assertIn("保留", tip)
        self.assertFalse(U.pause_button_view(False, False)[0])

    def test_clock(self):
        self.assertEqual(U.fmt_clock(None), "--:--")
        self.assertEqual(U.fmt_clock(133.4), "02:13")
        self.assertEqual(U.fmt_clock(3725), "1:02:05")
        self.assertEqual(U.fmt_clock(-3), "00:00")


class SpeakingTextTest(unittest.TestCase):
    def test_speaking_length(self):
        self.assertEqual(U.fmt_speaking(12.4), "喋っています 0:12")
        self.assertEqual(U.fmt_speaking(0), "喋っています 0:00")
        self.assertEqual(U.fmt_speaking(75), "喋っています 1:15")
        self.assertEqual(U.fmt_speaking(None), "—")                           # 無音のあいだ


class FinalizeProgressTest(unittest.TestCase):
    def test_nothing_running_shows_nothing(self):
        self.assertEqual(U.finalize_progress_text(None, 0.0, 0, running=False), "")

    def test_running_with_estimate(self):
        # 100 秒で 35% → 残り 約 186 秒 → あと約4分
        self.assertEqual(U.finalize_progress_text(0.35, 100.0, 0), "清書中 35%（あと約4分）")
        self.assertEqual(U.finalize_progress_text(0.5, 90.0, 0), "清書中 50%（あと約2分）")
        self.assertEqual(U.finalize_progress_text(0.9, 90.0, 0), "清書中 90%（あと約10秒）")

    def test_waiting_jobs_are_appended(self):
        self.assertEqual(U.finalize_progress_text(0.35, 100.0, 1), "清書中 35%（あと約4分）（待機 1 件）")
        self.assertEqual(U.finalize_progress_text(None, 0.0, 2, running=False), "清書の待機 2 件")

    def test_no_estimate_when_too_early(self):
        self.assertEqual(U.finalize_progress_text(0.0, 0.0, 0), "清書中 0%")
        self.assertEqual(U.finalize_progress_text(0.02, 60.0, 0), "清書中 2%")           # 進みが小さすぎる
        self.assertEqual(U.finalize_progress_text(0.5, 2.0, 1), "清書中 50%（待機 1 件）")  # 経過が短すぎる

    def test_remaining_wording(self):
        self.assertEqual(U.fmt_remaining(3), "あと約10秒")
        self.assertEqual(U.fmt_remaining(44), "あと約40秒")
        self.assertEqual(U.fmt_remaining(61), "あと約2分")
        self.assertEqual(U.fmt_remaining(3600), "あと約1時間")
        self.assertEqual(U.fmt_remaining(4000), "あと約1時間7分")


class LayoutLogicTest(unittest.TestCase):
    def test_h3_height_grows_with_font(self):
        self.assertLess(U.draft_lines_height(20, 28, 14), U.draft_lines_height(30, 28, 14))


class StatusBadgeTest(unittest.TestCase):
    def snap(self, **kw):
        d = dict(active=True, held=False, speaking=False, silence_sec=0.0, pending_recognitions=0, delivering=False)
        d.update(kw)
        return d

    def test_states_and_priority(self):
        sv = U.status_view
        self.assertEqual(sv({"active": False}), ("停止中", "stop"))
        self.assertEqual(sv(self.snap(speaking=True)), ("録音中", "good"))
        self.assertEqual(sv(self.snap()), ("無音待機中", "warn"))
        self.assertEqual(sv(self.snap(speaking=True, silence_sec=0.6)), ("無音待機中", "warn"))
        self.assertEqual(sv(self.snap(pending_recognitions=2, speaking=True)), ("認識中（2件）", "busy"))
        self.assertEqual(sv(self.snap(delivering=True, pending_recognitions=2)), ("送信中", "info"))
        self.assertEqual(sv(self.snap(held=True)), ("保留中", "warn"))
        self.assertEqual(sv(self.snap(), done=True)[0], "✓ 完了")
        self.assertEqual(sv(self.snap(speaking=True), done=True)[0], "録音中")
        self.assertEqual(sv({"active": False}, alert=True)[1], "alert")

    def test_finalize_on_the_badge_only_while_stopped(self):
        sv = U.status_view
        self.assertEqual(sv({"active": False}, finalize="清書中 35%（あと約3分）"), ("清書中 35%（あと約3分）", "busy"))
        self.assertEqual(sv({"active": False}, finalize="清書中 35%（待機 1 件）")[1], "busy")
        self.assertEqual(sv({"active": False}, finalize="清書待ち（録音が終わったら始めます）"), ("清書待ち", "warn"))
        self.assertEqual(sv({"active": False}, finalize="清書の待機 2 件"), ("清書待ち", "warn"))
        self.assertEqual(sv({"active": False}, finalize=""), ("停止中", "stop"))
        self.assertEqual(sv({"active": False}, alert=True, finalize="清書中 5%")[1], "alert")     # 発話警告が先
        self.assertEqual(sv(self.snap(delivering=True), finalize="清書中 5%"), ("送信中", "info"))   # セッション中は出さない
        self.assertEqual(sv(self.snap(pending_recognitions=1), finalize="清書中 5%")[1], "busy")
        self.assertEqual(sv(self.snap(speaking=True), finalize="清書中 5%"), ("録音中", "good"))

    def test_commit_button_waiting_text(self):
        import ui_dialogue_logic as L
        self.assertEqual(U.commit_button_text(L.PRESS_COMMIT), "確定して届ける")
        self.assertEqual(U.commit_button_text(L.PRESS_COMMIT, 2), "確定して届ける（認識待ち 2件…）")
        self.assertEqual(U.commit_button_text(L.PRESS_RETRY, 2), "未配達を再試行")

    def test_send_head_for_unconfirmed(self):
        import ui_dialogue_logic as L
        i = {"stage": "uia", "sent": "unknown"}
        self.assertTrue(L.send_unconfirmed(i))
        self.assertEqual(L.send_done_head(i), "⚠ 届きました。送信を確認できませんでした（入力欄を見てください）")
        self.assertFalse(L.send_unconfirmed({"stage": "record", "sent": "unknown"}))
        self.assertFalse(L.send_unconfirmed({"sent": "sent"}))
        self.assertEqual(L.send_done_head({"sent": "sent"}), "✓ 送信しました")
        self.assertEqual(L.send_done_head({"stage": "record"}), "✓ 記録しました")

    def test_countdown(self):
        c = U.silence_countdown_text
        self.assertEqual(c(self.snap(speaking=True, silence_sec=0.5), 1.2), "無音 0.5秒（あと0.7秒で認識開始）")
        self.assertEqual(c(self.snap(speaking=True, silence_sec=0.1), 1.2), "")       # 話している
        self.assertEqual(c(self.snap(speaking=False, silence_sec=5.0), 1.2), "")      # 発話が無い
        self.assertEqual(c(self.snap(speaking=True, silence_sec=0.5, held=True), 1.2), "")
        self.assertNotIn("送信", c(self.snap(speaking=True, silence_sec=0.5), 1.2))

    def test_models_text(self):
        self.assertEqual(U.models_text("small", "medium"), "認識 small ／ 清書 medium")
        self.assertEqual(U.models_text("small", "medium", False), "認識 small ／ 清書 なし")

    def test_alert_blink_and_due(self):
        a, b = U.alert_blink_colors(0.0), U.alert_blink_colors(U.ALERT_BLINK_PERIOD_SEC / 2 + 0.01)
        self.assertNotEqual(a, b)
        self.assertEqual(a, U.alert_blink_colors(U.ALERT_BLINK_PERIOD_SEC + 0.01))
        kw = dict(run_sec=2.0, now=100.0, last_at=None, enabled=True, monitoring=True, session_active=False)
        self.assertTrue(U.idle_alert_due(**kw))
        self.assertFalse(U.idle_alert_due(**{**kw, "monitoring": False}))            # 待機中のマイク確認が無効
        self.assertFalse(U.idle_alert_due(**{**kw, "enabled": False}))
        self.assertFalse(U.idle_alert_due(**{**kw, "session_active": True}))
        self.assertFalse(U.idle_alert_due(**{**kw, "last_at": 70.0}))                # 60秒は繰り返さない
        self.assertTrue(U.idle_alert_due(**{**kw, "last_at": 30.0}))



class HoldAlertColorTest(unittest.TestCase):
    def test_hold_alert_is_amber_and_stop_alert_is_red(self):
        on_h, off_h = U.alert_blink_colors(0.0, "hold"), U.alert_blink_colors(0.3, "hold")
        self.assertEqual(on_h, U.ALERT_BLINK_ON_AMBER)
        self.assertEqual(off_h, U.ALERT_BLINK_OFF_AMBER)
        self.assertEqual(U.alert_blink_colors(0.0), U.ALERT_BLINK_ON)       # 既定は赤（停止中）
        self.assertNotEqual(U.ALERT_BLINK_ON[1], U.ALERT_BLINK_ON_AMBER[1])

if __name__ == "__main__":
    unittest.main(verbosity=1)
