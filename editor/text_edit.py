# editor/text_edit.py
# エディタの本文ウィジェット。QPlainTextEdit に、行番号エリアと
# Ctrl+ホイールでのフォントサイズ変更、矩形選択(rect_edit の共用
# 部品。こちらの矩形は行を足せる)を足したもの。
#
# 行番号エリアは Qt の定番の作り(CodeEditor の例と同じ): エディタの
# 左端に細い帯を置き、viewport の余白で本文をずらす。スクロールと
# 再描画は updateRequest に追随する。
#
# 第3段で足した表示:
#   - 現在行の強調(薄い背景)。矩形のハイライトと同じ extra
#     selections の口に相乗りする(矩形側の差し替えに消されないよう、
#     まとめて出し直す _refresh_extra_selections が唯一の出口)
#   - 不可視文字の表示(タブ・スペース・全角スペース・改行を記号で
#     描く)。描画だけの話で、文書の内容は変えない
#   - 配色(editor/colors.py の組)をパレットと各部の色へ反映する
#   - 編集マーク: 開いて(または保存して)から編集した行の行番号を
#     別の色で描く。マークの追随は改行種別の控えと同じく
#     contentsChange で行い、基準化(開く・保存)は窓が
#     clear_edited_lines で行う
#   - ブックマーク: 印を付けた行の行番号のわきに ● を描く(編集
#     マーク=行番号の文字色 とは別物の記号なので、両方付いた行でも
#     見分けられる)。印の追随も contentsChange。編集マークと違い
#     保存では消えず、文書を開き直す・新規にするときだけ窓が
#     clear_bookmarks で消す(ファイルには保存しない)
#
# キーの通知(窓側がマクロ記録と F4 のために受ける):
#   move_key(命令名, 選択を広げるか) … Home / End / 矢印などの素の
#     移動キー。本文の動き自体は Qt の標準のまま(こちらでは実行
#     しない)。記録側は「起きたこと」として控えるだけ。
#   undo_redo_started / undo_redo_finished … Ctrl+Z / Ctrl+Y の
#     前後。文書の変化を記録から外すために、窓側が印を立てる。
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QPainter,
    QPalette,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import QPlainTextEdit, QStyle, QTextEdit, QWidget

import rect_edit
from editor import colors as colormod
from editor import document as docmod

# 移動キー → 命令名(keymap・マクロと共通の命令層の名前)。
_MOVE_KEYS = {
    Qt.Key.Key_Home: "move_line_start",
    Qt.Key.Key_End: "move_line_end",
    Qt.Key.Key_Up: "move_line_up",
    Qt.Key.Key_Down: "move_line_down",
    Qt.Key.Key_Left: "move_char_left",
    Qt.Key.Key_Right: "move_char_right",
}

FONT_SIZE_MIN = 7
FONT_SIZE_MAX = 32
# フォントの大きさが取れないとき(Pixel 指定等)の代わりの起点。
FALLBACK_FONT_SIZE = 10

# タブの表示幅(ピクセル)の Qt の既定。タブ幅(文字数)の指定が無い
# ときは触らないが、一度指定したあとで解除するときはこの値に戻す。
DEFAULT_TAB_STOP_DISTANCE = 80.0

# 行番号エリアの色は配色(editor/colors.py)が持つ。標準の組の値が
# 従来の (242,242,242) / (144,144,144) と同じにしてある。


# 改行の種別 → 行末に描く記号。CR と LF を分けて出す(混在した文書
# でも種類が見分けられること)。字形は MS Gothic にある矢印の範囲。
NEWLINE_GLYPHS = {"\r\n": "←↓", "\n": "↓", "\r": "←"}
# 文書の末尾(最後のブロックの末尾)に出す印。小さい字で描く。
EOF_MARK = "EOF"

# ブックマークのある行に、行番号のわきへ描く印。
BOOKMARK_GLYPH = "●"


def wrap_right_margin(available_width, char_width, document_margin, column):
    """桁数で折り返すための右余白(ピクセル)を計算する純粋関数。

    QPlainTextEdit には桁数指定の折り返しが無いので、右に余白を
    取って本文の見える幅を狭める。available_width は「余白を変えても
    動かない幅」(窓全体の幅から行番号エリア・スクロールバー・枠を
    引いたもの)を渡すこと。今の viewport の幅を基準にすると、余白を
    変えるたびに基準自体が動き、resizeEvent と行ったり来たりして
    プロセスごと落ちる(スタックオーバーフロー。レビュー
    req-20261113-109 の指摘)。
    目標は「本文の幅 = column × char_width + 文書の余白(両側)」。
    桁数が無い・文字幅が取れない・幅が足りないときは 0。"""
    if not column or column <= 0 or char_width <= 0:
        return 0
    target = column * char_width + 2 * document_margin
    return max(0, int(round(available_width - target)))


