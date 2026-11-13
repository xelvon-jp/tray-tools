# keep_awake.py
# スリープ抑止(Caffeine相当)。kernel32.SetThreadExecutionState を叩くだけなので追加依存は無い。
# トレイアイコンは持たないただの部品で、状態管理と自動解除タイマーはFeature側が行う。
#
# 重要: この状態は「呼び出したスレッド」に紐づく。ホットキーコールバック用のスレッドなど
# 一時的なスレッドから呼ぶと、そのスレッドが終わった時点でOSが勝手に抑止を解除してしまう。
# 必ずQtメインスレッドから呼ぶこと。
import ctypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# 0x80000000 は符号付きintに収まらないため、argtypes/restype を明示して符号なしで渡す。
# 専用の WinDLL から取る(ctypes.windll は共有で、署名が他モジュールと取り合いに
# なるため。window_tools と同じ作法)。
try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _SetThreadExecutionState = _kernel32.SetThreadExecutionState
    _SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    _SetThreadExecutionState.restype = ctypes.c_uint32
except (AttributeError, OSError):
    # Windows 以外では WinDLL 自体が無い。import だけ通し、呼び出しは失敗に倒す。
    _kernel32 = None
    _SetThreadExecutionState = None


def set_keep_awake(enabled: bool) -> bool:
    """スリープ抑止のON/OFFを切り替える。戻り値は成功したかどうか
    (SetThreadExecutionStateは失敗時に0を返す)。"""
    if _SetThreadExecutionState is None:
        return False
    flags = ES_CONTINUOUS
    if enabled:
        flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    return _SetThreadExecutionState(flags) != 0


# ---------------------------------------------------------------
# スリープさせる
# ---------------------------------------------------------------
# SetSuspendState(Hibernate, ForceCritical, DisableWakeEvent)。
# Hibernate=False でスリープ(休止ではない)、ForceCritical=False にするのは、
# 各アプリに「これから寝る」を知らせて保存の機会を与えるため。True にすると
# 問答無用で落とすので、編集中のものが失われうる。
try:
    _powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    _SetSuspendState = _powrprof.SetSuspendState
    _SetSuspendState.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]
    _SetSuspendState.restype = ctypes.c_bool
except (AttributeError, OSError):
    _powrprof = None
    _SetSuspendState = None


def hibernate_available() -> bool:
    """休止状態が使えるか。

    休止は環境によって無効にされている(powercfg /hibernate off)。無効なまま
    SetSuspendState(True, ...) を呼ぶと、何も起きないかスリープに落ちる。押しても
    反応がないと故障に見えるので、呼ぶ前に確かめて理由を伝えられるようにする。

    判定は powercfg の予約領域(hiberfil.sys)の有無ではなく、電源APIに聞く。
    IsPwrHibernateAllowed は「いま休止できるか」を返す。"""
    if _powrprof is None:
        return False
    try:
        return bool(_powrprof.IsPwrHibernateAllowed())
    except Exception:
        return False


def suspend(hibernate: bool = False) -> bool:
    """PCをスリープさせる。hibernate=True なら休止状態。掛けてある抑止は先に外す。

    抑止したまま呼ぶと、OSに「起きていろ」と言いながら「寝ろ」と言うことになり、
    寝ないか、寝てもすぐ起きる。呼ぶ側が忘れても事故らないよう、ここで外す。

    ForceCritical=False にするのは、各アプリに「これから寝る」を知らせて保存の機会を
    与えるため。True にすると問答無用で落とすので、編集中のものが失われうる。

    戻り値はAPIが受け付けたかどうか。実際に寝るかはOSとドライバ次第で、
    ここが True でも寝ないことはある。"""
    set_keep_awake(False)
    if _SetSuspendState is None:
        return False
    return bool(_SetSuspendState(bool(hibernate), False, False))
