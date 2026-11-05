# filer_history_bar.py
# あっふのフォルダ/ファイル履歴(Shift+H)で、窓の一覧の真下に出す絞り込みの段。
#
#   履歴 [全部][フォルダ][ファイル] [今日][3日][30日]    表示 12 / 34件  [✕]
#
# - 種別と期間のチップはどちらも択一(押したものだけが点く)。状態は窓(Pane)が持ち、
#   この段は表示とマウス操作だけ(絞り込み自体は filer.py が行う)
# - ✕ は履歴を閉じて、開く前にいたフォルダへ戻る
# - どの部品もフォーカスを取らない(ファイル窓のキー操作を奪わない)
# - ★の範囲の段(filer_rating_bar)と見た目を揃える(地の色・チップの形)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QWidget

KIND_CHIPS = (("all", "全部"), ("folder", "フォルダ"), ("file", "ファイル"))
PERIOD_CHIPS = ((1, "今日"), (3, "3日"), (30, "30日"))


class HistoryBar(QWidget):
    """種別・期間のチップ・表示件数・閉じる(✕)を並べた1段。"""

    pressed = Signal()                       # チップか ✕ を押した(窓をアクティブにするため)
    filter_changed = Signal(str, int)        # (種別, 期間の日数)
    close_requested = Signal()               # ✕

    def __init__(self, colors: dict, font, parent=None):
        super().__init__(parent)
        c = colors
        self.setObjectName("filerHistoryBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#filerHistoryBar {{ background-color: {c['status_background']}; }}"
            f" #filerHistoryBar QLabel {{ color: {c['status_text']}; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(6)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._colors = c
        self._font = font
        fm = QFontMetrics(font)

        def label(text=""):
            w = QLabel(text, self)
            w.setTextFormat(Qt.PlainText)
            w.setFont(font)
            return w

        layout.addWidget(label("履歴"))
        self.kind_buttons = {}
        for kind, text in KIND_CHIPS:
            b = self._make_chip(text, f"種類: {text}だけ出す")
            b.toggled.connect(lambda on, k=kind: self._chip_toggled("kind", k, on))
            self.kind_buttons[kind] = b
            layout.addWidget(b)
        self.period_buttons = {}
        for days, text in PERIOD_CHIPS:
            b = self._make_chip(text, f"期間: 直近{text}だけ出す")
            b.toggled.connect(lambda on, d=days: self._chip_toggled("period", d, on))
            self.period_buttons[days] = b
            layout.addWidget(b)
        self.count_label = label()
        self.close_button = QPushButton("✕", self)
        self.close_button.setFocusPolicy(Qt.NoFocus)
        self.close_button.setFont(font)
        self.close_button.setToolTip("履歴を閉じて、開く前にいたフォルダへ戻る")
        self.close_button.setFixedSize(fm.height() + 6, fm.height() + 2)
        self.close_button.setStyleSheet(
            f"QPushButton {{ background-color: {c['status_background']}; color: {c['status_text']};"
            f" border: 1px solid {c['fkey_border']}; padding: 0px; }}")
        self.close_button.pressed.connect(self.pressed)
        self.close_button.clicked.connect(self.close_requested)
        layout.addStretch()
        layout.addWidget(self.count_label)
        layout.addWidget(self.close_button)
        self._kind = "all"
        self._period = 30
        self._sync_chips()

    def _make_chip(self, text: str, tip: str) -> QPushButton:
        c = self._colors
        b = QPushButton(text, self)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.NoFocus)
        b.setFont(self._font)
        b.setToolTip(tip)
        b.setStyleSheet(
            f"QPushButton {{ background-color: {c['status_background']}; color: {c['status_text']};"
            f" border: 1px solid {c['fkey_border']}; border-radius: 8px; padding: 1px 8px; }}"
            f" QPushButton:checked {{ background-color: {c['rating_accent']}; color: {c['background']};"
            f" border: 1px solid {c['rating_accent']}; }}")
        b.pressed.connect(self.pressed)
        return b

    def _chip_toggled(self, group: str, value, on: bool) -> None:
        """チップの択一を保つ。点いているものを押し直しても消さない(空の絞り込みにしない)。"""
        current = self._kind if group == "kind" else self._period
        if not on:
            if value == current:      # いま点いているものを押した → 点けたままに戻す
                self._sync_chips()
            return
        if group == "kind":
            self._kind = value
        else:
            self._period = value
        self._sync_chips()
        self.filter_changed.emit(self._kind, self._period)

    def _sync_chips(self) -> None:
        for kind, b in self.kind_buttons.items():
            b.setChecked(kind == self._kind)
        for days, b in self.period_buttons.items():
            b.setChecked(days == self._period)

    def show_state(self, kind: str, period_days: int, shown: int, total: int) -> None:
        """窓の状態を段へ写す(信号は出さない)。"""
        if kind != self._kind or period_days != self._period:
            self._kind = kind if kind in self.kind_buttons else "all"
            self._period = period_days if period_days in self.period_buttons else 30
            self._sync_chips()
        self.count_label.setText(f"表示 {shown} / {total}件")
