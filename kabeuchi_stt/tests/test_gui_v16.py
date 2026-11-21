# -*- coding: utf-8 -*-
"""フェーズ1.6 の GUI テスト（逐次認識の録音中切替・一時停止中の話し声の帯・ボタン2案・表示サイズ・赤いメーター・右クリックメニュー）。
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
from PySide6.QtGui import QFontMetrics, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu

import test_gui_v15 as V15   # ダイアログ等の「呼んだら失敗」の保険・GuiBase・偽クリップボードを共用
import ui_style as S
from test_core_runtime import FakeSD
from test_gui_v15 import GuiBase, pump
from ui_menu import MenuItem
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


class LiveToggleGuiTest(Base):
    def test_live_checkbox_is_usable_while_recording(self):
        w, svc = self.make(live_enabled=False)
        svc.session.live_factory = lambda seg, st: LiveFake(seg)
        w.toggle_record()
        self.assertTrue(svc.session.is_recording)
        self.assertTrue(w.chk_live.isEnabled(), "録音中でも逐次認識のチェックは押せる")
        self.assertIsNone(svc.session.live)
        w.chk_live.setChecked(True)                       # 録音中に ON
        self.assertIsNotNone(svc.session.live)
        self.assertTrue(svc.session.live.started)
        self.assertTrue(svc.session.is_recording)
        live = svc.session.live
        pump(0.3, until=lambda: live.fed > 0)
        self.assertGreater(live.fed, 0)
        w.chk_live.setChecked(False)                      # 録音中に OFF
        self.assertIsNone(svc.session.live)
        self.assertTrue(pump(2, until=lambda: live.stopped))
        self.assertTrue(svc.session.is_recording, "切り替えても録音は止まらない")
        self.assertFalse(w.s["live_enabled"])
        w.toggle_record()

    def test_live_checkbox_enabled_when_idle_too(self):
        w, _ = self.make()
        self.assertTrue(w.chk_live.isEnabled())
        self.assertIn("録音中でも", w.chk_live.toolTip())


class PausedBandTest(Base):
    def test_paused_band_appears_when_speaking_while_paused(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.5)
        self.assertTrue(w.idle_band.isHidden(), "録音中（一時停止でない）は帯を出さない")
        w.toggle_pause()
        self.assertTrue(pump(8, until=lambda: not w.idle_band.isHidden()), "一時停止中に話すと帯")
        self.assertIn("一時停止中です", w.idle_label.text())
        self.assertEqual(w.btn_idle_rec.text(), "再開")
        w.btn_idle_rec.click()                            # 帯の［再開］
        self.assertFalse(svc.session.is_paused)
        self.assertTrue(svc.session.is_recording)
        self.assertTrue(w.idle_band.isHidden())
        w.toggle_record()

    def test_paused_band_hidden_window_and_disabled(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.3)
        w.toggle_pause()
        w.hide()
        pump(1.5)
        self.assertTrue(w.idle_band.isHidden(), "窓が隠れているときは出さない")
        w2, svc2 = self.make(idle_prompt_enabled=False)
        w2.toggle_record()
        pump(0.3)
        w2.toggle_pause()
        pump(2.5)
        self.assertTrue(w2.idle_band.isHidden(), "設定 OFF では一時停止中も出さない")
        w.show()
        w.toggle_record()
        w2.toggle_record()

    def test_band_texts_in_compact(self):
        w, _ = self.make()
        w.set_compact(True)
        w.show_idle_band("paused")
        self.assertEqual(w.idle_label.text(), "一時停止中です")
        w.show_idle_band("idle")
        self.assertEqual(w.idle_label.text(), "録音していません")
        self.assertEqual(w.btn_idle_rec.text(), "録音開始")


class ButtonStyleTest(Base):
    def test_label_and_icon_modes(self):
        w, _ = self.make()
        tools = (w.btn_mictest, w.btn_wav, w.btn_folder, w.btn_settings, w.btn_compact, w.btn_pin)
        w.apply_button_style("label")
        for b in tools:
            self.assertTrue(b.text(), "案1はアイコン+ラベル")
            self.assertFalse(b.icon().isNull())
            self.assertEqual(b.objectName(), "ghost")
            self.assertNotIn("📌", b.text())                  # 絵文字は使わない
        w.apply_button_style("icon")
        for b in tools:
            self.assertEqual(b.text(), "", "案2はアイコンのみ")
            self.assertTrue(b.toolTip(), "ツールチップ付き")
            self.assertEqual(b.objectName(), "ghostIcon")
            self.assertEqual((b.width(), b.height()), (36, 34))
        w.apply_button_style("label")

    def test_toggle_state_and_icons_differ(self):
        w, _ = self.make()
        self.assertTrue(w.btn_pin.isCheckable() and w.btn_compact.isCheckable())
        w.btn_pin.setChecked(True)
        pump(0.1)
        self.assertTrue(w.btn_pin.isChecked())
        ic = w.btn_pin.icon()
        off = ic.pixmap(16, 16, QIcon.Normal, QIcon.Off).toImage()
        on = ic.pixmap(16, 16, QIcon.Normal, QIcon.On).toImage()
        self.assertNotEqual(off, on, "トグル ON のアイコンはアクセント色")
        dis = ic.pixmap(16, 16, QIcon.Disabled, QIcon.Off).toImage()
        self.assertNotEqual(off, dis)
        w.set_compact(True)
        self.assertTrue(w.btn_compact.isChecked())
        self.assertEqual(w.btn_compact.text(), "", "コンパクト中は狭いのでアイコンのみ")
        w.set_compact(False)
        self.assertFalse(w.btn_compact.isChecked())

    def test_rec_pause_buttons_have_icons_no_glyphs(self):
        w, _ = self.make()
        self.assertFalse(w.btn_rec.icon().isNull())
        self.assertEqual(w.btn_rec.text(), "録音開始")
        w.toggle_record()
        self.assertEqual(w.btn_rec.text(), "停止して清書")
        self.assertFalse(w.btn_pause.icon().isNull())
        w.toggle_pause()
        self.assertEqual(w.btn_pause.text(), "再開")
        w.toggle_record()

    def test_layout_fits_in_both_modes(self):
        for mode in ("label", "icon"):
            w, _ = self.make()
            w.apply_button_style(mode)
            for width in (480, 700, 1000):
                w.resize(width, 760)
                V15.TopBarTest.check_bar(self, w, f"{mode} {width}px")


class DisplaySizeTest(Base):
    def check_bar(self, w, label):
        V15.TopBarTest.check_bar(self, w, label)

    def test_default_is_small_and_switch_applies_without_restart(self):
        w, _ = self.make()
        self.assertEqual(S.DISPLAY_SIZE, "small")
        self.assertEqual(S.BASE_PT, 10)
        small_time_w = w.lbl_time.width()
        small_font = app.font().pointSize()
        w.apply_display_size("large")
        self.assertEqual((S.DISPLAY_SIZE, S.BASE_PT, w.s["display_size"]), ("large", 12, "large"))
        self.assertGreater(w.lbl_time.width(), small_time_w, "時間表示の幅も大きくなる")
        self.assertGreater(app.font().pointSize(), small_font)
        w.apply_display_size("standard")
        self.assertEqual(S.BASE_PT, 11)
        self.assertIn("font-size: 11pt", S.QSS)
        w.apply_display_size("bogus")                    # 不正な値は既定（小）へ
        self.assertEqual(S.DISPLAY_SIZE, "small")

    def test_layout_holds_for_every_size_and_width(self):
        w, _ = self.make()
        for size in ("small", "standard", "large"):
            w.apply_display_size(size)
            for width in (480, 700, 1000):
                w.resize(width, 760)
                self.check_bar(w, f"{size} {width}px")
            w.set_compact(True)
            for width in (420, 560):
                w.resize(width, 320)
                self.check_bar(w, f"{size} compact {width}px")
            w.set_compact(False)

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

    def test_text_fits_without_clipping_at_large(self):
        w, _ = self.make()
        w.apply_display_size("large")
        w.resize(480, 800)
        pump(0.3)
        for b in (w.btn_rec, w.btn_pause, w.btn_mictest, w.btn_wav, w.btn_folder, w.btn_settings):
            fm = QFontMetrics(b.font())
            self.assertGreaterEqual(b.width(), fm.horizontalAdvance(b.text()), b.text())


class RedMeterTest(Base):
    def test_meter_turns_red_when_speaking_while_idle_and_paused(self):
        w, svc = self.make()
        self.assertTrue(pump(8, until=lambda: w.meter.unrecorded), "待機中に話すと即座に赤")
        self.assertEqual(w.meter.toolTip(), "録音されていません")
        w.toggle_record()                                 # 録音中は緑
        self.assertTrue(pump(3, until=lambda: not w.meter.unrecorded))
        pump(0.8)
        self.assertFalse(w.meter.unrecorded)
        self.assertEqual(w.meter.toolTip(), "")
        w.toggle_pause()                                  # 一時停止中に話すと赤
        self.assertTrue(pump(6, until=lambda: w.meter.unrecorded))
        w.toggle_pause()
        self.assertTrue(pump(3, until=lambda: not w.meter.unrecorded))
        w.toggle_record()

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


class ContextMenuTest(Base):
    def setup_final(self, w, svc):
        from timeline import Timeline
        tl = Timeline(30)
        tl.feed(C.sine(80, -10))
        tl.set_final([(1.0, 20.0, "最初の文"), (21.0, 25.0, "二番目です。"), (45.0, 50.0, "三番目")], 80)
        w.timeline_view.set_timeline(tl)
        w.timeline_view.refresh()
        pump(0.1)
        return tl

    def test_timeline_items_and_copy_results(self):
        w, svc = self.make()
        self.setup_final(w, svc)
        items = w.timeline_menu_items(0)
        self.assertEqual([i.key for i in items], ["copy_part", "copy_all", "copy_from_here"])
        clip = QGuiApplication.clipboard()
        w.run_menu_item(items[0])
        self.assertEqual(clip.text(), "最初の文二番目です。")                # その行（30 秒のまとまり）だけ・整形の改行なし
        self.assertIn("コピーしました（このパート）", w.statusBar().currentMessage())
        w._menu_source = "timeline"
        w.run_menu_item(items[1])                                       # 全文: 清書の出力と同じ整形（すき間の改行）
        from paragraph import format_for_output
        tl = w.timeline_view._model._tl
        self.assertEqual(clip.text(), format_for_output(tl.final_segments(), w.s, tl.final_gaps()))
        self.assertIn("全文", w.statusBar().currentMessage())
        # ここから下: 2 行目（30 秒〜）以降だけ
        rows = w._timeline_rows()
        idx2 = next(i for i, r in enumerate(rows) if r.start >= 30 and r.text)
        w.run_menu_item(MenuItem("copy_from_here", "x", index=idx2))
        self.assertEqual(clip.text(), "三番目")
        w.run_menu_item(MenuItem("copy_from_here", "x", index=0))
        self.assertTrue(clip.text().startswith("最初の文") and clip.text().endswith("三番目"))

    def test_before_final_uses_visible_rows_in_time_order(self):
        w, svc = self.make()
        from timeline import Timeline
        tl = Timeline(30)
        tl.feed(C.sine(100, -10))
        tl.add_draft(2, 5, "仮一")
        tl.add_draft(40, 45, "仮二")
        w.timeline_view.set_timeline(tl)
        w.timeline_view.refresh()
        w._menu_source = "timeline"
        w.run_menu_item(MenuItem("copy_all"))
        self.assertEqual(QGuiApplication.clipboard().text(), "仮一\n仮二")

    def test_wave_row_shows_no_row_items_and_empty_menu_is_not_shown(self):
        w, svc = self.make()
        from timeline import Timeline
        tl = Timeline(30)
        tl.feed(C.sine(100, -10))
        w.timeline_view.set_timeline(tl)
        w.timeline_view.refresh()
        self.assertEqual(w.timeline_menu_items(0), [])
        called = []
        w._menu_exec = lambda menu, pos: called.append(1)
        w._on_timeline_context(0, None)
        self.assertEqual(called, [], "項目が無ければメニューを出さない")

    def test_show_menu_goes_through_replaceable_exec(self):
        w, svc = self.make()
        self.setup_final(w, svc)
        seen = {}

        def fake_exec(menu, pos):
            seen["labels"] = [a.text() for a in menu.actions() if not a.isSeparator()]
            return next(a for a in menu.actions() if a.text() == "このパートをコピー")
        w._menu_exec = fake_exec
        w._on_timeline_context(0, None)
        self.assertEqual(seen["labels"], ["このパートをコピー", "全文コピー", "ここから下をコピー"])
        self.assertEqual(QGuiApplication.clipboard().text(), "最初の文二番目です。")

    def test_context_event_emits_signal_not_menu(self):
        w, svc = self.make()
        self.setup_final(w, svc)
        got = []
        w.timeline_view.contextRequested.connect(lambda i, pos: got.append(i))
        from PySide6.QtGui import QContextMenuEvent
        from PySide6.QtCore import QPoint
        ev = QContextMenuEvent(QContextMenuEvent.Mouse, QPoint(60, 20), QPoint(60, 20))
        w._menu_exec = lambda menu, pos: None            # 表示しない
        w.timeline_view.contextMenuEvent(ev)              # 内部呼び出し（OS へは何も送らない）。QMenu は出ない
        self.assertEqual(len(got), 1)

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
        w._menu_source = "history"
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
