# -*- coding: utf-8 -*-
"""AI対話モードの「下書き欄」（1つの編集可能な欄）。PySide6。

欄の中身は、前から順に
  1. 確定済み（白）: 清書（small など）の結果、またはユーザーが直した文字。キーボードで自由に直せる。
  2. 区切れたが、まだ清書が終わっていない発話（発話 ID＝区切りの開始秒の順）: 1発話につき1ブロック。
       バー（認識中。棒グラフの画像＋「認識中…」。長さ＝喋った秒数に比例し、1行 WAVE_ROW_SEC 秒で折り返す）… 読み取り専用。
       清書が済むと、その場で白い文字に置き換わる。
  3. いま喋っている途中の発話（末尾に1つ）: 同じ形のバーが、喋っている長さに比例して右へ（行末で折り返して次の行へ）伸び、
       右に「喋っています 0:12」と出る。区切れて認識に回ると、そのまま2のバーになる。無音のあいだは伸ばさない。
仮認識（黄色い暫定の文字）は廃止した。

ブロックの種類・発話 ID はブロックの書式の属性（PROP_KIND / PROP_UID / PROP_ENV / PROP_LABEL）に持つ。確定済みのブロックは属性なし。
不変条件: 「確定済みブロックが前、認識中のブロックが後ろ」（確定済みは先頭から連続）。これを守るため、後ろ側の編集は
  - 認識中のブロックには一切入力させない（文字・削除・貼り付け・IME）
  - 確定済みと後ろ側にまたがる削除・結合（境目での Backspace/Delete、またがる選択）は無視する
ドラッグ＆ドロップは受け付けない。元に戻す（Ctrl+Z）は、認識結果の差し込みと混ざって構造が壊れるため使えない。
欄の文字は選択して Ctrl+C／右クリック→コピーできる（このツールが自分でクリップボードへ入れることはしない）。
"""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QImage, QKeySequence, QPainter, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextImageFormat)
from PySide6.QtWidgets import QMenu, QSizePolicy, QTextEdit, QWidget

import ui_dialogue_logic as L
import ui_style as S

PROP_KIND = 0x100001
PROP_UID = 0x100002
PROP_ENV = 0x100003
PROP_LABEL = 0x100004

WAVE_H = 22
BAR_W, BAR_GAP = 3, 2
LABEL_WAIT = "  認識中…"


def _fmt_final() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(S.TEXT))
    f.setFontItalic(False)
    return f


def _fmt_label() -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(S.MUTED))
    f.setFontItalic(False)
    f.setFontPointSize(max(7, S.BASE_PT - 2))
    return f


def _block_fmt(kind: str = "", uid: float | None = None, env: tuple = (), label: str = "") -> QTextBlockFormat:
    b = QTextBlockFormat()
    if kind:
        b.setProperty(PROP_KIND, kind)
        b.setProperty(PROP_UID, float(uid if uid is not None else 0.0))
        if kind in L.TAIL_KINDS:
            b.setProperty(PROP_ENV, ",".join(str(int(v)) for v in env))
            b.setProperty(PROP_LABEL, str(label or ""))
            b.setTopMargin(3)
            b.setBottomMargin(3)
    return b


