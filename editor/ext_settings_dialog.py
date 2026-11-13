# editor/ext_settings_dialog.py
# 拡張子別の設定画面。拡張子を選び、項目(editor/ext_settings.py の
# ITEMS)ごとに全体の設定への上書きを一覧して変えられる窓。
#
# 持ち方は色の設定と同じ「全体+上書き」: この窓は上書きの対応
# (拡張子 → 項目 → 値)だけを持ち、変えるたびに
# on_change(上書きの対応) が呼ばれ、呼び側(窓)が開いている文書へ
# 反映して設定へ保存する(この窓自体は設定に触らない)。項目の欄には
# 実効の値(上書きがあれば上書き、無ければ全体の値)が出て、上書き
# 中の項目は表示名に印が付き、「全体に戻す」が押せるようになる。
#
# テストでは set_extension / set_item / reset_item を直接呼べる。
from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from editor import editor_icon
from editor import ext_settings as extmod

_WRAP_MODE_LABELS = (
    ("column", "桁数で折り返す"),
    ("window", "ウィンドウ幅で折り返す"),
    ("none", "折り返さない"),
)

# タブの幅だけは、全体の既定が「Qt の既定」(値なし)のことがある。
# そのとき欄に出す代わりの値(触らなければ上書きにはならない)。
_TAB_WIDTH_PLACEHOLDER = 8


