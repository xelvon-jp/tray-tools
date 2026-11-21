# -*- coding: utf-8 -*-
"""音量メーターを赤にする判定（純関数・GUI/Qt/COM 非依存）。

録音していない（待機中・一時停止中）のに話し声（発話らしい音量）を検出している間は、メーターを赤にして
「録音されていない」と一目で分かるようにする。帯を出すまでの 1.5 秒を待たず即時に赤、静かになったら
短い保持（既定 0.5 秒）のあとで戻す（ちらつき防止のヒステリシス）。
"""
from __future__ import annotations


def meter_unrecorded(is_recording: bool, is_paused: bool, monitor_active: bool, monitor_error: bool,
                     speaking: bool) -> bool:
    """メーターを「未録音の赤」にすべきか（瞬間値。保持は HoldLatch）。

    - 録音中（一時停止でない）→ 赤にしない（従来の緑）
    - 待機中モニターが閉じている / マイクを使用中で開けない → 赤にしない（音が見えていない）
    - 待機中または一時停止中で、話し声を検出している → 赤
    """
    if monitor_error or not monitor_active:
        return False
    if is_recording and not is_paused:
        return False
    return bool(speaking)


class HoldLatch:
    """True になったら即時に True。False になっても hold_sec は True のまま保つ。"""

    def __init__(self, hold_sec: float = 0.5):
        self.hold_sec = float(hold_sec)
        self._until = 0.0
        self.value = False

    def update(self, now: float, cond: bool) -> bool:
        if cond:
            self._until = now + self.hold_sec
            self.value = True
        elif self.value and now >= self._until:
            self.value = False
        return self.value

    def reset(self) -> None:
        self._until = 0.0
        self.value = False
