# mouse_jiggler.py
# マウスジグラー。無操作が続いているときだけ、極小のマウス入力を1組だけ起こす部品。
# keep_awake.py と同じ粒度のctypesの薄いラッパで、状態管理と自動解除タイマーは
# Feature側(feature_screen.py)が行う。
#
# スリープ抑止(keep_awake.py)との違い:
#   あちらは SetThreadExecutionState でOSに「スリープするな・画面を消すな」と申告する
#   だけで、「入力があったか」を見ているリモートデスクトップのアイドル切断やロック画面には
#   何の影響も無い。切断を防ぐには入力そのものを起こすしかない。
#
# SetCursorPos / mouse_event を使わない理由:
#   SetCursorPos はカーソルの座標を書き換えるだけで「入力」としては記録されない場合があり、
#   GetLastInputInfo もアイドルタイマーも更新されないため目的を達しない。
#   mouse_event は SendInput の旧版で非推奨API。よって SendInput を使う。
import ctypes

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001

# dwExtraInfo は ULONG_PTR(64bitでは8バイト)。c_ulong(4バイト)で書くと構造体の
# 大きさが足りず、SendInput が何もせず0を返すか、最悪その先のメモリを壊す。
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _INPUT_UNION(ctypes.Union):
    # 使うのは mi だけだが、unionの大きさは一番大きいメンバで決まる。本物の INPUT と
    # 同じ顔ぶれを並べておかないと sizeof がずれ、SendInput は cbSize が合っていないと
    # 何もせず0を返す(64bitでは INPUT 全体で40バイトになる)。
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    # _anonymous_ にすると input.mi.dx とCの書き方どおりに触れる(input.u.mi.dx でも可)。
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("u", _INPUT_UNION),
    ]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_ulong),
    ]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# argtypes/restype は必ず指定する。64bitでポインタがintに切り詰められると即クラッシュする。
_SendInput = _user32.SendInput
_SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
_SendInput.restype = ctypes.c_uint

_GetLastInputInfo = _user32.GetLastInputInfo
_GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
_GetLastInputInfo.restype = ctypes.c_bool

_GetTickCount = _kernel32.GetTickCount
_GetTickCount.argtypes = []
_GetTickCount.restype = ctypes.c_uint32


def idle_seconds():
    """最後のキーボード/マウス入力からの経過秒を返す。取得に失敗したらNone。"""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not _GetLastInputInfo(ctypes.byref(info)):
        return None
    # dwTime は GetTickCount 基準の32bitミリ秒で、約49.7日で一周する。引き算の結果を
    # 32bitに丸めれば、一周をまたいだ直後(now が小さく dwTime が大きい)でも差は正しく
    # 出る。丸めずに引くと巨大な負の値になり、「ずっと無操作」と誤判定してしまう。
    elapsed_ms = (_GetTickCount() - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0


def jiggle() -> bool:
    """カーソルを相対で+1px動かし、すぐ-1px戻す。戻り値は成功したかどうか。

    2つの入力は1回の SendInput にまとめて渡す。2回に分けて呼ぶと、その隙間に
    ユーザーが手を添えた場合にずれた1pxが残る。まとめれば差し引きゼロの移動が
    一続きで注入されるので、カーソルは実質その場に留まったまま入力だけが起きる。

    SendInput は「実際に送れた件数」を返す。UIPI(管理者権限のウィンドウが前面など)で
    弾かれると0になるので、渡した件数と一致しなければ失敗として扱う。"""
    events = (INPUT * 2)()
    for event, dx in zip(events, (1, -1)):
        event.type = INPUT_MOUSE
        event.mi.dx = dx
        event.mi.dy = 0
        event.mi.mouseData = 0
        event.mi.dwFlags = MOUSEEVENTF_MOVE
        event.mi.time = 0  # 0ならOSが現在のタイムスタンプを入れる
        event.mi.dwExtraInfo = 0
    return _SendInput(len(events), events, ctypes.sizeof(INPUT)) == len(events)


def jiggle_if_idle(idle_threshold_seconds: float):
    """無操作が idle_threshold_seconds 以上続いているときだけ jiggle() する。

    戻り値は3通り: True=送れた / False=送ろうとして失敗した / None=そもそも送らなかった。
    呼び出し側は「失敗」だけを知らせたいので、送らなかった場合と区別できるようにしている。

    操作中に割り込んでカーソルを跳ねさせるのは邪魔でしかないので、無操作の判定は
    必ずここを通す(送るだけの jiggle() を外から直接呼ばないこと)。経過を取れなかった
    ときも、当てずっぽうで動かすより何もしない方が害が小さいのでNoneで返す。"""
    elapsed = idle_seconds()
    if elapsed is None or elapsed < idle_threshold_seconds:
        return None
    return jiggle()
