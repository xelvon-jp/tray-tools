# window_tools.py
# 任意ウィンドウの最前面固定(Always on Top相当)と、開いているウィンドウの列挙
# (ウィンドウ単位キャプチャ用)。user32/dwmapi を ctypes で叩くだけの部品で、
# トレイアイコンは持たず、Qtにも依存しない(座標は物理ピクセルのまま返す)。
import ctypes

HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
# Zオーダーの最後尾。最前面属性も外れる(send_to_back を参照)。
HWND_BOTTOM = 1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
# 「Zオーダーだけ動かして、フォーカスは一切触るな」の指定。これを付け忘れると
# SetWindowPos が対象ウィンドウをアクティブにしてしまい、入力中の窓からフォーカスを
# 奪って文字が飛ぶ。定期的に押し上げる用途(taskbar_widget)では必須。
SWP_NOACTIVATE = 0x0010

# タスクバーのウィンドウクラス。プライマリは1つだけで、通知領域(TrayNotifyWnd)を持つのは
# そちらだけ。セカンダリはモニタごとに1つ存在しうる(通知領域は無い)。
PRIMARY_TASKBAR_CLASS = "Shell_TrayWnd"
SECONDARY_TASKBAR_CLASS = "Shell_SecondaryTrayWnd"

# ウィンドウクラス名の受け皿。Windowsのクラス名は256文字までと決まっている。
CLASS_NAME_BUFFER = 256

# デスクトップ(壁紙)のウィンドウクラス。常に全画面を占めるので、列挙に混ぜると
# 「どこをクリックしてもデスクトップ」になってしまう。
DESKTOP_CLASSES = ("Progman", "WorkerW")

# これより小さいウィンドウは列挙しない(物理ピクセル)。実体の無い管理用の小窓が
# カーソルの下に紛れ込むと、狙ったウィンドウの代わりにそちらを掴んでしまう。
MIN_WINDOW_EDGE = 20

# DwmGetWindowAttribute の属性番号。
# EXTENDED_FRAME_BOUNDS = 見た目どおりの矩形、CLOAKED = 表示されていないのに存在する状態。
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

try:
    _user32 = ctypes.windll.user32
except AttributeError:
    # Windows 以外では windll 自体が無い。import だけ通す(窓の操作自体は
    # Windows でしか呼ばれない)。dwmapi の読み込みと同じ作法。
    _user32 = None

if _user32 is not None:
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

    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int


class RECT(ctypes.Structure):
    """Win32のRECT。right/bottomは排他(その座標自体は含まない)なので、QRectへ直すときは
    QRect(left, top, right - left, bottom - top) で作ること。
    QRect(QPoint(left, top), QPoint(right, bottom)) にすると縦横が1pxずつ大きくなる。"""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


# EnumWindows のコールバック。第1引数のHWNDは c_void_p で受ける(このファイル冒頭の
# restype 指定と同じ理由で、64bit環境ではintだとハンドルが切り詰められる)。
_ENUM_WINDOWS_PROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

if _user32 is not None:
    _user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    _user32.GetWindowRect.restype = ctypes.c_bool

# dwmapi は Vista 以降なら必ずあるが、読み込めない環境でもモジュールのimportごと
# 失敗させたくない。使えないときは GetWindowRect だけで動かす(影の分だけ大きくなる)。
try:
    _dwmapi = ctypes.windll.dwmapi
    _dwmapi.DwmGetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
    ]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT。0(S_OK)以外は失敗
except (OSError, AttributeError):
    _dwmapi = None


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


