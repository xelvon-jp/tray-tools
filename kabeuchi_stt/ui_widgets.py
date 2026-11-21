# -*- coding: utf-8 -*-
"""独自ウィジェット: 音量メーター・タイムライン・チップ（折り返し）・クリックで再生できる本文欄・再生。"""
from __future__ import annotations

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QTextCursor,
                           QTextFormat)
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLayout, QListView, QPlainTextEdit,
                               QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QWidget)

import ui_style as S
from timeline import DRAFT, FINAL, PAUSE, SILENCE, WAVE, Row, _mmss


# ------------------------------------------------------------ 折り返すバー（幅が足りなければ次の段へ）
class WrapBarLayout(QLayout):
    """部品を左から並べ、幅が足りなければ次の段へ折り返す。grow に指定した部品（メーター等）は、
    その段の余った幅を埋める（最小幅までは縮む）。部品が隠れていれば詰める。

    どの幅でも「部品の最小幅」を割って切れないことが目的（時間表示・ボタンの文字が切れない）。
    """

    def __init__(self, parent=None, hspace: int = 10, vspace: int = 8):
        super().__init__(parent)
        self._items: list = []
        self._grow: set[int] = set()
        self._hs, self._vs = hspace, vspace
        self.setContentsMargins(0, 0, 0, 0)

    def add(self, widget: QWidget, grow: bool = False) -> None:
        if grow:
            self._grow.add(id(widget))
        self.addWidget(widget)

    # QLayout 必須
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._arrange(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, False)

    def sizeHint(self):
        """全部を 1 段に並べたときの大きさ（入れ子にしたとき、親が段への詰め込みに使う）。"""
        w = h = 0
        n = 0
        for it in self._items:
            if it.isEmpty():
                continue
            sh = it.sizeHint()
            w += max(sh.width(), it.minimumSize().width())
            h = max(h, sh.height())
            n += 1
        w += self._hs * max(0, n - 1)
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self):
        w = h = 0
        for it in self._items:
            if it.isEmpty():
                continue
            m = it.minimumSize()
            w, h = max(w, m.width()), max(h, it.sizeHint().height())
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def _need(self, it) -> int:
        """段への詰め込みで使う幅。grow は最小幅、それ以外は推奨幅。"""
        if it.widget() is not None and id(it.widget()) in self._grow:
            return it.minimumSize().width()
        return max(it.sizeHint().width(), it.minimumSize().width())

    def _arrange(self, rect: QRect, test: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        items = [it for it in self._items if not it.isEmpty()]
        lines: list[list] = []
        cur: list = []
        x = 0
        for it in items:
            w = self._need(it)
            if cur and x + w > r.width():
                lines.append(cur)
                cur, x = [], 0
            cur.append(it)
            x += w + self._hs
        if cur:
            lines.append(cur)
        y = r.y()
        for ln in lines:
            needs = [self._need(it) for it in ln]
            spare = r.width() - (sum(needs) + self._hs * (len(ln) - 1))
            grows = [it for it in ln if it.widget() is not None and id(it.widget()) in self._grow]
            widths = []
            for it, need in zip(ln, needs):
                w = need
                if it in grows and spare > 0:
                    w = need + spare // len(grows)
                widths.append(min(w, r.width()))
            # 高さ: 入れ子の折り返し部品などは、その幅での高さ（heightForWidth）を使う
            heights = [(it.heightForWidth(w) if it.hasHeightForWidth() else it.sizeHint().height())
                       for it, w in zip(ln, widths)]
            h = max(heights)
            x = r.x()
            for it, w, ih in zip(ln, widths, heights):
                if not test:
                    it.setGeometry(QRect(x, y + (h - ih) // 2, w, ih))
                x += w + self._hs
            y += h + self._vs
        total = (y - self._vs - r.y()) if lines else 0
        return total + m.top() + m.bottom()


# ------------------------------------------------------------ 幅が足りなければ「…」で省略するラベル
class ElidedLabel(QLabel):
    """text() は全文を返す。表示だけを幅に合わせて省略する（最小幅は小さい＝レイアウトを押し広げない）。"""

    def __init__(self, text: str = "", parent=None, pad: int = 0):
        super().__init__(parent)
        self._full = ""
        self._pad = pad  # スタイルシートの左右 padding + 余白の合計
        self.setText(text)

    def text(self) -> str:
        return self._full

    def setText(self, t: str) -> None:
        if (t or "") == self._full and self._full:
            return
        self._full = t or ""
        self.setToolTip(self._full if self._full else "")
        self._apply()
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(fm.horizontalAdvance(self._full) + self._pad + 2, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(min(self.sizeHint().width(), 56 + self._pad), super().minimumSizeHint().height())

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        w = self.width() - self._pad
        shown = self._full if (w <= 0 or fm.horizontalAdvance(self._full) <= w) else fm.elidedText(
            self._full, Qt.ElideRight, max(10, w))
        if QLabel.text(self) != shown:
            QLabel.setText(self, shown)


# ------------------------------------------------------------ チェックボックス（ON/OFF がひと目で分かる）
class CheckBox(QCheckBox):
    """ON = 明るいアクセント色の塗りつぶし＋白いチェック、OFF = 明るい枠線だけ。
    フォーカス時はリング、無効時は色を落とす。自前で描くので DPI・スタイルシートの影響を受けない。"""
    BOX = 18
    GAP = 9

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        fm = QFontMetrics(self.font())
        w = 4 + self.BOX + self.GAP + fm.horizontalAdvance(self.text()) + 4
        return QSize(w, max(self.BOX + 8, fm.height() + 8))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        on, en = self.isChecked(), self.isEnabled()
        hover = self.underMouse() and en
        focus = self.hasFocus() and en
        h = self.height()
        box = QRectF(4, (h - self.BOX) / 2.0, self.BOX, self.BOX)
        if focus:  # フォーカスリング
            ring = QPen(QColor(S.ACCENT_HI), 2)
            ring.setColor(QColor(125, 180, 255, 200))
            p.setPen(ring)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 7, 7)
        if on:
            fill = QColor(S.ACCENT_HI if hover else S.ACCENT) if en else QColor("#3a4a66")
            p.setPen(QPen(fill.lighter(115) if en else QColor("#4a5a78"), 1.5))
            p.setBrush(fill)
            p.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 5, 5)
            mark = QPainterPath()  # チェックマーク
            x0, y0, b = box.x(), box.y(), self.BOX
            mark.moveTo(x0 + b * 0.24, y0 + b * 0.54)
            mark.lineTo(x0 + b * 0.43, y0 + b * 0.72)
            mark.lineTo(x0 + b * 0.77, y0 + b * 0.29)
            pen = QPen(QColor("#ffffff") if en else QColor("#9aa6bd"), max(2.2, b * 0.14))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawPath(mark)
        else:
            edge = QColor(S.ACCENT_HI) if hover else QColor(S.CTRL_OFF_BORDER if en else "#3a4152")
            p.setPen(QPen(edge, 2.0))
            p.setBrush(QColor("#161920") if en else QColor("#14161a"))
            p.drawRoundedRect(box.adjusted(1, 1, -1, -1), 5, 5)
        p.setPen(QColor(S.TEXT) if en else QColor(S.CTRL_DISABLED))
        tr = QRectF(box.right() + self.GAP, 0, self.width() - box.right() - self.GAP, h)
        p.setFont(self.font())
        p.drawText(tr, Qt.AlignVCenter | Qt.AlignLeft, self.text())


# ------------------------------------------------------------ メーター
class MeterWidget(QWidget):
    """横長の音量メーター。0..1 を緑→黄→赤で描く。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self._hold = 0.0
        self._unrec = False
        self.setMinimumSize(110, 22)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(22)

    def set_unrecorded(self, on: bool) -> None:
        """録音していないのに話しているとき True（メーターを赤にし、「● 未録音」の印を添える）。"""
        on = bool(on)
        if on != self._unrec:
            self._unrec = on
            self.setToolTip("録音されていません" if on else "")
            self.update()

    @property
    def unrecorded(self) -> bool:
        return self._unrec

    def set_level(self, v: float) -> None:
        v = max(0.0, min(1.0, v))
        self._hold = max(v, self._hold - 0.02)
        if abs(v - self._v) > 0.003:
            self._v = v
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(S.BORDER)))
        p.setBrush(QColor(S.PANEL))
        p.drawRoundedRect(r, 5, 5)
        n = 40
        w = (r.width() - 6) / n
        for i in range(n):
            f = (i + 1) / n
            on = f <= self._v
            col = QColor(S.OKGREEN) if f < 0.7 else QColor("#e6c34a") if f < 0.9 else QColor(S.REC)
            if self._unrec:
                col = QColor("#ff5a5f") if f < 0.9 else QColor("#ff8a8d")   # 録音されていない: 全部赤
            if not on:
                col = QColor("#2a2f3b")
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(r.left() + 3 + i * w, r.top() + 4, max(1.0, w - 2), r.height() - 8), 1.5, 1.5)
        hx = r.left() + 3 + self._hold * (r.width() - 6)
        p.setPen(QPen(QColor(S.TEXT), 2))
        p.drawLine(int(hx), int(r.top() + 3), int(hx), int(r.bottom() - 3))
        if self._unrec:
            # 色に頼らない目印: 右端に赤い印「● 未録音」（狭いときは「●」だけ）
            f = QFont(S.FONT_FAMILY, max(7, S.BASE_PT - 2))
            f.setBold(True)
            p.setFont(f)
            fm = QFontMetrics(f)
            text = "● 未録音" if r.width() >= 150 else "●"
            w = fm.horizontalAdvance(text) + 12
            pill = QRectF(r.right() - w - 3, r.top() + 2, w, r.height() - 4)
            p.setPen(QPen(QColor("#ffffff"), 1))
            p.setBrush(QColor("#c0262b"))
            p.drawRoundedRect(pill, 5, 5)
            p.drawText(pill, Qt.AlignCenter, text)


# ------------------------------------------------------------ タイムライン
class TimelineModel(QAbstractListModel):
    RowRole = Qt.UserRole + 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[Row] = []
        self._tl = None
        self._ver = -1

    def set_timeline(self, tl) -> None:
        self.beginResetModel()
        self._tl, self._ver, self._rows = tl, -1, []
        self.endResetModel()
        self.refresh()

    def refresh(self) -> bool:
        """タイムラインが変わっていれば行を更新。変わったら True。"""
        tl = self._tl
        if tl is None or tl.version == self._ver:
            return False
        self._ver = tl.version
        new = tl.rows()
        old_n = len(self._rows)
        if len(new) > old_n:
            self.beginInsertRows(QModelIndex(), old_n, len(new) - 1)
            self._rows = new
            self.endInsertRows()
            if old_n:
                self.dataChanged.emit(self.index(0), self.index(old_n - 1))
        else:
            self._rows = new
            if new:
                self.dataChanged.emit(self.index(0), self.index(len(new) - 1))
            elif old_n:
                self.beginResetModel()
                self.endResetModel()
        return True

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        r = self._rows[index.row()]
        if role == self.RowRole:
            return r
        if role == Qt.DisplayRole:
            return r.text
        return None


class TimelineDelegate(QStyledItemDelegate):
    LABEL_W = 118
    LABEL_W_COMPACT = 62
    PAD = 10

    def __init__(self, view: "TimelineView"):
        super().__init__(view)
        self.view = view
        self.compact = False
        self.apply_font()

    def apply_font(self) -> None:
        """基準フォントが変わったとき（表示サイズの切替）に作り直す。"""
        self.font_text = QFont(S.FONT_FAMILY, S.BASE_PT + 1)
        self.font_text_compact = QFont(S.FONT_FAMILY, S.BASE_PT)
        self.font_label = QFont("Consolas", S.BASE_PT - 1)

    @property
    def label_w(self) -> int:
        return self.LABEL_W_COMPACT if self.compact else self.LABEL_W

    def _font(self) -> QFont:
        return self.font_text_compact if self.compact else self.font_text

    def _text_width(self) -> int:
        # paint() の本体の幅（行の左右 4px ずつを引いたあと label_w + PAD*3 を引く）と一致させる。ずれると 2 行目が切れる
        return max(100, self.view.viewport().width() - 8 - self.label_w - self.PAD * 3)

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        r: Row = index.data(TimelineModel.RowRole)
        if r is not None and r.kind in (PAUSE, SILENCE):
            return QSize(self.view.viewport().width(), 30 if self.compact else 38)
        h = 40 if self.compact else 52
        if r is not None and r.state in (DRAFT, FINAL) and r.text:
            fm = QFontMetrics(self._font())
            br = fm.boundingRect(0, 0, self._text_width(), 100000, Qt.TextWordWrap, r.text)
            h = max(h, br.height() + self.PAD * 2 + 4)
        return QSize(self.view.viewport().width(), h)

    def _paint_mark(self, p: QPainter, rect: QRect, r: Row) -> None:
        """一時停止の印 / 畳んだ無音の行（細い1行）。"""
        pause = r.kind == PAUSE
        col = QColor(S.ORANGE) if pause else QColor("#5b6270")
        p.setPen(QPen(QColor(S.BORDER)))
        p.setBrush(QColor("#221d12") if pause else QColor(S.PANEL))
        p.drawRoundedRect(QRectF(rect), 7, 7)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(rect.left(), rect.top() + 4, 4, rect.height() - 8), 2, 2)
        p.setFont(QFont(S.FONT_FAMILY, S.BASE_PT - 1 if self.compact else S.BASE_PT))
        p.setPen(QColor("#ffd08a") if pause else QColor("#8a93a3"))
        left = rect.left() + self.PAD + 6
        text = ("❚❚ " if pause else "◌ ") + r.label
        p.drawText(QRect(left, rect.top(), rect.width() - self.PAD * 2, rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
        fm = QFontMetrics(p.font())
        x2 = left + fm.horizontalAdvance(text) + 14
        p.setPen(QColor("#9a8a6a") if pause else QColor("#5b6270"))
        note = r.text if pause else f"{_mmss(r.start)}–{_mmss(r.end)}"
        p.drawText(QRect(x2, rect.top(), max(0, rect.right() - x2 - self.PAD), rect.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, note)

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index) -> None:
        r: Row = index.data(TimelineModel.RowRole)
        if r is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(4, 3, -4, -3)
        if r.kind in (PAUSE, SILENCE):
            self._paint_mark(p, rect, r)
            p.restore()
            return
        selected = bool(option.state & QStyle.State_Selected)
        bg = QColor(S.PANEL2) if selected else QColor(S.PANEL)
        p.setPen(QPen(QColor(S.BORDER)))
        p.setBrush(bg)
        p.drawRoundedRect(QRectF(rect), 7, 7)
        # 左の細い帯: 状態の色
        col = {WAVE: QColor(S.TEAL), DRAFT: QColor(S.ORANGE), FINAL: QColor("#ffffff")}[r.state]
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QRectF(rect.left(), rect.top() + 4, 4, rect.height() - 8), 2, 2)
        # 時刻ラベル（コンパクトでは開始時刻だけ）
        lw = self.label_w
        lab = QRect(rect.left() + self.PAD + 4, rect.top(), lw, rect.height())
        p.setFont(self.font_label)
        p.setPen(QColor(S.MUTED))
        tag = {WAVE: "波形", DRAFT: "仮認識", FINAL: "清書"}[r.state]
        if self.compact:
            # コンパクト: 開始時刻だけ（状態は左の帯の色。録音中の行だけ赤い点）
            if r.live:
                p.setPen(QColor(S.REC))
            p.drawText(lab, Qt.AlignVCenter | Qt.AlignLeft, _mmss(r.start) + (" ●" if r.live else ""))
        else:
            p.drawText(lab.adjusted(0, 8, 0, 0), Qt.AlignTop | Qt.AlignLeft, r.label)
            f2 = QFont(S.FONT_FAMILY, S.BASE_PT - 2)
            p.setFont(f2)
            if r.live:
                p.setPen(QColor(S.REC))
                tag = "● 録音中"
            else:
                p.setPen(col if r.state != FINAL else QColor("#cfd3da"))
            p.drawText(lab.adjusted(0, 0, 0, -8), Qt.AlignBottom | Qt.AlignLeft, tag)
        # 本体
        body = QRect(rect.left() + lw + self.PAD * 2, rect.top() + self.PAD // 2 + 1,
                     rect.width() - lw - self.PAD * 3, rect.height() - self.PAD - 2)
        if r.state == WAVE:
            self._paint_wave(p, body, r)
        else:
            p.setFont(self._font())
            p.setPen(QColor(S.ORANGE) if r.state == DRAFT else QColor("#ffffff"))
            p.drawText(body, Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap, r.text)
        p.restore()

    def _paint_wave(self, p: QPainter, body: QRect, r: Row) -> None:
        per_row = r.slots or 60  # 30秒 / 0.5秒（一時停止で区切られた短い行はその長さ）
        n = max(per_row, len(r.peaks))
        w = body.width() / n
        mid = body.center().y()
        half = body.height() / 2.0 - 1
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(S.TEAL))
        for i, v in enumerate(r.peaks):
            h = max(1.5, v * half)
            p.setOpacity(0.35 + 0.65 * v)
            p.drawRoundedRect(QRectF(body.left() + i * w + 0.5, mid - h, max(1.0, w - 1.5), h * 2), 1, 1)
        p.setOpacity(1.0)
        # 未録音の残りは点線
        if len(r.peaks) < per_row:
            p.setPen(QPen(QColor("#394052"), 1, Qt.DotLine))
            x0 = body.left() + len(r.peaks) * w
            p.drawLine(int(x0), int(mid), body.right(), int(mid))


class TimelineView(QListView):
    contextRequested = Signal(int, object)   # (行番号 or -1, グローバル座標)

    def contextMenuEvent(self, ev) -> None:
        idx = self.indexAt(ev.pos())
        self.contextRequested.emit(idx.row() if idx.isValid() else -1, ev.globalPos())
        ev.accept()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = TimelineModel(self)
        self.setModel(self._model)
        self.setItemDelegate(TimelineDelegate(self))
        self.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.setSelectionMode(QListView.SingleSelection)
        self.setUniformItemSizes(False)
        self.setResizeMode(QListView.Adjust)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"QListView {{ background: {S.BG}; border: none; }}")
        self.auto_follow = True
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._ignore_scroll = False

    def set_timeline(self, tl) -> None:
        self._model.set_timeline(tl)
        self.auto_follow = True

    def refresh_fonts(self) -> None:
        self.itemDelegate().apply_font()
        self._model.layoutAboutToBeChanged.emit()
        self._model.layoutChanged.emit()
        self.scheduleDelayedItemsLayout()

    def set_compact(self, on: bool) -> None:
        """コンパクト表示（行を低く・時刻ラベルを短く）。"""
        d = self.itemDelegate()
        if d.compact != on:
            d.compact = on
            self._model.layoutAboutToBeChanged.emit()
            self._model.layoutChanged.emit()
            self.scheduleDelayedItemsLayout()
            self.auto_follow = True
            QTimer.singleShot(0, self._scroll_end)

    def refresh(self) -> None:
        """タイマーから定期的に呼ぶ。変化があれば更新し、末尾を追従。"""
        if self._model.refresh():
            self.scheduleDelayedItemsLayout()
            if self.auto_follow:
                self._ignore_scroll = True
                QTimer.singleShot(0, self._scroll_end)

    def _scroll_end(self):
        try:
            self.scrollToBottom()
        finally:
            self._ignore_scroll = False

    def _on_scroll(self, v: int) -> None:
        if self._ignore_scroll:
            return
        sb = self.verticalScrollBar()
        self.auto_follow = v >= sb.maximum() - 4  # 自分で上へ戻したら追従を止める

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.scheduleDelayedItemsLayout()

    @property
    def row_count(self) -> int:
        return self._model.rowCount()


# ------------------------------------------------------------ チップ（折り返し）
class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._layout(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test):
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            w, h = it.sizeHint().width(), it.sizeHint().height()
            if x + w > rect.right() and line_h > 0:
                x = rect.x()
                y += line_h + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self._sp
            line_h = max(line_h, h)
        return y + line_h - rect.y()


class Chip(QFrame):
    removed = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.text = text
        self.setObjectName("chip")
        self.setStyleSheet(f"#chip {{ background: #243247; border: 1px solid #36507a; border-radius: 13px; }}"
                           f"QLabel {{ background: transparent; }}"
                           f"QPushButton {{ background: transparent; border: none; color: {S.MUTED}; padding: 0 4px; }}"
                           f"QPushButton:hover {{ color: {S.REC}; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(11, 3, 4, 3)
        lay.setSpacing(2)
        lay.addWidget(QLabel(text))
        b = QPushButton("×")
        b.setCursor(Qt.PointingHandCursor)
        b.setFixedSize(22, 22)
        b.setToolTip("削除")
        b.clicked.connect(lambda: self.removed.emit(self.text))
        lay.addWidget(b)


class ChipsWidget(QWidget):
    changed = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._flow = FlowLayout(self)
        self._words: list[str] = []

    def set_words(self, words: list[str]) -> None:
        self._words = list(dict.fromkeys(w for w in words if w))
        self._rebuild()

    def words(self) -> list[str]:
        return list(self._words)

    def add_word(self, w: str) -> bool:
        w = w.strip()
        if not w or w in self._words:
            return False
        self._words.append(w)
        self._rebuild()
        self.changed.emit(self.words())
        return True

    def remove_word(self, w: str) -> None:
        if w in self._words:
            self._words.remove(w)
            self._rebuild()
            self.changed.emit(self.words())

    def _rebuild(self) -> None:
        while self._flow.count():
            it = self._flow.takeAt(0)
            if it and it.widget():
                it.widget().deleteLater()
        for w in self._words:
            c = Chip(w, self)
            c.removed.connect(self.remove_word)
            self._flow.addWidget(c)
        self.updateGeometry()
        self._flow.invalidate()


# ------------------------------------------------------------ クリックで再生できる本文欄
class ClickableText(QPlainTextEdit):
    """行（段落）をクリックしたら lineClicked(行番号)。ドラッグ選択・キー編集では発火しない。"""
    lineClicked = Signal(int)
    contextRequested = Signal(int, str, object)   # (段落番号, 選択中の文字, グローバル座標)

    def contextMenuEvent(self, ev) -> None:
        cur = self.cursorForPosition(ev.pos())
        sel = self.textCursor().selectedText().replace("\u2029", "\n")
        self.contextRequested.emit(cur.blockNumber(), sel, ev.globalPos())
        ev.accept()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._press_pos = None
        self.click_enabled = True

    def mousePressEvent(self, ev):
        self._press_pos = ev.position().toPoint()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        super().mouseReleaseEvent(ev)
        try:
            if (self.click_enabled and ev.button() == Qt.LeftButton and self._press_pos is not None
                    and (ev.position().toPoint() - self._press_pos).manhattanLength() < 4
                    and not self.textCursor().hasSelection()):
                self.lineClicked.emit(self.cursorForPosition(ev.position().toPoint()).blockNumber())
        finally:
            self._press_pos = None

    def highlight_block(self, idx: int | None) -> None:
        sels = []
        if idx is not None and 0 <= idx < self.blockCount():
            from PySide6.QtWidgets import QTextEdit
            cur = QTextCursor(self.document().findBlockByNumber(idx))
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cur
            sel.format.setBackground(QColor("#2a3550"))
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sels.append(sel)
        self.setExtraSelections(sels)


# ------------------------------------------------------------ 再生
class AudioPlayer(QWidget):
    """QMediaPlayer の薄い包み。positionChanged(秒) / stateChanged(再生中か)。

    出力先: 設定（selected_id）が空なら「システム既定」＝**再生を始めるたびに、いまの既定デバイスを取り直す**
    （起動時のデバイスに固定されると、常駐ツールで既定を切り替えても追従しないため）。再生中に既定が
    切り替わったら（デバイス構成の変更通知）、出力を作り直して追従する。選んだデバイスが無くなったら
    システム既定に戻して selectionReset で知らせる。テストでは provider を差し替え、音量 0・ミュートにする。
    """
    position = Signal(float)
    state = Signal(bool)
    devicesChanged = Signal()
    selectionReset = Signal(str)   # 選んだ出力先が無くなってシステム既定に戻した（文言）

    def __init__(self, parent=None, volume: float = 1.0, provider=None, selected_id: str = "",
                 muted: bool = False):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
        from playback_devices import QtDeviceProvider
        self._QMediaPlayer, self._QAudioOutput = QMediaPlayer, QAudioOutput
        self.provider = provider or QtDeviceProvider()
        self._selected = selected_id or ""
        self._volume, self._muted = volume, muted
        self.out = None
        self.applied_id: str | None = None    # いま出力に使っているデバイス id（None=未設定）
        self.apply_count = 0                  # 出力を作り直した回数（確認用）
        self.player = QMediaPlayer(self)
        self.player.positionChanged.connect(lambda ms: self.position.emit(ms / 1000.0))
        self.player.playbackStateChanged.connect(
            lambda st: self.state.emit(st == QMediaPlayer.PlayingState))
        self.refresh_output(force=True)
        self._devs = QMediaDevices(self)
        self._devs.audioOutputsChanged.connect(lambda: self._on_devices_changed())
        self.hide()

    # ---- 出力先
    def devices(self) -> list:
        try:
            return self.provider.list()
        except Exception:
            return []

    @property
    def selected_id(self) -> str:
        return self._selected

    def set_selected(self, dev_id: str) -> None:
        self._selected = dev_id or ""
        self.refresh_output()

    def set_volume(self, v: float) -> None:
        self._volume = v
        if self.out is not None:
            self.out.setVolume(v)

    def set_muted(self, m: bool) -> None:
        self._muted = m
        if self.out is not None:
            self.out.setMuted(m)

    def refresh_output(self, force: bool = False) -> str:
        """設定と、いまのデバイス一覧から出力先を決め、変わっていれば出力を作り直す。知らせがあれば返す。"""
        from playback_devices import resolve_output
        res = resolve_output(self._selected, self.devices())
        notice = res.notice
        if res.reset_setting:
            self._selected = ""
            self.selectionReset.emit(notice)
        dev = res.device
        new_id = dev.id if dev is not None else ""
        if force or self.out is None or new_id != self.applied_id:
            qt = getattr(dev, "qt", None) if dev is not None else None
            out = self._QAudioOutput(qt, self) if qt is not None else self._QAudioOutput(self)
            out.setVolume(self._volume)
            out.setMuted(self._muted)
            self.player.setAudioOutput(out)
            old, self.out = self.out, out
            if old is not None:
                old.deleteLater()
            self.applied_id = new_id
            self.apply_count += 1
        return notice

    def _on_devices_changed(self) -> None:
        try:
            self.refresh_output()  # 既定が変わっていれば再生中でも切り替わる
        finally:
            self.devicesChanged.emit()

    def load(self, path: str) -> None:
        self.player.setSource(QUrl.fromLocalFile(path) if path else QUrl())

    def play_from(self, sec: float) -> None:
        self.refresh_output()  # 再生を始めるたびに、いまの既定（または選んだデバイス）を取り直す
        self.player.setPosition(int(max(0.0, sec) * 1000))
        self.player.play()

    def toggle(self) -> None:
        if self.player.playbackState() == self._QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.refresh_output()
            self.player.play()

    def stop(self) -> None:
        self.player.stop()

    def seek(self, sec: float) -> None:
        self.player.setPosition(int(max(0.0, sec) * 1000))

    @property
    def is_playing(self) -> bool:
        return self.player.playbackState() == self._QMediaPlayer.PlayingState

    @property
    def duration(self) -> float:
        return self.player.duration() / 1000.0
