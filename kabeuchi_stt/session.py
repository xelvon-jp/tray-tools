# -*- coding: utf-8 -*-
"""録音セッション（GUI/Qt/COM 非依存）。

Recorder・LiveTranscriber・タイムライン・マイク監視・無音監視をひとまとめにする。
録音スレッドが呼ぶ on_audio（AudioTap）は「各部へ軽く渡すだけ」で、どれかが例外を出しても
録音は続ける。認識（LiveTranscriber）は別スレッド、清書は FinalizeQueue（別スレッド）。

待機中モニター（フェーズ1.5）: 窓が見えている間だけ AudioInput（入力ストリーム）を開き、音量メーターと
音量診断・「話しているのに録音していない」検出を動かす。音声は保存も認識もしない。録音開始時は
同じ AudioInput を Recorder へ引き継ぐ（デバイスを開き直さない）。
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from audio_input import AudioInput
from idle_speech import IdleSpeechWatcher, prompt_kind, should_prompt
from mic_check import MicMonitor, diagnose, measure
from recorder import SAMPLE_RATE, Recorder
from settings import resolve_dir
from silence import SilenceWatcher
from timeline import Timeline

MIC_BUSY_MESSAGE = "マイクを使用中のため確認できません"
MONITOR_RETRY_SEC = 15.0  # 開けなかったとき、次に試すまでの間隔


class AudioTap:
    """Recorder.on_audio へ渡す。writer スレッドから呼ばれる。"""

    def __init__(self, *parts: Callable[[np.ndarray], None]):
        self.parts = [p for p in parts if p is not None]
        self.errors = 0

    def __call__(self, x: np.ndarray) -> None:
        for p in list(self.parts):   # 録音中の逐次認識の切替（別スレッド）で parts が変わっても安全に回す
            try:
                p(x)
            except Exception:
                self.errors += 1  # 録音は止めない


def mic_thresholds(settings: dict) -> dict:
    return {k: settings[k] for k in ("mic_low_peak_dbfs", "mic_clip_ratio",
                                     "mic_noise_floor_dbfs", "mic_dead_peak_dbfs") if k in settings}


class Session:
    def __init__(self, settings: dict, replacer, model_cache=None, sd=None,
                 on_status: Callable[[str], None] = lambda m: None, live_factory=None,
                 clock: Callable[[], float] = time.monotonic):
        self.s = settings
        self.replacer = replacer
        self.cache = model_cache
        self.sd = sd
        self.on_status = on_status
        self.live_factory = live_factory
        self.clock = clock
        self.timeline = Timeline(float(settings.get("row_sec", 30)))
        th, win = mic_thresholds(settings), float(settings.get("mic_window_sec", 8.0))
        self.monitor = MicMonitor(th, win)           # 録音中の常時監視
        self.idle_monitor = MicMonitor(th, win)      # 待機中（録音していない間）の監視
        self.idle_speech = IdleSpeechWatcher()
        self.silence = SilenceWatcher(float(settings.get("silence_stop_sec", 180)),
                                      float(settings.get("silence_stop_peak_dbfs", -50.0)))
        self.hub = AudioInput(settings.get("input_device"), sd, idle_consumer=self._idle_feed, on_error=on_status)
        self._hub_lock = threading.RLock()
        self._monitor_want = False
        self._worker: threading.Thread | None = None
        self._retry_at = 0.0
        self.monitor_error = ""         # 待機中モニターを開けなかったときの表示（空=問題なし）
        self.monitor_error_detail = ""
        self._last_prompt_at: float | None = None
        self.rec: Recorder | None = None
        self._tap: AudioTap | None = None
        self.live = None
        self.wav_path: Path | None = None
        self.started_at: dt.datetime | None = None
        self._pause_wall: float | None = None   # 一時停止を押した時刻（実時間）
        self._pause_lengths: list[float] = []
        self._pause_ids: list[int] = []
        self.paused_total = 0.0

    # ---- 状態
    @property
    def is_recording(self) -> bool:
        return self.rec is not None and self.rec.is_recording

    @property
    def is_paused(self) -> bool:
        return self.rec is not None and self.rec.is_paused

    @property
    def paused_sec(self) -> float:
        """いまの一時停止が始まってからの実時間（一時停止中でなければ 0）。"""
        return (self.clock() - self._pause_wall) if (self.is_paused and self._pause_wall is not None) else 0.0

    @property
    def monitor_active(self) -> bool:
        """待機中モニター（または録音）で入力ストリームが開いているか。"""
        return self.hub.is_open

    @property
    def level(self) -> float:
        return self.hub.level

    def speaking_now(self) -> bool:
        """いま話し声らしい音が入っているか（待機中・一時停止中の経路。録音中は録音側が受けるので False）。"""
        return self.idle_speech.speaking_now()

    def mic_issues(self):
        """いまのマイク診断（録音中は録音の監視、それ以外は待機中の監視）。"""
        if self.is_recording and not self.is_paused:
            return self.monitor.issues()
        return self.idle_monitor.issues()

    def new_wav_path(self) -> Path:
        out_dir = resolve_dir(self.s.get("out_dir", "out"))
        return out_dir / f"kabeuchi_{dt.datetime.now():%Y%m%d_%H%M%S}.wav"

    def _on_segment(self, a: float, b: float, text: str) -> None:
        self.timeline.add_draft(a, b, self.replacer.apply(text))

    # ---- 待機中モニター
    def _idle_feed(self, x: np.ndarray) -> None:  # hub の待機中スレッドから
        self.idle_monitor.feed(x)
        self.idle_speech.feed(x)

    def _reset_idle(self) -> None:
        self.idle_monitor.reset()
        self.idle_speech.reset()

    def request_monitor(self, want: bool) -> None:
        """UI から呼ぶ。実際の開閉は別スレッド（開くのに時間がかかっても画面を止めない）。"""
        self._monitor_want = bool(want)
        if self._monitor_pending() and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._monitor_loop, name="kabeuchi-monitor", daemon=True)
            self._worker.start()

    def apply_monitor_now(self, want: bool) -> None:
        """request_monitor の同期版（テスト用）。"""
        self._monitor_want = bool(want)
        self._monitor_loop()

    def _monitor_pending(self) -> bool:
        if self.is_recording:
            return False
        if self._monitor_want:
            return (not self.hub.is_open) and self.clock() >= self._retry_at
        return self.hub.is_open

    def _monitor_loop(self) -> None:
        for _ in range(4):
            with self._hub_lock:
                if not self._monitor_pending():
                    return
                if self._monitor_want:
                    self.hub.device_setting = self.s.get("input_device")
                    try:
                        self.hub.open()
                        self.monitor_error = self.monitor_error_detail = ""
                        self._reset_idle()
                    except Exception as e:  # 他のアプリが排他で使っている等。エラーにせず表示だけ
                        self.monitor_error = MIC_BUSY_MESSAGE
                        self.monitor_error_detail = f"{type(e).__name__}: {e}"
                        self._retry_at = self.clock() + MONITOR_RETRY_SEC
                else:
                    self.hub.close()
                    self._reset_idle()
                    self.monitor_error = ""

    def input_device_changed(self) -> None:
        """設定でマイクが変わった。待機中に開いているストリームは閉じる（次の tick で開き直す）。"""
        with self._hub_lock:
            if not self.is_recording and self.hub.is_open:
                self.hub.close()
                self._reset_idle()
            self._retry_at = 0.0

    def poll_idle_prompt(self, window_active: bool) -> str | None:
        """話し声の指摘を出すべきか。出すなら種類（"idle"=録音していません / "paused"=一時停止中です）を返す。
        出すと決めたらクールダウンを開始する（2 種類で共通）。録音中（一時停止でない）・窓が隠れている・
        設定 OFF のときは None。"""
        kind = prompt_kind(self.is_recording, self.is_paused)
        if kind is None:
            return None
        now = self.clock()
        ok = should_prompt(
            self.idle_speech.run_sec, now, self._last_prompt_at,
            enabled=bool(self.s.get("idle_prompt_enabled", True)),
            min_speech_sec=float(self.s.get("idle_prompt_speech_sec", 1.5)),
            cooldown_sec=float(self.s.get("idle_prompt_cooldown_sec", 60)),
            window_active=bool(window_active) and self.hub.is_open,
            recording=False)
        if ok:
            self._last_prompt_at = now
            self.idle_speech.reset_run()
            return kind
        return None

    # ---- 録音
    def start(self, wav_path: Path | None = None, live_enabled: bool | None = None,
              extra_parts: tuple = ()) -> Path:
        with self._hub_lock:
            if self.is_recording:
                raise RuntimeError("すでに録音中です")
            # 前の録音の清書がまだ裏で走っていても、その結果が新しい録音の画面に混ざらないよう、
            # 録音ごとに新しい Timeline を作る（清書ジョブは古い Timeline を持ち続ける）
            self.timeline = Timeline(float(self.s.get("row_sec", 30)))
            tl = self.timeline
            self.monitor.reset()
            self.silence.reset()
            self.live = None
            self._pause_wall, self._pause_lengths, self._pause_ids, self.paused_total = None, [], [], 0.0
            parts = [tl.feed, self.monitor.feed, self.silence.feed]
            parts.extend(p for p in extra_parts if p is not None)   # 対話モードの区切り器など（録音の頭から受ける）
            self._tap_parts = parts
            if live_enabled is None:
                live_enabled = bool(self.s.get("live_enabled", True))
            if live_enabled:
                if self.live_factory is not None:
                    self.live = self.live_factory(self._on_segment, self.on_status)
                else:
                    from transcriber import LiveTranscriber
                    ctx = None
                    if self.cache is not None:
                        name = self.s.get("live_model", "base")
                        th = int(self.s.get("live_cpu_threads", 2))
                        ctx = lambda: self.cache.use(name, th)  # noqa: E731
                    self.live = LiveTranscriber(self.s, on_status=self.on_status,
                                                on_segment=self._on_segment, model_context=ctx)
                parts.append(self.live.feed)
            self._tap = AudioTap(*parts)
            self.hub.device_setting = self.s.get("input_device")
            self.rec = Recorder(on_audio=self._tap, on_error=self.on_status, sd=self.sd,
                                source=self.hub, on_marker=lambda kind, sec, tl=tl: self._marker(tl, kind, sec))
            wav = Path(wav_path) if wav_path else self.new_wav_path()
            try:
                self.rec.start(wav)  # 開いている待機中のストリームを引き継ぐ。失敗したら例外（live はまだ始めていない）
            except Exception:
                if not self._monitor_want:
                    self.hub.close()
                raise
            self.idle_speech.reset_run()
            if self.live is not None:
                self.live.start()
            self.wav_path = wav
            self.started_at = dt.datetime.now()
            tl.set_recording(True)
            return wav

    def _make_live(self, offset: float):
        """逐次認識を作る。offset は録音のどこから始めるか（秒）。区間の時刻に足して WAV 上の時刻に合わせる。"""
        def on_seg(a: float, b: float, text: str) -> None:
            self._on_segment(a + offset, b + offset, text)
        if self.live_factory is not None:
            return self.live_factory(on_seg, self.on_status)
        from transcriber import LiveTranscriber
        ctx = None
        if self.cache is not None:
            name = self.s.get("live_model", "base")
            th = int(self.s.get("live_cpu_threads", 2))
            ctx = lambda: self.cache.use(name, th)  # noqa: E731
        return LiveTranscriber(self.s, on_status=self.on_status, on_segment=on_seg, model_context=ctx)

    def set_live(self, enabled: bool) -> None:
        """録音中に逐次認識を切り替える。録音（WAV・writer）には一切触れない。
        ON: その時点からの音声で逐次認識を始める（以前の区間は仮認識なしのまま。清書で拾われる）。
        OFF: 音声を渡すのをやめ、認識スレッドは別スレッドで止める（画面を待たせない）。"""
        enabled = bool(enabled)
        if not self.is_recording:
            return
        with self._hub_lock:
            if enabled and self.live is None:
                live = self._make_live(self.elapsed)
                live.start()
                self.live = live
                self._tap.parts.append(live.feed)
            elif not enabled and self.live is not None:
                live, self.live = self.live, None
                try:
                    self._tap.parts.remove(live.feed)
                except ValueError:
                    pass
                threading.Thread(target=lambda: live.stop(wait=2), name="kabeuchi-live-stop", daemon=True).start()

    def _marker(self, tl: Timeline, kind: str, sec: float) -> None:  # writer スレッドから（音声との順序は保たれる）
        if kind == "pause":
            self._pause_ids.append(tl.add_pause(sec))
        elif kind == "resume" and self._pause_ids and self._pause_lengths:
            tl.set_pause_length(self._pause_ids[-1], self._pause_lengths.pop(0))

    def pause(self) -> bool:
        if self.rec is None or not self.rec.pause():
            return False
        self._pause_wall = self.clock()
        self.idle_speech.reset_run()   # 一時停止の直前の発話を「停止中に話している」と数えない
        return True

    def resume(self) -> bool:
        if self.rec is None or not self.is_paused:
            return False
        length = self.clock() - (self._pause_wall if self._pause_wall is not None else self.clock())
        self.paused_total += length
        self._pause_lengths.append(length)
        self._pause_wall = None
        self.idle_speech.reset_run()
        return self.rec.resume()

    def toggle_pause(self) -> bool | None:
        """一時停止中なら再開、録音中なら一時停止。戻り値は操作後に一時停止中か（録音していなければ None）。"""
        if not self.is_recording:
            return None
        if self.is_paused:
            self.resume()
        else:
            self.pause()
        return self.is_paused

    def stop(self) -> Path | None:
        """録音を止める。WAV のパスを返す。逐次認識は解放する。待機中モニターが不要ならストリームも閉じる。"""
        if self.rec is None:
            return None
        was_paused, wall = self.is_paused, self._pause_wall
        tl = self.timeline
        wav = self.rec.stop()
        if was_paused and wall is not None and tl.pause_count:
            length = self.clock() - wall
            self.paused_total += length
            tl.set_pause_length(tl.pause_count - 1, length)
        self._pause_wall = None
        tl.set_recording(False)
        if self.live is not None:
            try:
                self.live.stop(wait=2)
            except Exception:
                pass
            self.live = None
        with self._hub_lock:
            if not self._monitor_want:
                self.hub.close()
            self._reset_idle()
        return wav

    def close(self) -> None:
        """終了時: 録音していれば止め、入力ストリームを閉じる。"""
        try:
            if self.is_recording:
                self.stop()
        finally:
            self._monitor_want = False
            self.hub.close()

    @property
    def elapsed(self) -> float:
        return self.rec.elapsed if self.rec else 0.0
