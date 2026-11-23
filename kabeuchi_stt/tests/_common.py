# -*- coding: utf-8 -*-
"""テスト共通: プロジェクトを import パスへ。本人のデータ置き場は一時フォルダへ向ける。"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# テストが本人の %LOCALAPPDATA%\kabeuchi_stt を汚さない
_TMP = tempfile.mkdtemp(prefix="kabeuchi_test_data_")
os.environ["KABEUCHI_DATA_DIR"] = _TMP

import faulthandler  # noqa: E402

# ハング対策: 一定時間で全スレッドのスタックを stderr に出して強制終了（無限待ちにしない）
faulthandler.dump_traceback_later(int(os.environ.get("KABEUCHI_TEST_TIMEOUT", "240")), exit=True)

import numpy as np  # noqa: E402


class _FakeClipboard:
    """テスト中の QGuiApplication.clipboard()。本物のクリップボード（利用者が使っている）を書き換えない。"""

    def __init__(self):
        self._t = ""

    def text(self, *a, **k):
        return self._t

    def setText(self, t, *a, **k):
        self._t = str(t)

    def clear(self, *a, **k):
        self._t = ""


def _isolate_clipboard():
    try:
        from PySide6.QtGui import QGuiApplication
        fake = _FakeClipboard()
        QGuiApplication.clipboard = staticmethod(lambda: fake)
    except Exception:
        pass


_isolate_clipboard()

SR = 16000


def dbfs_amp(db: float) -> float:
    return 32768.0 * (10 ** (db / 20.0))


def sine(sec: float, db: float, freq: float = 220.0) -> np.ndarray:
    """ピークが db dBFS の正弦波（int16）。"""
    t = np.arange(int(sec * SR)) / SR
    return (np.sin(2 * np.pi * freq * t) * min(32767.0, dbfs_amp(db))).astype(np.int16)


def speechlike(sec: float, db: float = -12, floor_db: float = -70) -> np.ndarray:
    """0.5秒の音＋0.5秒の静かな間を繰り返す（発話のつもり）。"""
    parts, n = [], int(sec)
    for i in range(n):
        parts.append(sine(0.5, db, 180 + 20 * (i % 5)))
        parts.append(noise(0.5, floor_db, i))
    return np.concatenate(parts)


def noise(sec: float, db_rms: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(sec * SR)) * dbfs_amp(db_rms)
    return np.clip(x, -32768, 32767).astype(np.int16)
