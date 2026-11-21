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
    status: str = PENDING
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def title(self) -> str:
        return Path(self.wav).stem


class FinalizeQueue:
    """run_job(job) が実際の清書。job.progress を更新し、結果を返す（例外=ERROR）。"""

    def __init__(self, run_job: Callable[[Job], Any],
                 on_event: Callable[[Job], None] | None = None):
        self._run_job = run_job
        self.on_event = on_event
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
               timeline: Any = None) -> Job:
        job = Job(next(self._ids), Path(wav), model, history_id, timeline)
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
        try:
            import sys
            if sys.platform == "win32":
                import ctypes
                k32 = ctypes.windll.kernel32
                k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # BELOW_NORMAL
        except Exception:
            pass
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if self._closed and not self._pending:
                    return
                job = self._pending.popleft()
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
