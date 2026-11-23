# -*- coding: utf-8 -*-
"""統合 GUI（S3 3行窓＋F3 引き出し）の GUI テスト。窓は画面に出さない(WA_DontShowOnScreen)・キー/マウスの実送信なし・
音なし・マイクなし（FakeSD）・ダイアログとメニューは開かない。このツールはクリップボードへ書かない（書いたら気づけるよう、偽物に差し替える）。
ワーカー・ヘッドセット・実モデルは使わない。画面の大きさ・見た目のテストは書かない（動きと文言だけ確かめる）。
AI対話の配達まわり（自動配達しない・宛先なし（記録だけ）・Copilot の前置き）は、フェイクの WorkerClient を使う Rig で確かめる。
"""
import os
import sys
import time
import unittest

os.environ["QT_SCALE_FACTOR"] = os.environ.get("KABEUCHI_TEST_SCALE", "1")

import _common as C  # noqa: F401
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QGuiApplication, QTextCursor
from PySide6.QtWidgets import QApplication, QMenu

import ui_dialogue_logic as L
import ui_style as S
import ui_unified_logic as U
from mic_check import Issue
from session import MIC_BUSY_MESSAGE
from settings import DEFAULTS
from test_dialogue_controller import PROFILE, Rig
from test_dialogue_session import wait_until
from test_gui_v15 import GuiBase, pump
from ui_dialogue import DialoguePanel
from ui_drawer import DRAWER_WIDTH, TAB_HISTORY, TAB_PREFIX, TAB_QUEUE, TAB_TERMS
from ui_main import SettingsDialog
from ui_mic_chip import MicChip
from ui_unified import NONE_KEY, PICK_KEY, UnifiedPanel

app = QApplication.instance()


def _forbid(*a, **k):
    raise AssertionError("テスト中に実メニューを表示しようとした")


QMenu.exec = _forbid
QMenu.popup = _forbid


def dlg_settings(**kw):
    d = {**DEFAULTS["dialogue"]}
    d.update(kw)
    return d


class UBase(GuiBase):
    """統合 GUI の MainWindow。"""

    def mk(self, dialogue=True, **kw):
        kw.setdefault("dialogue", dialogue if isinstance(dialogue, dict) else dlg_settings())
        kw.setdefault("idle_monitor", False)
        w, svc = self.make(**kw)
        if w.dialogue_panel is not None:
            w.dialogue_panel._confirm = lambda title, text: True
        return w, svc

    def tearDown(self):
        for win, _svc, _h in self._wins:
            try:
                win._shutdown_dialogue()
            except Exception:
                pass
        super().tearDown()


class LayoutTest(UBase):
    def test_window_is_the_unified_view_and_old_settings_are_ignored(self):
        self.assertNotIn("ui_layout", DEFAULTS)                    # 従来の画面は廃止
        self.assertNotIn("enabled", DEFAULTS["dialogue"])          # AI対話モードは常に有効
        w, _ = self.mk(ui_layout="classic", compact_mode=True, win_geometry_normal=[1, 2, 3, 4],
                       dialogue={**DEFAULTS["dialogue"], "enabled": False})      # 古い設定ファイルに残っていても無視する
        self.assertIsNotNone(w.unified)
        self.assertIsInstance(w.dialogue_panel, UnifiedPanel)
        self.assertIs(w.centralWidget(), w.unified)
        self.assertNotIn("unified_mode", DEFAULTS)                 # 壁打ちモードは廃止（モード切替は無い）
        for gone in ("btn_wall", "btn_dlg", "set_mode", "mode", "lbl_queue", "busy_for_mode_switch"):
            self.assertFalse(hasattr(w.unified, gone), gone)
        for gone in ("set_mode", "mode", "wall_commit", "_sync_wall", "btn_copy", "_set_clipboard"):
            self.assertFalse(hasattr(w.dialogue_panel, gone), gone)

    def test_settings_dialog_has_no_layout_choice(self):
        d = SettingsDialog(dict(DEFAULTS), [])
        self.assertFalse(hasattr(d, "layout_combo"))
        self.assertNotIn("ui_layout", d.values())

    def test_unified_geometry_keys_follow_the_drawer(self):
        w, _ = self.mk()
        self.assertEqual(w._geo_key(), "win_geometry_unified")
        w.unified.set_drawer(True)
        self.assertEqual(w._geo_key(), "win_geometry_unified_drawer")
        w.unified.set_drawer(False)
        for k in ("win_geometry_normal", "win_geometry_compact", "win_geometry_dialogue"):
            self.assertNotIn(k, DEFAULTS)

    def test_parts_for_the_drawer_exist_without_the_old_screen(self):
        w, _ = self.mk()
        for name in ("tabs", "timeline_view", "btn_rec", "btn_pause", "idle_band", "banner", "badge", "split"):
            self.assertFalse(hasattr(w, name), name)               # 従来の画面の部品は作らない
        self.assertTrue(w.statusBar().isHidden())


class MicChipTest(UBase):
    def test_chip_follows_host_state(self):
        w, svc = self.mk(unified_mode="wall")                # 古い設定キーが残っていても無視される
        w.timer.stop()
        u = w.unified
        u.refresh()
        self.assertEqual(u.chip.key, "wait")                  # 入力を開いていない（待機中モニターなし）
        w._issues = [Issue("low", "warn", "音が小さすぎます", "近づく")]
        w.toggle_record()                                     # 録音ボタン＝セッション開始
        pump(0.3)
        u.refresh()
        self.assertEqual(u.chip.key, "low")
        w._issues = []
        u.refresh()
        self.assertEqual(u.chip.key, "ok")
        w._issues = [Issue("clipping", "warn", "割れ", "離れる")]
        u.refresh()
        self.assertEqual(u.chip.key, "clip")
        w._issues = []
        w.toggle_record()                                     # 停止
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
        pump(0.1)
        w.meter.set_unrecorded(True)                          # 録音していないのに話している
        u.refresh()
        self.assertEqual(u.chip.key, "idle")
        w.meter.set_unrecorded(False)
        svc.session.monitor_error = MIC_BUSY_MESSAGE
        u.refresh()
        self.assertEqual(u.chip.key, "busy")
        svc.session.monitor_error = ""

    def test_click_shows_details_and_toggles(self):
        w, _ = self.mk()
        u = w.unified
        u.chip.set_state("noise")
        self.assertTrue(u.popup.isHidden())
        u.chip.click()
        self.assertFalse(u.popup.isHidden())
        self.assertIn("ノイズ大（要相談）", u.popup.text_lines()[0])
        self.assertLessEqual(u.popup.geometry().right(), u.width())
        u.chip.click()
        self.assertTrue(u.popup.isHidden())
        u.chip.click()
        u.popup.btn_close.click()
        self.assertTrue(u.popup.isHidden())

    def test_popup_opens_settings_only_by_button(self):
        w, _ = self.mk()
        called = []
        w.open_settings = lambda: called.append(1)
        w.unified.popup.openSettings.disconnect()
        w.unified.popup.openSettings.connect(lambda: w.open_settings())
        w.unified.chip.click()
        self.assertEqual(called, [])
        w.unified.popup.btn_settings.click()
        self.assertEqual(called, [1])


