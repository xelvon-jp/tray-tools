# slideshow_settings.py
# スライドショーの「フォルダと表示の設定」ダイアログ。トレイの「🖼 スライドショー」から開く。
#
#   - フォルダの登録・削除と、フォルダごとの「サブフォルダも含める」(行のチェック)
#   - 表示秒数・フェード秒数・ゆっくりズーム(Ken Burns)
# キー操作だけで使える普通のダイアログ(Alt+頭文字・Space でチェック・Del で削除・Enter で決定)。
# [OK] で初めて settings.json の slideshow の該当キーだけを書く(ほかのキーは消さない)。
#
# 常駐の中で開く小さなダイアログなので、写真は1枚も読まない(読むのは別プロセスの slideshow.py)。
import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

import slideshow_core as sc


class SlideshowSettingsDialog(QDialog):
    """config は slideshow_core.normalize_config の形。[OK] で save() が書く。

    choose_folder はフォルダを選ぶ関数(テストで差し替える。既定は QFileDialog)。"""

    def __init__(self, config: dict, settings_path=None, app_settings=None, parent=None,
                 choose_folder=None):
        super().__init__(parent)
        self.setWindowTitle("スライドショー — フォルダと表示の設定")
        self.settings_path = settings_path
        self.app_settings = app_settings
        self.choose_folder = choose_folder or self._choose_folder
        config = sc.normalize_config(config)

        layout = QVBoxLayout(self)
        head = QLabel("写真のフォルダ(&F)　— 行のチェック＝サブフォルダも含める")
        layout.addWidget(head)
        self.list = QListWidget()
        self.list.setMinimumWidth(520)
        head.setBuddy(self.list)
        layout.addWidget(self.list, 1)
        for folder in config["folders"]:
            self._add_row(folder["path"], folder["recursive"])

        row = QHBoxLayout()
        self.add_button = QPushButton("追加(&A)…")
        self.remove_button = QPushButton("削除(&D)")
        self.recursive_box = QCheckBox("サブフォルダも含める(&S)")
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addStretch(1)
        row.addWidget(self.recursive_box)
        layout.addLayout(row)

        form = QFormLayout()
        self.interval = QDoubleSpinBox()
        self.interval.setRange(*sc.INTERVAL_RANGE)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(config["interval_seconds"])
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0.0, sc.FADE_MAX)
        self.fade.setDecimals(1)
        self.fade.setSingleStep(0.5)
        self.fade.setSuffix(" 秒")
        self.fade.setValue(config["fade_seconds"])
        self.ken_burns = QCheckBox("ゆっくりズーム（Ken Burns）(&K)")
        self.ken_burns.setChecked(config["ken_burns"])
        interval_label = QLabel("1枚の表示(&I)")
        interval_label.setBuddy(self.interval)
        fade_label = QLabel("フェード(&E)")
        fade_label.setBuddy(self.fade)
        form.addRow(interval_label, self.interval)
        form.addRow(fade_label, self.fade)
        form.addRow("", self.ken_burns)
        layout.addLayout(form)
        note = QLabel("モニタはトレイの「🖵 表示するモニタ」で選びます。"
                      "あっふのフォルダで Ctrl+Enter →「このフォルダでスライドショー」でも再生できます。")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_button.clicked.connect(lambda _c=False: self.add_folder())
        self.remove_button.clicked.connect(lambda _c=False: self.remove_current())
        self.recursive_box.toggled.connect(self._on_recursive_toggled)
        self.list.currentItemChanged.connect(lambda *_a: self._sync_controls())
        self.list.itemChanged.connect(lambda *_a: self._sync_controls())
        self.list.installEventFilter(self)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._sync_controls()

    # --- 行 ---
    def _add_row(self, path: str, recursive: bool) -> QListWidgetItem:
        item = QListWidgetItem(path)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if recursive else Qt.Unchecked)
        item.setData(Qt.UserRole, path)     # 表示の文言に関わらず、本当のパスはここ
        if not os.path.isdir(path):
            item.setText(f"{path}　（見つかりません）")
            item.setForeground(Qt.gray)
        item.setToolTip("チェック＝サブフォルダも含める（Space で切り替え）")
        self.list.addItem(item)
        return item

    def folders(self) -> list:
        result = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            result.append({"path": item.data(Qt.UserRole),
                           "recursive": item.checkState() == Qt.Checked})
        return result

    def _choose_folder(self) -> str:
        return QFileDialog.getExistingDirectory(self, "写真のフォルダを選ぶ")

    def add_folder(self, path: str = None) -> bool:
        """フォルダを足す(既にあれば、その行を選ぶだけ)。足したら True。"""
        try:
            path = path if path is not None else self.choose_folder()
            if not path:
                return False
            path = os.path.normpath(path)
            key = sc.folder_key(path)
            for i in range(self.list.count()):
                if sc.folder_key(self.list.item(i).data(Qt.UserRole)) == key:
                    self.list.setCurrentRow(i)
                    return False
            self._add_row(path, True)
            self.list.setCurrentRow(self.list.count() - 1)
            return True
        except Exception as e:  # noqa: BLE001  スロットから例外を出さない
            QMessageBox.warning(self, "スライドショー", f"追加できませんでした\n{e}")
            return False

    def remove_current(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        self.list.takeItem(row)
        if self.list.count():
            self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._sync_controls()

    def _on_recursive_toggled(self, checked: bool) -> None:
        item = self.list.currentItem()
        if item is not None:
            state = Qt.Checked if checked else Qt.Unchecked
            if item.checkState() != state:
                item.setCheckState(state)

    def _sync_controls(self) -> None:
        item = self.list.currentItem()
        self.remove_button.setEnabled(item is not None)
        self.recursive_box.setEnabled(item is not None)
        self.recursive_box.blockSignals(True)
        self.recursive_box.setChecked(item is not None and item.checkState() == Qt.Checked)
        self.recursive_box.blockSignals(False)

    def eventFilter(self, obj, event) -> bool:
        # 一覧で Del を押したら、その行を外す
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
        return {
            "folders": self.folders(),
            "interval_seconds": round(self.interval.value(), 1),
            "fade_seconds": round(self.fade.value(), 1),
            "ken_burns": self.ken_burns.isChecked(),
        }

    def save(self) -> None:
        sc.save_config(self.settings_path, self.values(), self.app_settings)

    def accept(self) -> None:
        try:
            self.save()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "スライドショー", f"設定を保存できませんでした\n{e}")
            return
        super().accept()


def open_dialog(app_settings, settings_path, parent=None) -> SlideshowSettingsDialog:
    """設定ダイアログを(モードレスで)開いて返す。呼ぶ側は参照を持っておくこと。"""
    config = sc.load_config(settings_path) if settings_path else sc.normalize_config(
        (app_settings or {}).get(sc.SECTION))
    dialog = SlideshowSettingsDialog(config, settings_path, app_settings, parent)
    dialog.setAttribute(Qt.WA_DeleteOnClose, False)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
