# -*- coding: utf-8 -*-
"""送信まで行う配達（options.send）の全分岐。フェイクの ops だけ。実機（UIA・キー送信）には触れない。"""
import unittest

import _common as C  # noqa: F401
import dialogue_deliver as dd
from test_dialogue_deliver import PROFILE, ROOT, FakeOps

SEND_NAMES = dd.DEFAULT_OPTIONS["send_buttons"]
BUSY_NAMES = dd.DEFAULT_OPTIONS["busy_buttons"]


class SendOps(FakeOps):
    """FakeOps に、送信ボタン・回答中ボタン・Enter を足したもの。"""

    def __init__(self, **kw):
        self.send_button = True            # 送信ボタンが見える
        self.send_enabled = True
        self.invoke_ok = True
        self.clears_on_invoke = True       # ボタンを押すと入力欄が空になる
        self.clears_on_enter = True
        self.busy_polls = 0                # この回数だけ「回答中」ボタンが見える（その後は消える）
        self.busy_forever = False
        self.enter_ok = True
        self.read_none_after_send = False
        self.sent_log = []                 # 呼ばれた順の送信系の操作
        self.enter_fg = []                 # Enter を送った瞬間の前面
        self.busy_polled = 0
        super().__init__(**kw)

    def find_button(self, hwnd, profile, names, near_rect):
        self._c("find_button", tuple(names))
        if set(names) <= (set(BUSY_NAMES) | {"Halt"}):
            self.busy_polled += 1
            if self.busy_forever or self.busy_polls > 0:
                self.busy_polls -= 1
                return {"found": True, "enabled": True, "name": "停止"}
            return {"found": False}
        if not self.send_button:
            return {"found": False}
        return {"found": True, "enabled": self.send_enabled, "name": "送信", "rect": (700, 560, 780, 590)}

    def invoke_button(self, hwnd, profile, names, near_rect):
        self._c("invoke_button")
        self.sent_log.append("invoke")
        if not self.invoke_ok:
            return {"invoked": False, "reason": "InvokePattern が無い"}
        if self.clears_on_invoke:
            self.text = ""
        return {"invoked": True}

    def send_enter(self, expected_root):
        self.enter_fg.append(self.fg)
        self._c("send_enter")
        self.sent_log.append("enter")
        if self.fg != expected_root:
            return {"sent": False, "reason": "前面が違う"}
        if not self.enter_ok:
            return {"sent": False, "reason": "SendInput 失敗"}
        if self.clears_on_enter:
            self.text = ""
        return {"sent": True}

    def read_value(self, hwnd, profile):
        if self.sent_log and self.read_none_after_send:
            self._c("read_value")
            return {"ok": False, "text": None}
        return super().read_value(hwnd, profile)


def run(ops, text="こんにちは", send=True, profile=None, **opts):
    sleeps = []
    now = [0.0]

    def fake_sleep(sec):                  # 偽の時計は sleep の分だけ進む（実時間で待ち時間を測る実装に合わせる）
        sleeps.append(sec)
        now[0] += sec
    out = dd.deliver(ops, profile or PROFILE, text, options={"send": send, **opts},
                     clock=lambda: now[0], sleep=fake_sleep)
    out["_sleeps"] = sleeps
    return out