def wave_geometry(n: int) -> tuple[int, int]:
    """棒が n 本のバーの (列数, 行数)。1行 L.WAVE_ROW_BARS 本（＝WAVE_ROW_SEC 秒）で折り返す。"""
    n = max(1, int(n))
    cols = min(n, L.WAVE_ROW_BARS)
    return cols, -(-n // L.WAVE_ROW_BARS)


def wave_image(env: tuple, color: str | None = None) -> QImage:
    """波形のバー（棒グラフ）。長さ＝棒の本数（喋った秒数に比例）。1行 WAVE_ROW_SEC 秒ぶんで折り返して次の行へ続ける。
    背景は透明、色は青緑。"""
    n = max(1, len(env))
    cols, rows = wave_geometry(n)
    img = QImage(cols * (BAR_W + BAR_GAP), rows * WAVE_H, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    col = QColor(color or S.TEAL)
    col.setAlphaF(0.9)
    p.setBrush(col)
    for k, v in enumerate(env or (0,)):
        r, c = divmod(k, L.WAVE_ROW_BARS)
        h = max(2, int(round(WAVE_H * min(8, max(0, int(v))) / 8.0)))
        p.drawRoundedRect(QRectF(c * (BAR_W + BAR_GAP), r * WAVE_H + (WAVE_H - h) / 2.0, BAR_W, h), 1.5, 1.5)
    p.end()
    return img


class FixedBlock(QTextEdit):
    """下書き欄の上に積む、編集できない灰色寄りの固定ブロック（送信中の文章・挿入した前置き）。
    文字は選択して Ctrl+C／右クリック→コピーできる（このツールが自分でクリップボードへ入れることはしない）。
    高さは中身に合わせ、max_lines 行ぶんを超えたら内側でスクロールする。"""

    def __init__(self, max_lines: int = 4, parent=None):
        super().__init__(parent)
        self._max_lines = max(1, int(max_lines))
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setFrameShape(QTextEdit.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.setFocusPolicy(Qt.ClickFocus)
        self.document().setDocumentMargin(4)
        self.setStyleSheet(f"QTextEdit {{ background: transparent; color: {S.MUTED}; }}")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._fixed_h = 0
        self._fit()

    def set_block_text(self, text: str) -> None:
        if self.toPlainText() != text:
            self.setPlainText(text)
        self._fit()

    def _fit(self) -> None:
        doc = self.document()
        w = self.viewport().width()
        if w > 20:
            doc.setTextWidth(w)
        lines_h = int(self.fontMetrics().lineSpacing() * self._max_lines + 2 * doc.documentMargin())
        h = max(int(self.fontMetrics().lineSpacing() + 2 * doc.documentMargin()), min(lines_h, int(doc.size().height())))
        if h != self._fixed_h:
            self._fixed_h = h
            self.setFixedHeight(h)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._fit()


def _kind(block) -> str:
    v = block.blockFormat().property(PROP_KIND)
    return v if isinstance(v, str) else ""


def _uid(block) -> float | None:
    v = block.blockFormat().property(PROP_UID)
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _label(block) -> str:
    v = block.blockFormat().property(PROP_LABEL)
    return v if isinstance(v, str) else ""


def _env(block) -> tuple:
    v = block.blockFormat().property(PROP_ENV)
    if not isinstance(v, str) or not v:
        return ()
    try:
        return tuple(int(x) for x in v.split(","))
    except Exception:
        return ()


class DraftView(QTextEdit):
    """下書き欄。確定済み（白）＋認識中の発話ごとの波形。Ctrl+Enter で確定（この欄にフォーカスがあるときだけ）。"""
    commitRequested = Signal()
    aboutToEdit = Signal()         # 直す操作の直前（画面がバッファへ追いついてから、構造を判定するため）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.document().setUndoRedoEnabled(False)
        self.setTabChangesFocus(True)
        self.setAcceptDrops(False)
        self.setLineWrapMode(QTextEdit.WidgetWidth)
        self.document().setDocumentMargin(14)
        self.setCurrentCharFormat(_fmt_final())
        self.programmatic = False                    # コードから書き換え中（textChanged を無視する）
        self.extra_actions: list[QAction] = []       # 右クリックメニューの末尾へ足す項目
        self._img_names: set[str] = set()
        self._fixing_cursor = False
        self.cursorPositionChanged.connect(self._on_cursor_moved)

    # ------------------------------------------------------------------ 構造
    def _blocks(self) -> list:
        out, b = [], self.document().begin()
        while b.isValid():
            out.append(b)
            b = b.next()
        return out

    def _split(self) -> tuple[list, list]:
        """(確定済みブロック, 後ろ側ブロック)。後ろ側は最初の認識中ブロックから末尾まで。"""
        bl = self._blocks()
        nf = len(bl)
        for i, b in enumerate(bl):
            if _kind(b) in L.TAIL_KINDS:
                nf = i
                break
        return bl[:nf], bl[nf:]

    def final_text(self) -> str:
        """確定済み（白）の全文。ブロックの間は改行。"""
        fin, _ = self._split()
        return "\n".join(b.text() for b in fin).replace(" ", "\n")    # 行区切り（U+2028）も改行へ

    def final_end(self) -> int:
        fin, _ = self._split()
        b = fin[-1]
        return b.position() + b.length() - 1

    def tail_blocks(self) -> list[L.CurBlock]:
        _, tail = self._split()
        return [L.CurBlock(_uid(b) or 0.0, _kind(b), _env(b), _label(b)) for b in tail]

    @contextmanager
    def _prog(self):
        """コードからの書き換え。textChanged を無視させ、スクロール位置（下端にいたら下端）を保つ。"""
        sb = self.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 2
        keep = sb.value()
        was = self.programmatic
        self.programmatic = True
        try:
            yield
        finally:
            self.programmatic = was
        self._keep_out_of_wave()
        sb.setValue(sb.maximum() if at_bottom else keep)
        if at_bottom:
            QTimer.singleShot(0, lambda: sb.setValue(sb.maximum()))

    def _keep_out_of_wave(self) -> None:
        """カーソル（選択なし）が波形ブロックの中にあるときは、その手前（直前の波形でないブロックの末尾。波形だけなら確定済みの末尾）へ戻す。
        キー・クリック・コードからの書き換えのどれで入っても、波形の側にカーソルが居残らない。"""
        c = self.textCursor()
        if c.hasSelection():
            return
        b = self.document().findBlock(c.position())
        if not b.isValid() or _kind(b) not in L.TAIL_KINDS:
            return
        p = b.previous()
        while p.isValid() and _kind(p) in L.TAIL_KINDS:
            p = p.previous()
        if not p.isValid():
            return
        nc = QTextCursor(self.document())
        nc.setPosition(p.position() + p.length() - 1)
        was = self.programmatic
        self.programmatic = True
        try:
            self.setTextCursor(nc)
        finally:
            self.programmatic = was

    def _on_cursor_moved(self) -> None:
        if self._fixing_cursor:
            return
        self._fixing_cursor = True
        try:
            self._keep_out_of_wave()
        finally:
            self._fixing_cursor = False

    # ------------------------------------------------------------------ 確定済み部分の書き換え（バッファの変化に追いつく）
    def set_final_text(self, text: str) -> None:
        """確定済み部分を全部入れ替える（確定・破棄で空になった、など）。後ろ側（認識中）は残す。"""
        with self._prog():
            old_end = self.final_end()
            tc = self.textCursor()
            pos, anc = tc.position(), tc.anchor()
            c = QTextCursor(self.document())
            c.setPosition(0)
            c.setPosition(old_end, QTextCursor.KeepAnchor)
            c.insertText(text, _fmt_final())
            new_end = self.final_end()
            delta = new_end - old_end

            def adj(x: int) -> int:
                return x + delta if x > old_end else min(x, new_end)
            nc = QTextCursor(self.document())
            nc.setPosition(adj(anc))
            nc.setPosition(adj(pos), QTextCursor.KeepAnchor)
            self.setTextCursor(nc)

    def append_final(self, tail: str, cut: int = 0) -> None:
        """認識結果を確定済み部分の末尾へ足す（cut>0 なら、末尾の文字を先に cut 文字削る。「、」→「。」の置き換え用）。
        利用者が直している内容・カーソル・選択・スクロール位置は壊さない。
        カーソルが確定済み部分の末尾にあった（選択なし）ときだけ、足した分のあとへ進める。後ろ側（認識中）にあるカーソルは
        足した分だけずれる。"""
        with self._prog():
            doc = self.document()
            tc = self.textCursor()
            anchor, pos = tc.anchor(), tc.position()
            old_end = self.final_end()
            body_len = len(self.final_text().rstrip())      # 末尾の空白（利用者が打ったもの）の手前
            start = max(0, body_len - cut)
            delta = len(tail) - cut
            c = QTextCursor(doc)
            if cut:
                c.setPosition(start)
                c.setPosition(body_len, QTextCursor.KeepAnchor)
                c.removeSelectedText()
                c.insertText(tail, _fmt_final())
            else:
                c.setPosition(old_end)
                c.insertText(tail, _fmt_final())
            new_end = self.final_end()

            def adj(x: int) -> int:
                if not cut:
                    return x + delta if x > old_end else x
                if x <= start:
                    return x
                return x + delta if x >= body_len else start + len(tail)
            nc = QTextCursor(doc)
            if anchor == pos == old_end:
                nc.setPosition(new_end)
            else:
                nc.setPosition(min(adj(anchor), doc.characterCount() - 1))
                nc.setPosition(min(adj(pos), doc.characterCount() - 1), QTextCursor.KeepAnchor)
            self.setTextCursor(nc)

    # ------------------------------------------------------------------ 後ろ側（認識中）の書き換え（ブロック単位）
    def apply_tail_ops(self, ops: list[tuple]) -> None:
        """ui_dialogue_logic.plan_tail の操作を適用する。確定済みのブロックには触れない。"""
        if not ops:
            return
        # 利用者が置いたカーソル・選択は動かさない。確定済み部分（白）にあるカーソルは、後ろ側のブロックの挿入・削除・差し替えで
        # 数値の位置が変わらない（後ろ側は確定済みの末尾より後ろにある）。ただし Qt は「挿入位置にあるカーソル」を挿入のあとへ
        # 進めてしまう（白の末尾にカーソルがあると、新しい波形ブロックの中へ飛ぶ）ので、あとで元の位置へ戻す。
        tc = self.textCursor()
        a0, p0 = tc.anchor(), tc.position()
        fend = self.final_end()
        in_final = max(a0, p0) <= fend
        with self._prog():
            for op in ops:
                try:
                    if op[0] == "insert":
                        self._tail_insert(op[1], op[2])
                    elif op[0] == "remove":
                        self._tail_remove(op[1])
                    elif op[0] == "update":
                        self._tail_update(op[1], op[2])
                except Exception:
                    # 構造が想定と違っていた: 何も壊さずに次の同期へ任せる
                    continue
            if in_final:
                cur = self.textCursor()
                if (cur.anchor(), cur.position()) != (a0, p0):
                    nc = QTextCursor(self.document())
                    nc.setPosition(a0)
                    nc.setPosition(p0, QTextCursor.KeepAnchor)
                    self.setTextCursor(nc)

    def _fill(self, c: QTextCursor, item: L.TailItem) -> None:
        name = "wave:" + ",".join(str(v) for v in item.wave)
        if name not in self._img_names:
            self.document().addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), wave_image(item.wave))
            self._img_names.add(name)
        f = QTextImageFormat()
        f.setName(name)
        cols, rows = wave_geometry(len(item.wave))
        f.setWidth(cols * (BAR_W + BAR_GAP))
        f.setHeight(rows * WAVE_H)
        f.setVerticalAlignment(QTextCharFormat.AlignMiddle)
        c.insertImage(f)
        c.insertText(("  " + item.label) if item.label else (LABEL_WAIT if item.kind == L.K_WAVE else ""),
                     _fmt_label())

    def _tail_insert(self, i: int, item: L.TailItem) -> None:
        fin, tail = self._split()
        prev = (fin + tail)[len(fin) + i - 1]
        c = QTextCursor(prev)
        c.movePosition(QTextCursor.EndOfBlock)
        c.insertBlock(_block_fmt(item.kind, item.uid, item.wave, item.label), _fmt_label())
        self._fill(c, item)

    def _remove_block(self, prev, blk) -> None:
        """blk（prev の次のブロック）を取り除く。Qt はブロックの結合で、前のブロックが空だと後ろのブロックの書式が残るので、
        前のブロックの書式を結合のあとで掛け直す。"""
        pos = prev.position()
        bf, cf = prev.blockFormat(), prev.charFormat()
        c = QTextCursor(self.document())
        c.setPosition(prev.position() + prev.length() - 1)
        c.setPosition(blk.position() + blk.length() - 1, QTextCursor.KeepAnchor)
        c.removeSelectedText()
        c2 = QTextCursor(self.document())
        c2.setPosition(pos)
        c2.setBlockFormat(bf)
        if not c2.block().text():
            c2.setBlockCharFormat(cf)

    def _tail_remove(self, i: int) -> None:
        fin, tail = self._split()
        blk = tail[i]
        prev = (fin + tail)[len(fin) + i - 1]
        self._remove_block(prev, blk)

    def _tail_update(self, i: int, item: L.TailItem) -> None:
        fin, tail = self._split()
        blk = tail[i]
        keep_end = self.textCursor().position() == blk.position() + blk.length() - 1
        c = QTextCursor(blk)
        c.movePosition(QTextCursor.StartOfBlock)
        c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        c.removeSelectedText()
        c.setBlockFormat(_block_fmt(item.kind, item.uid, item.wave, item.label))
        c.setBlockCharFormat(_fmt_label())
        self._fill(c, item)
        if keep_end:
            end = QTextCursor(self.document())
            end.setPosition(c.position())
            self.setTextCursor(end)

    # ------------------------------------------------------------------ 入力の守り
    def _touched(self, kind: str) -> list:
        """この操作が触れるブロック。kind: insert / backspace / delete / cut / paste。"""
        doc = self.document()
        c = self.textCursor()
        if c.hasSelection():
            first, last = doc.findBlock(c.selectionStart()), doc.findBlock(c.selectionEnd())
        else:
            pos = c.position()
            first = last = doc.findBlock(pos)
            if kind == "backspace" and pos == first.position() and first.previous().isValid():
                first = first.previous()            # 前のブロックと結合する
            elif kind == "delete" and pos == last.position() + last.length() - 1 and last.next().isValid():
                last = last.next()                  # 次のブロックを取り込む
        out, b = [], first
        while b.isValid():
            out.append(b)
            if b.blockNumber() >= last.blockNumber():
                break
            b = b.next()
        return out

    def _decide(self, kind: str) -> bool:
        """この操作を受け付けてよいか。認識中（波形）のブロックに触れる操作は受け付けない。"""
        return all(_kind(b) not in L.TAIL_KINDS for b in self._touched(kind))

    def _guard(self, kind: str) -> bool:
        """操作を受け付けてよいか（直す操作の直前に、画面をバッファへ追いつかせてから判定する）。"""
        self.aboutToEdit.emit()
        return self._decide(kind)

    @staticmethod
    def _key_kind(ev) -> str:
        key, mods = ev.key(), ev.modifiers()
        if ev.matches(QKeySequence.Cut):
            return "cut"
        if ev.matches(QKeySequence.Paste):
            return "paste"
        if key == Qt.Key_Backspace:
            return "backspace"
        if key == Qt.Key_Delete:
            return "delete"
        if key in (Qt.Key_Return, Qt.Key_Enter):
            return "insert"
        t = ev.text()
        if t and t.isprintable() and not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
            return "insert"
        return ""

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter) and (ev.modifiers() & Qt.ControlModifier):
            ev.accept()
            self.commitRequested.emit()
            return
        if not self.isReadOnly():
            kind = self._key_kind(ev)
            if kind and not self._guard(kind):
                ev.accept()
                return
        super().keyPressEvent(ev)

    def inputMethodEvent(self, ev) -> None:
        if not self.isReadOnly() and (ev.commitString() or ev.preeditString() or ev.replacementLength()):
            if not self._guard("insert"):
                ev.ignore()
                return
        super().inputMethodEvent(ev)

    def insertFromMimeData(self, source) -> None:
        if self.isReadOnly() or not self._guard("paste"):
            return
        super().insertFromMimeData(source)

    def dropEvent(self, ev) -> None:
        ev.ignore()

    def contextMenuEvent(self, ev) -> None:
        m = QMenu(self)
        ro = self.isReadOnly()
        a_cut = m.addAction("切り取り")
        a_copy = m.addAction("コピー")
        a_paste = m.addAction("貼り付け")
        m.addSeparator()
        a_all = m.addAction("すべて選択")
        a_cut.setEnabled(not ro and self.textCursor().hasSelection())
        a_copy.setEnabled(self.textCursor().hasSelection())
        a_paste.setEnabled(not ro)
        a_cut.triggered.connect(lambda: (self._guard("cut") and self.cut()))
        a_copy.triggered.connect(self.copy)
        a_paste.triggered.connect(self.paste)
        a_all.triggered.connect(self.selectAll)
        if self.extra_actions:
            m.addSeparator()
            for a in self.extra_actions:
                m.addAction(a)
        m.exec(ev.globalPos())
