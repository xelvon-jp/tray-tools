# editor/color_dialog.py
# 色の設定画面。配色の項目(editor/colors.py の COLOR_ITEMS)を一覧し、
# その場で色を選んで変えられる窓。
#
# 配色の持ち方は「プリセット+上書き」: この窓はプリセットの選択と、
# 項目ごとの上書き(色)を持つ。変えるたびに on_change(プリセット名,
# 上書きの対応) が呼ばれ、呼び側(窓)が開いている文書へ反映して
# 設定へ保存する(この窓自体は設定に触らない)。上書きをやめる操作は
# 項目ごとの「プリセットに戻す」ボタン。上書き中の項目は、行の
# 表示名に印が付き、そのボタンが押せるようになる。
#
# 色の見本はボタンで、押すと Qt 標準の QColorDialog が開く。
# テストでは set_item_color / reset_item / set_preset を直接呼べる。
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from editor import colors as colormod
from editor import editor_icon


class ColorSettingsDialog(QDialog):
    """色の設定画面。

    preset_name は現在のプリセット名、overrides は現在の上書き
    (項目名 → 色)。on_change は (プリセット名, 上書きの対応) を
    受ける関数で、変更のたびに呼ばれる(呼ばれた側が反映と保存を
    する)。"""

    def __init__(self, parent=None, *, preset_name, overrides=None,
                 on_change=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。循環 import を
        # 避けるため文字列で持つ(findall_dialog と同じ)。
        self.setWindowTitle("色の設定 - EDDy")
        self.resize(480, 560)
        self._on_change = on_change
        if preset_name not in colormod.COLOR_SCHEMES:
            preset_name = colormod.DEFAULT_SCHEME
        self._preset_name = preset_name
        self._overrides = colormod.sanitize_overrides(overrides)

        layout = QVBoxLayout(self)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("プリセット"))
        self._preset_combo = QComboBox()
        self._preset_combo.addItems(colormod.scheme_names())
        self._preset_combo.setCurrentText(self._preset_name)
        self._preset_combo.currentTextChanged.connect(self.set_preset)
        preset_row.addWidget(self._preset_combo)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)
        layout.addWidget(QLabel(
            "項目の色を選ぶと、その項目だけプリセットから上書きされます。"))

        self._swatches = {}
        self._reset_buttons = {}
        self._labels = {}
        rows_widget = QWidget()
        rows = QVBoxLayout(rows_widget)
        rows.setContentsMargins(0, 0, 0, 0)
        for key, label in colormod.COLOR_ITEMS:
            row = QHBoxLayout()
            name_label = QLabel(label)
            name_label.setMinimumWidth(220)
            row.addWidget(name_label)
            swatch = QPushButton()
            swatch.setFixedWidth(110)
            swatch.clicked.connect(
                lambda _checked=False, k=key: self._pick_color(k))
            row.addWidget(swatch)
            reset = QPushButton("プリセットに戻す")
            reset.clicked.connect(
                lambda _checked=False, k=key: self.reset_item(k))
            row.addWidget(reset)
            row.addStretch(1)
            rows.addLayout(row)
            self._labels[key] = (name_label, label)
            self._swatches[key] = swatch
            self._reset_buttons[key] = reset
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows_widget)
        layout.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

        self._refresh_rows()

    # ---------------------------------------------------------------
    # 状態の読み書き(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def values(self):
        """(プリセット名, 上書きの対応の写し)。"""
        return self._preset_name, dict(self._overrides)

    def preset_name(self) -> str:
        return self._preset_name

    def overrides(self) -> dict:
        return dict(self._overrides)

    def item_color(self, key) -> str:
        """項目の実効の色(上書きがあれば上書き、無ければプリセット)。"""
        scheme = colormod.resolve_scheme(self._preset_name, self._overrides)
        return scheme[key]

    def is_overridden(self, key) -> bool:
        return key in self._overrides

    # ---------------------------------------------------------------
    # 変更(すべて on_change へ流れる)
    # ---------------------------------------------------------------
    def set_preset(self, name):
        """プリセットを切り替える。上書きはそのまま残る。"""
        if name not in colormod.COLOR_SCHEMES:
            name = colormod.DEFAULT_SCHEME
        if self._preset_combo.currentText() != name:
            self._preset_combo.setCurrentText(name)
        if name == self._preset_name:
            return
        self._preset_name = name
        self._refresh_rows()
        self._notify()

    def set_item_color(self, key, value):
        """項目の色を上書きする。値が不正なら何もしない。"""
        clean = colormod.sanitize_overrides({key: value})
        if key not in clean:
            return
        if self._overrides.get(key) == clean[key]:
            return
        self._overrides[key] = clean[key]
        self._refresh_row(key)
        self._notify()

    def reset_item(self, key):
        """項目の上書きをやめて、プリセットの値に戻す。"""
        if key not in self._overrides:
            return
        del self._overrides[key]
        self._refresh_row(key)
        self._notify()

    # ---------------------------------------------------------------
    # 内部
    # ---------------------------------------------------------------
    def _pick_color(self, key):
        color = QColorDialog.getColor(
            QColor(self.item_color(key)), self, "色の選択")
        if color.isValid():
            self.set_item_color(key, color.name())

    def _notify(self):
        if self._on_change is not None:
            self._on_change(self._preset_name, dict(self._overrides))

    def _refresh_rows(self):
        for key, _label in colormod.COLOR_ITEMS:
            self._refresh_row(key)

    def _refresh_row(self, key):
        value = self.item_color(key)
        swatch = self._swatches[key]
        swatch.setText(value)
        # 見本の文字は、色の明るさで黒か白かを選ぶ(読めないと困る)。
        text_color = "#000000" if QColor(value).lightness() > 128 else "#ffffff"
        swatch.setStyleSheet(
            f"background-color: {value}; color: {text_color};")
        overridden = key in self._overrides
        self._reset_buttons[key].setEnabled(overridden)
        name_label, label = self._labels[key]
        name_label.setText(label + "（上書き中）" if overridden else label)