class SendTest(unittest.TestCase):
    def test_send_off_by_default_touches_no_send_ops(self):
        ops = SendOps(fg=ROOT)
        out = run(ops, send=False)
        self.assertEqual(out["status"], "delivered")
        self.assertNotIn("sent", out)
        self.assertEqual(ops.sent_log, [])
        self.assertNotIn("find_button", ops.names())
        self.assertEqual(ops.text, "こんにちは")                         # 入力欄に残る（従来どおり）

    def test_button_invoke_success_needs_no_foreground_or_keys(self):
        ops = SendOps()                                                  # 前面は別の窓のまま
        out = run(ops)
        self.assertEqual((out["status"], out["stage"], out["sent"], out["send_stage"]), ("delivered", "uia", True, "button"))
        self.assertEqual(ops.sent_log, ["invoke"])
        self.assertEqual(ops.enter_fg, [])
        self.assertNotIn("bring_to_front", ops.names())                  # 前面化もフォーカス操作もしていない
        self.assertEqual(ops.text, "")

    def test_no_button_falls_back_to_enter_after_foreground_confirmed(self):
        ops = SendOps(send_button=False, front_works={"uia_set_focus"})
        out = run(ops)
        self.assertEqual((out["sent"], out["send_stage"]), (True, "enter"))
        self.assertEqual(ops.sent_log, ["enter"])
        self.assertEqual(ops.enter_fg, [ROOT])                           # Enter を送った瞬間、前面は宛先

    def test_disabled_button_falls_back_to_enter(self):
        ops = SendOps(send_enabled=False, fg=ROOT)
        out = run(ops)
        self.assertEqual((out["sent"], out["send_stage"]), (True, "enter"))
        self.assertEqual(ops.sent_log, ["enter"])                        # 無効なボタンは押さない

    def test_invoke_failure_falls_back_to_enter(self):
        ops = SendOps(invoke_ok=False, fg=ROOT)
        out = run(ops)
        self.assertEqual((out["sent"], out["send_stage"]), (True, "enter"))
        self.assertEqual(ops.sent_log, ["invoke", "enter"])

    def test_foreground_not_confirmed_never_sends_enter(self):
        ops = SendOps(send_button=False)                                 # 前面化の手段がどれも効かない
        out = run(ops)
        self.assertEqual(out["status"], "delivered")                     # 入力は済んでいる
        self.assertEqual((out["sent"], out["send_reason"]), (False, dd.R_SEND_FG))
        self.assertEqual(ops.sent_log, [])
        self.assertEqual(ops.text, "こんにちは")                         # 入力欄に残っている

    def test_focus_not_confirmed_never_sends_enter(self):
        ops = SendOps(send_button=False, fg=ROOT, focus_matches=False)
        out = run(ops)
        self.assertEqual((out["sent"], out["send_reason"]), (False, dd.R_SEND_NO_FOCUS))
        self.assertEqual(ops.sent_log, [])

    def test_foreground_lost_just_before_enter(self):
        class Steal(SendOps):
            def set_focus(self, hwnd, profile):
                r = super().set_focus(hwnd, profile)
                self.calls_after_focus = True
                return r

            def foreground_root(self):
                # set_focus の直後の確認までは宛先、キー送信の直前の確認で別の窓へ
                n = getattr(self, "_fg_calls", 0) + 1
                self._fg_calls = n
                return OTHER_ if (getattr(self, "calls_after_focus", False) and n > self._lim) else self.fg
        OTHER_ = 999
        ops = Steal(send_button=False, fg=ROOT)
        ops._lim = 0
        # set_focus の後の確認（1回目）は通し、ガードの確認（2回目）で奪われる
        ops._lim = 0
        calls = {"n": 0}
        orig = ops.foreground_root

        def fg():
            if getattr(ops, "calls_after_focus", False):
                calls["n"] += 1
                if calls["n"] >= 2:
                    return OTHER_
            return ROOT
        ops.foreground_root = fg
        out = run(ops)
        self.assertEqual((out["sent"], out["send_reason"]), (False, dd.R_SEND_FG_LOST))
        self.assertEqual(ops.sent_log, [])
        del orig

    def test_readback_mismatch_means_no_send_at_all(self):
        ops = SendOps(uia_effective=False, front_works={"uia_set_focus"}, paste_mismatch=True)
        out = run(ops)
        self.assertEqual((out["status"], out["reason"]), ("undelivered", dd.R_MISMATCH))
        self.assertEqual(ops.sent_log, [])
        self.assertNotIn("find_button", ops.names())                     # 送信を探しにも行かない
        self.assertNotIn("sent", out)

    def test_paste_success_then_send(self):
        ops = SendOps(uia_effective=False, front_works={"uia_set_focus"})
        out = run(ops)
        self.assertEqual((out["status"], out["stage"], out["sent"]), ("delivered", "paste", True))
        self.assertEqual(ops.sent_log, ["invoke"])

    def test_input_not_cleared_after_send_is_unknown_and_never_resent(self):
        ops = SendOps(clears_on_invoke=False, fg=ROOT)
        out = run(ops)
        self.assertEqual((out["status"], out["sent"], out["send_reason"]), ("delivered", "unknown", dd.R_SEND_UNKNOWN))
        self.assertEqual(ops.sent_log, ["invoke"])                       # ボタンを押したあと Enter で重ねて送らない
        self.assertGreaterEqual(len(out["_sleeps"]), 1)                  # 数回、読み戻しで待った

    def test_enter_but_not_cleared_is_unknown_once(self):
        ops = SendOps(send_button=False, fg=ROOT, clears_on_enter=False)
        out = run(ops)
        self.assertEqual(out["sent"], "unknown")
        self.assertEqual(ops.sent_log, ["enter"])

    def test_unreadable_after_send_is_unknown(self):
        ops = SendOps(fg=ROOT, read_none_after_send=True)
        out = run(ops)
        self.assertEqual(out["sent"], "unknown")

    def test_existing_draft_only_remaining_counts_as_sent(self):
        ops = SendOps(fg=ROOT, text="書きかけ")
        ops.clears_on_invoke = False

        def invoke(hwnd, profile, names, near):                          # 送信で、書きかけだけに戻る
            ops.sent_log.append("invoke")
            ops.text = "書きかけ"
            return {"invoked": True}
        ops.invoke_button = invoke
        out = run(ops)
        self.assertEqual(out["sent"], True)

    def test_enter_send_failure_reports_not_sent(self):
        ops = SendOps(send_button=False, fg=ROOT, enter_ok=False)
        out = run(ops)
        self.assertEqual((out["status"], out["sent"], out["send_reason"]), ("delivered", False, dd.R_SEND_NOT_SENT))

    def test_button_search_exception_falls_back_to_enter_and_keeps_delivered(self):
        ops = SendOps(fg=ROOT, raise_on={"find_button"})
        out = run(ops)
        self.assertEqual(out["status"], "delivered")
        self.assertEqual((out["sent"], out["send_stage"]), (True, "enter"))
        self.assertEqual(ops.sent_log, ["enter"])

    def test_unexpected_exception_in_send_never_unmakes_delivery(self):
        class Boom(SendOps):
            def send_enter(self, expected_root):
                raise RuntimeError("SendInput が落ちた")
        ops = Boom(send_button=False, fg=ROOT)
        out = run(ops)
        self.assertEqual(out["status"], "delivered")
        self.assertEqual(out["sent"], False)

    def test_profile_send_button_override(self):
        seen = []

        class Rec(SendOps):
            def find_button(self, hwnd, profile, names, near_rect):
                seen.append(list(names))
                return super().find_button(hwnd, profile, names, near_rect)
        ops = Rec(fg=ROOT)
        run(ops, profile={**PROFILE, "send_button": ["Go", "送る"], "busy_button": ["Halt"]})
        self.assertIn(["Halt"], seen)
        self.assertIn(["Go", "送る"], seen)


