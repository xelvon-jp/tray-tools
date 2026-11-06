# test_filer_diff.py
# あっふ内蔵の差分ビューア(filer_diff.py)のテスト。計算は純関数、窓は offscreen。
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import filer_diff as fd


def test_diff_rows():
    a = ["one", "two", "three"]
    b = ["one", "TWO", "three", "four"]
    rows = fd.diff_rows(a, b)
    assert rows == [
        (" ", "one"),
        ("-", "two"),
        ("+", "TWO"),
        (" ", "three"),
        ("+", "four"),
    ], rows
    assert fd.diff_counts(rows) == (2, 1)
    assert fd.diff_rows(a, a) == [(" ", line) for line in a]
    assert fd.diff_rows([], b) == [("+", line) for line in b]
    assert fd.diff_rows(a, []) == [("-", line) for line in a]


def test_read_text_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "a.txt")
        path.write_text("あ\nい\n", encoding="utf-8")
        lines, label, truncated = fd.read_text_lines(str(path))
        assert lines == ["あ", "い"] and not truncated and label
        big = Path(tmp, "big.txt")
        big.write_text("\n".join(f"row{i}" for i in range(fd.MAX_LINES + 10)),
                       encoding="utf-8")
        lines, _label, truncated = fd.read_text_lines(str(big))
        assert len(lines) == fd.MAX_LINES and truncated


def test_diff_window_offscreen():
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    rows = fd.diff_rows(["one", "two"], ["one", "TWO"])
    colors = {"background": "#00001c", "text": "#f1efe8"}
    window = fd.DiffWindow(None, "C:/a.txt", "C:/b.txt", rows,
                           QFont("Meiryo", 9), colors, "追加 1 行 / 削除 1 行")
    window.show()
    app.processEvents()
    text = window.view.toPlainText()
    assert text.splitlines() == ["  one", "- two", "+ TWO"]
    doc = window.view.document()
    bg_same = doc.findBlockByNumber(0).blockFormat().background()
    bg_del = doc.findBlockByNumber(1).blockFormat().background()
    bg_add = doc.findBlockByNumber(2).blockFormat().background()
    from PySide6.QtCore import Qt as _Qt
    assert bg_same.style() == _Qt.NoBrush   # 同じ行には背景を付けない
    assert bg_del.color() == QColor(fd.DELETE_BG)
    assert bg_add.color() == QColor(fd.INSERT_BG)
    window.close()
    window.deleteLater()


ALL_TESTS = [test_diff_rows, test_read_text_lines, test_diff_window_offscreen]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
