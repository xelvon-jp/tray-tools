# filer_contextmenu.py
# あっふの F7「エクスプローラの右クリックメニュー」を出す子プロセス。
#
# なぜ別プロセスなのか
#   シェルのコンテキストメニューは COM(IShellFolder / IContextMenu)でしか出せず、しかも
#   メニューを開いた瞬間に、入っているシェル拡張(圧縮ソフト・クラウド同期・ウイルス
#   対策…)が丸ごとこのプロセスに読み込まれる。CLAUDE.md にあるとおり COM の寿命の
#   間違いはプロセスを例外も残さず即死させるし、拡張側の不具合も同じ死に方をする。
#   ファイラ本体に持ち込むと、開いている左右の窓ごと消える。ここなら落ちても
#   「メニューが出なかった」で済む。
#
# 使い方
#   pythonw filer_contextmenu.py --x 物理X --y 物理Y --owner ファイラのHWND -- パス...
#   pythonw filer_contextmenu.py --json 一時ファイル      (パスが多くて引数に載らないとき)
#   python  filer_contextmenu.py --list [--repeat N] -- パス...
#       診断用。メニューを組み立てて項目を標準出力に出して終わる(画面には何も出さない)。
#       --repeat で同じことを N 回、毎回 gc.collect を挟んで繰り返す(COM の寿命の確かめ)。
#
# CLAUDE.md の COM の約束をここでも守る
#   - COM ポインタを ctypes.cast しない(型を変えるときは QueryInterface)
#   - COM オブジェクトは show_menu の外に出さない。ウィンドウプロシージャから
#     IContextMenu3 を使うための入れ物(_menu_handler)も、関数を出る前に必ず空にする
#   - ctypes は argtypes / restype を必ず指定する
#   - comtypes.COMError は OSError のサブクラスではないので、別に捕まえる
import ctypes
import gc
import os
import sys
import time
from ctypes import POINTER, byref, c_size_t, c_void_p, c_wchar_p, sizeof
from ctypes import wintypes

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, COMError, IUnknown

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import filer_core as core  # noqa: E402

UINT = wintypes.UINT
LRESULT = ctypes.c_ssize_t


# ---------------------------------------------------------------
# COM のインターフェース(使う手前のメソッドも順番どおりに並べる必要がある)
# ---------------------------------------------------------------
class IContextMenu(IUnknown):
    _iid_ = GUID("{000214E4-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "QueryContextMenu",
                  (["in"], wintypes.HMENU, "hmenu"), (["in"], UINT, "indexMenu"),
                  (["in"], UINT, "idCmdFirst"), (["in"], UINT, "idCmdLast"),
                  (["in"], UINT, "uFlags")),
        COMMETHOD([], HRESULT, "InvokeCommand", (["in"], c_void_p, "pici")),
        COMMETHOD([], HRESULT, "GetCommandString",
                  (["in"], c_size_t, "idCmd"), (["in"], UINT, "uType"),
                  (["in"], c_void_p, "pReserved"), (["in"], c_void_p, "pszName"),
                  (["in"], UINT, "cchMax")),
    ]


class IContextMenu2(IContextMenu):
    _iid_ = GUID("{000214F4-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "HandleMenuMsg",
                  (["in"], UINT, "uMsg"), (["in"], wintypes.WPARAM, "wParam"),
                  (["in"], wintypes.LPARAM, "lParam")),
    ]


class IContextMenu3(IContextMenu2):
    _iid_ = GUID("{BCFCE0A0-EC17-11D0-8D10-00A0C90F2719}")
    _methods_ = [
        COMMETHOD([], HRESULT, "HandleMenuMsg2",
                  (["in"], UINT, "uMsg"), (["in"], wintypes.WPARAM, "wParam"),
                  (["in"], wintypes.LPARAM, "lParam"), (["in"], c_void_p, "plResult")),
    ]


