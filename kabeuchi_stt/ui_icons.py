# -*- coding: utf-8 -*-
"""ボタン用のアイコン（Qt のコード描画。絵文字は使わない）。

make_icon(name) は QIcon を返す。状態ごとに色を変える:
  通常 = 明るいグレー / ホバー(Active) = 白 / 無効 = 暗いグレー / トグル ON(State On) = アクセント色
fixed_color を渡すと全状態をその色にする（赤い録音ボタンの白いアイコン等）。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

import ui_style as S

NAMES = ("rec", "stop", "pause", "play", "compact", "expand", "pin", "mic", "wave", "folder", "gear", "close", "drawer", "chev_down", "chev_up")
SIZES = (16, 20, 24, 32, 48)

NORMAL = "#c3c9d6"
HOVER = "#ffffff"
DISABLED = "#566073"
ON = S.ACCENT_HI


def _pen(color: QColor, w: float) -> QPen:
    p = QPen(color, w)
    p.setCapStyle(Qt.RoundCap)
    p.setJoinStyle(Qt.RoundJoin)
    return p


def draw(name: str, size: int, color: str) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    s = float(size)
    w = max(1.4, s * 0.1)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    if name == "rec":
        p.drawEllipse(QRectF(s * 0.22, s * 0.22, s * 0.56, s * 0.56))
    elif name == "stop":
        p.drawRoundedRect(QRectF(s * 0.24, s * 0.24, s * 0.52, s * 0.52), s * 0.1, s * 0.1)
    elif name == "pause":
        p.drawRoundedRect(QRectF(s * 0.26, s * 0.22, s * 0.17, s * 0.56), s * 0.05, s * 0.05)
        p.drawRoundedRect(QRectF(s * 0.57, s * 0.22, s * 0.17, s * 0.56), s * 0.05, s * 0.05)
    elif name == "play":
        path = QPainterPath()
        path.moveTo(s * 0.32, s * 0.2)
        path.lineTo(s * 0.8, s * 0.5)
        path.lineTo(s * 0.32, s * 0.8)
        path.closeSubpath()
        p.drawPath(path)
    elif name in ("compact", "expand"):
        # 窓の枠 + 角へ向かう / 角から離れる矢印
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w))
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.18, s * 0.72, s * 0.64), s * 0.1, s * 0.1)
        if name == "compact":     # 内側へ縮む矢印（先端は左上寄り）
            tip, tail, d = (0.44, 0.44), (0.74, 0.74), 0.17
        else:                     # 外側へ広がる矢印（先端は右下寄り）
            tip, tail, d = (0.76, 0.76), (0.46, 0.46), -0.17
        p.drawLine(QPointF(s * tail[0], s * tail[1]), QPointF(s * tip[0], s * tip[1]))
        p.drawLine(QPointF(s * tip[0], s * tip[1]), QPointF(s * (tip[0] + d), s * tip[1]))
        p.drawLine(QPointF(s * tip[0], s * tip[1]), QPointF(s * tip[0], s * (tip[1] + d)))
    elif name == "pin":
        # 画びょう: 頭（斜め）＋針
        p.save()
        p.translate(s * 0.5, s * 0.5)
        p.rotate(40)
        p.translate(-s * 0.5, -s * 0.5)
        p.drawRoundedRect(QRectF(s * 0.32, s * 0.12, s * 0.36, s * 0.14), s * 0.04, s * 0.04)
        p.drawRoundedRect(QRectF(s * 0.40, s * 0.24, s * 0.20, s * 0.30), s * 0.04, s * 0.04)
        p.drawRoundedRect(QRectF(s * 0.32, s * 0.50, s * 0.36, s * 0.10), s * 0.04, s * 0.04)
        p.setPen(_pen(c, w * 0.9))
        p.drawLine(QPointF(s * 0.5, s * 0.6), QPointF(s * 0.5, s * 0.9))
        p.restore()
    elif name == "mic":
        p.drawRoundedRect(QRectF(s * 0.38, s * 0.12, s * 0.24, s * 0.44), s * 0.12, s * 0.12)
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w))
        path = QPainterPath()
        path.moveTo(s * 0.26, s * 0.44)
        path.arcTo(QRectF(s * 0.26, s * 0.30, s * 0.48, s * 0.40), 180, 180)
        p.drawPath(path)
        p.drawLine(QPointF(s * 0.5, s * 0.70), QPointF(s * 0.5, s * 0.86))
        p.drawLine(QPointF(s * 0.38, s * 0.86), QPointF(s * 0.62, s * 0.86))
    elif name == "wave":
        p.setPen(_pen(c, w * 1.1))
        for i, h in enumerate((0.25, 0.5, 0.8, 0.45, 0.65, 0.3)):
            x = s * (0.18 + i * 0.13)
            p.drawLine(QPointF(x, s * (0.5 - h / 2)), QPointF(x, s * (0.5 + h / 2)))
    elif name == "folder":
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w))
        path = QPainterPath()
        path.moveTo(s * 0.12, s * 0.30)
        path.lineTo(s * 0.12, s * 0.78)
        path.lineTo(s * 0.88, s * 0.78)
        path.lineTo(s * 0.88, s * 0.36)
        path.lineTo(s * 0.50, s * 0.36)
        path.lineTo(s * 0.42, s * 0.24)
        path.lineTo(s * 0.12, s * 0.24)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "gear":
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w))
        p.drawEllipse(QRectF(s * 0.30, s * 0.30, s * 0.40, s * 0.40))
        for k in range(8):
            a = math.radians(k * 45)
            p.drawLine(QPointF(s * (0.5 + 0.30 * math.cos(a)), s * (0.5 + 0.30 * math.sin(a))),
                       QPointF(s * (0.5 + 0.40 * math.cos(a)), s * (0.5 + 0.40 * math.sin(a))))
    elif name == "drawer":
        # 窓の枠＋右の引き出し（縦線と、中の行）
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w))
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.6), s * 0.1, s * 0.1)
        p.drawLine(QPointF(s * 0.58, s * 0.2), QPointF(s * 0.58, s * 0.8))
        p.drawLine(QPointF(s * 0.67, s * 0.38), QPointF(s * 0.77, s * 0.38))
        p.drawLine(QPointF(s * 0.67, s * 0.5), QPointF(s * 0.77, s * 0.5))
        p.drawLine(QPointF(s * 0.67, s * 0.62), QPointF(s * 0.77, s * 0.62))
    elif name in ("chev_down", "chev_up"):
        p.setBrush(Qt.NoBrush)
        p.setPen(_pen(c, w * 1.1))
        y0, y1 = (0.38, 0.62) if name == "chev_down" else (0.62, 0.38)
        p.drawLine(QPointF(s * 0.28, s * y0), QPointF(s * 0.5, s * y1))
        p.drawLine(QPointF(s * 0.5, s * y1), QPointF(s * 0.72, s * y0))
    elif name == "close":
        p.setPen(_pen(c, w))
        p.drawLine(QPointF(s * 0.28, s * 0.28), QPointF(s * 0.72, s * 0.72))
        p.drawLine(QPointF(s * 0.72, s * 0.28), QPointF(s * 0.28, s * 0.72))
    p.end()
    return pm


def make_icon(name: str, fixed_color: str | None = None, on_color: str = ON) -> QIcon:
    icon = QIcon()
    for size in SIZES:
        if fixed_color:
            for mode in (QIcon.Normal, QIcon.Active, QIcon.Disabled, QIcon.Selected):
                for state in (QIcon.Off, QIcon.On):
                    icon.addPixmap(draw(name, size, fixed_color), mode, state)
            continue
        icon.addPixmap(draw(name, size, NORMAL), QIcon.Normal, QIcon.Off)
        icon.addPixmap(draw(name, size, HOVER), QIcon.Active, QIcon.Off)
        icon.addPixmap(draw(name, size, DISABLED), QIcon.Disabled, QIcon.Off)
        icon.addPixmap(draw(name, size, on_color), QIcon.Normal, QIcon.On)
        icon.addPixmap(draw(name, size, on_color), QIcon.Active, QIcon.On)
        icon.addPixmap(draw(name, size, DISABLED), QIcon.Disabled, QIcon.On)
    return icon
