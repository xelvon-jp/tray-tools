# -*- coding: utf-8 -*-
"""メイン窓（PySide6）。画面の組み立てと操作。ロジックは各コアモジュールに任せる。

スレッドの扱い: 清書キュー・ホットキーのコールバックは別スレッドから来るので、
Bridge の Signal 経由でメインスレッドへ渡す。全スロットは safe() で包み、例外で落とさない。
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QMenu, QPlainTextEdit, QPushButton,
                               QSlider, QSpinBox, QSystemTrayIcon,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

import audio_compress as AC
import models_catalog as MC
import ui_icons as I
import ui_style as S
import ui_unified_logic as U
from finalize_queue import CANCELLED, DONE, ERROR, PENDING, RUNNING, Job
from geometry_util import clamp_geometry, parse_geometry
from history import History, Record, snippet
from meter_state import HoldLatch, meter_unrecorded
from mic_check import Issue
from playback_devices import combo_entries
from presets import load_presets, save_presets
from replacements import save_pairs, suggest_replacements
from services import Services
from settings import dialogue_config, resolve_dir, save_settings
from ui_models import ModelManagerDialog, WavModelDialog, combo_value, fill_model_combo
from ui_menu import COPIED_MESSAGE, MenuItem, SEP, history_menu, is_text_row
from ui_widgets import AudioPlayer, CheckBox, ChipsWidget, ClickableText, ElidedLabel, MeterWidget, WrapBarLayout
from version import APP_NAME, VERSION
from viewer_core import block_for_time, seek_time_for_block, viewer_segments

log = logging.getLogger("kabeuchi")

MIN_FINALIZE_BYTES = 16000 * 2 * 1        # これ（約1秒ぶんの 16kHz 16bit モノラル）より小さい録音は、録音全体の清書を見送る
IDLE_MONITOR_TIMEOUT_SEC = 300  # 操作が無いままこの秒数たったら、待機中のマイク確認を止める（設定画面には出さない固定値）


def safe(fn):
    """スロットを包む。例外は error.log へ（PySide6 は未処理例外でプロセスごと落ちるため）。"""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception:
            log.exception("スロット %s で例外", fn.__name__)
            try:
                self.statusBar().showMessage(f"エラーが起きました（error.log を見てください）: {fn.__name__}", 8000)
            except Exception:
                pass
    return wrapper


def fmt_hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def fmt_dur(sec: float) -> str:
    s = int(round(sec))
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Bridge(QObject):
    """別スレッド → メインスレッドの橋渡し。"""
    job_event = Signal(object)
    status = Signal(str)
    hotkey = Signal(str)


def wav_seconds(path) -> float:
    """WAV の長さ（秒）。読めなければ 0。"""
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 16000)
    except Exception:
        return 0.0


def _pill(bg: str, fg: str, bold: bool = True) -> str:
    return (f"background: {bg}; color: {fg}; border-radius: 15px; padding: 3px 14px;"
            + (" font-weight: bold;" if bold else ""))


class SettingsDialog(QDialog):
    """設定ダイアログ。共通／録音と清書／AI対話の3タブ。"""

    def __init__(self, settings: dict, devices: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(560)
        self.setting_tabs = QTabWidget()

        def _page(title: str, desc: str):
            pg = QWidget()
            lay = QVBoxLayout(pg)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet(f"color: {S.MUTED}; font-size: 9pt;")
            lay.addWidget(d)
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignRight)
            lay.addLayout(form)
            lay.addStretch(1)
            self.setting_tabs.addTab(pg, title)
            return form

        f_common = _page("共通", "共通: 画面・マイク・認識モデルの設定")
        f_wall = _page("録音と清書", "録音と清書: セッションを終えたあとの、録音全体の清書と履歴・ホットキーの設定")
        f = _page("AI対話", "AI対話: 話しながら、AIアプリの入力欄へ届ける使い方の設定")
        self.size_combo = QComboBox()
        for key, label in S.SIZE_LABELS.items():
            self.size_combo.addItem(label, key)
        cur_size = str(settings.get("display_size", S.DEFAULT_SIZE))
        self.size_combo.setCurrentIndex(max(0, self.size_combo.findData(cur_size)))
        self.size_combo.setToolTip("文字とボタンの大きさ。保存するとすぐ変わります")
        self.model_dir = resolve_dir(settings.get("model_dir", "models"))
        self.final_model = QComboBox()
        fill_model_combo(self.final_model, self.model_dir, str(settings.get("final_model", "small")), "final")
        self.final_model.setToolTip("セッションを終えたあと、録音全体を文字にして履歴に保存するときの認識モデル（WAV を選んで清書するときの既定も"
                                    "これです）。大きいほど正確ですが、時間がかかります")
        self.finalize_on_end = QComboBox()
        self.finalize_on_end.addItem("する（右の清書モデルで、録音全体を丁寧に認識し直す）", True)
        self.finalize_on_end.addItem("しない（発話ごとの結果をそのまま履歴に保存）", False)
        self.finalize_on_end.setCurrentIndex(0 if bool(settings.get("finalize_on_end", True)) else 1)
        self.finalize_on_end.setToolTip("する: 録音を止めたあと、録音全体を「録音全体の清書モデル」で認識し直して履歴に保存します（次の録音が始まるまで待ち、"
                                        "録音中は始めません）。しない: 再認識せず、喋っている間に出た発話ごとの結果を、そのまま履歴の1件にします（待ち時間なし）")
        self.device = QComboBox()
        self.device.addItem("既定のマイク", None)
        cur = settings.get("input_device")
        for d in devices:
            self.device.addItem(f"{d['index']}: {d['name']} [{d['hostapi']}]", d["index"])
            if cur is not None and (cur == d["index"] or str(cur).lower() in d["name"].lower()):
                self.device.setCurrentIndex(self.device.count() - 1)
        self.idle_stop = QSpinBox()
        self.idle_stop.setRange(0, 7200)
        self.idle_stop.setSuffix(" 秒（0=無効）")
        self.idle_stop.setValue(int(dialogue_config(settings).get("idle_stop_sec", 180)))
        self.idle_stop.setToolTip("喋りが入らないまま、この秒数がたつとセッションを自動で終えます（次のセッションから）")
        self.hk_rec = QLineEdit(str(settings.get("hotkey_record", "")))
        self.hk_rec.setPlaceholderText("例 Ctrl+Alt+F9（空=未割り当て）")
        self.hk_pause = QLineEdit(str(settings.get("hotkey_pause", "")))
        self.hk_pause.setPlaceholderText("例 Ctrl+Alt+F8（空=未割り当て）")
        self.hk_show = QLineEdit(str(settings.get("hotkey_show", "")))
        self.hk_show.setPlaceholderText("例 Ctrl+Alt+F10（空=未割り当て）")
        dcfg = dialogue_config(settings)
        self.dlg_model = QComboBox()
        # カタログ外のモデル名（設定ファイルに手で書いたもの）も、末尾に足してそのまま残す
        fill_model_combo(self.dlg_model, self.model_dir, str(dcfg.get("model") or "small"), "dialogue")
        self.dlg_model.setToolTip("AI対話で、話を区切るたびに文字にする認識モデル。大きいほど正確ですが、下書きに出るまで時間がかかります"
                                  "（効くのは次のセッションから）")
        self.dlg_headset = CheckBox("ヘッドセットのボタンで確定する")
        self.dlg_headset.setChecked(bool(dcfg["headset_button"]))
        self.dlg_headset_note = QLabel("セッション中だけ、ヘッドセットのボタンを USB から直接読み取り（読み取り専用）、"
                                       "再生/一時停止ボタンも独占します。独占できないときは動画・音楽の再生/一時停止も"
                                       "同時に反応する場合があります。独占中は動画・音楽の再生操作が効かなくなります。"
                                       "検出方式は settings.json の dialogue.headset_mode（auto/hid/hotkey）")
        self.dlg_headset_note.setWordWrap(True)
        self.dlg_headset_note.setStyleSheet(f"color: {S.MUTED};")
        self.dlg_mouse_label = QLabel("マウスで確定（次回から）")
        self.dlg_mouse = QComboBox()
        for label, key in (("なし", ""), ("戻るボタン（サイドボタン）", "back"), ("進むボタン（サイドボタン）", "forward")):
            self.dlg_mouse.addItem(label, key)
        self.dlg_mouse.setCurrentIndex(max(0, self.dlg_mouse.findData(str(dcfg.get("mouse_button") or ""))))
        self.dlg_mouse.setToolTip("（効くのは次のセッションから）セッション中だけ、マウスのサイドボタンを押したら確定します。そのボタンは他のアプリへ"
                                  "渡しません（ブラウザの戻る／進むも効きません）。効くのは次のセッションから。"
                                  "会社の PC ではセキュリティソフトに警告されることがあります")
        self.dlg_click = CheckBox("舌打ち2回で確定（実験・次回から）")
        self.dlg_click.setChecked(bool(dcfg.get("click_commit")))
        self.dlg_click.setToolTip("（効くのは次のセッションから・実験機能）セッション中だけ、マイクの音で「舌打ちを2回続けて」（0.2〜0.7秒間隔）鳴らしたことを"
                                  "検出して、確定して届けます。前後0.3秒が静かなときだけ反応するので、静かな場所向けです。1回だけ・3回以上・"
                                  "会話の中の音では動きません（誤動作や取りこぼしが起こりえます）。追加ソフト・特別な権限は要りません")
        self.hk_dialogue = QLineEdit(str(settings.get("hotkey_dialogue_commit", "")))
        self.hk_dialogue.setPlaceholderText("例 Ctrl+Alt+F7（空=未割り当て）")
        self.dlg_nl = CheckBox("句点で改行する（文中の「。」「？」「！」の直後に改行。話を区切った所は空行）")
        self.dlg_nl.setChecked(bool(dcfg["newline_after_sentence"]))
        self.dlg_utt_gap = QDoubleSpinBox()
        self.dlg_utt_gap.setRange(0.3, 10.0)
        self.dlg_utt_gap.setSingleStep(0.1)
        self.dlg_utt_gap.setDecimals(1)
        self.dlg_utt_gap.setSuffix(" 秒の無音で区切る")
        self.dlg_utt_gap.setValue(float(dcfg["utterance_gap_sec"]))
        self.dlg_existing = QComboBox()
        for key, label in (("append", "追記（書きかけの後ろへ足す）"), ("overwrite", "上書き"), ("abort", "中止（未配達にする）")):
            self.dlg_existing.addItem(label, key)
        self.dlg_existing.setCurrentIndex(max(0, self.dlg_existing.findData(str(dcfg["existing_text"]))))
        self.compress = QComboBox()
        for key in AC.MODES:
            self.compress.addItem(AC.MODE_LABELS[key], key)
        self.compress.setCurrentIndex(max(0, self.compress.findData(AC.normalize_mode(settings.get("audio_compress")))))
        self.compress_kbps = QSpinBox()
        self.compress_kbps.setRange(6, 256)
        self.compress_kbps.setSuffix(" kbps（モノラル）")
        self.compress_kbps.setValue(AC.kbps_of(settings))
        self.compress.currentIndexChanged.connect(lambda _i: self._compress_changed())
        # 待機中のマイク確認（窓が見えている間だけ。保存・認識はしない）
        self.idle_monitor = CheckBox("待機中もマイクを確認する（窓が見えている間だけ。保存・認識はしない）")
        self.idle_monitor.setChecked(bool(settings.get("idle_monitor", True)))
        self.idle_monitor.setToolTip("録音していないときも、音量メーターとマイクの状態の表示を動かします。録音も文字起こしもしません")
        # 清書の改行
        self.para_on = CheckBox("清書の出力をすき間の長さで改行する（txt / md と、AI対話の下書き）")
        self.para_on.setChecked(bool(settings.get("paragraph_enabled", True)))
        self.para_gap = QDoubleSpinBox()
        self.para_gap.setRange(0.5, 30.0)
        self.para_gap.setSingleStep(0.5)
        self.para_gap.setDecimals(1)
        self.para_gap.setSuffix(" 秒以上（句点・？・！のあとだけ改行）")
        self.para_gap.setValue(float(settings.get("paragraph_gap_sec", 2.0)))
        self.para_force = QDoubleSpinBox()
        self.para_force.setRange(0.5, 60.0)
        self.para_force.setSingleStep(0.5)
        self.para_force.setDecimals(1)
        self.para_force.setSuffix(" 秒以上（必ず改行。句点がなければ「。」を補う）")
        self.para_force.setValue(float(settings.get("paragraph_force_gap_sec", 3.0)))
        note = QLabel("ホットキーは Claude アプリなど常用アプリのショートカットと衝突しない組み合わせにしてください。"
                      "Ctrl / Alt / Shift / Win のどれかを含める必要があります。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        f_common.addRow("表示サイズ", self.size_combo)
        f_common.addRow("マイク", self.device)
        f_common.addRow("", self.idle_monitor)
        f_common.addRow("録音の圧縮（清書のあと）", self.compress)
        f_common.addRow("圧縮の音質", self.compress_kbps)
        self.btn_models = QPushButton("モデルの取得・管理…")
        self.btn_models.setToolTip("認識モデルを取得（ダウンロード）します。押したときだけ通信します")
        self.btn_models.clicked.connect(self.open_model_manager)
        f_common.addRow("認識モデル", self.btn_models)
        f_wall.addRow("録音全体の清書", self.finalize_on_end)
        f_wall.addRow("録音全体の清書モデル", self.final_model)
        self.final_model.setEnabled(bool(self.finalize_on_end.currentData()))
        self.finalize_on_end.currentIndexChanged.connect(
            lambda _i: self.final_model.setEnabled(bool(self.finalize_on_end.currentData())))
        f_wall.addRow("無音で自動終了", self.idle_stop)
        f_wall.addRow("", self.para_on)
        f_wall.addRow("改行（句点のあと）", self.para_gap)
        f_wall.addRow("改行（必ず）", self.para_force)
        f_wall.addRow("ホットキー: 録音の開始/停止", self.hk_rec)
        f_wall.addRow("ホットキー: 保留/再開", self.hk_pause)
        f_wall.addRow("ホットキー: 窓の表示/非表示", self.hk_show)
        f_wall.addRow(note)
        f.addRow("AI対話の認識モデル", self.dlg_model)
        f.addRow("", self.dlg_headset)
        f.addRow("", self.dlg_headset_note)
        hk_row = QHBoxLayout()           # 高さを増やさないよう、確定ホットキーと同じ行に置く
        hk_row.setContentsMargins(0, 0, 0, 0)
        hk_row.addWidget(self.hk_dialogue, 1)
        hk_row.addWidget(self.dlg_mouse_label)
        hk_row.addWidget(self.dlg_mouse)
        hk_row.addWidget(self.dlg_click)
        f.addRow("ホットキー: 確定", hk_row)
        f.addRow("", self.dlg_nl)
        f.addRow("発話の区切り", self.dlg_utt_gap)
        self.dlg_force = CheckBox("読み戻しが不一致でも強制的に届ける（応急）")
        self.dlg_force.setChecked(bool(dcfg.get("force_on_mismatch")))
        self.dlg_force.setToolTip("入力後の読み戻しが一致しなくても、入力欄に文字があれば「届いた」として扱い、［送信までする］がONなら送信まで進める。"
                                  "Copilot などで、正しく入っているのに「読み戻しが一致しません」と出るときの応急策。入力欄が空・読めないときは対象外。"
                                  "本当に崩れて入っていても送ってしまう恐れがあります。")
        f.addRow("", self.dlg_force)
        f.addRow("入力欄に書きかけ", self.dlg_existing)
        dnote = QLabel("区切り・認識モデルの設定は、次のセッションから効きます。確定ホットキーは保存すると登録し直します。"
                       "確定は必ず操作（ボタン・ヘッドセット・ショートカット）で行います。無音で自動確定する機能はありません。"
                       "送信まで行うかどうかは、メイン画面の宛先の右の［送信までする］で選びます。")
        dnote.setWordWrap(True)
        dnote.setStyleSheet(f"color: {S.MUTED};")
        f.addRow(dnote)
        self._compress_changed()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("キャンセル")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(self.setting_tabs)
        self.model_warn = QLabel("")
        self.model_warn.setWordWrap(True)
        self.model_warn.setStyleSheet("color: #B45309;")
        root.addWidget(self.model_warn)
        root.addWidget(bb)
        self.final_model.currentIndexChanged.connect(lambda _i: self._update_model_warn())
        self.dlg_model.currentIndexChanged.connect(lambda _i: self._update_model_warn())
        self._update_model_warn()

    def missing_models(self) -> list[str]:
        """選んでいる録音全体の清書モデル・AI対話モデルのうち、まだ取得していないもの（保存は止めない）。"""
        out = []
        for cb in (self.final_model, self.dlg_model):
            m = combo_value(cb, "small")
            if not MC.is_downloaded(m, self.model_dir) and m not in out:
                out.append(m)
        return out

    def _update_model_warn(self) -> None:
        miss = self.missing_models()
        self.model_warn.setText(
            f"このモデルはまだ取得していません（{', '.join(miss)}）。［モデルの取得・管理…］で取得してください" if miss else "")

    def open_model_manager(self) -> None:
        dlg = ModelManagerDialog(self.model_dir, self)
        dlg.exec()
        for cb in (self.final_model, self.dlg_model):    # 取得状況の表示を更新（選択は保つ）
            fill_model_combo(cb, self.model_dir, combo_value(cb, "small"), "final" if cb is self.final_model else "dialogue")
        self._update_model_warn()

    def _compress_changed(self) -> None:
        self.compress_kbps.setEnabled(self.compress.currentData() != "off")

    def dialogue_values(self) -> dict:
        """dialogue 節へ入れるキーだけ（ほかのキーは触らない。send_after_deliver はメイン画面の［送信までする］が持つ）。"""
        return {
            "model": combo_value(self.dlg_model, "small"),
            "headset_button": self.dlg_headset.isChecked(),
            "mouse_button": self.dlg_mouse.currentData() or "",
            "click_commit": self.dlg_click.isChecked(),
            "force_on_mismatch": self.dlg_force.isChecked(),
            "idle_stop_sec": self.idle_stop.value(),
            "newline_after_sentence": self.dlg_nl.isChecked(),
            "break_blank_line": self.dlg_nl.isChecked(),
            "utterance_gap_sec": self.dlg_utt_gap.value(),
            "existing_text": self.dlg_existing.currentData(),
        }

    def values(self) -> dict:
        gap, force = self.para_gap.value(), self.para_force.value()
        return {
            "display_size": self.size_combo.currentData(),
            "final_model": combo_value(self.final_model, "small"),
            "finalize_on_end": bool(self.finalize_on_end.currentData()),
            "input_device": self.device.currentData(),
            "idle_monitor": self.idle_monitor.isChecked(),
            "paragraph_enabled": self.para_on.isChecked(),
            "paragraph_gap_sec": gap,
            "paragraph_force_gap_sec": max(gap, force),
            "hotkey_record": self.hk_rec.text().strip(),
            "hotkey_pause": self.hk_pause.text().strip(),
            "hotkey_show": self.hk_show.text().strip(),
            "audio_compress": self.compress.currentData(),
            "audio_compress_kbps": self.compress_kbps.value(),
            "hotkey_dialogue_commit": self.hk_dialogue.text().strip(),
            "dialogue_opts": self.dialogue_values(),            # open_settings が dialogue 節の該当キーだけ差し替える
        }


class SuggestDialog(QDialog):
    """修正で直した語を、置換辞書へ登録する候補の一覧（チェックしたものを登録）。"""

    def __init__(self, pairs: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("直した語を置換辞書へ登録")
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("元の清書と直した結果の違いです。登録したいものにチェックを入れてください。"))
        self.lst = QListWidget()
        # 行の部品（チェックボックス）に既定の padding を食わせない（高さが潰れて枠が欠ける）
        self.lst.setStyleSheet("QListWidget::item { padding: 0px 8px; }")
        self._checks: list[tuple[CheckBox, tuple[str, str]]] = []
        for a, b in pairs:
            it = QListWidgetItem()
            cb = CheckBox(f"{a}  →  {b}")
            cb.setChecked(True)
            it.setSizeHint(cb.sizeHint())
            self.lst.addItem(it)
            self.lst.setItemWidget(it, cb)
            self._checks.append((cb, (a, b)))
        lay.addWidget(self.lst)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("登録")
        bb.button(QDialogButtonBox.Cancel).setText("閉じる")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self) -> list[tuple[str, str]]:
        return [pair for cb, pair in self._checks if cb.isChecked()]


class MainWindow(QMainWindow):
    _ACTIVITY = (QEvent.MouseMove, QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel)

    def __init__(self, svc: Services, bridge: Bridge, use_tray: bool = True, volume: float = 1.0,
                 settings_path: Path | None = None, device_provider=None, muted: bool = False):
        super().__init__()
        self.svc, self.bridge = svc, bridge
        self.settings_path = settings_path  # None=既定の settings.json
        self.s = svc.settings
        self.setWindowTitle(f"{APP_NAME}  v{VERSION}")
        self.setWindowIcon(S.make_icon())
        self.setMinimumSize(380, 220)
        self._quitting = False
        self._last_text = ""            # 最後に清書が終わった全文（「送る」の対象）
        self._current_rec: Record | None = None
        self._edit_dirty = False
        self._loading_editor = False
        self._issues: list[Issue] = []
        self._tick_n = 0
        self._tray: QSystemTrayIcon | None = None
        self.hotkey_mgr = None
        self._pinned = bool(self.s.get("always_on_top", False))
        self._last_activity = time.monotonic()
        self._unrec_latch = HoldLatch(0.5)
        self._menu_exec = lambda menu, pos: menu.exec(pos)   # テストでは差し替える（実画面にメニューを出さない）
        self._geo_ready = False
        self._geo_saved: dict[str, list] = {}
        self.dialogue_ctl = None         # AI対話モード（常に有効）
        self.dialogue_panel = None       # 対話パネル（統合 GUI の UnifiedPanel）
        self.unified = None              # 統合 GUI（ui_unified.UnifiedView）
        self._job_t0: dict[int, float] = {}   # 清書の仕事の番号 → 実行を始めたのを見た時刻（残り時間の見積もり用）
        self._rec_ui_on = False          # トレイのメニュー・アイコンに出している「録音中」の状態

        if self._pinned:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self._build_ui(volume, device_provider, muted)
        self._connect()
        if use_tray:
            self._build_tray()
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.idle_timer = QTimer(self)
        self.idle_timer.setInterval(60000)
        self.idle_timer.timeout.connect(lambda: self.svc.cache.release_idle())
        self.idle_timer.start()
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(700)
        self._geo_timer.timeout.connect(lambda: self._save_geometry())
        self._ensure_dialogue()
        self.svc.queue.set_gate(self._queue_gate_open)      # セッション中は、録音全体の清書を始めない
        self.reload_history()
        self._update_queue_ui()
        if str(self.s.get("display_size", S.DEFAULT_SIZE)) != S.DISPLAY_SIZE:
            self.apply_display_size(str(self.s.get("display_size", S.DEFAULT_SIZE)))
        self._restore_geometry()
        self._geo_ready = True
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---- モーダルなダイアログの出入口（テストでは差し替えて、実画面に何も出さない）
    def _question(self, title, text, buttons=QMessageBox.Yes | QMessageBox.No):
        return QMessageBox.question(self, title, text, buttons)

    def _warn(self, title, text):
        QMessageBox.warning(self, title, text)

    def _open_file(self, title, start_dir, flt):
        return QFileDialog.getOpenFileName(self, title, start_dir, flt)[0]

    def _exec(self, dlg):
        return dlg.exec()

    # =============================================================== 画面の組み立て
    def _build_ui(self, volume: float, device_provider=None, muted: bool = False) -> None:
        """画面は統合 GUI（UnifiedView）だけ。履歴・用語辞書・Copilot・清書キュー・再生などの部品はここで作って持ち、
        見える場所（右の引き出し）へは Drawer が移し入れる。録音・履歴・辞書の処理はこの窓が受け持つ。"""
        self.statusBar().showMessage("待機中")
        self._build_recording_parts()
        self._build_side_groups()
        self._build_history_parts()
        self.queue_group = self._build_queue_panel()
        self.player = AudioPlayer(self, volume=volume, provider=device_provider,
                                  selected_id=str(self.s.get("playback_device", "")), muted=muted)
        self._fill_out_combo()
        self._build_unified()

    def _build_recording_parts(self) -> None:
        """録音まわりの部品。メーターは、録音していないのに話している状態（赤）の判定の入れ物として持つ（統合 GUI が値を読む）。
        WAV清書・出力フォルダは ⚙ メニューにある。"""
        self.meter = MeterWidget()

    def _build_side_groups(self) -> None:
        # 専門用語
        g = QGroupBox("専門用語（認識の手がかり）")
        self.grp_hot = g
        gl = QVBoxLayout(g)
        self.chips = ChipsWidget()
        self.chips.set_words(self._hotwords_list())
        row = QHBoxLayout()
        self.ed_hot = QLineEdit()
        self.ed_hot.setPlaceholderText("用語を入力して Enter")
        b = QPushButton("追加")
        row.addWidget(self.ed_hot, 1)
        row.addWidget(b)
        gl.addLayout(row)
        gl.addWidget(self.chips)
        self.btn_hot_add = b

        # 置換辞書
        g = QGroupBox("自動置換辞書（誤認識 → 正解）")
        self.grp_dict = g
        gl = QVBoxLayout(g)
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["誤認識", "正解"])
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setMinimumHeight(130)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        row = QHBoxLayout()
        self.btn_dict_add = QPushButton("行を追加")
        self.btn_dict_del = QPushButton("選択行を削除")
        row.addWidget(self.btn_dict_add)
        row.addWidget(self.btn_dict_del)
        row.addStretch(1)
        hint = QLabel("長い誤認識が優先・1回だけ適用。清書に効きます。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {S.MUTED}; font-size: {S.BASE_PT - 1}pt;")
        self.hint_dict = hint
        gl.addWidget(self.tbl)
        gl.addLayout(row)
        gl.addWidget(hint)
        self._fill_dict_table()

        # 前置き（名前＋本文）
        g = QGroupBox("前置き（メイン画面の［前置き ▾］から下書きの先頭へ入れられます）")
        self.grp_preset = g
        gl = QVBoxLayout(g)
        self.cmb_preset = QComboBox()
        self.ed_pname = QLineEdit()
        self.ed_pname.setPlaceholderText("前置きの名前")
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlaceholderText("前置きの本文")
        self.ed_prompt.setMinimumHeight(110)
        row = QHBoxLayout()
        self.btn_p_new = QPushButton("新規として追加")
        self.btn_p_save = QPushButton("保存")
        self.btn_p_del = QPushButton("削除")
        self.btn_p_del.setObjectName("danger")
        for b in (self.btn_p_new, self.btn_p_save, self.btn_p_del):
            row.addWidget(b)
        gl.addWidget(self.cmb_preset)
        gl.addWidget(self.ed_pname)
        gl.addWidget(self.ed_prompt)
        gl.addLayout(row)
        self.presets = load_presets()
        self._fill_presets()

    def _build_queue_panel(self) -> QWidget:
        """清書の状況（直近の清書の一覧と、実行中の中断）。進み具合・残り時間は、メイン画面の通知の行に出る。"""
        g = QGroupBox("清書の状況")
        lay = QVBoxLayout(g)
        lay.setContentsMargins(10, 6, 10, 8)
        self.btn_q_cancel = QPushButton("実行中の清書を中断")
        self.btn_q_cancel.setEnabled(False)
        lay.addWidget(self.btn_q_cancel)
        self.lst_queue = QListWidget()
        self.lst_queue.setFixedHeight(84)
        self.lst_queue.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst_queue.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.lst_queue)
        return g

    def _build_history_parts(self) -> None:
        """履歴（検索・一覧・閲覧・再生・修正）の部品。並べるのは引き出しの履歴タブ。"""
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("全文検索（空白区切りで AND）　例: 基地局 自動化")
        self.ed_search.setClearButtonEnabled(True)
        self.lst_hist = QListWidget()
        self.lst_hist.setSpacing(2)
        self.lst_hist.setWordWrap(True)
        self.lst_hist.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lbl_hist_count = QLabel("")
        self.lbl_hist_count.setStyleSheet(f"color: {S.MUTED};")
        self.lbl_rec_meta = QLabel("左の一覧から録音を選ぶと、ここに清書が出ます。行をクリックするとその位置から音声を再生します。")
        self.lbl_rec_meta.setWordWrap(True)
        self.lbl_rec_meta.setStyleSheet(f"color: {S.MUTED};")
        self.editor = ClickableText()
        self.editor.setStyleSheet(f"QPlainTextEdit {{ font-size: {S.BASE_PT + 1}pt; padding: 8px; }}")
        pw = QWidget()
        pl = WrapBarLayout(pw, hspace=10, vspace=6)
        self.btn_play = QPushButton("▶ 再生")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setMinimumWidth(120)
        self.lbl_pos = QLabel("0:00 / 0:00")
        self.chk_click = CheckBox("行クリックで再生")
        self.chk_click.setChecked(True)
        self.lbl_out = QLabel("出力先")
        self.lbl_out.setStyleSheet(f"color: {S.MUTED};")
        self.cmb_out = QComboBox()
        self.cmb_out.setMinimumWidth(180)
        self.cmb_out.setMaximumWidth(320)
        self.cmb_out.setToolTip("再生の出力先。「システム既定」は、既定のデバイスが切り替わったら追従します")
        pl.add(self.btn_play)
        pl.add(self.slider, grow=True)
        pl.add(self.lbl_pos)
        pl.add(self.chk_click)
        pl.add(self.lbl_out)
        pl.add(self.cmb_out)
        self.play_row = pw
        self.lbl_out_notice = QLabel("")
        self.lbl_out_notice.setStyleSheet("color: #ffcb7a;")
        self.lbl_out_notice.setWordWrap(True)
        self.lbl_out_notice.setVisible(False)
        bw = QWidget()
        self.hist_btn_row = bw       # 参照を持っておく（無いと、引き出しへ移す前にボタンごと消える）
        bl = WrapBarLayout(bw, hspace=8, vspace=6)
        self.btn_h_save = QPushButton("修正を保存")
        self.btn_h_save.setObjectName("primary")
        self.btn_h_revert = QPushButton("元の清書に戻す")
        self.btn_h_dict = QPushButton("直した語を辞書へ…")
        self.btn_h_redo = QPushButton("WAVから清書をやり直す")
        self.btn_h_del = QPushButton("削除（ごみ箱へ）")
        self.btn_h_del.setObjectName("danger")
        for b in (self.btn_h_save, self.btn_h_revert, self.btn_h_dict, self.btn_h_redo, self.btn_h_del):
            bl.add(b)
        self._set_viewer_enabled(False)

    # =============================================================== 接続
    def _connect(self) -> None:
        self.btn_q_cancel.clicked.connect(lambda: self.cancel_current())
        # 専門用語
        self.btn_hot_add.clicked.connect(lambda: self.add_hotword())
        self.ed_hot.returnPressed.connect(lambda: self.add_hotword())
        self.chips.changed.connect(lambda ws: self.save_hotwords(ws))
        # 置換辞書
        self.btn_dict_add.clicked.connect(lambda: self.dict_add_row())
        self.btn_dict_del.clicked.connect(lambda: self.dict_del_rows())
        self.tbl.itemChanged.connect(lambda it: self.dict_changed())
        # プリセット
        self.cmb_preset.currentIndexChanged.connect(lambda i: self.preset_selected())
        self.btn_p_new.clicked.connect(lambda: self.preset_new())
        self.btn_p_save.clicked.connect(lambda: self.preset_save())
        self.btn_p_del.clicked.connect(lambda: self.preset_delete())
        # 履歴
        self.ed_search.textChanged.connect(lambda t: self._search_later())
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self.reload_history())
        self.lst_hist.currentItemChanged.connect(lambda cur, prev: self.history_selected(cur))
        self.editor.lineClicked.connect(lambda i: self.editor_line_clicked(i))
        self.editor.contextRequested.connect(lambda i, sel, pos: self._on_history_context(i, sel, pos))
        self.editor.textChanged.connect(lambda: self._editor_changed())
        self.chk_click.toggled.connect(lambda v: setattr(self.editor, "click_enabled", v))
        self.btn_play.clicked.connect(lambda: self.player.toggle())
        self.slider.sliderMoved.connect(lambda v: self.player.seek(v / 1000.0))
        self.player.position.connect(self._player_position)
        self.player.state.connect(self._player_state)
        self.player.player.durationChanged.connect(lambda ms: self._player_duration(ms))
        self.player.devicesChanged.connect(lambda: self._fill_out_combo())
        self.player.selectionReset.connect(lambda msg: self._on_output_reset(msg))
        self.cmb_out.currentIndexChanged.connect(lambda i: self._on_out_changed(i))
        self.btn_h_save.clicked.connect(lambda: self.save_edit())
        self.btn_h_revert.clicked.connect(lambda: self.revert_edit())
        self.btn_h_dict.clicked.connect(lambda: self.suggest_dict())
        self.btn_h_redo.clicked.connect(lambda: self.redo_final())
        self.btn_h_del.clicked.connect(lambda: self.delete_record())
        # スレッド橋渡し
        self.bridge.job_event.connect(self._on_job_event)
        self.bridge.status.connect(lambda m: self.statusBar().showMessage(m, 10000))
        self.bridge.hotkey.connect(self._on_hotkey)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(S.make_icon(), self)
        self._tray.setToolTip(f"{APP_NAME} v{VERSION}")
        m = QMenu()
        self.act_toggle = QAction("画面を表示 / 隠す", m)
        self.act_toggle.triggered.connect(lambda: self.toggle_window())
        self.act_rec = QAction("録音を開始", m)
        self.act_rec.triggered.connect(lambda: self.toggle_record())
        self.act_pause = QAction("保留", m)
        self.act_pause.setEnabled(False)
        self.act_pause.triggered.connect(lambda: self.toggle_pause())
        a_dict = QAction("辞書・専門用語を開く", m)
        a_dict.triggered.connect(lambda: self.show_main(tab=0))
        a_hist = QAction("履歴・検索を開く", m)
        a_hist.triggered.connect(lambda: self.show_main(tab=1))
        a_set = QAction("設定…", m)
        a_set.triggered.connect(lambda: self.open_settings())
        a_quit = QAction("終了", m)
        a_quit.triggered.connect(lambda: self.quit_app())
        for a in (self.act_toggle, self.act_rec, self.act_pause, a_dict, a_hist, a_set):
            m.addAction(a)
        m.addSeparator()
        m.addAction(a_quit)
        self._tray_menu = m
        self._tray.setContextMenu(m)
        self._tray.activated.connect(lambda reason: self._tray_activated(reason))
        self._tray.show()

    @safe
    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # 左クリック
            self.toggle_window()

    # =============================================================== 窓の表示・モード・位置
    @safe
    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_main()

    @safe
    def show_main(self, tab: int | None = None) -> None:
        """自分の窓を表示して前面へ（トレイ/ホットキー/2つ目の起動からの要求）。"""
        if tab is not None:
            from ui_drawer import TAB_HISTORY, TAB_TERMS
            self.unified.set_drawer(True, tab=TAB_TERMS if tab == 0 else TAB_HISTORY)     # 辞書・専門用語 / 履歴・検索
        self._touch()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def eventFilter(self, obj, ev) -> bool:
        try:
            if ev.type() in self._ACTIVITY:
                self._last_activity = time.monotonic()
        except Exception:
            pass
        return False

    def showEvent(self, ev) -> None:
        self._touch()
        super().showEvent(ev)

    @staticmethod
    def _screens() -> list[tuple[int, int, int, int]]:
        out = []
        for sc in QGuiApplication.screens():
            g = sc.availableGeometry()
            out.append((g.x(), g.y(), g.width(), g.height()))
        return out

    def _geo_key(self) -> str:
        """位置とサイズは、引き出しの開閉ごとに別々に覚える。"""
        return "win_geometry_unified_drawer" if self.unified.drawer_open else "win_geometry_unified"

    def _current_rect(self) -> tuple[int, int, int, int] | None:
        if self.isMinimized() or self.isFullScreen():
            return None
        g = self.normalGeometry() if self.isMaximized() else self.geometry()
        return g.x(), g.y(), g.width(), g.height()

    def _save_geometry(self) -> None:
        """いまの位置とサイズを、現在のモード用に設定へ保存する（変わっていなければ書かない）。"""
        if not self._geo_ready:
            return
        r = self._current_rect()
        if r is None:
            return
        key = self._geo_key()
        if list(self.s.get(key) or []) != list(r):
            self.s[key] = list(r)
            self._save_settings()

    def _restore_geometry(self) -> None:
        """現在のモードの位置とサイズを復元（画面外に出ないようクランプ）。無ければ既定サイズ（位置は OS 任せ）。"""
        screens = self._screens()
        saved = parse_geometry(self.s.get(self._geo_key()))
        if saved is not None:
            self.setGeometry(*clamp_geometry(saved, screens))
            return
        w, h = self._unified_default_size()
        if screens:
            w, h = min(w, screens[0][2]), min(h, screens[0][3])
        self.resize(w, h)

    def moveEvent(self, ev) -> None:
        super().moveEvent(ev)
        if getattr(self, "_geo_ready", False):
            self._geo_timer.start()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if getattr(self, "_geo_ready", False):
            self._geo_timer.start()

    # ---- 表示サイズ（小/標準/大）。再起動せずに反映する
    def apply_display_size(self, name: str) -> None:
        name = S.set_size(name)
        self.s["display_size"] = name
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(S.QSS)
            app.setFont(S.app_font())
        self.editor.setStyleSheet(f"QPlainTextEdit {{ font-size: {S.BASE_PT + 1}pt; padding: 8px; }}")
        self.hint_dict.setStyleSheet(f"color: {S.MUTED}; font-size: {S.BASE_PT - 1}pt;")
        for w in self.findChildren(ElidedLabel):
            w._apply()
        for w in self.findChildren(QWidget):
            w.updateGeometry()
        self.unified.refit()
        self._apply_unified_min()

    @safe
    def set_always_on_top(self, on: bool) -> None:
        """最前面のピン。フラグを変えるとウィンドウが作り直されるので、位置・状態を戻して show し直す。
        前面化の強制（SetForegroundWindow 等）はしない（アクティブ化せずに表示するだけ）。"""
        on = bool(on)
        self._pinned = on
        self.s["always_on_top"] = on
        self._save_settings()
        if bool(self.windowFlags() & Qt.WindowStaysOnTopHint) == on:
            return
        geo, was_vis, was_min, was_max = self.geometry(), self.isVisible(), self.isMinimized(), self.isMaximized()
        self._geo_ready = False
        try:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on)   # 窓が再作成され、いったん隠れる
            if was_vis:
                self.setAttribute(Qt.WA_ShowWithoutActivating, True)
                try:
                    if was_min:
                        self.showMinimized()
                    elif was_max:
                        self.showMaximized()
                    else:
                        self.show()
                        self.setGeometry(geo)
                finally:
                    self.setAttribute(Qt.WA_ShowWithoutActivating, False)
        finally:
            self._geo_ready = True

    def closeEvent(self, ev) -> None:
        if self._tray is not None and not self._quitting:
            ev.ignore()
            self._save_geometry()
            self.hide()
            if not getattr(self, "_hint_shown", False):
                self._hint_shown = True
                self._notify("トレイに常駐しています", "終了はトレイアイコンの右クリック → 終了。")
            return
        self._save_geometry()
        self._geo_ready = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(ev)

    @safe
    def quit_app(self) -> None:
        sess, q = self.svc.session, self.svc.queue
        msg = []
        if sess.is_recording:
            msg.append("録音中です。止めて終了します（WAV は残ります）。")
        if q.busy:
            msg.append("清書が残っています。終了すると中断されます（あとで履歴から「WAVから清書」できます）。")
        if msg and self._question( "終了", "\n".join(msg) + "\n\n終了しますか？") != QMessageBox.Yes:
            return
        self._quitting = True
        self._save_geometry()
        self._shutdown_dialogue()       # ヘッドセットの独占・ワーカーを止める（録音の停止より先に）
        try:
            sess.close()
        except Exception:
            log.exception("終了時の録音停止")
        try:
            q.close(cancel_running=True)
        except Exception:
            pass
        if self.hotkey_mgr:
            self.hotkey_mgr.stop()
        if self._tray:
            self._tray.hide()
        QGuiApplication.quit()

    # =============================================================== 録音（＝AI対話のセッション。録音ボタン1つ）
    def _set_recording_ui(self, on: bool) -> None:
        """録音の開始・停止に合わせる部品（トレイのメニューとアイコン）。録音ボタンは統合 GUI が自分で更新する。"""
        self._rec_ui_on = bool(on)
        self._set_paused_ui(self._dialogue_held())
        if self._tray is not None:
            self.act_rec.setText("録音を停止" if on else "録音を開始")
            self.act_pause.setEnabled(on)
            self._tray.setIcon(S.make_icon(on))

    def _dialogue_held(self) -> bool:
        c = self.dialogue_ctl
        return bool(c is not None and c.active and c.snapshot().get("held"))

    def _set_paused_ui(self, paused: bool) -> None:
        if self._tray is not None:
            self.act_pause.setText("保留を解除" if paused else "保留")
            if self.svc.session.is_recording:
                self._tray.setToolTip(f"{APP_NAME} v{VERSION}" + ("（保留中）" if paused else "（録音中）"))
            else:
                self._tray.setToolTip(f"{APP_NAME} v{VERSION}")

    @safe
    def toggle_record(self) -> None:
        """録音ボタン・ホットキー・トレイ: 録音ボタンは1つ（セッション開始 ⇄ 終了）。"""
        self.unified.rec_pressed()

    @safe
    def toggle_pause(self) -> None:
        """保留（取り込みを止める）／保留を解除。"""
        self.unified.pause_pressed()

    def _sync_recording_ui(self) -> None:
        """録音（セッション）の開始・終了・保留に、トレイのメニュー・アイコンを合わせる（変わったときだけ）。"""
        on = bool(self.svc.session.is_recording)
        held = self._dialogue_held() if on else False
        key = (on, held)
        if key != getattr(self, "_rec_ui_key", None):
            self._rec_ui_key = key
            self._set_recording_ui(on)
            if on:
                self.player.stop()
                self._touch()
            else:
                self._set_issues([])

    # =============================================================== マイクの指摘
    def _set_issues(self, issues: list[Issue], recording: bool | None = None) -> None:
        """マイクの指摘を覚える（統合 GUI のマイク状態チップが読む）。"""
        self._issues = issues

    # =============================================================== 待機中のマイク確認
    def _monitor_wanted(self) -> bool:
        """窓が見えている間だけ。最小化・トレイ格納・一定時間操作なしでは閉じる。"""
        if not bool(self.s.get("idle_monitor", True)):
            return False
        if not self.isVisible() or self.isMinimized():
            return False
        to = float(IDLE_MONITOR_TIMEOUT_SEC)
        if to > 0 and (time.monotonic() - self._last_activity) > to:
            return False
        return True

    def _refresh_mic_ui(self) -> None:
        """待機中・一時停止中のマイクの指摘を更新。"""
        sess = self.svc.session
        if sess.is_recording and not sess.is_paused:
            return  # 録音中は録音の監視（_tick 内）が更新する
        if sess.monitor_active:
            if self.svc.session.hub.seconds_since_audio > 3.0:
                self._issues = []       # 「音が届いていません」はチップが自分で判定して出す
                return
            self._set_issues(sess.mic_issues())
        else:
            self._set_issues([])

    # =============================================================== tick
    @safe
    def _tick(self) -> None:
        self._tick_n += 1
        sess = self.svc.session
        rec = sess.rec
        now = time.monotonic()
        sess.request_monitor(self._monitor_wanted())
        if sess.is_recording and rec is not None:
            self.meter.set_level(sess.level)
            self._update_meter_unrecorded(now)
            if sess.is_paused:
                if self._tick_n % 5 == 0:
                    self._refresh_mic_ui()
            else:
                if rec.seconds_since_audio <= 2.0 and self._tick_n % 10 == 0:
                    self._set_issues(sess.monitor.issues())
                # 無音が続いたときの自動終了は、AI対話のセッション側（dialogue.idle_stop_sec）が行う
        else:
            self.meter.set_level(sess.level)  # 待機中のマイク確認が開いていれば動く（閉じていれば 0）
            self._update_meter_unrecorded(now)
            if self._tick_n % 5 == 0:
                self._refresh_mic_ui()
        self._sync_recording_ui()
        self.unified.refresh()
        if self._tick_n % 3 == 0:
            self.svc.queue.poke()          # セッションの開始・終了を、清書キューのゲートへ反映する
            self._update_queue_ui()

    def _update_meter_unrecorded(self, now: float) -> None:
        """録音していない（待機中・一時停止中）のに話しているとき、メーターを赤にする（即時・保持 0.5 秒）。"""
        sess = self.svc.session
        cond = meter_unrecorded(sess.is_recording, sess.is_paused, sess.monitor_active, bool(sess.monitor_error),
                                sess.speaking_now())
        self.meter.set_unrecorded(self._unrec_latch.update(now, cond))

    # =============================================================== 右クリックメニュー（履歴の文字起こし部分）
    def _copy_text(self, text: str, key: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(COPIED_MESSAGE.get(key, "コピーしました"), 4000)

    def run_menu_item(self, item: MenuItem) -> None:
        """メニュー項目を実行する。"""
        k = item.key
        if k in ("copy_selection", "copy_part"):
            self._copy_text(item.payload, k)
        elif k == "copy_all":
            self._copy_text(self._history_full_text(), k)
        elif k == "copy_from_here":
            self._copy_text(self._history_from_here(item.index), k)
        elif k == "play_from":
            self.editor_line_clicked(item.index)

    def _show_menu(self, items: list[MenuItem], pos, extra_actions=None) -> None:
        """項目を QMenu にして表示する。項目が無ければ出さない。表示は self._menu_exec（差し替え可）。"""
        items = [i for i in items if i.key == SEP or i.label]
        if not items:
            return
        menu = QMenu(self)
        actions = {}
        for it in items:
            if it.key == SEP:
                menu.addSeparator()
                continue
            a = menu.addAction(it.label)
            a.setEnabled(it.enabled)
            actions[a] = it
        if extra_actions:
            menu.addSeparator()
            for a in extra_actions:
                menu.addAction(a)
        chosen = self._menu_exec(menu, pos)
        if chosen in actions:
            self.run_menu_item(actions[chosen])

    def _history_blocks(self) -> list[str]:
        return self.editor.toPlainText().split("\n")

    def _history_full_text(self) -> str:
        return self.editor.toPlainText()

    def _history_from_here(self, idx: int) -> str:
        return "\n".join(b for b in self._history_blocks()[max(idx, 0):] if b.strip())

    def history_menu_items(self, idx: int, selection: str = "") -> list[MenuItem]:
        return history_menu(self._history_blocks(), idx, selection, can_play=self.btn_play.isEnabled())

    @safe
    def _on_history_context(self, idx: int, selection: str, pos) -> None:
        extra = []
        try:                                   # 編集用の標準項目（切り取り・貼り付け・元に戻す等）も下に残す
            extra = list(self.editor.createStandardContextMenu().actions())
        except Exception:
            extra = []
        self._show_menu(self.history_menu_items(idx, selection), pos, extra)

    # =============================================================== 清書キュー
    @safe
    def _on_job_event(self, job: Job) -> None:
        self._update_queue_ui()
        if job.status == DONE and job.result is not None:
            r = job.result
            self._last_text = r.text
            self._notify("清書が完了しました", f"{job.title}（{r.audio_seconds:.0f}秒の音声を{r.elapsed:.0f}秒で清書）\n履歴に保存しました")
            self.statusBar().showMessage(f"清書完了: {job.title}　履歴に保存しました", 15000)
            self.reload_history(keep=job.history_id)
        elif job.status == ERROR:
            self._notify("清書できませんでした", f"{job.title}: {job.error.splitlines()[0] if job.error else ''}")
            self.statusBar().showMessage(f"清書エラー: {job.error.splitlines()[0] if job.error else ''}", 20000)
            log.error("清書エラー %s: %s", job.title, job.error)

    def _queue_gate_open(self) -> bool:
        """清書キューのゲート: セッション中（録音中）は閉じる。終わったら開く（待機中の清書が始まる）。"""
        try:
            return not self._dialogue_active()
        except Exception:
            return True

    def progress_text(self) -> str:
        """メイン画面の通知の行に出す「清書中 35%（あと約3分）」。清書も待機も無いときは空文字（何も出さない）。"""
        q = self.svc.queue
        cur, wait = q.current, q.waiting
        if cur is None:
            return U.finalize_progress_text(None, 0.0, wait, running=False, gated=q.gated)
        t0 = self._job_t0.get(cur.id)
        elapsed = (time.monotonic() - t0) if t0 is not None else 0.0
        return U.finalize_progress_text(cur.progress, elapsed, wait, running=True)

    def _update_queue_ui(self) -> None:
        q = self.svc.queue
        cur = q.current
        jobs = q.jobs[-6:][::-1]
        if cur is not None and cur.id not in self._job_t0:
            self._job_t0[cur.id] = time.monotonic()
        for k in list(self._job_t0):                     # 終わった仕事の控えは捨てる
            if cur is None or k != cur.id:
                del self._job_t0[k]
        self.btn_q_cancel.setEnabled(cur is not None)
        self.lst_queue.clear()
        for j in jobs:
            icon = {PENDING: "⏳ 待機", RUNNING: f"▶ 清書中 {int(j.progress * 100)}%", DONE: "✔ 完了",
                    ERROR: "✖ エラー", CANCELLED: "－ 中断"}[j.status]
            extra = ""
            if j.status == DONE and j.result is not None:
                extra = f"　{j.result.audio_seconds:.0f}秒の音声 / {j.result.elapsed:.0f}秒"
            elif j.status == ERROR:
                extra = "　" + (j.error.splitlines()[0] if j.error else "")
            elif j.status == RUNNING and j.message:
                extra = "　" + j.message
            self.lst_queue.addItem(f"{icon}　{j.title}{extra}")

    def on_session_stopped(self, report: dict) -> None:
        """AI対話のセッションが終わった（統合 GUI から）。設定「録音全体の清書」が「する」なら、録音全体を清書して履歴に保存する仕事を
        積む（清書キューは裏で動くが、セッション中は始めない。次のセッションをすぐ始めてよい）。「しない」なら、再認識せず、
        セッション中の発話ごとの結果から履歴の1件を作る。録音ファイルが無い・短すぎるときは作らない。"""
        raw = str((report or {}).get("wav") or "")
        wav = Path(raw)
        u = self.unified
        if not bool(self.s.get("finalize_on_end", True)):
            self._save_session_history(wav, 0)
            return
        if not raw or not wav.is_file() or wav.stat().st_size < MIN_FINALIZE_BYTES:
            if u is not None:
                u.notice("録音が短い（または見つからない）ので、録音全体の清書は見送りました", "muted")
            return
        try:
            self.svc.queue.submit(wav, compress=True)
        except RuntimeError:        # 終了処理中
            return
        self._update_queue_ui()
        if u is not None:
            u.notice("セッションを終えました。録音全体の清書は、次の録音が始まる前なら始めます（終わると履歴に残ります）", "accent")

    def _save_session_history(self, wav: Path, tries: int) -> None:
        """「しない」: セッション中の発話ごとの結果（時刻つき）から履歴の1件を作る。認識待ちが残っていれば、終わるのを少し待つ。
        そのあと、WAV→Opus の圧縮（と無音カット）は軽い仕事として清書キューへ（ゲートに関係なく進む）。"""
        from paragraph import plain_text
        u = self.unified
        c = self.dialogue_ctl
        ds = c.ds if c is not None else None
        if ds is None:
            return
        try:
            pend = int(ds.pending_recognitions)
        except Exception:
            pend = 0
        if pend > 0 and tries < 120:                  # 認識が終わるのを待つ（最大2分）。終わった分は全部入れる
            QTimer.singleShot(1000, lambda: self._save_session_history(wav, tries + 1))
            return
        segs = [(float(a), float(b), str(t)) for a, b, t in ds.session_segments()]
        text = plain_text(segs)
        dur = wav_seconds(wav)
        if dur <= 0 and segs:
            dur = max(b for _a, b, _t in segs)
        if not text.strip() or dur < 1.0 or not wav.is_file():
            if u is not None:
                u.notice("録音が短い（または文字がない）ので、履歴は作りませんでした", "muted")
            return
        name = str(dialogue_config(self.s).get("model") or "small")
        hid = self.svc.history.add(text=text, segments=segs, duration=dur, model=f"{name}（発話ごと）", wav_path=wav)
        if AC.normalize_mode(self.s.get("audio_compress")) != "off":
            try:
                self.svc.queue.submit(wav, history_id=hid, compress_only=True)
            except RuntimeError:
                pass
        self.reload_history(keep=hid)
        self._update_queue_ui()
        if u is not None:
            u.notice("セッションを終えました。発話ごとの結果を履歴に保存しました", "accent")

    @safe
    def cancel_current(self) -> None:
        cur = self.svc.queue.current
        if cur is not None:
            self.svc.queue.cancel(cur)

    @safe
    def pick_wav(self) -> None:
        p = self._open_file("清書する WAV を選ぶ", str(resolve_dir(self.s.get("out_dir", "out"))),
                            "音声 (*.wav *.opus *.ogg);;すべて (*.*)")
        if not p:
            return
        model = self._ask_wav_model(Path(p))
        if model is None:
            return
        default = str(self.s.get("final_model", "small"))
        # 既定と違うモデルで清書したときは、txt/md を別名にして、同じ録音の別モデルの結果を上書きしない
        tag = "" if MC.model_name(model) == MC.model_name(default) else MC.model_name(model).replace("/", "-")
        self.svc.queue.submit(Path(p), model=MC.model_name(model), file_tag=tag)
        self.statusBar().showMessage(f"清書キューに入れました（モデル: {model}）: {Path(p).name}")

    def _ask_wav_model(self, wav: Path) -> str | None:
        """WAV清書で使うモデルを選ぶ（既定は設定の録音全体の清書モデル）。キャンセルなら None。"""
        dlg = WavModelDialog(wav.name, resolve_dir(self.s.get("model_dir", "models")),
                             str(self.s.get("final_model", "small")), self)
        if self._exec(dlg) != QDialog.Accepted:
            return None
        return dlg.model()

    @safe
    def open_out(self) -> None:
        d = resolve_dir(self.s.get("out_dir", "out"))
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(str(d))  # type: ignore[attr-defined]

    def _notify(self, title: str, msg: str) -> None:
        if self._tray is not None:
            self._tray.showMessage(title, msg, QSystemTrayIcon.Information, 8000)

    # =============================================================== 専門用語
    def _hotwords_list(self) -> list[str]:
        import re
        return [w.strip() for w in re.split(r"[,、，\n]", str(self.s.get("hotwords", ""))) if w.strip()]

    @safe
    def add_hotword(self) -> None:
        if self.chips.add_word(self.ed_hot.text()):
            self.ed_hot.clear()

    @safe
    def save_hotwords(self, words: list) -> None:
        self.s["hotwords"] = ", ".join(words)
        self._save_settings()

    def _save_settings(self) -> None:
        try:
            save_settings(self.s, self.settings_path)
        except OSError as e:
            self.statusBar().showMessage(f"設定を保存できませんでした: {e}", 8000)

    # =============================================================== 置換辞書
    def _fill_dict_table(self) -> None:
        from replacements import load_pairs
        self._dict_loading = True
        self.tbl.setRowCount(0)
        for p in load_pairs():
            self._append_dict_row(p["from"], p["to"])
        self._dict_loading = False

    def _append_dict_row(self, a: str, b: str) -> None:
        r = self.tbl.rowCount()
        self.tbl.insertRow(r)
        self.tbl.setItem(r, 0, QTableWidgetItem(a))
        self.tbl.setItem(r, 1, QTableWidgetItem(b))

    @safe
    def dict_add_row(self) -> None:
        self._dict_loading = True
        self._append_dict_row("", "")
        self._dict_loading = False
        self.tbl.setCurrentCell(self.tbl.rowCount() - 1, 0)
        self.tbl.editItem(self.tbl.item(self.tbl.rowCount() - 1, 0))

    @safe
    def dict_del_rows(self) -> None:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        for r in rows:
            self.tbl.removeRow(r)
        self.dict_changed()

    def dict_pairs(self) -> list[dict]:
        out = []
        for r in range(self.tbl.rowCount()):
            a = self.tbl.item(r, 0)
            b = self.tbl.item(r, 1)
            out.append({"from": a.text() if a else "", "to": b.text() if b else ""})
        return out

    @safe
    def dict_changed(self) -> None:
        if getattr(self, "_dict_loading", False):
            return
        pairs = self.dict_pairs()
        self.svc.replacer.set_pairs(pairs)
        save_pairs(pairs)

    def add_dict_pairs(self, pairs: list[tuple[str, str]]) -> int:
        have = {(p["from"]) for p in self.dict_pairs()}
        n = 0
        self._dict_loading = True
        for a, b in pairs:
            if a and a not in have:
                self._append_dict_row(a, b)
                have.add(a)
                n += 1
        self._dict_loading = False
        self.dict_changed()
        return n

    # =============================================================== プリセット
    def _fill_presets(self) -> None:
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        for p in self.presets:
            self.cmb_preset.addItem(p["name"])
        sel = self.s.get("selected_preset", "")
        names = [p["name"] for p in self.presets]
        if sel in names:
            self.cmb_preset.setCurrentIndex(names.index(sel))
        self.cmb_preset.blockSignals(False)
        self.preset_selected()
        if self.unified is not None and self.unified.panel is not None:      # 画面を組み立てている最中はまだ無い
            self.unified.panel.fill_prompts()       # メイン画面の「前置き ▾」のメニューも合わせる

    @safe
    def preset_selected(self) -> None:
        i = self.cmb_preset.currentIndex()
        if 0 <= i < len(self.presets):
            self.ed_pname.setText(self.presets[i]["name"])
            self.ed_prompt.setPlainText(self.presets[i]["prompt"])
            if self.s.get("selected_preset") != self.presets[i]["name"]:
                self.s["selected_preset"] = self.presets[i]["name"]
                self._save_settings()
        else:
            self.ed_pname.clear()
            self.ed_prompt.clear()

    @safe
    def preset_save(self) -> None:
        i = self.cmb_preset.currentIndex()
        name = self.ed_pname.text().strip()
        if not name:
            self.statusBar().showMessage("プリセット名を入れてください", 5000)
            return
        if 0 <= i < len(self.presets):
            self.presets[i] = {"name": name, "prompt": self.ed_prompt.toPlainText()}
        else:
            self.presets.append({"name": name, "prompt": self.ed_prompt.toPlainText()})
        save_presets(self.presets)
        self.s["selected_preset"] = name
        self._fill_presets()
        self.statusBar().showMessage(f"プリセット「{name}」を保存しました", 5000)

    @safe
    def preset_new(self) -> None:
        name = self.ed_pname.text().strip() or "新しいプリセット"
        names = {p["name"] for p in self.presets}
        base, n = name, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        self.presets.append({"name": name, "prompt": self.ed_prompt.toPlainText()})
        save_presets(self.presets)
        self.s["selected_preset"] = name
        self._fill_presets()

    @safe
    def preset_delete(self) -> None:
        i = self.cmb_preset.currentIndex()
        if not (0 <= i < len(self.presets)):
            return
        if self._question( "削除", f"プリセット「{self.presets[i]['name']}」を削除しますか？") != QMessageBox.Yes:
            return
        del self.presets[i]
        save_presets(self.presets)
        self._fill_presets()

    # =============================================================== 履歴と閲覧
    def _search_later(self) -> None:
        self._search_timer.start()

    def reload_history(self, keep: int | None = None) -> None:
        q = self.ed_search.text()
        recs = self.svc.history.search(q) if q.strip() else self.svc.history.list()
        cur_id = keep if keep is not None else (self._current_rec.id if self._current_rec else None)
        self.lst_hist.blockSignals(True)
        self.lst_hist.clear()
        sel_row = -1
        for i, r in enumerate(recs):
            when = r.created_at.replace("T", " ")[:16]
            edited = "  ✎修正あり" if r.edited_text is not None else ""
            sub = snippet(r.current_text, q) if q.strip() else r.title
            it = QListWidgetItem(f"{when}　{fmt_dur(r.duration)}　{r.model}{edited}\n{sub}")
            it.setData(Qt.UserRole, r.id)
            self.lst_hist.addItem(it)
            if r.id == cur_id:
                sel_row = i
        if sel_row >= 0:
            self.lst_hist.setCurrentRow(sel_row)
        self.lst_hist.blockSignals(False)
        self.lbl_hist_count.setText(f"{len(recs)} 件" + (f"（「{q.strip()}」を含む）" if q.strip() else ""))

    def _set_viewer_enabled(self, on: bool) -> None:
        for w in (self.editor, self.btn_play, self.slider, self.btn_h_save, self.btn_h_revert, self.btn_h_dict,
                  self.btn_h_redo, self.btn_h_del):
            w.setEnabled(on)

    def _confirm_discard(self) -> bool:
        if not self._edit_dirty or self._current_rec is None:
            return True
        r = self._question( "未保存の修正", "修正が保存されていません。保存しますか？",
                                 QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Yes:
            self.save_edit()
        self._edit_dirty = False
        return True

    @safe
    def history_selected(self, item) -> None:
        if item is None:
            return
        rid = item.data(Qt.UserRole)
        if self._current_rec is not None and rid != self._current_rec.id and not self._confirm_discard():
            # 選択を元に戻す
            self.lst_hist.blockSignals(True)
            for i in range(self.lst_hist.count()):
                if self.lst_hist.item(i).data(Qt.UserRole) == self._current_rec.id:
                    self.lst_hist.setCurrentRow(i)
            self.lst_hist.blockSignals(False)
            return
        self.open_record(rid)

    def open_record(self, rid: int) -> None:
        rec = self.svc.history.get(rid)
        if rec is None:
            return
        self.player.stop()
        self._current_rec = rec
        self._loading_editor = True
        if rec.edited_text is not None:
            text = rec.edited_text
        else:
            text = "\n".join(s[2] for s in viewer_segments(rec.segments)) if rec.segments else rec.text
        self.editor.setPlainText(text)
        self._loading_editor = False
        self._edit_dirty = False
        self.editor.highlight_block(None)
        self._set_viewer_enabled(True)
        has_wav = bool(rec.wav_path) and Path(rec.wav_path).exists()   # WAV または圧縮後の Opus
        self.btn_play.setEnabled(has_wav)
        self.slider.setEnabled(has_wav)
        self.btn_h_redo.setEnabled(has_wav)
        self.player.load(AC.playback_source(rec.wav_path) if has_wav else "")
        self.slider.setValue(0)
        when = rec.created_at.replace("T", " ")
        ed = f"　修正: {rec.edited_at.replace('T', ' ')}" if rec.edited_at else ""
        self.lbl_rec_meta.setText(f"{when}　長さ {fmt_dur(rec.duration)}　モデル {rec.model}{ed}"
                                  + ("" if has_wav else "　（録音ファイルが見つからないので再生・やり直しはできません）"))

    def _editor_changed(self) -> None:
        if not self._loading_editor:
            self._edit_dirty = True

    @staticmethod
    def _cuts_of(rec) -> list:
        """無音を縮めた Opus の履歴なら、その記録（元の時刻 -> 縮めた音声の時刻の変換に使う）。WAV・記録なしは空。"""
        cuts = getattr(rec, "cuts", None) or []
        return cuts if cuts and not AC.is_wav(rec.wav_path or "x.wav") else []

    def _audio_time(self, rec, t: float) -> float:
        """セグメントの時刻（元の時刻）-> 音声ファイル上の再生位置。"""
        c = self._cuts_of(rec)
        return AC.orig_to_cut_time(t, c) if c else t

    def _orig_time(self, rec, sec: float) -> float:
        """音声ファイル上の再生位置 -> セグメントの時刻（元の時刻）。"""
        c = self._cuts_of(rec)
        return AC.cut_to_orig_time(sec, c) if c else sec

    @safe
    def editor_line_clicked(self, idx: int) -> None:
        rec = self._current_rec
        if rec is None or not self.btn_play.isEnabled():
            return
        t = seek_time_for_block(viewer_segments(rec.segments), idx, self.editor.blockCount(), rec.duration)
        if t is not None:
            self.player.play_from(self._audio_time(rec, t))

    @safe
    def _player_position(self, sec: float) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(int(sec * 1000))
        self.lbl_pos.setText(f"{fmt_dur(sec)} / {fmt_dur(self.player.duration)}")
        rec = self._current_rec
        if rec is not None:
            segs = viewer_segments(rec.segments)
            if len(segs) == self.editor.blockCount():
                self.editor.highlight_block(block_for_time(segs, self._orig_time(rec, sec)))

    def _player_duration(self, ms: int) -> None:
        self.slider.setRange(0, int(ms))

    @safe
    def _player_state(self, playing: bool) -> None:
        self.btn_play.setText("❚❚ 一時停止" if playing else "▶ 再生")

    # ---- 再生の出力先
    def _fill_out_combo(self) -> None:
        """出力先のコンボを、いまのデバイス一覧で作り直す（デバイス構成が変わったときにも呼ばれる）。"""
        if not hasattr(self, "cmb_out"):
            return
        sel = str(self.s.get("playback_device", "") or "")
        entries = combo_entries(self.player.devices())
        self.cmb_out.blockSignals(True)
        self.cmb_out.clear()
        idx = 0
        for i, (text, dev_id) in enumerate(entries):
            self.cmb_out.addItem(text, dev_id)
            if dev_id == sel:
                idx = i
        self.cmb_out.setCurrentIndex(idx)
        self.cmb_out.blockSignals(False)
        if sel and idx == 0:   # 選んでいた出力先が見つからない → システム既定に戻す
            self.player.refresh_output()

    @safe
    def _on_out_changed(self, i: int) -> None:
        dev_id = self.cmb_out.itemData(i) or ""
        self.s["playback_device"] = dev_id
        self._save_settings()
        self.lbl_out_notice.setVisible(False)
        self.player.set_selected(dev_id)

    @safe
    def _on_output_reset(self, msg: str) -> None:
        """選んだ出力先が無くなった: システム既定に戻して、画面で知らせる。"""
        self.s["playback_device"] = ""
        self._save_settings()
        self.lbl_out_notice.setText("⚠ " + msg)
        self.lbl_out_notice.setVisible(True)
        self.statusBar().showMessage(msg, 10000)
        self.cmb_out.blockSignals(True)
        self.cmb_out.setCurrentIndex(0)
        self.cmb_out.blockSignals(False)

    @safe
    def save_edit(self) -> None:
        rec = self._current_rec
        if rec is None:
            return
        text = self.editor.toPlainText()
        self.svc.history.save_edit(rec.id, text)
        # 修正後の全文も隣に書き出す（元の .txt は触らない）
        if rec.txt_path:
            p = Path(rec.txt_path)
            if p.parent.exists():
                from paths import write_text_lf
                write_text_lf(p.with_name(p.stem + "_edited.txt"), text + "\n")
        self._edit_dirty = False
        self.reload_history(keep=rec.id)
        self._current_rec = self.svc.history.get(rec.id)
        self.statusBar().showMessage("修正を保存しました（元の清書も残してあります）", 8000)

    @safe
    def revert_edit(self) -> None:
        rec = self._current_rec
        if rec is None:
            return
        if self._question( "元に戻す", "修正を捨てて、元の清書に戻しますか？") != QMessageBox.Yes:
            return
        self.svc.history.revert_edit(rec.id)
        self._edit_dirty = False
        self.reload_history(keep=rec.id)
        self.open_record(rec.id)

    @safe
    def suggest_dict(self) -> None:
        rec = self._current_rec
        if rec is None:
            return
        orig = "\n".join(s[2] for s in viewer_segments(rec.segments)) if rec.segments else rec.text
        pairs = suggest_replacements(orig, self.editor.toPlainText())
        if not pairs:
            self._warn( "置換辞書", "登録できる違いが見つかりませんでした（語の置き換えだけが対象です）。")
            return
        dlg = SuggestDialog(pairs, self)
        if self._exec(dlg) == QDialog.Accepted:
            n = self.add_dict_pairs(dlg.chosen())
            self.statusBar().showMessage(f"置換辞書に {n} 件登録しました（次の清書から効きます）", 8000)

    @safe
    def redo_final(self) -> None:
        rec = self._current_rec
        if rec is None or not rec.wav_path:
            return
        if rec.edited_text is not None and self._question(
                "清書のやり直し", "やり直すと、いまの修正は破棄されます（新しい清書が正になります）。続けますか？") != QMessageBox.Yes:
            return
        self.svc.queue.submit(Path(rec.wav_path), history_id=rec.id)
        self.statusBar().showMessage("清書キューに入れました。終わると履歴が更新されます", 8000)

    @safe
    def delete_record(self) -> None:
        rec = self._current_rec
        if rec is None:
            return
        if self._question( "削除", f"この録音を履歴から消し、録音ファイル（WAV/Opus）・txt・md をごみ箱へ移します。\n\n{rec.title}\n\nよろしいですか？") != QMessageBox.Yes:
            return
        self.player.stop()
        self.player.load("")
        failed = self.svc.history.delete(rec.id)
        self._current_rec = None
        self._edit_dirty = False
        self.editor.clear()
        self._set_viewer_enabled(False)
        self.lbl_rec_meta.setText("削除しました。")
        self.reload_history()
        if failed:
            self._warn( "ごみ箱へ移せなかったファイル", "次のファイルは残してあります:\n" + "\n".join(failed))

    # =============================================================== 統合 GUI（3行窓＋引き出し）。AI対話モードは常に有効
    def _build_unified(self) -> None:
        """中央を統合 GUI にする。録音・履歴・辞書の処理はこの窓が持ち、履歴・辞書などの部品は引き出しへ移る。"""
        from ui_unified import UnifiedView
        self.unified = UnifiedView(self)
        self.setCentralWidget(self.unified)
        self.statusBar().setVisible(False)           # 通知は下書き欄の下の1行へ
        self.statusBar().messageChanged.connect(lambda m: self._status_to_notice(m))

    def _status_to_notice(self, msg: str) -> None:
        u = self.unified
        if msg and u is not None and u.panel is not None:
            u.panel.set_notice(msg, "muted")

    def _unified_default_size(self) -> tuple[int, int]:
        from ui_unified import UNIFIED_DEFAULT, UNIFIED_DRAWER_DEFAULT, UNIFIED_EXPANDED_DEFAULT
        u = self.unified
        if u.drawer_open:
            return UNIFIED_DRAWER_DEFAULT
        return UNIFIED_EXPANDED_DEFAULT if (u.panel is not None and u.panel.expanded) else UNIFIED_DEFAULT

    def _apply_unified_min(self) -> None:
        """統合 GUI の最小サイズ（部品が重ならない大きさ）。明示的な最小サイズがあるとレイアウトの最小が効かないので、自分で決める。"""
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)   # 部品の大きさを変えた直後は最小サイズの値がまだ古いので、先に再計算させる
        lay = self.unified.layout()
        if lay is not None:
            lay.activate()
        sh = self.unified.minimumSizeHint()
        self.setMinimumSize(max(sh.width(), 320), max(sh.height(), 200))

    def _unified_relayout(self, mutate) -> None:
        """引き出しの開閉など、窓の大きさが変わる切り替え。いまの状態の位置とサイズを保存し、切り替えたあとの状態の
        位置とサイズを復元する（まだ覚えていなければ、左上を保って幅を増減）。"""
        self._save_geometry()
        self._geo_ready = False
        try:
            keep = self.geometry()
            was_open = self.unified.drawer_open
            mutate()
            self._apply_unified_min()
            saved = parse_geometry(self.s.get(self._geo_key()))
            if saved is None:
                from ui_drawer import DRAWER_WIDTH
                now_open = self.unified.drawer_open
                w = keep.width() + (DRAWER_WIDTH if (now_open and not was_open) else -DRAWER_WIDTH if (was_open and not now_open) else 0)
                h = keep.height() if not (now_open and not was_open) else max(keep.height(), self._unified_default_size()[1])
                saved = (keep.x(), keep.y(), max(w, self.minimumWidth()), max(h, self.minimumHeight()))
            self.setGeometry(*clamp_geometry(saved, self._screens()))
        finally:
            self._geo_ready = True
        self._save_geometry()

    def _unified_resize_for_expand(self, on: bool) -> None:
        """下書きを広げた／直近3行へ戻した: 最小サイズを取り直し、戻したときは窓も縮める。
        部品の大きさを変えた直後は、入れ子のレイアウトの最小サイズがまだ古い（再計算はイベントループの次の回）ので、1回待ってから行う。"""
        QTimer.singleShot(0, lambda: self._unified_resize_for_expand_now(on))

    def _unified_resize_for_expand_now(self, on: bool) -> None:
        self._apply_unified_min()
        if self.isMaximized() or self.isMinimized():
            return
        g = self.geometry()
        if on:
            from ui_unified import UNIFIED_EXPANDED_DEFAULT
            h = max(g.height(), UNIFIED_EXPANDED_DEFAULT[1])
        else:
            from ui_unified import UNIFIED_DEFAULT
            h = max(self.minimumHeight(), UNIFIED_DEFAULT[1])
        self.resize(g.width(), h)

    def _dialogue_active(self) -> bool:
        c = self.dialogue_ctl
        return bool(c is not None and c.active)

    def _ensure_dialogue(self) -> None:
        """対話パネルを（なければ）作って統合 GUI へ取り付ける。部品の組み立てだけで、録音もワーカーも始めない。"""
        if self.dialogue_panel is not None:
            return
        from dialogue_controller import DialogueController
        from dialogue_session import make_whisper_recognizer
        from ui_unified import UnifiedPanel

        self.dialogue_ctl = DialogueController(
            self.s, self.svc.session, make_whisper_recognizer(self.s, model_cache=self.svc.cache),
            replacer=self.svc.replacer, on_status=lambda m: self.bridge.status.emit(m),
            on_last_target=lambda name: self._save_settings(), on_settings_changed=self._save_settings)
        self.dialogue_panel = UnifiedPanel(self.dialogue_ctl, host=self, parent=self.unified,
                                           on_started=self._sync_recording_ui, on_settings=self.open_settings)
        self.unified.attach_panel(self.dialogue_panel)

    def _shutdown_dialogue(self) -> None:
        """対話のセッション・ワーカー・ヘッドセットを必ず止める（何度呼んでも安全）。"""
        c, w = self.dialogue_ctl, self.dialogue_panel
        self.dialogue_ctl = self.dialogue_panel = None
        if self.unified is not None:
            self.unified.panel = None
        try:
            if c is not None:
                c.shutdown()
        except Exception:
            log.exception("対話モードの停止")
        if w is not None:
            try:
                w.timer.stop()
                w.hide()
                w.setParent(None)
                w.deleteLater()
            except Exception:
                pass

    # =============================================================== 設定・ホットキー
    @safe
    def open_settings(self) -> None:
        try:
            from recorder import list_input_devices
            devs = list_input_devices()
        except Exception:
            devs = []
        dlg = SettingsDialog(self.s, devs, self)
        if self._exec(dlg) != QDialog.Accepted:
            return
        v = dlg.values()
        dlg_opts = dict(v.pop("dialogue_opts", {}) or {})
        old_headset = bool(dialogue_config(self.s)["headset_button"])
        hk_keys = ("hotkey_record", "hotkey_show", "hotkey_pause", "hotkey_dialogue_commit")
        changed_hk = any(v[k] != self.s.get(k, "") for k in hk_keys)
        dev_changed = v["input_device"] != self.s.get("input_device")
        size_changed = v["display_size"] != self.s.get("display_size", S.DISPLAY_SIZE)
        self.s.update(v)
        # dialogue 節は該当キーだけ差し替える（worker_python・last_target など、ほかのキーは触らない）
        self.s["dialogue"] = {**(self.s.get("dialogue") or {}), **dlg_opts}
        if (self.dialogue_ctl is not None and bool(dlg_opts.get("headset_button", old_headset)) != old_headset
                and self.dialogue_ctl.active):
            self.dialogue_ctl.apply_headset_setting()      # セッション中なら、ヘッドセットの独占を今すぐ登録・解除
        self._save_settings()
        if size_changed:
            self.apply_display_size(v["display_size"])
        if dev_changed:
            self.svc.session.input_device_changed()
        if changed_hk:
            self.apply_hotkeys()
        self._touch()
        self.statusBar().showMessage("設定を保存しました", 5000)

    def apply_hotkeys(self) -> None:
        from hotkeys import HotkeyManager
        if self.hotkey_mgr:
            self.hotkey_mgr.stop()
        self.hotkey_mgr = HotkeyManager(
            {"record": str(self.s.get("hotkey_record", "")), "pause": str(self.s.get("hotkey_pause", "")),
             "show": str(self.s.get("hotkey_show", "")),
             "dialogue": str(self.s.get("hotkey_dialogue_commit", ""))},
            callback=lambda name: self.bridge.hotkey.emit(name))
        res = self.hotkey_mgr.start()
        errs = [f"{k}: {v}" for k, v in res.items() if v]
        if errs:
            self._warn( "ホットキーを登録できません", "\n".join(errs))
        elif res:
            self.statusBar().showMessage("ホットキーを登録しました: " + " / ".join(
                f"{k}={self.s.get('hotkey_dialogue_commit' if k == 'dialogue' else 'hotkey_' + k)}" for k in res), 8000)

    @safe
    def _on_hotkey(self, name: str) -> None:
        if name == "record":
            self.toggle_record()
        elif name == "pause":          # 保留／保留を解除
            self.toggle_pause()
        elif name == "show":
            self.toggle_window()
        elif name == "dialogue":
            if self.dialogue_panel is not None:
                self.dialogue_panel.commit_from_hotkey()
