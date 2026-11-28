# safe_input.py
# 「窓を崩さない入力」(MCP `click` / `key` / `type`、traytools_send.py の click / key / type)の部品。
#
# computer-use の left_click / key は、許可外の窓を全部最小化して窓配置を崩す。こちらは
# SendInput で「いま前面にある対象の窓」にだけ送り、窓の移動・前面化・サイズ変更・起動は
# 一切しない(SetForegroundWindow / SetWindowPos / ShowWindow 禁止)。
# 送る前の確認はすべて自動で、違えば送らずに理由と次の手を返す。承認や既定オフは付けない。
#
# 前半は Win32 にも Qt にも触れない純関数(引数解釈・キー指定の解析・拒否キー・前面/所有者連鎖/
# 座標の判定・INPUT 配列の組み立て)で、tests/test_safe_input.py が見る。後半の Win32Env だけが
# ctypes で実際に触る。execute() は env を差し替えられるので、テストでは偽物を渡して
# SendInput をモックし、INPUT 配列の中身だけを検証する(実際には入力を送らない)。
#
# 罠: INPUT 構造体は x64 で 40 バイト(MOUSEINPUT が union の最大で 32 バイト + type と詰め物 8)。
# 余計な詰め物を入れると SendInput が 0 を返し、例外なしで何も起きない。
import ctypes
import ctypes.wintypes as W
import os

import window_list

MAX_TEXT_CHARS = 1000
OWNER_CHAIN_MAX = 8  # 所有者連鎖を辿る上限(循環や異常な深さに備える)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010

VK_RETURN = 0x0D
VK_TAB = 0x09
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12


# ---------------------------------------------------------------
# INPUT 構造体
# ---------------------------------------------------------------
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", _InputUnion)]


# x64 では 40、x86 では 28。ずれていたら SendInput が黙って 0 を返すので import 時に落とす。
assert ctypes.sizeof(INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28), ctypes.sizeof(INPUT)


def mouse_input(flags: int) -> INPUT:
    i = INPUT()
    i.type = INPUT_MOUSE
    i.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
    return i


def key_input(vk: int, up: bool = False, scan: int = 0, extended: bool = False) -> INPUT:
    i = INPUT()
    i.type = INPUT_KEYBOARD
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def unicode_input(code_unit: int, up: bool = False) -> INPUT:
    i = INPUT()
    i.type = INPUT_KEYBOARD
    i.ki = KEYBDINPUT(0, code_unit, KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0), 0, 0)
    return i


def to_array(inputs):
    return (INPUT * len(inputs))(*inputs)


# ---------------------------------------------------------------
# キー指定の解析
# ---------------------------------------------------------------
_MODIFIERS = {"ctrl": VK_CONTROL, "control": VK_CONTROL, "shift": VK_SHIFT, "alt": VK_MENU, "menu": VK_MENU}
_WIN_NAMES = {"win", "windows", "lwin", "rwin", "meta", "super", "cmd", "command"}
_DELETE_NAMES = {"del", "delete"}

_NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "bs": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    # 記号キー(US配列の OEM キー。JIS配列でも同じ VK だが刻印は異なりうる)
    ";": 0xBA, "semicolon": 0xBA, "=": 0xBB, "equals": 0xBB, "+": 0xBB, "plus": 0xBB,
    ",": 0xBC, "comma": 0xBC, "-": 0xBD, "minus": 0xBD, ".": 0xBE, "period": 0xBE,
    "/": 0xBF, "slash": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for _c in "abcdefghijklmnopqrstuvwxyz":
    _NAMED_KEYS[_c] = ord(_c.upper())
for _d in "0123456789":
    _NAMED_KEYS[_d] = ord(_d)
for _n in range(1, 25):
    _NAMED_KEYS[f"f{_n}"] = 0x70 + _n - 1

# 拡張キー(これを付けないとテンキー側の同名キーとして届くことがある)
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}


def parse_keys(spec):
    """キー指定を (修飾キーの VK リスト, 主キーの VK) にする。不正・拒否キーは ValueError。
    例 "ctrl+r" "enter" "alt+left" "f5" "ctrl+shift+t" "ctrl++"。
    Win キー系と ctrl+alt+del は拒否(OS の予約・セキュリティ画面を呼ぶため)。"""
    s = (spec or "").strip()
    if not s:
        raise ValueError("keys が空です")
    if s == "+":
        tokens = ["+"]
    elif s.endswith("++"):
        tokens = s[:-2].split("+") + ["+"]
    else:
        tokens = s.split("+")
    tokens = [t.strip().lower() for t in tokens]
    if any(not t for t in tokens):
        raise ValueError(f"キー指定を解釈できません: {spec}（例: ctrl+r / enter / alt+left）")
    for t in tokens:
        if t in _WIN_NAMES:
            raise ValueError(f"Win キー系は送れません: {t}")
    *mod_names, key_name = tokens
    mods = []
    for m in mod_names:
        if m not in _MODIFIERS:
            raise ValueError(f"修飾キーは ctrl / shift / alt だけです: {m}")
        vk = _MODIFIERS[m]
        if vk not in mods:
            mods.append(vk)
    if key_name in _MODIFIERS:
        raise ValueError(f"主キーがありません（修飾キーだけです）: {spec}")
    if key_name not in _NAMED_KEYS:
        raise ValueError(f"知らないキーです: {key_name}")
    if key_name in _DELETE_NAMES and VK_CONTROL in mods and VK_MENU in mods:
        raise ValueError("ctrl+alt+del は送れません")
    return mods, _NAMED_KEYS[key_name]


