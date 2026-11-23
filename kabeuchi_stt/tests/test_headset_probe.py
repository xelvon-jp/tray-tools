# -*- coding: utf-8 -*-
"""ヘッドセット診断ツール（headset_probe_logic / headset_probe_gui）のテスト。
実デバイスは開かない・フックを入れない・キー/マウスを送らない。窓は画面に出さない(WA_DontShowOnScreen)。"""
import os
import unittest

import _common as C  # noqa: F401
import dialogue_hid as D
import headset_probe_logic as L

P1 = r"\\?\hid#vid_3511&pid_2ef2&mi_00&col01#8&2a&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
P2 = r"\\?\hid#vid_3511&pid_2ef2&mi_00&col02#8&2a&0&0001#{4d1e55b2-f16f-11cf-88cb-001111000030}"
PK = r"\\?\hid#vid_046d&pid_c52b&mi_00#7&1&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"


def rep(*head, n=512):
    return bytes(head) + bytes(n - len(head))


class TrackerTest(unittest.TestCase):
    def test_press_release_pulse(self):
        t = L.ReportTracker()
        e1 = t.feed(rep(1, 1))
        self.assertEqual(e1.kind, L.KIND_PRESS)
        self.assertEqual(e1.changed, [1])
        self.assertEqual(e1.kodama_view, D.PRESS)
        e2 = t.feed(rep(1, 0))
        self.assertEqual(e2.kind, L.KIND_RELEASE)
        self.assertEqual(e2.changed, [1])
        e3 = t.feed(rep(1, 1))                       # 2回目も押す（直前は離す）
        self.assertEqual(e3.kind, L.KIND_PRESS)

    def test_identical_consecutive_thinned(self):
        t = L.ReportTracker()
        self.assertIsNotNone(t.feed(rep(1, 1)))
        self.assertIsNone(t.feed(rep(1, 1)))
        self.assertIsNone(t.feed(rep(1, 1)))
        e = t.feed(rep(1, 0))
        self.assertEqual(e.suppressed_before, 2)
        self.assertEqual(t.total, 4)
        self.assertEqual(t.suppressed_total, 2)

    def test_other_report_id_and_unknown_bits(self):
        t = L.ReportTracker()
        e = t.feed(rep(2, 0x04, 0x01))
        self.assertEqual(e.kind, L.KIND_PRESS)
        self.assertIsNone(e.kodama_view)             # コダマは押下と見なさない
        self.assertEqual(e.changed, [1, 2])
        e = t.feed(rep(2, 0x04, 0x00))               # 0x01 が落ちる
        self.assertEqual(e.kind, L.KIND_RELEASE)
        e = t.feed(rep(2, 0x08, 0x00))               # 0x04 が落ち 0x08 が立つ
        self.assertEqual(e.kind, L.KIND_CHANGE)

    def test_empty_and_short(self):
        t = L.ReportTracker()
        self.assertIsNone(t.feed(b""))
        self.assertIsNone(t.feed(None))
        e = t.feed(bytes([1]))
        self.assertEqual(e.kind, L.KIND_NONE)

    def test_hex_display(self):
        t = L.ReportTracker()
        e = t.feed(rep(1, 1))
        self.assertEqual(L.hex_plain(e), "01 01 00 00 00 00 00 00 …(全512バイト)")
        h = L.hex_html(e)
        self.assertIn(L.HIGHLIGHT_STYLE + '">01</span>', h)
        self.assertEqual(h.count("<span"), 1)        # 変化した1バイトだけ強調
        self.assertEqual(L.change_text(e), "[1] 00→01")
        big = bytearray(512)
        big[0] = 1
        big[40] = 0x80
        e = t.feed(bytes(big))
        self.assertGreaterEqual(L.shown_length(e), 41)
        self.assertLessEqual(L.shown_length(e), L.MAX_SHOW)


class DeviceTest(unittest.TestCase):
    def devs(self):
        return [L.ProbeDevice(PK, "046D:C52B", "Mouse", 1, 2, 8, False),
                L.ProbeDevice(P2, "3511:2EF2", "", 0xFF00, 1, 64, False),
                L.ProbeDevice(P1, "3511:2EF2", "Loop120", 0x0C, 1, 512, True)]

    def test_vid_pid_and_label(self):
        self.assertEqual(L.vid_pid_from_path(P1), "3511:2EF2")
        self.assertEqual(L.vid_pid_from_path("x"), "????:????")
        lab = L.device_label(self.devs()[2])
        for s in ("3511:2EF2", "col01", "0x0C", "512", "Loop120"):
            self.assertIn(s, lab)

    def test_default_pick(self):
        ds = self.devs()
        self.assertEqual(ds[L.pick_default(ds)].path, P1)
        self.assertEqual(L.pick_default([]), -1)
        self.assertEqual(L.pick_default([ds[0]]), 0)    # 既定が無ければ最初の候補
        self.assertEqual(L.sort_devices(ds)[0].path, P1)

    def test_to_info_forces_readable(self):
        info = self.devs()[1].to_info()
        self.assertTrue(info.has_play_pause)
        self.assertEqual(D.rank_candidates([info], "3511:2EF2")[0].path, P2)


