# filer_renemo.py
# あっふの一括改名の窓(Renemo の画面に寄せたもの)。改名の中身の判断は
# filer_rename.py、実際の改名は呼び出し側(on_execute / on_undo)が行う。
#
#   列: 名前(元の名前・読み取り専用) | 編集名(1行1ファイルで編集) | サイズ | 更新日時 | 種類
#   - 編集名は既定で拡張子を隠す(.ext のトグルで拡張子も編集の対象に)。名前の部分と
#     拡張子の部分を分けて持つので、切り替えても編集した内容は消えない
#   - C:¥ のトグルで「名前」の列をフルパスに
#   - 編集した行は色で示す。確定前の検査で問題のある行は赤で示して実行させない
#   - 行の追加・削除はさせない(Enter は何もしない、行頭の BS・行末の Del で行を
#     つなげない)。範囲を消して行数が変わったときは、実行のときに止める
#   - 実行は Ctrl+Enter かツールバーの「実行」。Esc で閉じる(未実行の編集があれば確かめる)
#
# 配色は Renemo と同じ明るい配色(白地)にしている。ここは文字を打って直す
# エディタなので、ファイラの濃紺より白地に黒の方が長い名前を読み書きしやすい。
import os
import time

from PySide6.QtCore import QEvent, QFileInfo, QRect, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import filer_core as core
import filer_rename as fr
import filer_shell

EDITED_COLOR = "#fff3b0"     # 編集した行
ERROR_COLOR = "#ffc0c0"      # 確定前の検査で問題のある行
SORT_KINDS = (("name", "名前順"), ("original", "元の並び"), ("edited", "編集済みを先に"))


class _Gutter(QWidget):
    """編集名の左の欄(行番号とファイルの種類のアイコン)。"""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)


