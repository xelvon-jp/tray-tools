"""ファイルを Windows のごみ箱へ移す（完全には消さない）。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path

FO_DELETE = 3
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040  # ごみ箱へ
FOF_NOERRORUI = 0x0400
DRIVE_FIXED = 3


class TrashError(OSError):
    pass


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def has_recycle_bin(path: Path) -> bool:
    """ごみ箱があるのは固定ディスクだけ。ネットワークドライブなどで FOF_ALLOWUNDO を使うと、
    確認なしで完全に消えてしまうので、その場合は削除自体をしない。"""
    root = Path(path).resolve().anchor
    return ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED


def to_recycle_bin(paths: list[Path]) -> None:
    paths = [Path(p).resolve() for p in paths if Path(p).exists()]
    if not paths:
        return
    for p in paths:
        if not has_recycle_bin(p):
            raise TrashError(f"{p.anchor} にはごみ箱が無いため削除しません")
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = "\0".join(str(p) for p in paths) + "\0"  # 末尾はヌル2つ（ctypes が1つ足す）
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted or any(p.exists() for p in paths):
        raise TrashError(f"ごみ箱に移せませんでした（コード {rc}）")
