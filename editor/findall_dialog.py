# editor/findall_dialog.py
# 今開いている文書の中の「すべて検索」の窓。検索の実体は
# editor/findall.py の純粋関数で、ここは入力・結果の一覧と
# 「選んだ行へ飛ぶ」だけ。作りは grep の窓(grep_dialog.py)と
# そろえてある(裏のスレッドで探し、Signal 経由で窓に戻す。
# ワーカースレッドから Qt を直接触らない)。
#
# 対象の本文は、窓を開いた時点の控えを受け取る。この窓は
# モーダルなので、開いている間に本文は変わらない。
# 一覧の項目を選んで Enter かダブルクリックで、on_jump(行番号
# 1始まり) が呼ばれ、この窓は閉じる。
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from editor import editor_icon
from editor import findall as findallmod
from editor import rxfind


class _FindAllBridge(QObject):
    """検索スレッド → 窓への受け渡し。"""

    finished = Signal(object)


class FindAllDialog(QDialog):
    """今開いている文書の中を検索して、ヒットした行を一覧する窓。

    on_jump は (行番号) を受ける関数。結果を選んだときに呼ばれ、
    窓は閉じる。"""

    def __init__(self, parent=None, *, text="", word="",
                 case_sensitive=False, regex=False, on_jump=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。ここは循環
        # import を避けるため文字列で持つ(grep_dialog と同じ)。
        self.setWindowTitle("すべて検索 - EDDy")
        self.resize(720, 480)
        self._text = text
        self._on_jump = on_jump
        self._bridge = _FindAllBridge(self)
        self._bridge.finished.connect(self._on_finished)
        self._thread = None

        layout = QVBoxLayout(self)
        word_row = QHBoxLayout()
        word_row.addWidget(QLabel("検索"))
        self._word_edit = QLineEdit(word)
        self._word_edit.returnPressed.connect(self.start_search)
        word_row.addWidget(self._word_edit, 1)
        self._case_check = QCheckBox("大文字小文字を区別")
        self._case_check.setChecked(bool(case_sensitive))
        word_row.addWidget(self._case_check)
        self._regex_check = QCheckBox("正規表現")
        self._regex_check.setChecked(bool(regex))
        word_row.addWidget(self._regex_check)
        self._search_button = QPushButton("検索")
        self._search_button.clicked.connect(self.start_search)
        word_row.addWidget(self._search_button)
        layout.addLayout(word_row)

        self._status_label = QLabel(
            f"結果は先頭 {findallmod.MAX_RESULTS} 件まで表示します")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._results = QListWidget()
        self._results.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self._results, 1)

    # ---------------------------------------------------------------
    def start_search(self):
        if self._thread is not None:
            return
        word = self._word_edit.text()
        if not word:
            self._status_label.setText("検索する文字列を入れてください")
            return
        self._results.clear()
        self._search_button.setEnabled(False)
        self._status_label.setText("検索しています…")
        text = self._text
        case = self._case_check.isChecked()
        regex = self._regex_check.isChecked()

        def run():
            try:
                payload = findallmod.find_all(
                    text, word, case_sensitive=case, regex=regex)
            except rxfind.RegexFault as e:
                payload = e
            self._bridge.finished.emit(payload)

        self._thread = threading.Thread(
            target=run, daemon=True, name="editor-findall")
        self._thread.start()

    def _on_finished(self, payload):
        self._thread = None
        self._search_button.setEnabled(True)
        if isinstance(payload, rxfind.RegexFault):
            self._status_label.setText(str(payload))
            return
        result = payload
        for match in result.matches:
            item = QListWidgetItem(f"{match.line}: {match.text}")
            item.setData(Qt.ItemDataRole.UserRole, match.line)
            self._results.addItem(item)
        if not result.matches:
            self._status_label.setText("見つかりませんでした")
            return
        text = f"{len(result.matches)} 件見つかりました"
        if result.truncated:
            text += (f" — 件数が多いため先頭 {findallmod.MAX_RESULTS}"
                     " 件で打ち切りました")
        self._status_label.setText(text)

    def _on_item_activated(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and self._on_jump is not None:
            self._on_jump(int(data))
            self.accept()
