# folder_watch_settings.py
# フォルダ監視移動の「ルールの追加・編集」ダイアログ。トレイの画面アイコン
# 「📥 フォルダ監視移動 ▸ ルールの追加・編集…」から開く(slideshow_settings.py と同じ作り)。
#
#   - ルールの一覧(行のチェック＝有効)。追加・編集はサブダイアログで1件ずつ
#   - [OK] で初めて settings.json の folder_watch.rules だけを書く(ほかのキーは消さない)
#
# 常駐の中で開く小さなダイアログなので、フォルダの中身は一切読まない(読むのは
# feature_screen.py のタイマーと folder_watch.FolderWatcher)。
import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import folder_watch as fw


class RuleEditDialog(QDialog):
    """ルール1件の編集。choose_folder はフォルダを選ぶ関数(テストで差し替える)。"""

    def __init__(self, rule: dict, parent=None, choose_folder=None):
        super().__init__(parent)
        self.setWindowTitle("フォルダ監視移動 — ルール")
        self.choose_folder = choose_folder or self._choose_folder
        rule = fw.normalize_rule(rule) or {
            "source": "", "dest": "", "interval_seconds": fw.DEFAULT_INTERVAL_SECONDS,
            "exclude_extensions": list(fw.DEFAULT_EXCLUDE_EXTENSIONS),
            "exclude_patterns": [], "enabled": True,
        }

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.source = QLineEdit(rule["source"])
        self.source.setMinimumWidth(420)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source, 1)
        source_button = QPushButton("参照(&B)…")
        source_button.clicked.connect(lambda _c=False: self._browse(self.source))
        source_row.addWidget(source_button)
        form.addRow("元フォルダ(&S)", source_row)

        self.dest = QLineEdit(rule["dest"])
        dest_row = QHBoxLayout()
        dest_row.addWidget(self.dest, 1)
        dest_button = QPushButton("参照(&R)…")
        dest_button.clicked.connect(lambda _c=False: self._browse(self.dest))
        dest_row.addWidget(dest_button)
        form.addRow("先フォルダ(&D)", dest_row)

        self.interval = QSpinBox()
        self.interval.setRange(fw.MIN_INTERVAL_SECONDS, fw.MAX_INTERVAL_SECONDS)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(rule["interval_seconds"])
        form.addRow("動作間隔(&I)", self.interval)

        self.exclude_extensions = QLineEdit(fw.extensions_text(rule["exclude_extensions"]))
        form.addRow("除外する拡張子(&E)", self.exclude_extensions)
        self.exclude_patterns = QLineEdit(fw.patterns_text(rule["exclude_patterns"]))
        form.addRow("除外する名前パターン(&P)", self.exclude_patterns)

        self.enabled = QCheckBox("有効(&A)")
        self.enabled.setChecked(rule["enabled"])
        form.addRow("", self.enabled)
        layout.addLayout(form)

        note = QLabel(
            "拡張子とパターンはカンマ区切りです。拡張子の初期値（crdownload 等）は"
            "ダウンロード中のファイルを避けるためのもので、消せば対象に戻ります。\n"
            "このほかに、サイズと更新時刻が前回のチェックから変わっていない"
            "ファイルだけを移動します（書き込み途中のものを掴まないため）。")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "フォルダを選ぶ")

    def _browse(self, field: QLineEdit) -> None:
        try:
            path = self.choose_folder()
            if path:
                field.setText(os.path.normpath(path))
        except Exception as e:  # noqa: BLE001  スロットから例外を出さない
            QMessageBox.warning(self, "フォルダ監視移動", f"フォルダを選べませんでした\n{e}")

    def values(self) -> dict:
        return {
            "source": self.source.text().strip(),
            "dest": self.dest.text().strip(),
            "interval_seconds": int(self.interval.value()),
            "exclude_extensions": fw.parse_extensions_text(self.exclude_extensions.text()),
            "exclude_patterns": fw.parse_patterns_text(self.exclude_patterns.text()),
            "enabled": self.enabled.isChecked(),
        }

    def accept(self) -> None:
        rule = self.values()
        if not rule["source"] or not rule["dest"]:
            QMessageBox.warning(self, "フォルダ監視移動", "元フォルダと先フォルダを両方入れてください")
            return
        problem = fw.validate_rule(rule)
        if problem:
            QMessageBox.warning(self, "フォルダ監視移動", problem)
            return
        super().accept()


