# rect_edit.py
# 矩形選択(Alt+ドラッグで複数行の同じ桁をまとめて編集)の共通部品。
#
# 元は あっふ の一括改名(filer_renemo.py の RenameEdit)にあった実装を
# 切り出したもの。テキストエディタ(editor/text_edit.py の TextEdit)と
# リネーマーの両方から使う。桁は「文字の位置」ではなく「表示幅」で
# 数える(全角文字は2桁)。日本語の行でも矩形と中身がずれないため。
#
# 桁の数え方(純粋関数の側):
#   - 全角(East Asian Width が W / F)は2桁、それ以外は1桁
#   - タブは次のタブストップ(4桁)まで
#   - 表示桁 → 文字位置の変換で、桁が全角文字の真ん中に落ちたら、
#     左端はその文字の頭、右端はその文字の後ろに丸める(どちらの端でも
#     その文字を含む側)
#
# 行の数を変えるかどうかは部品のパラメータ(fixed_rows):
#   - リネーマーは True(1行が1ファイルなので行は増やせない。矩形への
#     貼り付けで余った行は捨てる)
#   - エディタは False(貼り付けで行が足りなければ行を足す)
import unicodedata

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QTextEdit

TAB_WIDTH = 4


def char_width(ch: str) -> int:
    """1文字の表示幅。全角は2、それ以外は1(タブは位置で変わるので、
    ここでは1としておき、桁の計算は display_width 側で扱う)。"""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _span(ch: str, col: int, tab_width: int) -> int:
    """col の位置にある ch が占める桁数。タブは次のストップまで。"""
    if ch == "\t":
        return tab_width - (col % tab_width)
    return char_width(ch)


def display_width(text: str, tab_width: int = TAB_WIDTH) -> int:
    """文字列の表示幅(桁数)。"""
    col = 0
    for ch in text:
        col += _span(ch, col, tab_width)
    return col


def col_of_index(text: str, index: int, tab_width: int = TAB_WIDTH) -> int:
    """文字位置 index の表示桁(その文字の左端の桁)。"""
    return display_width(text[:index], tab_width)


def index_at_col(text: str, dcol: int, edge: str = "left",
                 tab_width: int = TAB_WIDTH) -> int:
    """表示桁 dcol に対応する文字位置。

    桁が文字の真ん中に落ちたら、edge="left" ではその文字の頭、
    edge="right" ではその文字の後ろを返す(範囲の端では、その文字を
    含む側に倒れる)。行の幅を超えた桁は末尾を返す。"""
    if dcol <= 0:
        return 0
    col = 0
    for i, ch in enumerate(text):
        w = _span(ch, col, tab_width)
        if dcol <= col:
            return i
        if col < dcol < col + w:
            return i if edge == "left" else i + 1
        col += w
    return len(text)


def line_range_indices(line: str, d0: int, d1: int):
    """行 line の表示桁の範囲 [d0, d1) を文字位置に直す。

    戻り値は (行, 開始位置, 終了位置)。行が d0 まで届いていなければ、
    空白で埋めて d0 に届かせた行を返す(矩形の編集では、短い行にも
    同じ桁へ書き込むため)。"""
    if display_width(line) < d0:
        line = line + " " * (d0 - display_width(line))
    i0 = index_at_col(line, d0, "left")
    i1 = i0 if d1 <= d0 else index_at_col(line, d1, "right")
    return line, i0, max(i0, i1)


