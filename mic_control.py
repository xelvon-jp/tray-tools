# mic_control.py
# 既定の録音デバイス(マイク)のミュート操作。トレイアイコンは持たない部品で、
# 表示や通知は feature_audio 側が行う。
from ctypes import POINTER, cast

import comtypes
from comtypes import CLSCTX_ALL
from pycaw.api.endpointvolume import IAudioEndpointVolume
from pycaw.utils import AudioUtilities


# pycaw/comtypes が投げる COMError は Exception 直下で OSError のサブクラスではないため、
# OSError だけを捕まえても素通りしてしまう。録音デバイスが無い/切断された環境では
# COMError 側で落ちるので両方を捕まえる(起動時の _refresh から呼ばれるため、
# ここで落ちるとトレイアイコンが出る前にアプリごと死ぬ)。
_AUDIO_ERRORS = (OSError, comtypes.COMError)


def _ensure_com_initialized() -> None:
    # feature_audio と同じ作法。keyboardライブラリのホットキーコールバックは専用スレッドで
    # 実行され、そのスレッドではCOMが未初期化のためpycaw呼び出しが失敗する。
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def _endpoint_volume():
    _ensure_com_initialized()
    microphone = AudioUtilities.GetMicrophone()
    if microphone is None:
        return None
    interface = microphone.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def get_mute():
    """現在のミュート状態(True/False)。マイクが無い・取得に失敗した場合は None。"""
    try:
        volume = _endpoint_volume()
        if volume is None:
            return None
        return bool(volume.GetMute())
    except _AUDIO_ERRORS:
        return None


def set_mute(muted: bool) -> bool:
    try:
        volume = _endpoint_volume()
        if volume is None:
            return False
        volume.SetMute(1 if muted else 0, None)
        return True
    except _AUDIO_ERRORS:
        return False


def toggle_mute():
    """ミュートを反転する。戻り値は反転後の状態、失敗時は None。"""
    current = get_mute()
    if current is None:
        return None
    if not set_mute(not current):
        return None
    return not current
