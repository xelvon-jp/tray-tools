# filer_settings.py
# あっふの Z「設定」ダイアログ(あふｗの設定画面に寄せたもの)。
#
# タブは「拡張子別表示色」と「拡張子とツール」(Ctrl+Enter の拡張子ごとのメニューの
# この PC の分の編集。共有の分は読み取り専用で灰色に出す)。
# 配色は Windows 標準のまま(ファイラの濃紺には寄せない)。色の帯がそのままの色で
# 見えることがいちばん大事な画面なので、地の色に引っ張られない白地の方が判断しやすい。
#
# ここは編集だけ。決定で rules を呼び出し側が読み、settings.json への保存と
# 一覧の描き直しは filer.py が行う。拡張子とツールは filer_extmenu.MenuEditor(写し)を
# 編集し、決定のあとで filer.py が書き込む。中止なら何も起きない。
#
# AddToolDialog は「ツールを追加」の小ダイアログ(Ctrl+Enter のメニュー末尾と、
# 「拡張子とツール」タブの 追加/変更 から開く)。
import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import filer_core as core
import filer_extmenu


class SettingsDialog(QDialog):
    def __init__(self, parent, ext_rules, font=None, menu_editor=None, ask_tool=None):
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

        self.menu_editor = menu_editor
        self.ask_tool_fn = ask_tool
        if menu_editor is not None:
            self.tabs.addTab(self._build_tool_page(), "拡張子とツール(&T)")

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

        self.resize(720, 460)
        self.refresh(0)
        if menu_editor is not None:
            self.tool_refresh_kinds(0)

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

    # ---------------------------------------------------------------
    # 「拡張子とツール」タブ
    # ---------------------------------------------------------------
    def _build_tool_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        left.addWidget(QLabel("拡張子"))
        self.kind_list = QListWidget()
        self.kind_list.setMaximumWidth(140)
        self.kind_list.currentRowChanged.connect(lambda _row: self.tool_refresh_items())
        left.addWidget(self.kind_list)
        self.kind_add_button = QPushButton("拡張子(&X)…")
        self.kind_add_button.clicked.connect(self.tool_add_kind)
        left.addWidget(self.kind_add_button)
        layout.addLayout(left)

        right = QVBoxLayout()
        right.addWidget(QLabel("メニューの項目（灰色は共有・グループの分で、ここでは変えられません）"))
        self.tool_tree = QTreeWidget()
        self.tool_tree.setColumnCount(4)
        self.tool_tree.setHeaderLabels(["キー", "表示名", "コマンド", "置き場"])
        self.tool_tree.setRootIsDecorated(False)
        self.tool_tree.setUniformRowHeights(True)
        self.tool_tree.header().resizeSection(0, 40)
        self.tool_tree.header().resizeSection(1, 150)
        self.tool_tree.header().resizeSection(2, 220)
        self.tool_tree.itemDoubleClicked.connect(lambda _i, _c: self.tool_edit())
        right.addWidget(self.tool_tree)
        buttons = QHBoxLayout()
        self.tool_add_button = QPushButton("追加(&A)")
        self.tool_edit_button = QPushButton("変更(&C)")
        self.tool_delete_button = QPushButton("削除(&D)")
        self.tool_up_button = QPushButton("上(&U)")
        self.tool_down_button = QPushButton("下(&S)")
        self.tool_add_button.clicked.connect(self.tool_add)
        self.tool_edit_button.clicked.connect(self.tool_edit)
        self.tool_delete_button.clicked.connect(self.tool_delete)
        self.tool_up_button.clicked.connect(lambda: self.tool_move(-1))
        self.tool_down_button.clicked.connect(lambda: self.tool_move(1))
        for button in (self.tool_add_button, self.tool_edit_button, self.tool_delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(self.tool_up_button)
        buttons.addWidget(self.tool_down_button)
        for button in (self.kind_add_button, self.tool_add_button, self.tool_edit_button,
                       self.tool_delete_button, self.tool_up_button, self.tool_down_button):
            button.setAutoDefault(False)
        right.addLayout(buttons)
        layout.addLayout(right, 1)
        self.tool_rows = []
        return page

    def current_kind(self) -> str:
        item = self.kind_list.currentItem()
        return item.text() if item is not None else ""

    def tool_refresh_kinds(self, select=None) -> None:
        kinds = self.menu_editor.kinds()
        self.kind_list.blockSignals(True)
        self.kind_list.clear()
        self.kind_list.addItems(kinds)
        self.kind_list.blockSignals(False)
        if kinds:
            if isinstance(select, str):
                row = kinds.index(select) if select in kinds else 0
            else:
                row = select if isinstance(select, int) else 0
            self.kind_list.setCurrentRow(max(0, min(row, len(kinds) - 1)))
        self.tool_refresh_items()

    def select_kind(self, kind: str) -> None:
        kinds = [self.kind_list.item(i).text() for i in range(self.kind_list.count())]
        if kind in kinds:
            self.kind_list.setCurrentRow(kinds.index(kind))

    def tool_refresh_items(self, select_local=None) -> None:
        self.tool_tree.clear()
        self.tool_rows = []
        kind = self.current_kind()
        if not kind:
            return
        gray = QBrush(QColor("#808080"))
        for source, line in self.menu_editor.readonly_items(kind):
            item = QTreeWidgetItem(self._tool_columns(line, source))
            for col in range(4):
                item.setForeground(col, gray)
            self.tool_tree.addTopLevelItem(item)
            self.tool_rows.append(None)
        current = None
        for number, line in enumerate(self.menu_editor.local_items(kind)):
            item = QTreeWidgetItem(self._tool_columns(line, f"この PC {kind}.txt"))
            self.tool_tree.addTopLevelItem(item)
            self.tool_rows.append(number)
            if number == select_local:
                current = item
        if current is not None:
            self.tool_tree.setCurrentItem(current)
        elif self.tool_tree.topLevelItemCount():
            self.tool_tree.setCurrentItem(self.tool_tree.topLevelItem(0))

    @staticmethod
    def _tool_columns(line, source) -> list:
        if line.separator:
            return ["", "――――", "", source]
        return [line.key, line.label, line.command, source]

    def current_local(self):
        """選んでいる行が この PC の項目ならその番号、共有の分や未選択なら None。"""
        item = self.tool_tree.currentItem()
        if item is None:
            return None
        row = self.tool_tree.indexOfTopLevelItem(item)
        return self.tool_rows[row] if 0 <= row < len(self.tool_rows) else None

    def select_tool_row(self, row: int) -> None:
        item = self.tool_tree.topLevelItem(row)
        if item is not None:
            self.tool_tree.setCurrentItem(item)

    # 入力の口(テストではここを差し替える)
    def ask_tool(self, kind, initial=None):
        if self.ask_tool_fn is None:
            return None
        return self.ask_tool_fn(kind, initial)

    def ask_kind(self):
        value, ok = QInputDialog.getText(self, "拡張子を追加",
                                         "拡張子（例: pdf）。フォルダは folder")
        return value if ok else None

    def tool_add_kind(self) -> None:
        text = self.ask_kind()
        if not text:
            return
        kind = self.menu_editor.add_kind(text)
        if kind:
            self.tool_refresh_kinds(kind)

    def tool_add(self) -> None:
        kind = self.current_kind()
        if not kind:
            return
        result = self.ask_tool(kind)
        if not result:
            return
        try:
            self.menu_editor.add_item(kind, result, result.get("scope") or "local")
        except ValueError as e:
            QMessageBox.warning(self, "拡張子とツール", str(e))
            return
        local = self.menu_editor.local_items(kind)
        self.tool_refresh_items(len(local) - 1 if result.get("scope") != "shared" else None)

    def tool_edit(self) -> None:
        kind = self.current_kind()
        number = self.current_local()
        if number is None:
            return
        line = self.menu_editor.local_items(kind)[number]
        if line.separator:
            return
        program, args = filer_extmenu.describe_program(line.command)
        result = self.ask_tool(kind, {"label": line.label, "key": line.key,
                                      "program": program, "args": args})
        if not result:
            return
        self.menu_editor.replace_local(kind, number, result)
        self.tool_refresh_items(number)

    def tool_delete(self) -> None:
        kind = self.current_kind()
        number = self.current_local()
        if number is None:
            return
        line = self.menu_editor.local_items(kind)[number]
        if not self.confirm(f"この項目を削除しますか？\n{line.label or '区切り'}"):
            return
        self.menu_editor.delete_local(kind, number)
        self.tool_refresh_items(max(0, number - 1))

    def tool_move(self, step: int) -> None:
        kind = self.current_kind()
        number = self.current_local()
        if number is None:
            return
        if self.menu_editor.move_local(kind, number, step):
            self.tool_refresh_items(number + step)


# ---------------------------------------------------------------
# ツールを追加(小ダイアログ)
# ---------------------------------------------------------------
class AddToolDialog(QDialog):
    """拡張子にツールを紐づける。実行するものは「この拡張子を開けるアプリ」の候補・
    filer.tools に登録済みのツール・参照(exe を選ぶ)から選ぶ。

    結果(result_value)は {"label", "key", "program", "args", "cwd", "scope"}。
    program は ("exe", パス) / ("tool", 名前) / ("raw", 文字列)。ファイルへの書き込みは
    呼び出し側(MenuEditor)が行う。initial を渡すと変更(保存先は この PC に固定)。"""

    def __init__(self, parent, kind, editor, candidates=None, describe=None, folder="",
                 initial=None, font=None):
        super().__init__(parent)
        self.kind = kind
        self.editor = editor
        self.describe = describe or (lambda path: "")
        self.editing = initial is not None
        self.setWindowTitle(("項目の変更" if self.editing else "ツールを追加") + f"（{kind}）")
        if font is not None:
            self.setFont(font)
        self._label_touched = self.editing
        self._key_touched = self.editing
        used = set(editor.used_keys(kind)) if editor is not None else set()
        if self.editing and initial.get("key"):
            used.discard(str(initial.get("key")).upper())
        self.used_keys = used

        layout = QVBoxLayout(self)
        form = QFormLayout()
        program_row = QHBoxLayout()
        self.program_combo = QComboBox()
        self.program_combo.setMinimumWidth(360)
        program_row.addWidget(self.program_combo, 1)
        self.browse_button = QPushButton("参照(&B)…")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self.browse_exe)
        program_row.addWidget(self.browse_button)
        form.addRow("実行するもの", program_row)
        self.label_edit = QLineEdit()
        self.label_edit.textEdited.connect(self._label_edited)
        form.addRow("表示名", self.label_edit)
        self.key_edit = QLineEdit()
        self.key_edit.setMaxLength(1)
        self.key_edit.setMaximumWidth(40)
        self.key_edit.textEdited.connect(lambda _t: setattr(self, "_key_touched", True))
        form.addRow("キー", self.key_edit)
        args_row = QHBoxLayout()
        self.args_edit = QLineEdit("$MF")
        args_row.addWidget(self.args_edit, 1)
        for macro in ("$MF", "$P", "$F", "$O"):
            button = QPushButton(macro)
            button.setAutoDefault(False)
            button.setMaximumWidth(48)
            button.clicked.connect(lambda _c=False, m=macro: self.insert_macro(m))
            args_row.addWidget(button)
        form.addRow("引数", args_row)
        cwd_row = QHBoxLayout()
        self.cwd_edit = QLineEdit()
        self.cwd_edit.setPlaceholderText("空欄なら今の窓のフォルダ")
        cwd_row.addWidget(self.cwd_edit, 1)
        cwd_button = QPushButton("参照…")
        cwd_button.setAutoDefault(False)
        cwd_button.clicked.connect(self.browse_cwd)
        cwd_row.addWidget(cwd_button)
        form.addRow("作業フォルダ", cwd_row)
        layout.addLayout(form)

        scope_box = QGroupBox("保存先")
        scope_layout = QHBoxLayout(scope_box)
        self.scope_local = QRadioButton("この PC だけ(&L)")
        self.scope_shared = QRadioButton("全 PC で共有(&G)")
        self.scope_local.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.scope_local)
        group.addButton(self.scope_shared)
        scope_layout.addWidget(self.scope_local)
        scope_layout.addWidget(self.scope_shared)
        scope_box.setEnabled(not self.editing)
        layout.addWidget(scope_box)
        hint = QLabel("exe の場所は この PC の settings.json（filer.tools）に名前付きで登録し、"
                      "メニューには {tool:名前} と書きます（共有にしても個人のパスは載りません）。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        ok = QPushButton("OK")
        ok.clicked.connect(self._accept)
        cancel = QPushButton("キャンセル")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        bottom.addWidget(ok)
        bottom.addWidget(cancel)
        layout.addLayout(bottom)

        self.folder = folder
        self._fill_programs(candidates)
        self.program_combo.currentIndexChanged.connect(lambda _i: self._program_changed())
        if self.editing:
            self._apply_initial(initial)
        else:
            self._program_changed()

    # --- 候補 ---
    def _fill_programs(self, candidates) -> None:
        self.program_combo.addItem("（選んでください）", None)
        tools = self.editor.tools if self.editor is not None else {}
        for name in sorted(tools, key=str.lower):
            self.program_combo.addItem(f"★ {name}（登録済みのツール）", ("tool", name))
        try:
            found = candidates(self.kind) if candidates else []
        except Exception:  # noqa: BLE001  候補が出ないだけ
            found = []
        for path in found:
            self.add_exe(path, select=False)

    def add_exe(self, path: str, select: bool = True) -> int:
        for index in range(self.program_combo.count()):
            data = self.program_combo.itemData(index)
            if data and data[0] == "exe" and os.path.normcase(data[1]) == os.path.normcase(path):
                if select:
                    self.program_combo.setCurrentIndex(index)
                return index
        try:
            title = self.describe(path) or ""
        except Exception:  # noqa: BLE001
            title = ""
        name = os.path.basename(path)
        text = f"{title}（{name}）" if title else name
        self.program_combo.addItem(text, ("exe", path))
        index = self.program_combo.count() - 1
        self.program_combo.setItemData(index, path, Qt.ToolTipRole)
        if select:
            self.program_combo.setCurrentIndex(index)
        return index

    def select_program(self, program) -> None:
        program = tuple(program)
        for index in range(self.program_combo.count()):
            data = self.program_combo.itemData(index)
            if data is not None and tuple(data) == program:
                self.program_combo.setCurrentIndex(index)
                return
        kind, value = program
        if kind == "exe":
            self.add_exe(value)
            return
        label = f"★ {value}（登録済みのツール）" if kind == "tool" else value
        self.program_combo.addItem(label, program)
        self.program_combo.setCurrentIndex(self.program_combo.count() - 1)

    def _apply_initial(self, initial) -> None:
        program = initial.get("program")
        if program:
            self.select_program(program)
        self.label_edit.setText(initial.get("label") or "")
        self.key_edit.setText(initial.get("key") or "")
        self.args_edit.setText(initial.get("args") if initial.get("args") is not None else "$MF")
        self.cwd_edit.setText(initial.get("cwd") or "")

    def _program_changed(self) -> None:
        data = self.program_combo.currentData()
        if data is None or self._label_touched:
            return
        kind, value = data
        if kind == "exe":
            try:
                label = self.describe(value) or ""
            except Exception:  # noqa: BLE001
                label = ""
            label = label or os.path.splitext(os.path.basename(value))[0]
        else:
            label = value
        self.label_edit.setText(label)
        self._suggest_key()

    def _label_edited(self, _text) -> None:
        self._label_touched = True
        self._suggest_key()

    def _suggest_key(self) -> None:
        if not self._key_touched:
            self.key_edit.setText(filer_extmenu.suggest_key(self.label_edit.text(),
                                                            self.used_keys))

    def insert_macro(self, macro: str) -> None:
        text = self.args_edit.text()
        self.args_edit.insert(macro if not text or text.endswith(" ") else " " + macro)
        self.args_edit.setFocus()

    # --- 入力の口(テストではここを差し替える) ---
    def pick_exe(self):
        path, _ = QFileDialog.getOpenFileName(self, "実行するもの", self.folder,
                                              "プログラム (*.exe *.bat *.cmd *.com);;すべて (*.*)")
        return path or None

    def pick_folder(self):
        return QFileDialog.getExistingDirectory(self, "作業フォルダ", self.folder) or None

    def browse_exe(self) -> None:
        path = self.pick_exe()
        if path:
            self.add_exe(os.path.normpath(path))

    def browse_cwd(self) -> None:
        path = self.pick_folder()
        if path:
            self.cwd_edit.setText(os.path.normpath(path))

    def error_text(self):
        """OK できない理由(無ければ None)。"""
        if self.program_combo.currentData() is None:
            return "実行するものを選んでください"
        if not self.label_edit.text().strip():
            return "表示名を入れてください"
        key = self.key_edit.text().strip()
        if not (len(key) == 1 and key.isascii() and key.isalnum()):
            return "キーは英数字1字にしてください"
        return None

    def _accept(self) -> None:
        error = self.error_text()
        if error:
            QMessageBox.warning(self, self.windowTitle(), error)
            return
        self.accept()

    def result_value(self) -> dict:
        return {"label": self.label_edit.text().strip(),
                "key": self.key_edit.text().strip().upper(),
                "program": self.program_combo.currentData(),
                "args": self.args_edit.text().strip(),
                "cwd": self.cwd_edit.text().strip(),
                "scope": "shared" if self.scope_shared.isChecked() and not self.editing
                else "local"}
