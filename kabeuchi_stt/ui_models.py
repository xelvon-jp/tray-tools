# -*- coding: utf-8 -*-
"""認識モデルの取得・管理ダイアログと、WAV清書の「使うモデルを選ぶ」ダイアログ。

取得は別スレッド（画面を止めない）。利用者が［取得］を押したときだけ通信する。
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QGridLayout, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

import models_catalog as MC
import ui_style as S


def fill_model_combo(combo: QComboBox, model_dir, current: str, usage: str | None = None) -> None:
    """カタログから選択肢を作る。保存する値は itemData（モデル名）。current がカタログ外でも選べる。"""
    combo.blockSignals(True)
    combo.clear()
    for label, value in MC.combo_items(model_dir, current, usage):
        combo.addItem(label, value)
    idx = combo.findData(MC.model_name(current))
    combo.setCurrentIndex(max(0, idx))
    combo.blockSignals(False)


def combo_value(combo: QComboBox, default: str) -> str:
    return str(combo.currentData() or default)


def format_bytes(n: int) -> str:
    return f"{n / 1e9:.2f}GB" if n >= 1e9 else f"{n / 1e6:.0f}MB"


class _Signals(QObject):
    progress = Signal(str, object, object)     # key, done, total
    finished = Signal(str, object)             # key, DownloadResult


class ModelManagerDialog(QDialog):
    """候補ごとに 名前・大きさ・状態・［取得］・進み具合。同時に取得するのは1つだけ。"""

    def __init__(self, model_dir, parent=None, downloader=None):
        super().__init__(parent)
        self.setWindowTitle("モデルの取得・管理")
        self.setMinimumWidth(640)
        self.model_dir = Path(model_dir)
        self._downloader = downloader or MC.download
        self._sig = _Signals()
        self._sig.progress.connect(self._on_progress)
        self._sig.finished.connect(self._on_finished)
        self._cancel: threading.Event | None = None
        self._busy_key: str | None = None
        self._thread: threading.Thread | None = None
        self.rows: dict[str, dict] = {}

        lay = QVBoxLayout(self)
        head = QLabel(f"取得先: {self.model_dir}\n［取得］を押したときだけ、Hugging Face から取得します（大きいものは時間がかかります）。")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {S.MUTED}; font-size: 9pt;")
        lay.addWidget(head)
        grid = QGridLayout()
        grid.setColumnStretch(0, 1)
        lay.addLayout(grid)
        for r, m in enumerate(MC.CATALOG):
            name = QLabel(f"{m.title}\n精度:{m.accuracy}　速さ:{m.speed}" + (f"\n{m.note}" if m.note else ""))
            name.setWordWrap(True)
            size = QLabel(f"約{m.size_gb:g}GB")
            state = QLabel("")
            btn = QPushButton("取得")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setVisible(False)
            bar.setTextVisible(True)
            grid.addWidget(name, r * 2, 0)
            grid.addWidget(size, r * 2, 1)
            grid.addWidget(state, r * 2, 2)
            grid.addWidget(btn, r * 2, 3)
            grid.addWidget(bar, r * 2 + 1, 0, 1, 4)
            btn.clicked.connect(lambda _c=False, k=m.model: self._on_button(k))
            self.rows[m.model] = {"name": name, "size": size, "state": state, "btn": btn, "bar": bar}
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.button(QDialogButtonBox.Close).setText("閉じる")
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.refresh_states()

    # ---- 表示
    def refresh_states(self) -> None:
        for key, w in self.rows.items():
            done = MC.is_downloaded(key, self.model_dir)
            busy = (self._busy_key == key)
            w["state"].setText("取得済み" if done else "未取得")
            w["state"].setStyleSheet(f"color: {S.MUTED};" if done else "")
            w["btn"].setText("キャンセル" if busy else ("取得済み" if done else "取得"))
            w["btn"].setEnabled(busy or (not done and self._busy_key is None))
            for lab in (w["name"], w["size"]):
                lab.setStyleSheet(f"color: {S.MUTED};" if done else "")

    # ---- 操作
    def _on_button(self, key: str) -> None:
        if self._busy_key == key:
            if self._cancel is not None:
                self._cancel.set()
            self.rows[key]["btn"].setEnabled(False)
            return
        if self._busy_key is not None or MC.is_downloaded(key, self.model_dir):
            return
        self.start_download(key)

    def start_download(self, key: str) -> None:
        self._busy_key = key
        self._cancel = threading.Event()
        cancel = self._cancel
        self.msg.setText("取得中です…（キャンセルできます）")
        bar = self.rows[key]["bar"]
        bar.setValue(0)
        bar.setFormat("準備中…")
        bar.setVisible(True)
        self.refresh_states()

        def work() -> None:
            try:
                res = self._downloader(key, self.model_dir,
                                       lambda d, t: self._sig.progress.emit(key, d, t), cancel)
            except Exception as e:      # downloader は例外を投げない約束だが、念のため
                res = MC.DownloadResult(False, MC.failure_message(key, self.model_dir, f"{type(e).__name__}: {e}"))
            self._sig.finished.emit(key, res)

        self._thread = threading.Thread(target=work, name="kabeuchi-model-download", daemon=True)
        self._thread.start()

    def _on_progress(self, key: str, done, total) -> None:
        bar = self.rows[key]["bar"]
        total = int(total or 0)
        done = int(done or 0)
        if total > 0:
            bar.setValue(min(100, int(done * 100 / total)))
            bar.setFormat(f"{format_bytes(done)} / {format_bytes(total)}")
        else:
            bar.setFormat(format_bytes(done))

    def _on_finished(self, key: str, res) -> None:
        self._busy_key = None
        self._cancel = None
        bar = self.rows[key]["bar"]
        bar.setVisible(False)
        if res.ok:
            self.msg.setText(f"{key} を取得しました。")
        else:
            self.msg.setText(res.message)
        self.refresh_states()

    def reject(self) -> None:
        self._stop_download()
        super().reject()

    def done(self, r: int) -> None:
        self._stop_download()
        super().done(r)

    def _stop_download(self) -> None:
        """閉じるときは、取得を止める（途中の分は次回に続きから再開）。"""
        if self._cancel is not None:
            self._cancel.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3)


class WavModelDialog(QDialog):
    """WAV を選んだあとに、使うモデルを選ぶ（既定は設定の録音全体の清書モデル）。"""

    def __init__(self, wav_name: str, model_dir, default_model: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("WAV清書")
        self.setMinimumWidth(520)
        self.model_dir = Path(model_dir)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"清書する録音: {wav_name}"))
        lay.addWidget(QLabel("使うモデル"))
        self.model_combo = QComboBox()
        fill_model_combo(self.model_combo, self.model_dir, default_model)
        lay.addWidget(self.model_combo)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #B45309;")
        lay.addWidget(self.warn)
        note = QLabel("同じ録音を別のモデルで何度も清書できます。結果はそれぞれ履歴に並びます（モデル名つき）。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED}; font-size: 9pt;")
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("清書する")
        bb.button(QDialogButtonBox.Cancel).setText("キャンセル")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.model_combo.currentIndexChanged.connect(lambda _i: self._update_warn())
        self._update_warn()

    def model(self) -> str:
        return combo_value(self.model_combo, "small")

    def _update_warn(self) -> None:
        m = self.model()
        self.warn.setText("" if MC.is_downloaded(m, self.model_dir) else
                          "このモデルはまだ取得していません。清書を始めると自動で取得しようとします"
                          "（設定の［モデルの取得・管理…］で先に取得できます）")