class FolderWatchSettingsDialog(QDialog):
    """config は folder_watch.normalize_config の形。[OK] で save() が書く。"""

    def __init__(self, config: dict, settings_path=None, app_settings=None, parent=None,
                 choose_folder=None):
        super().__init__(parent)
        self.setWindowTitle("フォルダ監視移動 — ルールの追加・編集")
        self.settings_path = settings_path
        self.app_settings = app_settings
        self.choose_folder = choose_folder
        config = fw.normalize_config(config)

        layout = QVBoxLayout(self)
        head = QLabel("監視ルール(&L)　— 行のチェック＝有効")
        layout.addWidget(head)
        self.list = QListWidget()
        self.list.setMinimumWidth(560)
        head.setBuddy(self.list)
        layout.addWidget(self.list, 1)
        for rule in config["rules"]:
            self._add_row(rule)

        row = QHBoxLayout()
        self.add_button = QPushButton("追加(&A)…")
        self.edit_button = QPushButton("編集(&E)…")
        self.remove_button = QPushButton("削除(&D)")
        row.addWidget(self.add_button)
        row.addWidget(self.edit_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        layout.addLayout(row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(lambda _c=False: self.add_rule())
        self.edit_button.clicked.connect(lambda _c=False: self.edit_current())
        self.remove_button.clicked.connect(lambda _c=False: self.remove_current())
        self.list.itemDoubleClicked.connect(lambda _item: self.edit_current())
        self.list.currentItemChanged.connect(lambda *_a: self._sync_controls())
        self.list.installEventFilter(self)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._sync_controls()

    # --- 行 ---
    def _add_row(self, rule: dict) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if rule["enabled"] else Qt.Unchecked)
        item.setData(Qt.UserRole, rule)
        self._update_row_text(item, rule)
        self.list.addItem(item)
        return item

    @staticmethod
    def _update_row_text(item: QListWidgetItem, rule: dict) -> None:
        item.setText(f"{rule['source']} → {rule['dest']}（{rule['interval_seconds']}秒）")
        if not os.path.isdir(rule["source"]):
            item.setText(item.text() + "　（元フォルダが見つかりません）")
            item.setForeground(Qt.gray)

    def rules(self) -> list:
        result = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            rule = dict(item.data(Qt.UserRole))
            rule["enabled"] = item.checkState() == Qt.Checked
            result.append(rule)
        return result

    # --- 追加・編集・削除 ---
    def _edit_rule(self, rule: dict) -> dict | None:
        dialog = RuleEditDialog(rule, self, choose_folder=self.choose_folder)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.values()

    def add_rule(self) -> bool:
        try:
            rule = self._edit_rule({})
            if rule is None:
                return False
            key = (fw.folder_key(rule["source"]), fw.folder_key(rule["dest"]))
            for i in range(self.list.count()):
                existing = self.list.item(i).data(Qt.UserRole)
                if (fw.folder_key(existing["source"]), fw.folder_key(existing["dest"])) == key:
                    self.list.setCurrentRow(i)
                    return False
            self._add_row(rule)
            self.list.setCurrentRow(self.list.count() - 1)
            return True
        except Exception as e:  # noqa: BLE001  スロットから例外を出さない
            QMessageBox.warning(self, "フォルダ監視移動", f"追加できませんでした\n{e}")
            return False

    def edit_current(self) -> bool:
        item = self.list.currentItem()
        if item is None:
            return False
        try:
            rule = self._edit_rule(dict(item.data(Qt.UserRole)))
            if rule is None:
                return False
            rule["enabled"] = item.checkState() == Qt.Checked
            item.setData(Qt.UserRole, rule)
            item.setForeground(QBrush())  # _update_row_text の付け直し前に灰色を落とす
            self._update_row_text(item, rule)
            return True
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "フォルダ監視移動", f"編集できませんでした\n{e}")
            return False

    def remove_current(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        if self.list.count():
            self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._sync_controls()

    def _sync_controls(self) -> None:
        has_current = self.list.currentItem() is not None
        self.edit_button.setEnabled(has_current)
        self.remove_button.setEnabled(has_current)

    def eventFilter(self, obj, event) -> bool:
        try:
            if (obj is self.list and event.type() == QEvent.KeyPress
                    and event.key() == Qt.Key_Delete):
                self.remove_current()
                return True
        except Exception:  # noqa: BLE001
            pass
        return super().eventFilter(obj, event)

    # --- 決定 ---
    def values(self) -> dict:
        return {"rules": self.rules()}

    def save(self) -> None:
        fw.save_config(self.settings_path, self.values(), self.app_settings)

    def accept(self) -> None:
        try:
            self.save()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "フォルダ監視移動", f"設定を保存できませんでした\n{e}")
            return
        super().accept()


def open_dialog(app_settings, settings_path, parent=None) -> FolderWatchSettingsDialog:
    """設定ダイアログを(モードレスで)開いて返す。呼ぶ側は参照を持っておくこと。"""
    config = fw.load_config(settings_path) if settings_path else fw.normalize_config(
        (app_settings or {}).get(fw.SECTION))
    dialog = FolderWatchSettingsDialog(config, settings_path, app_settings, parent)
    dialog.setAttribute(Qt.WA_DeleteOnClose, False)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
