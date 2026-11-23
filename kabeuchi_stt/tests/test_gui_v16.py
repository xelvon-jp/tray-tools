# -*- coding: utf-8 -*-
"""フェーズ1.6 の GUI テスト（逐次認識の廃止・引き出しへ移るボタン・表示サイズ・赤いメーター・履歴の右クリックメニュー）。
窓は画面に出さない(WA_DontShowOnScreen)・キー/マウスの実送信なし・音なし・マイクなし・メニューは表示しない(QMenu.exec/popup は呼んだら失敗)。
"""
import os
import sys
import time
import unittest

os.environ["QT_SCALE_FACTOR"] = os.environ.get("KABEUCHI_TEST_SCALE", "1")

import numpy as np

import _common as C
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMenu

import test_gui_v15 as V15   # ダイアログ等の「呼んだら失敗」の保険・GuiBase・偽クリップボードを共用
import ui_style as S
from test_core_runtime import FakeSD
from test_gui_v15 import GuiBase, pump
from ui_main import SettingsDialog


def _forbid(*a, **k):
    raise AssertionError("テスト中に実メニューを表示しようとした（_menu_exec を差し替えること）")


QMenu.exec = _forbid
QMenu.popup = _forbid

app = QApplication.instance()


class LiveFake:
    def __init__(self, on_segment):
        self.on_segment, self.fed, self.started, self.stopped = on_segment, 0, False, False
        self.processed_segments = self.pending_segments = 0
        self.error = None

    def start(self):
        self.started = True

    def stop(self, wait=0):
        self.stopped = True

    def feed(self, x):
        self.fed += 1


class Base(GuiBase):
    def tearDown(self):
        super().tearDown()
        # 表示サイズはプロセス全体の状態。次のテストに持ち越さない
        if S.DISPLAY_SIZE != "small":
            S.set_size("small")
            app.setStyleSheet(S.QSS)
            app.setFont(S.app_font())


class NoLiveRecognitionTest(Base):
    def test_live_recognition_screen_parts_are_gone(self):
        """壁打ちの逐次認識（録音中に仮の文字を出す）は廃止した: チェックも、設定の逐次認識モデルも無い。"""
        w, _ = self.make()
        self.assertFalse(hasattr(w, "chk_live"))
        self.assertFalse(hasattr(w, "_set_live"))
        d = SettingsDialog(w.s, [], w)
        self.assertFalse(hasattr(d, "live_model"))
        self.assertNotIn("live_model", d.values())


class DisplaySizeTest(Base):
    def test_default_is_small_and_switch_applies_without_restart(self):
        w, _ = self.make()
        self.assertEqual(S.DISPLAY_SIZE, "small")
        self.assertEqual(S.BASE_PT, 10)
        small_time_w = w.unified.lbl_time.width()
        small_font = app.font().pointSize()
        w.apply_display_size("large")
        self.assertEqual((S.DISPLAY_SIZE, S.BASE_PT, w.s["display_size"]), ("large", 12, "large"))
        self.assertGreater(w.unified.lbl_time.width(), small_time_w, "時間表示の幅も大きくなる")
        self.assertGreater(app.font().pointSize(), small_font)
        w.apply_display_size("standard")
        self.assertEqual(S.BASE_PT, 11)
        self.assertIn("font-size: 11pt", S.QSS)
        w.apply_display_size("bogus")                    # 不正な値は既定（小）へ
        self.assertEqual(S.DISPLAY_SIZE, "small")

    def test_settings_dialog_has_size_and_applies(self):
        w, _ = self.make()
        d = SettingsDialog(w.s, [], w)
        self.assertEqual(d.size_combo.currentData(), "small")
        self.assertEqual([d.size_combo.itemText(i) for i in range(3)], ["小", "標準", "大"])
        d.size_combo.setCurrentIndex(d.size_combo.findData("large"))
        self.assertEqual(d.values()["display_size"], "large")
        w._exec = lambda dlg: 1
        import ui_main
        orig = ui_main.SettingsDialog
        ui_main.SettingsDialog = lambda *a, **k: d
        try:
            from PySide6.QtWidgets import QDialog
            w._exec = lambda dlg: QDialog.Accepted
            w.open_settings()
        finally:
            ui_main.SettingsDialog = orig
        self.assertEqual(S.DISPLAY_SIZE, "large")         # 再起動せずに反映

    def test_startup_with_saved_size(self):
        w, _ = self.make(display_size="large")
        self.assertEqual(S.DISPLAY_SIZE, "large")

