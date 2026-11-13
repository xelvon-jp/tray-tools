# テキストエディタのアイコン。白い文書のページに本文の行と鉛筆を
# QPainter で描いて作る(バイナリの画像ファイルは置かない)。
# 小さいサイズでは鉛筆を落としてページと行だけにする。

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF

_PAGE_FILL = QColor(255, 255, 255)
_PAGE_BORDER = QColor(122, 130, 142)
_FOLD_FILL = QColor(221, 227, 236)
_LINE_HEAD = QColor(61, 90, 153)
_LINE_BODY = QColor(167, 176, 190)
_PENCIL_BODY = QColor(245, 166, 35)
_PENCIL_WOOD = QColor(243, 226, 199)
_PENCIL_LEAD = QColor(58, 63, 71)
_PENCIL_ERASER = QColor(232, 140, 160)
_PENCIL_FERRULE = QColor(176, 183, 194)

_SIZES = (16, 24, 32, 48, 64)


def _draw_page(painter, s, with_pencil):
    """文書のページ(折り返しつき)と本文の行を描く。"""
    left = 0.10 * s if with_pencil else 0.08 * s
    right = 0.82 * s if with_pencil else 0.92 * s
    top, bottom = 0.05 * s, 0.95 * s
    fold = 0.17 * s
    painter.setPen(QPen(_PAGE_BORDER, max(1.0, 0.040 * s)))
    painter.setBrush(_PAGE_FILL)
    page = QPolygonF([
        QPointF(left, top), QPointF(right - fold, top),
        QPointF(right, top + fold), QPointF(right, bottom),
        QPointF(left, bottom),
    ])
    painter.drawPolygon(page)
    # 右上の折り返し。
    painter.setBrush(_FOLD_FILL)
    painter.drawPolygon(QPolygonF([
        QPointF(right - fold, top), QPointF(right, top + fold),
        QPointF(right - fold, top + fold),
    ]))
    # 本文の行。1行目だけ見出し色で濃く。
    line_x = left + 0.10 * s
    line_w = (right - 0.09 * s) - line_x
    line_h = 0.052 * s
    rows = [(0.30, 1.00, _LINE_HEAD), (0.44, 1.00, _LINE_BODY),
            (0.58, 0.85, _LINE_BODY), (0.72, 0.55, _LINE_BODY)]
    painter.setPen(Qt.PenStyle.NoPen)
    for cy, frac, color in rows:
        if not with_pencil and cy > 0.60:
            continue  # 小さいサイズは行を間引く
        painter.setBrush(color)
        painter.drawRoundedRect(
            QRectF(line_x, cy * s, line_w * frac, line_h),
            line_h / 2, line_h / 2)


def _draw_pencil(painter, s):
    """右下に斜めの鉛筆を描く(先が左下)。"""
    painter.save()
    painter.translate(0.74 * s, 0.72 * s)
    painter.rotate(-38)
    length, thick = 0.54 * s, 0.125 * s
    x0 = -length / 2
    painter.setPen(QPen(_PAGE_BORDER, max(1.0, 0.028 * s)))
    # 本体。
    painter.setBrush(_PENCIL_BODY)
    painter.drawRect(QRectF(x0 + 0.16 * s, -thick / 2,
                            length - 0.22 * s, thick))
    # 木の先と芯。
    painter.setBrush(_PENCIL_WOOD)
    painter.drawPolygon(QPolygonF([
        QPointF(x0, 0), QPointF(x0 + 0.16 * s, -thick / 2),
        QPointF(x0 + 0.16 * s, thick / 2),
    ]))
    painter.setBrush(_PENCIL_LEAD)
    tip = 0.055 * s
    painter.drawPolygon(QPolygonF([
        QPointF(x0, 0), QPointF(x0 + tip, -thick / 2 * (tip / (0.16 * s))),
        QPointF(x0 + tip, thick / 2 * (tip / (0.16 * s))),
    ]))
    # 金具と消しゴム。
    painter.setBrush(_PENCIL_FERRULE)
    painter.drawRect(QRectF(x0 + length - 0.11 * s, -thick / 2,
                            0.05 * s, thick))
    painter.setBrush(_PENCIL_ERASER)
    painter.drawRect(QRectF(x0 + length - 0.06 * s, -thick / 2,
                            0.06 * s, thick))
    painter.restore()


def render(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    with_pencil = size >= 24
    _draw_page(painter, size, with_pencil)
    if with_pencil:
        _draw_pencil(painter, size)
    painter.end()
    return pixmap


def qicon() -> QIcon:
    """窓・タスクバー用の QIcon(複数サイズ入り)。"""
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(render(size))
    return icon
