# editor/outline_pane.py
# エディタの左ペイン。アウトライン(見出しのツリー)が主役で、下部に
# ファイルの情報を小さく出す。あとから別の表示を載せる場所でもある
# ので、窓からは独立した部品にしてある。
#
# 抽出と階層付けは editor/outline.py の純粋関数が担い、ここは表示と
# 「項目が選ばれたら jump_requested に行番号(0始まり)を流す」だけ。
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

PANE_WIDTH = 220


class OutlinePane(QWidget):
    """左ペイン。アウトラインのツリー＋下部のファイル情報。"""

    jump_requested = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(PANE_WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._tree, 1)

        self._path_label = QLabel("—")
        self._path_label.setWordWrap(True)
        self._format_label = QLabel("—")
        self._format_label.setWordWrap(True)
        self._size_label = QLabel("—")
        self._counts_label = QLabel("—")
        self._cursor_label = QLabel("—")
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(2)
        form.addRow("パス", self._path_label)
        form.addRow("形式", self._format_label)
        form.addRow("大きさ", self._size_label)
        form.addRow("行数", self._counts_label)
        form.addRow("位置", self._cursor_label)
        layout.addLayout(form)

    # ---------------------------------------------------------------
    # アウトライン
    # ---------------------------------------------------------------
    def set_outline(self, roots):
        """build_outline_tree の結果(OutlineNode の並び)でツリーを
        作り直す。全て展開した状態にする。"""
        self._tree.clear()
        for node in roots:
            self._tree.addTopLevelItem(self._to_item(node))
        self._tree.expandAll()

    def _to_item(self, node):
        item = QTreeWidgetItem([node.entry.text])
        item.setData(0, Qt.ItemDataRole.UserRole, node.entry.line)
        for child in node.children:
            item.addChild(self._to_item(child))
        return item

    def _on_item_clicked(self, item, _column):
        line = item.data(0, Qt.ItemDataRole.UserRole)
        if line is not None:
            self.jump_requested.emit(int(line))

    def tree(self) -> QTreeWidget:
        return self._tree

    # ---------------------------------------------------------------
    # 下部のファイル情報
    # ---------------------------------------------------------------
    def set_file_info(self, *, path, format_text, size_text):
        self._path_label.setText(path if path else "（無題）")
        self._format_label.setText(format_text)
        self._size_label.setText(size_text)

    def set_counts(self, line_count, char_count):
        self._counts_label.setText(f"{line_count} 行 / {char_count} 文字")

    def set_cursor_position(self, line, column):
        self._cursor_label.setText(f"{line} 行 {column} 桁")