class SingleFlowTest(UBase):
    """壁打ちモードを廃止して、画面は AI対話の流れ1つ。録音ボタン1つ＝セッション開始／停止。"""

    def test_one_flow_with_rows_visible(self):
        w, _ = self.mk()
        u, p = w.unified, w.dialogue_panel
        self.assertFalse(p.row_dest.isHidden())               # 宛先・送信までするは常にある
        self.assertEqual(p.btn_commit.text(), "確定して届ける")
        self.assertFalse(hasattr(p, "btn_session"))           # 「セッション開始」は廃止
        self.assertFalse(hasattr(p, "btn_hold"))
        self.assertEqual(u.btn_rec.text(), "録音")

    def test_old_unified_mode_setting_is_ignored(self):
        w, _ = self.mk(unified_mode="wall")
        self.assertEqual(w.dialogue_panel.btn_commit.text(), "確定して届ける")
        self.assertFalse(w.dialogue_panel.row_dest.isHidden())

    def test_session_by_the_record_button_and_busy_state(self):
        w, svc = self.mk()
        u = w.unified
        w.toggle_record()                                     # 録音ボタン＝セッション開始
        self.assertTrue(w._dialogue_active())
        self.assertTrue(svc.session.is_recording)
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "停止")
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
        pump(0.2)
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "録音")

    def test_tray_has_no_mode_entry(self):
        w, _ = self.mk()
        self.assertFalse(hasattr(w, "act_dialogue"))
        self.assertFalse(hasattr(w, "open_dialogue"))


class PauseHoldTest(UBase):
    def test_pause_button_is_hold(self):
        w, svc = self.mk()
        u = w.unified
        u.refresh()
        self.assertFalse(u.btn_pause.isEnabled())
        w.toggle_record()
        self.assertTrue(w._dialogue_active())
        u.refresh()
        self.assertTrue(u.btn_pause.isEnabled())
        u.btn_pause.click()                                   # 保留
        self.assertTrue(w.dialogue_ctl.ds.is_held)
        self.assertFalse(svc.session.is_paused)               # マイクの録音は止めない
        u.refresh()
        self.assertTrue(u.btn_pause.isChecked())
        self.assertIn("保留", u.btn_pause.toolTip())
        u.btn_pause.click()
        self.assertFalse(w.dialogue_ctl.ds.is_held)
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))


class SessionFinalizeTest(UBase):
    """セッション終了時に、録音全体を清書して履歴に保存する（設定「録音全体の清書モデル」・ON/OFF は設定）。"""

    def record_and_stop(self, w, seconds=1.4):
        w.toggle_record()
        self.assertTrue(w._dialogue_active())
        pump(seconds)
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))

    def test_stop_finalizes_the_whole_recording_into_history(self):
        w, svc = self.mk()
        self.assertTrue(DEFAULTS["finalize_on_end"])
        self.record_and_stop(w)
        self.assertTrue(pump(8, until=lambda: w.lst_hist.count() == 1))
        rec = svc.history.list()[0]
        self.assertEqual(rec.model, w.s.get("final_model", "small"))      # 録音全体の清書モデル
        self.assertIn("基地局", rec.text)
        self.assertEqual(self._clip.text(), "")                           # クリップボードへは何も入れない

    def test_off_saves_per_utterance_result_without_finalize(self):
        w, svc = self.mk(finalize_on_end=False)
        self.record_and_stop(w)
        pump(1.5)
        self.assertFalse([j for j in svc.queue.jobs if not j.compress_only])      # 録音全体の清書は積まない
        recs = svc.history.list()
        if recs:                                                                  # 発話の結果があれば、1件だけ（発話ごと）
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0].model.endswith("（発話ごと）"))

    def test_too_short_recording_is_skipped_with_a_notice(self):
        w, svc = self.mk()
        w.toggle_record()
        w.toggle_record()                                                 # ほぼ 0 秒
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
        pump(0.3)
        self.assertEqual(svc.queue.jobs, [])
        self.assertIn("録音全体の清書は見送りました", w.dialogue_panel.notice_text())

    def test_progress_text_in_the_notice_line(self):
        from types import SimpleNamespace
        w, svc = self.mk()
        w.timer.stop()
        p = w.dialogue_panel
        self.assertEqual(w.progress_text(), "")
        self.assertEqual(p.notice_text(), "")                             # 何も無いときは出さない
        cur = SimpleNamespace(id=1, progress=0.35)
        w._job_t0[1] = time.monotonic() - 100.0
        svc.queue._current = cur
        try:
            self.assertEqual(w.progress_text(), "清書中 35%（あと約4分）")
            p.refresh()
            self.assertEqual(p.lbl_notice.text(), "")                           # 通知の行には出さない（状態バッジに出る）
            svc.queue._pending.append(object())
            self.assertEqual(w.progress_text(), "清書中 35%（あと約4分）（待機 1 件）")
            svc.queue._pending.clear()
            p.set_notice("別の通知です", "ok")
            self.assertEqual(p.notice_text(), "別の通知です")
        finally:
            svc.queue._current = None
            svc.queue._pending.clear()
        p._notice.clear()
        p.refresh()
        self.assertEqual(p.lbl_notice.text(), "")

    def test_no_queue_label_remains(self):
        w, _ = self.mk()
        for gone in ("lbl_queue", "lbl_queue_compact", "prog"):
            self.assertFalse(hasattr(w, gone), gone)
        self.assertEqual(w.queue_group.title(), "清書の状況")             # 「清書キュー」という表示はやめた


