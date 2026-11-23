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
from PySide6.QtGui import QGuiApplication
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
    segs = [(0.0, 3.0, "壁打ちを始めます。"), (3.5, 6.0, "基地局の話です"), (12.0, 14.0, "以上です")]
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


def QLabelText(lb):
    from PySide6.QtWidgets import QLabel
    return QLabel.text(lb)


class RecordingFlowTest(GuiBase):
    def test_hold_resume_button_flow(self):
        """一時停止のボタンは［保留］（取り込みを止める）。録音（マイク）は続き、経過時間も進む。"""
        w, svc = self.make()
        u = w.unified
        u.refresh()
        self.assertFalse(u.btn_pause.isEnabled())
        w.toggle_record()                          # 録音ボタン＝セッション開始
        u.refresh()
        self.assertTrue(w._dialogue_active())
        self.assertTrue(u.btn_pause.isEnabled())
        self.assertFalse(u.btn_pause.isChecked())
        pump(0.6, until=lambda: u.lbl_time.text() not in ("--:--", "0:00"))
        u.btn_pause.click()                       # 内部の click()。OS へのマウス送信ではない
        self.assertTrue(w.dialogue_ctl.ds.is_held)
        self.assertFalse(svc.session.is_paused)    # マイクの録音は止めない
        pump(0.3)
        self.assertTrue(u.btn_pause.isChecked())
        self.assertIn("保留", u.btn_pause.toolTip())
        self.assertTrue(pump(2.0, until=lambda: w.meter._v > 0.0), "保留中もメーターは動く")
        u.btn_pause.click()
        self.assertFalse(w.dialogue_ctl.ds.is_held)
        self.assertTrue(svc.session.is_recording)
        w.toggle_record()                          # 停止＝セッション終了。録音全体を清書して履歴に載せる
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
        u.refresh()
        self.assertFalse(u.btn_pause.isEnabled())
        self.assertTrue(pump(8, until=lambda: w.lst_hist.count() == 1))   # 保留を挟んでも 1 本として履歴に載る
        self.assertEqual(len(self.wins_history()), 1)

    def wins_history(self):
        return self._wins[0][2].list()

    def test_pause_hotkey_toggles_hold_and_registration(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.3)
        w.bridge.hotkey.emit("pause")             # ホットキー押下の代わりに、スロットへ直接シグナル
        pump(0.1)
        self.assertTrue(w.dialogue_ctl.ds.is_held)
        w.bridge.hotkey.emit("pause")
        pump(0.1)
        self.assertFalse(w.dialogue_ctl.ds.is_held)
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
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

    def test_silence_ends_the_session_and_finalizes_the_recording(self):
        """無音が続くと、AI対話のセッション（dialogue.idle_stop_sec）が自動で終わり、録音全体の清書が履歴に載る。"""
        w, svc = self.make(sd=FakeSD(np.zeros(16000 * 5, dtype=np.int16), speed=10.0, loop=True),
                           dialogue={**DEFAULTS["dialogue"], "idle_stop_sec": 1})
        w.toggle_record()
        self.assertTrue(w._dialogue_active())
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active() and not svc.session.is_recording),
                        "無音が続いたのにセッションが終わらない")
        self.assertTrue(pump(8, until=lambda: w.lst_hist.count() == 1))

    def test_history_keeps_raw_text_and_nothing_goes_to_the_clipboard(self):
        w, svc = self.make()
        w.toggle_record()
        pump(0.5)
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: w.lst_hist.count() == 1))
        pump(0.2)
        self.assertEqual(clip_text(), "")                    # 清書が終わっても、クリップボードへは自動で入れない
        rec = self._wins[0][2].list()[0]
        self.assertEqual(rec.text, "壁打ちを始めます。\n基地局の話です\n以上です")   # 履歴には整形しない本文
        self.assertEqual(len(rec.segments), 3)
        for gone in ("btn_send", "btn_h_send", "send_main", "send_history", "chk_live", "lbl_queue_compact", "prog"):
            self.assertFalse(hasattr(w, gone), gone)         # 「コピーして送る」系の部品は無い


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
        import ui_main
        w, svc = self.make()
        with mock.patch.object(ui_main, "IDLE_MONITOR_TIMEOUT_SEC", 60):
            self._monitor_closes(w, svc)

    def _monitor_closes(self, w, svc):
        self.assertTrue(pump(3, until=lambda: svc.session.monitor_active))
        w._last_activity -= 120
        self.assertTrue(pump(3, until=lambda: not svc.session.monitor_active), "一定時間操作がなければ閉じる")
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
        w.unified.refresh()
        self.assertEqual(w.unified.chip.key, "busy")          # マイクを使用中のため確認できない（エラーにはしない）
        self.assertEqual([a for a in self.asked if a.startswith("warn:")], [])   # ダイアログは出さない



