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
_SetThreadExecutionState = ctypes.windll.kernel32.SetThreadExecutionState
_SetThreadExecutionState.argtypes = [ctypes.c_uint32]
_SetThreadExecutionState.restype = ctypes.c_uint32


def set_keep_awake(enabled: bool) -> bool:
    """スリープ抑止のON/OFFを切り替える。戻り値は成功したかどうか
    (SetThreadExecutionStateは失敗時に0を返す)。"""
    flags = ES_CONTINUOUS
    if enabled:
        flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    return _SetThreadExecutionState(flags) != 0