class DrawerTest(UBase):
    def test_open_close_changes_width_and_remembers_each_state(self):
        w, _ = self.mk()
        u = w.unified
        self.assertTrue(u.drawer.isHidden())
        w0 = w.width()
        u.btn_drawer.click()
        pump(0.1)
        self.assertTrue(u.drawer_open)
        self.assertFalse(u.drawer.isHidden())
        self.assertGreaterEqual(w.width(), w0 + DRAWER_WIDTH - 2)
        self.assertTrue(w.s["unified_drawer_open"])
        self.assertEqual(w._geo_key(), "win_geometry_unified_drawer")
        w.resize(w.width() + 40, w.height())
        u.btn_drawer.click()
        pump(0.1)
        self.assertTrue(u.drawer.isHidden())
        self.assertLessEqual(w.width(), w0 + 2)
        self.assertFalse(w.s["unified_drawer_open"])
        self.assertTrue(w.s["win_geometry_unified"])          # 閉じた状態の位置・サイズ
        self.assertTrue(w.s["win_geometry_unified_drawer"])   # 開いた状態の位置・サイズ
        u.btn_drawer.click()                                  # もう一度開くと、前の大きさに戻る
        pump(0.1)
        self.assertEqual(w.width(), list(w.s["win_geometry_unified_drawer"])[2])

    def test_draft_expands_while_the_drawer_is_open(self):
        w, _ = self.mk()
        p = w.dialogue_panel
        # 下書き欄は引き出しの開閉に関わらず窓の高さに合わせて伸びる（最小は直近3行ぶん）
        self.assertGreater(p.txt_draft.maximumHeight(), 5000)
        h_min = p.txt_draft.minimumHeight()
        w.unified.set_drawer(True)
        self.assertGreater(p.txt_draft.maximumHeight(), 5000)
        self.assertGreater(p.txt_draft.minimumHeight(), h_min)       # 引き出しを開いている間は少し大きめ
        w.unified.set_drawer(False)
        self.assertEqual(p.txt_draft.minimumHeight(), h_min)

    def test_four_tabs_and_features_moved_not_dropped(self):
        w, _ = self.mk()
        d = w.unified.drawer
        self.assertEqual([d.tabs.tabText(i) for i in range(d.tabs.count())], ["履歴", "用語・置換", "前置き", "清書の状況"])
        for name in ("ed_search", "lst_hist", "editor", "play_row", "btn_h_save", "btn_h_revert", "btn_h_dict",
                     "btn_h_redo", "btn_h_del", "btn_play", "slider", "queue_group",
                     "chips", "ed_hot", "tbl", "btn_dict_add", "btn_dict_del", "cmb_preset", "ed_prompt",
                     "btn_p_save", "btn_p_new", "btn_p_del"):
            self.assertTrue(d.isAncestorOf(getattr(w, name)), name)
        self.assertFalse(d.isAncestorOf(w.dialogue_panel))
        for gone in ("btn_send", "btn_h_send", "chk_live", "btn_mictest", "btn_wav", "btn_folder", "start_mic_test",
                     "show_mic_test_result", "_set_dialogue_opts"):    # 「コピーして送る」・逐次認識・マイクテストは廃止
            self.assertFalse(hasattr(w, gone), gone)
        for gone in ("chk_headset", "chk_nl", "refresh_settings", "btn_all"):     # 引き出しに設定タブは無い（設定は ⚙ の設定画面だけ）
            self.assertFalse(hasattr(d, gone), gone)
        self.assertTrue(d.isAncestorOf(w.queue_group))
        self.assertEqual(d.tabs.indexOf(d.tabs.widget(TAB_QUEUE)), TAB_QUEUE)
        for gone in ("btn_copy", "btn_p_copy", "btn_p_send", "btn_send"):    # 前置きのタブはクリップボードへ入れる部品を持たない
            self.assertFalse(hasattr(w, gone), gone)
        for gone in ("btn_send", "btn_h_send", "chk_live"):
            self.assertFalse(hasattr(w, gone), gone)

    def test_history_search_select_and_delete_still_work(self):
        w, svc = self.mk()
        svc.history.add(text="基地局の話をしました", segments=[(0, 5, "基地局の話をしました")], duration=60, model="small",
                        created_at="2026-11-20T09:00:00", wav_path="")
        svc.history.add(text="夕飯の相談", segments=[(0, 5, "夕飯の相談")], duration=30, model="small",
                        created_at="2026-11-21T09:00:00", wav_path="")
        w.reload_history()
        self.assertEqual(w.lst_hist.count(), 2)
        w.ed_search.setText("基地局")
        pump(0.6)
        self.assertEqual(w.lst_hist.count(), 1)
        w.lst_hist.setCurrentRow(0)
        self.assertIn("基地局", w.editor.toPlainText())
        self.assertTrue(w.btn_h_del.isEnabled())
        w.btn_h_del.click()                                   # 確認は「はい」に差し替え済み
        pump(0.1)
        self.assertEqual(svc.history.list().__len__(), 1)

    def test_terms_dictionary_and_presets_still_work(self):
        w, _ = self.mk()
        w.ed_hot.setText("一次解析")
        w.btn_hot_add.click()
        self.assertIn("一次解析", w.s["hotwords"])
        w.btn_dict_add.click()
        w.tbl.item(w.tbl.rowCount() - 1, 0).setText("えーあい")
        w.tbl.item(w.tbl.rowCount() - 1, 1).setText("AI")
        self.assertIn({"from": "えーあい", "to": "AI"}, w.dict_pairs())
        self.assertEqual(w.svc.replacer.apply("えーあい"), "AI")
        w.ed_pname.setText("テスト前置き")
        w.ed_prompt.setPlainText("次を要約してください。")
        w.btn_p_new.click()
        self.assertIn("テスト前置き", [p["name"] for p in w.presets])
        names = [a.text() for a in w.dialogue_panel.menu_prefix.actions() if not a.isSeparator()]
        self.assertIn("テスト前置き", names)                        # メイン画面の「前置き ▾」にも出る

    def test_tray_entries_open_the_right_tab(self):
        w, _ = self.mk()
        w.show_main(tab=1)
        self.assertTrue(w.unified.drawer_open)
        self.assertEqual(w.unified.drawer.current_tab(), TAB_HISTORY)
        w.show_main(tab=0)
        self.assertEqual(w.unified.drawer.current_tab(), TAB_TERMS)

    def test_status_messages_go_to_the_one_line_notice(self):
        w, _ = self.mk()
        w.statusBar().showMessage("テストの通知です", 5000)
        pump(0.1)
        self.assertEqual(w.dialogue_panel.notice_text(), "テストの通知です")