class LineNumberArea(QWidget):
    """行番号を描く帯。描画そのものはエディタ側が持つ(本文のブロック
    形状が要るため)。"""

    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit

    def sizeHint(self):  # noqa: N802  Qt の上書き
        return QSize(self._edit.line_number_area_width(), 0)

    def paintEvent(self, event):  # noqa: N802  Qt の上書き
        self._edit.paint_line_number_area(event)


class TextEdit(rect_edit.RectEditMixin, QPlainTextEdit):
    """エディタの本文。font_size_changed には、Ctrl+ホイールで変わった
    あとの大きさ(pt)が流れる(窓側が設定へ保存する)。move_key には
    移動キーの命令名と「選択を広げるか」が流れる(窓側がマクロ記録の
    材料にする。本文は標準のまま動く)。unrecorded_move は、Ctrl+矢印
    など命令で表せない移動が起きた合図。mouse_pressed はマウスが
    押された合図(どちらも、記録中に起きたら窓側が利用者へ知らせる)。"""

    font_size_changed = Signal(int)
    move_key = Signal(str, bool)
    unrecorded_move = Signal()
    mouse_pressed = Signal()
    undo_redo_started = Signal()
    undo_redo_finished = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._line_numbers = True
        self._wrap_column = None
        # タブの表示幅(半角の文字数)。None は Qt の既定のまま
        # 触らない(距離を設定しない)。
        self._tab_width = None
        self._updating_margins = False
        # 行ごとの改行種別の控え(開いた文書の元の並び。表示専用)。
        # 編集には _track_line_newlines が追随させ、控えが無い・
        # 足りない行は _default_newline で描く。
        self._line_newlines = None
        self._default_newline = "\r\n"
        # 貼り付けの最中だけ、貼るテキストの改行種別の並びをここに
        # 置く(_track_line_newlines が挿入された改行の種別として拾う)。
        self._paste_kinds = None
        self.document().contentsChange.connect(self._track_line_newlines)
        # 編集マーク(ブロック番号の組)。開いた直後・保存した直後が
        # 基準で、そこから変わった行に付く。_edited_base_count は
        # 直前の contentsChange 時点のブロック数(増減の切り分け用)。
        self._edited_lines = set()
        self._edited_base_count = self.document().blockCount()
        self.document().contentsChange.connect(self._track_edited_lines)
        # ブックマーク(ブロック番号の組)。開いている間だけの印で、
        # 追随は編集マークと同じ作法(_track_bookmarks)。
        # _bookmarks_base_count は直前の contentsChange 時点の
        # ブロック数(増減の切り分け用)。
        self._bookmarks = set()
        self._bookmarks_base_count = self.document().blockCount()
        self.document().contentsChange.connect(self._track_bookmarks)
        self._wheel_remainder = 0.0
        self._area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_area_width)
        self.updateRequest.connect(self._on_update_request)
        # スクロールバーの出入りで本文の幅が変わったら、桁数折り返し
        # の余白を計算し直す(計算の基準は窓全体の幅なので安定)。
        self.verticalScrollBar().rangeChanged.connect(
            lambda *_args: self._update_area_width())
        self._update_area_width()
        # エディタの矩形は、貼り付けで行が足りなければ足せる。
        self.init_rect_selection(fixed_rows=False)
        # 表示(第3段)。配色は既定(標準)で始め、窓が設定の組で上書きする。
        self._show_invisibles = False
        self._current_line_enabled = True
        self._scheme = colormod.get_scheme(colormod.DEFAULT_SCHEME)
        self._current_line_color = QColor(self._scheme["current_line"])
        self._invisible_color = QColor(self._scheme["invisible"])
        self._area_background = QColor(
            self._scheme["line_number_background"])
        self._area_foreground = QColor(
            self._scheme["line_number_foreground"])
        self._edited_color = QColor(
            self._scheme["line_number_edited_foreground"])
        self._bookmark_color = QColor(
            self._scheme["line_number_bookmark_foreground"])
        self.cursorPositionChanged.connect(self._refresh_extra_selections)

    def _on_rect_changed(self) -> None:
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        """extra selections の唯一の出口。矩形のハイライトと現在行の
        強調を合わせて出し直す(片方の更新でもう片方が消えないよう)。"""
        selections = list(self.rect_extra_selections())
        if self._current_line_enabled:
            # ExtraSelection の型は QTextEdit 側にある(rect_edit と同じ)。
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(self._current_line_color)
            selection.format.setProperty(
                QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            selections.append(selection)
        QPlainTextEdit.setExtraSelections(self, selections)

    # ---------------------------------------------------------------
    # 配色
    # ---------------------------------------------------------------
    def apply_colors(self, scheme):
        """editor/colors.py の配色の組を、パレットと各部の色へ反映する。"""
        self._scheme = dict(scheme)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base,
                         QColor(scheme["background"]))
        palette.setColor(QPalette.ColorRole.Text,
                         QColor(scheme["foreground"]))
        palette.setColor(QPalette.ColorRole.Highlight,
                         QColor(scheme["selection_background"]))
        palette.setColor(QPalette.ColorRole.HighlightedText,
                         QColor(scheme["foreground"]))
        self.setPalette(palette)
        self._current_line_color = QColor(scheme["current_line"])
        self._invisible_color = QColor(scheme["invisible"])
        self._area_background = QColor(scheme["line_number_background"])
        self._area_foreground = QColor(scheme["line_number_foreground"])
        self._edited_color = QColor(
            scheme["line_number_edited_foreground"])
        self._bookmark_color = QColor(
            scheme["line_number_bookmark_foreground"])
        self._refresh_extra_selections()
        self._area.update()
        self.viewport().update()

    def color_scheme(self) -> dict:
        return dict(self._scheme)

    # ---------------------------------------------------------------
    # 現在行の強調
    # ---------------------------------------------------------------
    def current_line_enabled(self) -> bool:
        return self._current_line_enabled

    def set_current_line_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled == self._current_line_enabled:
            return
        self._current_line_enabled = enabled
        self._refresh_extra_selections()

    # ---------------------------------------------------------------
    # 不可視文字の表示(描画だけ。本文は変えない)
    # ---------------------------------------------------------------
    def show_invisibles(self) -> bool:
        return self._show_invisibles

    def set_show_invisibles(self, visible):
        visible = bool(visible)
        if visible == self._show_invisibles:
            return
        self._show_invisibles = visible
        self.viewport().update()

    def set_line_newlines(self, kinds, default_newline=None):
        """行ごとの改行種別の控えを据える(窓が開く・保存のたびに渡す)。

        kinds はブロック番号と同じ並びで、値は改行そのもの
        ("\\r\\n" / "\\n" / "\\r")か、改行が無い行は None。据えた後の
        編集には _track_line_newlines が追随させる。それでも控えが
        無い行は default_newline(文書の改行設定)で描く。描画だけの
        話で、文書の内容には触らない。"""
        self._line_newlines = list(kinds) if kinds is not None else None
        if default_newline:
            self._default_newline = default_newline
        if self._show_invisibles:
            self.viewport().update()

    def line_newlines(self):
        """行ごとの改行種別の控えの写し。控えが無ければ None。

        編集への追随(_track_line_newlines)を受けた今の並びなので、
        保存のときに「いま混在しているか」の判定と、「混在のまま」
        書くときの材料に使う(窓が保存の直前に読み取る)。"""
        if self._line_newlines is None:
            return None
        return list(self._line_newlines)

    def _track_line_newlines(self, position, chars_removed, chars_added):
        """文書の変化に、改行種別の控えを追随させる。

        控えはブロック番号と対応する並びなので、行を足す・消す・
        並べ替えると、そのままでは記号がずれる。contentsChange
        (位置・削除文字数・追加文字数)から、変わった範囲のブロック
        だけを作り直す:
          - 挿入された改行で終わる行は、挿入テキスト自身の種別
            (貼り付けのときだけ insertFromMimeData が _paste_kinds
            に控えておく)か、無ければ文書の改行設定で描く。キー
            入力で増えた行は文書の改行設定になる
          - 範囲の最後の行は、元の並びで範囲の最後だった行の種別を
            引き継ぐ。範囲より後ろの行の種別は、そのまま後ろへ
            ずれる(削除で行が消えても、残りの行の記号は合う)
        元に戻す・やり直しでも同じ信号が来るので、範囲の作り直し
        としては追随する。ただし、戻した行の改行種別そのものまでは
        復元できない(文書側に残っていない)ので、文書の改行設定で
        描く側に倒れる。"""
        old = self._line_newlines
        if old is None or (chars_removed == 0 and chars_added == 0):
            return
        doc = self.document()
        new_count = doc.blockCount()
        last_pos = max(0, doc.characterCount() - 1)
        first_block = doc.findBlock(min(position, last_pos))
        if not first_block.isValid():
            return
        first = first_block.blockNumber()
        end_block = doc.findBlock(min(position + chars_added, last_pos))
        last = end_block.blockNumber() if end_block.isValid() else first
        if last < first:
            last = first
        span_new = last - first + 1
        span_old = span_new - (new_count - len(old))
        if span_old < 1:
            span_old = 1
        if span_new == 1 and span_old == 1:
            return  # 行の出入りが無い変化。控えはそのまま合っている
        paste = self._paste_kinds
        entries = []
        for j in range(span_new):
            if j < span_new - 1:
                # 挿入された改行で終わる行。
                if paste is not None and j < len(paste):
                    entries.append(paste[j])
                else:
                    entries.append(self._default_newline)
            else:
                # 範囲の最後の行は、元の範囲の最後の行の種別を引き継ぐ。
                idx = first + span_old - 1
                entries.append(old[idx] if 0 <= idx < len(old) else None)
        new_kinds = old[:first] + entries + old[first + span_old:]
        if len(new_kinds) != new_count:
            # 想定外の変化への保険。長さだけはブロック数に合わせ、
            # 引き当てが壊れないことを優先する。
            if len(new_kinds) < new_count:
                tail = new_kinds[-1:] or [None]
                new_kinds = (
                    new_kinds[:-1]
                    + [self._default_newline] * (new_count - len(new_kinds))
                    + tail)
            else:
                new_kinds = new_kinds[:new_count - 1] + [None]
        self._line_newlines = new_kinds
        if self._show_invisibles:
            self.viewport().update()

    def insertFromMimeData(self, source):  # noqa: N802  Qt の上書き
        """貼り付け・ドロップ。貼るテキスト自身の改行種別を、控えの
        追随(_track_line_newlines)が拾えるよう、挿入の前に切り出して
        おく。控え自体が無い(新規文書など)ときは、今ある行はすべて
        文書の改行設定だという前提で控えを作り、追随を始める。"""
        kinds = None
        if source is not None and source.hasText():
            kinds = docmod.line_endings(source.text())[:-1]
        if self._line_newlines is None:
            count = self.document().blockCount()
            self._line_newlines = (
                [self._default_newline] * (count - 1) + [None])
        self._paste_kinds = kinds
        try:
            super().insertFromMimeData(source)
        finally:
            self._paste_kinds = None

    def newline_glyph(self, block_number) -> str:
        """ブロック(行)を終える改行の記号。控えから種別を引き、
        無ければ文書の改行設定で決める。"""
        kind = None
        if (self._line_newlines is not None
                and 0 <= block_number < len(self._line_newlines)):
            kind = self._line_newlines[block_number]
        if kind is None:
            kind = self._default_newline
        return NEWLINE_GLYPHS.get(kind, NEWLINE_GLYPHS["\n"])

    def block_end_mark(self, block) -> str:
        """ブロックの末尾に描く印。改行があればその種別の記号、
        文書の最後のブロックなら EOF_MARK。"""
        if block.next().isValid():
            return self.newline_glyph(block.blockNumber())
        return EOF_MARK

    # ---------------------------------------------------------------
    # 編集マーク(開いて・保存してから編集した行)
    # ---------------------------------------------------------------
    def edited_lines(self):
        """編集マークの付いた行のブロック番号(昇順のリスト)。"""
        return sorted(self._edited_lines)

    def edited_line_color(self) -> QColor:
        """編集マークの行番号に使う色(配色の組から)。"""
        return QColor(self._edited_color)

    def clear_edited_lines(self):
        """編集マークを全部消し、いまの内容を新しい基準にする。

        窓が、文書を開いた直後と保存した直後に呼ぶ。ブロック数の
        控えもいまの文書に合わせ直す(差し替えの最中の追随で残った
        ずれを、ここで断ち切る)。"""
        self._edited_lines = set()
        self._edited_base_count = self.document().blockCount()
        self._area.update()

    def _track_edited_lines(self, position, chars_removed, chars_added):
        """文書の変化に、編集マークを追随させる。

        考え方は改行種別の控え(_track_line_newlines)と同じで、
        変わった範囲(スパン)の新しい行にマークを付ける:
          - 範囲より前の行はそのまま、後ろの行は増減ぶんずれる
          - 範囲に含まれた古い行のマークは消え、代わりに新しい
            行に付く(行が消えれば、その行のマークも消える)
        例外を2つだけ置く:
          - 追加がちょうど行の境目で終わり、変化の位置も行の
            先頭だったとき(行の先頭への行ごとの貼り付け等)は、
            境目の先の行は変わっていないので範囲に含めない
          - 削除だけで行が減り、位置が行の先頭に来たとき(行ごとの
            削除)は、位置に残った行そのものは変わっていないので
            新しくは付けない。ただし、その行がもともと範囲の
            最後の行としてマークを持っていたら引き継ぐ(消えた
            行のマークが次の行へ移るのとは別の話)
        元に戻す・やり直しの変化も同じく追随する。開いた時点の
        内容に完全に戻しても、変わった行のマークは残る(保存すれば
        消える)。マークは「基準から一度でも変わった行」の印で、
        内容の比較まではしない、という単純な決め。"""
        if chars_removed == 0 and chars_added == 0:
            return
        doc = self.document()
        new_count = doc.blockCount()
        old_count = self._edited_base_count
        self._edited_base_count = new_count
        last_pos = max(0, doc.characterCount() - 1)
        first_block = doc.findBlock(min(position, last_pos))
        if not first_block.isValid():
            return
        first = first_block.blockNumber()
        end_block = doc.findBlock(min(position + chars_added, last_pos))
        last = end_block.blockNumber() if end_block.isValid() else first
        if chars_added > 0 and end_block.isValid() \
                and position + chars_added == end_block.position() \
                and position == first_block.position():
            # 追加がちょうど行の境目で終わった。境目の先の行は
            # 変化に含まれない(上の例外1)。
            last -= 1
        if last < first:
            last = first
        span_new = last - first + 1
        delta = new_count - old_count
        span_old = span_new - delta
        if span_old < 0:
            span_old = 0
        pure_line_removal = (
            chars_added == 0 and delta < 0
            and position == first_block.position())
        updated = set()
        for line in self._edited_lines:
            if line < first:
                updated.add(line)
            elif line >= first + span_old:
                updated.add(line + delta)
        if pure_line_removal:
            # 範囲の最後に残った行のマークだけ引き継ぐ(例外2)。
            if first + span_old - 1 in self._edited_lines:
                updated.add(last)
        else:
            updated.update(range(first, last + 1))
        if updated != self._edited_lines:
            self._edited_lines = updated
            self._area.update()

    # ---------------------------------------------------------------
    # ブックマーク(開いている間だけの行の印)
    # ---------------------------------------------------------------
    def bookmarks(self):
        """ブックマークの付いた行のブロック番号(昇順のリスト)。"""
        return sorted(self._bookmarks)

    def bookmark_color(self) -> QColor:
        """ブックマークの印に使う色(配色の組から)。"""
        return QColor(self._bookmark_color)

    def toggle_bookmark(self, block_number) -> bool:
        """行のブックマークを付け外しする。付けたら True、外したら
        False。範囲外の行番号は何もしない(戻り値は今の状態)。"""
        if not 0 <= block_number < self.document().blockCount():
            return block_number in self._bookmarks
        if block_number in self._bookmarks:
            self._bookmarks.discard(block_number)
            marked = False
        else:
            self._bookmarks.add(block_number)
            marked = True
        self._area.update()
        return marked

    def clear_bookmarks(self):
        """ブックマークを全部消す。

        窓が、文書を開いた直後と新規にした直後に呼ぶ(保存では
        呼ばない。編集マークと違い、保存しても印は残る)。ブロック数
        の控えもいまの文書に合わせ直す。"""
        self._bookmarks = set()
        self._bookmarks_base_count = self.document().blockCount()
        self._area.update()

    def _track_bookmarks(self, position, chars_removed, chars_added):
        """文書の変化に、ブックマークを追随させる。

        変わった範囲(スパン)の切り分けは編集マーク
        (_track_edited_lines)と同じで、行の境目で終わる追加の例外も
        同じ。違うのは範囲の中の扱いだけ:
          - 範囲より前の行はそのまま、後ろの行は増減ぶんずれる
          - 範囲の中の行は、範囲の中での順番のまま残る(行の中で
            打った・行を割った等では、印はその行に残る)。新しい
            範囲の行数を超えた分は、行ごと消えたので印も消える
          - 行ごとの削除(削除だけで行が減り、位置が行頭に来た
            とき)は編集マークと同じ例外で、範囲の最後に残った行
            だけが引き継ぐ。消えた行の印が次の行へ移ることはない
        元に戻す・やり直しの変化も同じく追随する。"""
        if chars_removed == 0 and chars_added == 0:
            return
        doc = self.document()
        new_count = doc.blockCount()
        old_count = self._bookmarks_base_count
        self._bookmarks_base_count = new_count
        last_pos = max(0, doc.characterCount() - 1)
        first_block = doc.findBlock(min(position, last_pos))
        if not first_block.isValid():
            return
        first = first_block.blockNumber()
        end_block = doc.findBlock(min(position + chars_added, last_pos))
        last = end_block.blockNumber() if end_block.isValid() else first
        if chars_added > 0 and end_block.isValid() \
                and position + chars_added == end_block.position() \
                and position == first_block.position():
            # 追加がちょうど行の境目で終わった。境目の先の行は
            # 変化に含まれない(編集マークと同じ例外)。
            last -= 1
        if last < first:
            last = first
        span_new = last - first + 1
        delta = new_count - old_count
        span_old = span_new - delta
        if span_old < 0:
            span_old = 0
        pure_line_removal = (
            chars_added == 0 and delta < 0
            and position == first_block.position())
        updated = set()
        for line in self._bookmarks:
            if line < first:
                updated.add(line)
            elif line >= first + span_old:
                updated.add(line + delta)
            elif pure_line_removal:
                # 範囲の最後に残った行だけ引き継ぐ。
                if line == first + span_old - 1:
                    updated.add(last)
            else:
                # 範囲の中の行は、順番のまま残る。新しい行数を
                # 超えた分(行ごと消えた分)は印も消える。
                offset = line - first
                if offset < span_new:
                    updated.add(first + offset)
        if updated != self._bookmarks:
            self._bookmarks = updated
            self._area.update()

    def _marker_font(self):
        """ブックマークの印用の、本文より小さいフォント(EOF の印と
        同じ割り切り)。"""
        font = QFont(self.font())
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(6.0, font.pointSizeF() * 0.7))
        elif font.pixelSize() > 0:
            font.setPixelSize(max(6, round(font.pixelSize() * 0.7)))
        return font

    def _eof_font(self):
        """EOF の印用の、本文より小さいフォント。"""
        font = QFont(self.font())
        if font.pointSizeF() > 0:
            font.setPointSizeF(max(6.0, font.pointSizeF() * 0.7))
        elif font.pixelSize() > 0:
            font.setPixelSize(max(6, round(font.pixelSize() * 0.7)))
        return font

    def paintEvent(self, event):  # noqa: N802  Qt の上書き
        super().paintEvent(event)
        if self._show_invisibles:
            self._paint_invisibles()

    def _paint_invisibles(self):
        """見えている行のタブ・スペース・改行・末尾を記号で描く。

        タブは →、半角スペースは ·、全角スペースは □。行末には
        改行の種別の記号(LF は ↓、CR は ←、CRLF は ←↓)を、文書の
        最後の行には EOF の印を小さい字で描く。すべて配色の
        invisible の色(本文より薄い色)。位置は cursorRect で取る
        (折り返しやスクロールに追随する)。描くのは見えている
        ブロックだけに絞る。"""
        painter = QPainter(self.viewport())
        painter.setPen(self._invisible_color)
        painter.setFont(self.font())
        metrics = self.fontMetrics()
        eof_metrics = QFontMetrics(self._eof_font())

        def draw_mark(rect, mark, mark_metrics):
            # cursorRect の幅はカーソルの幅(1px。太いカーソルでも
            # 3px)しか無く、drawText はその幅で字を切り取って
            # しまう(・も→も幅1pxに潰れて見えなくなる)。記号の
            # 実幅まで広げてから描く。
            width = mark_metrics.horizontalAdvance(mark)
            if rect.width() < width:
                rect.setWidth(width)
            painter.drawText(rect, Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter, mark)

        scratch = QTextCursor(self.document())
        block = self.firstVisibleBlock()
        viewport_height = self.viewport().height()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(
                self.contentOffset())
            if geometry.top() > viewport_height:
                break
            if block.isVisible():
                text = block.text()
                base = block.position()
                for i, ch in enumerate(text):
                    if ch == "\t":
                        symbol = "→"
                    elif ch == " ":
                        symbol = "·"
                    elif ch == "　":
                        symbol = "□"
                    else:
                        continue
                    scratch.setPosition(base + i)
                    rect = self.cursorRect(scratch)
                    draw_mark(rect, symbol, metrics)
                scratch.setPosition(base + len(text))
                rect = self.cursorRect(scratch)
                mark = self.block_end_mark(block)
                if mark == EOF_MARK:
                    painter.setFont(self._eof_font())
                    draw_mark(rect, mark, eof_metrics)
                    painter.setFont(self.font())
                else:
                    draw_mark(rect, mark, metrics)
            block = block.next()

    def keyPressEvent(self, event):  # noqa: N802  Qt の上書き
        if (event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            # 元に戻す・やり直しの最中の文書変化は、記録の材料に
            # しない(窓側が印で受ける)。
            self.undo_redo_started.emit()
            try:
                super().keyPressEvent(event)
            finally:
                self.undo_redo_finished.emit()
            return
        if self.has_block_selection() and self.block_key(event):
            return
        name = _MOVE_KEYS.get(event.key())
        if name is not None:
            mods = event.modifiers()
            # 素の移動か(Shift の選択拡張とキーパッドは素に含める)。
            # 修飾キーを明示的に見る。~ の反転で計算すると、PySide6
            # では Ctrl 等が付いていても素の扱いになってしまう。
            extra = mods & (Qt.KeyboardModifier.ControlModifier
                            | Qt.KeyboardModifier.AltModifier
                            | Qt.KeyboardModifier.MetaModifier)
            if extra:
                # Ctrl+矢印の単語移動・Ctrl+Home の文書頭などは、
                # 1文字・1行の移動の命令では表せない。記録には入れず、
                # 窓側へ知らせる(黙って1文字移動として記録しない)。
                self.unrecorded_move.emit()
            else:
                self.move_key.emit(
                    name, bool(mods & Qt.KeyboardModifier.ShiftModifier))
        super().keyPressEvent(event)

    def mousePressEvent(self, event):  # noqa: N802  Qt の上書き
        # マウスでのカーソル移動はマクロに記録できない。記録中なら
        # 窓側が知らせるために合図する。
        self.mouse_pressed.emit()
        super().mousePressEvent(event)

    # ---------------------------------------------------------------
    # 行番号
    # ---------------------------------------------------------------
    def line_numbers_visible(self) -> bool:
        return self._line_numbers

    def set_line_numbers_visible(self, visible):
        visible = bool(visible)
        if visible == self._line_numbers:
            return
        self._line_numbers = visible
        self._area.setVisible(visible)
        self._update_area_width()
        self._area.update()

    def line_number_area_width(self) -> int:
        if not self._line_numbers:
            return 0
        digits = len(str(max(1, self.blockCount())))
        return (self.fontMetrics().horizontalAdvance("9") * digits + 10
                + self._bookmark_marker_width())

    def _bookmark_marker_width(self) -> int:
        """ブックマークの印(●)を描く柱の幅。印が1つも無いときも
        幅は確保したままにする(付け外しで本文が左右に動かない)。"""
        return self.fontMetrics().horizontalAdvance(BOOKMARK_GLYPH) + 4

    def _update_area_width(self, *_args):
        # 左は行番号エリア、右は桁数折り返しの余白。両方ここで決める。
        # 入り直しの印と「同じなら呼ばない」で、setViewportMargins が
        # 起こす resizeEvent との往復を断つ(レビュー req-20261113-109)。
        if self._updating_margins:
            return
        left = self.line_number_area_width()
        right = self._wrap_right_margin()
        current = self.viewportMargins()
        if (current.left() == left and current.top() == 0
                and current.right() == right and current.bottom() == 0):
            return
        self._updating_margins = True
        try:
            self.setViewportMargins(left, 0, right, 0)
        finally:
            self._updating_margins = False

    # ---------------------------------------------------------------
    # 桁数での折り返し(右の余白で見える幅を狭める)
    # ---------------------------------------------------------------
    def set_wrap_column(self, column):
        """桁数で折り返す。column 桁(半角の文字数)で折り返して
        見えるよう、右に余白を取る。None で解除(余白を戻す)。
        折り返しそのものは窓側が WidgetWidth にして行う。"""
        if column is not None:
            try:
                column = int(column)
            except (TypeError, ValueError):
                column = None
            if column is not None and column <= 0:
                column = None
        if column == self._wrap_column:
            if column is not None:
                self._update_area_width()
            return
        self._wrap_column = column
        self._update_area_width()

    def wrap_column(self):
        return self._wrap_column

    # ---------------------------------------------------------------
    # タブの表示幅(半角の文字数で指定する)
    # ---------------------------------------------------------------
    def set_tab_width(self, width):
        """タブの表示幅を、半角の文字数で指定する。None で解除
        (Qt の既定の距離に戻す)。距離の実体は setTabStopDistance で、
        フォントの文字幅から計算するので、フォントが変わったら
        changeEvent 側で計算し直す。"""
        if width is not None:
            try:
                width = int(width)
            except (TypeError, ValueError):
                width = None
            if width is not None and width <= 0:
                width = None
        if width == self._tab_width:
            return
        self._tab_width = width
        self._refresh_tab_stop()

    def tab_width(self):
        return self._tab_width

    def _refresh_tab_stop(self):
        if self._tab_width is None:
            self.setTabStopDistance(DEFAULT_TAB_STOP_DISTANCE)
            return
        width = self._char_width()
        if width > 0:
            self.setTabStopDistance(width * self._tab_width)

    def _char_width(self) -> float:
        """半角1文字の幅(ピクセル)。取れなければ 0。"""
        metrics = self.fontMetrics()
        width = metrics.averageCharWidth()
        if width <= 0:
            width = metrics.horizontalAdvance("0")
        return width

    def _wrap_right_margin(self) -> int:
        if not self._wrap_column:
            return 0
        # 基準は窓全体の幅から、余白で動かない分(行番号エリア・
        # スクロールバー・枠)を引いた安定した幅にする。今の viewport
        # の幅を基準にすると往復して落ちる(wrap_right_margin の説明)。
        available = (self.width() - self.line_number_area_width()
                     - self._scrollbar_extent() - 2 * self.frameWidth())
        return wrap_right_margin(
            available, self._char_width(),
            self.document().documentMargin(), self._wrap_column)

    def _scrollbar_extent(self) -> int:
        """縦スクロールバーの幅(出ていなくても一定の前提で数える)。"""
        return self.style().pixelMetric(
            QStyle.PixelMetric.PM_ScrollBarExtent)

    def _on_update_request(self, rect, dy):
        if dy:
            self._area.scroll(0, dy)
        else:
            self._area.update(0, rect.y(), self._area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_area_width()

    def resizeEvent(self, event):  # noqa: N802  Qt の上書き
        super().resizeEvent(event)
        contents = self.contentsRect()
        self._area.setGeometry(QRect(
            contents.left(), contents.top(),
            self.line_number_area_width(), contents.height()))
        # 窓の幅が変わったら、桁数折り返しの余白も計算し直す。
        self._update_area_width()

    def changeEvent(self, event):  # noqa: N802  Qt の上書き
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # 字の大きさが変わると番号の桁幅も変わる。
            self._update_area_width()
            self._area.update()
            # タブの表示幅は文字幅から計算するので、フォントが
            # 変わったら距離も計算し直す(指定が無ければ触らない)。
            if self._tab_width is not None:
                self._refresh_tab_stop()

    def paint_line_number_area(self, event):
        painter = QPainter(self._area)
        painter.fillRect(event.rect(), self._area_background)
        height = self.fontMetrics().height()
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        geometry = self.blockBoundingGeometry(block)
        top = geometry.translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                marker_width = self._bookmark_marker_width()
                if number in self._bookmarks:
                    # ブックマークの行は、行番号のわきに印を描く
                    # (編集マーク=行番号の文字色 とは別物の記号)。
                    painter.setPen(self._bookmark_color)
                    painter.setFont(self._marker_font())
                    painter.drawText(
                        0, int(top), marker_width, height,
                        Qt.AlignmentFlag.AlignCenter, BOOKMARK_GLYPH)
                    painter.setFont(self.font())
                # 編集マークの行は、行番号の色を変えて描く。
                painter.setPen(
                    self._edited_color if number in self._edited_lines
                    else self._area_foreground)
                painter.drawText(
                    marker_width, int(top),
                    self._area.width() - marker_width - 4, height,
                    Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            if not block.isValid():
                break
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            number += 1

    # ---------------------------------------------------------------
    # フォントサイズ(Ctrl+ホイール)
    # ---------------------------------------------------------------
    def font_size(self) -> int:
        """今のフォントの大きさ(pt)。取れなければ代わりの値。"""
        size = self.font().pointSizeF()
        if size <= 0:
            return FALLBACK_FONT_SIZE
        return int(round(size))

    def set_font_size(self, size):
        """大きさを範囲内に丸めて設定する。変わったら Signal を出す。"""
        size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, int(size)))
        if size == self.font_size():
            return
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)
        self.font_size_changed.emit(size)

    def wheelEvent(self, event):  # noqa: N802  Qt の上書き
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                # 1ノッチ(120)で1pt。細かい刻みのホイールでも
                # 合計が1ノッチに届いた分だけ動くよう、端数を貯める。
                self._wheel_remainder += delta / 120.0
                steps = int(self._wheel_remainder)
                if steps:
                    self._wheel_remainder -= steps
                    self.set_font_size(self.font_size() + steps)
            event.accept()
            return
        super().wheelEvent(event)