class RedMeterTest(Base):
    def test_meter_turns_red_when_speaking_while_idle(self):
        w, svc = self.make()
        self.assertTrue(pump(8, until=lambda: w.meter.unrecorded), "待機中に話すと即座に赤")
        self.assertEqual(w.meter.toolTip(), "録音されていません")
        w.toggle_record()                                 # 録音中は緑
        self.assertTrue(pump(3, until=lambda: not w.meter.unrecorded))
        pump(0.8)
        self.assertFalse(w.meter.unrecorded)
        self.assertEqual(w.meter.toolTip(), "")
        w.toggle_record()                                 # セッションを終える（録音を止める）
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))

    def test_not_red_when_quiet_monitor_off_or_mic_busy(self):
        w, _ = self.make(sd=FakeSD(C.noise(5, -70, 3), speed=10.0, loop=True))
        pump(1.5)
        self.assertFalse(w.meter.unrecorded, "静かなときは赤にしない")
        w2, _ = self.make(idle_monitor=False)
        pump(1.5)
        self.assertFalse(w2.meter.unrecorded, "待機中モニター OFF")
        w3, _ = self.make(sd=FakeSD(np.zeros(1600, dtype=np.int16), fail=OSError("exclusive")))
        pump(1.5)
        self.assertFalse(w3.meter.unrecorded, "マイクを使用中で開けない")

    def test_hysteresis_holds_briefly_then_clears(self):
        w, svc = self.make(sd=FakeSD(C.noise(5, -70, 3), speed=10.0, loop=True))
        pump(0.5)
        w.timer.stop()
        sess = svc.session
        sess.speaking_now = lambda: True
        t = time.monotonic()
        w._update_meter_unrecorded(t)
        self.assertTrue(w.meter.unrecorded)
        sess.speaking_now = lambda: False
        w._update_meter_unrecorded(t + 0.3)
        self.assertTrue(w.meter.unrecorded, "保持（ちらつき防止）")
        w._update_meter_unrecorded(t + 0.6)
        self.assertFalse(w.meter.unrecorded)

    def test_red_state_renders_differently_with_mark(self):
        w, _ = self.make()
        w.meter.set_level(0.6)
        w.meter.resize(240, 22)
        a = w.meter.grab().toImage()
        w.meter.set_unrecorded(True)
        b = w.meter.grab().toImage()
        self.assertNotEqual(a, b)
        # 赤: 点灯部分の色は緑より赤が強い。右端に印（白い枠）が描かれる
        c = b.pixelColor(12, 11)
        self.assertGreater(c.red(), c.green())
        w.meter.resize(120, 22)
        self.assertNotEqual(w.meter.grab().toImage(), a)   # 狭くても印（●）が出る


class HistoryMenuTest(Base):
    def test_history_menu(self):
        import wave
        from pathlib import Path
        w, svc = self.make()
        wavp = Path(self._d.name) / "h.wav"
        with wave.open(str(wavp), "wb") as f:
            f.setnchannels(1), f.setsampwidth(2), f.setframerate(16000)
            f.writeframes(C.sine(6, -20).tobytes())
        svc.history.add(text="一行目。\n二行目。\n三行目。", segments=[(0, 2, "一行目。"), (2, 4, "二行目。"), (4, 6, "三行目。")],
                        duration=6.0, model="small", wav_path=str(wavp))
        w.reload_history()
        w.lst_hist.setCurrentRow(0)
        pump(0.3)
        items = w.history_menu_items(1, selection="二行")
        self.assertEqual([i.key for i in items], ["copy_selection", "copy_part", "copy_all", "copy_from_here", "-", "play_from"])
        clip = QGuiApplication.clipboard()
        w.run_menu_item(items[1])
        self.assertEqual(clip.text(), "二行目。")
        w.run_menu_item(items[2])
        self.assertEqual(clip.text(), "一行目。\n二行目。\n三行目。")
        w.run_menu_item(items[3])
        self.assertEqual(clip.text(), "二行目。\n三行目。")
        w.run_menu_item(items[0])
        self.assertEqual(clip.text(), "二行")
        w.run_menu_item(items[5])                         # ここから再生（既存のクリック再生と同じ）
        self.assertTrue(pump(5, until=lambda: w.player.is_playing))
        w.player.stop()
        # 経路: 履歴の右クリックも差し替え可能な表示を通る
        shown = []
        w._menu_exec = lambda menu, pos: shown.append([a.text() for a in menu.actions() if a.text()][:3])
        w._on_history_context(0, "", None)
        self.assertEqual(shown[0][:2], ["このパートをコピー", "全文コピー"])


if __name__ == "__main__":
    unittest.main()
