# -*- coding: utf-8 -*-
"""Windows のタスクバー関連（ctypes のみ・COM 不使用）。"""
from __future__ import annotations

import sys

APP_USER_MODEL_ID = "kabeuchi.stt"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """このプロセスに専用の AppUserModelID を設定する（python のアイコンにまとまらないように）。

    QApplication や窓を作る**前**に呼ぶ。成功したら True。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        r = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
        return r == 0  # S_OK
    except Exception:
        return False


def get_app_user_model_id() -> str | None:
    """いまのプロセスの明示的な AppUserModelID（未設定なら None）。確認用。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        p = ctypes.c_void_p()
        r = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(p))
        if r != 0 or not p.value:
            return None
        s = ctypes.wstring_at(p.value)
        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(p.value))
        return s
    except Exception:
        return None