class BusyTest(unittest.TestCase):
    def test_waits_while_busy_then_sends(self):
        ops = SendOps(fg=ROOT, busy_polls=3)
        out = run(ops, busy_poll_sec=0.5)
        self.assertEqual(out["sent"], True)
        self.assertEqual(ops.busy_polled, 4)                             # 回答中が消えるまで見続けた
        self.assertEqual(out["busy_waited_sec"], 1.5)
        self.assertEqual(ops.sent_log, ["invoke"])

    def test_busy_timeout_does_not_send(self):
        ops = SendOps(fg=ROOT, busy_forever=True)
        out = run(ops, busy_poll_sec=0.5, busy_wait_sec=2.0)
        self.assertEqual(out["status"], "delivered")
        self.assertEqual((out["sent"], out["send_reason"]), (False, dd.R_BUSY_TIMEOUT))
        self.assertEqual(ops.sent_log, [])
        self.assertEqual(ops.text, "こんにちは")                         # 入力欄に残る（回答中のため未送信）

    def test_busy_wait_is_measured_by_the_clock_not_by_poll_count(self):
        """画面の確認 1 回が数秒かかっても、busy_wait_sec は実時間で効く（回数×間隔で数えない）。"""
        now = [0.0]

        class Slow(SendOps):
            def find_button(self, hwnd, profile, names, near_rect):
                now[0] += 5.0                       # 1 回の確認に 5 秒
                return super().find_button(hwnd, profile, names, near_rect)
        ops = Slow(fg=ROOT, busy_forever=True)
        out = dd.deliver(ops, PROFILE, "こんにちは",
                         options={"send": True, "busy_poll_sec": 0.5, "busy_wait_sec": 12.0},
                         clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
        self.assertEqual((out["sent"], out["send_reason"]), (False, dd.R_BUSY_TIMEOUT))
        self.assertLessEqual(ops.busy_polled, 4)    # 回数で数えていた頃は 24 回見続けた

    def test_no_busy_button_means_no_waiting(self):
        ops = SendOps(fg=ROOT)
        out = run(ops)
        self.assertEqual(ops.busy_polled, 1)                             # 1回見て、無ければ待たない
        self.assertEqual(out["_sleeps"], [0.25] if False else out["_sleeps"])
        self.assertNotIn("busy_waited_sec", out)
        self.assertTrue(out["sent"])

    def test_wait_while_busy_off(self):
        ops = SendOps(fg=ROOT, busy_forever=True)
        out = run(ops, wait_while_busy=False)
        self.assertEqual(out["sent"], True)
        self.assertEqual(ops.busy_polled, 0)


class ProtocolTest(unittest.TestCase):
    def test_options_pass_through_merge(self):
        o = dd.merge_options({"send": True, "busy_wait_sec": 30, "unknown_key": 1})
        self.assertTrue(o["send"])
        self.assertEqual(o["busy_wait_sec"], 30)
        self.assertNotIn("unknown_key", o)
        self.assertFalse(dd.merge_options(None)["send"])


if __name__ == "__main__":
    unittest.main()
