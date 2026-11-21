# -*- coding: utf-8 -*-
"""窓の位置とサイズの保存・復元（純関数・Qt 非依存）。画面外に出ないようにクランプする。"""
from __future__ import annotations

from typing import Sequence

Rect = tuple[int, int, int, int]  # (x, y, w, h)


def parse_geometry(value) -> Rect | None:
    """設定値 [x, y, w, h] を (x, y, w, h) に。不正なら None。"""
    try:
        x, y, w, h = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return x, y, w, h


def _overlap(a: Rect, b: Rect) -> int:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return max(0, w) * max(0, h)


def clamp_geometry(rect: Rect, screens: Sequence[Rect], min_size: tuple[int, int] = (320, 200)) -> Rect:
    """rect を、いちばん重なりの大きい画面（無ければ先頭の画面）の作業領域に収める。

    サイズは画面より大きければ画面に合わせて縮め、位置は窓が全体として画面内に入るように寄せる。
    screens が空なら何もしない。
    """
    if not screens:
        return rect
    x, y, w, h = rect
    scr = max(screens, key=lambda s: _overlap(rect, s))
    if _overlap(rect, scr) == 0:
        scr = screens[0]
    sx, sy, sw, sh = scr
    w = max(min(min_size[0], sw), min(w, sw))
    h = max(min(min_size[1], sh), min(h, sh))
    x = min(max(x, sx), sx + sw - w)
    y = min(max(y, sy), sy + sh - h)
    return x, y, w, h
