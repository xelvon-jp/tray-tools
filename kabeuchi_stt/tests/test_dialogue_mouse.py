# -*- coding: utf-8 -*-
"""マウスのサイドボタンで確定する経路。実際の WH_MOUSE_LL フックは入れない・マウス入力は注入しない
（判定は純関数 decide_mouse_event、配線はフェイクのバックエンドで確認する）。"""
import unittest

import _common as C  # noqa: F401
import dialogue_mouse as M
import ui_dialogue_logic as L
from test_dialogue_controller import Rig
from test_dialogue_session import wait_until


def MK(btn):
    return (btn & 0xFFFF) << 16   # mouseData の上位ワード


class DecideTest(unittest.TestCase):
    def test_back_down_up_swallowed_down_notifies(self):
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON1), 0, "back"), (True, True))
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONUP, MK(M.XBUTTON1), 0, "back"), (True, False))

    def test_forward(self):
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON2), 0, "forward"), (True, True))
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONUP, MK(M.XBUTTON2), 0, "forward"), (True, False))

    def test_other_button_passes_through(self):
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON2), 0, "back"), (False, False))
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONUP, MK(M.XBUTTON1), 0, "forward"), (False, False))

    def test_non_xbutton_messages_pass_through(self):
        for msg in (0x0200, 0x0201, 0x0202, 0x020A, 0x0204, 0x0207):   # 移動・左・左up・ホイール・右・中
            self.assertEqual(M.decide_mouse_event(msg, MK(M.XBUTTON1), 0, "back"), (False, False))

    def test_injected_passes_through(self):
        for fl in (M.LLMHF_INJECTED, M.LLMHF_LOWER_IL_INJECTED, 3):
            self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON1), fl, "back"), (False, False))

    def test_disabled_always_passes_through(self):
        for b in ("", None, "left"):
            self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON1), 0, b), (False, False))

    def test_low_word_of_mouse_data_ignored(self):
        self.assertEqual(M.decide_mouse_event(M.WM_XBUTTONDOWN, MK(M.XBUTTON1) | 0x0002, 0, "back"), (True, True))

    def test_normalize(self):
        self.assertEqual([M.normalize_button(x) for x in ("back", " Forward ", "", None, "x")],
                         ["back", "forward", "", "", ""])


class FakeBE:
    def __init__(self, ok=True, raise_on_start=None):
        self.ok, self.raise_on_start = ok, raise_on_start
        self.starts = self.stops = 0
        self.on_press = None

    def start(self, on_press, on_exit):
        self.starts += 1
        if self.raise_on_start:
            raise self.raise_on_start
        self.on_press, self.on_exit = on_press, on_exit
        return M.MouseResult(True) if self.ok else M.MouseResult(False, "フックを入れられません", 5)

    def stop(self):
        self.stops += 1


class MouseButtonTest(unittest.TestCase):
    def make(self, button="back", be=None):
        self.logs, self.calls = [], []
        self.now = [100.0]
        be = be or FakeBE()
        mb = M.MouseButton(lambda: self.calls.append(1), button, backend=be, clock=lambda: self.now[0],
                           log=lambda ev, **f: self.logs.append((ev, f)))
        return mb, be

    def test_empty_setting_starts_nothing(self):
        mb, be = self.make("")
        self.assertFalse(mb.start().ok)
        self.assertEqual(be.starts, 0)
        self.assertEqual(mb.state, "stopped")

    def test_start_stop_and_press(self):
        mb, be = self.make()
        self.assertTrue(mb.start().ok)
        self.assertTrue(mb.active)
        be.on_press()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(any(ev == "mouse_press" and f["via"] == "mouse" and f["accepted"] for ev, f in self.logs))
        mb.stop()
        self.assertEqual((be.stops, mb.state), (1, "stopped"))
        be.on_press()                    # 停止後の押下は受けない
        self.assertEqual(len(self.calls), 1)

    def test_rapid_press_thinned_but_later_accepted(self):
        mb, be = self.make()
        mb.start()
        be.on_press()
        self.now[0] += 0.1
        be.on_press()
        self.now[0] += 0.5
        be.on_press()
        self.assertEqual((len(self.calls), mb.dropped), (2, 1))
        mb.stop()

    def test_failure_is_returned_and_logged_not_raised(self):
        mb, be = self.make(be=FakeBE(ok=False))
        r = mb.start()
        self.assertFalse(r.ok)
        self.assertEqual((mb.state, mb.last_reason), ("failed", "フックを入れられません"))
        self.assertTrue(any(ev == "mouse_register" and f["via"] == "mouse" and not f["ok"] for ev, f in self.logs))
        mb.stop()

    def test_backend_exception_is_contained(self):
        mb, be = self.make(be=FakeBE(raise_on_start=RuntimeError("boom")))
        r = mb.start()
        self.assertFalse(r.ok)
        self.assertIn("boom", r.reason)

    def test_unexpected_exit_marks_failed(self):
        mb, be = self.make()
        mb.start()
        be.on_exit()
        self.assertEqual(mb.state, "failed")
        mb.stop()

    def test_callback_error_does_not_propagate(self):
        mb = M.MouseButton(lambda: 1 / 0, "back", backend=FakeBE(), log=lambda *a, **k: None)
        mb.start()
        mb._on_press()
        mb.stop()

    def test_status_line(self):
        self.assertIn("使わない", L.mouse_status_line({}, ""))
        self.assertIn("受付中", L.mouse_status_line({"state": "active", "presses": 2}, "back"))
        self.assertIn("戻る", L.mouse_status_line({"state": "stopped"}, "back"))
        s = L.mouse_status_line({"state": "failed", "reason": "なぜか"}, "forward")
        self.assertIn("受け付けられません", s)
        self.assertIn("なぜか", s)

    def test_press_notice(self):
        text, _ = L.press_notice("mouse", L.PRESS_COMMIT)
        self.assertIn("マウスのボタン", text)

    def test_win32_backend_untouched_until_start(self):
        b = M._Win32MouseBackend("back")
        self.assertFalse(b.thread_alive())
        b.stop()          # 未開始の stop は何もしない