class LayoutFitTest(UBase):
    def test_expand_toggle_changes_height_and_collapses_back(self):
        w, _ = self.mk()
        p = w.dialogue_panel
        w.resize(400, 330)
        pump(0.1)
        h0 = w.height()
        p.btn_expand.click()
        pump(0.1)
        self.assertTrue(p.expanded)
        self.assertTrue(w.s["unified_expanded"])
        self.assertGreater(w.height(), h0)
        h1 = w.height()
        p.btn_expand.click()
        pump(0.1)
        self.assertFalse(p.expanded)
        self.assertLess(w.height(), h1)                       # 戻すと窓も標準の高さへ縮む
        from ui_unified import UNIFIED_DEFAULT
        self.assertLessEqual(w.height(), max(w.minimumHeight(), UNIFIED_DEFAULT[1]) + 2)

    def test_draft_shows_many_lines_by_default_and_grows_with_window(self):
        w, _ = self.mk()
        p = w.dialogue_panel
        fm = QFontMetrics(p.txt_draft.font())
        w.resize(*__import__("ui_unified").UNIFIED_DEFAULT)
        pump(0.1)
        h = p.txt_draft.height()
        self.assertGreaterEqual(h, 6 * fm.lineSpacing())      # 標準でも6行以上見える
        w.resize(w.width(), w.height() + 200)
        pump(0.1)
        self.assertGreater(p.txt_draft.height(), h + 150)     # 窓を縦に広げれば、その分伸びる


class DialogueSessionInUnifiedTest(UBase):
    def test_session_start_stop_with_the_record_button_and_notice(self):
        w, svc = self.mk()
        u, p = w.unified, w.dialogue_panel
        u.btn_rec.click()
        self.assertTrue(w._dialogue_active())
        self.assertTrue(svc.session.is_recording)
        pump(0.2)
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "停止")
        self.assertIn("セッションを始めました", p.notice_text())
        u.btn_rec.click()                                     # 停止＝セッション終了
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))
        pump(0.2)
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "録音")

    def test_commit_hotkey_goes_through_the_panel(self):
        w, _ = self.mk()
        calls = []
        w.dialogue_panel.commit_from_hotkey = lambda: calls.append("hk")
        w._on_hotkey("dialogue")
        self.assertEqual(calls, ["hk"])


# ===================================================================================================
# AI対話の配達まわり（フェイクの WorkerClient）。UnifiedPanel 単体
class PanelRig(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        from test_gui_v15 import FakeClipboard
        self._clip = FakeClipboard()                          # 本人のクリップボードは触らない。書き込みがあれば気づけるように偽物にする
        self._clip_patch = mock.patch.object(QGuiApplication, "clipboard", staticmethod(lambda: self._clip))
        self._clip_patch.start()

    def make(self, **kw):
        self.rig = Rig(**kw)
        p = UnifiedPanel(self.rig.ctl)
        p._confirm = lambda title, text: True
        p._warn_box = lambda title, text: None
        p._ask_text = lambda title, label, default="": None
        p._ask_end_choice = lambda report: "keep"
        p.setAttribute(Qt.WA_DontShowOnScreen, True)
        p.show()
        pump(0.1)
        self.p = p
        return p

    def tearDown(self):
        self._clip_patch.stop()
        self.p.timer.stop()
        self.rig.close()
        self.p.deleteLater()
        pump(0.1)


class NoAutoDeliveryTest(PanelRig):
    def test_nothing_is_delivered_without_an_operation(self):
        p = self.make()
        p.start_session()
        self.rig.say()                                        # 話した内容が下書きに入る
        pump(1.5)                                             # 無音のまま待っても自動確定しない
        self.assertEqual(self.rig.client.delivered, [])
        self.assertIn("こんにちは", p.txt_draft.toPlainText())
        p.refresh()
        self.assertEqual(self.rig.client.delivered, [])
        self.assertEqual(p.commit("window"), L.PRESS_COMMIT)  # 利用者の操作だけで届く
        self.assertTrue(wait_until(lambda: bool(self.rig.client.delivered)))
        self.assertEqual(self.rig.client.delivered[0][0], "相談窓")

    def test_expanding_and_drawer_do_not_deliver(self):
        p = self.make()
        p.start_session()
        self.rig.say()
        p.set_expanded(True)
        p.set_force_expanded(True)
        pump(0.5)
        self.assertEqual(self.rig.client.delivered, [])
        self.assertEqual(self.rig.ctl.ds.buffer.draft_text, "こんにちは。元気ですか。")   # 下書きは捨てずに残る
        self.assertIn("こんにちは", p.txt_draft.toPlainText())


class DestinationNoneTest(PanelRig):
    """宛先「なし（記録だけ）」: 何も届けず、クリップボードにも入れず、確定済みとして記録に残して画面から消す。"""

    def test_record_only_never_uses_the_worker_or_the_clipboard(self):
        p = self.make()
        self.assertEqual([p.cmb_preset.itemData(i) for i in range(p.cmb_preset.count())],
                         ["", "相談窓", NONE_KEY, PICK_KEY])
        self.assertEqual(p.cmb_preset.itemText(p.cmb_preset.findData(NONE_KEY)), "なし（記録だけ）")
        p.cmb_preset.setCurrentIndex(p.cmb_preset.findData(NONE_KEY))
        p.preset_chosen()
        self.assertTrue(self.rig.ctl.record_only)
        self.assertTrue(self.rig.settings["dialogue"]["dest_record_only"])
        self.assertNotIn("dest_clipboard", self.rig.settings["dialogue"])
        self.assertTrue(p.chk_send.isHidden())                # 送信までするは意味が無い
        p.start_session()
        self.rig.say()
        self.assertTrue(pump(2, until=lambda: p.btn_commit.text() == "確定して記録する"))
        self.assertEqual(p.commit("window"), L.PRESS_COMMIT)
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(5, until=lambda: bool(buf.history) and not buf.delivering and "記録しました" in p.notice_text()))
        self.assertEqual(self._clip.text(), "")               # クリップボードには何も入れない
        self.assertEqual(self.rig.client.delivered, [])       # 入力欄へは何もしない
        self.assertEqual(buf.undelivered_count, 0)
        self.assertEqual(buf.draft_text, "")                  # 下書きからは消える
        pump(0.5, until=lambda: p.txt_draft.final_text() == "")
        self.assertEqual(p.txt_draft.final_text(), "")        # 画面からも消える
        self.assertEqual((buf.history[0].stage, buf.history[0].text), ("record", "こんにちは。元気ですか。"))
        self.assertIn("記録しました ✓", p.notice_text())
        self.assertEqual(self.rig.client.held_hwnd, 0)        # 窓も探さない

    def test_history_dialog_says_record_only(self):
        p = self.make()
        self.rig.ctl.set_record_only(True)
        p.start_session()
        self.rig.say()
        p.commit("window")
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(5, until=lambda: bool(buf.history) and not buf.delivering))
        shown = []
        p._show_history = lambda d: shown.append(d)
        p.open_history()
        shown[0].refresh()
        self.assertIn("記録のみ", shown[0].lst.item(0).text())
        shown[0].timer.stop()

    def test_choosing_a_real_target_turns_it_off(self):
        p = self.make()
        self.rig.ctl.set_record_only(True)
        p.fill_presets()
        self.assertEqual(p.cmb_preset.currentData(), NONE_KEY)
        p.cmb_preset.setCurrentIndex(p.cmb_preset.findData("相談窓"))
        p.preset_chosen()
        self.assertFalse(self.rig.ctl.record_only)
        self.assertFalse(p.chk_send.isHidden())

    def test_pick_item_starts_a_pick_and_keeps_the_current_destination(self):
        p = self.make()
        p.cmb_preset.setCurrentIndex(p.cmb_preset.findData(PICK_KEY))
        p.preset_chosen()
        self.assertTrue(wait_until(lambda: bool(self.rig.client.picks)))
        self.assertEqual(p.cmb_preset.currentData(), "相談窓")
        pump(0.3)