def send_to_back(hwnd) -> bool:
    """Zオーダーのいちばん後ろへ送る(最前面属性も外れる)。

    フルスクリーンのアプリ(動画の全画面表示など)が前面に来たとき、その上に自前の
    ウィジェットが出続けると邪魔でしかない。かといって hide() すると hideEvent で
    押し上げのタイマーごと止まり、全画面が終わったことに気づけなくなる。Zオーダーを
    下げるだけならタイマーは回り続けるので、戻ってきたら押し上げを再開できる。

    HWND_NOTOPMOST ではなく HWND_BOTTOM を使う。前者は「最前面ではないウィンドウ全部の
    上」に置く指定で、最前面属性は外れるものの全画面アプリより手前に居座ってしまう
    (実際それで隠れなかった)。HWND_BOTTOM なら確実に後ろへ回る。"""
    return bool(
        _user32.SetWindowPos(
            hwnd,
            HWND_BOTTOM,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    )


def foreground_bounds():
    """前面のウィンドウの矩形(物理px)。前面が無い/デスクトップのときは None。

    デスクトップを除くのは、何も前面に無いときにあれが前面扱いになるため。覆われて
    いても普通にウィジェットを出したい相手なので、判定から外す。

    覆っているかどうかの判断は呼ぶ側でする。ここが返すのは物理ピクセルで、比べたい
    相手(画面の矩形)はQtの論理座標なので、換算を持っている側で揃えるほうが確実。"""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    if get_window_class(hwnd) in DESKTOP_CLASSES:
        return None
    return get_window_bounds(hwnd)


def push_topmost(hwnd) -> bool:
    """最前面グループの中で、もう一度いちばん手前へ押し上げる。

    set_topmost と違い SWP_NOACTIVATE を付ける。タスクバーも最前面なので、その上に
    自前のウィジェットを出し続けるには定期的に押し上げるしかないが、そのたびに
    フォーカスが飛んで来ると作業中の入力を奪ってしまう(taskbar_widget が500ms間隔で
    呼ぶ経路なので、1回でも奪えば実害が出る)。

    失敗しても呼び出し側は何もできない(次の周期でまた試す)ので、真偽値を返すだけで
    例外は投げない。"""
    return bool(
        _user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    )


# ---------------------------------------------------------------
# ウィンドウ列挙(ウィンドウ単位キャプチャ用)
#
# 範囲選択オーバーレイは全モニタを覆うため、表示してしまうと WindowFromPoint は
# オーバーレイ自身しか返さない。そこで「オーバーレイを出す前に一覧を作っておき、
# あとはカーソル座標から引く」という使い方をする(capture_overlay 側で引く)。
#
# ここの関数は例外を投げない(explorer_nav と同じ理由。呼び出し元はQtのスロットで、
# PySide6 はスロットから例外が抜けると常駐アプリごと終了する)。
# ---------------------------------------------------------------
def get_window_class(hwnd) -> str:
    """ウィンドウクラス名。取れなければ空文字。

    同じものが explorer_nav.window_class にもあるが、あちらは COM(Shell.Application)を
    前提にしたモジュールで、こちらから import すると循環参照になる(explorer_nav →
    window_tools)。数行なのでそれぞれに持たせている。"""
    buffer = ctypes.create_unicode_buffer(CLASS_NAME_BUFFER)
    if not _user32.GetClassNameW(hwnd, buffer, CLASS_NAME_BUFFER):
        return ""
    return buffer.value


def is_cloaked(hwnd) -> bool:
    """DWMのクローク状態(存在はするが画面には出ていない)かどうか。

    UWPアプリは終了しても不可視のウィンドウを残す。IsWindowVisible は真のままなので、
    これを見ないと「見えていないウィンドウ」を掴んでしまう。"""
    if _dwmapi is None:
        return False
    cloaked = ctypes.c_int(0)
    hresult = _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    if hresult != 0:
        return False  # 判定できなければクロークしていない扱い(見えているものを消さない)
    return cloaked.value != 0


def get_window_bounds(hwnd):
    """見た目どおりのウィンドウ矩形 (left, top, right, bottom)。取れなければ None。
    物理ピクセルで、right/bottom は排他。

    GetWindowRect はドロップシャドウを含むため、Windows 10/11 では見た目より一回り
    大きい矩形が返る(そのまま撮ると縁に背景が写り込む)。DWMの
    DWMWA_EXTENDED_FRAME_BOUNDS を優先し、取れないときだけ GetWindowRect に落とす。"""
    rect = RECT()
    if _dwmapi is not None:
        hresult = _dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if hresult == 0:
            return rect.left, rect.top, rect.right, rect.bottom
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return rect.left, rect.top, rect.right, rect.bottom
    return None


def _describe_window(hwnd, exclude_hwnd):
    """列挙の対象なら (hwnd, タイトル, (left, top, right, bottom))、対象外なら None。"""
    if not hwnd:
        return None
    if exclude_hwnd and int(hwnd) == int(exclude_hwnd):
        return None
    if not _user32.IsWindowVisible(hwnd):
        return None
    if _user32.IsIconic(hwnd):
        return None  # 最小化。矩形は残っているが画面には無い
    if get_window_class(hwnd) in DESKTOP_CLASSES:
        return None
    if is_cloaked(hwnd):
        return None

    bounds = get_window_bounds(hwnd)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    if right - left < MIN_WINDOW_EDGE or bottom - top < MIN_WINDOW_EDGE:
        return None
    return int(hwnd), get_window_title(hwnd), bounds


def list_windows(exclude_hwnd=None) -> list:
    """画面に見えているトップレベルウィンドウの一覧。各要素は
    (hwnd, タイトル, (left, top, right, bottom))で、矩形は物理ピクセル。

    EnumWindows はZ順(前面から)で返すので、ある座標を含む最初の1件がその位置の
    最前面のウィンドウになる。exclude_hwnd には自分自身のウィンドウを渡す。

    ctypes のコールバックから投げた例外は呼び出し元へ伝わらず、標準エラーに出るだけで
    列挙は続いてしまう。1枚分の失敗で全体を諦めないよう、コールバックの中でも受ける。"""
    windows = []

    def on_window(hwnd, _lparam):
        try:
            entry = _describe_window(hwnd, exclude_hwnd)
        except OSError:
            return True
        if entry is not None:
            windows.append(entry)
        return True

    # コールバックは変数に受けてから渡す。列挙中に回収されないよう参照を残す。
    callback = _ENUM_WINDOWS_PROC(on_window)
    try:
        _user32.EnumWindows(callback, None)
    except OSError:
        pass
    return windows


def find_windows_by_class(class_name: str) -> list:
    """クラス名が一致するトップレベルウィンドウのhwndを、Z順(前面から)で返す。

    FindWindowW だと最初の1つしか取れない。セカンダリディスプレイのタスクバーは
    モニタの数だけ存在しうる(Shell_SecondaryTrayWnd がモニタごとに1つ)ので、
    全部拾えるように EnumWindows で回す。

    list_windows と違い、可視性やサイズでの絞り込みはしない。タスクバーのような
    「アプリのウィンドウではないもの」を名指しで探す用途で、あちらの除外条件
    (最小化・クローク・最小サイズ)は当てはまらないため。"""
    found = []

    def on_window(hwnd, _lparam):
        try:
            if get_window_class(hwnd) == class_name:
                found.append(int(hwnd))
        except OSError:
            pass
        return True

    # コールバックは変数に受けてから渡す。列挙中に回収されないよう参照を残す。
    callback = _ENUM_WINDOWS_PROC(on_window)
    try:
        _user32.EnumWindows(callback, None)
    except OSError:
        pass
    return found


def get_taskbar_bounds(include_primary: bool = True) -> list:
    """タスクバーの矩形 (left, top, right, bottom) を見つかった順に返す。
    物理ピクセルで、right/bottom は排他。1つも無ければ空リスト。

    セカンダリ(Shell_SecondaryTrayWnd)はモニタの数だけ存在しうるので、1つに絞らず
    全部返す。どれを使うかは呼び出し側の判断(タスクバーウィジェットは全部に1つずつ置く)。

    include_primary=False でプライマリを外す。プライマリには本物の通知領域があるので、
    そちらには出したくない、という選択ができる。"""
    classes = [SECONDARY_TASKBAR_CLASS]
    if include_primary:
        # プライマリを先頭にする。「1番目がプライマリ」と決まっていたほうが、
        # 数が増えたときに結果を読み解きやすい(Z順は再現しない)。
        classes.insert(0, PRIMARY_TASKBAR_CLASS)

    found = []
    for class_name in classes:
        for hwnd in find_windows_by_class(class_name):
            bounds = get_window_bounds(hwnd)
            if bounds is not None:
                found.append(bounds)
    return found


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


# ---------------------------------------------------------------
# 窓の配置(窓を集める・配置プリセット。window_layout.py から使う)
# ---------------------------------------------------------------
if _user32 is not None:
    _user32.IsZoomed.argtypes = [ctypes.c_void_p]
    _user32.IsZoomed.restype = ctypes.c_bool

try:
    _kernel32 = ctypes.windll.kernel32
except AttributeError:
    _kernel32 = None
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
if _kernel32 is not None:
    _kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    _kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_bool


def is_zoomed(hwnd) -> bool:
    """最大化されているか。"""
    if _user32 is None:
        return False
    return bool(_user32.IsZoomed(hwnd))


def get_window_exe(hwnd) -> str:
    """ウィンドウの持ち主の実行ファイルのフルパス。取れなければ空文字。

    窓の照合(配置プリセット)はタイトルだけでは足りない(同じタイトルの別アプリが
    ありうる)ので、exe を第一の手掛かりにする。"""
    pid = ctypes.c_ulong()
    if not _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
        return ""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        _kernel32.CloseHandle(handle)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]