class RenameEdit(QPlainTextEdit):
    """編集名の欄。1行が1ファイルで、行の数は変えさせない。

    Alt+ドラッグで矩形選択ができる(複数行の同じ桁をまとめて直す用)。
    矩形が生きている間の入力・BS・Del・コピーは、全行に同じ操作をする。"""

    # ガター(行番号とアイコンの欄)の寸法。[左余白][番号][間][アイコン][右余白]
    _GUTTER_LEFT = 6
    _GUTTER_MID = 4
    _GUTTER_ICON = 16
    _GUTTER_RIGHT = 8
    BLOCK_COLOR = "#b8d8f8"   # 矩形選択の地

    def __init__(self, window):
        super().__init__(window)
        self.window_ = window
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.gutter = _Gutter(self)
        self.blockCountChanged.connect(lambda _n: self._update_margin())
        self.updateRequest.connect(self._update_gutter)
        self._update_margin()
        # 矩形選択: [開始行, 開始桁, 現在行, 現在桁](桁は行内の文字位置)。
        self._block = None
        self._block_drag = False
        self._row_selections = []   # 窓側の行ハイライト(編集済み・問題あり)

    def gutter_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return (self._GUTTER_LEFT
                + QFontMetrics(self.font()).horizontalAdvance("9" * digits)
                + self._GUTTER_MID + self._GUTTER_ICON + self._GUTTER_RIGHT)

    def _update_margin(self) -> None:
        width = self.gutter_width()
        if self.viewportMargins().left() != width:
            self.setViewportMargins(width, 0, 0, 0)

    def changeEvent(self, event):
        # 字が差し替わると番号の幅が変わる。余白を取り直さないと、ガターの
        # 絵(こちらは resizeEvent で毎回測る)に文字の左端が隠れる。
        # render() は blockSignals で blockCountChanged を止めるので、
        # 行数の変化でも取り直せないことがある(左端が欠ける実例)。
        if event.type() == QEvent.FontChange:
            self._update_margin()
        super().changeEvent(event)

    def _update_gutter(self, rect, dy) -> None:
        # 描画のたびに余白も点検する。どの経路で幅が変わっても、描かれる前に
        # 必ず追いつくための最後の受け皿(値が同じなら何もしない)。
        self._update_margin()
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_margin()
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event) -> None:
        painter = QPainter(self.gutter)
        try:
            painter.fillRect(event.rect(), QColor("#f0f0f0"))
            block = self.firstVisibleBlock()
            number = block.blockNumber()
            top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
            height = round(self.blockBoundingRect(block).height())
            digits_w = (self.gutter_width() - self._GUTTER_MID
                        - self._GUTTER_ICON - self._GUTTER_RIGHT)
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible():
                    painter.setPen(QColor("#909090"))
                    painter.drawText(QRect(0, top, digits_w, height), Qt.AlignRight | Qt.AlignVCenter,
                                     str(number + 1))
                    icon = self.window_.icon_for_row(number)
                    if icon is not None:
                        icon.paint(painter, QRect(digits_w + self._GUTTER_MID,
                                                  top + (height - self._GUTTER_ICON) // 2,
                                                  self._GUTTER_ICON, self._GUTTER_ICON))
                block = block.next()
                top += height
                number += 1
        finally:
            painter.end()

    # --- 矩形選択 ---
    def setExtraSelections(self, selections) -> None:
        # 窓側は行ハイライトだけを渡す。矩形のハイライトはこちらで重ねる。
        self._row_selections = list(selections)
        self._apply_selections()

    def _apply_selections(self) -> None:
        super().setExtraSelections(self._row_selections + self._block_selections())

    def _block_range(self):
        """正規化した (先頭行, 末行, 左桁, 右桁)。矩形が無ければ None。"""
        if self._block is None:
            return None
        ab, ac, cb, cc = self._block
        return min(ab, cb), max(ab, cb), min(ac, cc), max(ac, cc)

    def _block_selections(self) -> list:
        rng = self._block_range()
        if rng is None:
            return []
        first, last, c0, c1 = rng
        selections = []
        doc = self.document()
        for row in range(first, last + 1):
            block = doc.findBlockByNumber(row)
            if not block.isValid():
                continue
            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + min(c0, block.length() - 1))
            cursor.setPosition(block.position() + min(c1, block.length() - 1),
                               QTextCursor.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(self.BLOCK_COLOR))
            sel.format = fmt
            sel.cursor = cursor
            selections.append(sel)
        return selections

    def block_rows(self):
        """矩形が選んでいる行の一覧。無ければ None(窓の _selected_rows 用)。"""
        rng = self._block_range()
        if rng is None:
            return None
        return list(range(rng[0], rng[1] + 1))

    def set_block_selection(self, anchor, current) -> None:
        """(行, 桁) の組で矩形を作る。マウス操作とテストの入口。"""
        self._block = [anchor[0], anchor[1], current[0], current[1]]
        self._apply_selections()

    def clear_block_selection(self) -> None:
        if self._block is not None:
            self._block = None
            self._apply_selections()

    def block_text(self) -> str:
        """矩形の中身(行ごとの切り取りを改行でつないだもの)。"""
        rng = self._block_range()
        if rng is None:
            return ""
        first, last, c0, c1 = rng
        doc = self.document()
        parts = []
        for row in range(first, last + 1):
            block = doc.findBlockByNumber(row)
            parts.append(block.text()[c0:c1] if block.isValid() else "")
        return "\n".join(parts)

    def _block_edit_lines(self, make) -> None:
        """矩形の各行を make(行文字列, 左桁, 右桁) -> 新しい行文字列 で差し替える。
        全行分を1回の取り消しで戻せるようにする。"""
        rng = self._block_range()
        if rng is None:
            return
        first, last, c0, c1 = rng
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for row in range(first, last + 1):
                block = doc.findBlockByNumber(row)
                if not block.isValid():
                    continue
                c = QTextCursor(block)
                c.select(QTextCursor.LineUnderCursor)
                c.insertText(make(block.text(), c0, c1))
        finally:
            cursor.endEditBlock()

    def block_insert_text(self, text: str) -> None:
        """矩形の範囲を text で置き換える(短い行は左桁まで空白で埋める)。
        置き換えた後は、text の直後に幅0の矩形を残す(続けて打てる)。"""
        if self._block_range() is None:
            return
        self._block_edit_lines(lambda line, c0, c1: line[:c0].ljust(c0) + text + line[c1:])
        _first, _last, c0, _c1 = self._block_range()
        self._block[1] = self._block[3] = c0 + len(text)
        self._apply_selections()

    def block_delete(self, backward: bool) -> None:
        """矩形の範囲を消す。幅0なら BS/Del として桁の前後1文字を全行で消す
        (行頭・行末を越えて行をつなぐことはしない)。"""
        rng = self._block_range()
        if rng is None:
            return
        _first, _last, c0, c1 = rng
        if c0 != c1:
            self._block_edit_lines(lambda line, a, b: line[:a] + line[b:])
            self._block[1] = self._block[3] = c0
        elif backward and c0 > 0:
            self._block_edit_lines(
                lambda line, a, b: line[:a - 1] + line[a:] if len(line) >= a else line)
            self._block[1] = self._block[3] = c0 - 1
        elif not backward:
            self._block_edit_lines(
                lambda line, a, b: line[:a] + line[a + 1:] if len(line) > a else line)
        self._apply_selections()

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton
                and event.modifiers() & Qt.AltModifier):
            cursor = self.cursorForPosition(event.pos())
            anchor = (cursor.blockNumber(), cursor.positionInBlock())
            self._block = [anchor[0], anchor[1], anchor[0], anchor[1]]
            self._block_drag = True
            plain = QTextCursor(cursor)
            plain.clearSelection()
            self.setTextCursor(plain)
            self._apply_selections()
            event.accept()
            return
        if self._block is not None and not (event.modifiers() & Qt.AltModifier):
            self.clear_block_selection()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._block_drag:
            cursor = self.cursorForPosition(event.pos())
            self._block[2] = cursor.blockNumber()
            self._block[3] = cursor.positionInBlock()
            self._apply_selections()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._block_drag:
            self._block_drag = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _block_key(self, event) -> bool:
        """矩形が生きている間のキー。処理したら True。

        英数の直接入力は全行に行き渡る(IME の入力は通常のカーソル位置に
        入るので、矩形の対象外)。矢印キーなどは矩形を解いて通常の動きへ。"""
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
                    self._block_paste(text.replace("\r\n", "\n").replace("\r", "\n"))
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

    def _block_paste(self, text: str) -> None:
        """矩形への貼り付け。1行なら全行へ、複数行なら行ごとに配る。"""
        rng = self._block_range()
        if rng is None:
            return
        parts = text.split("\n")
        if len(parts) <= 1:
            self.block_insert_text(text)
            return
        first, last, c0, c1 = rng
        doc = self.document()
        cursor = self.textCursor()
        cursor.beginEditBlock()
        try:
            for i, row in enumerate(range(first, last + 1)):
                block = doc.findBlockByNumber(row)
                if not block.isValid():
                    continue
                piece = parts[i] if i < len(parts) else ""
                c = QTextCursor(block)
                c.select(QTextCursor.LineUnderCursor)
                line = block.text()
                c.insertText(line[:c0].ljust(c0) + piece + line[c1:])
        finally:
            cursor.endEditBlock()
        self.clear_block_selection()

    def keyPressEvent(self, event):
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if mods & Qt.ControlModifier:
                self.window_.execute()
            return          # 改行は入れない(行の数を変えない)
        if self._block is not None and self._block_key(event):
            return
        cursor = self.textCursor()
        if not cursor.hasSelection():
            if key == Qt.Key_Backspace and cursor.positionInBlock() == 0:
                return      # 行頭の BS で前の行とつなげない
            if key == Qt.Key_Delete and cursor.atBlockEnd():
                return      # 行末の Del で次の行とつなげない
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        """改行を含む貼り付けは、いまの行から下へ1行ずつ差し替える(Excel などの名前の
        一覧を貼るとき)。行は増やさない(あふれた分は捨てる)。"""
        text = source.text() if source.hasText() else ""
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if len(lines) <= 1 or self.textCursor().hasSelection():
            super().insertFromMimeData(source)
            return
        if lines and lines[-1] == "":
            lines.pop()
        cursor = self.textCursor()
        start = cursor.blockNumber()
        cursor.beginEditBlock()
        try:
            for offset, line in enumerate(lines):
                block = self.document().findBlockByNumber(start + offset)
                if not block.isValid():
                    break
                c = QTextCursor(block)
                c.select(QTextCursor.LineUnderCursor)
                c.insertText(line)
        finally:
            cursor.endEditBlock()


