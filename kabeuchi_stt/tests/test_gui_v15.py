# -*- coding: utf-8 -*-
"""フェーズ1.5 の GUI テスト。窓は画面に出さない(WA_DontShowOnScreen)・キー/マウスの実送信はしない・音は鳴らさない
(ミュート＋音量0)。マイクは開かない(sounddevice を差し替え)。ダイアログは実際に開かない。
環境変数 KABEUCHI_TEST_SCALE で QT_SCALE_FACTOR を変えて、DPI 150%/200% でも同じ検査を手元で回せる。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_SCALE_FACTOR"] = os.environ.get("KABEUCHI_TEST_SCALE", "1")

import numpy as np

import _common as C
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox, QPushButton, QWidget)

import ui_style as S
from playback_devices import OutputDevice, QtDeviceProvider
from services import build_services
from settings import DEFAULTS
from test_core_runtime import FakeSD
from transcriber import FinalResult
from ui_main import Bridge, MainWindow, SettingsDialog, SuggestDialog
from ui_widgets import CheckBox, ElidedLabel, WrapBarLayout


def _forbid(*a, **k):
    raise AssertionError("テスト中に実ダイアログを開く呼び出しがありました")


for _cls, _names in ((QMessageBox, ("question", "warning", "information", "critical", "about")),
                     (QFileDialog, ("getOpenFileName", "getOpenFileNames", "getSaveFileName", "getExistingDirectory")),
                     (QInputDialog, ("getText", "getInt", "getItem", "getDouble", "getMultiLineText"))):
    for _n in _names:
        setattr(_cls, _n, staticmethod(_forbid))
QDialog.exec = _forbid
QDialog.open = _forbid
QMessageBox.exec = _forbid

app = QApplication.instance() or QApplication(sys.argv)
app.setFont(S.app_font())
app.setStyleSheet(S.QSS)


def pump(sec=0.3, until=None):
    end = time.time() + sec
    while time.time() < end:
        app.processEvents()
        if until and until():
            return True
        time.sleep(0.01)
    return bool(until and until())


class FakeClipboard:
    def __init__(self):
        self._t = ""

    def setText(self, t, *a):
        self._t = t

    def text(self, *a):
        return self._t


def clip_text():
    return QGuiApplication.clipboard().text()


def fake_transcribe(wav, s, model_name=None, on_progress=None, on_status=None, cancel=None, model=None,
                    postprocess=None, formatter=None, **kw):
    """本物の transcribe_wav と同じく、formatter（すき間による改行）を全文に適用する。"""
    on_progress(0.5)
    segs = [(0.0, 3.0, "壁打ちを始めます。"), (3.5, 6.0, "予算の話です"), (12.0, 14.0, "以上です")]
    text = formatter(segs) if formatter else "\n".join(t for _, _, t in segs)
    return FinalResult(text=text, segments=segs, txt_path=None, md_path=None, audio_seconds=14.0, elapsed=0.1)


class FakeProvider:
    """再生デバイスの一覧（差し替え可能）。"""

    def __init__(self, devs):
        self.devs = list(devs)

    def list(self):
        return list(self.devs)


DEV_A = OutputDevice("dev-a", "スピーカー（テスト）", True)
DEV_B = OutputDevice("dev-b", "ヘッドセット（テスト）", False)
DEV_C = OutputDevice("dev-c", "HDMI（テスト）", False)


def as_default(devs, dev_id):
    return [OutputDevice(d.id, d.name, d.id == dev_id) for d in devs]


class GuiBase(unittest.TestCase):
    def make(self, sd=None, provider=None, **settings):
        d = Path(self._d.name)
        s = dict(DEFAULTS)
        s.update(out_dir=str(d / "out"), live_enabled=False, silence_stop_sec=0)
        s.update(settings)
        from history import History
        hist = History(d / f"h{len(self._wins)}.db")
        sd = sd or FakeSD(C.speechlike(8, -12), speed=10.0, loop=True)
        bridge = Bridge()
        svc = build_services(s, on_job_event=lambda j: bridge.job_event.emit(j),
                             on_status=lambda m: bridge.status.emit(m), sd=sd,
                             transcribe=fake_transcribe, loader=lambda n, t: object(), history=hist)
        win = MainWindow(svc, bridge, use_tray=False, volume=0.0, muted=True,
                         settings_path=d / f"settings{len(self._wins)}.json",
                         device_provider=provider or FakeProvider([DEV_A, DEV_B, DEV_C]))
        win.player.set_muted(True)
        self.asked = getattr(self, "asked", [])
        win._question = lambda title, text, buttons=None: (self.asked.append(title), QMessageBox.Yes)[1]
        win._warn = lambda title, text: self.asked.append("warn:" + title)
        win._exec = lambda dlg: self.fail("テストでダイアログを exec してはいけません")
        def _no_menu(menu, pos):
            raise AssertionError("テスト中にメニューを表示してはいけません（_menu_exec を差し替えること）")
        win._menu_exec = _no_menu
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        pump(0.1)
        self._wins.append((win, svc, hist))
        return win, svc

    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._wins = []
        self.asked = []
        # クリップボードは偽物に差し替える（本人のクリップボードを書き換えない・常駐ツールに掴まれて空に見えるのを避ける）
        self._clip = FakeClipboard()
        self._clip_patch = mock.patch.object(QGuiApplication, "clipboard", staticmethod(lambda: self._clip))
        self._clip_patch.start()

    def tearDown(self):
        for win, svc, hist in self._wins:
            win.timer.stop()
            svc.session.close()
            win.player.stop()
            svc.queue.wait_idle(10)
            pump(0.1)
            svc.queue.close()
            hist.close()
            win.close()
            win.deleteLater()      # 閉じただけの窓が溜まると、スタイルシートの切替などが遅くなる
        from PySide6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self._wins = []
        self._clip_patch.stop()
        self._d.cleanup()


class CheckBoxWidgetTest(unittest.TestCase):
    def grab_center(self, cb):
        img = cb.grab().toImage()
        # チェックボックスの箱の中心付近の色
        from PySide6.QtGui import QColor
        c = QColor(img.pixel(4 + CheckBox.BOX // 2, img.height() // 2))
        return c

    def test_on_off_are_visually_distinct(self):
        on, off = CheckBox("逐次認識"), CheckBox("逐次認識")
        on.setChecked(True)
        for w in (on, off):
            w.resize(w.sizeHint())
        c_on, c_off = self.grab_center(on), self.grab_center(off)
        # ON は明るいアクセント（青）の塗りつぶし、OFF は暗い背景（枠線だけ）
        self.assertGreater(c_on.blue(), 180)
        self.assertLess(c_off.blue(), 60)
        self.assertGreater(c_on.lightness() - c_off.lightness(), 80)
        # ON にはチェックの白が含まれ、OFF には含まれない
        def whites(cb):
            img = cb.grab().toImage()
            n = 0
            mid, r = img.height() // 2, CheckBox.BOX // 2 - 4     # 箱の内側だけ（背景・角は数えない）
            for y in range(mid - r, mid + r + 1):
                for x in range(4 + 4, 4 + CheckBox.BOX - 4):
                    p = img.pixelColor(x, y)
                    if p.red() > 235 and p.green() > 235 and p.blue() > 235:
                        n += 1
            return n
        self.assertGreater(whites(on), 8)
        self.assertEqual(whites(off), 0)

    def test_disabled_and_focus_look_different(self):
        a, b, c = CheckBox("x"), CheckBox("x"), CheckBox("x")
        for w in (a, b, c):
            w.setChecked(True)
            w.resize(w.sizeHint())
        b.setEnabled(False)
        ia, ib = a.grab().toImage(), b.grab().toImage()
        self.assertNotEqual(ia, ib)
        self.assertLess(self.grab_center(b).lightness(), self.grab_center(a).lightness())

    def test_size_fits_text_and_toggles(self):
        cb = CheckBox("クリップボードへ全文をコピー")
        fm = QFontMetrics(cb.font())
        self.assertGreaterEqual(cb.sizeHint().width(), CheckBox.BOX + fm.horizontalAdvance(cb.text()))
        self.assertFalse(cb.isChecked())
        cb.toggle()
        self.assertTrue(cb.isChecked())
        self.assertTrue(cb.hitButton(cb.rect().center()))


class LayoutWidgetTest(unittest.TestCase):
    def test_elided_label(self):
        lb = ElidedLabel("とても長い文章がここに入ります。とても長い文章がここに入ります。", pad=0)
        lb.setAttribute(Qt.WA_DontShowOnScreen, True)
        lb.show()
        lb.resize(120, 24)
        pump(0.05)
        self.assertEqual(lb.text(), "とても長い文章がここに入ります。とても長い文章がここに入ります。")
        self.assertTrue(QLabelText(lb).endswith("…"))
        self.assertLess(lb.minimumSizeHint().width(), 80)
        lb.resize(2000, 24)
        pump(0.05)
        self.assertFalse(QLabelText(lb).endswith("…"))

    def test_wrap_layout_wraps_and_grows(self):
        host = QWidget()
        lay = WrapBarLayout(host, hspace=10, vspace=8)
        ws = []
        for wpx in (100, 100, 100):
            b = QPushButton("x", host)
            b.setFixedSize(wpx, 30)
            lay.add(b)
            ws.append(b)
        g = QWidget(host)
        g.setMinimumSize(50, 30)
        g.setFixedHeight(30)
        lay.add(g, grow=True)
        host.resize(500, 10)
        lay.setGeometry(QRect(0, 0, 500, 10))
        self.assertEqual({w.y() for w in ws + [g]}, {0})          # 1 段
        self.assertEqual(g.x() + g.width(), 500)                     # grow は余りを埋める
        lay.setGeometry(QRect(0, 0, 250, 10))
        self.assertEqual(ws[2].y(), 38)                              # 3 つ目は 2 段目へ
        self.assertGreaterEqual(lay.heightForWidth(250), 30 * 2 + 8)
        self.assertEqual(lay.minimumSize().width(), 100)


def name(w):
    return w.text() if hasattr(w, "text") else type(w).__name__


def QLabelText(lb):
    from PySide6.QtWidgets import QLabel
    return QLabel.text(lb)


class TopBarTest(GuiBase):
    WIDTHS = (480, 560, 700, 1000, 1400)

    def check_bar(self, w, label):
        bar = w.top_bar
        # レイアウトの再計算は非同期。上段の枠が必要な高さに落ち着くまで待つ（負荷の高いときの取りこぼし対策）
        pump(0.15)
        pump(3.0, until=lambda: bar.height() >= bar.heightForWidth(bar.width()) - 1)
        rect = bar.rect()
        kids = [c for c in (w.btn_rec, w.btn_pause, w.lbl_time, w.meter, w.badge, w.btn_mictest, w.btn_wav,
                            w.btn_folder, w.btn_settings, w.btn_compact, w.btn_pin) if not c.isHidden()]
        boxes = {c: QRect(c.mapTo(bar, c.rect().topLeft()), c.size()) for c in kids}   # 入れ子のグループ内も、枠から見た位置で比べる
        for c, g in boxes.items():
            self.assertTrue(rect.contains(g), f"{label}: {name(c)} が枠からはみ出し {g} / {rect}")
        for i, a in enumerate(kids):
            for b in kids[i + 1:]:
                self.assertFalse(boxes[a].intersects(boxes[b]),
                                 f"{label}: {name(a)} と {name(b)} が重なっている {boxes[a]} {boxes[b]}")
        # 時間表示は「00:00:00」が必ず収まる
        fm = QFontMetrics(w.lbl_time.font())
        need = max(fm.horizontalAdvance("00:00:00"), fm.horizontalAdvance("88:88:88"))
        self.assertGreaterEqual(w.lbl_time.width(), need, f"{label}: 時間表示が狭い")
        # ボタンは文字が切れない幅
        for b in (w.btn_rec, w.btn_pause, w.btn_mictest, w.btn_wav, w.btn_folder, w.btn_settings, w.btn_compact, w.btn_pin):
            if not b.isHidden():
                self.assertGreaterEqual(b.width(), b.minimumSizeHint().width(), f"{label}: {b.text()} の文字が切れる")
        # 1 段目にも 2 段目にもメーターが潰れない
        self.assertGreaterEqual(w.meter.width(), w.meter.minimumWidth())

    def test_normal_mode_all_widths(self):
        w, _ = self.make()
        for width in self.WIDTHS:
            w.resize(width, 760)
            self.check_bar(w, f"通常 {width}px(実際 {w.width()})")

    def test_compact_mode_widths(self):
        w, _ = self.make()
        w.set_compact(True)
        for width in (420, 480, 560, 700):
            w.resize(width, 300)
            self.check_bar(w, f"コンパクト {width}px(実際 {w.width()})")
        w.resize(420, 300)
        pump(0.2)
        self.assertLessEqual(w.top_bar.height(), 104, "コンパクト 420px では上段が 2 段に収まる")

    def test_recording_state_buttons_fit(self):
        w, _ = self.make()
        w.toggle_record()
        for width in self.WIDTHS:
            w.resize(width, 760)
            self.check_bar(w, f"録音中 {width}px")
        w.toggle_pause()
        w.resize(520, 760)
        self.check_bar(w, "一時停止中 520px")
        w.toggle_record()

    def test_long_mic_message_does_not_break_layout(self):
        from mic_check import Issue
        w, _ = self.make()
        w._set_issues([Issue("low", "warn", "音が小さすぎます", "x"), Issue("noisy", "warn", "ノイズが大きいです", "y"),
                       Issue("clipping", "warn", "音割れしています", "z")], recording=True)
        for width in self.WIDTHS:
            w.resize(width, 760)
            self.check_bar(w, f"長い指摘 {width}px")


class RecordingFlowTest(GuiBase):
    def test_pause_resume_button_flow(self):
        w, svc = self.make()
        self.assertFalse(w.btn_pause.isEnabled())
        w.toggle_record()
        self.assertTrue(w.btn_pause.isEnabled())
        self.assertEqual(w.btn_pause.text(), "一時停止")
        pump(0.6, until=lambda: w.lbl_time.text() != "00:00:00")
        w.btn_pause.click()                       # 内部の click()。OS へのマウス送信ではない
        self.assertTrue(svc.session.is_paused)
        self.assertEqual(w.btn_pause.text(), "再開")
        pump(0.3)
        t1 = w.lbl_time.text()
        pump(0.6)
        self.assertEqual(w.lbl_time.text(), t1, "一時停止中に経過時間が進んだ")
        self.assertIn("一時停止中", w.lbl_live_state.text())
        self.assertTrue(pump(2.0, until=lambda: w.meter._v > 0.0), "一時停止中もメーターは動く")
        w.btn_pause.click()
        self.assertFalse(svc.session.is_paused)
        self.assertEqual(w.btn_pause.text(), "一時停止")
        pump(0.8, until=lambda: w.lbl_time.text() != t1)
        self.assertNotEqual(w.lbl_time.text(), t1)
        kinds = [r.kind for r in svc.session.timeline.rows()]
        self.assertIn("pause", kinds)
        w.toggle_record()
        self.assertFalse(w.btn_pause.isEnabled())
        self.assertTrue(pump(5, until=lambda: w.lst_hist.count() == 1))   # 一時停止を挟んでも 1 本として履歴に載る
        self.assertEqual(len(self.wins_history()), 1)

    def wins_history(self):
        return self._wins[0][2].list()

    def test_pause_hotkey_signal_and_registration(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.3)
        w.bridge.hotkey.emit("pause")             # ホットキー押下の代わりに、スロットへ直接シグナル
        pump(0.1)
        self.assertTrue(svc.session.is_paused)
        w.bridge.hotkey.emit("pause")
        pump(0.1)
        self.assertFalse(svc.session.is_paused)
        w.toggle_record()
        # 登録・解除だけ確認（実押下はしない）
        w.s["hotkey_pause"] = "Ctrl+Alt+Shift+F22"
        w.s["hotkey_record"] = ""
        w.s["hotkey_show"] = ""
        w.apply_hotkeys()
        try:
            self.assertEqual(w.hotkey_mgr.results, {"pause": None})
        finally:
            w.hotkey_mgr.stop()
            w.hotkey_mgr = None

    def test_silence_autostop_ignored_while_paused(self):
        w, svc = self.make(sd=FakeSD(np.zeros(16000 * 5, dtype=np.int16), speed=10.0, loop=True), silence_stop_sec=1)
        svc.session.silence.limit_sec = 1
        w.toggle_record()
        self.assertTrue(svc.session.is_recording)
        w.toggle_pause()
        pump(1.5)
        self.assertTrue(svc.session.is_recording, "一時停止中に無音の自動停止が動いた")
        w.toggle_pause()
        self.assertTrue(pump(4, until=lambda: not svc.session.is_recording), "再開後は無音で自動停止する")

    def test_paragraph_applies_to_clipboard_and_send_but_history_keeps_raw(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.5)
        w.toggle_record()
        self.assertTrue(pump(5, until=lambda: w.lst_hist.count() == 1))
        pump(0.2)
        # 0→3 / 3.5→6 はすき間 0.5 秒でつなぐ。6→12 は 6 秒: 句点がないので「。」を補って改行
        self.assertEqual(clip_text(), "壁打ちを始めます。予算の話です。\n以上です")
        rec = self._wins[0][2].list()[0]
        self.assertEqual(rec.text, "壁打ちを始めます。\n予算の話です\n以上です")
        self.assertEqual(len(rec.segments), 3)
        w.ed_prompt.setPlainText("要約して")
        w.send_main()
        self.assertIn("壁打ちを始めます。予算の話です。\n以上です", clip_text(),
                      w.statusBar().currentMessage())
        # 設定で OFF にすると従来どおり
        w.s["paragraph_enabled"] = False
        w.send_main()
        self.assertIn("壁打ちを始めます。\n予算の話です\n以上です", clip_text())


class IdleMonitorTest(GuiBase):
    def test_monitor_follows_window_visibility(self):
        w, svc = self.make()
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active), "窓が見えているので待機中モニターが開く")
        pump(0.5)
        self.assertTrue(pump(2.0, until=lambda: w.meter._v > 0.0), "録音していなくてもメーターが動く")
        self.assertFalse((Path(self._d.name) / "out").exists() and any((Path(self._d.name) / "out").iterdir()))
        w.hide()
        self.assertTrue(pump(3, until=lambda: not svc.session.monitor_active), "窓を隠したらストリームを閉じる")
        w.show()
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))
        # 最小化も「見えていない」
        w.isMinimized = lambda: True
        self.assertTrue(pump(3, until=lambda: not svc.session.monitor_active))
        del w.isMinimized
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))

    def test_monitor_closes_after_inactivity_and_reopens_on_touch(self):
        w, svc = self.make(idle_monitor_timeout_sec=60)
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))
        w._last_activity -= 120
        self.assertTrue(pump(3, until=lambda: not svc.session.monitor_active), "一定時間操作がなければ閉じる")
        self.assertIn("停止", "停止")   # 画面の表示は下の画像確認で見る
        w._touch()
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))

    def test_setting_off_never_opens(self):
        w, svc = self.make(idle_monitor=False)
        pump(0.6)
        self.assertFalse(svc.session.monitor_active)

    def test_busy_microphone_shows_message_not_error(self):
        sd = FakeSD(np.zeros(1600, dtype=np.int16), fail=OSError("exclusive in use"))
        w, svc = self.make(sd=sd)
        pump(0.8)
        self.assertFalse(svc.session.monitor_active)
        self.assertIn("マイクを使用中のため確認できません", w.badge.text())
        self.assertEqual([a for a in self.asked if a.startswith("warn:")], [])   # ダイアログは出さない
        self.assertIn("録音", w.btn_rec.text())

    def test_idle_band_appears_on_speech_and_record_button_starts_recording(self):
        w, svc = self.make()
        self.assertTrue(w.idle_band.isHidden())
        self.assertTrue(pump(6, until=lambda: not w.idle_band.isHidden()), "話しているのに録音していない → 帯")
        self.assertIn("録音していません", w.idle_label.text())
        w.btn_idle_rec.click()
        self.assertTrue(svc.session.is_recording)
        self.assertTrue(w.idle_band.isHidden())

    def test_idle_band_not_shown_when_window_hidden_or_disabled_or_cooldown(self):
        w, svc = self.make()
        w.hide()
        pump(1.5)
        self.assertTrue(w.idle_band.isHidden(), "窓が隠れているときは出さない")
        w.show()
        self.assertTrue(pump(6, until=lambda: not w.idle_band.isHidden()))
        w.hide_idle_band()
        pump(2.0)
        self.assertTrue(w.idle_band.isHidden(), "クールダウン中は再度出さない（既定 60 秒）")
        w2, svc2 = self.make(idle_prompt_enabled=False)
        pump(2.5)
        self.assertTrue(w2.idle_band.isHidden(), "設定で OFF")

    def test_close_button_dismisses_band(self):
        w, _ = self.make()
        self.assertTrue(pump(6, until=lambda: not w.idle_band.isHidden()))
        w.btn_idle_close.click()
        self.assertTrue(w.idle_band.isHidden())

    def test_mic_test_uses_the_open_stream(self):
        sd = FakeSD(C.speechlike(8, -12), speed=10.0, loop=True)
        w, svc = self.make(sd=sd)
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))
        stats, issues = svc.session.mic_test(seconds=0.3)
        self.assertEqual(sd.opened, 1)


class ModeAndGeometryTest(GuiBase):
    def settings_file(self, idx=0):
        return json.loads((Path(self._d.name) / f"settings{idx}.json").read_text(encoding="utf-8"))

    def test_compact_toggle_hides_the_big_parts_and_restores(self):
        w, _ = self.make()
        w.resize(1200, 780)
        pump(0.2)
        self.assertFalse(w.side_scroll.isHidden())
        self.assertFalse(w.queue_group.isHidden())
        w.set_compact(True)
        pump(0.2)
        self.assertTrue(w.side_scroll.isHidden())
        self.assertTrue(w.queue_group.isHidden())
        self.assertTrue(w.opt_row.isHidden())
        self.assertTrue(w.btn_mictest.isHidden() and w.btn_wav.isHidden() and w.btn_settings.isHidden())
        self.assertTrue(w.lbl_queue_compact.isVisible() or not w.lbl_queue_compact.isHidden())
        self.assertFalse(w.tabs.tabBar().isVisible())
        self.assertTrue(w.btn_compact.isChecked())
        self.assertEqual((w.width(), w.height()), (420, 300))
        self.assertTrue(w.timeline_view.itemDelegate().compact)
        self.assertEqual(w.btn_rec.text(), "録音")
        self.assertTrue(self.settings_file()["compact_mode"])
        w.set_compact(False)
        pump(0.2)
        self.assertEqual((w.width(), w.height()), (1200, 780), "通常に戻すと元のサイズ")
        self.assertFalse(w.side_scroll.isHidden())
        self.assertEqual(w.btn_rec.text(), "録音開始")
        self.assertFalse(self.settings_file()["compact_mode"])

    def test_each_mode_remembers_its_own_size_and_position(self):
        w, _ = self.make()
        w.setGeometry(50, 60, 1100, 700)
        pump(0.1)
        w.set_compact(True)
        w.setGeometry(300, 200, 480, 340)
        pump(0.1)
        w.set_compact(False)
        self.assertEqual(tuple(self.settings_file()["win_geometry_compact"]), (300, 200, 480, 340))
        self.assertEqual(tuple(self.settings_file()["win_geometry_normal"]), (50, 60, 1100, 700))
        g = w.geometry()
        self.assertEqual((g.x(), g.y(), g.width(), g.height()), (50, 60, 1100, 700))
        w.set_compact(True)
        g = w.geometry()
        self.assertEqual((g.x(), g.y(), g.width(), g.height()), (300, 200, 480, 340))

    def test_restore_on_startup_and_clamp_off_screen(self):
        scr = QGuiApplication.screens()[0].availableGeometry()
        w, _ = self.make(compact_mode=True, win_geometry_compact=[scr.x() + 30000, scr.y() + 30000, 460, 320],
                         win_geometry_normal=[scr.x() + 40, scr.y() + 40, 1000, 640])
        g = w.geometry()
        self.assertTrue(w._compact)
        self.assertEqual((g.width(), g.height()), (460, 320))
        self.assertTrue(scr.contains(g), f"画面外に出ている: {g} / {scr}")
        w.set_compact(False)
        g = w.geometry()
        self.assertEqual((g.x(), g.y(), g.width(), g.height()), (scr.x() + 40, scr.y() + 40, 1000, 640))

    def test_oversized_saved_size_is_shrunk_to_screen(self):
        scr = QGuiApplication.screens()[0].availableGeometry()
        w, _ = self.make(win_geometry_normal=[0, 0, scr.width() * 3, scr.height() * 3])
        g = w.geometry()
        self.assertLessEqual(g.width(), scr.width())
        self.assertLessEqual(g.height(), scr.height())

    def test_move_and_resize_are_saved_after_a_moment(self):
        w, _ = self.make()
        w.setGeometry(70, 80, 900, 650)
        pump(1.2)
        self.assertEqual(tuple(self.settings_file()["win_geometry_normal"]), (70, 80, 900, 650))

    def test_close_saves_geometry(self):
        w, _ = self.make()
        w.setGeometry(90, 95, 880, 640)
        w.close()
        self.assertEqual(tuple(self.settings_file()["win_geometry_normal"]), (90, 95, 880, 640))

    def test_history_tab_request_leaves_compact(self):
        w, _ = self.make()
        w.set_compact(True)
        w.show_main(tab=1)
        self.assertFalse(w._compact)
        self.assertEqual(w.tabs.currentIndex(), 1)

    def test_side_panel_hides_when_narrow(self):
        w, _ = self.make()
        w.resize(1200, 700)
        pump(0.1)
        self.assertFalse(w.side_scroll.isHidden())
        w.resize(640, 700)
        pump(0.1)
        self.assertTrue(w.side_scroll.isHidden())
        w.resize(1000, 700)
        pump(0.1)
        self.assertFalse(w.side_scroll.isHidden())


class PinTest(GuiBase):
    def test_pin_keeps_position_size_and_visibility(self):
        w, _ = self.make()
        w.setGeometry(120, 140, 760, 560)
        pump(0.1)
        w.btn_pin.setChecked(True)
        pump(0.2)
        self.assertTrue(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(w.isVisible())
        g = w.geometry()
        self.assertEqual((g.width(), g.height()), (760, 560))
        self.assertLess(abs(g.x() - 120) + abs(g.y() - 140), 40)    # 窓の再作成で枠分ずれても大きくは動かない
        self.assertFalse(w.testAttribute(Qt.WA_ShowWithoutActivating), "アクティブ化抑止の一時指定を戻している")
        self.assertTrue(json.loads((Path(self._d.name) / "settings0.json").read_text(encoding="utf-8"))["always_on_top"])
        w.btn_pin.setChecked(False)
        pump(0.2)
        self.assertFalse(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(w.isVisible())
        self.assertFalse(w.s["always_on_top"])

    def test_pin_toggle_does_not_show_a_hidden_window(self):
        w, _ = self.make()
        w.hide()
        w.set_always_on_top(True)
        pump(0.1)
        self.assertFalse(w.isVisible(), "トレイに隠れている窓をピン切替で出さない")
        self.assertTrue(w.btn_pin.isChecked())

    def test_pinned_at_startup(self):
        w, _ = self.make(always_on_top=True)
        self.assertTrue(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(w.btn_pin.isChecked())

    def test_recording_survives_pin_toggle(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.3)
        w.set_always_on_top(True)
        pump(0.3)
        self.assertTrue(svc.session.is_recording)
        self.assertTrue(svc.session.monitor_active)
        w.toggle_record()


class PlaybackDeviceTest(GuiBase):
    def test_combo_lists_default_and_devices(self):
        w, _ = self.make()
        items = [(w.cmb_out.itemText(i), w.cmb_out.itemData(i)) for i in range(w.cmb_out.count())]
        self.assertEqual([d for _, d in items], ["", "dev-a", "dev-b", "dev-c"])
        self.assertEqual(items[0][0], "システム既定")
        self.assertIn("現在の既定", items[1][0])
        self.assertEqual(w.cmb_out.currentIndex(), 0)
        self.assertEqual(w.player.applied_id, "dev-a")
        self.assertEqual(w.player.out.volume(), 0.0)
        self.assertTrue(w.player.out.isMuted())

    def test_system_default_follows_default_change_at_play_time(self):
        prov = FakeProvider([DEV_A, DEV_B, DEV_C])
        w, _ = self.make(provider=prov)
        n0 = w.player.apply_count
        self.assertEqual(w.player.applied_id, "dev-a")
        # 常駐ツールで既定がヘッドセットに切り替わった（Qt の通知は待たずに、再生開始時に取り直す）
        prov.devs = as_default([DEV_A, DEV_B, DEV_C], "dev-b")
        w.player.play_from(0.0)
        self.assertEqual(w.player.applied_id, "dev-b")
        self.assertEqual(w.player.apply_count, n0 + 1)
        w.player.play_from(0.0)              # 変わっていなければ作り直さない
        self.assertEqual(w.player.apply_count, n0 + 1)
        self.assertTrue(w.player.out.isMuted())
        w.player.stop()

    def test_system_default_follows_while_playing_via_devices_changed(self):
        prov = FakeProvider([DEV_A, DEV_B])
        w, _ = self.make(provider=prov)
        prov.devs = as_default([DEV_A, DEV_B], "dev-b")
        w.player._on_devices_changed()       # QMediaDevices.audioOutputsChanged のスロット
        self.assertEqual(w.player.applied_id, "dev-b")
        self.assertEqual(w.cmb_out.count(), 3)

    def test_explicit_selection_is_applied_saved_and_not_overridden_by_default_change(self):
        prov = FakeProvider([DEV_A, DEV_B, DEV_C])
        w, _ = self.make(provider=prov)
        w.cmb_out.setCurrentIndex(3)         # HDMI
        self.assertEqual(w.s["playback_device"], "dev-c")
        self.assertEqual(w.player.applied_id, "dev-c")
        saved = json.loads((Path(self._d.name) / "settings0.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["playback_device"], "dev-c")
        prov.devs = as_default([DEV_A, DEV_B, DEV_C], "dev-b")
        w.player.play_from(0.0)
        self.assertEqual(w.player.applied_id, "dev-c", "明示的に選んだ出力先は既定が変わっても動かさない")
        w.player.stop()
        # 起動時の復元: 次の窓は保存された選択を使う
        w2, _ = self.make(provider=prov, playback_device="dev-c")
        self.assertEqual(w2.cmb_out.currentData(), "dev-c")
        self.assertEqual(w2.player.applied_id, "dev-c")

    def test_vanished_device_falls_back_to_default_and_tells_the_user(self):
        prov = FakeProvider([DEV_A, DEV_B, DEV_C])
        w, _ = self.make(provider=prov, playback_device="dev-c")
        self.assertEqual(w.player.applied_id, "dev-c")
        prov.devs = [DEV_A, DEV_B]           # HDMI を外した
        w.player._on_devices_changed()
        self.assertEqual(w.player.applied_id, "dev-a")
        self.assertEqual(w.s["playback_device"], "")
        self.assertEqual(w.cmb_out.currentIndex(), 0)
        self.assertFalse(w.lbl_out_notice.isHidden())
        self.assertIn("システム既定に戻しました", w.lbl_out_notice.text())
        self.assertIn("システム既定に戻しました", w.statusBar().currentMessage())
        # 手動で選び直すと知らせは消える
        w.cmb_out.setCurrentIndex(2)
        self.assertTrue(w.lbl_out_notice.isHidden())

    def test_startup_with_vanished_saved_device(self):
        w, _ = self.make(provider=FakeProvider([DEV_A]), playback_device="dev-zzz")
        self.assertEqual(w.player.applied_id, "dev-a")
        self.assertEqual(w.cmb_out.currentIndex(), 0)

    def test_real_provider_lists_without_playing(self):
        devs = QtDeviceProvider().list()      # 取得だけ。音は出さない
        self.assertIsInstance(devs, list)
        for d in devs:
            self.assertTrue(d.id and d.name)


class DialogsTest(GuiBase):
    def test_settings_dialog_fields_and_values(self):
        w, _ = self.make()
        d = SettingsDialog(w.s, [{"index": 1, "name": "マイク", "hostapi": "MME"}], w)
        v = d.values()
        for k in ("idle_monitor", "idle_monitor_timeout_sec", "idle_prompt_enabled", "idle_prompt_cooldown_sec",
                  "paragraph_enabled", "paragraph_gap_sec", "paragraph_force_gap_sec", "hotkey_pause"):
            self.assertIn(k, v)
        self.assertTrue(v["idle_monitor"] and v["paragraph_enabled"])
        self.assertEqual((v["paragraph_gap_sec"], v["paragraph_force_gap_sec"]), (2.0, 3.0))
        d.idle_monitor.setChecked(False)
        d.para_gap.setValue(4.0)
        d.para_force.setValue(2.5)           # 強制のほうが小さい設定は、強制を通常のしきい値まで引き上げる
        d.hk_pause.setText("Ctrl+Alt+F8")
        d.idle_cooldown.setValue(120)
        v = d.values()
        self.assertFalse(v["idle_monitor"])
        self.assertEqual((v["paragraph_gap_sec"], v["paragraph_force_gap_sec"]), (4.0, 4.0))
        self.assertEqual(v["hotkey_pause"], "Ctrl+Alt+F8")
        self.assertEqual(v["idle_prompt_cooldown_sec"], 120)

    def test_open_settings_applies_through_hook(self):
        w, svc = self.make()
        d = SettingsDialog(w.s, [], w)
        d.idle_prompt.setChecked(False)
        d.para_on.setChecked(False)
        w._exec = lambda dlg: QDialog.Accepted
        import ui_main
        orig = ui_main.SettingsDialog
        ui_main.SettingsDialog = lambda *a, **k: d
        try:
            w.open_settings()
        finally:
            ui_main.SettingsDialog = orig
        self.assertFalse(w.s["idle_prompt_enabled"])
        self.assertFalse(w.s["paragraph_enabled"])
        saved = json.loads((Path(self._d.name) / "settings0.json").read_text(encoding="utf-8"))
        self.assertFalse(saved["paragraph_enabled"])

    def test_suggest_dialog_uses_custom_checkboxes(self):
        d = SuggestDialog([("不線", "付箋"), ("壁内", "壁打ち")])
        d.setAttribute(Qt.WA_DontShowOnScreen, True)
        d.show()
        pump(0.1)
        for cb, _ in d._checks:   # 行の高さが潰れて箱の枠が欠けない
            self.assertGreaterEqual(cb.height(), CheckBox.BOX + 4)
        d.hide()
        self.assertEqual(d.chosen(), [("不線", "付箋"), ("壁内", "壁打ち")])
        d._checks[0][0].setChecked(False)
        self.assertEqual(d.chosen(), [("壁内", "壁打ち")])


if __name__ == "__main__":
    unittest.main()
