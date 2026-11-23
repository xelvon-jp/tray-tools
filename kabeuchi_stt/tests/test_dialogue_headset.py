# -*- coding: utf-8 -*-
"""ヘッドセットボタンの純部分（間引き・状態遷移・解除の保証）。RegisterHotKey は呼ばない（フェイクのバックエンド）。"""
import ast
import unittest
from pathlib import Path

import _common as C  # noqa: F401
import dialogue_headset as H


class FakeBackend:
    def __init__(self, result=None, raise_on_start=None):
        self.result = result or H.HeadsetResult(True)
        self.raise_on_start = raise_on_start
        self.starts = self.stops = 0
        self.on_press = self.on_exit = None

    def start(self, on_press, on_exit):
        self.starts += 1
        if self.raise_on_start:
            raise self.raise_on_start
        self.on_press, self.on_exit = on_press, on_exit
        return self.result

    def stop(self):
        self.stops += 1


class PureTest(unittest.TestCase):
    def test_should_accept_press(self):
        self.assertTrue(H.should_accept_press(1.0, None))
        self.assertFalse(H.should_accept_press(1.1, 1.0, 0.3))
        self.assertTrue(H.should_accept_press(1.3, 1.0, 0.3))
        self.assertTrue(H.should_accept_press(5.0, 1.0, 0.3))

    def test_next_state(self):
        self.assertEqual(H.next_state(H.STOPPED, "start_ok"), H.ACTIVE)
        self.assertEqual(H.next_state(H.STOPPED, "start_fail"), H.FAILED)
        self.assertEqual(H.next_state(H.ACTIVE, "stop"), H.STOPPED)
        self.assertEqual(H.next_state(H.ACTIVE, "error"), H.FAILED)
        self.assertEqual(H.next_state(H.FAILED, "start_ok"), H.ACTIVE)
        self.assertEqual(H.next_state(H.ACTIVE, "start_fail"), H.ACTIVE)   # 二重 start で受付中を壊さない
        self.assertEqual(H.next_state(H.STOPPED, "bogus"), H.STOPPED)

    def test_reason_for_error(self):
        self.assertIn("すでに", H.reason_for_error(H.ERROR_HOTKEY_ALREADY_REGISTERED))
        self.assertIn("5", H.reason_for_error(5))

    def test_constants(self):
        self.assertEqual(H.VK_MEDIA_PLAY_PAUSE, 0xB3)


class ButtonTest(unittest.TestCase):
    def make(self, **kw):
        self.t = [0.0]
        self.calls = []
        be = kw.pop("backend", None) or FakeBackend()
        hb = H.HeadsetButton(lambda: self.calls.append(self.t[0]), clock=lambda: self.t[0], backend=be, **kw)
        return hb, be

    def press(self, be, at):
        self.t[0] = at
        be.on_press()

    def test_press_flow_and_debounce(self):
        hb, be = self.make(min_interval=0.3)
        self.assertEqual(hb.state, H.STOPPED)
        self.assertFalse(hb.active)
        self.assertTrue(hb.start().ok)
        self.assertTrue(hb.active)
        self.press(be, 1.0)
        self.press(be, 1.1)     # 間引かれる
        self.press(be, 4.0)
        self.assertEqual(self.calls, [1.0, 4.0])
        self.assertEqual((hb.presses, hb.dropped), (2, 1))
        hb.stop()

    def test_failure_reports_reason(self):
        hb, be = self.make(backend=FakeBackend(H.HeadsetResult(False, "他のアプリが使用中")))
        r = hb.start()
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "他のアプリが使用中")
        self.assertEqual(hb.state, H.FAILED)
        self.assertEqual(hb.last_reason, "他のアプリが使用中")
        self.assertFalse(hb.active)
        be.on_press()            # 失敗状態の押下は通知しない
        self.assertEqual(self.calls, [])

    def test_backend_exception_is_a_failure_not_a_crash(self):
        hb, _ = self.make(backend=FakeBackend(raise_on_start=OSError("nope")))
        r = hb.start()
        self.assertFalse(r.ok)
        self.assertIn("nope", r.reason)
        self.assertEqual(hb.state, H.FAILED)

    def test_stop_always_releases(self):
        hb, be = self.make()
        hb.start()
        hb.stop()
        hb.stop()
        self.assertEqual(hb.state, H.STOPPED)
        self.assertGreaterEqual(be.stops, 1)
        self.press(be, 9.0)       # 停止後の押下は通知しない
        self.assertEqual(self.calls, [])

    def test_failed_start_stop_still_calls_backend_stop(self):
        hb, be = self.make(backend=FakeBackend(H.HeadsetResult(False, "x")))
        hb.start()
        hb.stop()
        self.assertEqual(be.stops, 1)

    def test_callback_exception_does_not_break_loop(self):
        t = [0.0]
        be = FakeBackend()

        def boom():
            raise RuntimeError("x")
        hb = H.HeadsetButton(boom, clock=lambda: t[0], backend=be)
        hb.start()
        be.on_press()
        t[0] = 5.0
        be.on_press()
        self.assertEqual(hb.presses, 2)
        hb.stop()

    def test_loop_exit_marks_failed(self):
        hb, be = self.make()
        hb.start()
        be.on_exit()              # stop() 以外でループが止まった
        self.assertEqual(hb.state, H.FAILED)
        self.assertTrue(hb.last_reason)
        hb.stop()
        self.assertEqual(hb.state, H.STOPPED)

    def test_restart_resets_debounce(self):
        hb, be = self.make(min_interval=10.0)
        hb.start()
        self.press(be, 1.0)
        hb.stop()
        hb.start()
        self.press(be, 1.5)       # 再開直後の最初の押下は受ける
        self.assertEqual(self.calls, [1.0, 1.5])
        hb.stop()


