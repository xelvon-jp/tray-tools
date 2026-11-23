# -*- coding: utf-8 -*-
"""マイク状態チップ（固定サイズ）と、押したときの詳細の吹き出し。PySide6。

チップは「文言と色だけ」が変わる。大きさは、全状態の文言のうち最長のものが収まる幅に固定する（状態が変わっても
レイアウトが動かない）。詳細は押したときだけ（吹き出しは窓の中の子ウィジェット。別窓・前面化はしない）。
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

import ui_style as S
import ui_unified_logic as U

TONE_COLORS = {
    "good": S.OKGREEN, "warn": "#f0b429", "bad": S.REC, "noise": "#ff9f43", "none": "#ff7a9c",
    "busy": "#b392f0", "idle": "#9aa3b2", "paused": S.ACCENT, "wait": S.MUTED,
}
POPUP_SEC = 15.0          # 詳細の吹き出しは、この秒数で自動的に閉じる


class MicChip(QPushButton):
    """固定サイズの角丸チップ。set_state(key) で文言と色だけ変える。押すと detailRequested。"""
    detailRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._key = "wait"
        self._alert = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setFlat(True)
        self.refit()
        self.clicked.connect(lambda: self.detailRequested.emit())
        self._apply_tip()

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        return U.CHIP_STATES[self._key][0]

    def chip_font(self) -> QFont:
        f = QFont(self.font())
        f.setFamily(S.FONT_FAMILY)
        f.setPointSizeF(max(8.0, S.BASE_PT - 2.5))
        f.setBold(True)
        return f

    def refit(self) -> None:
        """最長の文言が収まる固定サイズにする（表示サイズを変えたときにも呼ぶ）。"""
        fm = QFontMetrics(self.chip_font())
        pad = 10 + 7 + 10                                                          # 左右の余白・点・点と文字の間
        full = max(fm.horizontalAdvance(t) for t in U.CHIP_LABELS)
        # 録音の行に ピン・⚙・引き出し のボタンも並ぶので、幅を抑える（長い2つの文言は末尾を省略。全文はツールチップと詳細の吹き出し）
        w = min(full, fm.horizontalAdvance("ノイズ大（要相談）")) + pad
        self.setFixedSize(QSize(w, max(26, fm.height() + 9)))
        self.update()

    def set_state(self, key: str) -> bool:
        """状態を変える。変わったら True。大きさは変わらない。"""
        if key not in U.CHIP_STATES:
            key = "wait"
        if key == self._key:
            return False
        self._key = key
        self._apply_tip()
        self.update()
        return True

    def set_alert(self, on: bool) -> None:
        """停止中の発話警告のあいだ、未録音の状態（idle）のチップを赤くする。"""
        on = bool(on)
        if on != self._alert:
            self._alert = on
            self.update()

    def _apply_tip(self) -> None:
        self.setToolTip(f"マイクの状態: {self.label}（押すと詳細）")
        self.setAccessibleName(f"マイクの状態 {self.label}")

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        col = QColor(S.REC if (self._alert and self._key == "idle") else TONE_COLORS[U.CHIP_STATES[self._key][1]])
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg = QColor(col)
        bg.setAlphaF(0.16)
        edge = QColor(col)
        edge.setAlphaF(0.55)
        p.setBrush(bg)
        p.setPen(QPen(edge, 1))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(QPoint(int(r.left() + 11), int(r.center().y())), 3, 3)
        p.setFont(self.chip_font())
        p.setPen(col)
        tr = QRectF(r.left() + 20, r.top(), r.width() - 24, r.height())
        fm = QFontMetrics(self.chip_font())
        p.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(self.label, Qt.ElideRight, int(tr.width())))
        p.end()


class ChipPopup(QFrame):
    """チップを押したときの詳細。親ウィンドウの中に重ねる子ウィジェット（別窓にしない）。"""
    openSettings = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("chipPop")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(f"#chipPop {{ background: #262b35; border: 1px solid #3a404c; border-radius: 10px; }}"
                           f" #chipPop QLabel {{ background: transparent; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(4)
        self.lbl_title = QLabel("")
        self.lbl_title.setStyleSheet("font-weight: bold;")
        self.lbl_body = QLabel("")
        self.lbl_body.setWordWrap(True)
        self.lbl_advice = QLabel("")
        self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setStyleSheet(f"color: {S.MUTED};")
        row = QHBoxLayout()
        self.btn_close = QPushButton("閉じる")
        self.btn_settings = QPushButton("設定を開く")
        row.addWidget(self.btn_close)
        row.addWidget(self.btn_settings)
        row.addStretch(1)
        for w in (self.lbl_title, self.lbl_body, self.lbl_advice):
            lay.addWidget(w)
        lay.addLayout(row)
        self.setFixedWidth(280)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(POPUP_SEC * 1000))
        self._timer.timeout.connect(self.hide)
        self.btn_close.clicked.connect(self.hide)
        self.btn_settings.clicked.connect(lambda: (self.hide(), self.openSettings.emit()))

    def refresh_text(self, key: str, issues=()) -> bool:
        """開いている間に状態が変わったら、文言だけ差し替える（位置と自動で閉じるまでの時間は変えない）。"""
        title, body, advice = U.chip_detail(key, issues)
        if [title, body, advice] == self.text_lines():
            return False
        self.lbl_title.setText(title)
        self.lbl_body.setText(body)
        self.lbl_advice.setText(advice)
        self.adjustSize()
        return True

    def show_for(self, chip: QWidget, key: str, issues=()) -> None:
        self.refresh_text(key, issues)
        self.adjustSize()
        par = self.parentWidget()
        tl = chip.mapTo(par, QPoint(0, chip.height() + 6))
        x = min(max(4, tl.x() + chip.width() - self.width()), max(4, par.width() - self.width() - 4))
        self.move(x, tl.y())
        self.show()
        self.raise_()
        self._timer.start()

    def toggle_for(self, chip: QWidget, key: str, issues=()) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_for(chip, key, issues)

    def text_lines(self) -> list[str]:
        return [self.lbl_title.text(), self.lbl_body.text(), self.lbl_advice.text()]