class RectEditMixin:
    """QPlainTextEdit に矩形選択を足す mixin。

    使い方は、QPlainTextEdit との多重継承 + __init__ で
    init_rect_selection(fixed_rows=...) を呼ぶこと。矩形の状態が
    変わるたびに _on_rect_changed() が呼ばれるので、ハイライトの
    重ね方を持つ側(リネーマーの行ハイライト等)はそこで描き直す。

    矩形の座標は [開始行, 開始桁, 現在行, 現在桁] で、桁は表示幅。"""

    BLOCK_COLOR = "#b8d8f8"   # 矩形選択の地

    def init_rect_selection(self, fixed_rows: bool = False) -> None:
        self._rect_fixed_rows = bool(fixed_rows)
        self._block = None
        self._block_drag = False
        # 矩形のドラッグが欄の外へ出たら、出た向きへ少しずつ送る
        # (離すまで続ける)。マウスを止めていても送れるよう、移動の
        # イベントではなくタイマーで回す。
        self._drag_pos = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(40)
        self._scroll_timer.timeout.connect(self._auto_scroll)

    # --- 状態 ---
    def has_block_selection(self) -> bool:
        return self._block is not None

    def block_range(self):
        """正規化した (先頭行, 末行, 左桁, 右桁)。矩形が無ければ None。
        桁は表示幅。"""
        if self._block is None:
            return None
        ab, ac, cb, cc = self._block
        return min(ab, cb), max(ab, cb), min(ac, cc), max(ac, cc)

    def block_rows(self):
        """矩形が選んでいる行の一覧。無ければ None。"""
        rng = self.block_range()
        if rng is None:
            return None
        return list(range(rng[0], rng[1] + 1))

    def set_block_selection(self, anchor, current) -> None:
        """(行, 表示桁) の組で矩形を作る。マウス操作とテストの入口。"""
        self._block = [anchor[0], anchor[1], current[0], current[1]]
        self._on_rect_changed()

    def clear_block_selection(self) -> None:
        if self._block is not None:
            self._block = None
            self._on_rect_changed()

    def _on_rect_changed(self) -> None:
        """矩形が変わったときの受け皿。ハイライトの描き直しは
        持つ側がここで行う(既定は何もしない)。"""

    # --- ハイライト ---
    def rect_extra_selections(self) -> list:
        rng = self.block_range()
        if rng is None:
            return []
        first, last, d0, d1 = rng
        selections = []
        doc = self.document()
        for row in range(first, last + 1):
            block = doc.findBlockByNumber(row)
            if not block.isValid():
                continue
            _line, i0, i1 = line_range_indices(block.text(), d0, d1)
            # 選択の位置は行の実文字数までに丸める(空白で埋めた分は
            # 実在しないので、次の行へはみ出させない)。
            i0 = min(i0, block.length() - 1)
            i1 = min(i1, block.length() - 1)
            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + i0)
            cursor.setPosition(block.position() + i1, QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(self.BLOCK_COLOR))
            sel.format = fmt
            sel.cursor = cursor
            selections.append(sel)
        return selections

    # --- 中身の読み書き ---
    def block_text(self) -> str:
        """矩形の中身(行ごとの切り取りを改行でつないだもの)。"""
        rng = self.block_range()
        if rng is None:
            return ""
        first, last, d0, d1 = rng
        doc = self.document()
        parts = []
        for row in range(first, last + 1):
            block = doc.findBlockByNumber(row)
            if not block.isValid():
                parts.append("")
                continue
            _line, i0, i1 = line_range_indices(block.text(), d0, d1)
            parts.append(block.text()[i0:i1])
        return "\n".join(parts)

    def _block_edit_lines(self, make, pad: bool = True) -> None:
        """矩形の各行を make(行文字列, 開始位置, 終了位置) -> 新しい
        行文字列 で差し替える。全行分を1回の取り消しで戻せるように
        する。位置は表示桁から直した文字位置。

        pad=True では、開始桁まで届かない短い行を空白で埋めてから
        渡す(挿入・貼り付け用: 同じ桁へ書き込むため)。pad=False では
        生の行のまま渡す(削除用: 消すだけの行に空白を足さない)。"""
        rng = self.block_range()
        if rng is None:
            return
        first, last, d0, d1 = rng
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for row in range(first, last + 1):
                block = doc.findBlockByNumber(row)
                if not block.isValid():
                    continue
                if pad:
                    line, i0, i1 = line_range_indices(block.text(), d0, d1)
                else:
                    line = block.text()
                    i0 = index_at_col(line, d0, "left")
                    i1 = i0 if d1 <= d0 else index_at_col(line, d1, "right")
                c = QTextCursor(block)
                c.select(QTextCursor.LineUnderCursor)
                c.insertText(make(line, i0, i1))
        finally:
            cursor.endEditBlock()

    def block_insert_text(self, text: str) -> None:
        """矩形の範囲を text で置き換える(短い行は左桁まで空白で
        埋める)。置き換えた後は、text の直後に幅0の矩形を残す
        (続けて打てる)。"""
        if self.block_range() is None:
            return
        self._block_edit_lines(
            lambda line, i0, i1: line[:i0] + text + line[i1:])
        _first, _last, d0, _d1 = self.block_range()
        width = display_width(text)
        self._block[1] = self._block[3] = d0 + width
        self._on_rect_changed()

    def block_delete(self, backward: bool) -> None:
        """矩形の範囲を消す。幅0なら BS/Del として桁の前後1文字を
        全行で消す(行頭・行末を越えて行をつなぐことはしない)。"""
        rng = self.block_range()
        if rng is None:
            return
        _first, _last, d0, d1 = rng
        if d0 != d1:
            self._block_edit_lines(
                lambda line, i0, i1: line[:i0] + line[i1:], pad=False)
            self._block[1] = self._block[3] = d0
        elif backward and d0 > 0:
            # 消えた文字の幅だけ矩形を左へ寄せる。幅は行ごとに違い
            # うるので、実際に変わる最初の行で編集前に測る。
            doc = self.document()
            shift = 0
            for row in range(_first, _last + 1):
                block = doc.findBlockByNumber(row)
                if not block.isValid() or display_width(block.text()) < d0:
                    continue
                i = index_at_col(block.text(), d0, "left")
                if i > 0:
                    shift = (col_of_index(block.text(), i)
                             - col_of_index(block.text(), i - 1))
                    break

            def drop_before(line, _i0, _i1):
                if display_width(line) < d0:
                    return line
                i = index_at_col(line, d0, "left")
                if i <= 0:
                    return line
                return line[:i - 1] + line[i:]
            self._block_edit_lines(drop_before, pad=False)
            if shift:
                self._block[1] = self._block[3] = d0 - shift
        elif not backward:
            def drop_after(line, _i0, _i1):
                if display_width(line) <= d0:
                    return line
                i = index_at_col(line, d0, "left")
                if i >= len(line):
                    return line
                return line[:i] + line[i + 1:]
            self._block_edit_lines(drop_after, pad=False)
        self._on_rect_changed()

    def block_paste(self, text: str) -> None:
        """矩形への貼り付け。1行なら全行へ、複数行なら行ごとに配る。

        複数行が矩形の行数より多いときは、fixed_rows なら余りを
        捨て、そうでなければ行を足して配る。"""
        rng = self.block_range()
        if rng is None:
            return
        parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(parts) <= 1:
            self.block_insert_text(text)
            return
        first, last, d0, d1 = rng
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for i, row in enumerate(range(first, last + 1)):
                block = doc.findBlockByNumber(row)
                if not block.isValid():
                    continue
                piece = parts[i] if i < len(parts) else ""
                line, i0, i1 = line_range_indices(block.text(), d0, d1)
                c = QTextCursor(block)
                c.select(QTextCursor.LineUnderCursor)
                c.insertText(line[:i0] + piece + line[i1:])
            extra = parts[last - first + 1:]
            if extra and not self._rect_fixed_rows:
                # 行が足りない分は、末尾の行の下へ新しい行を足す。
                # 新しい行は左桁まで空白で埋めて、その桁から配る。
                tail = doc.findBlockByNumber(last)
                c = QTextCursor(tail)
                c.movePosition(QTextCursor.EndOfBlock)
                for piece in extra:
                    c.insertBlock()
                    c.insertText(" " * d0 + piece)
        finally:
            cursor.endEditBlock()
        self.clear_block_selection()

    # --- マウス ---
    def mousePressEvent(self, event):  # noqa: N802  Qt の上書き
        if (event.button() == Qt.LeftButton
                and event.modifiers() & Qt.AltModifier):
            cursor = self.cursorForPosition(event.pos())
            anchor = (cursor.blockNumber(),
                      col_of_index(cursor.block().text(),
                                   cursor.positionInBlock()))
            self._block = [anchor[0], anchor[1], anchor[0], anchor[1]]
            self._block_drag = True
            plain = QTextCursor(cursor)
            plain.clearSelection()
            self.setTextCursor(plain)
            self._on_rect_changed()
            event.accept()
            return
        if self._block is not None and not (event.modifiers() & Qt.AltModifier):
            self.clear_block_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802  Qt の上書き
        if self._block_drag:
            self._drag_pos = event.pos()
            self._extend_block(self._drag_pos)
            if self.viewport().rect().contains(self._drag_pos):
                self._scroll_timer.stop()
            elif not self._scroll_timer.isActive():
                self._scroll_timer.start()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802  Qt の上書き
        if self._block_drag:
            self._block_drag = False
            self._scroll_timer.stop()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _extend_block(self, pos) -> None:
        """矩形の端を pos(欄の座標)の文字へ。欄の外なら欄の縁で測る。"""
        rect = self.viewport().rect()
        inside = QPoint(min(max(pos.x(), rect.left()), rect.right()),
                        min(max(pos.y(), rect.top()), rect.bottom()))
        cursor = self.cursorForPosition(inside)
        self._block[2] = cursor.blockNumber()
        self._block[3] = col_of_index(cursor.block().text(),
                                      cursor.positionInBlock())
        self._on_rect_changed()

    def _auto_scroll(self) -> None:
        """矩形のドラッグ中にマウスが欄の外にあれば、その向きへ
        送って端を伸ばす。離れているほど速く送る(縦は行、横は
        およそ文字の幅の単位)。"""
        if not self._block_drag or self._drag_pos is None or self._block is None:
            self._scroll_timer.stop()
            return
        rect = self.viewport().rect()
        pos = self._drag_pos
        dy = (pos.y() - rect.bottom() if pos.y() > rect.bottom()
              else pos.y() - rect.top() if pos.y() < rect.top() else 0)
        dx = (pos.x() - rect.right() if pos.x() > rect.right()
              else pos.x() - rect.left() if pos.x() < rect.left() else 0)
        if not dx and not dy:
            self._scroll_timer.stop()
            return
        if dy:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + (1 + abs(dy) // 20) * (1 if dy > 0 else -1))
        if dx:
            bar = self.horizontalScrollBar()
            step = QFontMetrics(self.font()).horizontalAdvance("0")
            bar.setValue(bar.value() + step * (1 + abs(dx) // 20) * (1 if dx > 0 else -1))
        self._extend_block(pos)

    # --- キー ---
    def block_key(self, event) -> bool:
        """矩形が生きている間のキー。処理したら True。

        英数の直接入力は全行に行き渡る(IME の入力は通常のカーソル
        位置に入るので、矩形の対象外)。矢印キーなどは矩形を解いて
        通常の動きへ。"""
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key_Escape:
            self.clear_block_selection()
            return True
        if key == Qt.Key_Backspace:
            self.block_delete(backward=True)
            return True
        if key == Qt.Key_Delete:
            self.block_delete(backward=False)
            return True
        if mods & Qt.ControlModifier:
            if key == Qt.Key_C:
                QGuiApplication.clipboard().setText(self.block_text())
                return True
            if key == Qt.Key_X:
                QGuiApplication.clipboard().setText(self.block_text())
                self.block_delete(backward=True)
                return True
            if key == Qt.Key_V:
                text = QGuiApplication.clipboard().text()
                if text:
                    self.block_paste(text)
                return True
            self.clear_block_selection()
            return False
        if mods & (Qt.AltModifier | Qt.MetaModifier):
            self.clear_block_selection()
            return False
        text = event.text()
        if text and text.isprintable():
            self.block_insert_text(text)
            return True
        self.clear_block_selection()
        return False
