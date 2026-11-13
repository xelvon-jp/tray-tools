# tests/test_rect_edit.py
# 矩形選択の共用部品(rect_edit.py)を offscreen で確かめる。桁は
# 表示幅(全角=2)で数えること、混じった行での入力・削除・コピー、
# エディタ側(fixed_rows=False)の貼り付けでの行追加と、リネーマー
# 側(fixed_rows=True)の行数不変を、実 Qt で確かめる。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_rect_edit.py
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtWidgets import QApplication, QPlainTextEdit  # noqa: E402

import rect_edit  # noqa: E402
from rect_edit import (  # noqa: E402
    RectEditMixin,
    char_width,
    col_of_index,
    display_width,
    index_at_col,
    line_range_indices,
)

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


class HostEdit(RectEditMixin, QPlainTextEdit):
    """部品だけを載せた最小の受け皿。"""

    def __init__(self, fixed_rows):
        super().__init__()
        self.init_rect_selection(fixed_rows=fixed_rows)

    def _on_rect_changed(self):
        QPlainTextEdit.setExtraSelections(self, self.rect_extra_selections())


def make_edit(text, fixed_rows=False):
    app()
    edit = HostEdit(fixed_rows)
    edit.setPlainText(text)
    edit.show()
    return edit


def test_char_width_fullwidth_counts_two():
    assert char_width("a") == 1
    assert char_width("あ") == 2
    assert char_width("Ａ") == 2
    assert char_width(" ") == 1


def test_display_width_counts_fullwidth_and_tabs():
    assert display_width("abc") == 3
    assert display_width("あい") == 4
    assert display_width("aあb") == 4
    # タブは次の 4 の倍数の桁まで進む
    assert display_width("\t") == 4
    assert display_width("ab\t") == 4
    assert display_width("abcde\t") == 8


def test_col_and_index_roundtrip_with_fullwidth():
    line = "あbい"          # 表示桁: あ=0-1, b=2, い=3-4
    assert col_of_index(line, 0) == 0
    assert col_of_index(line, 1) == 2
    assert col_of_index(line, 2) == 3
    assert col_of_index(line, 3) == 5
    assert index_at_col(line, 0, "left") == 0
    assert index_at_col(line, 2, "left") == 1
    assert index_at_col(line, 3, "left") == 2
    # 全角文字の内側: 左端は文字の頭、右端は文字の後ろ
    assert index_at_col(line, 1, "left") == 0
    assert index_at_col(line, 1, "right") == 1
    assert index_at_col(line, 4, "right") == 3


def test_line_range_pads_short_lines():
    # 短い行は開始桁まで空白で埋められる(編集で同じ桁に書けるように)
    line, i0, i1 = line_range_indices("ab", 4, 6)
    assert line == "ab  "
    assert (i0, i1) == (4, 4)


def test_block_insert_fullwidth_keeps_columns_aligned():
    edit = make_edit("あい\nab")
    edit.set_block_selection((0, 2), (1, 2))
    edit.block_insert_text("X")
    # 表示桁2は、1行目では「い」の頭、2行目では行末。どちらも
    # 「表示桁2の前」に同じ文字が入る(桁でそろう)。
    assert edit.toPlainText() == "あXい\nabX"


def test_block_text_fullwidth():
    edit = make_edit("あいう\nabcde")
    edit.set_block_selection((0, 2), (1, 4))
    # 表示桁 2..4: 1行目は「い」、2行目は "cd"
    assert edit.block_text() == "い\ncd"


def test_block_delete_fullwidth_backward():
    edit = make_edit("あい\nab")
    edit.set_block_selection((0, 4), (1, 4))
    edit.block_delete(True)
    # 1行目は桁4の直前の「い」(幅2)が消え、2行目は行が桁4に
    # 届かないのでそのまま。矩形は先頭行で消えた幅(2)だけ左へ。
    assert edit.toPlainText() == "あ\nab"
    _first, _last, c0, c1 = edit.block_range()
    assert (c0, c1) == (2, 2)


def test_block_delete_selection_range_fullwidth():
    edit = make_edit("あいうえ\nabcdef")
    edit.set_block_selection((0, 2), (1, 4))
    edit.block_delete(False)
    # 表示桁 2..4 を消す: 1行目は「い」、2行目は "cd"
    assert edit.toPlainText() == "あうえ\nabef"


def test_block_paste_editor_mode_adds_rows():
    edit = make_edit("x\ny", fixed_rows=False)
    edit.set_block_selection((0, 1), (1, 1))
    edit.block_paste("1\n2\n3")
    # 行が足りない分は新しい行が足される(空白で桁をそろえる)
    assert edit.toPlainText() == "x1\ny2\n 3"


def test_block_paste_fixed_rows_keeps_row_count():
    edit = make_edit("ab\ncd", fixed_rows=True)
    edit.set_block_selection((0, 1), (1, 1))
    edit.block_paste("1\n2\n3")
    assert edit.toPlainText() == "a1b\nc2d"
    assert edit.document().blockCount() == 2


def test_block_paste_single_line_fills_all_rows():
    edit = make_edit("あ\nい", fixed_rows=False)
    edit.set_block_selection((0, 2), (1, 2))
    edit.block_paste("Z")
    assert edit.toPlainText() == "あZ\nいZ"


def test_block_rows_and_clear():
    edit = make_edit("aa\nbb\ncc")
    assert not edit.has_block_selection()
    edit.set_block_selection((2, 0), (0, 1))
    assert edit.has_block_selection()
    assert list(edit.block_rows()) == [0, 1, 2]
    edit.clear_block_selection()
    assert not edit.has_block_selection()


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("FAIL", name, type(e).__name__, e)
    print(f"{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
