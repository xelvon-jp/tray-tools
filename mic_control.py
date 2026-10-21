# mic_control.py
# 既定の録音デバイス(マイク)のミュート操作。トレイアイコンは持たない部品で、
# 表示や通知は feature_audio 側が行う。
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


def _with_endpoint_volume(action):
    """既定のマイクの IAudioEndpointVolume を作り、action(volume) の戻り値を返す。
    マイクが無ければ None。

    【ctypes.cast で COM ポインタの型を変えないこと】(2026-10-21 に突き止めた常駐の持病)
    以前は Activate が返した IUnknown を cast(interface, POINTER(IAudioEndpointVolume))
    で型だけ変えていた。これには2つの害が重なっていた。

    - cast は AddRef しない。COM 側の参照は1つなのに、Python 側では interface と
      cast の結果の2つがそれぞれ __del__ で Release する(=解放しすぎ)。
    - cast は元と結果に同じ _objects(dict)を持たせ、その dict に元のポインタ自身を
      入れる。元のポインタは「自分の dict が自分を抱える」循環になり、参照カウントでは
      消えず、循環 GC が回ってきたときにだけ解放される。

    結果、cast の結果が先に Release して COM オブジェクトは消え、そのあと
    「いつ・どのスレッドで来るか分からない」循環 GC が残った interface を Release して、
    解放済みのメモリを触る。crash.log の「Garbage-collecting → unknwn.py Release →
    __del__ ← (毎回違う処理)」の正体がこれで、GC を呼び込んだ側は引き金にすぎない。
    tools/com_gc_harness.py で再現し(--probe で犯人がここだと記録される)、
    tests/test_com_gc.py で直したことを確かめている。

    型を変えるときは QueryInterface を使う(AddRef する。循環も作らない)。どの
    ポインタも参照カウントで、この関数の中・作ったスレッドの上で解放される。"""
    _ensure_com_initialized()
    microphone = AudioUtilities.GetMicrophone()
    if microphone is None:
        return None
    interface = microphone.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = interface.QueryInterface(IAudioEndpointVolume)
    return action(volume)


def get_mute():
    """現在のミュート状態(True/False)。マイクが無い・取得に失敗した場合は None。"""
    try:
        return _with_endpoint_volume(lambda volume: bool(volume.GetMute()))
    except _AUDIO_ERRORS:
        return None


def set_mute(muted: bool) -> bool:
    def apply(volume):
        volume.SetMute(1 if muted else 0, None)
        return True

    try:
        return bool(_with_endpoint_volume(apply))
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
