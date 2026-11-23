# -*- coding: utf-8 -*-
"""統合 GUI の右の引き出し（履歴・検索・再生／用語・置換／前置き／清書の状況）。PySide6。

機能は作り直さず、メイン窓（MainWindow）がすでに持っている部品（履歴の一覧・閲覧・再生・清書のやり直し・削除、
専門用語のチップ、置換辞書、前置きの編集、清書の状況…）を、この引き出しへ「移す」。
部品を持っている側のロジック（検索・再生・保存・辞書の反映）はそのまま働く。
設定は ⚙ の設定画面だけ（引き出しに設定タブは無い）。前置きのタブは編集（追加・保存・削除）だけで、クリップボードへ入れる部品は無い。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget

import ui_style as S

TAB_HISTORY, TAB_TERMS, TAB_PREFIX, TAB_QUEUE = range(4)
TAB_NAMES = ("履歴", "用語・置換", "前置き", "清書の状況")
DRAWER_WIDTH = 460


def adopt(w: QWidget) -> QWidget:
    """w を、もとのレイアウトから外して返す（新しいレイアウトへ入れ直せるように。Qt の「すでにレイアウトにある」警告を避ける）。"""
    par = w.parentWidget()
    lay = par.layout() if par is not None else None
    if lay is not None:
        lay.removeWidget(w)
    return w


def _scroll(inner: QWidget) -> QScrollArea:
    sc = QScrollArea()
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setWidget(inner)
    return sc


class Drawer(QWidget):
    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.setObjectName("drawer")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs)
        self.tabs.addTab(self._history_page(), TAB_NAMES[TAB_HISTORY])
        self.tabs.addTab(self._terms_page(), TAB_NAMES[TAB_TERMS])
        self.tabs.addTab(self._prefix_page(), TAB_NAMES[TAB_PREFIX])
        self.tabs.addTab(self._queue_page(), TAB_NAMES[TAB_QUEUE])
        self.tabs.currentChanged.connect(lambda i: self._on_tab(i))

    # ---- 履歴・検索・再生
    def _history_page(self) -> QWidget:
        h = self.host
        page = QWidget()
        pl = QVBoxLayout(page)
        pl.setContentsMargins(8, 8, 8, 6)
        pl.setSpacing(6)
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(4)
        for w in (h.ed_search, h.lbl_hist_count):
            tl.addWidget(adopt(w))
        tl.addWidget(adopt(h.lst_hist), 1)
        top.setMinimumHeight(110)
        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(adopt(h.lbl_rec_meta))
        bl.addWidget(adopt(h.editor), 1)
        bl.addWidget(adopt(h.play_row))
        bl.addWidget(adopt(h.lbl_out_notice))
        bl.addWidget(adopt(h.btn_h_save.parentWidget()))     # 修正を保存・元に戻す・辞書へ・WAVから清書をやり直す・削除
        bottom.setMinimumHeight(220)
        split.addWidget(top)
        split.addWidget(bottom)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 2)
        split.setSizes([160, 360])
        pl.addWidget(split, 1)
        return page

    # ---- 用語・置換
    def _terms_page(self) -> QWidget:
        h = self.host
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(8, 8, 8, 8)
        il.setSpacing(10)
        il.addWidget(adopt(h.grp_hot))
        il.addWidget(adopt(h.grp_dict))
        il.addStretch(1)
        return _scroll(inner)

    # ---- 前置き（名前＋本文の追加・保存・削除だけ。使うのはメイン画面の「前置き ▾」）
    def _prefix_page(self) -> QWidget:
        h = self.host
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(8, 8, 8, 8)
        il.setSpacing(10)
        note = QLabel("ここで作った前置きは、メイン画面の［前置き ▾］から選ぶと、いまの下書きの先頭に1回だけ入ります"
                      "（送るときは 前置き＋改行＋下書き）。自動では付きません。")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {S.MUTED};")
        il.addWidget(note)
        il.addWidget(adopt(h.grp_preset))
        il.addStretch(1)
        return _scroll(inner)

    # ---- 清書の状況（直近の清書の一覧・実行中の中断）。進み具合・残り時間はメイン画面の通知の行
    def _queue_page(self) -> QWidget:
        h = self.host
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(8, 8, 8, 8)
        il.setSpacing(10)
        il.addWidget(adopt(h.queue_group))
        h.queue_group.setVisible(True)
        il.addStretch(1)
        return _scroll(inner)

    def _on_tab(self, i: int) -> None:
        self.host.s["unified_drawer_tab"] = int(i)

    def current_tab(self) -> int:
        return self.tabs.currentIndex()

    def set_tab(self, i: int) -> None:
        i = max(0, min(self.tabs.count() - 1, int(i)))
        self.tabs.setCurrentIndex(i)
