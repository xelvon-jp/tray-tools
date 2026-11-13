# editor/bookmark_dialog.py
# ブックマークの一覧の窓。今開いている文書の印の付いた行を
# 「行番号: 行の内容」で並べ、選んだ行へ飛ぶ。作りは すべて検索
# の窓(findall_dialog.py)とそろえてある(一覧は QListWidget、
# Enter かダブルクリックで on_jump(行番号 1始まり) が呼ばれて
# 窓が閉じる)。
#
# 印の状態は本文(editor/text_edit.py)が持っていて、変わるのは
# 利用者が付け外ししたときだけなので、grep や すべて検索 の
# ような裏のスレッドは要らない。項目は開いた時点の控えを
# 受け取る(この窓はモーダルなので、開いている間に本文は
# 変わらない)。
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from editor import editor_icon


class BookmarkDialog(QDialog):
    """ブックマークの付いた行を一覧する窓。

    entries は (行番号 1始まり, 行の内容) の並び。on_jump は
    (行番号) を受ける関数で、結果を選んだときに呼ばれ、窓は閉じる。"""

    def __init__(self, parent=None, *, entries=(), on_jump=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。ここは循環
        # import を避けるため文字列で持つ(findall_dialog と同じ)。
        self.setWindowTitle("ブックマーク一覧 - EDDy")
        self.resize(720, 480)
        self._on_jump = on_jump

        layout = QVBoxLayout(self)
        entries = list(entries)
        if entries:
            self._status_label = QLabel(
                f"{len(entries)} 件のブックマークがあります")
        else:
            self._status_label = QLabel("ブックマークはありません")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._results = QListWidget()
        for line, text in entries:
            item = QListWidgetItem(f"{line}: {text}")
            item.setData(Qt.ItemDataRole.UserRole, line)
            self._results.addItem(item)
        self._results.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self._results, 1)

    def _on_item_activated(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and self._on_jump is not None:
            self._on_jump(int(data))
            self.accept()