class PrefixTest(PanelRig):
    """前置き: 「前置き ▾」で選んだときだけ、いまの下書きの先頭に1回だけ入る（×で外せる）。送る文字列 = 前置き本文 + 改行 + 下書き。
    どの宛先にも使える。自動で毎回は付けない・選択は記憶しない。"""

    def make(self, **kw):
        p = super().make(**kw)
        p._prompt_presets = lambda: [{"name": "要約", "prompt": "次を要約して。"}, {"name": "返事", "prompt": "返事を考えて。"}]
        p.fill_prompts()
        return p

    def test_menu_lists_saved_prefixes_and_none_and_inserts_only_on_choice(self):
        p = self.make()
        acts = [a.text() for a in p.menu_prefix.actions() if not a.isSeparator()]
        self.assertEqual(acts, ["要約", "返事", "（なし）"])
        self.assertTrue(p.box_prefix.isHidden())                     # 自動では入らない
        p.start_session()
        self.rig.say()
        p.commit("window")                                           # 前置きを選んでいない: そのまま届く
        self.assertTrue(wait_until(lambda: self.rig.client.delivered and not self.rig.ctl.ds.buffer.delivering))
        self.assertEqual(self.rig.client.delivered[0], ("相談窓", "こんにちは。元気ですか。"))
        self.assertTrue(p.box_prefix.isHidden())
        self.assertEqual(self.rig.ctl.prefix_text, "")

    def test_insert_shows_a_fixed_block_and_the_sent_text_is_prefix_newline_draft(self):
        p = self.make()
        p.start_session()
        self.rig.say()
        p.menu_prefix.actions()[0].trigger()                         # 要約
        self.assertFalse(p.box_prefix.isHidden())
        self.assertEqual(p.txt_prefix.toPlainText(), "次を要約して。")
        self.assertTrue(p.txt_prefix.isReadOnly())                   # 編集できない
        self.assertIn("要約", p.lbl_prefix_name.text())
        self.assertNotIn("要約", p.txt_draft.toPlainText())          # 下書き本体には混ぜない
        p.commit("window")
        self.assertTrue(wait_until(lambda: self.rig.client.delivered and not self.rig.ctl.ds.buffer.delivering))
        self.assertEqual(self.rig.client.delivered[0], ("相談窓", "次を要約して。\nこんにちは。元気ですか。"))
        pump(0.4)
        self.assertTrue(p.box_prefix.isHidden())                     # 1回だけ: 送ったら消える
        self.assertEqual(self.rig.ctl.prefix_text, "")
        self.assertEqual(self.rig.ctl.sending_prefix, "")
        self.assertEqual(self.rig.ctl.ds.buffer.history[0].text, "こんにちは。元気ですか。")   # 履歴の本文は下書きのまま

    def test_x_button_and_none_remove_the_block(self):
        p = self.make()
        p.menu_prefix.actions()[1].trigger()
        self.assertFalse(p.box_prefix.isHidden())
        p.btn_prefix_x.click()
        self.assertTrue(p.box_prefix.isHidden())
        self.assertEqual(self.rig.ctl.prefix_text, "")
        p.menu_prefix.actions()[0].trigger()
        p.menu_prefix.actions()[-1].trigger()                        # （なし）
        self.assertTrue(p.box_prefix.isHidden())
        p.menu_prefix.actions()[0].trigger()
        p.menu_prefix.actions()[1].trigger()                         # 入れ直せば置き換わる（二重に付かない）
        self.assertEqual(p.txt_prefix.toPlainText(), "返事を考えて。")

    def test_works_for_any_destination_not_only_copilot(self):
        p = self.make()
        self.assertEqual(self.rig.ctl.profile["name"], "相談窓")     # Copilot ではない宛先
        p.start_session()
        self.rig.say()
        p.insert_prefix("返事")
        p.commit("window")
        self.assertTrue(wait_until(lambda: bool(self.rig.client.delivered)))
        self.assertTrue(self.rig.client.delivered[0][1].startswith("返事を考えて。\n"))

    def test_record_only_does_not_send_and_the_block_is_consumed(self):
        p = self.make()
        self.rig.ctl.set_record_only(True)
        p.start_session()
        self.rig.say()
        p.insert_prefix("要約")
        p.commit("window")
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(5, until=lambda: bool(buf.history) and not buf.delivering))
        self.assertEqual(self.rig.client.delivered, [])
        pump(0.4)
        self.assertTrue(p.box_prefix.isHidden())
        self.assertEqual(self._clip.text(), "")

    def test_retry_after_a_failure_keeps_the_same_prefix(self):
        p = self.make(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        p.start_session()
        self.rig.say()
        p.insert_prefix("要約")
        p.commit("window")
        self.assertTrue(wait_until(lambda: self.rig.ctl.ds.buffer.undelivered_count == 1))
        p.retry()
        self.assertTrue(wait_until(lambda: len(self.rig.client.delivered) == 2 and not self.rig.ctl.ds.buffer.delivering))
        self.assertEqual(self.rig.client.delivered[0][1], self.rig.client.delivered[1][1])
        self.assertTrue(self.rig.client.delivered[1][1].startswith("次を要約して。\n"))
        pump(0.3)
        self.assertEqual(self.rig.ctl.sending_prefix, "")

    def test_nothing_is_remembered_between_drafts(self):
        p = self.make()
        p.insert_prefix("要約")
        self.assertNotIn("prompt_preset", self.rig.settings["dialogue"])   # 選択は記憶しない
        self.assertFalse(hasattr(p, "cmb_prompt"))
        self.assertFalse(hasattr(p, "row_prompt"))


class SendingBlockTest(PanelRig):
    """確定して届けるを押したあと、届いて送信できたと確かめるまで、下書きを消さず「送信中…」の固定ブロックで見せる。"""

    def slow_delivery(self):
        import threading
        gate = threading.Event()
        orig = self.rig.client.deliver

        def slow(profile, text, **kw):
            gate.wait(5)
            return orig(profile, text, **kw)
        self.rig.client.deliver = slow
        return gate

    def test_block_shows_the_message_while_sending_then_done_then_goes_away(self):
        p = self.make()
        gate = self.slow_delivery()
        p.FLASH_SEC = 0.6
        p.start_session()
        self.rig.say()
        p.commit("window")
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(4, until=lambda: buf.delivering and not p.box_send.isHidden()))
        self.assertEqual(p.lbl_send_head.text(), "送信中…")
        self.assertEqual(p.txt_send.toPlainText(), "こんにちは。元気ですか。")
        self.assertTrue(p.txt_send.isReadOnly())                        # 編集できない
        self.assertEqual(buf.draft_text, "")                            # バッファの下書きは取り出し済み
        gate.set()
        self.assertTrue(pump(4, until=lambda: p.lbl_send_head.text() in ("✓ 送信しました", "✓ 届けました")))
        self.assertFalse(p.box_send.isHidden())
        self.assertTrue(pump(3, until=lambda: p.box_send.isHidden()))   # 少しして消える

    def test_unconfirmed_send_stays_red_until_clicked(self):
        p = self.make(results=[{"status": "delivered", "stage": "uia", "sent": "unknown"}])
        p.FLASH_SEC = 0.3
        p.start_session()
        self.rig.say()
        p.commit("window")
        head = "⚠ 届きました。送信を確認できませんでした（入力欄を見てください）"
        self.assertTrue(pump(4, until=lambda: p.lbl_send_head.text() == head))
        pump(1.0)
        self.assertFalse(p.box_send.isHidden())                         # 自動では消えない
        self.assertIn(S.REC, p.lbl_send_head.styleSheet())
        p.dismiss_send_warning()
        pump(0.3)
        self.assertTrue(p.box_send.isHidden())

    def test_silence_countdown_goes_to_the_live_row_not_the_notice(self):
        p = self.make()
        p.start_session()
        p._countdown_text = lambda: "無音 0.5秒（あと0.7秒で認識開始）"
        self.rig.fs.feed(np.concatenate([C.noise(1, -70, 0), C.sine(1.0, -12)]))     # 喋っている途中（バーが出る）
        self.assertTrue(pump(3, until=lambda: any(b.label.startswith("無音") for b in p.txt_draft.tail_blocks())))
        p.refresh()
        self.assertNotIn("無音", p.lbl_notice.text())

    def test_flash_lasts_one_and_a_half_seconds_by_default(self):
        self.make()
        self.assertEqual(UnifiedPanel.FLASH_SEC, 1.5)

    def test_sent_header_when_the_send_is_confirmed(self):
        p = self.make(results=[{"status": "delivered", "stage": "uia", "sent": "sent"}])
        p.start_session()
        self.rig.say()
        p.commit("window")
        self.assertTrue(pump(4, until=lambda: p.lbl_send_head.text() == "✓ 送信しました"))

    def test_record_only_says_recorded(self):
        p = self.make()
        self.rig.ctl.set_record_only(True)
        p.start_session()
        self.rig.say()
        p.commit("window")
        self.assertTrue(pump(4, until=lambda: p.lbl_send_head.text() == "✓ 記録しました"))

    def test_failure_removes_the_block_and_shows_the_undelivered_row(self):
        p = self.make(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        gate = self.slow_delivery()
        p.start_session()
        self.rig.say()
        p.commit("window")
        self.assertTrue(pump(4, until=lambda: not p.box_send.isHidden()))
        gate.set()
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(wait_until(lambda: buf.undelivered_count == 1))
        pump(0.5)
        self.assertTrue(p.box_send.isHidden())
        self.assertFalse(p.box_und.isHidden())                          # いまの未配達の表示（赤い枠）へ
        self.assertEqual(p.txt_und.toPlainText(), "こんにちは。元気ですか。")

    def test_speech_during_sending_goes_to_a_new_draft_below(self):
        p = self.make()
        gate = self.slow_delivery()
        p.start_session()
        self.rig.say()
        p.commit("window")
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(4, until=lambda: buf.delivering and not p.box_send.isHidden()))
        buf.add_utterance("つづきです。", 20.0, 21.0, uid=20.0)           # 送信中に続けて喋った内容
        self.assertTrue(pump(3, until=lambda: "つづきです" in p.txt_draft.toPlainText()))
        self.assertNotIn("つづきです", p.txt_send.toPlainText())          # 送信中の文章に混ぜない
        self.assertEqual(p.txt_send.toPlainText(), "こんにちは。元気ですか。")
        gate.set()
        self.assertTrue(wait_until(lambda: self.rig.client.delivered and not buf.delivering))
        self.assertEqual(self.rig.client.delivered[0][1], "こんにちは。元気ですか。")
        self.assertIn("つづきです", p.txt_draft.toPlainText())

    def test_sending_block_shows_the_prefix_too(self):
        p = self.make()
        gate = self.slow_delivery()
        p._prompt_presets = lambda: [{"name": "要約", "prompt": "次を要約して。"}]
        p.start_session()
        self.rig.say()
        p.insert_prefix("要約")
        p.commit("window")
        self.assertTrue(pump(4, until=lambda: not p.box_send.isHidden()))
        self.assertEqual(p.txt_send.toPlainText(), "次を要約して。\nこんにちは。元気ですか。")   # 実際に送られるものが見える
        self.assertTrue(p.box_prefix.isHidden())
        gate.set()
        self.assertTrue(wait_until(lambda: bool(self.rig.client.delivered)))
        self.assertEqual(self.rig.client.delivered[0][1], "次を要約して。\nこんにちは。元気ですか。")


