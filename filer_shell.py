# filer_shell.py
# 簡易ファイラ(filer.py)が使う Win32 の呼び出し。ctypes だけで書き、COM は使わない。
#
# - ファイルのコピー/移動/削除 … SHFileOperationW。COM(IFileOperation)ではないので、
#   CLAUDE.md の「COM ポインタの寿命で落ちる」類の罠を持ち込まない。上書きの確認や
#   進捗の窓、ごみ箱・元に戻す(FOF_ALLOWUNDO)は Windows 標準のものがそのまま出る。
# - デスクトップのパス … SHGetKnownFolderPath(返ってきた文字列は CoTaskMemFree で返す)。
#   OneDrive にデスクトップを移している PC では %USERPROFILE%\Desktop ではないため。
# - ドライブ一覧 … GetLogicalDrives / GetDriveTypeW / GetVolumeInformationW。
#
# ctypes は argtypes / restype を必ず指定する(64bit でハンドルやポインタが int に
# 切り詰められて落ちる。CLAUDE.md)。
import ctypes
import os
import string
import threading
import time
from ctypes import wintypes

import filer_core

# ---------------------------------------------------------------
# SHFileOperationW
# ---------------------------------------------------------------
FO_MOVE = 0x0001
FO_COPY = 0x0002
FO_DELETE = 0x0003

FOF_SILENT = 0x0004
FOF_RENAMEONCOLLISION = 0x0008
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMMKDIR = 0x0200
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        # pFrom/pTo は NUL 区切り・末尾 NUL 2つの文字列。c_wchar_p だと最初の NUL で
        # 切れたものとして扱われかねないので、自前で確保したバッファを指す void* にする。
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ole32 = ctypes.WinDLL("ole32")

_SHFileOperationW = _shell32.SHFileOperationW
_SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
_SHFileOperationW.restype = ctypes.c_int


def _wide_buffer(text: str):
    """埋め込み NUL ごと UTF-16 のバッファへ写す(create_unicode_buffer に文字列を
    渡すと、作り方によっては最初の NUL で切れるので、長さを決めてから中身を入れる)。"""
    buf = ctypes.create_unicode_buffer(len(text) + 1)
    for index, ch in enumerate(text):
        buf[index] = ch
    return buf


def file_operation(func: int, sources, dest: str = None, hwnd: int = 0, flags: int = 0):
    """SHFileOperationW を呼ぶ。(戻り値, ユーザーが中断したか) を返す。

    戻り値 0 が成功。0 以外は SHFileOperation 固有のエラーコード(DE_*)のことが多く、
    GetLastError とは別物なので、呼ぶ側では数値をそのまま見せる。
    sources は絶対パスの並び(相対パスは SHFileOperation がカレントで解決してしまう)。"""
    sources = [os.path.abspath(p) for p in sources]
    if not sources:
        return 0, False
    src_buf = _wide_buffer(filer_core.double_null_join(sources))
    dst_buf = _wide_buffer(filer_core.double_null_join([os.path.abspath(dest)])) if dest else None
    op = SHFILEOPSTRUCTW()
    op.hwnd = wintypes.HWND(hwnd or 0)
    op.wFunc = func
    op.pFrom = ctypes.cast(src_buf, ctypes.c_void_p)
    op.pTo = ctypes.cast(dst_buf, ctypes.c_void_p) if dst_buf is not None else None
    op.fFlags = flags
    result = _SHFileOperationW(ctypes.byref(op))
    return result, bool(op.fAnyOperationsAborted)


def copy_files(sources, dest: str, hwnd: int = 0, extra_flags: int = 0):
    """コピー。上書きの確認は Windows 標準のダイアログに任せる(FOF_NOCONFIRMATION を付けない)。"""
    return file_operation(FO_COPY, sources, dest, hwnd,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMMKDIR | extra_flags)


def move_files(sources, dest: str, hwnd: int = 0, extra_flags: int = 0):
    return file_operation(FO_MOVE, sources, dest, hwnd,
                          FOF_ALLOWUNDO | FOF_NOCONFIRMMKDIR | extra_flags)


def delete_files(sources, hwnd: int = 0, permanent: bool = False, extra_flags: int = 0):
    """削除。確認はファイラ側で済ませているので、Windows 側の確認は出さない
    (FOF_NOCONFIRMATION。二度聞かれると、あふｗの指の流れが止まる)。

    ごみ箱へ送る(permanent=False)とき、ごみ箱が使えない場所(ネットワーク等)では
    Windows が黙って完全削除に切り替えうる。FOF_WANTNUKEWARNING でそのときだけ警告を出させる。"""
    flags = FOF_NOCONFIRMATION | extra_flags
    if not permanent:
        flags |= FOF_ALLOWUNDO | FOF_WANTNUKEWARNING
    return file_operation(FO_DELETE, sources, None, hwnd, flags)


# ---------------------------------------------------------------
# 既知フォルダ(デスクトップ)
# ---------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(text: str) -> GUID:
    import uuid

    raw = uuid.UUID(text).bytes_le
    return GUID.from_buffer_copy(raw)


FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

_SHGetKnownFolderPath = _shell32.SHGetKnownFolderPath
_SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
_SHGetKnownFolderPath.restype = ctypes.c_long  # HRESULT

