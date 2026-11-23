# -*- coding: utf-8 -*-
"""HID 直接読み（dialogue_hid）の純部分とリーダーのスレッド。実デバイスは開かない（フェイクの lister / opener）。"""
import ctypes
import threading
import time
import unittest
from pathlib import Path

import _common as C  # noqa: F401
import dialogue_hid as D

P_COL01 = r"\\?\hid#vid_3511&pid_2ef2&mi_00&col01#8&2a&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
P_COL02 = r"\\?\hid#vid_3511&pid_2ef2&mi_00&col02#8&2a&0&0001#{4d1e55b2-f16f-11cf-88cb-001111000030}"
P_OTHER = r"\\?\hid#vid_046d&pid_c52b&mi_00#7&1&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"
P_UPPER = P_COL01.upper()


class PureTest(unittest.TestCase):
    def test_parse_vid_pid(self):
        self.assertEqual(D.parse_vid_pid("3511:2EF2"), (0x3511, 0x2EF2))
        self.assertEqual(D.parse_vid_pid(" 3511 : 2ef2 "), (0x3511, 0x2EF2))
        self.assertEqual(D.parse_vid_pid("VID_3511&PID_2EF2"), (0x3511, 0x2EF2))
        for bad in ("", "3511", "zz:11", "1:2:3", "12345:1", None):
            self.assertIsNone(D.parse_vid_pid(bad), bad)

    def test_path_matches_and_col01(self):
        self.assertTrue(D.path_matches(P_COL01, 0x3511, 0x2EF2))
        self.assertTrue(D.path_matches(P_UPPER, 0x3511, 0x2EF2))
        self.assertFalse(D.path_matches(P_OTHER, 0x3511, 0x2EF2))
        self.assertFalse(D.path_matches("", 0x3511, 0x2EF2))
        self.assertTrue(D.is_col01(P_COL01) and D.is_col01(P_UPPER))
        self.assertFalse(D.is_col01(P_COL02))
        self.assertEqual(D.short_path(P_COL01), "mi_00&col01")

    def test_parse_report(self):
        self.assertEqual(D.parse_report(bytes([1, 1]) + bytes(510)), D.PRESS)
        self.assertEqual(D.parse_report(bytes([1, 0]) + bytes(510)), D.RELEASE)
        self.assertIsNone(D.parse_report(bytes([2, 1, 0])))        # 別のレポート ID
        self.assertIsNone(D.parse_report(bytes([1])))              # 短い
        self.assertIsNone(D.parse_report(b""))
        self.assertIsNone(D.parse_report(None))
        self.assertIsNone(D.parse_report(bytes([1, 0x80])))        # 未知のビット
        self.assertEqual(D.parse_report(bytearray([1, 1, 0])), D.PRESS)

    def test_caps_have_play_pause(self):
        self.assertTrue(D.caps_have_play_pause([(0x0C, False, 0xCD, 0xCD)]))
        self.assertTrue(D.caps_have_play_pause([(0x0C, True, 0x00, 0x3FF)]))     # 配列（範囲）に含まれる
        self.assertFalse(D.caps_have_play_pause([(0x0C, False, 0xE9, 0xE9)]))    # Vol+ だけ
        self.assertFalse(D.caps_have_play_pause([(0x01, False, 0xCD, 0xCD)]))    # ページ違い
        self.assertFalse(D.caps_have_play_pause([]))

    def test_rank_candidates(self):
        infos = [D.HidInfo(P_OTHER, has_play_pause=True), D.HidInfo(P_COL02, has_play_pause=False),
                 D.HidInfo(P_COL01, has_play_pause=None)]
        r = D.rank_candidates(infos)
        self.assertEqual([i.path for i in r], [P_COL01])            # 確認不能でも col01 は保険で残る。他社・別コレクションは除外
        infos = [D.HidInfo(P_COL01, has_play_pause=None), D.HidInfo(P_COL02, has_play_pause=True)]
        self.assertEqual([i.path for i in D.rank_candidates(infos)], [P_COL02, P_COL01])   # caps で確認できたものが先
        self.assertEqual(D.rank_candidates(infos, "bad"), [])
        self.assertEqual(D.rank_candidates([D.HidInfo(P_OTHER)]), [])

    def test_struct_sizes_match_windows(self):
        self.assertEqual(ctypes.sizeof(D._HIDP_BUTTON_CAPS), 72)
        self.assertEqual(ctypes.sizeof(D._HIDP_VALUE_CAPS), 72)
        self.assertEqual(ctypes.sizeof(D._HIDP_CAPS), 64)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(D._SP_DEVICE_INTERFACE_DATA), 32)
            self.assertEqual(ctypes.sizeof(D._OVERLAPPED), 32)


