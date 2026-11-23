# -*- coding: utf-8 -*-
"""清書キュー（GUI/Qt/COM 非依存）。

停止した録音を順番に清書する。清書は専用の低優先度スレッドで 1 件ずつ。
録音スレッドとは完全に独立なので、清書中でも次の録音を始められる。
コールバック on_event(job) は**ワーカースレッドから**呼ばれる（Qt からはシグナルで受けること）。
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PENDING, RUNNING, DONE, ERROR, CANCELLED = "pending", "running", "done", "error", "cancelled"


@dataclass
class Job:
    id: int
    wav: Path
    model: str | None = None          # None=設定の final_model
    history_id: int | None = None     # 清書やり直しの対象（None=新規）
    timeline: Any = None              # 結果を反映するタイムライン（あれば）
    compress: bool = False            # 清書の完了後に、WAV を設定どおり Opus へ圧縮する（録音直後のジョブだけ True）
    compress_only: bool = False       # 清書せず圧縮（と無音カット）だけ行う軽い仕事。ゲートが閉じていても進める（履歴は登録済み）
    file_tag: str = ""                # txt/md のファイル名に足す印（別モデルで清書し直したとき上書きしない）
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def title(self) -> str:
        return Path(self.wav).stem


def _set_thread_priority(low: bool, native_id: int | None = None) -> None:
    """スレッドの優先度を切り替える（Windows のみ）。low=True: IDLE（対話の認識スレッド＝LOWEST より低い）、False: BELOW_NORMAL。
    native_id が無ければ呼び出しスレッド自身。"""
    try:
        import sys
        if sys.platform != "win32":
            return
        import ctypes
        k32 = ctypes.windll.kernel32
        level = -15 if low else -1
        if native_id is None:
            k32.SetThreadPriority(k32.GetCurrentThread(), level)
        else:
            h = k32.OpenThread(0x0020, False, native_id)     # THREAD_SET_INFORMATION
            if h:
                try:
                    k32.SetThreadPriority(h, level)
                finally:
                    k32.CloseHandle(h)
    except Exception:
        pass


class FinalizeQueue:
    """run_job(job) が実際の清書。job.progress を更新し、結果を返す（例外=ERROR）。"""

    def __init__(self, run_job: Callable[[Job], Any],
                 on_event: Callable[[Job], None] | None = None,
                 set_priority: Callable[[bool], None] | None = None):
        """set_priority(low): ワーカースレッド自身の優先度を切り替える（テスト用に差し替え可。low=True は最低に近い優先度）。"""
        self._run_job = run_job
        self.on_event = on_event
        self._gate: Callable[[], bool] | None = None
        self._gate_open = True
        self._set_priority = set_priority or _set_thread_priority
        self._native_id: int | None = None
        self._cv = threading.Condition()
        self._pending: deque[Job] = deque()
        self._jobs: list[Job] = []
        self._ids = itertools.count(1)
        self._current: Job | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="kabeuchi-final-queue", daemon=True)
        self._thread.start()

    # ---- 操作
    def submit(self, wav: Path, model: str | None = None, history_id: int | None = None,
               timeline: Any = None, compress: bool = False, file_tag: str = "", compress_only: bool = False) -> Job:
        job = Job(next(self._ids), Path(wav), model, history_id, timeline, compress=compress, file_tag=file_tag,
                  compress_only=compress_only)
        with self._cv:
            if self._closed:
                raise RuntimeError("キューは終了しています")
            self._jobs.append(job)
            self._pending.append(job)
            self._cv.notify()
        self._emit(job)
        return job

    def cancel(self, job: Job) -> None:
        """待機中なら取り消し、実行中なら中断を要求する（それまでの分は保存される）。"""
        job.cancel.set()
        with self._cv:
            if job in self._pending:
                self._pending.remove(job)
                job.status = CANCELLED
                self._emit(job)

    def close(self, cancel_running: bool = True) -> None:
        with self._cv:
            self._closed = True
            for j in list(self._pending):
                j.status = CANCELLED
            self._pending.clear()
            if cancel_running and self._current:
                self._current.cancel.set()
            self._cv.notify_all()

    # ---- ゲート（セッション中は、待機中の清書を始めない）
    def set_gate(self, gate: Callable[[], bool] | None) -> None:
        """gate() が False の間は、待機中の（重い）清書を開始しない。圧縮だけの軽い仕事は通す。実行中の仕事は止めず、
        ワーカースレッドの優先度を最低に近くして、喋っている間の認識を遅くしない。gate=None で常に開く。"""
        self._gate = gate
        self.poke()

    def _gate_is_open(self) -> bool:
        g = self._gate
        if g is None:
            return True
        try:
            return bool(g())
        except Exception:
            return True

    @property
    def gated(self) -> bool:
        """ゲートが閉じていて、そのせいで待っている仕事があるか（「清書待ち」の表示用）。"""
        with self._cv:
            return (not self._gate_is_open()) and any(not j.compress_only for j in self._pending)

    def poke(self) -> None:
        """ゲートの状態を再評価する（セッションの開始・終了で呼ぶ。定期的に呼んでよい）。開いたら待機中の仕事を始める。"""
        open_now = self._gate_is_open()
        with self._cv:
            changed = open_now != self._gate_open
            self._gate_open = open_now
            if changed:
                self._cv.notify_all()
        if changed:
            self._apply_priority()

    def _apply_priority(self) -> None:
        try:
            if self._set_priority is _set_thread_priority:      # 既定: ワーカースレッドへ他のスレッドから設定する
                _set_thread_priority(not self._gate_open, self._native_id)
            else:
                self._set_priority(not self._gate_open)
        except Exception:
            pass

    def _next_job(self) -> Job | None:
        """開始してよい仕事（ゲートが開いている間は先頭、閉じている間は軽い仕事だけ）。cv を持って呼ぶ。"""
        if self._gate_is_open():
            return self._pending.popleft() if self._pending else None
        for j in self._pending:
            if j.compress_only:
                self._pending.remove(j)
                return j
        return None

    # ---- 状態
    @property
    def jobs(self) -> list[Job]:
        with self._cv:
            return list(self._jobs)

    @property
    def waiting(self) -> int:
        with self._cv:
            return len(self._pending)

    @property
    def current(self) -> Job | None:
        return self._current

    @property
    def busy(self) -> bool:
        with self._cv:
            return self._current is not None or bool(self._pending)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.busy:
                return True
            time.sleep(0.02)
        return False

    # ---- 内部
    def _emit(self, job: Job) -> None:
        if self.on_event:
            try:
                self.on_event(job)
            except Exception:
                pass

    def _loop(self) -> None:
        _set_thread_priority(False)
        self._native_id = threading.get_native_id()
        while True:
            with self._cv:
                job = None
                while True:
                    if self._closed and not self._pending:
                        return
                    job = self._next_job() if self._pending else None
                    if job is not None:
                        break
                    self._cv.wait(0.5)       # ゲートの再評価（poke）が来なくても、0.5 秒ごとに見直す
                self._current = job
            job.status = RUNNING
            self._emit(job)
            try:
                job.result = self._run_job(job)
                job.progress = 1.0
                job.status = CANCELLED if job.cancel.is_set() and job.result is None else DONE
            except Exception as e:  # 1 件の失敗でキューを止めない
                job.status = ERROR
                job.error = str(e)
            finally:
                with self._cv:
                    self._current = None
            self._emit(job)
