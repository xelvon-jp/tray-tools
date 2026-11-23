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


# ---- 認識開始ライン（音量バー上のしきい値。純関数）
# バーは -60..0 dBFS（audio_input.level_from_rms と同じ）。ラインは -60..-6 に丸める。
BAR_DB_MIN = -60.0
BAR_DB_MAX = 0.0
LINE_DB_MIN = -60.0
LINE_DB_MAX = -6.0
LINE_DB_DEFAULT = -50.0


def clamp_line_db(v, default: float = LINE_DB_DEFAULT) -> float:
    """認識開始ラインの値を整数 dB に丸め、範囲内へ収める。None・不正値は既定。"""
    try:
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return float(default)
    except (TypeError, ValueError):
        return float(default)
    return float(max(LINE_DB_MIN, min(LINE_DB_MAX, round(f))))


def db_to_frac(db: float) -> float:
    """dBFS → バー上の位置（0..1）。"""
    return max(0.0, min(1.0, (float(db) - BAR_DB_MIN) / (BAR_DB_MAX - BAR_DB_MIN)))


def frac_to_db(frac: float) -> float:
    return BAR_DB_MIN + max(0.0, min(1.0, float(frac))) * (BAR_DB_MAX - BAR_DB_MIN)


def x_to_line_db(x: float, left: float, width: float) -> float:
    """バー上の x 座標（left から width 幅の領域）→ 丸めた dBFS。"""
    if width <= 0:
        return LINE_DB_DEFAULT
    return clamp_line_db(frac_to_db((float(x) - left) / width))


def line_db_to_x(db: float, left: float, width: float) -> float:
    return left + db_to_frac(db) * width


def store_voice_min(settings: dict, db) -> float:
    """設定 dialogue.voice_min_dbfs へ丸めた値を保存して返す（dialogue 節が無ければ作る）。"""
    v = clamp_line_db(db)
    d = settings.get("dialogue")
    if not isinstance(d, dict):
        d = {}
        settings["dialogue"] = d
    d["voice_min_dbfs"] = v
    return v