class SummaryTest(unittest.TestCase):
    def test_not_started(self):
        self.assertIn("開始", "\n".join(L.build_summary(L.ProbeStats())))

    def test_hotkey_error_text(self):
        self.assertIn("他のアプリ", L.hotkey_error_text(1409))
        self.assertIn("5", L.hotkey_error_text(5))

    def test_hid_ok_hotkey_fail_media_none(self):
        s = L.ProbeStats(started=True, monitoring=True, a_enabled=True, a_ok=True, a_reports=2, a_presses=1,
                         a_releases=1, b_enabled=True, b_ok=False, b_code=1409,
                         b_reason=L.hotkey_error_text(1409), c_enabled=True, c_ok=True)
        t = "\n".join(L.build_summary(s))
        self.assertIn("HID: 押す/離すを検出 → 使えます", t)
        self.assertIn("反応なし（登録失敗", t)
        self.assertIn("他のアプリ", t)
        self.assertIn("メディアキー: 届いていません", t)
        self.assertIn("HID だけで検出できます", t)

    def test_nothing_reacts(self):
        s = L.ProbeStats(started=True, monitoring=True, a_enabled=True, a_ok=True, b_enabled=True, b_ok=True,
                         c_enabled=True, c_ok=True)
        t = "\n".join(L.build_summary(s))
        self.assertIn("HID: 反応なし", t)
        self.assertIn("1回だけ押して", t)
        self.assertIn("占有", t)

    def test_other_button_reports(self):
        s = L.ProbeStats(started=True, monitoring=True, a_enabled=True, a_ok=True, a_reports=3)
        self.assertIn("ではありません", "\n".join(L.build_summary(s)))

    def test_open_failed_and_media_only(self):
        s = L.ProbeStats(started=True, monitoring=True, a_enabled=True, a_ok=False, a_reason="CreateFile 失敗",
                         b_enabled=False, c_enabled=True, c_ok=True, c_counts={0xB3: 2})
        t = "\n".join(L.build_summary(s))
        self.assertIn("開けませんでした", t)
        self.assertIn("独占しない", t)
        self.assertIn("再生/一時停止 2回", t)
        self.assertIn("キーとして届いています", t)

    def test_verdict_of(self):
        self.assertEqual(L.verdict_of(True), "ACCEPT")
        self.assertEqual(L.verdict_of(False, "重複（別経路が…）"), "DUP")
        self.assertEqual(L.verdict_of(False, "間引き（…）"), "DROP")
        self.assertEqual(L.verdict_of(False, "state=stopped"), "NG")

    def test_fmt_time(self):
        self.assertRegex(L.fmt_time(1700000000.123), r"^\d\d:\d\d:\d\d\.12\d$")


class GuiBuildTest(unittest.TestCase):
    def test_build_without_side_effects(self):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
        import headset_probe_gui as G
        devs = [L.ProbeDevice(P2, "3511:2EF2", "", 0xFF00, 1, 64, False),
                L.ProbeDevice(P1, "3511:2EF2", "Loop120", 0x0C, 1, 512, True)]
        w = G.ProbeWindow(lister=lambda: devs, auto_scan=False)
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        w.rescan()
        self.assertEqual(w.combo.count(), 2)
        self.assertEqual(w._selected().path, P1)
        self.assertFalse(w.running)                  # 起動しただけでは監視しない
        self.assertIsNone(w.hb)
        self.assertIsNone(w.keyhook)
        self.assertTrue(w.cb_excl.isChecked())
        # 監視中でないときのイベントは無視される
        w._on_event("report", {"ev": L.ReportTracker().feed(rep(1, 1)), "t": 1.0})
        self.assertEqual(w.stats.a_presses, 0)
        # 監視中扱いにして偽イベントを流す（実デバイスは開かない）
        w.stats.monitoring = w.stats.started = True
        w.stats.a_enabled = w.stats.a_ok = True
        t = L.ReportTracker()
        w._on_event("report", {"ev": t.feed(rep(1, 1)), "t": 1700000000.0})
        w._on_event("report", {"ev": t.feed(rep(1, 0)), "t": 1700000000.1})
        w._on_event("hotkey", 1700000000.2)
        w._on_event("mediakey", {"vk": 0xB3, "inj": False, "t": 1700000000.3})
        w._on_event("verdict", {"via": "hid", "accepted": True, "why": "", "t": 1700000000.4})
        w._on_event("verdict", {"via": "hotkey", "accepted": False, "why": "重複（x）", "t": 1700000000.5})
        s = w.stats
        self.assertEqual((s.a_presses, s.a_releases, s.b_presses, s.c_total, s.k_accept, s.k_dup),
                         (1, 1, 1, 1, 1, 1))
        txt = w.log_text()
        self.assertIn("01 01", txt)
        self.assertIn("押す(推定)", txt)
        self.assertIn("DUP", txt)
        self.assertEqual(w.row_a.count, 1)
        self.assertIn("使えます", w.summary.text())
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "sub", "h.txt")
        self.assertEqual(w.save_log(p), p)
        with open(p, encoding="utf-8") as f:
            self.assertIn("01 01", f.read())
        for i in range(G.MAX_LOG_LINES + 50):
            w._note(f"line {i}")
        self.assertLessEqual(w.log.document().blockCount(), G.MAX_LOG_LINES)
        w.stop()                                     # 何も開いていなくても例外にならない
        w.close()


if __name__ == "__main__":
    unittest.main()