def _scan_code(vk: int) -> int:
    try:
        return int(ctypes.WinDLL("user32").MapVirtualKeyW(vk, 0)) & 0xFFFF
    except (OSError, AttributeError):
        return 0


def key_events(mods, vk, scan_of=_scan_code):
    """修飾キーを押す→主キー→離す(逆順)の INPUT 列。1回の SendInput にまとめて送る。"""
    ext = vk in _EXTENDED_VKS
    events = [key_input(m, scan=scan_of(m)) for m in mods]
    events.append(key_input(vk, scan=scan_of(vk), extended=ext))
    events.append(key_input(vk, up=True, scan=scan_of(vk), extended=ext))
    events += [key_input(m, up=True, scan=scan_of(m)) for m in reversed(mods)]
    return events


def key_release_events(mods, vk):
    """途中失敗の後始末。押しっぱなしを残さないために、関係するキー全部の KEYUP。"""
    ext = vk in _EXTENDED_VKS
    return [key_input(vk, up=True, extended=ext)] + [key_input(m, up=True) for m in reversed(mods)]


# ---------------------------------------------------------------
# type / click の INPUT
# ---------------------------------------------------------------
def check_text(text):
    """type の本文の検査。(整えた本文, エラー文 or None)。改行は \\n に揃える。"""
    if not isinstance(text, str) or text == "":
        return None, "text が空です"
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_TEXT_CHARS:
        return None, f"text が長すぎます（{len(text)}文字。上限{MAX_TEXT_CHARS}文字）"
    for ch in text:
        if ord(ch) < 0x20 and ch not in "\n\t":
            return None, f"送れない制御文字が含まれています（U+{ord(ch):04X}）"
    return text, None


def text_events(text):
    """本文を KEYEVENTF_UNICODE の押す/離すにする。改行は Enter キー、タブは Tab キー。
    UTF-16 のサロゲートペアは1単位ずつ送る(Windows がそれで1文字に組む)。"""
    events = []
    for ch in text:
        if ch == "\n":
            events += [key_input(VK_RETURN), key_input(VK_RETURN, up=True)]
        elif ch == "\t":
            events += [key_input(VK_TAB), key_input(VK_TAB, up=True)]
        else:
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):
                unit = int.from_bytes(data[i:i + 2], "little")
                events += [unicode_input(unit), unicode_input(unit, up=True)]
    return events