class IShellFolder(IUnknown):
    _iid_ = GUID("{000214E6-0000-0000-C000-000000000046}")
    _methods_ = [
        COMMETHOD([], HRESULT, "ParseDisplayName",
                  (["in"], wintypes.HWND), (["in"], c_void_p), (["in"], c_wchar_p),
                  (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "EnumObjects",
                  (["in"], wintypes.HWND), (["in"], wintypes.DWORD), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "BindToObject",
                  (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "BindToStorage",
                  (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "CompareIDs",
                  (["in"], wintypes.LPARAM), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "CreateViewObject",
                  (["in"], wintypes.HWND), (["in"], c_void_p), (["in"], c_void_p)),
        COMMETHOD([], HRESULT, "GetAttributesOf",
                  (["in"], UINT), (["in"], c_void_p), (["in"], c_void_p)),
        # apidl は相対 PIDL の配列、ppv は受け取る IContextMenu のポインタの置き場。
        COMMETHOD([], HRESULT, "GetUIObjectOf",
                  (["in"], wintypes.HWND, "hwndOwner"), (["in"], UINT, "cidl"),
                  (["in"], c_void_p, "apidl"), (["in"], POINTER(GUID), "riid"),
                  (["in"], c_void_p, "rgfReserved"), (["in"], c_void_p, "ppv")),
    ]


# ---------------------------------------------------------------
# Win32
# ---------------------------------------------------------------
class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CMINVOKECOMMANDINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.DWORD),
        ("hwnd", wintypes.HWND),
        ("lpVerb", c_void_p),          # MAKEINTRESOURCE(番号) を入れるので void*
        ("lpParameters", c_void_p),
        ("lpDirectory", c_void_p),
        ("nShow", ctypes.c_int),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("lpTitle", c_void_p),
        ("lpVerbW", c_void_p),
        ("lpParametersW", c_void_p),
        ("lpDirectoryW", c_wchar_p),
        ("lpTitleW", c_void_p),
        ("ptInvoke", POINT),
    ]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", c_wchar_p),
        ("lpszClassName", c_wchar_p),
    ]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", UINT), ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", POINT)]


user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _fn(dll, name, restype, *argtypes):
    func = getattr(dll, name)
    func.argtypes = list(argtypes)
    func.restype = restype
    return func


SHParseDisplayName = _fn(shell32, "SHParseDisplayName", ctypes.c_long,
                         c_wchar_p, c_void_p, POINTER(c_void_p), wintypes.ULONG, c_void_p)
SHBindToParent = _fn(shell32, "SHBindToParent", ctypes.c_long,
                     c_void_p, POINTER(GUID), c_void_p, POINTER(c_void_p))
CoTaskMemFree = _fn(ole32, "CoTaskMemFree", None, c_void_p)
OleInitialize = _fn(ole32, "OleInitialize", ctypes.c_long, c_void_p)
OleUninitialize = _fn(ole32, "OleUninitialize", None)
OleFlushClipboard = _fn(ole32, "OleFlushClipboard", ctypes.c_long)

CreatePopupMenu = _fn(user32, "CreatePopupMenu", wintypes.HMENU)
DestroyMenu = _fn(user32, "DestroyMenu", wintypes.BOOL, wintypes.HMENU)
GetMenuItemCount = _fn(user32, "GetMenuItemCount", ctypes.c_int, wintypes.HMENU)
GetMenuItemID = _fn(user32, "GetMenuItemID", UINT, wintypes.HMENU, ctypes.c_int)
GetMenuStringW = _fn(user32, "GetMenuStringW", ctypes.c_int,
                     wintypes.HMENU, UINT, wintypes.LPWSTR, ctypes.c_int, UINT)
GetSubMenu = _fn(user32, "GetSubMenu", wintypes.HMENU, wintypes.HMENU, ctypes.c_int)
TrackPopupMenuEx = _fn(user32, "TrackPopupMenuEx", wintypes.BOOL,
                       wintypes.HMENU, UINT, ctypes.c_int, ctypes.c_int, wintypes.HWND, c_void_p)
