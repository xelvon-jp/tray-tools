# editor/grep_dialog.py
# フォルダ内の横断検索(grep)の窓。検索の実体は editor/grep.py の
# 純粋関数で、ここは入力・進捗・結果の一覧と「選んだ行へ飛ぶ」だけ。
#
# 検索は裏のスレッドで走らせ、進捗と結果は Signal 経由で窓に戻す
# (ワーカースレッドから Qt を直接触らない。_SendBridge と同じ作法)。
# 一覧の項目を選んで Enter かダブルクリックで、on_jump(パス, 行番号
# 1始まり) が呼ばれ、この窓は閉じる。
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from editor import editor_icon
from editor import grep as grepmod


class _GrepBridge(QObject):
    """検索スレッド → 窓への受け渡し。"""

    progress = Signal(int, str)
    finished = Signal(object)


class GrepDialog(QDialog):
    """フォルダを選んで横断検索する窓。

    on_jump は (パス, 行番号) を受ける関数。結果を選んだときに
    呼ばれ、窓は閉じる。"""

    def __init__(self, parent=None, *, folder="", word="",
                 case_sensitive=False, on_jump=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。ここは循環
        # import を避けるため文字列で持つ。
        self.setWindowTitle("フォルダ内を検索 - EDDy")
        self.resize(720, 480)
        self._on_jump = on_jump
        self._bridge = _GrepBridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.finished.connect(self._on_finished)
        self._thread = None

        layout = QVBoxLayout(self)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("フォルダ"))
        self._folder_edit = QLineEdit(folder)
        folder_row.addWidget(self._folder_edit, 1)
        browse = QPushButton("参照…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        word_row = QHBoxLayout()
        word_row.addWidget(QLabel("検索"))
        self._word_edit = QLineEdit(word)
        self._word_edit.returnPressed.connect(self.start_search)
        word_row.addWidget(self._word_edit, 1)
        self._case_check = QCheckBox("大文字小文字を区別")
        self._case_check.setChecked(bool(case_sensitive))
        word_row.addWidget(self._case_check)
        self._search_button = QPushButton("検索")
        self._search_button.clicked.connect(self.start_search)
        word_row.addWidget(self._search_button)
        layout.addLayout(word_row)

        self._status_label = QLabel(
            f"対象はテキストファイル（{grepmod.MAX_FILE_BYTES // (1024 * 1024)} MB まで）。"
            f"結果は先頭 {grepmod.MAX_RESULTS} 件まで表示します")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._results = QListWidget()
        self._results.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self._results, 1)

    # ---------------------------------------------------------------
    def _browse_folder(self):
        start = self._folder_edit.text() or ""
        folder = QFileDialog.getExistingDirectory(self, "検索するフォルダ", start)
        if folder:
            self._folder_edit.setText(folder)

    def start_search(self):
        if self._thread is not None:
            return
        folder = self._folder_edit.text().strip()
        word = self._word_edit.text()
        if not folder or not os.path.isdir(folder):
            self._status_label.setText("フォルダを選んでください")
            return
        if not word:
            self._status_label.setText("検索する文字列を入れてください")
            return
        self._results.clear()
        self._search_button.setEnabled(False)
        self._status_label.setText("検索しています…")
        case = self._case_check.isChecked()

        def run():
            result = grepmod.grep_folder(
                folder, word, case_sensitive=case,
                progress=lambda n, path: self._bridge.progress.emit(n, path))
            self._bridge.finished.emit(result)

        self._thread = threading.Thread(
            target=run, daemon=True, name="editor-grep")
        self._thread.start()

    def _on_progress(self, count, path):
        self._status_label.setText(
            f"検索しています…（{count} ファイル目: {os.path.basename(path)}）")

    def _on_finished(self, result):
        self._thread = None
        self._search_button.setEnabled(True)
        for match in result.matches:
            item = QListWidgetItem(
                f"{os.path.basename(match.path)}  {match.line}: {match.text}")
            item.setData(Qt.ItemDataRole.UserRole, (match.path, match.line))
            item.setToolTip(match.path)
            self._results.addItem(item)
        text = (f"{len(result.matches)} 件見つかりました"
                f"（{result.files_scanned} ファイルを検索）")
        if result.truncated:
            text += f" — 件数が多いため先頭 {grepmod.MAX_RESULTS} 件で打ち切りました"
        if not result.matches:
            text = f"見つかりませんでした（{result.files_scanned} ファイルを検索）"
        self._status_label.setText(text)

    def _on_item_activated(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and self._on_jump is not None:
            path, line = data
            self._on_jump(path, line)
            self.accept()