def click_events(button="left", double=False):
    down, up = ((MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP) if button == "right"
                else (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    one = [mouse_input(down), mouse_input(up)]
    return one * 2 if double else one


# ---------------------------------------------------------------
# 引数の翻訳(常駐側が受ける形)
# ---------------------------------------------------------------
def parse_input_args(kind, args):
    """常駐が受ける引数を (params, エラー文 or None) にする。
    オプション: --window=… --dry --double --button=left|right --right --text=… --keys=…
    それ以外は位置引数(click: x y / key: キー指定 / type: 本文)。"""
    window = None
    dry = double = False
    button = "left"
    text = keys = None
    positional = []
    for raw in args:
        s = str(raw)
        if s.startswith("--window="):
            window = s[len("--window="):].strip()
        elif s == "--dry":
            dry = True
        elif s == "--double":
            double = True
        elif s == "--right":
            button = "right"
        elif s.startswith("--button="):
            button = s[len("--button="):].strip().lower()
        elif s.startswith("--text="):
            text = s[len("--text="):]
        elif s.startswith("--keys="):
            keys = s[len("--keys="):]
        else:
            positional.append(s)
    if not window or not window_list.parse_window_spec(window):
        return None, "window（exe名 か hwnd）が必要です"
    params = {"window": window, "dry": dry}
    if kind == "click":
        if button not in ("left", "right"):
            return None, f"button は left / right です: {button}"
        if text is not None or keys is not None or len(positional) != 2:
            return None, "click は x y（整数2つ）が必要です"
        try:
            x, y = int(positional[0]), int(positional[1])
        except ValueError:
            return None, f"x y は整数です: {positional[0]} {positional[1]}"
        params.update(x=x, y=y, button=button, double=double)
    elif kind == "key":
        if keys is None:
            if len(positional) != 1:
                return None, "key はキー指定が1つ必要です（例: ctrl+r）"
            keys = positional[0]
        try:
            params["mods"], params["vk"] = parse_keys(keys)
        except ValueError as e:
            return None, str(e)
        params["keys"] = keys.strip()
    elif kind == "type":
        if text is None:
            text = " ".join(positional) if positional else None
        body, err = check_text(text)
        if err:
            return None, err
        params["text"] = body
    else:
        return None, f"知らない入力の種類です: {kind}"
    return params, None


# ---------------------------------------------------------------
# 送る前の確認(純関数。違えば理由つきのエラー文を返す)
# ---------------------------------------------------------------
def owner_chain_reaches(hwnd, target, get_owner, max_depth=OWNER_CHAIN_MAX) -> bool:
    """hwnd 自身か、その所有者連鎖(GW_OWNER)のどこかが target か。"""
    cur = int(hwnd or 0)
    for _ in range(max_depth + 1):
        if not cur:
            return False
        if cur == int(target):
            return True
        cur = int(get_owner(cur) or 0)
    return False


def check_elevated(target):
    """対象が管理者権限(または判定不能)なら中止。UIPI で入力が黙って捨てられるため。"""
    if target["elevated"] is False:
        return None
    why = "管理者権限で動いています" if target["elevated"] else "権限を判定できません（アクセス拒否）"
    return (f"対象 {target['exe']} は{why}。UIPI により入力が黙って捨てられるため送りません"
            "（tray-tools も管理者で動かすか、別の窓で操作してください）")


def check_foreground(fg, target_hwnd, get_owner, describe):
    """前面の窓が対象（または対象の所有する窓＝ダイアログ）であること。"""
    if owner_chain_reaches(fg, target_hwnd, get_owner):
        return None
    return f"対象を前面に出してください（いまの前面: {describe(fg)}）。窓は動かさないので送りません"


def check_click_point(x, y, rect):
    """(x, y) が対象窓の論理矩形 (rx, ry, w, h) の中か。"""
    rx, ry, w, h = rect
    if rx <= x < rx + w and ry <= y < ry + h:
        return None
    return f"座標({x},{y})は対象窓の矩形の外です（矩形: x={rx}..{rx + w - 1}, y={ry}..{ry + h - 1}）"


def check_under_point(root, target_hwnd, get_owner, describe, x, y):
    """座標の下の窓のルート（または所有者連鎖）が対象であること。"""
    if not root:
        return f"座標({x},{y})の下に窓がありません"
    if owner_chain_reaches(root, target_hwnd, get_owner):
        return None
    return (f"座標({x},{y})の一番上は対象ではなく {describe(root)} です"
            "（対象が覆われています。送りません）")


# ---------------------------------------------------------------
# 実行(env を差し替えればテストできる)
# ---------------------------------------------------------------
class InputError(Exception):
    pass


class Result:
    def __init__(self, ok, text, sent=False, hwnd=0, log=""):
        self.ok = ok        # False なら ERR（text に理由）
        self.text = text    # 応答の1行目以降（"OK …" / "ERR …" は付けない）
        self.sent = sent    # 実際に SendInput したか（dry や中止は False）
        self.hwnd = hwnd
        self.log = log      # 操作ログ用（type の本文は入れない）


def _send(env, inputs, cleanup=()):
    n = env.send_input(to_array(inputs), len(inputs))
    if n != len(inputs):
        if cleanup:
            try:
                env.send_input(to_array(list(cleanup)), len(cleanup))
            except Exception:  # noqa: BLE001  後始末の失敗で元の理由を隠さない
                pass
        raise InputError(f"SendInput が {len(inputs)} 件のうち {n} 件しか受け付けませんでした（入力は届いていない可能性があります）")


def _who(w):
    return f"{w['exe'] or '?'} hwnd={w['hwnd']}「{(w['title'] or '')[:30]}」"


def execute(kind, p, windows, env) -> Result:
    """確認 → 送信。窓には一切触れない（前面化・移動・サイズ変更をしない）。"""
    spec = window_list.parse_window_spec(p["window"])
    target, candidates, err = window_list.resolve_window(windows, spec)
    if target is None:
        if len(candidates) > 1:
            err += "\n" + "\n".join(window_list.format_line(w) for w in candidates)
        return Result(False, err)
    hwnd = int(target["hwnd"])
    if target["minimized"] or not target["rect"]:
        return Result(False, f"窓 {_who(target)} は最小化されています。送りません（窓は動かしません）")
    err = check_elevated(target)
    if err:
        return Result(False, err)
    err = check_foreground(env.foreground(), hwnd, env.owner, env.describe)
    if err:
        return Result(False, err)

    if kind == "click":
        x, y = p["x"], p["y"]
        err = check_click_point(x, y, target["rect"])
        if err:
            return Result(False, err)
        px, py = env.to_physical(x, y, hwnd)
        err = check_under_point(env.root_at(px, py), hwnd, env.owner, env.describe, x, y)
        if err:
            return Result(False, err)
        what = f"{p['button']}{'ダブル' if p['double'] else ''}クリック({x},{y})"
        log = f"click {target['exe']} hwnd={hwnd} ({x},{y}) {p['button']}{' double' if p['double'] else ''}"
    elif kind == "key":
        what = f"キー {p['keys']}"
        log = f"key {target['exe']} hwnd={hwnd} {p['keys']}"
    else:
        n = len(p["text"])
        what = f"{n}文字を入力"
        log = f"type {target['exe']} hwnd={hwnd} {n}文字"  # 本文はログに出さない

    if p.get("dry"):
        return Result(True, f"確認のみ（dry）: {_who(target)} に {what} を送れる状態です（送っていません）",
                      sent=False, hwnd=hwnd)

    try:
        if kind == "click":
            old = env.get_cursor()
            if not env.set_cursor(px, py):
                raise InputError("カーソルを動かせませんでした（SetCursorPos 失敗）")
            try:
                events = click_events(p["button"], p["double"])
                up = MOUSEEVENTF_RIGHTUP if p["button"] == "right" else MOUSEEVENTF_LEFTUP
                _send(env, events, cleanup=[mouse_input(up)])
            finally:
                if old is not None:
                    env.set_cursor(old[0], old[1])
        elif kind == "key":
            _send(env, key_events(p["mods"], p["vk"]), cleanup=key_release_events(p["mods"], p["vk"]))
        else:
            _send(env, text_events(p["text"]))
    except InputError as e:
        return Result(False, str(e), hwnd=hwnd)
    return Result(True, f"{_who(target)} に {what} を送りました", sent=True, hwnd=hwnd, log=log)


# ---------------------------------------------------------------
# 実環境(ctypes)。テストでは使わない
# ---------------------------------------------------------------
class Win32Env:
    """実際の Win32。専用の WinDLL を持つ(他モジュールと argtypes を取り合わない)。
    dpr_at(物理x, 物理y) -> 画面倍率 は呼び出し側(Qt 依存)から渡す。"""

    def __init__(self, dpr_at=None):
        import window_tools
        self._wt = window_tools
        self._dpr_at = dpr_at or (lambda x, y: 1.0)
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = ctypes.c_void_p
        u.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetWindow.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u.GetAncestor.restype = ctypes.c_void_p
        u.WindowFromPoint.argtypes = [W.POINT]
        u.WindowFromPoint.restype = ctypes.c_void_p
        u.GetCursorPos.argtypes = [ctypes.POINTER(W.POINT)]
        u.GetCursorPos.restype = ctypes.c_bool
        u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        u.SetCursorPos.restype = ctypes.c_bool
        u.SendInput.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
        u.SendInput.restype = ctypes.c_uint
        self._u = u

    def foreground(self):
        return int(self._u.GetForegroundWindow() or 0)

    def owner(self, hwnd):
        return int(self._u.GetWindow(hwnd, 4) or 0)  # GW_OWNER

    def root_at(self, x, y):
        h = self._u.WindowFromPoint(W.POINT(x, y))
        return int(self._u.GetAncestor(h, 2) or 0) if h else 0  # GA_ROOT

    def to_physical(self, x, y, hwnd):
        """論理座標 → 物理。窓一覧の物理→論理(窓の中心がある画面の倍率で割る)の逆。"""
        b = self._wt.get_window_bounds(hwnd)
        dpr = self._dpr_at((b[0] + b[2]) // 2, (b[1] + b[3]) // 2) if b else 1.0
        return int(round(x * dpr)), int(round(y * dpr))

    def describe(self, hwnd):
        title = self._wt.get_window_title(hwnd) if hwnd else ""
        path = self._wt.get_window_exe(hwnd) if hwnd else ""
        return f"{os.path.basename(path) if path else '?'}「{title[:30]}」hwnd={int(hwnd or 0)}"

    def get_cursor(self):
        p = W.POINT()
        return (p.x, p.y) if self._u.GetCursorPos(ctypes.byref(p)) else None

    def set_cursor(self, x, y):
        return bool(self._u.SetCursorPos(x, y))

    def send_input(self, array, n):
        return int(self._u.SendInput(n, array, ctypes.sizeof(INPUT)))
