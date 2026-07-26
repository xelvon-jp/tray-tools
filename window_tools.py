# window_tools.py
# 任意ウィンドウの最前面固定(Always on Top相当)。user32 を ctypes で叩くだけの部品で、
# トレイアイコンは持たない。
import ctypes

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001

_user32 = ctypes.windll.user32

# HWNDは64bit Windowsではポインタ幅。ctypesの既定の戻り値は32bit intなので、
# 明示しないとハンドルが切り詰められて別ウィンドウを操作しかねない。
_user32.GetForegroundWindow.restype = ctypes.c_void_p
_user32.IsWindow.argtypes = [ctypes.c_void_p]
_user32.IsWindow.restype = ctypes.c_bool
_user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_bool


def get_foreground_window() -> int:
    return _user32.GetForegroundWindow()


def is_window(hwnd: int) -> bool:
    return bool(_user32.IsWindow(hwnd))


def get_window_title(hwnd: int) -> str:
    length = _user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def is_own_window(hwnd: int) -> bool:
    """自プロセスのウィンドウ(付箋ウインドウ等)かどうか。自分を最前面固定しても
    意味が無いうえ、付箋は元から最前面なので解除side effectだけが残る。"""
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == ctypes.windll.kernel32.GetCurrentProcessId()


def set_topmost(hwnd: int, topmost: bool) -> bool:
    return bool(
        _user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST if topmost else HWND_NOTOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE,
        )
    )


class TopmostTracker:
    """自分が最前面固定したhwndの集合。トグル判定に使う。
    ウィンドウは勝手に閉じられ、hwndは別のウィンドウに再利用されうるので、
    参照するたびにIsWindowで生死を確認して無効なものを捨てる。"""

    def __init__(self):
        self._hwnds = set()

    def _prune(self) -> None:
        self._hwnds = {hwnd for hwnd in self._hwnds if is_window(hwnd)}

    def is_pinned(self, hwnd: int) -> bool:
        self._prune()
        return hwnd in self._hwnds

    def toggle_foreground(self):
        """フォアグラウンドウィンドウの最前面固定をトグルする。
        戻り値は (ウィンドウタイトル, 固定したか) のタプル。対象外なら None。"""
        hwnd = get_foreground_window()
        if not hwnd or not is_window(hwnd) or is_own_window(hwnd):
            return None

        pinned = not self.is_pinned(hwnd)
        if not set_topmost(hwnd, pinned):
            return None

        if pinned:
            self._hwnds.add(hwnd)
        else:
            self._hwnds.discard(hwnd)
        return get_window_title(hwnd), pinned

    def release_all(self) -> None:
        self._prune()
        for hwnd in list(self._hwnds):
            set_topmost(hwnd, False)
        self._hwnds.clear()