_MONITOR_ENUM_PROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(RECT), ctypes.c_void_p)
if _user32 is not None:
    _user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, _MONITOR_ENUM_PROC, ctypes.c_void_p]
    _user32.EnumDisplayMonitors.restype = ctypes.c_bool
    _user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = ctypes.c_bool
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
    _user32.GetCursorPos.restype = ctypes.c_bool


def list_monitor_work_areas() -> list:
    """モニタごとの作業領域 (left, top, right, bottom) の並び(物理ピクセル)。

    作業領域はタスクバーを除いた範囲。窓を集める・寄せる先はモニタ全体では
    なくこちら(タスクバーの下に隠れる置き方をしないため)。"""
    areas = []

    def on_monitor(hmonitor, _hdc, _rect, _lparam):
        try:
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                r = info.rcWork
                areas.append((r.left, r.top, r.right, r.bottom))
        except OSError:
            pass
        return True

    if _user32 is None:
        return areas
    callback = _MONITOR_ENUM_PROC(on_monitor)
    try:
        _user32.EnumDisplayMonitors(None, None, callback, None)
    except OSError:
        pass
    return areas


def get_cursor_pos():
    """マウスカーソルの位置 (x, y)(物理ピクセル)。取れなければ None。"""
    point = _POINT()
    if _user32 is None:
        return None
    if _user32.GetCursorPos(ctypes.byref(point)):
        return point.x, point.y
    return None