class FakeSource:
    """read_report が、用意したレポートを順に返し、尽きたら cancel まで待つ。"""

    def __init__(self, reports, raise_at_end=None):
        self.reports = list(reports)
        self.raise_at_end = raise_at_end
        self.stop = threading.Event()
        self.closed = False

    def read_report(self):
        if self.reports:
            return self.reports.pop(0)
        if self.raise_at_end:
            raise self.raise_at_end
        self.stop.wait(5)
        return None

    def cancel(self):
        self.stop.set()

    def close(self):
        self.closed = True


def make_reader(infos, source=None, open_error=None):
    opened = []

    def opener(info):
        opened.append(info)
        if open_error:
            raise open_error
        return source
    return D.HidButtonReader("3511:2EF2", lister=lambda: infos, opener=opener), opened


class ReaderTest(unittest.TestCase):
    def test_press_edges_only_and_clean_stop(self):
        press = bytes([1, 1]) + bytes(510)
        rel = bytes([1, 0]) + bytes(510)
        src = FakeSource([press, rel, bytes([2, 1]), press, rel])
        rd, opened = make_reader([D.HidInfo(P_OTHER), D.HidInfo(P_COL02), D.HidInfo(P_COL01, 512, 1, 512, True)], src)
        got, exits = [], []
        r = rd.start(lambda: got.append(1), lambda: exits.append(1))
        self.assertTrue(r.ok, r.reason)
        self.assertEqual([i.path for i in opened], [P_COL01])
        self.assertEqual(rd.device, "mi_00&col01")
        for _ in range(200):
            if len(got) >= 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(got), 2)                       # 離し・別レポート ID は通知しない
        self.assertEqual((rd.reports, rd.presses), (5, 2))
        self.assertTrue(rd.thread_alive())
        rd.stop()
        self.assertFalse(rd.thread_alive())
        self.assertTrue(src.closed)                         # ハンドル（読み取り元）を閉じた
        self.assertEqual(exits, [1])
        self.assertIsNone(rd._source)

    def test_not_found_and_no_candidates_have_reasons(self):
        rd, _ = make_reader([D.HidInfo(P_OTHER)])
        r = rd.start(lambda: None, lambda: None)
        self.assertFalse(r.ok)
        self.assertIn("見つかりません", r.reason)
        rd, _ = make_reader([D.HidInfo(P_COL02, has_play_pause=False)])
        r = rd.start(lambda: None, lambda: None)
        self.assertFalse(r.ok)
        self.assertIn("再生/一時停止ボタンを持つ", r.reason)

    def test_open_failure_returns_reason_and_code(self):
        err = D.HidReadError("CreateFile 失敗（Win32 エラー 5）")
        err.winerror = 5
        rd, _ = make_reader([D.HidInfo(P_COL01)], open_error=err)
        r = rd.start(lambda: None, lambda: None)
        self.assertFalse(r.ok)
        self.assertIn("Win32 エラー 5", r.reason)
        self.assertEqual(r.code, 5)
        self.assertFalse(rd.thread_alive())

    def test_lister_exception_and_bad_id_are_failures_not_crashes(self):
        def boom():
            raise OSError("x")
        rd = D.HidButtonReader("3511:2EF2", lister=boom)
        self.assertIn("列挙に失敗", rd.start(lambda: None, lambda: None).reason)
        rd = D.HidButtonReader("???", lister=lambda: [])
        self.assertIn("読めません", rd.start(lambda: None, lambda: None).reason)

    def test_device_unplugged_ends_loop_and_notifies(self):
        src = FakeSource([], raise_at_end=D.HidReadError("ReadFile 失敗（Win32 エラー 1167）"))
        rd, _ = make_reader([D.HidInfo(P_COL01)], src)
        exited = threading.Event()
        self.assertTrue(rd.start(lambda: None, exited.set).ok)
        self.assertTrue(exited.wait(3))
        self.assertIn("1167", rd.exit_reason)
        self.assertTrue(src.closed)
        rd.stop()

    def test_callback_exception_does_not_stop_reading(self):
        press = bytes([1, 1])
        src = FakeSource([press, press])
        rd, _ = make_reader([D.HidInfo(P_COL01)], src)
        n = []

        def boom():
            n.append(1)
            raise RuntimeError("x")
        self.assertTrue(rd.start(boom, lambda: None).ok)
        for _ in range(200):
            if len(n) >= 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(n), 2)
        rd.stop()

    def test_diagnostics_has_counts(self):
        src = FakeSource([bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])])
        rd, _ = make_reader([D.HidInfo(P_COL01)], src)
        rd.start(lambda: None, lambda: None)
        for _ in range(200):
            if rd.reports:
                break
            threading.Event().wait(0.01)
        d = rd.diagnostics()
        self.assertEqual((d["device"], d["reports"], d["presses"]), ("mi_00&col01", 1, 1))
        self.assertEqual(d["last_report"], "01 01 00 00 00 00 00 00")
        rd.stop()