class GeometryTest(GuiBase):
    """位置とサイズの保存・復元（引き出しを閉じた状態のキー win_geometry_unified）。"""

    def settings_file(self, idx=0):
        return json.loads((Path(self._d.name) / f"settings{idx}.json").read_text(encoding="utf-8"))

    def test_restore_on_startup_and_clamp_off_screen(self):
        scr = QGuiApplication.screens()[0].availableGeometry()
        w, _ = self.make(win_geometry_unified=[scr.x() + 30000, scr.y() + 30000, 460, 320])
        g = w.geometry()
        self.assertTrue(scr.contains(g), f"画面外に出ている: {g} / {scr}")

    def test_oversized_saved_size_is_shrunk_to_screen(self):
        scr = QGuiApplication.screens()[0].availableGeometry()
        w, _ = self.make(win_geometry_unified=[0, 0, scr.width() * 3, scr.height() * 3])
        g = w.geometry()
        self.assertLessEqual(g.width(), scr.width())
        self.assertLessEqual(g.height(), scr.height())

    def test_move_and_resize_are_saved_after_a_moment(self):
        w, _ = self.make()
        w.setGeometry(70, 80, 900, 650)
        pump(1.2)
        self.assertEqual(tuple(self.settings_file()["win_geometry_unified"]), (70, 80, 900, 650))

    def test_close_saves_geometry(self):
        w, _ = self.make()
        w.setGeometry(90, 95, 880, 640)
        w.close()
        self.assertEqual(tuple(self.settings_file()["win_geometry_unified"]), (90, 95, 880, 640))

    def test_history_tab_request_opens_the_drawer(self):
        from ui_drawer import TAB_HISTORY
        w, _ = self.make()
        w.show_main(tab=1)
        self.assertTrue(w.unified.drawer_open)
        self.assertEqual(w.unified.drawer.current_tab(), TAB_HISTORY)


class PinTest(GuiBase):
    def test_pin_keeps_position_size_and_visibility(self):
        w, _ = self.make()
        w.setGeometry(120, 140, 760, 560)
        pump(0.1)
        w.unified.btn_pin.setChecked(True)
        pump(0.2)
        self.assertTrue(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(w.isVisible())
        g = w.geometry()
        self.assertEqual((g.width(), g.height()), (760, 560))
        self.assertLess(abs(g.x() - 120) + abs(g.y() - 140), 40)    # 窓の再作成で枠分ずれても大きくは動かない
        self.assertFalse(w.testAttribute(Qt.WA_ShowWithoutActivating), "アクティブ化抑止の一時指定を戻している")
        self.assertTrue(json.loads((Path(self._d.name) / "settings0.json").read_text(encoding="utf-8"))["always_on_top"])
        w.unified.btn_pin.setChecked(False)
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
        self.assertTrue(w._pinned)

    def test_pinned_at_startup(self):
        w, _ = self.make(always_on_top=True)
        self.assertTrue(bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
        self.assertTrue(w.unified.btn_pin.isChecked())

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
        for k in ("idle_monitor", "paragraph_enabled", "paragraph_gap_sec", "paragraph_force_gap_sec", "hotkey_pause"):
            self.assertIn(k, v)
        for k in ("idle_monitor_timeout_sec", "idle_prompt_enabled", "idle_prompt_cooldown_sec", "ui_layout", "dialogue_enabled"):
            self.assertNotIn(k, v, "画面から消した項目は保存しない（固定値）")
        self.assertTrue(v["idle_monitor"] and v["paragraph_enabled"])
        self.assertEqual((v["paragraph_gap_sec"], v["paragraph_force_gap_sec"]), (2.0, 3.0))
        d.idle_monitor.setChecked(False)
        d.para_gap.setValue(4.0)
        d.para_force.setValue(2.5)           # 強制のほうが小さい設定は、強制を通常のしきい値まで引き上げる
        d.hk_pause.setText("Ctrl+Alt+F8")
        v = d.values()
        self.assertFalse(v["idle_monitor"])
        self.assertEqual((v["paragraph_gap_sec"], v["paragraph_force_gap_sec"]), (4.0, 4.0))
        self.assertEqual(v["hotkey_pause"], "Ctrl+Alt+F8")

    def test_open_settings_applies_through_hook(self):
        w, svc = self.make()
        d = SettingsDialog(w.s, [], w)
        d.idle_monitor.setChecked(False)
        d.para_on.setChecked(False)
        w._exec = lambda dlg: QDialog.Accepted
        import ui_main
        orig = ui_main.SettingsDialog
        ui_main.SettingsDialog = lambda *a, **k: d
        try:
            w.open_settings()
        finally:
            ui_main.SettingsDialog = orig
        self.assertFalse(w.s["idle_monitor"])
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
