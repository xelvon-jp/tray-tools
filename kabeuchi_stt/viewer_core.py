# -*- coding: utf-8 -*-
"""閲覧画面のロジック（GUI/Qt/COM 非依存）。テキストの行 → 音声の再生位置。"""
from __future__ import annotations


def viewer_segments(segments: list) -> list:
    """閲覧画面で 1 行 = 1 区間として並べる、空でない区間（txt の行と一致させる）。"""
    return [s for s in segments if str(s[2]).strip()]


def seek_time_for_block(segments: list, block_index: int, block_count: int, duration: float = 0.0) -> float | None:
    """クリックした行（段落）番号から再生開始秒を求める。

    清書の各区間が 1 段落になっている間は区間の開始時刻を使う。ユーザーが段落を増減して
    数が合わなくなったら、位置に比例して概算する（duration が無ければ最後の区間の終わり）。
    """
    if block_count <= 0 or block_index < 0:
        return None
    if segments and len(segments) == block_count:
        return float(segments[block_index][0])
    total = duration or (float(segments[-1][1]) if segments else 0.0)
    if total <= 0:
        return None
    return total * min(block_index, block_count - 1) / block_count


def block_for_time(segments: list, t: float) -> int | None:
    """再生位置（秒）に対応する区間番号（再生中の行のハイライト用）。"""
    idx = None
    for i, (a, b, _) in enumerate(segments):
        if a <= t:
            idx = i
        else:
            break
    return idx