class SourceSafetyTest(unittest.TestCase):
    def test_import_has_no_side_effects_comtypes_or_writes(self):
        src = (Path(__file__).resolve().parent.parent / "dialogue_hid.py").read_text(encoding="utf-8")
        self.assertNotIn("import comtypes", src)
        self.assertNotIn("pycaw", src)
        # 書き込み系の API 名は、説明文（docstring）以外のコードに現れない
        import ast
        tree = ast.parse(src)
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for bad in ("WriteFile", "HidD_SetFeature", "HidD_SetOutputReport", "SetOutputReport", "SendInput"):
            self.assertNotIn(bad, names)
        # import しただけで DLL を読まない: WinDLL の呼び出しは関数の中だけ
        for n in tree.body:
            if isinstance(n, ast.Expr) and not isinstance(getattr(n, "value", None), ast.Constant):
                self.fail("トップレベルに副作用のある式がある")



class TelephonyInterfaceTest(unittest.TestCase):
    """マイクを使っている間は、多機能ボタンが電話用（Page 0x0B）のインターフェースに出る（実測）。それも押下として扱う。"""

    def test_telephony_report_counts_as_a_press_and_stops_cleanly(self):
        main_src = FakeSource([])                                   # 再生/一時停止のほうは、何も来ない
        tele_src = FakeSource([bytes([2, 0, 0]), bytes([1, 0, 0]), bytes([2, 0, 0])])   # レポートID 2 だけが押下
        sources = {}

        def opener(info):
            return sources[info.path]
        main = D.HidInfo(P_COL01, 0x0C, 1, 512, True)
        tele = D.HidInfo(P_COL02.replace("col02", "col02") + "_tel", D.USAGE_PAGE_TELEPHONY, 5, 3, False)
        sources[main.path], sources[tele.path] = main_src, tele_src
        rd = D.HidButtonReader("3511:2EF2", lister=lambda: [main, tele], opener=opener)
        got = []
        r = rd.start(lambda: got.append(1), lambda: None)
        self.assertTrue(r.ok)
        t0 = time.time()
        while len(got) < 2 and time.time() - t0 < 3:
            time.sleep(0.02)
        self.assertEqual(len(got), 2)
        self.assertEqual(rd.diagnostics()["tele_reports"], 2)
        rd.stop()
        self.assertTrue(tele_src.closed)

if __name__ == "__main__":
    unittest.main()