_CoTaskMemFree = _ole32.CoTaskMemFree
_CoTaskMemFree.argtypes = [ctypes.c_void_p]
_CoTaskMemFree.restype = None


def known_folder(folder_id: str = FOLDERID_DESKTOP):
    """既知フォルダのパス。取れなければ None。"""
    ptr = ctypes.c_void_p()
    guid = _guid(folder_id)
    hr = _SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(ptr))
    try:
        if hr != 0 or not ptr.value:
            return None
        return ctypes.wstring_at(ptr.value)
    finally:
        if ptr.value:
            _CoTaskMemFree(ptr)


def desktop_path() -> str:
    return known_folder(FOLDERID_DESKTOP) or os.path.join(os.path.expanduser("~"), "Desktop")


# ---------------------------------------------------------------
# ドライブ
# ---------------------------------------------------------------
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

_DRIVE_KIND = {
    DRIVE_REMOVABLE: "リムーバブル",
    DRIVE_FIXED: "ローカル",
    DRIVE_REMOTE: "ネットワーク",
    DRIVE_CDROM: "CD/DVD",
    DRIVE_RAMDISK: "RAM",
}

_GetLogicalDrives = _kernel32.GetLogicalDrives
_GetLogicalDrives.argtypes = []
_GetLogicalDrives.restype = wintypes.DWORD

_GetDriveTypeW = _kernel32.GetDriveTypeW
_GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
_GetDriveTypeW.restype = wintypes.UINT

_GetVolumeInformationW = _kernel32.GetVolumeInformationW
_GetVolumeInformationW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
_GetVolumeInformationW.restype = wintypes.BOOL

# 「ディスクを挿入してください」の窓を出させない。媒体の無いドライブのラベルを
# 読みにいったときに、ファイラの操作がシステムのダイアログで止まるのを防ぐ。
SEM_FAILCRITICALERRORS = 0x0001
_SetThreadErrorMode = _kernel32.SetThreadErrorMode
_SetThreadErrorMode.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_SetThreadErrorMode.restype = wintypes.BOOL


def drive_type(root: str) -> int:
    return int(_GetDriveTypeW(root))


def _volume_label(root: str):
    old = wintypes.DWORD()
    _SetThreadErrorMode(SEM_FAILCRITICALERRORS, ctypes.byref(old))
    buf = ctypes.create_unicode_buffer(261)
    ok = _GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
    return buf.value if ok else None


_GetDiskFreeSpaceExW = _kernel32.GetDiskFreeSpaceExW
_GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulonglong),
                                 ctypes.POINTER(ctypes.c_ulonglong),
                                 ctypes.POINTER(ctypes.c_ulonglong)]
_GetDiskFreeSpaceExW.restype = wintypes.BOOL


def disk_space(root: str):
    """(総容量, 空き容量) をバイトで。読めなければ None。UNC の根(\\\\server\\share\\)も可。

    ネットワーク越しでは止まりうるので、UI スレッドからは呼ばないこと。空きは
    「このユーザーが使える空き」(クォータを考えた値。エクスプローラと同じ)。"""
    free_to_caller = ctypes.c_ulonglong()
    total = ctypes.c_ulonglong()
    free = ctypes.c_ulonglong()
    old = wintypes.DWORD()
    _SetThreadErrorMode(SEM_FAILCRITICALERRORS, ctypes.byref(old))
    if not _GetDiskFreeSpaceExW(root, ctypes.byref(free_to_caller), ctypes.byref(total),
                                ctypes.byref(free)):
        return None
    return int(total.value), int(free_to_caller.value)


def drive_label(root: str) -> str:
    """窓の2行目に出すドライブの名前。ボリュームラベル、無ければ種別(ローカル等)。
    UNC なら "server\\share"。止まりうるので UI スレッドからは呼ばないこと。"""
    if root.startswith("\\\\"):
        return root.strip("\\")
    try:
        label = _volume_label(root)
    except OSError:
        label = None
    if label:
        return label
    return _DRIVE_KIND.get(drive_type(root), "")


def list_drives(label_timeout: float = 1.0) -> list:
    """[(ルート "C:\\", 表示名), ...]。表示名は「C:  ラベル  [種別]」。

    ラベルの読み取りは、切断されたネットワークドライブや媒体の無いドライブで止まりうる。
    1本ずつ別スレッドで読み、全体で label_timeout 秒待っても返らなかったものは
    ラベル無しで出す(ドライブ一覧が出ないより、ラベルが欠ける方がまし)。
    ネットワークドライブは最初から読みに行かない(止まりやすい上、名前は種別で足りる)。"""
    mask = int(_GetLogicalDrives())
    roots = [f"{letter}:\\" for i, letter in enumerate(string.ascii_uppercase) if mask & (1 << i)]
    kinds = {root: drive_type(root) for root in roots}
    labels = {}

    def worker(root):
        try:
            labels[root] = _volume_label(root)
        except OSError:
            labels[root] = None

    threads = []
    for root in roots:
        if kinds[root] == DRIVE_REMOTE:
            continue
        t = threading.Thread(target=worker, args=(root,), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + label_timeout
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    result = []
    for root in roots:
        parts = [root[:2]]
        label = labels.get(root)
        if label:
            parts.append(label)
        kind = _DRIVE_KIND.get(kinds[root])
        if kind:
            parts.append(f"[{kind}]")
        result.append((root, "  ".join(parts)))
    return result