class ExtensionSettingsDialog(QDialog):
    """拡張子別の設定画面。

    settings_map は現在の上書き(拡張子 → 項目 → 値)、global_values
    は全体の値(6項目すべて。tab_width は None でもよい)、
    extensions は候補へ足す拡張子(開いているタブ・設定済みのもの)。
    on_change は (上書きの対応) を受ける関数で、変更のたびに
    呼ばれる(呼ばれた側が反映と保存をする)。"""

    def __init__(self, parent=None, *, settings_map=None,
                 global_values=None, extensions=None,
                 current_extension=None, on_change=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。循環 import を
        # 避けるため文字列で持つ(color_dialog と同じ)。
        self.setWindowTitle("拡張子別の設定 - EDDy")
        self.resize(520, 480)
        self._on_change = on_change
        self._settings = extmod.sanitize(settings_map)
        self._global = dict(global_values or {})

        layout = QVBoxLayout(self)
        ext_row = QHBoxLayout()
        ext_row.addWidget(QLabel("拡張子"))
        self._ext_combo = QComboBox()
        self._ext_combo.setEditable(True)
        candidates = set(extmod.COMMON_EXTENSIONS)
        candidates.update(self._settings)
        for ext in extensions or ():
            normalized = extmod.normalize_extension(ext)
            if normalized:
                candidates.add(normalized)
        current = extmod.normalize_extension(current_extension)
        if current:
            candidates.add(current)
        self._ext_combo.addItems(sorted(candidates))
        self._ext = current or self._ext_combo.itemText(0)
        self._ext_combo.setCurrentText(self._ext)
        self._ext_combo.currentTextChanged.connect(self.set_extension)
        ext_row.addWidget(self._ext_combo)
        ext_row.addStretch(1)
        layout.addLayout(ext_row)
        layout.addWidget(QLabel(
            "項目を変えると、その拡張子だけ全体の設定から上書きされます。"))

        self._controls = {}
        self._reset_buttons = {}
        self._labels = {}
        for key, label in extmod.ITEMS:
            row = QHBoxLayout()
            name_label = QLabel(label)
            name_label.setMinimumWidth(150)
            row.addWidget(name_label)
            control = self._make_control(key)
            row.addWidget(control)
            reset = QPushButton("全体に戻す")
            reset.clicked.connect(
                lambda _checked=False, k=key: self.reset_item(k))
            row.addWidget(reset)
            row.addStretch(1)
            layout.addLayout(row)
            self._labels[key] = (name_label, label)
            self._controls[key] = control
            self._reset_buttons[key] = reset
        layout.addStretch(1)

        bottom = QHBoxLayout()
        clear_button = QPushButton("この拡張子をすべて全体に戻す")
        clear_button.clicked.connect(self.reset_extension)
        bottom.addWidget(clear_button)
        bottom.addStretch(1)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

        self._refresh_rows()

    # ---------------------------------------------------------------
    # 部品(項目ごとに型が違う)
    # ---------------------------------------------------------------
    def _make_control(self, key):
        if key == "wrap_mode":
            control = QComboBox()
            for value, label in _WRAP_MODE_LABELS:
                control.addItem(label, value)
            control.currentIndexChanged.connect(
                lambda _i: self._on_control_changed("wrap_mode"))
            return control
        if key == "wrap_column":
            control = QSpinBox()
            control.setRange(extmod.WRAP_COLUMN_MIN,
                             extmod.WRAP_COLUMN_MAX)
            control.valueChanged.connect(
                lambda _v: self._on_control_changed("wrap_column"))
            return control
        if key == "markdown_highlight":
            control = QComboBox()
            control.addItem("有効", True)
            control.addItem("無効", False)
            control.currentIndexChanged.connect(
                lambda _i: self._on_control_changed("markdown_highlight"))
            return control
        if key == "font_family":
            control = QFontComboBox()
            control.currentFontChanged.connect(
                lambda _f: self._on_control_changed("font_family"))
            return control
        if key == "font_size":
            control = QSpinBox()
            control.setRange(extmod.FONT_SIZE_MIN, extmod.FONT_SIZE_MAX)
            control.setSuffix(" pt")
            control.valueChanged.connect(
                lambda _v: self._on_control_changed("font_size"))
            return control
        if key == "tab_width":
            control = QSpinBox()
            control.setRange(extmod.TAB_WIDTH_MIN, extmod.TAB_WIDTH_MAX)
            control.setSuffix(" 文字")
            control.valueChanged.connect(
                lambda _v: self._on_control_changed("tab_width"))
            return control
        raise KeyError(key)

    def _control_value(self, key):
        control = self._controls[key]
        if key in ("wrap_mode", "markdown_highlight"):
            return control.currentData()
        if key == "font_family":
            return control.currentFont().family()
        return control.value()

    def _set_control_value(self, key, value):
        control = self._controls[key]
        blocker = QSignalBlocker(control)
        if key in ("wrap_mode", "markdown_highlight"):
            index = control.findData(value)
            control.setCurrentIndex(index if index >= 0 else 0)
        elif key == "font_family":
            control.setCurrentFont(QFont(value))
        elif key == "tab_width" and value is None:
            control.setValue(_TAB_WIDTH_PLACEHOLDER)
        else:
            control.setValue(value)
        del blocker

    # ---------------------------------------------------------------
    # 状態の読み書き(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def values(self) -> dict:
        """上書きの対応(拡張子 → 項目 → 値)の写し。"""
        return {ext: dict(items) for ext, items in self._settings.items()}

    def extension(self) -> str:
        return self._ext

    def is_overridden(self, key) -> bool:
        return key in self._settings.get(self._ext, {})

    def item_value(self, key):
        """今の拡張子での実効の値(上書きがあれば上書き、無ければ全体)。"""
        overrides = self._settings.get(self._ext, {})
        if key in overrides:
            return overrides[key]
        return self._global.get(key)

    # ---------------------------------------------------------------
    # 変更(すべて on_change へ流れる)
    # ---------------------------------------------------------------
    def set_extension(self, ext):
        """編集対象の拡張子を切り替える。表記は揃える(大文字・
        先行ドットは落とす)。使えない表記なら切り替えない。"""
        normalized = extmod.normalize_extension(ext)
        if not normalized:
            return
        if self._ext_combo.currentText() != normalized:
            blocker = QSignalBlocker(self._ext_combo)
            self._ext_combo.setCurrentText(normalized)
            del blocker
        if normalized == self._ext:
            return
        self._ext = normalized
        if self._ext_combo.findText(normalized) < 0:
            self._ext_combo.addItem(normalized)
        self._refresh_rows()

    def set_item(self, key, value):
        """今の拡張子の1項目を上書きする。値が不正なら何もしない。"""
        cleaned = extmod.sanitize_item(key, value)
        if cleaned is None:
            return
        overrides = self._settings.get(self._ext, {})
        if overrides.get(key) == cleaned:
            return
        updated = dict(overrides)
        updated[key] = cleaned
        self._settings[self._ext] = updated
        self._settings = extmod.sanitize(self._settings)
        self._refresh_row(key)
        self._notify()

    def reset_item(self, key):
        """1項目の上書きをやめて、全体の値に戻す。"""
        overrides = self._settings.get(self._ext, {})
        if key not in overrides:
            return
        updated = dict(overrides)
        del updated[key]
        if updated:
            self._settings[self._ext] = updated
        else:
            del self._settings[self._ext]
        self._refresh_row(key)
        self._notify()

    def reset_extension(self):
        """今の拡張子の上書きを全部やめる。"""
        if self._ext not in self._settings:
            return
        del self._settings[self._ext]
        self._refresh_rows()
        self._notify()

    # ---------------------------------------------------------------
    # 内部
    # ---------------------------------------------------------------
    def _on_control_changed(self, key):
        self.set_item(key, self._control_value(key))

    def _notify(self):
        if self._on_change is not None:
            self._on_change(self.values())

    def _refresh_rows(self):
        for key, _label in extmod.ITEMS:
            self._refresh_row(key)

    def _refresh_row(self, key):
        self._set_control_value(key, self.item_value(key))
        overridden = self.is_overridden(key)
        self._reset_buttons[key].setEnabled(overridden)
        name_label, label = self._labels[key]
        name_label.setText(label + "（上書き中）" if overridden else label)
