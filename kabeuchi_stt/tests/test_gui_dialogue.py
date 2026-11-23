# -*- coding: utf-8 -*-
"""AI対話モードのコンパクト窓のスモークテスト（offscreen・grab）。日本語は豆腐になりうる（レイアウトの確認用）。
実ダイアログ・クリップボード（このツールは自分では書かない）・マイク・ワーカー・前面化は使わない。フェイクの録音・認識器・WorkerClient。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

import _common as C  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QInputDialog  # noqa: E402

import ui_dialogue_logic as L  # noqa: E402
import ui_style as S  # noqa: E402
from test_dialogue_controller import Rig, PROFILE  # noqa: E402
from test_dialogue_session import wait_until  # noqa: E402
from ui_dialogue import DialoguePanel  # noqa: E402


def _forbid(*a, **k):
    raise AssertionError("テスト中に実ダイアログを開く呼び出しがありました")


for _cls, _names in ((QMessageBox, ("question", "warning", "information", "critical")),
                     (QInputDialog, ("getText",))):
    for _n in _names:
        setattr(_cls, _n, staticmethod(_forbid))
QDialog.exec = _forbid
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


class PanelBase(unittest.TestCase):
    def make(self, **kw):
        self.rig = Rig(**kw)
        self.win = DialoguePanel(self.rig.ctl)
        w = self.win
        self.warned, self.confirms = [], []
        self.answer, self.end_choice, self.text_answers = True, "keep", []
        w._confirm = lambda title, text: (self.confirms.append(title), self.answer)[1]
        w._warn_box = lambda title, text: self.warned.append(title)
        w._ask_end_choice = lambda report: self.end_choice
        w._ask_text = lambda title, label, default="": self.text_answers.pop(0) if self.text_answers else None
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        pump(0.1)
        return w

    def tearDown(self):
        self.win.timer.stop()
        self.rig.close()
        self.win.deleteLater()
        pump(0.1)


def type_at_final_end(w, text):
    """キーボードで確定済み部分の末尾へ打つのと同じ経路（textChanged）。"""
    from PySide6.QtGui import QTextCursor
    ed = w.txt_draft
    c = ed.textCursor()
    c.setPosition(ed.final_end())
    ed.setTextCursor(c)
    c.insertText(text)


def send_text(ed, text):
    """日本語などの非ASCII文字のキー入力（QTest.keyClicks は非ASCIIでプロセスごと落ちるため、キーイベントを直接送る）。"""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    for ch in text:
        QApplication.sendEvent(ed, QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, ch))


def count_pixels(img, pred):
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if pred(img.pixelColor(x, y)):
                n += 1
    return n


def is_orange(c):
    return c.red() > 200 and 120 < c.green() < 190 and c.blue() < 110


def is_teal(c):
    return c.red() < 110 and c.green() > 150 and c.blue() > 130


def is_white(c):
    return c.red() > 215 and c.green() > 215 and c.blue() > 215


class WindowTest(PanelBase):
    def test_renders_and_grabs_one_column(self):
        w = self.make()
        img = w.grab()
        self.assertFalse(img.isNull())
        self.assertEqual(w.btn_session.text(), "セッション開始")
        self.assertTrue(w.btn_commit.isEnabled() is False)              # セッション前は確定できない
        self.assertTrue(w.box_und.isHidden())
        self.assertTrue(w.lbl_warn.isHidden())                           # 宛先が見つかっているか未確認のときは警告なし
        self.assertEqual(w.cmb_preset.itemData(w.cmb_preset.currentIndex()), "相談窓")   # last_target を選択済み
        # 消したもの: 状態ピル・認識の状態・宛先の詳細・常設の履歴一覧・仮のラベル
        for gone in ("lbl_state", "lbl_rec", "lbl_recog", "lbl_target_name", "lbl_target_win", "lbl_target_status",
                     "lst_hist", "lbl_prov", "lst_und", "lbl_headset"):
            self.assertFalse(hasattr(w, gone), gone)

    def test_session_commit_undelivered_retry_end_flow(self):
        w = self.make(results=[{"status": "undelivered", "reason": "foreground_failed"}])
        w.start_session()
        self.assertTrue(self.rig.ctl.active)
        self.assertTrue(w.btn_commit.isEnabled() or pump(0.3, until=lambda: w.btn_commit.isEnabled()))
        self.assertTrue(pump(2, until=lambda: self.rig.ctl.target_state == L.TG_HELD))
        self.assertTrue(w.lbl_warn.isHidden())                           # 握れているときは「握った窓」等を出さない
        self.rig.say()
        pump(0.5, until=lambda: w.txt_draft.final_text() == "こんにちは。元気ですか。")
        self.assertEqual(w.txt_draft.final_text(), "こんにちは。元気ですか。")
        w.btn_commit.click()
        self.assertTrue(pump(3, until=lambda: not w.box_und.isHidden()))
        self.assertIn("未配達 1件", w.lbl_und.text())
        self.assertEqual(w.btn_commit.text(), "未配達を再試行")          # 次の確定は再試行
        self.assertTrue(pump(2, until=lambda: "届けられませんでした" in w.notice_text()))   # 下段の1行にも出る（警告は残る）
        self.assertFalse(w.grab().isNull())
        self.assertFalse(hasattr(w, "btn_copy"))                              # コピーのボタンは無い（クリップボード方針）
        self.assertEqual(w.txt_und.toPlainText(), "こんにちは。元気ですか。")     # 未配達の文は、選択してコピーできるテキストで見せる
        self.assertTrue(w.txt_und.isReadOnly())
        self.assertTrue(w.txt_und.textInteractionFlags() & Qt.TextSelectableByMouse)
        w.btn_retry.click()
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(3, until=lambda: buf.history and buf.history[0].status == "delivered"))
        self.assertTrue(w.box_und.isHidden())
        self.assertEqual(len(buf.history), 1)
        self.assertTrue(pump(2, until=lambda: "✓" in w.notice_text()))
        self.assertIn("入力欄に入れました ✓", w.notice_text())             # 短い成功通知（時刻つき）
        w.end_session()
        self.assertTrue(pump(5, until=lambda: not self.rig.ctl.active and not w._ending))
        self.assertEqual(self.rig.client.shutdowns, 1)

    def test_end_with_undelivered_keeps_or_discards_and_never_copies(self):
        w = self.make(results=[{"status": "undelivered", "reason": "x"}])
        w.start_session()
        self.rig.say()
        w.commit("window")
        self.assertTrue(pump(3, until=lambda: not w.box_und.isHidden()))
        self.end_choice = "keep"
        w.end_session()
        self.assertTrue(pump(5, until=lambda: not self.rig.ctl.active and not w._ending))
        self.assertEqual(self.rig.ctl.ds.buffer.undelivered_count, 1)       # 残す（次のセッションへ持ち越す）
        self.assertFalse(hasattr(w, "_set_clipboard"))                      # クリップボードへは何も入れない
        w.discard_undelivered()                                             # 確認 → 破棄
        self.assertEqual(self.rig.ctl.ds.buffer.undelivered_count, 0)
        self.assertIn("未配達を破棄", self.confirms)

    def test_discard_declined_keeps_everything(self):
        w = self.make(results=[{"status": "undelivered", "reason": "x"}])
        w.start_session()
        self.rig.say()
        w.commit()
        self.assertTrue(pump(3, until=lambda: not w.box_und.isHidden()))
        self.answer = False
        w.discard_undelivered()
        self.assertEqual(self.rig.ctl.ds.buffer.undelivered_count, 1)

    def test_hold_and_hotkey_commit(self):
        w = self.make()
        w.start_session()
        w.btn_hold.click()
        self.assertTrue(self.rig.ctl.ds.is_held)
        self.assertEqual(w.btn_hold.text(), "保留を解除")
        w.btn_hold.click()
        self.assertFalse(self.rig.ctl.ds.is_held)
        self.rig.say()
        w.commit_from_hotkey()
        self.assertTrue(wait_until(lambda: self.rig.client.delivered))

    def test_pick_flow_saves_with_validated_name(self):
        w = self.make(preset=False)
        self.assertEqual(w.btn_repick.text(), "宛先を選ぶ")                  # 宛先が無いときは「選ぶ」
        self.assertFalse(w.lbl_warn.isHidden())
        self.assertIn("宛先なし", w.lbl_warn.text())
        self.text_answers = ["a/b", "新しい窓"]          # 不正な名前 → 聞き直し → 保存
        w.pick_target("")
        self.assertTrue(pump(3, until=lambda: self.rig.ctl.profile is not None))
        self.assertEqual(self.rig.ctl.profile["name"], "新しい窓")
        self.assertEqual(self.warned, ["名前を確認してください"])
        self.assertEqual(w.cmb_preset.findData("新しい窓") >= 0, True)
        # 同名の上書きは確認する
        self.text_answers = ["新しい窓"]
        self.answer = False
        self.rig.ctl.picked = {"profile": {**PROFILE, "name": ""}}
        w._pick_name = ""
        w._after_picked({"profile": PROFILE})
        self.assertIn("上書きの確認", self.confirms)

    def test_pick_failure_notice(self):
        w = self.make(preset=False, pick_result={"status": "timeout"})
        w.pick_target("")
        self.assertTrue(pump(3, until=lambda: "クリックされません" in w.notice_text()))

    def test_target_not_found_shows_short_warning_only(self):
        w = self.make(resolve_result={"status": "none", "candidates": []})
        w.start_session()
        self.assertTrue(pump(3, until=lambda: not w.lbl_warn.isHidden()))
        self.assertIn("見つかりません", w.lbl_warn.text())
        self.assertNotIn("app.exe", w.lbl_warn.text())                   # プロセス名・タイトルは出さない
        self.assertIn(S.ORANGE, w.lbl_warn.styleSheet())

    def test_headset_registration_failure_warns_in_header(self):
        w = self.make(headset_ok=False)
        w.start_session()
        self.assertTrue(pump(2, until=lambda: not w.lbl_warn.isHidden()))
        self.assertIn("⚠ ヘッドセット受付できません", w.lbl_warn.text())
        self.assertIn("他のアプリ", w.lbl_warn.text())

    def test_headset_press_always_gives_a_reaction(self):
        w = self.make()
        w.start_session()
        pump(0.2)
        w.refresh()
        self.rig.headset.press()                                         # 下書きが空のとき
        self.assertTrue(pump(2, until=lambda: "下書きが空です" in w.notice_text()))
        self.assertIn("ヘッドセットのボタンを受け取りました → 下書きが空です", w.notice_text())
        self.assertEqual(self.rig.client.delivered, [])                  # 何も届けない
        self.rig.say()
        pump(0.5, until=lambda: w.txt_draft.final_text() != "")
        self.rig.headset.press()
        self.assertTrue(pump(3, until=lambda: self.rig.client.delivered))
        self.assertTrue(pump(2, until=lambda: "✓" in w.notice_text()))

    def test_headset_press_with_undelivered_says_retry(self):
        w = self.make(results=[{"status": "undelivered", "reason": "x"}])
        w.start_session()
        self.rig.say()
        pump(0.4, until=lambda: w.txt_draft.final_text() != "")
        self.rig.headset.press()
        self.assertTrue(pump(3, until=lambda: not w.box_und.isHidden()))
        events = []
        orig = w.set_notice
        w.set_notice = lambda t, tone="muted": (events.append(t), orig(t, tone))
        self.rig.headset.press()
        self.assertTrue(pump(3, until=lambda: any("再試行します" in t for t in events)))

    def test_menu_has_settings_history_headset_and_opens_them(self):
        w = self.make()
        called, shown, infos = [], [], []
        w.on_settings = lambda: called.append(1)
        w._show_history = lambda d: shown.append(d)
        w._show_diag = lambda title, text: infos.append((title, text))
        w.act_settings.trigger()
        w.act_history.trigger()
        w.act_headset.trigger()
        self.assertEqual(called, [1])
        self.assertEqual(len(shown), 1)
        self.assertIn("ヘッドセット", infos[0][1])
        self.assertIn("診断ログ", infos[0][1])
        shown[0].timer.stop()

    def test_history_dialog_lists_messages_with_status(self):
        w = self.make()
        w.start_session()
        buf = self.rig.ctl.ds.buffer
        buf.add_utterance("せいしょ", 5.0, 6.0, uid=5.0)
        buf.commit()
        shown = []
        w._show_history = lambda d: shown.append(d)
        w.open_history()
        d = shown[0]
        d.refresh()
        self.assertEqual(d.lst.count(), 1)
        self.assertIn("せいしょ", d.lst.item(0).text())
        self.assertEqual(d.txt.toPlainText(), "せいしょ")
        self.assertNotIn("清書版", d.txt.toPlainText())                      # 清書版の表示は廃止（仮認識の廃止）
        self.assertTrue(d.txt.isReadOnly())                                   # 選択して Ctrl+C でコピーできる
        d.timer.stop()

    def test_notice_is_transient_but_warning_stays(self):
        w = self.make()
        w._notice.hold_sec, w._notice.fade_sec = 0.2, 0.2
        w.set_notice("届けました ✓ 14:32", "ok")
        self.assertIn("届けました", w.notice_text())
        self.assertTrue(pump(1.0, until=lambda: "届けました" not in w.notice_text()))
        w.set_notice("送信できませんでした", "warn")
        pump(0.8)
        self.assertEqual(w.notice_text(), "送信できませんでした")

    def test_state_colors_follow_tone(self):
        w = self.make()
        w.start_session()
        pump(0.3)
        self.assertEqual(w.btn_session.text(), "セッション終了")
        self.assertEqual(w.btn_session.objectName(), "recStop")


class DraftEditorTest(PanelBase):
    """下書き欄（1つの編集可能な欄）: 確定済み（白）・認識中（小さな波形）、手で直す、Ctrl+Enter、送信チェック。"""

    def start(self, **kw):
        w = self.make(**kw)
        w.start_session()
        pump(0.2)
        return w

    def test_edit_by_keyboard_goes_to_buffer_and_commit_sends_the_edit(self):
        w = self.start()
        self.rig.say()
        pump(0.5, until=lambda: w.txt_draft.final_text() == "こんにちは。元気ですか。")
        self.assertFalse(w.txt_draft.isReadOnly())
        w.txt_draft.set_final_text("")                                       # いったん空にして、打ち直す（キーボード編集と同じ経路）
        type_at_final_end(w, "直した文章です")
        self.assertEqual(self.rig.ctl.ds.buffer.draft_text, "直した文章です")
        self.assertTrue(self.rig.ctl.ds.buffer.draft_edited)
        w.btn_commit.click()                                                # 確定は欄の現在の内容を送る
        self.assertTrue(wait_until(lambda: self.rig.client.delivered))
        self.assertEqual(self.rig.client.delivered[0][1], "直した文章です")
        pump(0.5, until=lambda: w.txt_draft.final_text() == "")
        self.assertEqual(w.txt_draft.final_text(), "")                       # 確定したら欄は空
        hist = self.rig.ctl.ds.buffer.history
        self.assertEqual(len(hist), 1)
        self.assertTrue(hist[0].edited)

    def test_real_key_events_edit_the_white_text(self):
        from PySide6.QtTest import QTest
        w = self.start()
        self.rig.ctl.ds.buffer.add_utterance("確定文")
        pump(0.4, until=lambda: w.txt_draft.final_text() == "確定文")
        ed = w.txt_draft
        ed.setFocus()
        c = ed.textCursor()
        c.setPosition(ed.final_end())
        ed.setTextCursor(c)
        QTest.keyClicks(ed, "ABC")
        QTest.keyClick(ed, Qt.Key_Backspace)
        self.assertEqual(ed.final_text(), "確定文AB")
        self.assertEqual(self.rig.ctl.ds.buffer.draft_text, "確定文AB")

    def test_new_recognition_appends_without_breaking_cursor_or_selection(self):
        from PySide6.QtGui import QTextCursor
        w = self.start()
        buf = self.rig.ctl.ds.buffer
        type_at_final_end(w, "こんにちは")
        ed = w.txt_draft
        # カーソルが途中にあるとき: 位置を変えない
        c = ed.textCursor()
        c.setPosition(2)
        ed.setTextCursor(c)
        buf.add_utterance("追記")
        pump(0.5, until=lambda: ed.final_text() == "こんにちは追記")
        self.assertEqual(ed.final_text(), "こんにちは追記")
        self.assertEqual((ed.textCursor().anchor(), ed.textCursor().position()), (2, 2))
        # 選択中: 選択を変えない
        c = ed.textCursor()
        c.setPosition(1)
        c.setPosition(4, QTextCursor.KeepAnchor)
        ed.setTextCursor(c)
        buf.add_utterance("さらに")
        pump(0.5, until=lambda: ed.final_text().endswith("さらに"))
        self.assertEqual((ed.textCursor().anchor(), ed.textCursor().position()), (1, 4))
        self.assertEqual(ed.textCursor().selectedText(), "んにち")
        # カーソルが確定済み部分の末尾にあるとき: 足した分のあとへ進む（打ち続けられる）
        c = ed.textCursor()
        c.setPosition(ed.final_end())
        ed.setTextCursor(c)
        buf.add_utterance("最後")
        pump(0.5, until=lambda: ed.final_text().endswith("最後"))
        self.assertEqual(ed.textCursor().position(), ed.final_end())
        # 手で直した内容は、追記で壊れない。バッファも一致
        self.assertEqual(buf.draft_text, ed.final_text())

    def test_user_typing_between_recognitions_is_kept(self):
        w = self.start()
        buf = self.rig.ctl.ds.buffer
        buf.add_utterance("一つ目")
        pump(0.5, until=lambda: w.txt_draft.final_text() == "一つ目")
        type_at_final_end(w, "を直した")                                       # 利用者の修正
        buf.add_utterance("二つ目")
        pump(0.5, until=lambda: w.txt_draft.final_text().endswith("二つ目"))
        self.assertEqual(w.txt_draft.final_text(), "一つ目を直した二つ目")
        self.assertEqual(buf.draft_text, "一つ目を直した二つ目")

    def test_recognition_arriving_during_edit_conflict_is_not_lost(self):
        w = self.start()
        buf = self.rig.ctl.ds.buffer
        buf.add_utterance("一つ目")
        pump(0.5, until=lambda: w.txt_draft.final_text() == "一つ目")
        buf.add_utterance("二つ目")                                         # 画面が追いつく前に、利用者が打つ
        type_at_final_end(w, "!")
        pump(0.6, until=lambda: "二つ目" in buf.draft_text and "二つ目" in w.txt_draft.final_text())
        self.assertIn("二つ目", buf.draft_text)                              # 認識結果は失われない
        self.assertIn("二つ目", w.txt_draft.final_text())
        self.assertEqual(buf.draft_text, w.txt_draft.final_text().strip())

    def test_period_patch_keeps_cursor_at_end(self):
        w = self.start()
        buf = self.rig.ctl.ds.buffer
        buf.gap_sec, buf.force_gap_sec = 2.0, 3.0
        buf.add_utterance("えっと、", 0.0, 1.0)
        pump(0.5, until=lambda: w.txt_draft.final_text() == "えっと、")
        buf.add_utterance("つぎです", 20.0, 21.0)                            # 間が長い: 「、」→「。」＋改行2つ（区切りは空行）
        pump(0.5, until=lambda: w.txt_draft.final_text() == "えっと。\n\nつぎです")
        self.assertEqual(w.txt_draft.final_text(), "えっと。\n\nつぎです")
        self.assertEqual(w.txt_draft.textCursor().position(), w.txt_draft.final_end())

    # ---- 認識中（波形）→ 白（同じ発話がその場で置き換わる）
    def test_recognizing_row_is_a_small_wave_and_final_replaces_it_in_place(self):
        w = self.start()
        ds = self.rig.ctl.ds
        buf = ds.buffer
        ed = w.txt_draft
        buf.add_utterance("確定済み", uid=0.0)
        pump(0.4, until=lambda: ed.final_text() == "確定済み")
        self.assertEqual(ed.tail_blocks(), [])
        waves = [{"uid": 7.0, "start": 7.0, "end": 9.0, "env": (1, 3, 5, 8, 6, 4, 2, 1)}]
        ds.pending_waves = lambda: list(waves)                                # 区切れて、清書待ちの発話
        pump(0.5, until=lambda: bool(ed.tail_blocks()))
        self.assertEqual([(b.uid, b.kind) for b in ed.tail_blocks()], [(7.0, L.K_WAVE)])
        self.assertEqual(ed.tail_blocks()[0].wave, (1, 3, 5, 8, 6, 4, 2, 1))
        self.assertIn("認識中", ed.toPlainText())                              # 「変換待ち」ではなく「認識中…」
        self.assertNotIn("変換待ち", ed.toPlainText())
        self.assertEqual(ed.final_text(), "確定済み")                         # 確定済み部分には混ざらない
        img = ed.grab().toImage()
        self.assertGreater(count_pixels(img, is_teal), 10)                    # 波形（青緑）が描かれる
        self.assertEqual(count_pixels(img, is_orange), 0)                     # 黄色（仮認識）は出ない
        waves.clear()
        buf.add_utterance("せいしょ", 7.0, 9.0, uid=7.0)                        # 清書の結果が出ると、白い文字に置き換わる
        pump(0.6, until=lambda: not ed.tail_blocks() and ed.final_text().endswith("せいしょ"))
        self.assertEqual(ed.tail_blocks(), [])
        self.assertEqual(ed.final_text(), "確定済みせいしょ")
        self.assertEqual(count_pixels(ed.grab().toImage(), is_teal), 0)

    def test_white_and_wave_have_distinct_colors_in_one_grab(self):
        w = self.start()
        ds = self.rig.ctl.ds
        ed = w.txt_draft
        ds.buffer.add_utterance("しろいぶん", uid=0.0)
        ds.pending_waves = lambda: [{"uid": 9.0, "start": 9.0, "end": 10.0, "env": (2, 5, 8, 4, 6, 3)}]
        pump(0.6, until=lambda: len(ed.tail_blocks()) == 1 and ed.final_text() == "しろいぶん")
        img = ed.grab().toImage()
        self.assertGreater(count_pixels(img, is_white), 5)
        self.assertGreater(count_pixels(img, is_teal), 5)
        self.assertEqual(count_pixels(img, is_orange), 0)
        self.assertLessEqual(img.width(), 460)

    def test_wave_block_ignores_typing_and_deleting(self):
        from PySide6.QtTest import QTest
        w = self.start()
        ds = self.rig.ctl.ds
        ed = w.txt_draft
        ds.buffer.add_utterance("あ", uid=0.0)
        ds.pending_waves = lambda: [{"uid": 9.0, "start": 9.0, "end": 10.0, "env": (2, 5, 8, 4, 6, 3)}]
        pump(0.6, until=lambda: ed.tail_blocks() and ed.final_text() == "あ")
        before = ed.toPlainText()
        ed.setFocus()
        wave_block = ed.document().lastBlock()
        for pos in (wave_block.position(), wave_block.position() + 1):
            c = ed.textCursor()
            c.setPosition(pos)
            ed.setTextCursor(c)
            self.assertEqual(ed.textCursor().position(), ed.final_end())        # 波形の中にはカーソルが入らない（白の末尾へ戻る）
        # 波形をまたぐ選択での削除・貼り付けは受け付けない（構造ガード）
        self.assertEqual([(b.uid, b.kind) for b in ed.tail_blocks()], [(9.0, L.K_WAVE)])
        # 選択が波形をまたぐ削除・貼り付けも受け付けない
        ed.selectAll()
        QTest.keyClick(ed, Qt.Key_Backspace)
        self.assertEqual(ed.toPlainText(), before)
        self.assertEqual(ed.final_text(), "あ")
        # 境目（確定済みの末尾で Delete / 後ろ側の先頭で Backspace）で結合させない
        c = ed.textCursor()
        c.setPosition(ed.final_end())
        ed.setTextCursor(c)
        QTest.keyClick(ed, Qt.Key_Delete)
        self.assertEqual(ed.toPlainText(), before)
        # 確定済み部分は普通に直せる
        send_text(ed, "い")
        self.assertEqual(ed.final_text(), "あい")
        self.assertEqual(len(ed.tail_blocks()), 1)

    def test_ctrl_enter_commits_but_plain_enter_is_a_newline(self):
        from PySide6.QtTest import QTest
        w = self.start()
        self.rig.ctl.ds.buffer.add_utterance("送る文")
        pump(0.4, until=lambda: w.txt_draft.final_text() == "送る文")
        w.txt_draft.setFocus()
        c = w.txt_draft.textCursor()
        c.setPosition(w.txt_draft.final_end())
        w.txt_draft.setTextCursor(c)
        QTest.keyClick(w.txt_draft, Qt.Key_Return)                           # Enter は改行のまま
        self.assertEqual(w.txt_draft.final_text(), "送る文\n")
        self.assertEqual(self.rig.client.delivered, [])
        QTest.keyClick(w.txt_draft, Qt.Key_Return, Qt.ControlModifier)       # Ctrl+Enter で確定
        self.assertTrue(wait_until(lambda: self.rig.client.delivered))
        self.assertEqual(self.rig.client.delivered[0][1], "送る文")

    def test_discard_button_clears_editor_and_waves(self):
        w = self.start()
        ds = self.rig.ctl.ds
        buf = ds.buffer
        buf.add_utterance("消す文")
        ds.pending_waves = lambda: [{"uid": 9.0, "start": 9.0, "end": 10.0, "env": (2, 5)}]
        pump(0.5, until=lambda: w.txt_draft.tail_blocks() and w.txt_draft.final_text() == "消す文")
        ds.pending_waves = lambda: []
        w.btn_draft_discard.click()
        pump(0.4)
        self.assertEqual(w.txt_draft.final_text(), "")
        self.assertEqual(w.txt_draft.tail_blocks(), [])
        self.assertIn("下書きを全部消す", self.confirms)
        self.assertFalse(buf.has_draft)

    def test_editor_read_only_without_session_and_placeholder(self):
        w = self.make()
        self.assertTrue(w.txt_draft.isReadOnly())

    def test_send_checkbox_is_linked_to_settings(self):
        w = self.make()
        self.assertFalse(w.chk_send.isChecked())
        w.chk_send.setChecked(True)
        self.assertTrue(self.rig.settings["dialogue"]["send_after_deliver"])
        self.rig.ctl.set_option("send_after_deliver", False)                 # 設定画面からの変更に追従
        pump(0.5, until=lambda: not w.chk_send.isChecked())
        self.assertFalse(w.chk_send.isChecked())

    def test_not_sent_notice_tells_what_to_press_and_stays(self):
        w = self.make(dialogue={"send_after_deliver": True},
                      results=[{"status": "delivered", "stage": "uia", "sent": False, "send_reason": "busy_timeout"}])
        self.assertTrue(w.chk_send.isChecked())
        w.start_session()
        self.rig.say()
        w.commit("window")
        self.assertTrue(pump(3, until=lambda: "送信できませんでした" in w.notice_text()))
        self.assertIn("Enter／送信ボタンを押してください", w.notice_text())
        buf = self.rig.ctl.ds.buffer
        self.assertTrue(pump(2, until=lambda: len(buf.history) == 1))
        self.assertEqual(buf.history[0].sent, "not_sent")
        self.assertTrue(w.box_und.isHidden())                                # 未配達ではない（再試行で二重入力しない）
        pump(0.5)
        self.assertIn("送信できませんでした", w.notice_text())                # 警告は消えない

    def test_never_delivers_without_user_operation_in_the_one_column_ui(self):
        """絶対条件: 画面の同期・波形・編集のどれも、ユーザーの操作なしに配達しない。"""
        w = self.start()
        ds = self.rig.ctl.ds
        ds.buffer.add_utterance("あ", uid=0.0)
        ds.pending_waves = lambda: [{"uid": 9.0, "start": 9.0, "end": 10.0, "env": (2, 5)}]
        pump(0.8)
        self.assertEqual(self.rig.client.delivered, [])
        self.assertEqual(ds.buffer.history, [])


class MainWindowBase(unittest.TestCase):
    """統合 GUI の MainWindow（AI対話モードは常に有効）。"""

    def setUp(self):
        from history import History
        from services import build_services
        from settings import DEFAULTS
        from test_core_runtime import FakeSD
        from test_gui_smoke import fake_transcribe
        from ui_main import Bridge, MainWindow
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self._d.name)
        self.s = dict(DEFAULTS)
        self.s["dialogue"] = dict(DEFAULTS["dialogue"])
        self.s.update(out_dir=str(d / "out"), live_enabled=False, silence_stop_sec=0)
        self.hist = History(d / "h.db")
        self.bridge = Bridge()
        self.svc = build_services(self.s, on_job_event=lambda j: self.bridge.job_event.emit(j),
                                  on_status=lambda m: self.bridge.status.emit(m),
                                  sd=FakeSD(C.speechlike(8, -12), speed=10.0, loop=True),
                                  transcribe=fake_transcribe, loader=lambda n, t: object(), history=self.hist)
        self.win = MainWindow(self.svc, self.bridge, use_tray=False, volume=0.0, settings_path=d / "settings.json",
                              muted=True)
        self.win._warn = lambda t, m: None
        self.win.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.win.show()
        pump(0.1)

    def tearDown(self):
        self.win.timer.stop()
        self.win._shutdown_dialogue()
        self.svc.session.close()
        self.svc.queue.close()
        self.hist.close()
        self.win.close()
        self._d.cleanup()

class MainWindowWiringTest(MainWindowBase):
    def test_dialogue_is_always_available_and_session_guards_recording(self):
        self.assertNotIn("enabled", self.s["dialogue"])                    # 有効・無効の設定は無い（常に有効）
        self.assertFalse(hasattr(self.win, "open_dialogue"))              # モードの切り替えは無い（画面は AI対話の流れ1つ）
        dw = self.win.dialogue_panel
        self.assertIsNotNone(dw)
        self.assertIs(self.win.dialogue_ctl.ds, None)
        dw.start_session()
        self.assertTrue(self.win._dialogue_active())
        self.assertTrue(self.svc.session.is_recording)
        self.win._on_hotkey("dialogue")                                     # ホットキー経由の確定（空の下書きなら何も届かない）
        dw.end_session()
        self.assertTrue(pump(5, until=lambda: not self.win._dialogue_active()))


class MainIntegrationTest(MainWindowBase):
    """メイン窓への統合（対話パネル・設定画面）。"""

    def test_panel_settings_menu_opens_main_settings(self):
        called = []
        self.win.open_settings = lambda: called.append(1)
        self.win.dialogue_panel.on_settings = self.win.open_settings
        self.win.dialogue_panel.act_settings.trigger()
        self.assertEqual(called, [1])

    def _open_settings_with(self, mutate):
        import ui_main
        from ui_main import SettingsDialog
        d = SettingsDialog(self.win.s, [], self.win)
        mutate(d)
        self.win._exec = lambda dlg: QDialog.Accepted
        orig = ui_main.SettingsDialog
        ui_main.SettingsDialog = lambda *a, **k: d
        try:
            self.win.open_settings()
        finally:
            ui_main.SettingsDialog = orig
        return d

    def test_settings_dialog_has_dialogue_items(self):
        from ui_main import SettingsDialog
        d = SettingsDialog(self.s, [], self.win)
        for name in ("dlg_model", "dlg_headset", "hk_dialogue", "dlg_utt_gap", "dlg_existing", "finalize_on_end", "idle_stop"):
            self.assertTrue(hasattr(d, name), name)
            self.assertTrue(getattr(d, name).isEnabled(), name)               # 常に有効（グレーにしない）
        for name in ("dialogue_on", "dlg_send", "dlg_auto", "dlg_msg_gap", "layout_combo", "idle_timeout", "idle_prompt",
                     "idle_cooldown", "compress_note", "dlg_prov", "live_model", "silence"):
            self.assertFalse(hasattr(d, name), name)                          # 消した項目（無音の自動確定は UI から有効にできない）
        tabs = [d.setting_tabs.tabText(i) for i in range(d.setting_tabs.count())]
        self.assertEqual(tabs, ["共通", "録音と清書", "AI対話"])                   # 「詳細」タブは無い
        self.assertEqual([d.dlg_model.itemData(i) for i in range(3)], ["tiny", "base", "small"])   # カタログから作る
        self.assertEqual(d.dlg_model.currentData(), "small")
        self.assertIn("再生/一時停止ボタンも独占", d.dlg_headset_note.text())
        self.assertNotIn("provisional", d.dialogue_values())
        self.assertNotIn("auto_commit", d.dialogue_values())
        self.assertNotIn("message_gap_sec", d.dialogue_values())
        self.assertNotIn("send_after_deliver", d.dialogue_values())           # 送信までするかは、メイン画面の［送信までする］だけ

    def test_save_replaces_only_dialogue_keys_and_applies(self):
        self.s["dialogue"].update(worker_python="X:/py.exe", last_target="相談窓", send_after_deliver=True)
        self.s["hotkey_dialogue_commit"] = ""
        hk = []
        self.win.apply_hotkeys = lambda: hk.append(1)

        def mutate(d):
            d.dlg_model.setCurrentIndex(d.dlg_model.findData("medium"))
            d.dlg_headset.setChecked(True)
            d.hk_dialogue.setText("Ctrl+Alt+F7")
            d.dlg_utt_gap.setValue(2.0)
            d.dlg_existing.setCurrentIndex(d.dlg_existing.findData("overwrite"))
        self._open_settings_with(mutate)
        dl = self.s["dialogue"]
        self.assertEqual((dl["model"], dl["headset_button"]), ("medium", True))
        self.assertNotIn("provisional", self.win.settings_path.read_text(encoding="utf-8"))   # 廃止した設定は書き戻さない
        self.assertEqual((dl["utterance_gap_sec"], dl["existing_text"]), (2.0, "overwrite"))
        self.assertEqual((dl["worker_python"], dl["last_target"]), ("X:/py.exe", "相談窓"))   # ほかのキーは触らない
        self.assertTrue(dl["send_after_deliver"])                              # 設定ダイアログは送信の設定に触れない
        self.assertEqual(self.s["hotkey_dialogue_commit"], "Ctrl+Alt+F7")
        self.assertTrue(hk)                                                   # ホットキーを登録し直す
        self.assertNotIn("dialogue_opts", self.s)
        self.assertNotIn("dialogue_enabled", self.s)
        saved = self.win.settings_path.read_text(encoding="utf-8")
        self.assertIn("Ctrl+Alt+F7", saved)
        self.assertNotIn("auto_commit", saved)

    def test_headset_setting_applied_immediately_only_during_session(self):
        calls = []
        self.win.dialogue_ctl.apply_headset_setting = lambda: calls.append(1)
        self._open_settings_with(lambda d: d.dlg_headset.setChecked(True))     # セッション外: 次の開始から
        self.assertEqual(calls, [])
        self.assertTrue(self.s["dialogue"]["headset_button"])
        self.s["dialogue"]["headset_button"] = False
        self.win.dialogue_panel.start_session()
        self.assertTrue(self.win._dialogue_active())
        self._open_settings_with(lambda d: d.dlg_headset.setChecked(True))     # セッション中: 今すぐ独占を登録
        self.assertEqual(calls, [1])

    def test_hotkey_change_alone_reregisters(self):
        hk = []
        self.win.apply_hotkeys = lambda: hk.append(1)
        self._open_settings_with(lambda d: d.hk_dialogue.setText("Ctrl+Alt+F6"))
        self.assertEqual(hk, [1])
        hk.clear()
        self._open_settings_with(lambda d: None)                               # 変更なしなら登録し直さない
        self.assertEqual(hk, [])

    def test_hotkey_commit_goes_to_panel(self):
        called = []
        self.win.dialogue_panel.commit_from_hotkey = lambda: called.append(1)
        self.win._on_hotkey("dialogue")
        self.assertEqual(called, [1])


class CursorStabilityTest(unittest.TestCase):
    """認識の更新（認識中の波形→白）で、利用者が白の中に置いたカーソル・選択・スクロールを動かさない。波形の中にカーソルを入れない。"""

    def setUp(self):
        import ui_draft
        self.v = ui_draft.DraftView()
        self.v.resize(380, 300)
        self.v.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.v.show()
        pump(0.05)

    def tearDown(self):
        self.v.deleteLater()

    def tail(self, *items):
        self.v.apply_tail_ops(L.plan_tail(self.v.tail_blocks(), list(items)))

    def place(self, a, p=None):
        from PySide6.QtGui import QTextCursor
        c = self.v.textCursor()
        c.setPosition(a)
        if p is not None:
            c.setPosition(p, QTextCursor.KeepAnchor)
        self.v.setTextCursor(c)

    def cur(self):
        c = self.v.textCursor()
        return c.anchor(), c.position()

    def stream(self):
        """波形（更新を重ねる）→ 次の発話の波形 → 一つずつ消える …… を連続で流す。"""
        W = L.K_WAVE
        yield [L.TailItem(5.0, W, (1, 2, 3))]
        yield [L.TailItem(5.0, W, (1, 2, 3, 4))]
        yield [L.TailItem(5.0, W, (1, 2, 3, 4)), L.TailItem(9.0, W, (2, 5))]
        yield [L.TailItem(9.0, W, (2, 5, 6))]
        yield []

    def test_cursor_and_selection_in_white_are_never_moved_by_tail_updates(self):
        self.v.set_final_text("こんにちは。\nお元気ですか。")
        for a, p in ((3, 3), (2, 7), (0, 0), (13, 13)):
            self.place(a, p)
            keep = self.cur()
            for items in self.stream():
                self.tail(*items)
                self.assertEqual(self.cur(), keep, (a, p, items))
                self.assertNotEqual(L._kind if False else self.v.document().findBlock(self.v.textCursor().position()).blockFormat()
                                    .property(0x100001), L.K_WAVE)

    def test_cursor_at_end_of_white_stays_put_when_wave_is_inserted(self):
        """白の末尾にカーソルがあるとき、新しい波形ブロックの中へ飛ばない（原因: 挿入位置のカーソルは挿入のあとへ進む）。"""
        self.v.set_final_text("おわり")
        end = self.v.final_end()
        self.place(end)
        self.tail(L.TailItem(7.0, L.K_WAVE, (1, 3, 5)))
        self.assertEqual(self.cur(), (end, end))
        self.tail(L.TailItem(7.0, L.K_WAVE, (1, 3, 5, 8)))
        self.assertEqual(self.cur(), (end, end))
        self.tail()
        self.assertEqual(self.cur(), (end, end))

    def test_empty_white_with_tail_keeps_cursor_at_start_and_typing_goes_to_white(self):
        from PySide6.QtTest import QTest
        self.assertEqual(self.v.final_text(), "")
        self.place(0)
        self.tail(L.TailItem(7.0, L.K_WAVE, (1, 3, 5)))
        self.assertEqual(self.cur(), (0, 0))
        self.v.setFocus()
        QTest.keyClicks(self.v, "ab")
        self.assertEqual(self.v.final_text(), "ab")
        self.assertEqual([b.kind for b in self.v.tail_blocks()], [L.K_WAVE])

    def test_white_append_follows_only_a_cursor_at_the_white_end(self):
        self.v.set_final_text("あい")
        self.tail(L.TailItem(7.0, L.K_WAVE, (1, 3, 5)))
        self.place(1)
        self.v.append_final("うえ")
        self.assertEqual(self.cur(), (1, 1))                                 # 途中: 動かさない
        self.place(self.v.final_end())
        self.v.append_final("お")
        self.assertEqual(self.cur(), (self.v.final_end(), self.v.final_end()))   # 末尾: 追記のあとへ（打ち続けられる）
        self.assertEqual(self.v.final_text(), "あいうえお")
        self.assertEqual([b.kind for b in self.v.tail_blocks()], [L.K_WAVE])

    def test_cursor_cannot_stay_inside_a_wave_block(self):
        from PySide6.QtTest import QTest
        self.v.set_final_text("あい")
        self.tail(L.TailItem(7.0, L.K_WAVE, (1, 3, 5)))
        wave = self.v.document().lastBlock()
        self.place(wave.position() + 1)                                      # クリックなどで波形の中へ
        self.assertEqual(self.cur(), (self.v.final_end(),) * 2)              # 白の末尾へ戻る
        self.v.setFocus()
        QTest.keyClick(self.v, Qt.Key_Down)                                  # キー操作でも居残らない
        QTest.keyClick(self.v, Qt.Key_End, Qt.ControlModifier)
        pos = self.v.textCursor().position()
        self.assertLessEqual(pos, self.v.final_end())
        self.assertEqual(self.v.final_text(), "あい")

    def test_cursor_between_two_wave_blocks_goes_back_to_end_of_white(self):
        self.v.set_final_text("あ")
        self.tail(L.TailItem(5.0, L.K_WAVE, (1, 2)), L.TailItem(9.0, L.K_WAVE, (1, 2)))
        wave = self.v.document().lastBlock()
        self.place(wave.position())
        self.assertEqual(self.cur(), (self.v.final_end(),) * 2)

    def test_scroll_position_is_kept_when_scrolled_up(self):
        self.v.set_final_text("\n".join(f"行{i}" for i in range(60)))
        pump(0.1)
        sb = self.v.verticalScrollBar()
        sb.setValue(0)
        self.place(5)
        for items in self.stream():
            self.tail(*items)
            self.assertEqual(sb.value(), 0)
            self.assertEqual(self.cur(), (5, 5))


if __name__ == "__main__":
    unittest.main()