class FakeHid:
    """HID 経路のフェイク（dialogue_hid.HidButtonReader 互換）。"""

    def __init__(self, ok=True, reason="", code=0):
        self.ok, self.reason, self.code = ok, reason, code
        self.starts = self.stops = 0
        self.on_press = self.on_exit = None
        self.exit_reason = ""

    def start(self, on_press, on_exit):
        self.starts += 1
        self.on_press, self.on_exit = on_press, on_exit
        return H.HeadsetResult(self.ok, self.reason, self.code)

    def stop(self):
        self.stops += 1

    def diagnostics(self):
        return {"device": "mi_00&col01", "reports": 7, "presses": 2, "last_report": "01 01"}


class PathsTest(unittest.TestCase):
    """検出経路（HID 直接読み・ホットキー）の組み合わせ。"""

    def make(self, hid=None, hot=None, **kw):
        self.t = [0.0]
        self.calls, self.logs = [], []
        self.hid = hid if hid is not None else FakeHid()
        self.hot = hot if hot is not None else FakeBackend()
        hb = H.HeadsetButton(lambda: self.calls.append(self.t[0]), clock=lambda: self.t[0], backend=self.hot,
                             hid_backend=self.hid, log=lambda ev, **f: self.logs.append((ev, f)), wall=lambda: 0.0, **kw)
        return hb

    def press(self, which, at):
        self.t[0] = at
        (self.hid if which == "hid" else self.hot).on_press()

    def test_classify_press_pure(self):
        c = H.classify_press
        self.assertEqual(c(1.0, None, None, "hid"), H.ACCEPT)
        self.assertEqual(c(1.2, 1.0, "hid", "hotkey"), H.DUP)        # 別経路・0.5 秒以内 = 重複
        self.assertEqual(c(1.6, 1.0, "hid", "hotkey"), H.ACCEPT)
        self.assertEqual(c(1.1, 1.0, "hid", "hid"), H.DROP)          # 同じ経路・短すぎる連続
        self.assertEqual(c(1.4, 1.0, "hid", "hid"), H.ACCEPT)
        self.assertEqual(c(3.9, 1.0, "hid", "hid"), H.ACCEPT)

    def test_normalize_mode(self):
        self.assertEqual([H.normalize_mode(x) for x in ("auto", "HID", "hotkey", "x", None, "")],
                         ["auto", "hid", "hotkey", "auto", "auto", "auto"])

    def test_hid_ok_hotkey_fails_is_active_via_hid(self):
        hb = self.make(hot=FakeBackend(H.HeadsetResult(False, "他のアプリが使用中", 1409)))
        r = hb.start()
        self.assertTrue(r.ok)
        self.assertEqual((r.paths, r.exclusive, r.reason), (("hid",), False, ""))
        self.assertEqual(hb.state, H.ACTIVE)
        self.assertEqual(hb.register_code, 1409)                      # ホットキーの失敗コードは診断に残る
        self.press("hid", 1.0)
        self.assertEqual(self.calls, [1.0])
        d = hb.diagnostics()
        self.assertEqual(d["paths"]["hotkey"]["ok"], False)
        self.assertEqual(d["paths"]["hotkey"]["code"], 1409)
        self.assertTrue(d["paths"]["hid"]["ok"])
        self.assertEqual((d["paths"]["hid"]["received"], d["paths"]["hotkey"]["received"]), (1, 0))
        self.assertEqual(d["paths"]["hid"]["detail"]["reports"], 7)
        self.assertFalse(d["exclusive"])
        hb.stop()

    def test_hotkey_ok_hid_fails_is_active_exclusive(self):
        hb = self.make(hid=FakeHid(False, "HID デバイスが見つかりません"))
        r = hb.start()
        self.assertTrue(r.ok)
        self.assertEqual((r.paths, r.exclusive), (("hotkey",), True))
        self.press("hotkey", 1.0)
        self.assertEqual(self.calls, [1.0])
        self.assertIn("見つかりません", hb.diagnostics()["paths"]["hid"]["reason"])
        hb.stop()

    def test_both_fail_reports_both_reasons(self):
        hb = self.make(hid=FakeHid(False, "開けません"), hot=FakeBackend(H.HeadsetResult(False, "使用中", 1409)))
        r = hb.start()
        self.assertFalse(r.ok)
        self.assertEqual(hb.state, H.FAILED)
        self.assertIn("開けません", r.reason)
        self.assertIn("使用中", r.reason)
        self.assertEqual(r.code, 1409)
        self.assertFalse(hb.active)
        self.hid.on_press()                                            # 失敗状態の押下は通知しない
        self.assertEqual(self.calls, [])

    def test_both_ok_dedups_same_press_and_logs_via(self):
        hb = self.make(min_interval=0.3)
        r = hb.start()
        self.assertEqual((r.ok, r.paths, r.exclusive), (True, ("hid", "hotkey"), True))
        self.press("hid", 1.0)
        self.press("hotkey", 1.05)        # 同じ押下を別経路が拾った → 重複
        self.press("hotkey", 4.0)
        self.press("hid", 4.02)           # 重複
        self.assertEqual(self.calls, [1.0, 4.0])
        self.assertEqual((hb.presses, hb.dropped, hb.deduped, hb.received), (2, 2, 2, 4))
        press_logs = [f for e, f in self.logs if e == "headset_press"]
        self.assertEqual([(f["via"], f["accepted"]) for f in press_logs],
                         [("hid", True), ("hotkey", False), ("hotkey", True), ("hid", False)])
        self.assertIn("重複", press_logs[1]["why"])
        hb.stop()

    def test_same_path_quick_repeat_is_dropped_but_later_press_accepted(self):
        hb = self.make(min_interval=0.3)
        hb.start()
        self.press("hid", 1.0)
        self.press("hid", 1.1)            # 3秒未満の連続（同じ経路）
        self.press("hid", 4.5)
        self.assertEqual(self.calls, [1.0, 4.5])
        hb.stop()

    def test_one_path_exit_keeps_active_both_exit_fails(self):
        hb = self.make()
        hb.start()
        self.hid.on_exit()
        self.assertEqual(hb.state, H.ACTIVE)                           # ホットキーが生きている
        self.assertFalse(hb.diagnostics()["paths"]["hid"]["alive"])
        self.hot.on_exit()
        self.assertEqual(hb.state, H.FAILED)
        self.assertTrue(hb.last_reason)
        hb.stop()
        self.assertEqual(hb.state, H.STOPPED)

    def test_mode_hid_only_never_touches_hotkey_and_mode_hotkey_never_touches_hid(self):
        hb = self.make(mode="hid")
        r = hb.start()
        self.assertEqual((r.ok, r.paths, self.hot.starts, self.hid.starts), (True, ("hid",), 0, 1))
        self.assertFalse(hb.diagnostics()["paths"]["hotkey"]["enabled"])
        hb.stop()
        self.assertEqual(self.hot.stops, 0)
        hb = self.make(mode="hotkey")
        r = hb.start()
        self.assertEqual((r.ok, r.paths, self.hot.starts, self.hid.starts), (True, ("hotkey",), 1, 0))
        hb.stop()

    def test_mode_hid_failure_does_not_fall_back_to_hotkey(self):
        hb = self.make(mode="hid", hid=FakeHid(False, "開けません"))
        r = hb.start()
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "開けません")
        self.assertEqual(self.hot.starts, 0)

    def test_stop_stops_every_path(self):
        hb = self.make()
        hb.start()
        hb.stop()
        self.assertEqual((self.hid.stops, self.hot.stops), (1, 1))
        self.assertEqual(hb.state, H.STOPPED)
        self.hid.on_press()                                            # 停止後の押下は通知しない
        self.assertEqual(self.calls, [])

    def test_backend_only_means_no_real_hid(self):
        hb = H.HeadsetButton(lambda: None, backend=FakeBackend())
        self.assertEqual(list(hb._backends), ["hotkey"])               # テストで実機の HID を開かない
        self.assertFalse(hb.diagnostics()["paths"]["hid"]["enabled"])

    def test_diagnostics_and_register_log_have_paths(self):
        hb = self.make(hot=FakeBackend(H.HeadsetResult(False, "x", 1409)))
        hb.start()
        ev = [f for e, f in self.logs if e == "headset_register"][0]
        self.assertEqual((ev["ok"], ev["paths"], ev["mode"], ev["exclusive"]), (True, "hid", "auto", False))
        pv = {f["via"]: f for e, f in self.logs if e == "headset_path"}
        self.assertEqual((pv["hid"]["ok"], pv["hotkey"]["ok"], pv["hotkey"]["code"]), (True, False, 1409))
        d = hb.diagnostics()
        self.assertEqual(d["mode"], "auto")
        self.assertIsNone(d["thread_alive"])                           # フェイクは thread_alive を持たない