class RenemoWindow(QDialog):
    """items は [{"folder", "name", "is_dir", "size", "mtime"}]。

    on_execute(pairs) は [(元のフルパス, 新しいフルパス)] を受け取り、失敗の理由か None を
    返す(None なら閉じる)。on_undo(履歴の1件) も同じ。history は履歴の並び。
    existing_by_folder は検査に使う {フォルダ: その中の名前}。"""

    def __init__(self, parent, items, existing_by_folder, history=None,
                 on_execute=None, on_undo=None, font=None):
        super().__init__(parent)
        self.setWindowTitle("一括改名（Renemo風）")
        self.setObjectName("renemo")
        # Renemo と同じ白地の配色(この窓だけ。エディタとして読みやすさを優先)。
        self.setStyleSheet(
            "#renemo { background-color: #f0f0f0; }"
            " #renemo QPlainTextEdit { background-color: #ffffff; color: #000000;"
            " border: 1px solid #c0c0c0; }"
            " #renemo QLabel { color: #000000; }")
        self.items = [dict(item) for item in items]
        provider = QFileIconProvider()
        for item in self.items:
            info = QFileInfo(os.path.join(item["folder"], item["name"]))
            item["icon"] = provider.icon(info)
            # 種類はエクスプローラと同じ名前(テキスト ドキュメント など)。取れなければ
            # Qt の種類、それも無ければ拡張子。
            item["type"] = (filer_shell.file_type_name(item["name"], item["is_dir"])
                            or provider.type(info)
                            or ("フォルダー" if item["is_dir"]
                                else os.path.splitext(item["name"])[1].lstrip(".")))
            stem, ext = fr.split_ext(item["name"], item["is_dir"])
            item["stem"], item["ext"] = stem, ext
        self.existing_by_folder = existing_by_folder
        self.history = list(history or [])
        self.on_execute = on_execute
        self.on_undo = on_undo
        self.order = list(range(len(self.items)))
        self.stems = [item["stem"] for item in self.items]
        self.exts = [item["ext"] for item in self.items]
        self.show_ext = False
        self.full_path = False
        self.problems = {}
        self.message = ""
        self._syncing = False

        font = QFont(font) if font is not None else QFont()
        font.setFamilies(["MS Gothic", "Consolas"])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._build_actions()
        menubar = QMenuBar(self)
        edit_menu = menubar.addMenu("編集(&E)")
        for action in (self.act_upper, self.act_lower, self.act_half, self.act_full):
            edit_menu.addAction(action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_replace)
        edit_menu.addAction(self.act_number)
        edit_menu.addAction(self.act_mask)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_undo)
        view_menu = menubar.addMenu("表示(&V)")
        view_menu.addAction(self.act_ext)
        view_menu.addAction(self.act_fullpath)
        sort_menu = view_menu.addMenu("並べ替え(&S)")
        for kind, label in SORT_KINDS:
            sort_menu.addAction(label, lambda k=kind: self.sort_rows(k))
        run_menu = menubar.addMenu("実行(&X)")
        run_menu.addAction(self.act_execute)
        run_menu.addAction(self.act_history)
        layout.setMenuBar(menubar)

        toolbar = QToolBar(self)
        for action in (self.act_execute, self.act_ext, self.act_fullpath, self.act_replace,
                       self.act_number, self.act_mask, self.act_history):
            toolbar.addAction(action)
        layout.addWidget(toolbar)

        headers = QHBoxLayout()
        headers.setContentsMargins(4, 2, 4, 2)
        for text, stretch in (("名前", 3), ("編集名", 4), ("サイズ / 更新日時 / 種類", 4)):
            label = QLabel(text)
            headers.addWidget(label, stretch)
        layout.addLayout(headers)

        body = QHBoxLayout()
        body.setSpacing(2)
        self.names_view = self._side_view(font)
        self.editor = RenameEdit(self)
        self.editor.setFont(font)
        self.info_view = self._side_view(font)
        body.addWidget(self.names_view, 3)
        body.addWidget(self.editor, 4)
        body.addWidget(self.info_view, 4)
        layout.addLayout(body, 1)

        status = QHBoxLayout()
        status.setContentsMargins(4, 1, 4, 1)
        self.status_path = QLabel()
        self.status_path.setTextFormat(Qt.PlainText)
        self.status_size = QLabel()
        self.status_message = QLabel()
        self.status_message.setStyleSheet("color: #c00000;")
        status.addWidget(self.status_path, 1)
        status.addWidget(self.status_message)
        status.addWidget(self.status_size)
        layout.addLayout(status)

        # 3つの列のスクロールをそろえる(どれを動かしても、同じ行が横に並ぶ)。
        bars = [v.verticalScrollBar() for v in (self.names_view, self.editor, self.info_view)]
        for bar in bars:
            bar.valueChanged.connect(lambda value, src=bar: self._sync_scroll(src, value, bars))
        self.editor.textChanged.connect(self._on_text_changed)
        self.editor.cursorPositionChanged.connect(self._update_status)
        self.editor.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(self._context_menu)

        self.render()
        self.resize(1100, 600)
        self.editor.setFocus()

    # --- 部品 ---
    def _side_view(self, font):
        view = QPlainTextEdit(self)
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.NoWrap)
        view.setFocusPolicy(Qt.NoFocus)
        view.setFont(font)
        view.setStyleSheet("background-color: #f7f7f7;")
        return view

    def _build_actions(self) -> None:
        def act(text, slot, shortcut=None, checkable=False):
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.setCheckable(checkable)
            action.triggered.connect(slot)
            self.addAction(action)
            return action
        self.act_execute = act("実行", self.execute, "Ctrl+Return")
        self.act_ext = act(".ext", self.toggle_ext, checkable=True)
        self.act_fullpath = act("C:¥", self.toggle_fullpath, checkable=True)
        self.act_upper = act("大文字(&U)", lambda: self.convert_selection("upper"), "Ctrl+Shift+U")
        self.act_lower = act("小文字(&L)", lambda: self.convert_selection("lower"), "Ctrl+U")
        self.act_half = act("全角→半角(&H)", lambda: self.convert_selection("half"))
        self.act_full = act("半角→全角(&F)", lambda: self.convert_selection("full"))
        self.act_replace = act("置換(&R)…", self.replace_dialog, "Ctrl+H")
        self.act_number = act("連番(&N)…", self.number_dialog)
        self.act_mask = act("マスク(&M)…", self.mask_dialog)
        self.act_undo = act("元に戻す(&Z)", lambda: self.editor.undo())
        self.act_history = act("履歴(&I)…", self.history_dialog)

    def icon_for_row(self, row: int):
        if 0 <= row < len(self.order):
            return self.items[self.order[row]]["icon"]
        return None

    def _sync_scroll(self, source, value, bars) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for bar in bars:
                if bar is not source:
                    bar.setValue(value)
        finally:
            self._syncing = False

    # --- 表示 ---
    def _line_for(self, index: int) -> str:
        return self.stems[index] + (self.exts[index] if self.show_ext else "")

    def render(self) -> None:
        """いまの並びとトグルで3つの列を書き直す(編集名の取り消しの記録は消える)。"""
        names, infos = [], []
        for index in self.order:
            item = self.items[index]
            names.append(os.path.join(item["folder"], item["name"]) if self.full_path
                         else item["name"])
            size = "" if item["is_dir"] else f"{item['size']:,}"
            stamp = ""
            if item["mtime"]:
                t = time.localtime(item["mtime"])
                # Renemo と同じ書式(時は0で埋めない: 2024/02/16 8:20:01)。
                stamp = f"{t.tm_year}/{t.tm_mon:02}/{t.tm_mday:02} {t.tm_hour}:{t.tm_min:02}:{t.tm_sec:02}"
            infos.append(f"{size:>13}  {stamp}  {item['type']}")
        self.names_view.setPlainText("\n".join(names))
        self.info_view.setPlainText("\n".join(infos))
        self.editor.clear_block_selection()
        self.editor.blockSignals(True)
        self.editor.setPlainText("\n".join(self._line_for(i) for i in self.order))
        self.editor.blockSignals(False)
        self.editor.document().clearUndoRedoStacks()
        self._refresh_marks()
        self._update_status()

    def editor_lines(self) -> list:
        return self.editor.toPlainText().split("\n")

    def sync_from_editor(self) -> bool:
        """編集名の欄の内容を、名前の部分と拡張子の部分へ書き戻す。行数が合わなければ False。"""
        lines = self.editor_lines()
        if len(lines) != len(self.order):
            return False
        for row, index in enumerate(self.order):
            line = lines[row]
            if self.show_ext:
                stem, ext = fr.split_ext(line, self.items[index]["is_dir"])
                self.stems[index], self.exts[index] = stem, ext
            else:
                self.stems[index] = line
        return True

    def new_names(self) -> list:
        """元の並び(items の並び)での新しい名前。"""
        return [self.stems[i] + self.exts[i] for i in range(len(self.items))]

    def is_dirty(self) -> bool:
        self.sync_from_editor()
        return any(new != item["name"] for new, item in zip(self.new_names(), self.items))

    def _on_text_changed(self) -> None:
        self.problems = {}
        self.message = ""
        self._refresh_marks()

    def _refresh_marks(self) -> None:
        """編集した行(黄)と問題のある行(赤)に色を付ける。"""
        lines = self.editor_lines()
        selections = []
        for row, index in enumerate(self.order):
            original = self.items[index]["stem"] + (self.items[index]["ext"] if self.show_ext else "")
            color = None
            if index in self.problems:
                color = ERROR_COLOR
            elif row < len(lines) and (lines[row] != original or
                                       (not self.show_ext and self.exts[index] != self.items[index]["ext"])):
                color = EDITED_COLOR
            if color is None:
                continue
            block = self.editor.document().findBlockByNumber(row)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(color))
            fmt.setProperty(QTextFormat.FullWidthSelection, True)
            sel.format = fmt
            sel.cursor = QTextCursor(block)
            selections.append(sel)
        self.editor.setExtraSelections(selections)
        self.status_message.setText(self.message)

    def _update_status(self) -> None:
        row = self.editor.textCursor().blockNumber()
        if 0 <= row < len(self.order):
            item = self.items[self.order[row]]
            self.status_path.setText(os.path.join(item["folder"], item["name"]))
            self.status_size.setText("" if item["is_dir"] else f"{item['size'] / 1024:,.2f} KB")
            problem = self.problems.get(self.order[row])
            if problem:
                self.status_message.setText(problem)
            else:
                self.status_message.setText(self.message)

    # --- トグル・並べ替え ---
    def toggle_ext(self) -> None:
        self.sync_from_editor()
        self.show_ext = self.act_ext.isChecked()
        self.render()

    def toggle_fullpath(self) -> None:
        self.sync_from_editor()
        self.full_path = self.act_fullpath.isChecked()
        self.render()

    def sort_rows(self, kind: str) -> None:
        """並べ替えても、元の名前との対応は崩さない(行ごとの組のまま並べ直す)。"""
        if not self.sync_from_editor():
            return
        if kind == "name":
            self.order.sort(key=lambda i: self.items[i]["name"].casefold())
        elif kind == "original":
            self.order.sort()
        elif kind == "edited":
            news = self.new_names()
            # 並べ替えの最中は並び自身を引けない(空に見える)ので、今の位置を先に控える。
            position = {index: row for row, index in enumerate(self.order)}
            self.order.sort(key=lambda i: (news[i] == self.items[i]["name"], position[i]))
        self.render()

    # --- 変換・置換・連番(取り消しできるよう、文字の差し替えで行う) ---
    def _selected_rows(self):
        block_rows = self.editor.block_rows()
        if block_rows is not None:
            return block_rows   # 矩形選択があれば、その行が対象
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return None
        start = self.editor.document().findBlock(cursor.selectionStart()).blockNumber()
        end = self.editor.document().findBlock(cursor.selectionEnd()).blockNumber()
        return list(range(start, end + 1))

    def _replace_lines(self, new_lines) -> None:
        """編集名の欄を new_lines に差し替える(1回の取り消しで戻せる)。"""
        self.editor.clear_block_selection()   # 中身が変わるので矩形は座標が合わなくなる
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        cursor.select(QTextCursor.Document)
        cursor.insertText("\n".join(new_lines))
        cursor.endEditBlock()

    def convert_selection(self, mode: str) -> None:
        """選んだ文字(無ければ全行)を変換する。"""
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText()
            cursor.insertText(fr.convert(text, mode).replace(" ", "\n"))
            return
        self._replace_lines([fr.convert(line, mode) for line in self.editor_lines()])

    def ask_replace(self):
        """置換の条件。{"find", "repl", "regex", "case", "keep_ext"} か None。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("置換")
        form = QFormLayout(dialog)
        find, repl = QLineEdit(), QLineEdit()
        regex, case, keep = QCheckBox("正規表現"), QCheckBox("大文字小文字を区別"), \
            QCheckBox("拡張子を除く")
        keep.setChecked(True)
        form.addRow("検索", find)
        form.addRow("置換", repl)
        for box in (regex, case, keep):
            form.addRow(box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return {"find": find.text(), "repl": repl.text(), "regex": regex.isChecked(),
                "case": case.isChecked(), "keep_ext": keep.isChecked()}

    def replace_dialog(self) -> None:
        options = self.ask_replace()
        if not options or not options["find"]:
            return
        rows = self._selected_rows()
        lines = self.editor_lines()
        try:
            for row in (rows if rows is not None else range(len(lines))):
                if row >= len(self.order):
                    continue
                is_dir = self.items[self.order[row]]["is_dir"]
                lines[row] = fr.replace_name(lines[row], options["find"], options["repl"],
                                             options["regex"], options["case"],
                                             options["keep_ext"] and self.show_ext, is_dir)
        except ValueError as e:
            self.message = str(e)
            self._refresh_marks()
            return
        self._replace_lines(lines)

    def ask_numbering(self):
        """連番の条件。{"fmt", "start", "step", "digits"} か None。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("連番")
        form = QFormLayout(dialog)
        fmt = QLineEdit("{n}")
        start, step, digits = QSpinBox(), QSpinBox(), QSpinBox()
        start.setRange(-999999, 999999)
        start.setValue(1)
        step.setRange(-1000, 1000)
        step.setValue(1)
        digits.setRange(1, 10)
        digits.setValue(3)
        form.addRow("書式（{n} が番号。例 photo_{n:03}）", fmt)
        form.addRow("開始番号", start)
        form.addRow("増分", step)
        form.addRow("桁数（{n} のとき）", digits)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return {"fmt": fmt.text(), "start": start.value(), "step": step.value(),
                "digits": digits.value()}

    def ask_mask(self):
        """マスクの条件。{"mask", "start", "step"} か None。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("マスク")
        form = QFormLayout(dialog)
        mask = QLineEdit("{name}_{n:03}")
        start, step = QSpinBox(), QSpinBox()
        start.setRange(-999999, 999999)
        start.setValue(1)
        step.setRange(-1000, 1000)
        step.setValue(1)
        form.addRow("マスク（{name} 元の名前 / {ext} 拡張子 / {n} 連番 / "
                    "{date} 更新日 / {time} 時刻）", mask)
        form.addRow("開始番号", start)
        form.addRow("増分", step)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        return {"mask": mask.text(), "start": start.value(), "step": step.value()}

    def mask_dialog(self) -> None:
        """選んだ行(無ければ全行)をマスクで組み立て直す。マスクに {ext} が
        無ければ元の拡張子を残す(計算は filer_rename.mask_names)。"""
        options = self.ask_mask()
        if not options or not options["mask"].strip():
            return
        if not self.show_ext and "{ext}" in options["mask"]:
            # 拡張子を隠しているときは、編集欄の行が名前の部分だけになる。
            # ここで {ext} を展開すると拡張子が二重になるので、先に断る
            # (置換の keep_ext が show_ext と連動するのと同じ理由)。
            self.message = "{ext} を使うときは、拡張子を表示してください（.ext ボタン）"
            self._refresh_marks()
            return
        rows = self._selected_rows()
        lines = self.editor_lines()
        rows = [r for r in (rows if rows is not None else range(len(lines)))
                if r < len(self.order)]
        dir_flags = [self.items[self.order[r]]["is_dir"] for r in rows]
        mtimes = [self.items[self.order[r]]["mtime"] for r in rows]
        try:
            named = fr.mask_names([lines[r] for r in rows], options["mask"],
                                  options["start"], options["step"], 1,
                                  mtimes=mtimes, dir_flags=dir_flags)
        except ValueError as e:
            self.message = str(e)
            self._refresh_marks()
            return
        for r, name in zip(rows, named):
            lines[r] = name
        self._replace_lines(lines)

    def number_dialog(self) -> None:
        """選んだ行(無ければ全行)に連番。拡張子は残す。"""
        options = self.ask_numbering()
        if not options:
            return
        rows = self._selected_rows()
        lines = self.editor_lines()
        rows = [r for r in (rows if rows is not None else range(len(lines))) if r < len(self.order)]
        dir_flags = [self.items[self.order[r]]["is_dir"] for r in rows]
        named = fr.numbered_names([lines[r] for r in rows], options["fmt"], options["start"],
                                  options["step"], options["digits"], keep_ext=self.show_ext,
                                  dir_flags=dir_flags)
        for r, name in zip(rows, named):
            lines[r] = name
        self._replace_lines(lines)

    def _context_menu(self, pos) -> None:
        menu = QMenu(self)
        for action in (self.act_upper, self.act_lower, self.act_half, self.act_full,
                       self.act_replace, self.act_number, self.act_mask, self.act_undo):
            menu.addAction(action)
        menu.exec(self.editor.mapToGlobal(pos))

    # --- 実行 ---
    def build_pairs(self):
        """(組, 誤り)。組は [(元のフルパス, 新しいフルパス)](元の並び)。"""
        if not self.sync_from_editor():
            return None, "行数が合いません（行の追加・削除はできません。元に戻す: Ctrl+Z）"
        news = self.new_names()
        items = [(item["folder"], item["name"]) for item in self.items]
        self.problems = fr.validate_batch(items, news, self.existing_by_folder)
        if self.problems:
            return None, f"問題のある行が {len(self.problems)} 件あります（赤の行）"
        pairs = [(os.path.join(item["folder"], item["name"]), os.path.join(item["folder"], new))
                 for item, new in zip(self.items, news)]
        return pairs, ""

    def execute(self) -> None:
        try:
            pairs, error = self.build_pairs()
            if error:
                self.message = error
                self._refresh_marks()
                return
            if not any(src != dst for src, dst in pairs):
                self.message = "変更がありません"
                self._refresh_marks()
                return
            if self.on_execute is not None:
                error = self.on_execute(pairs)
                if error:
                    self.message = error
                    self._refresh_marks()
                    return
            self.accept()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            self.message = f"{type(e).__name__}: {e}"
            self._refresh_marks()

    # --- 履歴 ---
    def choose_history(self):
        """元に戻す記録を選ぶ。履歴の番号か None。"""
        if not self.history:
            return None
        labels = [fr.describe_history(e) for e in self.history]
        label, ok = QInputDialog.getItem(self, "履歴（元に戻す）", "元に戻す一括改名", labels, 0, False)
        return labels.index(label) if ok else None

    def history_dialog(self) -> None:
        if not self.history:
            self.message = "履歴がありません"
            self._refresh_marks()
            return
        index = self.choose_history()
        if index is None or self.on_undo is None:
            return
        error = self.on_undo(self.history[index])
        if error:
            self.message = error
            self._refresh_marks()
            return
        self.accept()

    # --- 閉じる ---
    def confirm(self, text: str) -> bool:
        return QMessageBox.question(self, "一括改名", text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def keyPressEvent(self, event):
        # 編集欄は Esc を使わないので、ここ(窓)まで上がってくる。
        if event.key() == Qt.Key_Escape:
            try:
                if self.is_dirty() and not self.confirm("編集した内容を捨てて閉じますか？"):
                    return
            except Exception:  # noqa: BLE001
                pass
            self.reject()
            return
        super().keyPressEvent(event)
