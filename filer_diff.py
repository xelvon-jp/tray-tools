# filer_diff.py
# あっふ内蔵のテキスト差分ビューア(Alt+D)。WinMerge(Alt+W)と同じ対象の
# 選び方で、2つのファイルを読み取り専用で比べる。外部ツールを入れていない
# 環境でも差分だけはすぐ見られるようにするためのもの。
#
# 差分の計算は difflib の純関数(diff_rows)。行の上限つきで読み、超えた分は
# 切り捨てて断る(巨大なファイル同士を丸ごと比べる用途は WinMerge に任せる)。
import difflib
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget

MAX_BYTES = 2 * 1024 * 1024
MAX_LINES = 5000

DELETE_BG = "#5c2626"
INSERT_BG = "#274d27"


def read_text_lines(path: str):
    """ファイルを (行のリスト, 文字コードの表示名, 切り捨てたか) で読む。

    文字コードの判定はビュア・プレビューと同じ filer_viewer.detect_encoding。"""
    import filer_viewer

    with open(path, "rb") as f:
        data = f.read(MAX_BYTES + 1)
    truncated = len(data) > MAX_BYTES
    data = data[:MAX_BYTES]
    encoding, start = filer_viewer.detect_encoding(data)
    text = data[start:].decode(encoding, errors="replace")
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        truncated = True
    label = filer_viewer.ENCODING_LABELS.get(encoding, encoding)
    return lines, label, truncated


def diff_rows(a_lines, b_lines):
    """差分を (印, 行) の並びにする。印は ' '(同じ) / '-'(左だけ) / '+'(右だけ)。"""
    rows = []
    matcher = difflib.SequenceMatcher(a=a_lines, b=b_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows.extend((" ", line) for line in a_lines[i1:i2])
        if tag in ("delete", "replace"):
            rows.extend(("-", line) for line in a_lines[i1:i2])
        if tag in ("insert", "replace"):
            rows.extend(("+", line) for line in b_lines[j1:j2])
    return rows


def diff_counts(rows):
    """(増えた行数, 減った行数)。"""
    added = sum(1 for mark, _line in rows if mark == "+")
    removed = sum(1 for mark, _line in rows if mark == "-")
    return added, removed


class DiffWindow(QWidget):
    """差分を出す独立窓。読み取り専用。Esc で閉じる。"""

    def __init__(self, parent, title_a: str, title_b: str, rows, font, colors: dict,
                 summary: str = ""):
        super().__init__(parent)
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(f"差分 {os.path.basename(title_a)} ⇔ {os.path.basename(title_b)}")
        self.resize(880, 560)
        layout = QVBoxLayout(self)
        head = QLabel(summary)
        head.setTextFormat(Qt.PlainText)
        head.setFont(font)
        layout.addWidget(head)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setFont(font)
        self.view.setStyleSheet(f"background-color: {colors['background']};"
                                f" color: {colors['text']}; border: none;")
        self.view.setPlainText("\n".join(f"{mark} {line}" for mark, line in rows))
        self._paint_rows(rows)
        layout.addWidget(self.view, 1)

    def _paint_rows(self, rows) -> None:
        """変わった行にだけ背景色を付ける(ブロック書式。スクロールしても残る)。"""
        doc = self.view.document()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        try:
            for index, (mark, _line) in enumerate(rows):
                if mark == " ":
                    continue
                block = doc.findBlockByNumber(index)
                if not block.isValid():
                    continue
                fmt = QTextBlockFormat()
                fmt.setBackground(QColor(DELETE_BG if mark == "-" else INSERT_BG))
                cursor.setPosition(block.position())
                cursor.setBlockFormat(fmt)
        finally:
            cursor.endEditBlock()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
