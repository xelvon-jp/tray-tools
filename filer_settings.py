# filer_settings.py
# あっふの Z「設定」ダイアログ(あふｗの設定画面に寄せたもの)。
#
# 将来ほかの設定も入れられるようにタブにしてあるが、いまは「拡張子別表示色」だけ。
# 配色は Windows 標準のまま(ファイラの濃紺には寄せない)。色の帯がそのままの色で
# 見えることがいちばん大事な画面なので、地の色に引っ張られない白地の方が判断しやすい。
#
# ここは編集だけ。決定で rules を呼び出し側が読み、settings.json への保存と
# 一覧の描き直しは filer.py が行う。中止なら何も起きない。
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import filer_core as core


class SettingsDialog(QDialog):
    def __init__(self, parent, ext_rules, font=None):
        super().__init__(parent)
        self.setWindowTitle("あっふの設定")
        if font is not None:
            self.setFont(font)
        # 編集用の写し。決定までは呼び出し側の設定に触らない。
        self.rules = [dict(r, exts=list(r["exts"])) for r in ext_rules]

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        group = QGroupBox("登録内容")
        group_layout = QVBoxLayout(group)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["表示色", "拡張子"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.ElideRight)   # 長い拡張子の並びは … で省く
        self.tree.header().setSectionResizeMode(0, QHeaderView.Fixed)
        self.tree.header().resizeSection(0, 150)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self.edit_row())
        self.tree.installEventFilter(self)
        group_layout.addWidget(self.tree)

        row_buttons = QHBoxLayout()
        self.new_button = QPushButton("新規(&N)")
        self.edit_button = QPushButton("変更(&C)")
        self.delete_button = QPushButton("削除(&D)")
        self.up_button = QPushButton("上(&U)")
        self.down_button = QPushButton("下(&S)")
        self.new_button.clicked.connect(self.new_row)
        self.edit_button.clicked.connect(self.edit_row)
        self.delete_button.clicked.connect(self.delete_row)
        self.up_button.clicked.connect(lambda: self.move_row(-1))
        self.down_button.clicked.connect(lambda: self.move_row(1))
        for button in (self.new_button, self.edit_button, self.delete_button):
            row_buttons.addWidget(button)
        row_buttons.addStretch(1)
        row_buttons.addWidget(self.up_button)
        row_buttons.addWidget(self.down_button)
        group_layout.addLayout(row_buttons)
        page_layout.addWidget(group)

        color_group = QGroupBox("表示色")
        color_layout = QHBoxLayout(color_group)
        color_layout.addStretch(1)
        self.color_button = QPushButton("色(&X)")
        self.color_button.clicked.connect(self.change_color)
        color_layout.addWidget(self.color_button)
        page_layout.addWidget(color_group)
        self.tabs.addTab(page, "拡張子別表示色(&E)")

        bottom = QHBoxLayout()
        self.reset_button = QPushButton("初期値(&R)")
        self.reset_button.clicked.connect(self.reset_defaults)
        bottom.addWidget(self.reset_button)
        bottom.addStretch(1)
        self.ok_button = QPushButton("決定")
        self.ok_button.setDefault(False)
        self.ok_button.setAutoDefault(False)
        self.ok_button.clicked.connect(self.accept)
        self.cancel_button = QPushButton("中止")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        bottom.addWidget(self.ok_button)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        for button in (self.new_button, self.edit_button, self.delete_button, self.up_button,
                       self.down_button, self.color_button, self.reset_button):
            button.setAutoDefault(False)   # Enter は一覧の「変更」に使う(窓を閉じない)

        self.resize(620, 440)
        self.refresh(0)

    # --- 一覧 ---
    def refresh(self, select: int = None) -> None:
        self.tree.clear()
        for rule in self.rules:
            item = QTreeWidgetItem(["", " ".join(rule["exts"])])
            item.setBackground(0, QBrush(QColor(rule["color"])))
            item.setToolTip(1, " ".join(rule["exts"]))
            self.tree.addTopLevelItem(item)
        if self.rules:
            row = 0 if select is None else max(0, min(select, len(self.rules) - 1))
            self.tree.setCurrentItem(self.tree.topLevelItem(row))
        self.tree.setFocus()

    def current_row(self) -> int:
        item = self.tree.currentItem()
        return self.tree.indexOfTopLevelItem(item) if item is not None else -1

    def eventFilter(self, obj, event):
        # 一覧の上で Enter は「変更」、Del は「削除」(あふｗの一覧と同じ指で)。
        if obj is self.tree and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.edit_row()
                return True
            if event.key() == Qt.Key_Delete:
                self.delete_row()
                return True
        return super().eventFilter(obj, event)

    # --- 入力の口(テストではここを差し替える) ---
    def ask_exts(self, title: str, text: str = ""):
        value, ok = QInputDialog.getText(self, title, "拡張子（空白区切り。例: zip 7z rar）",
                                         text=text)
        return value if ok else None

    def ask_color(self, initial: str):
        color = QColorDialog.getColor(QColor(initial), self, "表示色")
        return color.name() if color.isValid() else None

    def confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "拡張子別表示色", text,
                                    QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    # --- 操作 ---
    def _settle_duplicates(self, row: int) -> int:
        """row の拡張子がほかの行にもあれば、ほかの行から外すか尋ねる。新しい row を返す。
        外さなければそのまま(上の行が勝つ)。"""
        dups = core.ext_duplicates(self.rules, row)
        if not dups:
            return row
        names = " ".join(sorted(dups))
        if self.confirm(f"ほかの行にもある拡張子があります: {names}\n"
                        "ほかの行から外しますか？（外さなければ、上の行の色が使われます）"):
            target = self.rules[row]
            self.rules = core.remove_exts_from_others(self.rules, row, dups)
            row = self.rules.index(target)
        return row

    def new_row(self) -> None:
        text = self.ask_exts("新規")
        exts = core.normalize_exts(text) if text else []
        if not exts:
            return
        color = self.ask_color("#ffffff") or "#ffffff"
        row = self.current_row() + 1 if self.rules else 0
        self.rules.insert(row, {"color": color, "exts": exts})
        self.refresh(self._settle_duplicates(row))

    def edit_row(self) -> None:
        row = self.current_row()
        if row < 0:
            return
        text = self.ask_exts("変更", " ".join(self.rules[row]["exts"]))
        if text is None:
            return
        exts = core.normalize_exts(text)
        if not exts:
            return
        self.rules[row]["exts"] = exts
        self.refresh(self._settle_duplicates(row))

    def delete_row(self) -> None:
        row = self.current_row()
        if row < 0:
            return
        if not self.confirm(f"この行を削除しますか？\n{' '.join(self.rules[row]['exts'])}"):
            return
        del self.rules[row]
        self.refresh(row)

    def move_row(self, step: int) -> None:
        row = self.current_row()
        target = row + step
        if row < 0 or not (0 <= target < len(self.rules)):
            return
        self.rules[row], self.rules[target] = self.rules[target], self.rules[row]
        self.refresh(target)

    def change_color(self) -> None:
        row = self.current_row()
        if row < 0:
            return
        color = self.ask_color(self.rules[row]["color"])
        if color:
            self.rules[row]["color"] = color.lower()
            self.refresh(row)

    def reset_defaults(self) -> None:
        if self.confirm("拡張子別表示色を初期値に戻しますか？"):
            self.rules = core.normalize_ext_colors(core.DEFAULT_EXT_COLORS)
            self.refresh(0)
