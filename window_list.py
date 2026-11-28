# window_list.py
# 窓の一覧(MCP `windows` / `traytools_send.py windows`)と、capture の `--window=` が
# 使う部品。読み取り専用で、窓の移動・前面化・入力は一切しない(SetForegroundWindow 禁止)。
#
# 前半は Qt にも Win32 にも依存しない純関数(絞り込み・引数解釈・重なり判定・整形)で、
# tests/test_window_list.py が見る。後半の collect_windows だけが ctypes で窓を集める。
# 矩形は capture と同じ論理座標(x, y, w, h)。物理→論理の変換は呼び出し側から渡す
# (capture_grab は Qt に依存するので、ここでは import しない)。
import ctypes
import os

import window_layout
import window_tools

# 重なりとして警告する下限。対象の窓の面積に対する割合。数pxの縁が触れているだけの
# 窓まで並べると、警告が常に出て読まれなくなる。
OVERLAP_MIN_RATIO = 0.02
# 警告に並べる窓の数の上限(多すぎると1行で読めない)。
OVERLAP_MAX_LISTED = 5


# ---------------------------------------------------------------
# 純関数
# ---------------------------------------------------------------
def parse_windows_args(args):
    """`windows` の引数を (exe, title, all, エラー文 or None) にする。
    書式は `exe=名前` `title=文字列` `--all`(`all=true` も可)。"""
    exe = title = None
    show_all = False
    for raw in args:
        text = str(raw).strip()
        if not text:
            continue
        key, sep, value = text.partition("=")
        key = key.lower()
        if key == "exe" and sep:
            exe = value.strip() or None
        elif key == "title" and sep:
            title = value.strip() or None
        elif key in ("--all", "all"):
            show_all = (not sep) or value.strip().lower() in ("1", "true", "yes", "on")
        else:
            return None, None, False, f"知らない引数です: {text}（exe=… title=… --all）"
    return exe, title, show_all, None


def exe_matches(window_exe: str, query: str) -> bool:
    """exe名の部分一致(大文字小文字は無視)。"""
    return query.lower() in (window_exe or "").lower()


def filter_windows(windows, exe=None, title=None, show_all=False):
    """既定は最小化・ツール窓・タイトル空を除く。show_all で全部。exe/title は部分一致。"""
    result = []
    for w in windows:
        if not show_all:
            if w["minimized"] or w["toolwindow"] or not (w["title"] or "").strip():
                continue
        if exe and not exe_matches(w["exe"], exe):
            continue
        if title and title.lower() not in (w["title"] or "").lower():
            continue
        result.append(w)
    return result


