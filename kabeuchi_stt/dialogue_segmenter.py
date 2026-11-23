# -*- coding: utf-8 -*-
"""AI対話モード: 録音の音声ブロックを、無音で発話ごとに区切る（純ロジック。GUI/Qt/COM 非依存）。

- feed(x) は録音の AudioTap から呼ばれる（int16, 16kHz）。**軽く保つ**: 認識はしない、例外を外へ出さない。
  区切れた発話は戻り値で返し、on_utterance があればそれにも渡す（呼び出しはロックの外）。
- 「発話あり」の判定は silence.SilenceWatcher と同じ基準（0.1秒フレームのピーク dBFS と、直近30秒の床との差）。
- 区切りの規則
    * 無音が gap_sec 続いたら発話を確定（末尾の無音は tail_sec だけ残して切り詰める）。
    * 発話の頭に、直前の preroll_sec 分の音を付ける（出だしの取りこぼし防止）。
    * 声のあるフレームが min_voiced_sec に満たない塊は捨てる（クリック音・咳払いの断片）。
    * max_utt_sec に達したら強制で区切る。区切る位置は、末尾の側でいちばん静かなフレーム
      （単語の途中を割りにくくする）。区切った残りは、そのまま次の発話の頭になる。
- 位置（start_sec / end_sec）は feed した総サンプル数から数える。WAV 上の時刻と一致する
  （reset しても数え直さない。discard_pending は位置も床も保つ）。
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from mic_check import FRAME, SAMPLE_RATE, to_dbfs

FRAME_SEC = FRAME / SAMPLE_RATE  # 0.1 秒
WARMUP_FRAMES = 10               # 床の推定が落ち着くまでのフレーム数（約1秒）


@dataclass
class Utterance:
    audio: np.ndarray      # int16 モノラル 16kHz
    start_sec: float       # 先頭（preroll を含む）の、録音の頭からの秒
    end_sec: float
    voiced_sec: float      # 声ありと判定したフレームの合計秒
    forced: bool = False   # 最長で強制的に区切ったか

    @property
    def seconds(self) -> float:
        return len(self.audio) / SAMPLE_RATE


class _Frame:
    __slots__ = ("data", "rms", "voiced", "loud")

    def __init__(self, data, rms, voiced, loud=True):
        self.data, self.rms, self.voiced, self.loud = data, rms, voiced, loud


class UtteranceSegmenter:
    def __init__(self, gap_sec: float = 1.2, abs_dbfs: float = -50.0, min_voiced_sec: float = 0.3,
                 max_utt_sec: float = 30.0, preroll_sec: float = 0.3, tail_sec: float = 0.3,
                 on_utterance: Callable[[Utterance], None] | None = None,
                 loud_dbfs: float | None = None, min_loud_sec: float = 0.3):
        """loud_dbfs（None なら使わない）: 「明らかに喋っている」と言える大きさ（0.1秒ごとの平均＝RMS の dBFS。音量バーの表示と同じ基準）。この大きさの
        フレームが min_loud_sec 分に満たない塊は、声ありのフレームがあっても捨てる（小さな物音・遠くの声・幻覚の元を、
        波形・仮認識・清書へ回さない）。"""
        self.loud_dbfs = None if loud_dbfs is None else float(loud_dbfs)
        self.min_loud_frames = max(1, int(round(float(min_loud_sec) / FRAME_SEC)))
        self.gap_frames = max(1, int(round(float(gap_sec) / FRAME_SEC)))
        self.abs_dbfs = float(abs_dbfs)
        self.min_voiced_frames = max(1, int(round(float(min_voiced_sec) / FRAME_SEC)))
        self.max_frames = max(self.gap_frames + 1, int(round(float(max_utt_sec) / FRAME_SEC)))
        self.preroll_frames = max(0, int(round(float(preroll_sec) / FRAME_SEC)))
        self.tail_frames = max(0, int(round(float(tail_sec) / FRAME_SEC)))
        self.on_utterance = on_utterance
        self.errors = 0          # on_utterance が例外を出した回数（録音は止めない）
        self._lock = threading.Lock()
        self._carry = np.zeros(0, dtype=np.int16)
        self._rms_hist: deque[float] = deque(maxlen=300)
        self._floor_db = -120.0
        self._n = 0                      # 処理したフレーム総数（位置の基準）
        self._pre: deque[_Frame] = deque(maxlen=self.preroll_frames) if self.preroll_frames else deque(maxlen=1)
        self._cur: list[_Frame] = []
        self._cur_start = 0              # _cur[0] の絶対フレーム番号
        self._active = False
        self._sil = 0                    # 最後に声ありだったフレーム以降の連続無音フレーム数

    def set_loud_dbfs(self, v: float | None) -> None:
        """認識開始音量（loud_dbfs）を差し替える。次のフレームから効く（None なら無効）。"""
        with self._lock:
            self.loud_dbfs = None if v is None else float(v)

    # ---- 状態（どのスレッドからでも読める。整数/真偽の単純な読み取り）
    @property
    def in_speech(self) -> bool:
        """いま発話の途中（声があり、まだ区切っていない）か。"""
        return self._active

    @property
    def silence_sec(self) -> float:
        """最後に声が入ってからの無音の長さ（秒）。"""
        return self._sil * FRAME_SEC

    @property
    def position_sec(self) -> float:
        return self._n * FRAME_SEC

    # ---- 入力
    def feed(self, x: np.ndarray) -> list[Utterance]:
        out: list[Utterance] = []
        try:
            x = np.asarray(x)
            if x.size == 0:
                return out
            if x.dtype != np.int16:
                x = x.astype(np.int16)
            with self._lock:
                buf = np.concatenate([self._carry, x]) if self._carry.size else x
                n = len(buf) // FRAME
                for i in range(n):
                    self._frame(buf[i * FRAME:(i + 1) * FRAME], out)
                self._carry = buf[n * FRAME:].copy()
        except Exception:
            self.errors += 1
        self._emit(out)
        return out

    def flush(self) -> list[Utterance]:
        """いま途中の発話を、無音を待たずに確定する（確定操作・セッション終了で呼ぶ）。"""
        out: list[Utterance] = []
        try:
            with self._lock:
                if self._active:
                    self._finish(len(self._cur), forced=False, out=out)
        except Exception:
            self.errors += 1
        self._emit(out)
        return out

    def current_audio(self, max_silence_sec: float | None = None):
        """いま話している途中の発話（まだ区切っていない）の (開始秒, 終了秒, 音声のコピー)。無ければ None。
        仮認識（逐次認識）・波形の表示用。ロックの中でコピーするだけ。
        声のあるフレームが最短長（min_voiced_sec）に満たない塊（クリック音・小さな物音。このあと捨てられる）は返さない。
        max_silence_sec を渡すと、最後の声からそれ以上の無音が続いているときも返さない（無音の間は波形を出さない）。"""
        with self._lock:
            if not self._active or not self._cur:
                return None
            if sum(1 for f in self._cur if f.voiced) < self.min_voiced_frames:
                return None
            if not self._loud_enough(self._cur):
                return None
            if max_silence_sec is not None and self._sil * FRAME_SEC > float(max_silence_sec):
                return None
            audio = np.concatenate([f.data for f in self._cur])
            start = self._cur_start * FRAME_SEC
            return start, start + len(self._cur) * FRAME_SEC, audio

    def discard_pending(self) -> None:
        """途中の発話と直前の音を捨てる（保留の開始時）。位置・床・無音の数えは保つ。"""
        with self._lock:
            self._cur, self._active = [], False
            self._pre.clear()

    def drop_recent(self, samples_ago: int) -> bool:
        """samples_ago サンプル前（そのサンプルを含む 0.1 秒フレーム）から今までの音を、いま途中の発話から取り除く
        （舌打ち2回の合図の音を、文字起こしへ回さないため）。取り除いた残りに声が無ければ途中の発話ごと捨てる。位置・床は保つ。
        何か取り除いたら True。"""
        with self._lock:
            total = self._n * FRAME + len(self._carry)
            fi = max(0, (total - max(0, int(samples_ago))) // FRAME)       # 取り除き始めるフレームの絶対番号
            changed = False
            if self._active and self._cur:
                keep = max(0, fi - self._cur_start)
                if keep < len(self._cur):
                    old = self._cur
                    self._cur = old[:keep]
                    changed = True
                    if any(f.voiced for f in self._cur):
                        self._sil = 0
                        for f in reversed(self._cur):
                            if f.voiced:
                                break
                            self._sil += 1
                    else:
                        self._cur, self._active, self._sil = [], False, 0
                        self._pre.clear()
                        if self.preroll_frames:
                            for f in old[:keep][-self.preroll_frames:]:
                                self._pre.append(f)
            else:
                n_old = len(self._pre)
                keep_pre = [f for f in self._pre]          # 発話前の音（preroll）は声が無いので、新しい側を捨てるだけ
                if self._pre and fi < self._n:
                    drop = min(len(keep_pre), self._n - fi)
                    self._pre.clear()
                    for f in keep_pre[:len(keep_pre) - drop]:
                        self._pre.append(f)
                    changed = drop > 0 and n_old > 0
            return changed

    def reset(self) -> None:
        """すべて初期状態へ。位置の数えも 0 に戻る。"""
        with self._lock:
            self._carry = np.zeros(0, dtype=np.int16)
            self._rms_hist.clear()
            self._floor_db = -120.0
            self._n = 0
            self._pre.clear()
            self._cur, self._active, self._sil, self._cur_start = [], False, 0, 0

    # ---- 内部
    def _emit(self, items: list[Utterance]) -> None:
        cb = self.on_utterance
        if cb is None:
            return
        for u in items:
            try:
                cb(u)
            except Exception:
                self.errors += 1

    def _is_voiced(self, fr: np.ndarray, rms: float) -> bool:
        # silence.SilenceWatcher と同じ判定
        peak_db = to_dbfs(float(np.abs(fr.astype(np.int32)).max()))
        rms_db = to_dbfs(rms)
        if self._n < WARMUP_FRAMES:
            # 立ち上がりの最初の約1秒は、床（周囲の静けさ）がまだ分からない。床との差で判定すると、環境ノイズがすべて「声」に
            # なってしまい、無音なのに発話・波形ができる。この間は絶対値の基準（かなり大きい音）だけで判定する
            return rms_db >= self.abs_dbfs + 20.0
        return ((peak_db >= self.abs_dbfs and (rms_db - self._floor_db) >= 10.0)
                or rms_db >= self.abs_dbfs + 20.0)

    def _loud_enough(self, frames) -> bool:
        if self.loud_dbfs is None:
            return True
        return sum(1 for f in frames if f.loud) >= self.min_loud_frames

    def _frame(self, fr: np.ndarray, out: list[Utterance]) -> None:
        rms = float(np.sqrt(np.mean(fr.astype(np.float32) ** 2)))
        self._rms_hist.append(rms)
        self._n += 1
        if self._n % 10 == 1:
            self._floor_db = to_dbfs(float(np.percentile(list(self._rms_hist), 10)))
        voiced = self._is_voiced(fr, rms)
        # 音量バーに出ている基準（0.1秒の平均＝RMS）で判定する。バーの青い線より小さい音は処理対象外、と画面の見た目に合わせる
        loud = self.loud_dbfs is None or to_dbfs(rms) >= self.loud_dbfs
        f = _Frame(fr, rms, voiced, loud)
        idx = self._n - 1                       # このフレームの絶対番号
        if voiced:
            if not self._active:
                self._active = True
                self._cur = list(self._pre) if self.preroll_frames else []
                self._pre.clear()
                self._cur_start = idx - len(self._cur)
            self._cur.append(f)
            self._sil = 0
        else:
            self._sil += 1
            if self._active:
                self._cur.append(f)
                if self._sil >= self.gap_frames:
                    self._finish(len(self._cur) - max(0, self._sil - self.tail_frames), forced=False, out=out)
            elif self.preroll_frames:
                self._pre.append(f)
        if self._active and len(self._cur) >= self.max_frames:
            self._force_split(out)

    def _force_split(self, out: list[Utterance]) -> None:
        """最長に達した。末尾 1/4 のうちいちばん静かなフレームの直後で区切り、残りを次の発話の頭にする。"""
        n = len(self._cur)
        lo = max(self.min_voiced_frames, n - max(2, n // 4))
        cut = min(range(lo, n), key=lambda i: (self._cur[i].rms, -i)) + 1 if lo < n else n
        self._finish(cut, forced=True, out=out)

    def _finish(self, cut: int, forced: bool, out: list[Utterance]) -> None:
        """_cur[:cut] を発話として確定。_cur[cut:] は残す（強制の区切りのとき）。"""
        head, rest = self._cur[:cut], self._cur[cut:]
        start = self._cur_start
        voiced_n = sum(1 for f in head if f.voiced)
        if head and voiced_n >= self.min_voiced_frames and self._loud_enough(head):
            audio = np.concatenate([f.data for f in head])
            out.append(Utterance(audio, start * FRAME_SEC, (start + len(head)) * FRAME_SEC,
                                 voiced_n * FRAME_SEC, forced))
        self._cur_start = start + len(head)
        if rest and any(f.voiced for f in rest):
            self._cur = rest
        else:
            self._cur, self._active = [], False
            if rest and self.preroll_frames:     # 声のない残りは、次の発話の頭用に戻す
                self._pre.clear()
                for f in rest[-self.preroll_frames:]:
                    self._pre.append(f)
        if self._active and not self._cur:
            self._active = False
