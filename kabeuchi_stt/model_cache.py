# -*- coding: utf-8 -*-
"""モデルの常駐キャッシュ（GUI/Qt/COM 非依存）。

常駐中はモデルをメモリに保持して初回待ちをなくし、最後の使用から idle_sec 経ったら解放する。
使用中（acquire 〜 release の間）は解放しない。loader は差し替え可能（テスト用）。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Callable


class ModelCache:
    def __init__(self, loader: Callable[[str, int], Any], idle_sec: float = 600.0,
                 clock: Callable[[], float] = time.monotonic):
        self._loader = loader
        self.idle_sec = float(idle_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._models: dict[tuple[str, int], Any] = {}
        self._users: dict[tuple[str, int], int] = {}
        self._last: dict[tuple[str, int], float] = {}
        self.loads = 0

    @contextmanager
    def use(self, name: str, cpu_threads: int = 0):
        key = (name, int(cpu_threads))
        with self._lock:
            self._users[key] = self._users.get(key, 0) + 1
            model = self._models.get(key)
        try:
            if model is None:
                with self._load_lock:  # 同じモデルの二重ロードを避ける
                    with self._lock:
                        model = self._models.get(key)
                    if model is None:
                        model = self._loader(name, int(cpu_threads))
                        with self._lock:
                            self._models[key] = model
                            self.loads += 1
            yield model
        finally:
            with self._lock:
                self._users[key] -= 1
                self._last[key] = self._clock()

    def release_idle(self) -> list[str]:
        """アイドル超過かつ未使用のモデルを解放。解放した名前を返す。"""
        out = []
        now = self._clock()
        with self._lock:
            for key in list(self._models):
                if self._users.get(key, 0) == 0 and now - self._last.get(key, now) >= self.idle_sec:
                    del self._models[key]
                    out.append(key[0])
        return out

    def release_all(self) -> None:
        with self._lock:
            for key in list(self._models):
                if self._users.get(key, 0) == 0:
                    del self._models[key]

    @property
    def loaded(self) -> list[str]:
        with self._lock:
            return [k[0] for k in self._models]