def parse_window_spec(text: str):
    """capture の window= の値を ("hwnd", 整数) か ("exe", 名前) にする。
    10進または 0x 付きの16進を hwnd とみなす。空なら None。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if text.lower().startswith("0x"):
            return "hwnd", int(text, 16)
        if text.isdigit():
            return "hwnd", int(text)
    except ValueError:
        return None
    return "exe", text


def exe_equals(window_exe: str, query: str) -> bool:
    """capture の window=exe 用。名前の完全一致(.exe の有無は問わない)。
    「chrome」で「chromedriver」まで拾わないよう、一覧の部分一致より厳しくしてある。"""
    def norm(name):
        name = (name or "").strip().lower()
        return name[:-4] if name.endswith(".exe") else name
    return norm(window_exe) == norm(query)


def resolve_window(windows, spec):
    """spec(parse_window_spec の結果)から対象を決める。
    (窓 or None, 候補リスト, エラー文 or None)。候補が2つ以上ならエラー文つきで返す。
    windows は最小化も含む全窓(filter 前)を渡す。"""
    kind, value = spec
    if kind == "hwnd":
        for w in windows:
            if w["hwnd"] == value:
                return w, [w], None
        return None, [], f"hwnd {value} の窓が見つかりません（可視の最上位窓ではない、または閉じた）"
    candidates = [
        w for w in windows
        if exe_equals(w["exe"], value) and not w["toolwindow"] and (w["title"] or "").strip()
    ]
    if not candidates:
        return None, [], f"exe「{value}」の窓が見つかりません（windows で確認してください）"
    if len(candidates) > 1:
        return None, candidates, f"exe「{value}」の窓が{len(candidates)}個あります。hwnd で指定してください"
    return candidates[0], candidates, None


def rect_intersection_area(a, b) -> int:
    """(x, y, w, h) 同士の重なり面積。"""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def find_overlaps(target, windows, min_ratio=OVERLAP_MIN_RATIO):
    """target より前面(Z順で手前)にあって矩形が重なる窓を、手前から返す。

    windows は Z順(前面から)の全窓。target より前にあるものだけが写り込む側。
    最小化・矩形なしは除く。重なりが target の面積の min_ratio 未満なら無視。"""
    if not target.get("rect"):
        return []
    area = target["rect"][2] * target["rect"][3]
    if area <= 0:
        return []
    result = []
    for w in windows:
        if w["hwnd"] == target["hwnd"]:
            break  # ここから後ろは target より奥
        if w["minimized"] or not w.get("rect"):
            continue
        if rect_intersection_area(target["rect"], w["rect"]) / area >= min_ratio:
            result.append(w)
    return result


def format_overlaps(overlaps) -> str:
    """応答に付ける警告。空なら空文字。"""
    if not overlaps:
        return ""
    names = [f"{(w['title'] or w['cls'])[:20]}({w['exe'] or '?'})" for w in overlaps[:OVERLAP_MAX_LISTED]]
    more = len(overlaps) - OVERLAP_MAX_LISTED
    tail = f" ほか{more}件" if more > 0 else ""
    return "重なり: " + ", ".join(names) + tail + "（画面から切り出すので重なりも写っています）"


def _elevated_text(value) -> str:
    return {True: "admin", False: "user"}.get(value, "権限不明(アクセス拒否)")


def format_line(w) -> str:
    """1窓1行。"""
    if w["minimized"] or not w.get("rect"):
        rect = "最小化"
    else:
        x, y, ww, hh = w["rect"]
        rect = f"{x},{y},{ww},{hh}"
    mon = f"mon{w['monitor']}" if w.get("monitor") else "mon?"
    flags = []
    if w["foreground"]:
        flags.append("前面")
    if w["minimized"]:
        flags.append("最小化")
    if w["topmost"]:
        flags.append("TOPMOST")
    flags.append(_elevated_text(w["elevated"]))
    title = (w["title"] or "").replace("\r", " ").replace("\n", " ")
    if len(title) > 60:
        title = title[:59] + "…"
    return (f"hwnd={w['hwnd']} {w['exe'] or '?'} pid={w['pid']} \"{title}\" "
            f"class={w['cls']} rect={rect} {mon} [{' '.join(flags)}]")


# ---------------------------------------------------------------
# 窓を集める(ctypes。読み取りだけ)
# ---------------------------------------------------------------
TOKEN_QUERY = 0x0008
TokenElevation = 20
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080

try:
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.OpenProcessToken.restype = ctypes.c_bool
    _advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    _advapi32.GetTokenInformation.restype = ctypes.c_bool
except (AttributeError, OSError):
    _advapi32 = None


def process_elevated(pid: int):
    """管理者権限で動いているか。True/False、開けなければ None(アクセス拒否など)。"""
    if _advapi32 is None or not pid:
        return None
    k32 = window_tools._kernel32
    handle = k32.OpenProcess(window_tools.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = ctypes.c_void_p()
    try:
        if not _advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            elevation = ctypes.c_ulong(0)
            returned = ctypes.c_ulong(0)
            if not _advapi32.GetTokenInformation(
                    token, TokenElevation, ctypes.byref(elevation), ctypes.sizeof(elevation),
                    ctypes.byref(returned)):
                return None
            return elevation.value != 0
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(handle)


def _exe_name(pid: int, hwnd: int, cache: dict) -> str:
    if pid in cache:
        return cache[pid]
    name = ""
    try:
        import psutil
        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001  プロセスが消えた・拒否など。パス側へ落とす
        path = window_tools.get_window_exe(hwnd)
        name = os.path.basename(path) if path else ""
    cache[pid] = name
    return name


def collect_windows(to_logical, monitors):
    """可視の最上位窓を Z順(前面から)で返す。最小化も含む(絞り込みは filter_windows)。

    to_logical: 物理ピクセルの (left, top, right, bottom) → 論理座標の (x, y, w, h)。
    monitors: 論理座標 (left, top, right, bottom) のリスト(capture の画面番号と同じ並び)。
    例外は投げない。"""
    user32 = window_tools._user32
    foreground = window_tools.get_foreground_window()
    foreground = int(foreground) if foreground else 0
    exe_cache = {}
    windows = []

    def on_window(hwnd, _lparam):
        try:
            if not hwnd or not user32.IsWindowVisible(hwnd):
                return True
            cls = window_tools.get_window_class(hwnd)
            if cls in window_tools.DESKTOP_CLASSES or window_tools.is_cloaked(hwnd):
                return True
            minimized = bool(user32.IsIconic(hwnd))
            ex_style = window_tools.get_ex_style(hwnd)
            pid_c = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_c))
            pid = int(pid_c.value)
            rect = None
            monitor = None
            if not minimized:
                bounds = window_tools.get_window_bounds(hwnd)
                if bounds is not None:
                    x, y, w, h = to_logical(bounds)
                    rect = (x, y, w, h)
                    index = window_layout.window_monitor((x, y, x + w, y + h), monitors)
                    monitor = index + 1 if index is not None else None
            windows.append({
                "hwnd": int(hwnd),
                "exe": _exe_name(pid, hwnd, exe_cache),
                "pid": pid,
                "title": window_tools.get_window_title(hwnd),
                "cls": cls,
                "rect": rect,
                "monitor": monitor,
                "minimized": minimized,
                "topmost": bool(ex_style & WS_EX_TOPMOST),
                "toolwindow": bool(ex_style & WS_EX_TOOLWINDOW),
                "elevated": process_elevated(pid),
                "foreground": int(hwnd) == foreground,
            })
        except Exception:  # noqa: BLE001  1窓の失敗で全体を諦めない(コールバックの例外は伝わらない)
            pass
        return True

    callback = window_tools._ENUM_WINDOWS_PROC(on_window)
    try:
        user32.EnumWindows(callback, None)
    except OSError:
        pass
    return windows
