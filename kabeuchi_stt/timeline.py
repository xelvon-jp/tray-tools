# -*- coding: utf-8 -*-
"""タイムラインのデータモデル（GUI/Qt/COM 非依存、スレッドセーフ）。

録音を row_sec 秒（既定30秒）ごとの行に分け、各行は次の順に状態が進む:
    WAVE（波形のみ） → DRAFT（仮認識の文字・オレンジ） → FINAL（清書済み・白）
- feed(samples)      : 録音ブロックを渡すと波形（0.5秒ごとのピーク）を積む。録音スレッドから呼んでよい。
- add_draft(s, e, t) : 仮認識の区間を追加（認識スレッドから）。
- set_final(segs)    : 清書結果で全行を置き換える（行は FINAL へ）。
- add_pending(s, e) / remove_pending(s) : AI対話モード。区切った発話が認識待ちの間の印（行の見出しに「認識中」）。
- commit_drafts() / drop_uncommitted() : AI対話モード。ここまでの仮認識を「確定」（白）にする / 確定前の仮認識を捨てる。
- add_pause(at_sec)  : 一時停止の印（WAV 上の位置）。印の行を1行出し、行はその位置で区切る。
- set_pause_length(i, sec): 再開したとき、その一時停止の休止時間（実時間）を入れる。

行の種類（Row.kind）: audio（通常）/ pause（一時停止の印）/ silence（清書後、発話なしの連続を畳んだ行）
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

import numpy as np

from mic_check import to_dbfs

SAMPLE_RATE = 16000
WAVE, DRAFT, FINAL = "wave", "draft", "final"
AUDIO, PAUSE, SILENCE = "audio", "pause", "silence"
BUCKET_SEC = 0.5
_BUCKET = int(SAMPLE_RATE * BUCKET_SEC)


def peak_to_level(peak: float) -> float:
    """int16 のピーク → 0..1（-60dBFS..0dBFS）。波形表示用。"""
    return max(0.0, min(1.0, (to_dbfs(peak) + 60.0) / 60.0))


def fmt_mmss(t: float) -> str:
    t = int(round(t)) if t >= 0 else 0
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:d}:{t % 60:02d}"


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


@dataclass
class Row:
    index: int
    start: float
    end: float
    state: str
    text: str
    peaks: list = field(default_factory=list)  # 0.5 秒ごとの 0..1
    live: bool = False                         # 録音中の最終行
    kind: str = AUDIO
    tag: str = ""                              # 見出しの文言を上書きする（対話モードの「確定」「認識中」）
    slots: int = 0                             # 波形の枠の数（0=row_sec 相当）。区切られた短い行で「未録音の点線」を出さないため

    @property
    def label(self) -> str:
        if self.kind == PAUSE:
            return f"一時停止 {_mmss(self.start)}"
        if self.kind == SILENCE:
            return f"無音 {fmt_mmss(self.end - self.start)}"
        return f"{_mmss(self.start)}–{_mmss(self.end)}"


class Timeline:
    def __init__(self, row_sec: float = 30.0):
        self.row_sec = max(5.0, float(row_sec))
        self._lock = threading.RLock()
        self._peaks: list[float] = []
        self._carry = np.zeros(0, dtype=np.int16)
        self._drafts: list[tuple[float, float, str]] = []
        self._finals: list[tuple[float, float, str]] = []
        self._pending: list[tuple[float, float]] = []   # 認識待ちの発話（対話モード）
        self._committed: set[float] = set()             # 「確定」済みの仮認識（開始秒。対話モード）
        self._pauses: list[list] = []   # [WAV上の秒, 休止の実時間(秒) or None（休止中）]
        self._is_final = False
        self._final_gaps: list | None = None
        self._final_duration = 0.0
        self._recording = False
        self.version = 0  # 変わったら再描画

    # ---- 書き込み
    def set_recording(self, on: bool) -> None:
        with self._lock:
            self._recording = on
            self.version += 1

    def feed(self, x: np.ndarray) -> None:
        x = np.asarray(x)
        if x.size == 0:
            return
        with self._lock:
            buf = np.concatenate([self._carry, x]) if self._carry.size else x
            n = len(buf) // _BUCKET
            for i in range(n):
                seg = buf[i * _BUCKET:(i + 1) * _BUCKET]
                self._peaks.append(peak_to_level(float(np.abs(seg.astype(np.int32)).max())))
            self._carry = buf[n * _BUCKET:].copy()
            self.version += 1

    def add_draft(self, start: float, end: float, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._drafts.append((float(start), float(end), text))
            self.version += 1

    def add_pending(self, start: float, end: float) -> None:
        with self._lock:
            self._pending.append((float(start), float(end)))
            self.version += 1

    def remove_pending(self, start: float) -> None:
        with self._lock:
            for i, (s, _) in enumerate(self._pending):
                if abs(s - float(start)) < 1e-6:
                    del self._pending[i]
                    self.version += 1
                    return

    def set_draft(self, start: float, end: float, text: str) -> None:
        """対話モード: 開始秒をキーに、仮認識（base）→清書（small）の置き換えができる版の add_draft。空文字なら消す。"""
        text = (text or "").strip()
        start = float(start)
        with self._lock:
            for i, (s, _e, _t) in enumerate(self._drafts):
                if abs(s - start) < 1e-6:
                    if text:
                        self._drafts[i] = (s, float(end), text)
                    else:
                        del self._drafts[i]
                    self.version += 1
                    return
            if text:
                self._drafts.append((start, float(end), text))
                self._drafts.sort(key=lambda d: d[0])
                self.version += 1

    def remove_draft(self, start: float) -> None:
        self.set_draft(start, start, "")

    def commit_drafts(self) -> None:
        """いまある仮認識をすべて「確定」にする（対話モードで確定操作を受け付けたとき）。"""
        with self._lock:
            now = {s for s, _e, _t in self._drafts}
            if not now <= self._committed:
                self._committed |= now
                self.version += 1

    def drop_uncommitted(self) -> None:
        """確定前の仮認識を捨てる（対話モードで下書きを破棄したとき）。"""
        with self._lock:
            keep = [d for d in self._drafts if d[0] in self._committed]
            if len(keep) != len(self._drafts):
                self._drafts = keep
                self.version += 1

    def add_pause(self, at_sec: float) -> int:
        """一時停止の印を追加。戻り値は印の番号（set_pause_length 用）。"""
        with self._lock:
            self._pauses.append([max(0.0, float(at_sec)), None])
            self.version += 1
            return len(self._pauses) - 1

    def set_pause_length(self, index: int, sec: float) -> None:
        with self._lock:
            if 0 <= index < len(self._pauses):
                self._pauses[index][1] = max(0.0, float(sec))
                self.version += 1

    def set_final(self, segments: list, duration: float | None = None, gaps: list | None = None) -> None:
        with self._lock:
            self._final_gaps = list(gaps) if gaps is not None else None
            self._finals = [(float(a), float(b), str(t)) for a, b, t in segments]
            self._is_final = True
            if duration is not None:
                self._final_duration = float(duration)
            self.version += 1

    def clear(self) -> None:
        with self._lock:
            v = self.version + 1
            self._peaks, self._carry = [], np.zeros(0, dtype=np.int16)
            self._drafts, self._finals, self._pauses = [], [], []
            self._pending, self._committed = [], set()
            self._is_final, self._final_duration, self._recording = False, 0.0, False
            self.version = v

    # ---- 読み出し
    @property
    def is_final(self) -> bool:
        return self._is_final

    @property
    def pause_count(self) -> int:
        with self._lock:
            return len(self._pauses)

    @property
    def duration(self) -> float:
        with self._lock:
            d = (len(self._peaks) * _BUCKET + len(self._carry)) / SAMPLE_RATE
            if self._is_final:
                d = max(d, self._final_duration, max((e for _, e, _ in self._finals), default=0.0))
            return d

    def final_text(self) -> str:
        with self._lock:
            return "\n".join(t for _, _, t in self._finals if t)

    def final_gaps(self) -> list | None:
        with self._lock:
            return None if self._final_gaps is None else list(self._final_gaps)

    def final_segments(self) -> list:
        with self._lock:
            return list(self._finals)

    def draft_segments(self) -> list:
        """仮認識の区間 (開始秒, 終了秒, 文字) の控え（開始秒順ではなく追加順）。統合 GUI の下書き欄が、発話ごとの黄色の行にするために読む。"""
        with self._lock:
            return list(self._drafts)

    def rows(self) -> list[Row]:
        with self._lock:
            dur = self.duration
            rs = self.row_sec
            per_row = int(rs / BUCKET_SEC)
            pauses = sorted(self._pauses, key=lambda p: p[0])
            # 時間軸を、一時停止の位置で区切った区間に分ける
            cuts = [0.0] + [min(p[0], dur) for p in pauses] + [dur]
            rows: list[Row] = []
            for k in range(len(cuts) - 1):
                s0, s1 = cuts[k], cuts[k + 1]
                closed = k < len(pauses)            # この区間は一時停止で閉じている（これ以上伸びない）
                n = int(math.ceil((s1 - s0) / rs - 1e-9)) if s1 - s0 > 1e-6 else 0
                for i in range(n):
                    a, b = s0 + i * rs, min(s0 + (i + 1) * rs, s1)
                    last = i == n - 1
                    # 一時停止で閉じた区間の最後の行は、その位置までの区間だけを取る（次の区間へはみ出さない）。
                    # 現在伸びている最後の行は、従来どおり a+rs 未満の開始を取る
                    hi = b if (not last or closed) else a + rs
                    ia = int(round(a / BUCKET_SEC))
                    peaks = self._peaks[ia:ia + per_row]
                    tag = ""
                    if self._is_final:
                        state = FINAL
                        text = "".join(t for s, _, t in self._finals if a <= s < hi)
                    else:
                        inrow = [(s, t) for s, _, t in self._drafts if a <= s < hi]
                        drafts = [t for _s, t in inrow]
                        state = DRAFT if drafts else WAVE
                        text = "".join(drafts)
                        if drafts and all(s in self._committed for s, _t in inrow):
                            state, tag = FINAL, "確定"        # 対話モード: 確定済みの発話だけの行は白
                        elif not drafts and any(a <= s < hi for s, _ in self._pending):
                            tag = "認識中"
                    slots = per_row if (last and not closed) else max(1, int(math.ceil((b - a) / BUCKET_SEC - 1e-9)))
                    rows.append(Row(0, a, b, state, text, list(peaks),
                                    live=self._recording and last and not closed, tag=tag,
                                    slots=0 if slots >= per_row else slots))
                if k < len(pauses):
                    at, length = pauses[k]
                    note = "休止中" if length is None else f"休止 {fmt_mmss(length)}"
                    rows.append(Row(0, at, at, FINAL if self._is_final else DRAFT, note, [], kind=PAUSE))
            rows = self._collapse_silence(rows)
            for i, r in enumerate(rows):
                r.index = i
            return rows

    @staticmethod
    def _collapse_silence(rows: list[Row]) -> list[Row]:
        """清書後、発話なし（空の FINAL 行）の連続を「無音 2:30」の1行へ畳む。"""
        out: list[Row] = []
        for r in rows:
            quiet = r.kind == AUDIO and r.state == FINAL and not r.text and not r.live
            if quiet:
                if out and out[-1].kind == SILENCE:
                    out[-1].end = r.end
                    continue
                out.append(Row(0, r.start, r.end, FINAL, "", [], kind=SILENCE))
            else:
                out.append(r)
        return out
