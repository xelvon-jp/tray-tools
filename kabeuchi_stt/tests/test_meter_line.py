# -*- coding: utf-8 -*-
"""認識開始ライン（音量バー上のしきい値）の純関数。画面は見ない。"""
import unittest

import _common  # noqa: F401
import meter_state as M


class MeterLineTest(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(M.clamp_line_db(-34.4), -34.0)
        self.assertEqual(M.clamp_line_db(-100), M.LINE_DB_MIN)
        self.assertEqual(M.clamp_line_db(5), M.LINE_DB_MAX)
        for bad in (None, "", "abc", float("nan"), float("inf")):
            self.assertEqual(M.clamp_line_db(bad), M.LINE_DB_DEFAULT)

    def test_x_db_roundtrip(self):
        left, w = 10.0, 300.0
        self.assertEqual(M.x_to_line_db(left, left, w), -60.0)
        self.assertEqual(M.x_to_line_db(left + w, left, w), M.LINE_DB_MAX)     # 右端は上限へ丸め
        self.assertEqual(M.x_to_line_db(left + w / 2, left, w), -30.0)   # バーの中央＝-30 dB（目盛りの話。既定値とは別）
        self.assertEqual(M.x_to_line_db(-500, left, w), -60.0)
        for db in (-55.0, -30.0, -12.0):
            x = M.line_db_to_x(db, left, w)
            self.assertEqual(M.x_to_line_db(x, left, w), db)
        self.assertEqual(M.x_to_line_db(5, 0, 0), M.LINE_DB_DEFAULT)

    def test_store(self):
        s = {}
        self.assertEqual(M.store_voice_min(s, -34.4), -34.0)
        self.assertEqual(s["dialogue"]["voice_min_dbfs"], -34.0)
        s = {"dialogue": {"voice_min_dbfs": None, "model": "small"}}
        M.store_voice_min(s, None)
        self.assertEqual(s["dialogue"]["voice_min_dbfs"], -50.0)
        self.assertEqual(s["dialogue"]["model"], "small")


if __name__ == "__main__":
    unittest.main()
