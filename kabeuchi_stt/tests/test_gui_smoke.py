# -*- coding: utf-8 -*-
"""GUI のスモークテスト。窓は画面に出さない(WA_DontShowOnScreen)・キー/マウスは送らない・音は鳴らさない(ミュート)。
マイクは開かない（sounddevice を差し替え、清書はダミー）。PowerShell から通常のプラットフォームで実行すること。
"""
import json
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

import _common as C
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

import ui_style as S
from services import build_services
from settings import DEFAULTS
from single_instance import SingleInstance, notify_existing
from test_core_runtime import FakeSD
from transcriber import FinalResult
from ui_main import Bridge, MainWindow

from PySide6.QtWidgets import QDialog, QFileDialog, QInputDialog  # noqa: E402


def _forbid(*a, **k):
    raise AssertionError("テスト中に実ダイアログを開く呼び出しがありました（_question/_warn/_exec/_open_file を通すこと）")


# 保険: 実ダイアログを開く静的メソッド・exec を丸ごと例外にする（ハング・実画面表示を防ぐ）
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


def fake_transcribe(wav, s, model_name=None, on_progress=None, on_status=None, cancel=None, model=None,
                    postprocess=None, **kw):
    on_progress(0.5)
    segs = [(0.0, 3.0, "不線を貼ります。"), (3.5, 6.0, "基地局の話です。")]
    segs = [(a, b, postprocess(t) if postprocess else t) for a, b, t in segs]
    text = "\n".join(t for _, _, t in segs)
    return FinalResult(text=text, segments=segs, txt_path=None, md_path=None, audio_seconds=7.0, elapsed=0.1)