SetForegroundWindow = _fn(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
PostMessageW = _fn(user32, "PostMessageW", wintypes.BOOL,
                   wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
DefWindowProcW = _fn(user32, "DefWindowProcW", LRESULT,
                     wintypes.HWND, UINT, wintypes.WPARAM, wintypes.LPARAM)
RegisterClassW = _fn(user32, "RegisterClassW", wintypes.ATOM, POINTER(WNDCLASSW))
CreateWindowExW = _fn(user32, "CreateWindowExW", wintypes.HWND,
                      wintypes.DWORD, c_wchar_p, c_wchar_p, wintypes.DWORD,
                      ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                      wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, c_void_p)
DestroyWindow = _fn(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
GetModuleHandleW = _fn(kernel32, "GetModuleHandleW", wintypes.HMODULE, c_wchar_p)
PeekMessageW = _fn(user32, "PeekMessageW", wintypes.BOOL,
                   POINTER(MSG), wintypes.HWND, UINT, UINT, UINT)
TranslateMessage = _fn(user32, "TranslateMessage", wintypes.BOOL, POINTER(MSG))
DispatchMessageW = _fn(user32, "DispatchMessageW", LRESULT, POINTER(MSG))
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
EnumWindows = _fn(user32, "EnumWindows", wintypes.BOOL, EnumWindowsProc, wintypes.LPARAM)
GetWindowThreadProcessId = _fn(user32, "GetWindowThreadProcessId", wintypes.DWORD,
                               wintypes.HWND, POINTER(wintypes.DWORD))
IsWindowVisible = _fn(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
IsWindow = _fn(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)

CMF_NORMAL = 0x0
CMF_EXPLORE = 0x4
CMF_CANRENAME = 0x10
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
MF_BYPOSITION = 0x400
WM_NULL = 0x0000
WM_DRAWITEM = 0x002B
WM_MEASUREITEM = 0x002C
WM_INITMENUPOPUP = 0x0117
WM_MENUCHAR = 0x0120
PM_REMOVE = 0x0001
SW_SHOWNORMAL = 1
CMIC_MASK_UNICODE = 0x00004000
CMIC_MASK_PTINVOKE = 0x20000000
ID_FIRST = 1
ID_LAST = 0x7FFF

# InvokeCommand の後、シェル拡張は別スレッドで窓を出して処理を続けることがある
# (プロパティ、削除の確認や進捗など)。プロセスを終えるとそれも消えるので、
# この時間のうちに窓が出なければ終わる、出ていれば閉じるまで待つ。
INVOKE_GRACE_SECONDS = 3.0
WAIT_POLL_SECONDS = 0.05

_menu_handler = None   # ウィンドウプロシージャから IContextMenu2/3 へ渡す口。show_menu の間だけ
_class_registered = False
_CLASS_NAME = "TrayToolsFilerContextMenuOwner"


def _wndproc(hwnd, msg, wparam, lparam):
    # ここで例外を投げ切ると ctypes のコールバックから戻れなくなる。必ず受ける。
    try:
        handler = _menu_handler
        if handler is not None and msg in (WM_INITMENUPOPUP, WM_DRAWITEM,
                                           WM_MEASUREITEM, WM_MENUCHAR):
            handled, result = handler(msg, wparam, lparam)
            if handled:
                return result
    except Exception:  # noqa: BLE001
        pass
    return DefWindowProcW(hwnd, msg, wparam, lparam)


_WNDPROC = WNDPROC(_wndproc)   # 参照を持ち続けないと、コールバックが解放されて落ちる


def _owner_window():
    """メニューの持ち主にする非表示のトップレベル窓(表示はしない)。"""
    global _class_registered
    hinst = GetModuleHandleW(None)
    if not _class_registered:
        wc = WNDCLASSW()
        wc.lpfnWndProc = _WNDPROC
        wc.hInstance = hinst
        wc.lpszClassName = _CLASS_NAME
        if not RegisterClassW(byref(wc)):
            raise OSError(ctypes.get_last_error(), "RegisterClassW")
        _class_registered = True
    hwnd = CreateWindowExW(0, _CLASS_NAME, "filer context menu", 0,
                           0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        raise OSError(ctypes.get_last_error(), "CreateWindowExW")
    return hwnd


def _parse_pidls(paths):
    pidls = []
    try:
        for path in paths:
            pidl = c_void_p()
            hr = SHParseDisplayName(os.path.normpath(path), None, byref(pidl), 0, None)
            if hr != 0 or not pidl.value:
                raise OSError(hr, f"パスを解釈できません: {path}")
            pidls.append(pidl)
        return pidls
    except Exception:
        for pidl in pidls:
            CoTaskMemFree(pidl)
        raise


def _menu_items(hmenu, depth=0):
    """メニューの項目を [(深さ, ID, 名前)] で返す(--list 用)。"""
    items = []
    for pos in range(GetMenuItemCount(hmenu)):
        buf = ctypes.create_unicode_buffer(512)
        GetMenuStringW(hmenu, pos, buf, 512, MF_BYPOSITION)
        sub = GetSubMenu(hmenu, pos)
        item_id = -1 if sub else int(GetMenuItemID(hmenu, pos))
        items.append((depth, item_id, buf.value))
        if sub and depth < 2:
            items.extend(_menu_items(sub, depth + 1))
    return items


def _own_visible_windows(exclude):
    pid = os.getpid()
    found = []

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        GetWindowThreadProcessId(hwnd, byref(owner))
        if owner.value == pid and hwnd != exclude and IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(callback), 0)
    return found


def _pump_messages():
    msg = MSG()
    while PeekMessageW(byref(msg), None, 0, 0, PM_REMOVE):
        TranslateMessage(byref(msg))
        DispatchMessageW(byref(msg))


def _wait_for_shell_windows(owner_hwnd) -> None:
    """InvokeCommand の後、シェルがこのプロセスで出した窓(プロパティ等)が閉じるまで待つ。

    SHSetInstanceExplorer で参照を数える方法もあるが、Python で IUnknown を実装する
    必要があり(COM オブジェクトをこちらが持つことになる)、拡張によっては参照を
    返し忘れて終われなくなる。窓が見えている間だけ待つ方が、壊れ方が穏やか。"""
    deadline = time.monotonic() + INVOKE_GRACE_SECONDS
    while True:
        _pump_messages()
        if _own_visible_windows(owner_hwnd):
            deadline = time.monotonic() + 1.0   # 窓が残っている間は待ち続ける
        elif time.monotonic() > deadline:
            return
        time.sleep(WAIT_POLL_SECONDS)


def show_menu(paths, x: int, y: int, list_only: bool = False, filer_hwnd: int = 0):
    """paths のコンテキストメニューを出す(list_only なら項目を返すだけ)。

    COM のオブジェクトはこの関数の中だけで作って使い、出る前に手放す。"""
    global _menu_handler
    pidls = _parse_pidls(paths)
    folder = None
    menu = None
    menu2 = None
    menu3 = None
    hmenu = None
    owner = None
    try:
        owner = _owner_window()
        # 子の相対 PIDL は、それぞれの絶対 PIDL の中を指す(別に解放しない)。
        children = []
        for index, pidl in enumerate(pidls):
            parent = POINTER(IShellFolder)()
            child = c_void_p()
            hr = SHBindToParent(pidl, byref(IShellFolder._iid_), byref(parent), byref(child))
            if hr != 0:
                raise OSError(hr, f"SHBindToParent に失敗: {paths[index]}")
            if index == 0:
                folder = parent   # 同じフォルダの中のものなので、親は先頭のものを使う
            del parent
            children.append(child.value)
        array = (c_void_p * len(children))(*children)
        menu = POINTER(IContextMenu)()
        folder.GetUIObjectOf(owner, len(children), array, byref(IContextMenu._iid_),
                             None, byref(menu))
        hmenu = CreatePopupMenu()
        menu.QueryContextMenu(hmenu, 0, ID_FIRST, ID_LAST, CMF_NORMAL | CMF_EXPLORE)
        try:
            menu3 = menu.QueryInterface(IContextMenu3)
        except COMError:
            try:
                menu2 = menu.QueryInterface(IContextMenu2)
            except COMError:
                pass

        def handler(msg, wparam, lparam):
            # 「送る」「新規作成」などのサブメニューは、開くときにこのメッセージで中身が
            # 作られる。IContextMenu3 があればそちら、無ければ IContextMenu2 へ渡す。
            if menu3 is not None:
                result = LRESULT()
                try:
                    menu3.HandleMenuMsg2(msg, wparam, lparam, byref(result))
                except COMError:
                    return False, 0
                return True, result.value
            if menu2 is not None and msg != WM_MENUCHAR:
                try:
                    menu2.HandleMenuMsg(msg, wparam, lparam)
                except COMError:
                    return False, 0
                return True, 0
            return False, 0

        _menu_handler = handler
        if list_only:
            return _menu_items(hmenu)

        # 前面を取っておかないと、Esc や外側のクリックでメニューが閉じなくなる。
        # 取る権利はファイラが起こす前に譲ってくれている(AllowSetForegroundWindow)。
        SetForegroundWindow(owner)
        command = TrackPopupMenuEx(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, x, y, owner, None)
        PostMessageW(owner, WM_NULL, 0, 0)
        if command < ID_FIRST:
            # 何も選ばずに閉じた。前面をファイラへ返す(今は前面を持っているので返せる)。
            if filer_hwnd:
                SetForegroundWindow(filer_hwnd)
            return None
        info = CMINVOKECOMMANDINFOEX()
        info.cbSize = sizeof(CMINVOKECOMMANDINFOEX)
        info.fMask = CMIC_MASK_UNICODE | CMIC_MASK_PTINVOKE
        info.hwnd = owner
        info.lpVerb = command - ID_FIRST        # MAKEINTRESOURCE(番号)
        info.lpVerbW = command - ID_FIRST
        info.lpDirectoryW = os.path.dirname(os.path.normpath(paths[0]))
        info.nShow = SW_SHOWNORMAL
        info.ptInvoke = POINT(x, y)
        menu.InvokeCommand(byref(info))
        _wait_for_shell_windows(owner)
        return command
    finally:
        _menu_handler = None
        # 手放す順: 派生のインターフェース → メニュー → フォルダ。PIDL はその後。
        menu3 = None
        menu2 = None
        menu = None
        folder = None
        if hmenu:
            DestroyMenu(hmenu)
        if owner:
            DestroyWindow(owner)
        for pidl in pidls:
            CoTaskMemFree(pidl)


def main(argv=None) -> int:
    args = core.parse_context_args(sys.argv[1:] if argv is None else argv)
    if args["json"]:
        try:
            os.remove(args["json"])
        except OSError:
            pass
    if not args["paths"]:
        return 2
    try:
        # 拡大率の違う画面でも、ファイラが渡す物理座標どおりに出す。
        user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext(c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        pass
    if args["list"]:
        # 標準出力は UTF-8 で(呼ぶ側のテストが読む)。
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    # 「送る」や「切り取り/コピー」はクリップボードや D&D を使うので、COM ではなく
    # OLE として初期化する(STA)。
    OleInitialize(None)
    code = 0
    try:
        for round_ in range(args["repeat"]):
            items = show_menu(args["paths"], args["x"], args["y"],
                              list_only=args["list"], filer_hwnd=args["owner"])
            if args["list"]:
                for depth, item_id, label in items:
                    print(f"{round_}\t{depth}\t{item_id}\t{label}")
            gc.collect()
        # 「切り取り」「コピー」でクリップボードに載せたデータは、このプロセスが持っている。
        # 終わる前に実体にしておかないと、プロセスと一緒にクリップボードから消える。
        OleFlushClipboard()
    except (OSError, COMError) as e:
        print(f"error: {e}", file=sys.stderr)
        code = 1
    finally:
        gc.collect()
        OleUninitialize()
    if args["list"]:
        print("done")
    return code


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # comtypes は終了処理で CoUninitialize を呼ぶ。こちらは OleUninitialize 済みなので、
    # 二重の後始末で揉めないように、ここで後始末を飛ばして抜ける。
    os._exit(code)