class WaitingNoticeTest(PanelRig):
    def test_notice_says_waiting_for_recognition_while_committing(self):
        import threading
        gate = threading.Event()
        p = self.make()
        self.rig.ctl._recognizer = lambda audio, ctx: (gate.wait(5), "こんにちは。")[1]
        p.start_session()
        self.rig.fs.feed(np.concatenate([C.noise(1, -70, 0), C.sine(1.5, -12), C.noise(2, -70, 1)]))   # 区切れて、清書待ち
        self.assertTrue(pump(3, until=lambda: bool(p.txt_draft.tail_blocks())))
        p.commit("window")                                            # 清書が済むのを待ってから確定する
        self.assertTrue(pump(3, until=lambda: p.btn_commit.text() == "確定して届ける（認識待ち 1件…）"))
        self.assertTrue(p.btn_commit.isEnabled())                       # 押せなくはしない
        self.assertNotIn("認識を待っています", p.notice_text())           # 通知の行には出さない
        gate.set()
        self.assertTrue(wait_until(lambda: bool(self.rig.client.delivered)))
        pump(0.5)
        self.assertNotIn("認識待ち", p.btn_commit.text())


class TopRowAndMenuTest(UBase):
    def test_pin_gear_drawer_sit_at_the_right_end_of_the_record_row(self):
        w, _ = self.mk()
        u = w.unified
        for b in (u.btn_pin, u.btn_gear, u.btn_drawer, u.chip, u.btn_rec):
            self.assertIs(b.parentWidget(), u.row_top)                # ボタンだけの上段の行は無い
        lay = u.row_top.layout()
        order = [lay.itemAt(i).widget() for i in range(lay.count()) if lay.itemAt(i).widget() is not None]
        self.assertLess(order.index(u.chip), order.index(u.btn_pin))
        self.assertLess(order.index(u.btn_pin), order.index(u.btn_gear))
        self.assertLess(order.index(u.btn_gear), order.index(u.btn_drawer))
        self.assertIs(u.col_layout.itemAt(0).widget(), u.row_top)      # 一番上が録音の行

    def test_gear_menu_items(self):
        w, _ = self.mk()
        texts = [a.text() for a in w.unified.menu.actions() if not a.isSeparator()]
        self.assertEqual(texts, ["設定…", "WAV を選んで清書…", "出力フォルダを開く", "確定履歴を開く…", "接続の状態（診断）…"])
        p = w.dialogue_panel
        for gone in ("act_repick", "act_del_preset", "btn_und_repick", "act_history_ctx"):
            self.assertFalse(hasattr(p, gone), gone)
        self.assertEqual(p.txt_draft.extra_actions, [])               # 下書き欄の右クリックに「確定履歴を開く」は無い

    def test_connection_status_includes_headset_mouse_and_click(self):
        w, _ = self.mk()
        p = w.dialogue_panel
        shown = []
        p._show_diag = lambda title, text: shown.append((title, text))
        p.act_headset.trigger()
        title, text = shown[0]
        self.assertEqual(title, "接続の状態（診断）")
        for key in ("ヘッドセット", "マウスのボタン", "舌打ち2回で確定", "診断ログ"):
            self.assertIn(key, text)
        self.assertEqual(self._clip.text(), "")                       # クリップボードへは自動で入れない

    def test_diagnostic_dialog_is_selectable_read_only_text_without_copy_buttons(self):
        from PySide6.QtWidgets import QPushButton
        from ui_dialogue import DiagnosticDialog
        d = DiagnosticDialog("接続の状態（診断）", "ヘッドセット: 受付中\nマウス: 停止中")
        self.assertTrue(d.txt.isReadOnly())
        self.assertTrue(d.txt.textInteractionFlags() & Qt.TextSelectableByMouse)
        self.assertEqual([b.text() for b in d.findChildren(QPushButton)], ["閉じる"])
        d.txt.selectAll()
        self.assertEqual(self._clip.text(), "")
        d.deleteLater()