class ImportSafetyTest(unittest.TestCase):
    def test_import_has_no_win32_side_effects_and_no_com(self):
        src = (Path(__file__).resolve().parent.parent / "dialogue_headset.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        top_imports = set()
        for n in tree.body:          # 先頭レベルの import だけ（関数の中の ctypes は start() 以降）
            if isinstance(n, ast.Import):
                top_imports |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                top_imports.add((n.module or "").split(".")[0])
        self.assertNotIn("ctypes", top_imports)
        self.assertNotIn("import comtypes", src)
        self.assertNotIn("import pycaw", src)
        self.assertNotIn("from pycaw", src)
        # 副作用のあるトップレベルの呼び出し文が無い（docstring 以外の式文が無い）
        for n in tree.body[1:]:
            self.assertNotIsInstance(n, ast.Expr)


class DiagnosticsTest(unittest.TestCase):
    """診断（ログ・状態）。実機で「効かない」ときに、どこまで届いているかを後から読めるようにする。"""

    def make(self, **kw):
        self.t = [0.0]
        self.calls, self.logs = [], []
        be = kw.pop("backend", None) or FakeBackend()
        hb = H.HeadsetButton(lambda: self.calls.append(1), clock=lambda: self.t[0], backend=be,
                             log=lambda ev, **f: self.logs.append((ev, f)), wall=lambda: 0.0, **kw)
        return hb, be

    def names(self):
        return [e for e, _ in self.logs]

    def test_register_result_is_logged_with_error_code(self):
        hb, _ = self.make(backend=FakeBackend(H.HeadsetResult(False, "他のアプリが使用中", 1409)))
        hb.start()
        ev, f = self.logs[0]
        self.assertEqual(ev, "headset_register")
        self.assertEqual((f["ok"], f["code"]), (False, 1409))
        d = hb.diagnostics()
        self.assertEqual((d["state"], d["register_code"]), (H.FAILED, 1409))
        self.assertIn("使用中", d["reason"])
        hb2, _ = self.make()
        hb2.start()
        self.assertEqual(self.logs[-1][1]["ok"], True)
        self.assertEqual(hb2.diagnostics()["register_code"], 0)

    def test_every_press_is_logged_accepted_or_dropped(self):
        hb, be = self.make(min_interval=0.3)
        hb.start()
        self.t[0] = 1.0
        be.on_press()
        self.t[0] = 1.1
        be.on_press()         # 間引き
        accepted = [f["accepted"] for e, f in self.logs if e == "headset_press"]
        self.assertEqual(accepted, [True, False])
        d = hb.diagnostics()
        self.assertEqual((d["presses"], d["dropped"], d["received"]), (1, 1, 2))
        self.assertTrue(d["last_press"])

    def test_press_while_stopped_is_logged_but_not_delivered(self):
        hb, _ = self.make()
        hb._on_press()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.logs[-1][0], "headset_press")
        self.assertFalse(self.logs[-1][1]["accepted"])

    def test_press_during_registration_is_not_lost(self):
        """登録の応答を待っている間（状態が ACTIVE になる前）に届いた押下も受ける。"""
        hb, _ = self.make()

        class Early(FakeBackend):
            def start(s, on_press, on_exit):
                on_press()           # 登録直後・start() が戻る前に押された
                return H.HeadsetResult(True)
        hb._backends["hotkey"] = Early()
        hb.start()
        self.assertEqual(self.calls, [1])
        hb.stop()

    def test_callback_error_and_unexpected_exit_are_logged(self):
        hb, be = self.make()

        def boom():
            raise RuntimeError("x")
        hb.callback = boom
        hb.start()
        be.on_press()
        self.assertIn("headset_callback_error", self.names())
        be.on_exit()
        ev, f = [x for x in self.logs if x[0] == "headset_loop_exit"][0]
        self.assertTrue(f["unexpected"])
        self.assertEqual(hb.diagnostics()["state"], H.FAILED)

    def test_log_line_has_no_dialogue_text_and_rotates_small(self):
        import dialogue_log as DL
        line = DL.fmt_line("commit_call", {"source": "headset", "draft_chars": 12}, now=0.0)
        self.assertIn("commit_call source=headset draft_chars=12", line)
        self.assertEqual(DL.MAX_BYTES, 128 * 1024)
        DL.dlog("test_event", n=1)        # 書けても書けなくても例外は出ない


if __name__ == "__main__":
    unittest.main()