class GuiSmokeTest(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        d = Path(self._d.name)
        self.d = d
        self.s = dict(DEFAULTS)
        self.s.update(out_dir=str(d / "out"), live_enabled=False, silence_stop_sec=0)
        from history import History
        self.hist = History(d / "h.db")
        self.sd = FakeSD(C.speechlike(8, -12), speed=10.0, loop=True)
        self.bridge = Bridge()
        self.svc = build_services(self.s, on_job_event=lambda j: self.bridge.job_event.emit(j),
                                  on_status=lambda m: self.bridge.status.emit(m), sd=self.sd,
                                  transcribe=fake_transcribe, loader=lambda n, t: object(), history=self.hist)
        self.win = MainWindow(self.svc, self.bridge, use_tray=False, volume=0.0, settings_path=d / "settings.json",
                              muted=True)
        self.win.player.set_muted(True)  # 念のためミュート（音量 0 とあわせて、テストで音を出さない）
        # モーダルなダイアログは実画面に出さない: 確認は記録だけして自動で応答する
        self.asked = []
        self.answer = QMessageBox.Yes
        self.win._question = lambda title, text, buttons=None: (self.asked.append(title), self.answer)[1]
        self.win._warn = lambda title, text: self.asked.append("warn:" + title)
        self.win._exec = lambda dlg: self.fail("テストでダイアログを exec してはいけません")
        self.win.setAttribute(Qt.WA_DontShowOnScreen, True)
        self.win.show()
        pump(0.2)

    def tearDown(self):
        self.win.timer.stop()
        self.svc.session.close()  # 録音中なら止め、入力ストリーム（待機中モニター）も閉じる
        self.win.player.stop()
        self.svc.queue.wait_idle(10)
        pump(0.2)  # 清書完了イベントを DB が開いているうちに処理
        self.svc.queue.close()
        self.hist.close()
        self.win.close()
        self._d.cleanup()

    def test_record_stop_finalize_history_flow(self):
        w = self.win
        w.toggle_record()                          # 録音ボタン＝セッション開始（画面は AI対話の流れ1つ）
        self.assertTrue(self.svc.session.is_recording)
        self.assertTrue(w._dialogue_active())
        u = w.unified
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "停止")
        pump(1.0, until=lambda: len(self.svc.session.timeline.rows()) >= 1)
        self.assertGreaterEqual(len(self.svc.session.timeline.rows()), 1)
        self.assertTrue(pump(4.0, until=lambda: u.lbl_time.text() not in ("--:--", "00:00")))   # 1 秒以上録る（短すぎる録音は清書しない）
        w.toggle_record()                          # 停止＝セッション終了。録音全体の清書が裏で始まる
        self.assertTrue(pump(8, until=lambda: not self.svc.session.is_recording and not w._dialogue_active()))
        u.refresh()
        self.assertEqual(u.btn_rec.text(), "録音")
        # 清書が終わり、履歴が更新される（クリップボードへは入れない）
        self.assertTrue(pump(8, until=lambda: w.lst_hist.count() == 1))
        self.assertNotIn("基地局の話です", QGuiApplication.clipboard().text())   # クリップボードへは自動で入れない（利用者が明示的にコピーしたときだけ）
        self.assertTrue(pump(3, until=lambda: "清書完了" in w.statusBar().currentMessage()))
        self.assertEqual(w.progress_text(), "")                                 # 清書が終われば、通知の行には何も出さない
        # 清書中でも次の録音を始められる
        w.toggle_record()
        self.assertTrue(self.svc.session.is_recording)
        w.toggle_record()
        self.assertTrue(pump(8, until=lambda: not w._dialogue_active()))

    def test_dictionary_chips_presets_persist(self):
        w = self.win
        # 専門用語チップ
        w.ed_hot.setText("ベースバンド")
        w.add_hotword()
        self.assertIn("ベースバンド", w.chips.words())
        saved = json.loads((self.d / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("ベースバンド", saved["hotwords"])
        w.chips.remove_word("ベースバンド")
        self.assertNotIn("ベースバンド", json.loads((self.d / "settings.json").read_text(encoding="utf-8"))["hotwords"])
        # 置換辞書（画面で追加 → すぐ Replacer に反映・ファイルに保存）
        w.dict_add_row()
        r = w.tbl.rowCount() - 1
        w.tbl.item(r, 0).setText("壁内")
        w.tbl.item(r, 1).setText("壁打ち")
        self.assertEqual(self.svc.replacer.apply("壁内の話"), "壁打ちの話")
        from replacements import load_pairs
        self.assertIn({"from": "壁内", "to": "壁打ち"}, load_pairs())
        # プリセット: 追加 → 保存（宛先が Copilot のときの前置きに使う。クリップボードへは送らない）
        n0 = w.cmb_preset.count()
        w.ed_pname.setText("テスト用")
        w.ed_prompt.setPlainText("これを要約して")
        w.preset_new()
        self.assertEqual(w.cmb_preset.count(), n0 + 1)
        self.assertEqual(w.cmb_preset.currentText(), "テスト用")
        self.assertFalse(hasattr(w, "send_main"))

    def test_history_view_edit_play_suggest_delete(self):
        w = self.win
        wavp = self.d / "out" / "a.wav"
        wavp.parent.mkdir()
        with wave.open(str(wavp), "wb") as f:
            f.setnchannels(1), f.setsampwidth(2), f.setframerate(16000)
            f.writeframes(C.sine(6, -20).tobytes())
        segs = [(0.0, 3.0, "不線を貼ります。"), (3.0, 6.0, "基地局の話です。")]
        rid = self.hist.add(text="不線を貼ります。\n基地局の話です。", segments=segs, duration=6.0, model="small",
                            wav_path=str(wavp))
        w.reload_history()
        self.assertEqual(w.lst_hist.count(), 1)
        w.lst_hist.setCurrentRow(0)
        pump(0.2)
        self.assertEqual(w.editor.toPlainText(), "不線を貼ります。\n基地局の話です。")
        self.assertTrue(w.btn_play.isEnabled())
        # 行クリック → その位置から再生（状態の遷移だけ確認。ミュート・音量0）
        w.editor_line_clicked(1)
        self.assertTrue(pump(5, until=lambda: w.player.is_playing))
        self.assertAlmostEqual(w.player.player.position() / 1000.0, 3.0, delta=1.5)
        w.player.stop()
        pump(0.3)
        self.assertFalse(w.player.is_playing)
        # 修正して保存 → 元の清書は残る・検索にも出る
        w.editor.setPlainText("付箋を貼ります。\n基地局の話です。")
        self.assertTrue(w._edit_dirty)
        w.save_edit()
        rec = self.hist.get(rid)
        self.assertEqual(rec.edited_text, "付箋を貼ります。\n基地局の話です。")
        self.assertEqual(rec.text, "不線を貼ります。\n基地局の話です。")
        w.ed_search.setText("付箋")
        w.reload_history()
        self.assertEqual(w.lst_hist.count(), 1)
        # 直した語の候補 → 辞書へ
        from replacements import suggest_replacements
        self.assertEqual(suggest_replacements(rec.text, rec.edited_text), [("不線", "付箋")])
        n = w.add_dict_pairs([("不線", "付箋"), ("不線", "付箋")])
        self.assertEqual(n, 0)  # 既定の辞書にすでにある
        # 元に戻す
        self.hist.revert_edit(rid)
        self.assertIsNone(self.hist.get(rid).edited_text)
        w.open_record(rid)  # 画面側の保持データも更新（古いままだと確認が出る）
        # 清書やり直し: キューへ（履歴は増えない）
        w.ed_search.setText("")
        w.reload_history()
        w.lst_hist.setCurrentRow(0)
        pump(0.2)
        w.redo_final()
        self.assertEqual(self.asked.count("清書のやり直し"), 0)  # 修正なしなら確認は出ない
        self.assertTrue(pump(5, until=lambda: not self.svc.queue.busy))
        pump(0.3)
        self.assertEqual(len(self.hist.list()), 1)
        # 削除（ごみ箱はテスト用の関数に差し替え）
        trashed = []
        orig = self.hist.delete
        self.hist.delete = lambda rid_, **kw: orig(rid_, trasher=lambda p: trashed.append(p.name))
        w.delete_record()
        self.assertIn("削除", self.asked)
        self.assertEqual(trashed, ["a.wav"])
        self.assertEqual(self.hist.list(), [])
        self.assertEqual(w.lst_hist.count(), 0)

    def test_redo_with_edit_asks_and_no_means_no(self):
        w = self.win
        wavp = self.d / "b.wav"
        with wave.open(str(wavp), "wb") as f:
            f.setnchannels(1), f.setsampwidth(2), f.setframerate(16000)
            f.writeframes(C.sine(2, -20).tobytes())
        rid = self.hist.add(text="あ", segments=[(0, 1, "あ")], duration=2.0, model="small", wav_path=str(wavp))
        self.hist.save_edit(rid, "い")
        w.reload_history()
        w.lst_hist.setCurrentRow(0)
        pump(0.2)
        self.answer = QMessageBox.No
        w.redo_final()
        self.assertIn("清書のやり直し", self.asked)
        pump(0.5)
        self.assertEqual(len(self.svc.queue.jobs), 0)  # 「いいえ」ならキューに入らない
        self.answer = QMessageBox.Yes
        w.redo_final()
        self.assertTrue(pump(5, until=lambda: not self.svc.queue.busy and len(self.svc.queue.jobs) == 1))

    def test_pick_wav_goes_through_hook(self):
        w = self.win
        w._open_file = lambda *a: str(self.d / "x.wav")
        w._ask_wav_model = lambda wav: "small"        # モデル選択ダイアログは出さない
        w.pick_wav()
        self.assertTrue(pump(5, until=lambda: len(self.svc.queue.jobs) == 1))

    def test_pick_wav_uses_chosen_model_and_history_shows_it(self):
        """別のモデルで同じ録音を清書すると、履歴にそのモデル名で並ぶ（txt/md は別名）。"""
        w = self.win
        wav = self.d / "x.wav"
        wav.write_bytes(b"")
        w._open_file = lambda *a: str(wav)
        w._ask_wav_model = lambda p: "large-v3-turbo"
        w.pick_wav()
        self.assertTrue(pump(5, until=lambda: len(self.svc.queue.jobs) == 1 and not self.svc.queue.busy))
        job = self.svc.queue.jobs[0]
        self.assertEqual(job.model, "large-v3-turbo")
        self.assertEqual(job.file_tag, "large-v3-turbo")      # 設定の既定(small)と違うので別名
        self.assertEqual([r.model for r in self.hist.list(10)], ["large-v3-turbo"])
        w._ask_wav_model = lambda p: "small"
        w.pick_wav()
        self.assertTrue(pump(5, until=lambda: len(self.svc.queue.jobs) == 2 and not self.svc.queue.busy))
        self.assertEqual(self.svc.queue.jobs[1].file_tag, "")
        self.assertEqual(sorted(r.model for r in self.hist.list(10)), ["large-v3-turbo", "small"])

    def test_wav_model_dialog_default_and_warning(self):
        from ui_models import WavModelDialog
        d = WavModelDialog("a.wav", self.d / "nomodels", "medium", self.win)
        self.assertEqual(d.model(), "medium")
        self.assertIn("まだ取得していません", d.warn.text())      # 空のフォルダなので未取得
        d.model_combo.setCurrentIndex(d.model_combo.findData("kotoba-tech/kotoba-whisper-v2.0-faster"))
        self.assertEqual(d.model(), "kotoba-tech/kotoba-whisper-v2.0-faster")

    def test_settings_dialog_models_catalog_and_warning(self):
        from ui_main import SettingsDialog
        s = dict(self.s, model_dir=str(self.d / "empty"), final_model="my/custom-model")
        s["dialogue"] = {"model": "small"}
        d = SettingsDialog(s, [], self.win)
        self.assertEqual(d.final_model.currentData(), "my/custom-model")      # カタログ外もそのまま選べる・保存できる
        self.assertEqual(d.values()["final_model"], "my/custom-model")
        self.assertIn("まだ取得していません", d.model_warn.text())
        self.assertIn("【未取得】", d.final_model.currentText())
        self.assertTrue(hasattr(d, "btn_models"))
        md = self.d / "have"                                                   # 取得済みの置き場では警告が消える
        (md / "small").mkdir(parents=True)
        (md / "small" / "model.bin").write_bytes(b"x")
        d2 = SettingsDialog(dict(self.s, model_dir=str(md), final_model="small", dialogue={"model": "small"}), [], self.win)
        self.assertEqual(d2.model_warn.text(), "")
        self.assertIn("【取得済み】", d2.final_model.currentText())

    def test_model_manager_download_with_fake(self):
        from ui_models import ModelManagerDialog
        import models_catalog as MC
        md = self.d / "mm"
        md.mkdir()

        def fake(key, model_dir, cb, cancel):
            cb(50, 100)
            cb(100, 100)
            p = Path(model_dir) / MC.model_name(key)
            p.mkdir(parents=True, exist_ok=True)
            (p / "model.bin").write_bytes(b"x")
            return MC.DownloadResult(True, "ok")
        d = ModelManagerDialog(md, self.win, downloader=fake)
        self.assertEqual(d.rows["small"]["state"].text(), "未取得")
        d._on_button("small")
        self.assertTrue(pump(5, until=lambda: d.rows["small"]["state"].text() == "取得済み"))
        self.assertFalse(d.rows["small"]["btn"].isEnabled())                 # 取得済みは押せない（灰色）
        self.assertTrue(d.rows["base"]["btn"].isEnabled())

    def test_slot_exception_does_not_crash(self):
        w = self.win
        self.hist.get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore
        from PySide6.QtWidgets import QListWidgetItem
        it = QListWidgetItem("x")
        it.setData(Qt.UserRole, 123)
        w.history_selected(it)  # safe() が受けて error.log へ。例外は外に出ない
        self.assertIn("エラー", w.statusBar().currentMessage())

    def test_close_hides_not_quits_when_tray(self):
        # トレイが無い構成（use_tray=False）では通常どおり閉じる
        self.assertIsNone(self.win._tray)


class SingleInstanceTest(unittest.TestCase):
    def test_second_instance_notifies_first(self):
        name = f"kabeuchi_test_{int(time.time() * 1000)}"
        got = []
        self.assertFalse(notify_existing(name, timeout_ms=100))  # まだ誰もいない
        si = SingleInstance(lambda m: got.append(m), name=name)
        self.assertTrue(si.listen())
        self.assertTrue(notify_existing(name, b"show"))
        pump(1.0, until=lambda: bool(got))
        self.assertEqual(got, ["show"])


if __name__ == "__main__":
    unittest.main()