class PanelBasicsTest(PanelRig):
    def test_undelivered_row_and_retry_discard_buttons_and_selectable_text(self):
        p = self.make(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        p.start_session()
        self.rig.say()
        p.commit("window")
        self.assertTrue(wait_until(lambda: self.rig.ctl.ds.buffer.undelivered_count == 1))
        pump(0.4)
        p.refresh()
        self.assertFalse(p.box_und.isHidden())
        self.assertEqual(p.btn_commit.text(), "未配達を再試行")
        self.assertFalse(hasattr(p, "btn_copy"))              # コピーのボタンは無い（クリップボード方針）
        self.assertEqual(p.txt_und.toPlainText(), "こんにちは。元気ですか。")   # 選択して Ctrl+C／右クリック→コピーできる
        self.assertTrue(p.txt_und.isReadOnly())
        self.assertTrue(p.txt_und.textInteractionFlags() & Qt.TextSelectableByMouse)
        self.assertTrue(p.lbl_und.textInteractionFlags() & Qt.TextSelectableByMouse)
        self.assertEqual(self._clip.text(), "")
        p.btn_discard.click()
        pump(0.2)
        p.refresh()
        self.assertTrue(p.box_und.isHidden())

    def test_target_warning_is_not_shown_for_unselected_or_record_only(self):
        p = self.make(preset=False)
        p.refresh()
        self.assertTrue(p.lbl_warn.isHidden())                # 未選択は、コンボの「（宛先を選ぶ）」で足りる
        self.rig.ctl.set_record_only(True)
        p.refresh()
        self.assertTrue(p.lbl_warn.isHidden())

    def test_ctrl_enter_in_the_draft_commits(self):
        p = self.make()
        p.start_session()
        self.rig.say()
        pump(0.3)
        ed = p.txt_draft
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        QApplication.sendEvent(ed, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.ControlModifier))
        self.assertTrue(wait_until(lambda: bool(self.rig.client.delivered)))


