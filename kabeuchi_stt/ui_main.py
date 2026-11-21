# -*- coding: utf-8 -*-
"""メイン窓（PySide6）。画面の組み立てと操作。ロジックは各コアモジュールに任せる。

スレッドの扱い: 清書キュー・逐次認識・ホットキーのコールバックは別スレッドから来るので、
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
                               QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QMenu, QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QSlider, QSpinBox, QSplitter, QSystemTrayIcon,
                               QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

import ui_icons as I
import ui_style as S
from finalize_queue import CANCELLED, DONE, ERROR, PENDING, RUNNING, Job
from geometry_util import clamp_geometry, parse_geometry
from history import History, Record, snippet
from meter_state import HoldLatch, meter_unrecorded
from mic_check import Issue
from paragraph import format_for_output
from playback_devices import combo_entries
from presets import build_prompt, load_presets, save_presets
from replacements import save_pairs, suggest_replacements
from services import Services
from settings import resolve_dir, save_settings
from timeline import Timeline
from ui_menu import COPIED_MESSAGE, MenuItem, SEP, history_menu, is_text_row, timeline_menu
from ui_widgets import (AudioPlayer, CheckBox, ChipsWidget, ClickableText, ElidedLabel, MeterWidget,
                        TimelineView, WrapBarLayout)
from version import VERSION
from viewer_core import block_for_time, seek_time_for_block, viewer_segments

log = logging.getLogger("kabeuchi")

NORMAL_DEFAULT = (1320, 820)
COMPACT_DEFAULT = (420, 300)
SIDE_PANEL_MIN_WINDOW_W = 780   # この幅より狭い通常表示では、右パネルを自動で隠す
IDLE_BAND_SEC = 20              # 「録音していません」の帯を出しておく秒数


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
    mic_test = Signal(object)


def _pill(bg: str, fg: str, bold: bool = True) -> str:
    return (f"background: {bg}; color: {fg}; border-radius: 15px; padding: 3px 14px;"
            + (" font-weight: bold;" if bold else ""))


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, devices: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(560)
        f = QFormLayout(self)
        f.setLabelAlignment(Qt.AlignRight)
        self.size_combo = QComboBox()
        for key, label in S.SIZE_LABELS.items():
            self.size_combo.addItem(label, key)
        cur_size = str(settings.get("display_size", S.DEFAULT_SIZE))
        self.size_combo.setCurrentIndex(max(0, self.size_combo.findData(cur_size)))
        self.final_model = QComboBox()
        self.final_model.setEditable(True)
        self.final_model.addItems(["base", "small", "medium", "large-v3"])
        self.final_model.setCurrentText(str(settings.get("final_model", "small")))
        self.live_model = QComboBox()
        self.live_model.setEditable(True)
        self.live_model.addItems(["tiny", "base", "small"])
        self.live_model.setCurrentText(str(settings.get("live_model", "base")))
        self.device = QComboBox()
        self.device.addItem("既定のマイク", None)
        cur = settings.get("input_device")
        for d in devices:
            self.device.addItem(f"{d['index']}: {d['name']} [{d['hostapi']}]", d["index"])
            if cur is not None and (cur == d["index"] or str(cur).lower() in d["name"].lower()):
                self.device.setCurrentIndex(self.device.count() - 1)
        self.silence = QSpinBox()
        self.silence.setRange(0, 7200)
        self.silence.setSuffix(" 秒（0=無効）")
        self.silence.setValue(int(settings.get("silence_stop_sec", 180)))
        self.hk_rec = QLineEdit(str(settings.get("hotkey_record", "")))
        self.hk_rec.setPlaceholderText("例 Ctrl+Alt+F9（空=未割り当て）")
        self.hk_pause = QLineEdit(str(settings.get("hotkey_pause", "")))
        self.hk_pause.setPlaceholderText("例 Ctrl+Alt+F8（空=未割り当て）")
        self.hk_show = QLineEdit(str(settings.get("hotkey_show", "")))
        self.hk_show.setPlaceholderText("例 Ctrl+Alt+F10（空=未割り当て）")
        self.auto_copy = CheckBox("清書が終わったらクリップボードへ全文をコピー")
        self.auto_copy.setChecked(bool(settings.get("auto_copy", True)))
        # 待機中のマイク監視
        self.idle_monitor = CheckBox("待機中もマイクを確認する（窓が見えている間だけ。保存・認識はしない）")
        self.idle_monitor.setChecked(bool(settings.get("idle_monitor", True)))
        self.idle_timeout = QSpinBox()
        self.idle_timeout.setRange(0, 86400)
        self.idle_timeout.setSuffix(" 秒（0=閉じない）")
        self.idle_timeout.setValue(int(settings.get("idle_monitor_timeout_sec", 300)))
        self.idle_prompt = CheckBox("録音していないのに話しているとき「録音していません」を出す")
        self.idle_prompt.setChecked(bool(settings.get("idle_prompt_enabled", True)))
        self.idle_cooldown = QSpinBox()
        self.idle_cooldown.setRange(5, 3600)
        self.idle_cooldown.setSuffix(" 秒")
        self.idle_cooldown.setValue(int(settings.get("idle_prompt_cooldown_sec", 60)))
        # 清書の改行
        self.para_on = CheckBox("清書の出力をすき間の長さで改行する（txt / md / クリップボード）")
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
        f.addRow("表示サイズ", self.size_combo)
        f.addRow("清書モデル", self.final_model)
        f.addRow("逐次認識モデル", self.live_model)
        f.addRow("マイク", self.device)
        f.addRow("無音で自動停止", self.silence)
        f.addRow("", self.idle_monitor)
        f.addRow("モニターを閉じるまで", self.idle_timeout)
        f.addRow("", self.idle_prompt)
        f.addRow("指摘の間隔", self.idle_cooldown)
        f.addRow("", self.para_on)
        f.addRow("改行（句点のあと）", self.para_gap)
        f.addRow("改行（必ず）", self.para_force)
        f.addRow("ホットキー: 録音の開始/停止", self.hk_rec)
        f.addRow("ホットキー: 一時停止/再開", self.hk_pause)
        f.addRow("ホットキー: 窓の表示/非表示", self.hk_show)
        f.addRow("", self.auto_copy)
        f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("保存")
        bb.button(QDialogButtonBox.Cancel).setText("キャンセル")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self) -> dict:
        gap, force = self.para_gap.value(), self.para_force.value()
        return {
            "display_size": self.size_combo.currentData(),
            "final_model": self.final_model.currentText().strip() or "small",
            "live_model": self.live_model.currentText().strip() or "base",
            "input_device": self.device.currentData(),
            "silence_stop_sec": self.silence.value(),
            "idle_monitor": self.idle_monitor.isChecked(),
            "idle_monitor_timeout_sec": self.idle_timeout.value(),
            "idle_prompt_enabled": self.idle_prompt.isChecked(),
            "idle_prompt_cooldown_sec": self.idle_cooldown.value(),
            "paragraph_enabled": self.para_on.isChecked(),
            "paragraph_gap_sec": gap,
            "paragraph_force_gap_sec": max(gap, force),
            "hotkey_record": self.hk_rec.text().strip(),
            "hotkey_pause": self.hk_pause.text().strip(),
            "hotkey_show": self.hk_show.text().strip(),
            "auto_copy": self.auto_copy.isChecked(),
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
        self.setWindowTitle(f"壁打ち文字起こし  v{VERSION}")
        self.setWindowIcon(S.make_icon())
        self.setMinimumSize(380, 220)
        self._quitting = False
        self._last_text = ""            # 最後に清書が終わった全文（「送る」の対象）
        self._current_rec: Record | None = None
        self._edit_dirty = False
        self._loading_editor = False
        self._mic_test_info = ""
        self._mic_test_until = 0.0      # マイクテストの結果を、この時刻まで監視の表示で上書きしない
        self._issues: list[Issue] = []
        self._tick_n = 0
        self._tray: QSystemTrayIcon | None = None
        self.hotkey_mgr = None
        self._compact = bool(self.s.get("compact_mode", False))
        self._pinned = bool(self.s.get("always_on_top", False))
        self._last_activity = time.monotonic()
        self._idle_band_until = 0.0
        self._unrec_latch = HoldLatch(0.5)
        self._menu_source = "timeline"
        self._menu_exec = lambda menu, pos: menu.exec(pos)   # テストでは差し替える（実画面にメニューを出さない）
        self._geo_ready = False
        self._geo_saved: dict[str, list] = {}

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
        self.timeline_view.set_timeline(self.svc.session.timeline)
        self.reload_history()
        self._update_queue_ui()
        self._apply_mode_widgets()
        if str(self.s.get("display_size", S.DEFAULT_SIZE)) != S.DISPLAY_SIZE:
            self.apply_display_size(str(self.s.get("display_size", S.DEFAULT_SIZE)))
        self._restore_geometry()
        self._geo_ready = True
        self._apply_responsive()
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
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_main_tab(), "壁打ち")
        self.tabs.addTab(self._build_history_tab(), "履歴・検索")
        self.player = AudioPlayer(self, volume=volume, provider=device_provider,
                                  selected_id=str(self.s.get("playback_device", "")), muted=muted)
        self.statusBar().showMessage("待機中")
        self._fill_out_combo()

    def _frame(self) -> QFrame:
        f = QFrame()
        f.setObjectName("bar")
        f.setAttribute(Qt.WA_StyledBackground, True)
        f.setStyleSheet(f"#bar {{ background: {S.PANEL}; border: 1px solid {S.BORDER}; border-radius: 10px; }}")
        return f

    def _time_style(self, pt: int) -> str:
        return f"font-family: Consolas; font-size: {pt}pt; font-weight: bold; color: {S.TEXT};"

    def _fit_time_label(self) -> None:
        """00:00:00 が必ず収まる幅を、実際の描画フォントから決める（切れない・重ならない）。"""
        lb = self.lbl_time
        lb.setStyleSheet(self._time_style(S.TIME_PT_COMPACT if self._compact else S.TIME_PT))
        lb.ensurePolished()
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(lb.font())
        w = max(fm.horizontalAdvance("00:00:00"), fm.horizontalAdvance("88:88:88")) + 10
        lb.setFixedWidth(w)
        lb.setFixedHeight(fm.height() + 6)

    def _build_main_tab(self) -> QWidget:
        w = QWidget()
        self.main_tab = w
        root = QVBoxLayout(w)
        self.main_root = root
        root.setContentsMargins(10, 10, 10, 8)
        root.setSpacing(8)

        # --- 上段（幅が足りなければ折り返す。時間表示は固定幅で切れない）
        top = self._frame()
        self.top_bar = top
        tl = WrapBarLayout(top, hspace=12, vspace=8)
        tl.setContentsMargins(12, 10, 12, 10)
        self.top_layout = tl
        self.btn_rec = QPushButton("録音開始")
        self.btn_rec.setObjectName("rec")
        self.btn_rec.setCursor(Qt.PointingHandCursor)
        self.btn_pause = QPushButton("一時停止")
        self.btn_pause.setObjectName("pause")
        self.btn_pause.setCursor(Qt.PointingHandCursor)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip("録音を一時停止 / 再開（同じ WAV に続けます。休止中は経過時間も止まります）")
        self.lbl_time = QLabel("00:00:00")
        self.meter = MeterWidget()
        self.badge = ElidedLabel("", pad=34)
        self.badge.setMinimumHeight(30)
        self.badge.setMaximumWidth(380)
        self.btn_mictest = QPushButton("マイクテスト")
        self.btn_wav = QPushButton("WAV清書")
        self.btn_folder = QPushButton("フォルダ")
        self.btn_settings = QPushButton("設定")
        self.btn_compact = QPushButton("コンパクト")
        self.btn_compact.setCheckable(True)     # コンパクト表示中は ON（アクセント色）
        self.btn_pin = QPushButton("最前面")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(self._pinned)
        self._btn_mode = "label"   # "label"=アイコン+ラベル / "icon"=アイコンのみ（ツールチップ）
        self._fit_time_label()
        # 道具ボタンは 1 つのグループにして、幅が足りなければグループごと次の段へ（1 つだけ取り残されない）
        tools = QWidget()
        self.tools_group = tools
        tg = WrapBarLayout(tools, hspace=8, vspace=6)
        for b in (self.btn_mictest, self.btn_wav, self.btn_folder, self.btn_settings, self.btn_compact, self.btn_pin):
            tg.add(b)
        tl.add(self.btn_rec)
        tl.add(self.btn_pause)
        tl.add(self.lbl_time)
        tl.add(self.meter, grow=True)
        tl.add(self.badge)
        tl.add(tools)
        root.addWidget(top)

        # 「録音していません（話しているようです）」の帯（待機中モニターが話し声を検出したとき）
        band = QFrame()
        band.setObjectName("idleBand")
        band.setAttribute(Qt.WA_StyledBackground, True)
        band.setStyleSheet(f"#idleBand {{ background: {S.WARN_BG}; border: 1px solid {S.ORANGE}; border-radius: 8px; }}"
                           f" #idleBand QLabel {{ background: transparent; color: #ffd9a0; font-weight: bold; }}")
        bl = WrapBarLayout(band, hspace=12, vspace=6)
        bl.setContentsMargins(12, 7, 12, 7)
        self.idle_label = ElidedLabel("録音していません（話しているようです）")
        self.btn_idle_rec = QPushButton("録音開始")
        self.btn_idle_rec.setObjectName("recSmall")
        self.btn_idle_rec.setCursor(Qt.PointingHandCursor)
        self.btn_idle_close = QPushButton()
        self.btn_idle_close.setObjectName("ghostIcon")
        self.btn_idle_close.setIcon(I.make_icon("close"))
        self.btn_idle_close.setIconSize(QSize(14, 14))
        self.btn_idle_close.setFixedSize(30, 28)
        self.btn_idle_close.setToolTip("閉じる")
        self._band_kind = "idle"
        bl.add(self.idle_label, grow=True)
        bl.add(self.btn_idle_rec)
        bl.add(self.btn_idle_close)
        band.setVisible(False)
        self.idle_band = band
        root.addWidget(band)

        # マイクの指摘（具体的な対処つき）。録音は止めない
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.setVisible(False)
        self.banner.setContentsMargins(14, 8, 14, 8)
        root.addWidget(self.banner)

        opt = QWidget()
        self.opt_row = opt
        ol = WrapBarLayout(opt, hspace=14, vspace=4)
        self.chk_live = CheckBox("逐次認識（録音中に仮の文字を出す）")
        self.chk_live.setChecked(bool(self.s.get("live_enabled", True)))
        self.chk_live.setToolTip("録音中でも切り替えられます（ON にするとその時点から仮の文字を出します。録音は止まりません）")
        self.lbl_live_state = ElidedLabel("")
        self.lbl_live_state.setStyleSheet(f"color: {S.MUTED};")
        legend = QLabel(f'<span style="color:{S.TEAL}">■ 波形（未認識）</span>　'
                        f'<span style="color:{S.ORANGE}">■ 仮認識</span>　'
                        f'<span style="color:#ffffff">■ 清書済み</span>')
        legend.setTextFormat(Qt.RichText)
        ol.add(self.chk_live)
        ol.add(self.lbl_live_state, grow=True)
        ol.add(legend)
        root.addWidget(opt)

        # --- 中央: タイムライン | 右パネル
        split = QSplitter(Qt.Horizontal)
        self.split = split
        self.timeline_view = TimelineView()
        self.timeline_view.setMinimumWidth(220)
        split.addWidget(self.timeline_view)
        self.side_scroll = self._build_side_panel()
        split.addWidget(self.side_scroll)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([860, 420])
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)

        # --- 下段: 清書キュー（通常）／ 1行の要約（コンパクト）
        self.queue_group = self._build_queue_panel()
        root.addWidget(self.queue_group)
        self.lbl_queue_compact = ElidedLabel("待機中（清書なし）")
        self.lbl_queue_compact.setStyleSheet(f"color: {S.MUTED}; padding: 0 4px;")
        root.addWidget(self.lbl_queue_compact)
        return w

    def _build_side_panel(self) -> QWidget:
        host = QWidget()
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 14, 8, 4)
        lay.setSpacing(10)

        # 専門用語
        g = QGroupBox("専門用語（認識の手がかり）")
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
        lay.addWidget(g)

        # 置換辞書
        g = QGroupBox("自動置換辞書（誤認識 → 正解）")
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
        hint = QLabel("長い誤認識が優先・1回だけ適用。清書と仮認識の両方に効きます。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {S.MUTED}; font-size: {S.BASE_PT - 1}pt;")
        self.hint_dict = hint
        gl.addWidget(self.tbl)
        gl.addLayout(row)
        gl.addWidget(hint)
        lay.addWidget(g)
        self._fill_dict_table()

        # Copilot プリセット
        g = QGroupBox("Copilot プロンプト（クリップボードへ送る）")
        gl = QVBoxLayout(g)
        self.cmb_preset = QComboBox()
        self.ed_pname = QLineEdit()
        self.ed_pname.setPlaceholderText("プリセット名")
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlaceholderText("プロンプト本文")
        self.ed_prompt.setMinimumHeight(110)
        row = QHBoxLayout()
        self.btn_p_new = QPushButton("新規として追加")
        self.btn_p_save = QPushButton("保存")
        self.btn_p_del = QPushButton("削除")
        self.btn_p_del.setObjectName("danger")
        for b in (self.btn_p_new, self.btn_p_save, self.btn_p_del):
            row.addWidget(b)
        self.btn_send = QPushButton("プロンプト＋文字起こしをコピー")
        self.btn_send.setObjectName("primary")
        gl.addWidget(self.cmb_preset)
        gl.addWidget(self.ed_pname)
        gl.addWidget(self.ed_prompt)
        gl.addLayout(row)
        gl.addWidget(self.btn_send)
        lay.addWidget(g)
        lay.addStretch(1)
        self.presets = load_presets()
        self._fill_presets()

        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.setWidget(host)
        sc.setMinimumWidth(360)
        return sc

    def _build_queue_panel(self) -> QWidget:
        g = QGroupBox("清書キュー")
        lay = QVBoxLayout(g)
        lay.setContentsMargins(10, 6, 10, 8)
        row = WrapBarLayout(hspace=10, vspace=4)
        self.lbl_queue = ElidedLabel("待機中（清書なし）")
        self.prog = QProgressBar()
        self.prog.setRange(0, 100)
        self.prog.setValue(0)
        self.prog.setMaximumWidth(360)
        self.prog.setMinimumWidth(120)
        self.btn_q_cancel = QPushButton("実行中を中断")
        self.btn_q_cancel.setEnabled(False)
        row.add(self.lbl_queue, grow=True)
        row.add(self.prog)
        row.add(self.btn_q_cancel)
        lay.addLayout(row)
        self.lst_queue = QListWidget()
        self.lst_queue.setFixedHeight(84)
        self.lst_queue.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst_queue.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.lst_queue)
        return g

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        self.history_page = w
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 8)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        lay.addWidget(split)
        # 左: 検索＋一覧
        left = QWidget()
        left.setMinimumWidth(160)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("全文検索（空白区切りで AND）　例: 予算 自動化")
        self.ed_search.setClearButtonEnabled(True)
        self.lst_hist = QListWidget()
        self.lst_hist.setSpacing(2)
        self.lst_hist.setWordWrap(True)
        self.lst_hist.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lbl_hist_count = QLabel("")
        self.lbl_hist_count.setStyleSheet(f"color: {S.MUTED};")
        ll.addWidget(self.ed_search)
        ll.addWidget(self.lbl_hist_count)
        ll.addWidget(self.lst_hist, 1)
        split.addWidget(left)
        # 右: 閲覧
        right = QWidget()
        right.setMinimumWidth(240)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
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
        bl = WrapBarLayout(bw, hspace=8, vspace=6)
        self.btn_h_save = QPushButton("修正を保存")
        self.btn_h_save.setObjectName("primary")
        self.btn_h_revert = QPushButton("元の清書に戻す")
        self.btn_h_dict = QPushButton("直した語を辞書へ…")
        self.btn_h_redo = QPushButton("WAVから清書をやり直す")
        self.btn_h_send = QPushButton("Copilot用にコピー")
        self.btn_h_del = QPushButton("削除（ごみ箱へ）")
        self.btn_h_del.setObjectName("danger")
        for b in (self.btn_h_save, self.btn_h_revert, self.btn_h_dict, self.btn_h_redo, self.btn_h_send, self.btn_h_del):
            bl.add(b)
        rl.addWidget(self.lbl_rec_meta)
        rl.addWidget(self.editor, 1)
        rl.addWidget(pw)
        rl.addWidget(self.lbl_out_notice)
        rl.addWidget(bw)
        split.addWidget(right)
        split.setSizes([420, 860])
        self._set_viewer_enabled(False)
        return w

    # =============================================================== 接続
    def _connect(self) -> None:
        self.btn_rec.clicked.connect(lambda: self.toggle_record())
        self.btn_pause.clicked.connect(lambda: self.toggle_pause())
        self.btn_idle_rec.clicked.connect(lambda: self.start_from_idle_band())
        self.btn_idle_close.clicked.connect(lambda: self.hide_idle_band())
        self.btn_mictest.clicked.connect(lambda: self.start_mic_test())
        self.btn_wav.clicked.connect(lambda: self.pick_wav())
        self.btn_folder.clicked.connect(lambda: self.open_out())
        self.btn_settings.clicked.connect(lambda: self.open_settings())
        self.btn_compact.clicked.connect(lambda: self.set_compact(not self._compact))
        self.btn_pin.toggled.connect(lambda v: self.set_always_on_top(bool(v)))
        self.btn_q_cancel.clicked.connect(lambda: self.cancel_current())
        self.chk_live.toggled.connect(lambda v: self._set_live(v))
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
        self.btn_send.clicked.connect(lambda: self.send_main())
        # 履歴
        self.ed_search.textChanged.connect(lambda t: self._search_later())
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(lambda: self.reload_history())
        self.lst_hist.currentItemChanged.connect(lambda cur, prev: self.history_selected(cur))
        self.editor.lineClicked.connect(lambda i: self.editor_line_clicked(i))
        self.editor.contextRequested.connect(lambda i, sel, pos: self._on_history_context(i, sel, pos))
        self.timeline_view.contextRequested.connect(lambda i, pos: self._on_timeline_context(i, pos))
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
        self.btn_h_send.clicked.connect(lambda: self.send_history())
        self.btn_h_del.clicked.connect(lambda: self.delete_record())
        # スレッド橋渡し
        self.bridge.job_event.connect(self._on_job_event)
        self.bridge.status.connect(lambda m: self.statusBar().showMessage(m, 10000))
        self.bridge.hotkey.connect(self._on_hotkey)
        self.bridge.mic_test.connect(self._on_mic_test)

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(S.make_icon(), self)
        self._tray.setToolTip(f"壁打ち文字起こし v{VERSION}")
        m = QMenu()
        self.act_toggle = QAction("画面を表示 / 隠す", m)
        self.act_toggle.triggered.connect(lambda: self.toggle_window())
        self.act_rec = QAction("録音を開始", m)
        self.act_rec.triggered.connect(lambda: self.toggle_record())
        self.act_pause = QAction("一時停止", m)
        self.act_pause.setEnabled(False)
        self.act_pause.triggered.connect(lambda: self.toggle_pause())
        self.act_compact = QAction("コンパクト表示にする", m)
        self.act_compact.triggered.connect(lambda: self.set_compact(not self._compact))
        a_dict = QAction("辞書・専門用語を開く", m)
        a_dict.triggered.connect(lambda: self.show_main(tab=0))
        a_hist = QAction("履歴・検索を開く", m)
        a_hist.triggered.connect(lambda: self.show_main(tab=1))
        a_set = QAction("設定…", m)
        a_set.triggered.connect(lambda: self.open_settings())
        a_quit = QAction("終了", m)
        a_quit.triggered.connect(lambda: self.quit_app())
        for a in (self.act_toggle, self.act_rec, self.act_pause, self.act_compact, a_dict, a_hist, a_set):
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
            if self._compact:       # 履歴・辞書は通常表示でだけ
                self.set_compact(False)
            self.tabs.setCurrentIndex(tab)
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

    def _geo_key(self, compact: bool | None = None) -> str:
        return "win_geometry_compact" if (self._compact if compact is None else compact) else "win_geometry_normal"

    def _current_rect(self) -> tuple[int, int, int, int] | None:
        if self.isMinimized() or self.isFullScreen():
            return None
        g = self.normalGeometry() if self.isMaximized() else self.geometry()
        return g.x(), g.y(), g.width(), g.height()

    def _save_geometry(self, compact: bool | None = None) -> None:
        """いまの位置とサイズを、現在のモード用に設定へ保存する（変わっていなければ書かない）。"""
        if not self._geo_ready:
            return
        r = self._current_rect()
        if r is None:
            return
        key = self._geo_key(compact)
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
        w, h = COMPACT_DEFAULT if self._compact else NORMAL_DEFAULT
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
            self._apply_responsive()

    def _apply_responsive(self) -> None:
        """通常表示で幅が狭いときは右パネルを隠す（タイムラインを読めるように）。"""
        show = (not self._compact) and self.width() >= SIDE_PANEL_MIN_WINDOW_W
        if self.side_scroll.isHidden() == show:
            self.side_scroll.setVisible(show)

    def _rec_texts(self) -> tuple[str, str]:
        return ("録音", "停止") if self._compact else ("録音開始", "停止して清書")

    # ---- 表示サイズ（小/標準/大）。再起動せずに反映する
    def apply_display_size(self, name: str) -> None:
        name = S.set_size(name)
        self.s["display_size"] = name
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(S.QSS)
            app.setFont(S.app_font())
        self._fit_time_label()
        self.timeline_view.refresh_fonts()
        self.editor.setStyleSheet(f"QPlainTextEdit {{ font-size: {S.BASE_PT + 1}pt; padding: 8px; }}")
        self.hint_dict.setStyleSheet(f"color: {S.MUTED}; font-size: {S.BASE_PT - 1}pt;")
        for w in self.findChildren(ElidedLabel):
            w._apply()
        for w in self.findChildren(QWidget):
            w.updateGeometry()
        self._style_buttons()
        self.top_layout.invalidate()
        self.main_root.invalidate()

    # ---- ボタンの見た目（アイコンはコード描画。2 案: label=アイコン+ラベル / icon=アイコンのみ）
    def _style_btn(self, b: QPushButton, icon_name: str, label: str, tip: str, icon_only: bool) -> None:
        name = "ghostIcon" if icon_only else "ghost"
        if b.objectName() != name:
            b.setObjectName(name)
            b.style().unpolish(b)
            b.style().polish(b)
        b.setIcon(I.make_icon(icon_name))
        b.setIconSize(QSize(16, 16))
        b.setText("" if icon_only else label)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        if icon_only:
            b.setFixedSize(36, 34)
        else:
            b.setMinimumSize(0, 0)
            b.setMaximumSize(16777215, 16777215)

    def apply_button_style(self, mode: str) -> None:
        """上段の補助ボタンの見た目。label=アイコン+短いラベル / icon=アイコンのみ+ツールチップ。"""
        self._btn_mode = "icon" if mode == "icon" else "label"
        self._style_buttons()

    def _style_buttons(self) -> None:
        c = self._compact
        io = self._btn_mode == "icon"
        self._style_btn(self.btn_mictest, "mic", "マイクテスト", "マイクテスト（3秒）: 話しながら押すと、音量を診断します", io)
        self._style_btn(self.btn_wav, "wave", "WAV清書", "WAV ファイルを選んで清書する", io)
        self._style_btn(self.btn_folder, "folder", "フォルダ", "出力フォルダ（録音・文字起こし）を開く", io)
        self._style_btn(self.btn_settings, "gear", "設定", "設定", io)
        # コンパクトと最前面は、狭いコンパクト表示では常にアイコンのみ。トグル状態は ON のときアクセント色
        self._style_btn(self.btn_compact, "expand" if c else "compact", "コンパクト",
                        "通常表示に戻す" if c else "コンパクト表示（録音・時間・メーター・直近の行だけの小さな窓）", io or c)
        self.btn_compact.blockSignals(True)
        self.btn_compact.setChecked(c)
        self.btn_compact.blockSignals(False)
        self._style_btn(self.btn_pin, "pin", "最前面",
                        "最前面を解除" if self.btn_pin.isChecked() else "他の窓より前に固定（前面化の強制はしません）", io or c)
        self.top_layout.invalidate()
        self.tools_group.updateGeometry()

    def _apply_mode_widgets(self) -> None:
        c = self._compact
        for w in (self.btn_mictest, self.btn_wav, self.btn_folder, self.btn_settings, self.opt_row,
                  self.queue_group):
            w.setVisible(not c)
        self.lbl_queue_compact.setVisible(c)
        self.tabs.tabBar().setVisible(not c)
        if c:
            self.tabs.setCurrentIndex(0)
        self.timeline_view.set_compact(c)
        # QTabWidget の最小サイズは隠れているページも含めて決まる。コンパクトでは履歴ページを無視させて小さくできるようにする
        self.history_page.setSizePolicy(QSizePolicy.Ignored if c else QSizePolicy.Preferred,
                                        QSizePolicy.Ignored if c else QSizePolicy.Preferred)
        self._refresh_band_texts()
        self._style_buttons()
        m = 6 if c else 10
        self.main_root.setContentsMargins(m, m, m, 6 if c else 8)
        self.main_root.setSpacing(5 if c else 8)
        self.top_layout.setContentsMargins(8 if c else 12, 6 if c else 10, 8 if c else 12, 6 if c else 10)
        self.btn_rec.setMinimumWidth(0 if c else 150)
        self._fit_time_label()
        self._set_recording_ui(self.svc.session.is_recording)
        self._apply_responsive()
        self._set_issues(self._issues)
        if hasattr(self, "act_compact"):
            self.act_compact.setText("通常表示にする" if c else "コンパクト表示にする")
        self.top_layout.invalidate()

    @safe
    def set_compact(self, on: bool) -> None:
        """通常 / コンパクトの切り替え。それぞれの位置とサイズを保存・復元する。"""
        on = bool(on)
        if on == self._compact:
            return
        self._save_geometry()                 # 今のモードの位置を保存してから切り替える
        self._geo_ready = False
        try:
            keep = self.geometry()
            self._compact = on
            self.s["compact_mode"] = on
            self._apply_mode_widgets()
            saved = parse_geometry(self.s.get(self._geo_key()))
            if saved is None:                  # 初めてのモード: いまの左上のまま、既定サイズ
                dw, dh = COMPACT_DEFAULT if on else NORMAL_DEFAULT
                saved = (keep.x(), keep.y(), dw, dh)
            self.setGeometry(*clamp_geometry(saved, self._screens()))
            self._apply_responsive()          # 新しい幅で右パネルの出し入れを決め直す
        finally:
            self._geo_ready = True
        self._save_settings()
        self._save_geometry()

    @safe
    def set_always_on_top(self, on: bool) -> None:
        """最前面のピン。フラグを変えるとウィンドウが作り直されるので、位置・状態を戻して show し直す。
        前面化の強制（SetForegroundWindow 等）はしない（アクティブ化せずに表示するだけ）。"""
        on = bool(on)
        if self.btn_pin.isChecked() != on:
            self.btn_pin.blockSignals(True)
            self.btn_pin.setChecked(on)
            self.btn_pin.blockSignals(False)
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
        if self._tray is None and not self._quitting:
            # --no-tray（tray-tools から起こした場合など）: 窓を閉じるとプロセスが終わる（main が
            # setQuitOnLastWindowClosed(True) にしている）。録音中・清書中に誤って閉じて失わないよう確かめる。
            msg = self._busy_messages()
            if msg and self._question("終了", "\n".join(msg) + "\n\n窓を閉じると終了します。終了しますか？") != QMessageBox.Yes:
                ev.ignore()
                return
        self._save_geometry()
        self._geo_ready = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(ev)

    def _busy_messages(self) -> list:
        """終了すると失われるものの説明（録音中・清書が残っている）。無ければ空。"""
        msg = []
        if self.svc.session.is_recording:
            msg.append("録音中です。止めて終了します（WAV は残ります）。")
        if self.svc.queue.busy:
            msg.append("清書が残っています。終了すると中断されます（あとで履歴から「WAVから清書」できます）。")
        return msg

    @safe
    def quit_app(self) -> None:
        sess, q = self.svc.session, self.svc.queue
        msg = self._busy_messages()
        if msg and self._question( "終了", "\n".join(msg) + "\n\n終了しますか？") != QMessageBox.Yes:
            return
        self._quitting = True
        self._save_geometry()
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

    # =============================================================== 録音
    def _set_live(self, v: bool) -> None:
        """逐次認識の ON/OFF。録音中なら、その場で切り替える（録音・WAV には触れない）。"""
        self.s["live_enabled"] = bool(v)
        sess = self.svc.session
        if sess.is_recording:
            try:
                sess.set_live(bool(v))
            except Exception:
                log.exception("録音中の逐次認識の切替")
                self.statusBar().showMessage("逐次認識を切り替えられませんでした（録音は続いています）", 8000)
                return
            self.lbl_live_state.setText("" if v else "逐次認識: OFF")
            self.statusBar().showMessage("逐次認識を始めました（この時点から）" if v else "逐次認識を止めました（録音は続いています）", 6000)

    def _set_recording_ui(self, on: bool) -> None:
        idle_t, rec_t = self._rec_texts()
        self.btn_rec.setObjectName("recStop" if on else "rec")
        self.btn_rec.setText(rec_t if on else idle_t)
        self.btn_rec.setIcon(I.make_icon("stop", fixed_color=S.REC) if on else I.make_icon("rec", fixed_color="#ffffff"))
        self.btn_rec.setIconSize(QSize(14, 14))
        self.btn_rec.style().unpolish(self.btn_rec)
        self.btn_rec.style().polish(self.btn_rec)
        self.btn_mictest.setEnabled(not on)
        # 逐次認識のチェックは録音中でも切り替えられる（録音自体は止めない）
        self.btn_pause.setEnabled(on)
        self._set_paused_ui(on and self.svc.session.is_paused)
        if self._tray is not None:
            self.act_rec.setText("録音を停止" if on else "録音を開始")
            self.act_pause.setEnabled(on)
            self._tray.setIcon(S.make_icon(on))

    def _set_paused_ui(self, paused: bool) -> None:
        self.btn_pause.setObjectName("pauseOn" if paused else "pause")
        self.btn_pause.setText("再開" if paused else "一時停止")
        self.btn_pause.setIcon(I.make_icon("play", fixed_color=S.ORANGE) if paused else I.make_icon("pause"))
        self.btn_pause.setIconSize(QSize(14, 14))
        self.btn_pause.style().unpolish(self.btn_pause)
        self.btn_pause.style().polish(self.btn_pause)
        if self._tray is not None:
            self.act_pause.setText("再開" if paused else "一時停止")
            if self.svc.session.is_recording:
                self._tray.setToolTip(f"壁打ち文字起こし v{VERSION}" + ("（一時停止中）" if paused else "（録音中）"))
            else:
                self._tray.setToolTip(f"壁打ち文字起こし v{VERSION}")

    @safe
    def toggle_record(self) -> None:
        if self.svc.session.is_recording:
            self.stop_recording("停止")
        else:
            self.start_recording()

    @safe
    def toggle_pause(self) -> None:
        sess = self.svc.session
        if not sess.is_recording:
            return
        paused = sess.toggle_pause()
        self._set_paused_ui(bool(paused))
        self.statusBar().showMessage("一時停止しました（再開すると同じ WAV に続きます）" if paused else "再開しました", 6000)
        self._touch()

    def start_recording(self) -> None:
        sess = self.svc.session
        self.player.stop()
        try:
            wav = sess.start()
        except Exception as e:
            log.exception("録音開始に失敗")
            self._warn( "録音を開始できません", f"{e}\n\nマイクの接続と、設定のマイク選択を確認してください。")
            return
        self.timeline_view.set_timeline(sess.timeline)
        self._mic_test_info = ""
        self.hide_idle_band()
        self._touch()
        self._set_recording_ui(True)
        self.statusBar().showMessage(f"録音中: {wav.name}")

    def stop_recording(self, why: str = "停止") -> None:
        sess = self.svc.session
        tl = sess.timeline
        wav = sess.stop()
        self._set_recording_ui(False)
        self.lbl_live_state.setText("")
        self._touch()
        self._set_issues([])
        if wav is None:
            return
        job = self.svc.queue.submit(Path(wav), timeline=tl)
        self.statusBar().showMessage(f"{why}しました。清書キューに入れました: {Path(wav).name}")
        self._update_queue_ui()

    # ---- 「録音していません」の帯
    def show_idle_band(self, kind: str = "idle") -> None:
        """kind: "idle"=録音していません / "paused"=一時停止中です（話していますが録音されていません）。"""
        self._band_kind = kind
        self._refresh_band_texts()
        self.idle_band.setVisible(True)
        self._idle_band_until = time.monotonic() + IDLE_BAND_SEC

    def _refresh_band_texts(self) -> None:
        c = self._compact
        if self._band_kind == "paused":
            self.idle_label.setText("一時停止中です" if c else "一時停止中です（話していますが録音されていません）")
            self.btn_idle_rec.setText("再開")
            self.btn_idle_rec.setIcon(I.make_icon("play", fixed_color="#ffffff"))
        else:
            self.idle_label.setText("録音していません" if c else "録音していません（話しているようです）")
            self.btn_idle_rec.setText("録音開始")
            self.btn_idle_rec.setIcon(I.make_icon("rec", fixed_color="#ffffff"))
        self.btn_idle_rec.setIconSize(QSize(12, 12))

    @safe
    def hide_idle_band(self) -> None:
        self.idle_band.setVisible(False)
        self._idle_band_until = 0.0

    @safe
    def start_from_idle_band(self) -> None:
        kind = self._band_kind
        self.hide_idle_band()
        sess = self.svc.session
        if kind == "paused":
            if sess.is_recording and sess.is_paused:
                self.toggle_pause()
        elif not sess.is_recording:
            self.start_recording()

    # =============================================================== マイクの指摘
    def _set_issues(self, issues: list[Issue], recording: bool | None = None) -> None:
        """バッジと帯を更新する。recording=None なら実際の状態から決める。
        「監視中」= 録音中、または待機中モニターが入力を開いているとき。"""
        sess = self.svc.session
        self._issues = issues
        parts = []
        rec_now = sess.is_recording if recording is None else recording
        monitoring = rec_now or sess.monitor_active
        if monitoring:
            if issues:
                err = any(i.severity == "error" for i in issues)
                self._set_badge("⚠ " + " / ".join(i.message for i in issues), _pill(S.REC if err else S.ORANGE, "#101010"))
            elif (not rec_now) and sess.idle_monitor.stats() is None:
                self._set_badge("◌ 確認中…", _pill(S.PANEL2, S.MUTED, bold=False))
            else:
                self._set_badge("● 入力OK", _pill("#1f3a2c", S.OKGREEN, bold=False))
        elif sess.monitor_error:
            self._set_badge("⚠ " + sess.monitor_error, _pill(S.PANEL2, "#ffcb7a", bold=False))
        else:
            self._set_badge(self._mic_test_badge(), self._mic_test_badge_style())
        if self._mic_test_info and not rec_now:
            parts.append(f'<span style="color:{S.MUTED}">{self._mic_test_info}</span>')
        for i in issues:
            col = "#ff9a9d" if i.severity == "error" else "#ffcb7a"
            parts.append(f'<span style="color:{col}; font-weight:bold">⚠ {i.message}</span>　{i.advice}')
        self.banner.setVisible(bool(parts) and not self._compact)
        if parts:
            any_err = any(i.severity == "error" for i in issues)
            bg = S.ERR_BG if any_err else S.WARN_BG if issues else S.PANEL
            style = f"background: {bg}; border-radius: 8px; border: 1px solid {S.BORDER};"
            if self.banner.styleSheet() != style:
                self.banner.setStyleSheet(style)
            html = "<br>".join(parts)
            if self.banner.text() != html:
                self.banner.setText(html)

    def _set_badge(self, text: str, style: str) -> None:
        """バッジの文字と見た目。変わったときだけ更新する（毎回スタイルを入れ直すと再描画・再配置が走る）。"""
        if self.badge.styleSheet() != style:
            self.badge.setStyleSheet(style)
        if self.badge.text() != text:
            self.badge.setText(text)

    def _mic_test_badge(self) -> str:
        return getattr(self, "_mic_badge_text", "")

    def _mic_test_badge_style(self) -> str:
        return getattr(self, "_mic_badge_style", "")

    @safe
    def start_mic_test(self) -> None:
        if self.svc.session.is_recording:
            return
        self.btn_mictest.setEnabled(False)
        self.btn_mictest.setText("聞いています…" if self._btn_mode == "label" else "")
        self.btn_mictest.setToolTip("聞いています…（話してください）")

        def work():
            try:
                res = self.svc.session.mic_test()
            except Exception as e:
                res = e
            self.bridge.mic_test.emit(res)
        threading.Thread(target=work, name="kabeuchi-mictest", daemon=True).start()

    @safe
    def _on_mic_test(self, res) -> None:
        self.btn_mictest.setEnabled(True)
        self._style_buttons()
        if isinstance(res, Exception):
            self._mic_test_info = f"マイクテストできませんでした: {res}"
            self._mic_badge_text = "⚠ マイクを開けません"
            self._mic_badge_style = _pill(S.REC, "#101010")
            self._set_issues([])
            return
        stats, issues = res
        self.show_mic_test_result(stats, issues)

    def show_mic_test_result(self, stats, issues) -> None:
        self._mic_test_info = (f"マイクテスト: ピーク {stats.peak_dbfs:.0f} dBFS ／ 無音時の床 {stats.floor_dbfs:.0f} dBFS ／ "
                               f"音割れ {stats.clip_ratio * 100:.2f}%")
        self._mic_test_until = time.monotonic() + 8.0
        if issues:
            self._mic_badge_text = "⚠ " + " / ".join(i.message for i in issues)
            err = any(i.severity == "error" for i in issues)
            self._mic_badge_style = _pill(S.REC if err else S.ORANGE, "#101010")
        else:
            self._mic_badge_text = "● マイクOK"
            self._mic_badge_style = _pill("#1f3a2c", S.OKGREEN, bold=False)
        self._set_issues(issues)

    # =============================================================== 待機中モニター
    def _monitor_wanted(self) -> bool:
        """窓が見えている間だけ。最小化・トレイ格納・一定時間操作なしでは閉じる。"""
        if not bool(self.s.get("idle_monitor", True)):
            return False
        if not self.isVisible() or self.isMinimized():
            return False
        to = float(self.s.get("idle_monitor_timeout_sec", 300))
        if to > 0 and (time.monotonic() - self._last_activity) > to:
            return False
        return True

    def _refresh_mic_ui(self) -> None:
        """待機中・一時停止中のマイク表示（バッジ・帯）を更新。マイクテストの直後は上書きしない。"""
        sess = self.svc.session
        if time.monotonic() < self._mic_test_until:
            return
        if sess.is_recording and not sess.is_paused:
            return  # 録音中は録音の監視（_tick 内）が更新する
        if sess.monitor_active:
            if self.svc.session.hub.seconds_since_audio > 3.0:
                self._issues = []
                self._set_badge("⚠ 音が届いていません", _pill(S.REC, "#101010"))
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
        active = self.isVisible() and not self.isMinimized()
        sess.request_monitor(self._monitor_wanted())
        if sess.is_recording and rec is not None:
            paused = sess.is_paused
            self.lbl_time.setText(fmt_hms(rec.elapsed))
            self.meter.set_level(sess.level)
            self._update_meter_unrecorded(now)
            if paused:
                self.lbl_live_state.setText(f"一時停止中 {fmt_dur(sess.paused_sec)}（録音は止まっています）")
                if self._tick_n % 5 == 0:
                    self._refresh_mic_ui()
                if self._tick_n % 3 == 0:
                    kind = sess.poll_idle_prompt(active)      # 一時停止中に話し声 → 「一時停止中です」
                    if kind:
                        self.show_idle_band(kind)
            else:
                if self._band_kind == "paused" and not self.idle_band.isHidden():
                    self.hide_idle_band()        # 再開したら「一時停止中です」の帯は消す
                if rec.seconds_since_audio > 2.0:
                    self._set_badge("⚠ 音が届いていません", _pill(S.REC, "#101010"))
                elif self._tick_n % 10 == 0:
                    self._set_issues(sess.monitor.issues())
                if sess.silence.should_stop():
                    self.stop_recording("無音が続いたので自動停止")
                    self._notify("自動停止しました", f"{int(self.s.get('silence_stop_sec', 180))}秒以上の無音が続いたため録音を止め、清書に回しました。")
                live = sess.live
                if live is not None:
                    if live.error:
                        self.lbl_live_state.setText("逐次認識は無効です（録音は継続）")
                    else:
                        self.lbl_live_state.setText(f"逐次認識: 処理済み {live.processed_segments}・待ち {live.pending_segments}")
        else:
            self.meter.set_level(sess.level)  # 待機中モニターが開いていれば動く（閉じていれば 0）
            self._update_meter_unrecorded(now)
            if self._tick_n % 3 == 0:
                kind = sess.poll_idle_prompt(active)
                if kind:
                    self.show_idle_band(kind)
            if self._tick_n % 5 == 0:
                self._refresh_mic_ui()
        if self._idle_band_until and now > self._idle_band_until:
            self.hide_idle_band()
        self.timeline_view.refresh()
        if self._tick_n % 3 == 0:
            self._update_queue_ui()

    def _update_meter_unrecorded(self, now: float) -> None:
        """録音していない（待機中・一時停止中）のに話しているとき、メーターを赤にする（即時・保持 0.5 秒）。"""
        sess = self.svc.session
        cond = meter_unrecorded(sess.is_recording, sess.is_paused, sess.monitor_active, bool(sess.monitor_error),
                                sess.speaking_now())
        self.meter.set_unrecorded(self._unrec_latch.update(now, cond))

    # =============================================================== 右クリックメニュー（文字起こし部分）
    def _timeline_rows(self) -> list:
        return self.timeline_view._model._rows

    def timeline_menu_items(self, idx: int, selection: str = "") -> list[MenuItem]:
        return timeline_menu(self._timeline_rows(), idx, selection)

    def _full_text_for_menu(self) -> str:
        """現在の録音の全文。清書済みなら清書の出力と同じ整形（すき間の改行）、前なら見えている行を時刻順に連結。"""
        tl = self.timeline_view._model._tl
        rows = self._timeline_rows()
        if tl is not None and tl.is_final and tl.final_segments():
            return format_for_output(tl.final_segments(), self.s, tl.final_gaps())
        from ui_menu import rows_text_from
        return rows_text_from(rows, 0)

    def _from_here_text(self, idx: int) -> str:
        tl = self.timeline_view._model._tl
        rows = self._timeline_rows()
        if tl is not None and tl.is_final and tl.final_segments() and 0 <= idx < len(rows):
            t0 = rows[idx].start
            segs = tl.final_segments()
            gaps = tl.final_gaps()
            keep = [i for i, sg in enumerate(segs) if sg[0] >= t0 - 1e-6]
            return format_for_output([segs[i] for i in keep], self.s,
                                     None if gaps is None else [gaps[i] if i < len(gaps) else None for i in keep])
        from ui_menu import rows_text_from
        return rows_text_from(rows, idx)

    def _copy_text(self, text: str, key: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(COPIED_MESSAGE.get(key, "コピーしました"), 4000)

    def run_menu_item(self, item: MenuItem) -> None:
        """メニュー項目を実行する（タイムライン・履歴の両方）。"""
        k = item.key
        if k in ("copy_selection", "copy_part"):
            self._copy_text(item.payload, k)
        elif k == "copy_all":
            self._copy_text(self._history_full_text() if self._menu_source == "history" else self._full_text_for_menu(), k)
        elif k == "copy_from_here":
            self._copy_text(self._history_from_here(item.index) if self._menu_source == "history"
                            else self._from_here_text(item.index), k)
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

    @safe
    def _on_timeline_context(self, idx: int, pos) -> None:
        self._menu_source = "timeline"
        sel = ""
        self._show_menu(self.timeline_menu_items(idx, sel), pos)

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
        self._menu_source = "history"
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
            msg = "全文をクリップボードへコピーしました" if self.s.get("auto_copy", True) else "清書が終わりました"
            if self.s.get("auto_copy", True):
                QGuiApplication.clipboard().setText(r.text)
            self._notify("清書が完了しました", f"{job.title}（{r.audio_seconds:.0f}秒の音声を{r.elapsed:.0f}秒で清書）\n{msg}")
            self.statusBar().showMessage(f"清書完了: {job.title}　{msg}", 15000)
            self.reload_history(keep=job.history_id)
        elif job.status == ERROR:
            self._notify("清書できませんでした", f"{job.title}: {job.error.splitlines()[0] if job.error else ''}")
            self.statusBar().showMessage(f"清書エラー: {job.error.splitlines()[0] if job.error else ''}", 20000)
            log.error("清書エラー %s: %s", job.title, job.error)

    def _update_queue_ui(self) -> None:
        q = self.svc.queue
        cur = q.current
        wait = q.waiting
        jobs = q.jobs[-6:][::-1]
        if cur is not None:
            text = f"清書中: {cur.title}（{int(cur.progress * 100)}%）　待機 {wait} 件"
            self.lbl_queue.setText(text)
            self.lbl_queue_compact.setText(f"清書中 {int(cur.progress * 100)}%　待機 {wait} 件")
            self.prog.setValue(int(cur.progress * 100))
        elif wait:
            self.lbl_queue.setText(f"待機 {wait} 件")
            self.lbl_queue_compact.setText(f"清書 待機 {wait} 件")
            self.prog.setValue(0)
        else:
            done = next((j for j in jobs if j.status == DONE), None)
            self.lbl_queue.setText("待機中（清書なし）")
            self.lbl_queue_compact.setText(f"清書完了: {done.title}" if (done and jobs and jobs[0] is done)
                                           else "清書: 待機中（なし）")
            self.prog.setValue(0)
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

    @safe
    def cancel_current(self) -> None:
        cur = self.svc.queue.current
        if cur is not None:
            self.svc.queue.cancel(cur)

    @safe
    def pick_wav(self) -> None:
        p = self._open_file("清書する WAV を選ぶ", str(resolve_dir(self.s.get("out_dir", "out"))),
                            "WAV (*.wav);;すべて (*.*)")
        if p:
            self.svc.queue.submit(Path(p))
            self.statusBar().showMessage(f"清書キューに入れました: {Path(p).name}")

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

    def _compose(self, transcript: str) -> str | None:
        if not transcript.strip():
            self.statusBar().showMessage("送る文字起こしがまだありません（清書が終わるか、履歴から開いてください）", 8000)
            return None
        return build_prompt(self.ed_prompt.toPlainText(), transcript)

    def _main_transcript(self) -> str:
        """「送る」の対象: 今の録音の清書。設定が ON なら、すき間で改行した全文。"""
        tl = self.svc.session.timeline
        if tl.is_final and tl.final_text():
            return format_for_output(tl.final_segments(), self.s, tl.final_gaps())
        return self._last_text

    @safe
    def send_main(self) -> None:
        out = self._compose(self._main_transcript())
        if out is not None:
            QGuiApplication.clipboard().setText(out)
            self.statusBar().showMessage("プロンプト＋文字起こしをクリップボードへコピーしました。Copilot に貼り付けてください", 10000)

    @safe
    def send_history(self) -> None:
        out = self._compose(self.editor.toPlainText())
        if out is not None:
            QGuiApplication.clipboard().setText(out)
            self.statusBar().showMessage("プロンプト＋文字起こしをクリップボードへコピーしました。Copilot に貼り付けてください", 10000)

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
                  self.btn_h_redo, self.btn_h_send, self.btn_h_del):
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
        has_wav = bool(rec.wav_path) and Path(rec.wav_path).exists()
        self.btn_play.setEnabled(has_wav)
        self.slider.setEnabled(has_wav)
        self.btn_h_redo.setEnabled(has_wav)
        self.player.load(rec.wav_path if has_wav else "")
        self.slider.setValue(0)
        when = rec.created_at.replace("T", " ")
        ed = f"　修正: {rec.edited_at.replace('T', ' ')}" if rec.edited_at else ""
        self.lbl_rec_meta.setText(f"{when}　長さ {fmt_dur(rec.duration)}　モデル {rec.model}{ed}"
                                  + ("" if has_wav else "　（WAV が見つからないので再生・やり直しはできません）"))

    def _editor_changed(self) -> None:
        if not self._loading_editor:
            self._edit_dirty = True

    @safe
    def editor_line_clicked(self, idx: int) -> None:
        rec = self._current_rec
        if rec is None or not self.btn_play.isEnabled():
            return
        t = seek_time_for_block(viewer_segments(rec.segments), idx, self.editor.blockCount(), rec.duration)
        if t is not None:
            self.player.play_from(t)

    @safe
    def _player_position(self, sec: float) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(int(sec * 1000))
        self.lbl_pos.setText(f"{fmt_dur(sec)} / {fmt_dur(self.player.duration)}")
        rec = self._current_rec
        if rec is not None:
            segs = viewer_segments(rec.segments)
            if len(segs) == self.editor.blockCount():
                self.editor.highlight_block(block_for_time(segs, sec))

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
        if self._question( "削除", f"この録音を履歴から消し、WAV・txt・md をごみ箱へ移します。\n\n{rec.title}\n\nよろしいですか？") != QMessageBox.Yes:
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
        hk_keys = ("hotkey_record", "hotkey_show", "hotkey_pause")
        changed_hk = any(v[k] != self.s.get(k, "") for k in hk_keys)
        dev_changed = v["input_device"] != self.s.get("input_device")
        size_changed = v["display_size"] != self.s.get("display_size", S.DISPLAY_SIZE)
        self.s.update(v)
        self._save_settings()
        self.svc.session.silence.limit_sec = float(v["silence_stop_sec"])
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
             "show": str(self.s.get("hotkey_show", ""))},
            callback=lambda name: self.bridge.hotkey.emit(name))
        res = self.hotkey_mgr.start()
        errs = [f"{k}: {v}" for k, v in res.items() if v]
        if errs:
            self._warn( "ホットキーを登録できません", "\n".join(errs))
        elif res:
            self.statusBar().showMessage("ホットキーを登録しました: " + " / ".join(
                f"{k}={self.s.get('hotkey_' + k)}" for k in res), 8000)

    @safe
    def _on_hotkey(self, name: str) -> None:
        if name == "record":
            self.toggle_record()
        elif name == "pause":
            self.toggle_pause()
        elif name == "show":
            self.toggle_window()
