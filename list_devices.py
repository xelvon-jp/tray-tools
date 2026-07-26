"""接続中の音声デバイス一覧とIDを表示する。settings.json の audio.devices を書き換える用。
デバイスIDはPC固有なので、別のPCでは必ずここで取り直す(先頭の * が有効なデバイス)。"""

from pycaw.utils import AudioUtilities
from pycaw.constants import AudioDeviceState

if __name__ == "__main__":
    for d in AudioUtilities.GetAllDevices():
        mark = "*" if d.state == AudioDeviceState.Active else " "
        print(f"{mark} {d.FriendlyName!r} | {d.id}")