class LiveWaveTest(PanelRig):
    """喋っている途中は、下書き欄の末尾の行に、喋った長さに比例してバーが伸びる（無音のあいだは伸ばさない）。
    区切れたら、そのバーが「認識中…」の長さ固定のバーになり、清書が出たら白い文字に置き換わる。1行 WAVE_ROW_SEC 秒で折り返す。"""

    def live_blocks(self, p):
        return [b for b in p.txt_draft.tail_blocks() if b.kind == L.K_LIVE]

    def test_bar_grows_with_speaking_length_in_the_draft_tail_and_freezes_in_silence(self):
        p = self.make()
        self.assertFalse(hasattr(p, "live_wave"))                 # 下書き欄の下の別の波形は無い
        p.start_session()
        p.update_live()
        self.assertEqual(self.live_blocks(p), [])                 # まだ喋っていない
        self.rig.fs.feed(np.concatenate([C.noise(1, -70, 0), C.sine(1.0, -12)]))
        p.update_live()
        n1 = len(self.live_blocks(p)[0].wave)
        self.assertRegex(self.live_blocks(p)[0].label, r"^喋っています 0:0\d$")
        self.assertIn("喋っています", p.txt_draft.toPlainText())
        self.rig.fs.feed(C.sine(1.0, -12))
        p.update_live()
        n2 = len(self.live_blocks(p)[0].wave)
        self.assertGreater(n2, n1)                                # 喋るほど伸びる
        self.assertAlmostEqual(n2 - n1, L.WAVE_PER_SEC, delta=2)  # 1秒ぶんの棒だけ
        self.rig.fs.feed(C.noise(0.7, -70, 1))                    # 無音: 伸ばさない
        p.update_live()
        self.assertEqual(len(self.live_blocks(p)[0].wave), n2)
        self.assertRegex(self.live_blocks(p)[0].label, r"^無音 0\.\d秒（あと0\.\d秒で認識開始）$")   # 黙ると、同じ行がカウントダウンになる

    def test_hidden_again_when_the_session_ends(self):
        p = self.make()
        p.start_session()
        self.rig.fs.feed(np.concatenate([C.noise(1, -70, 0), C.sine(1.0, -12)]))
        p.update_live()
        self.assertTrue(self.live_blocks(p))
        p.end_session()
        self.assertTrue(pump(6, until=lambda: not self.rig.ctl.active and not p._ending))
        p.refresh()
        self.assertEqual(self.live_blocks(p), [])

    def test_bar_wraps_to_the_next_row_every_row_seconds(self):
        from ui_draft import wave_geometry, wave_image
        self.make()
        self.assertEqual(L.WAVE_ROW_BARS, L.WAVE_ROW_SEC * L.WAVE_PER_SEC)
        self.assertEqual(wave_geometry(5), (5, 1))
        self.assertEqual(wave_geometry(L.WAVE_ROW_BARS), (L.WAVE_ROW_BARS, 1))
        self.assertEqual(wave_geometry(L.WAVE_ROW_BARS + 1), (L.WAVE_ROW_BARS, 2))
        self.assertEqual(wave_geometry(L.WAVE_ROW_BARS * 2 + 3)[1], 3)
        img = wave_image((4,) * (L.WAVE_ROW_BARS + 6))
        self.assertGreater(img.height(), wave_image((4,) * 6).height())     # 行が増える
        self.assertEqual(img.width(), wave_image((4,) * L.WAVE_ROW_BARS).width())   # 幅は1行ぶんまで
        self.assertEqual(L.wave_bars_for(20), 20 * L.WAVE_PER_SEC)           # 認識中のバーも同じ尺度（折り返しても頭打ちにしない）

    def test_cut_utterance_keeps_its_bar_as_recognizing_row_then_white_text(self):
        import threading
        gate = threading.Event()
        p = self.make()
        self.rig.ctl._recognizer = lambda audio, ctx: (gate.wait(5), "こんにちは。")[1]
        p.start_session()
        self.rig.fs.feed(np.concatenate([C.noise(1, -70, 0), C.sine(1.5, -12), C.noise(2, -70, 1)]))   # 区切れて、清書待ち
        self.assertTrue(pump(3, until=lambda: any(b.kind == L.K_WAVE for b in p.txt_draft.tail_blocks())))
        self.assertIn("認識中", p.txt_draft.toPlainText())
        self.assertEqual(self.live_blocks(p), [])                  # 喋っている途中のバーは、認識中のバーに変わった
        self.assertEqual(p.txt_draft.final_text(), "")
        gate.set()
        self.assertTrue(pump(5, until=lambda: p.txt_draft.final_text() == "こんにちは。" and not p.txt_draft.tail_blocks()))
        self.assertEqual(p.txt_draft.toPlainText().strip(), "こんにちは。")        # 白い文字に置き換わる


class NoClipboardWriteTest(PanelRig):
    """このツールがクリップボードへ文字を入れてよいのは、利用者が明示的に Ctrl+C／右クリックのコピーをしたときだけ。"""

    def test_nothing_writes_to_the_clipboard_during_a_whole_session(self):
        p = self.make(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        p.start_session()
        self.rig.say()
        p.commit("window")                                    # 未配達になる
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(wait_until(lambda: buf.undelivered_count == 1))
        pump(0.4)
        self.assertEqual(self._clip.text(), "")
        p.end_session()
        self.assertTrue(pump(6, until=lambda: not self.rig.ctl.active and not p._ending))
        self.assertEqual(self._clip.text(), "")
        for gone in ("_set_clipboard", "copy_undelivered"):
            self.assertFalse(hasattr(p, gone), gone)

    def test_texts_stay_selectable_so_the_user_can_copy_them_by_hand(self):
        """Ctrl+C／右クリック→コピーは利用者の操作で行う。そのために、文字は選択できる状態を保つ（下書き・未配達・確定履歴）。"""
        p = self.make()
        p.start_session()
        self.rig.say()
        pump(0.5, until=lambda: p.txt_draft.final_text() != "")
        self.assertEqual(self._clip.text(), "")
        p.txt_draft.selectAll()
        self.assertEqual(p.txt_draft.textCursor().selectedText(), "こんにちは。元気ですか。")
        self.assertTrue(p.txt_draft.textInteractionFlags() & Qt.TextSelectableByMouse)
        self.assertEqual(self._clip.text(), "")               # 選択しただけでは、何も入らない
        shown = []
        p._show_history = lambda d: shown.append(d)
        p.open_history()
        self.assertTrue(shown[0].txt.textInteractionFlags() & Qt.TextSelectableByMouse)
        shown[0].timer.stop()


if __name__ == "__main__":
    unittest.main(verbosity=1)