class MouseControllerTest(unittest.TestCase):
    def rig(self, mouse="back", mouse_be=None, **kw):
        r = Rig(dialogue={"mouse_button": mouse, **kw})
        made = []

        def factory(cb, button):
            mb = M.MouseButton(cb, button, backend=mouse_be or FakeBE(), min_interval=0.0, log=lambda *a, **k: None)
            made.append(mb)
            return mb

        r.ctl._mouse_factory = factory
        r.made = made
        return r

    def test_start_stop_follow_session(self):
        r = self.rig()
        try:
            r.ctl.start()
            mb = r.made[0]
            self.assertEqual(mb._backend.starts, 1)
            self.assertEqual(r.ctl.mouse_state(), "active")
            r.ctl.stop()
            self.assertGreaterEqual(mb._backend.stops, 1)
            self.assertEqual(r.ctl.mouse_state(), "stopped")
        finally:
            r.close()

    def test_press_commits_and_delivers(self):
        r = self.rig()
        try:
            r.ctl.start()
            r.say()
            r.made[0]._backend.on_press()
            self.assertTrue(wait_until(lambda: r.client.delivered and not r.ctl.ds.buffer.delivering))
            self.assertEqual(r.client.delivered[0], ("相談窓", "こんにちは。元気ですか。"))
            r.ctl.stop()
        finally:
            r.close()

    def test_works_when_headset_disabled(self):
        r = self.rig(headset_button=False)
        try:
            r.ctl.start()
            self.assertIsNone(r.headset)
            self.assertEqual(r.ctl.mouse_state(), "active")
            r.ctl.stop()
        finally:
            r.close()

    def test_empty_setting_does_not_create_or_start(self):
        r = self.rig(mouse="")
        try:
            r.ctl.start()
            self.assertEqual(r.made, [])
            self.assertIsNone(r.ctl.mouse)
            r.ctl.stop()
        finally:
            r.close()

    def test_failure_reported_and_headset_independent(self):
        r = self.rig(mouse_be=FakeBE(ok=False))
        try:
            r.ctl.start()
            self.assertEqual(r.ctl.mouse_state(), "failed")
            self.assertIn("フック", r.ctl.mouse_reason)
            self.assertIn("mouse_failed", r.names())
            self.assertEqual(r.headset.state, "active")       # ヘッドセットは失敗扱いにならない
            r.ctl.stop()
        finally:
            r.close()

    def test_shutdown_stops_mouse(self):
        r = self.rig()
        try:
            r.ctl.start()
            be = r.made[0]._backend
            r.ctl.shutdown()
            self.assertGreaterEqual(be.stops, 1)
        finally:
            r.tmp.cleanup()

    def test_default_setting_is_empty(self):
        import settings as S
        self.assertEqual(S.dialogue_config({})["mouse_button"], "")


if __name__ == "__main__":
    unittest.main()