SW_SHOWNORMAL = 1
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4

GWL_EXSTYLE = -20
if _user32 is not None:
    _user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t


def get_ex_style(hwnd) -> int:
    """拡張スタイル(WS_EX_*)。取れなければ 0。"""
    if _user32 is None:
        return 0
    return int(_user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)) & 0xFFFFFFFF


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", _POINT),
                ("ptMaxPosition", _POINT), ("rcNormalPosition", RECT)]


if _user32 is not None:
    _user32.SetWindowPlacement.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WINDOWPLACEMENT)]
    _user32.SetWindowPlacement.restype = ctypes.c_bool
    _user32.GetWindowPlacement.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WINDOWPLACEMENT)]
    _user32.GetWindowPlacement.restype = ctypes.c_bool


def get_placement(hwnd):
    """(通常時の矩形, 最大化か)。取れなければ None。

    最大化中の窓の GetWindowRect はモニタ全体を返すので、元の大きさに戻す話は
    こちらの「通常時の矩形」でないとできない。"""
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    if not _user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return None
    r = placement.rcNormalPosition
    return (r.left, r.top, r.right, r.bottom), placement.showCmd == SW_MAXIMIZE


def set_placement(hwnd, rect, zoomed: bool = False) -> bool:
    """窓を指定の矩形へ移す。zoomed なら移した先で最大化する。

    SetWindowPos と違い、最大化中の窓もそのまま扱える(最大化を解いて矩形を
    当て、必要なら移した先で最大化し直す、までが1回の呼び出しで済む)。
    フォーカスは移さない(呼び出しは配置の都合で、操作の対象を変えない)。"""
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(_WINDOWPLACEMENT)
    placement.flags = 0
    placement.rcNormalPosition = RECT(*rect)
    if not zoomed:
        placement.showCmd = SW_SHOWNORMAL
        return bool(_user32.SetWindowPlacement(hwnd, ctypes.byref(placement)))
    # 最大化中の窓へいきなり SW_MAXIMIZE を当てても、窓は今のモニタで最大化された
    # まま動かず、通常時の矩形だけが書き換わる(2026-11-06 実機)。一度通常表示で
    # 移した先へ置いてから(前面にはしない)、そこで最大化し直す。
    placement.showCmd = SW_SHOWNOACTIVATE
    if not _user32.SetWindowPlacement(hwnd, ctypes.byref(placement)):
        return False
    placement.showCmd = SW_MAXIMIZE
    return bool(_user32.SetWindowPlacement(hwnd, ctypes.byref(placement)))
