# feature_audio.py
# 音声まわりのFeature。QSystemTrayIconを1つ持ち、出力デバイスのトグル切替(メニュー/
# ホットキー)と、マイクのミュート切替(mic_control.py に委譲)を提供する。
# (audio-switcher/main.py, audio_device.py を統合・一般化して移植)
import os
import time

import comtypes
from pycaw.constants import ERole
from pycaw.utils import AudioUtilities
from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

import mic_control
from qt_image import pil_to_qicon

# eConsole/eMultimedia/eCommunications の全ロールに反映しないと、
# アプリによっては切替後も旧デバイスに音が出続けることがあるため全て設定する。
_ROLES = [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications]

# ホットキー長押し時のOSキーリピートで do_toggle が連続発火するのを防ぐ最小間隔(秒)
_TOGGLE_COOLDOWN = 0.6

_DEFAULT_ICON_COLOR = "#6b7280"

_NOTIFY_MS = 3000

# mic_control._AUDIO_ERRORS と同じ理由。pycaw/comtypes が投げる COMError は Exception 直下で
# OSError のサブクラスではないため、OSError だけを捕まえても素通りしてしまう。
# settings.json のデバイスIDはPCごとに異なるGUIDで、別PCへ持ち込むと必ず実在しないIDになり、
# COMError(-2147023728, '要素が見つかりません') で落ちる。pythonw起動では無言で死ぬため必ず捕まえる。
_AUDIO_ERRORS = (OSError, comtypes.COMError)

_UNSET_MENU_TEXT = "音声デバイスが未設定です"
_SETUP_HINT = "setup.py を実行するか、メニューの「設定」から audio.devices を設定してください"


def _ensure_com_initialized() -> None:
    # keyboardライブラリのホットキーコールバックは専用スレッドで実行され、
    # そのスレッドではCOMが未初期化のためpycaw呼び出しが失敗する。
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def _get_current_device_id():
    """現在の既定の出力デバイスID。取得できない場合は None。"""
    try:
        _ensure_com_initialized()
        return AudioUtilities.GetSpeakers().id
    except _AUDIO_ERRORS:
        return None


def _set_default_device(device_id: str) -> bool:
    """既定の出力デバイスを切り替える。成功したら True。"""
    try:
        _ensure_com_initialized()
        AudioUtilities.SetDefaultDevice(device_id, roles=_ROLES)
        return True
    except _AUDIO_ERRORS:
        return False


def _device_exists(device_id: str) -> bool:
    """このPCに実在するデバイスIDか。GetDevice は存在しなければ COMError を投げる。
    全デバイス列挙(GetAllDevices)と違いプロパティを読まないので、メニューを開くたびに
    呼んでも軽い。"""
    if not device_id:
        return False
    try:
        _ensure_com_initialized()
        return AudioUtilities.GetDeviceEnumerator().GetDevice(device_id) is not None
    except _AUDIO_ERRORS:
        return False


def _draw_headphone(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.arc((14, 10, 50, 44), start=180, end=360, fill=color, width=5)
    draw.rounded_rectangle((11, 30, 23, 50), radius=4, fill=color)
    draw.rounded_rectangle((41, 30, 53, 50), radius=4, fill=color)


def _draw_monitor(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.rounded_rectangle((10, 14, 54, 42), radius=4, outline=color, width=5)
    draw.rectangle((29, 42, 35, 48), fill=color)
    draw.rectangle((21, 48, 43, 52), fill=color)


def _draw_mute_slash(draw: ImageDraw.ImageDraw) -> None:
    """マイクミュートの目印。通知領域のアイコンは実質16px相当で、隅の小さなバッジは
    潰れて見えない。アイコン全体を横切る斜線1本にして、縮小しても判別できるようにする。
    デバイス色に負けないよう白で縁取ってから赤を重ねる。"""
    draw.line((10, 54, 54, 10), fill="white", width=14)
    draw.line((10, 54, 54, 10), fill="#dc2626", width=8)


def _make_icon_image(device: dict, mic_muted: bool = False) -> Image.Image:
    color = device.get("color", _DEFAULT_ICON_COLOR)
    shape = device.get("icon")
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, 62, 62), fill=color)
    if shape == "headphone":
        _draw_headphone(draw, "white")
    elif shape == "monitor":
        _draw_monitor(draw, "white")
    if mic_muted:
        _draw_mute_slash(draw)
    return img


class AudioFeature:
    """Featureの規約: コンストラクタでQSystemTrayIconを1つ構築してself.tray_iconに保持し、
    hotkeys()で{"設定キー名": 関数}を返す。"""

    def __init__(self, app_settings: dict, settings_path=None):
        self.settings_path = settings_path
        self.devices = app_settings.get("audio", {}).get("devices", [])
        self._last_toggle_time = 0.0

        self.tray_icon = QSystemTrayIcon()
        self.menu = QMenu()

        hotkey_config = app_settings.get("hotkeys", {})
        toggle_hotkey = hotkey_config.get("audio_toggle")
        toggle_label = f"切り替え ({toggle_hotkey})" if toggle_hotkey else "切り替え"
        mic_hotkey = hotkey_config.get("mic_mute")
        mic_label = f"マイクをミュート ({mic_hotkey})" if mic_hotkey else "マイクをミュート"

        self._current_action = self.menu.addAction("現在: -")
        self._current_action.setEnabled(False)
        self.menu.addAction(toggle_label, self.do_toggle)
        self.menu.addSeparator()
        # 未設定/IDが実在しない状態を黙って隠さず、無効項目として見せる。
        # 表示するかどうかは実際のデバイスの有無を見て _refresh が決める。
        self._unset_action = self.menu.addAction(_UNSET_MENU_TEXT)
        self._unset_action.setEnabled(False)
        for device in self.devices:
            self.menu.addAction(device.get("label", "(名称未設定)"), lambda d=device: self._switch_to(d))
        self.menu.addSeparator()
        self._mic_action = self.menu.addAction(mic_label, self.do_mic_toggle)
        self._mic_action.setCheckable(True)
        self.menu.addSeparator()
        # 音声とキャプチャで settings.json を共有しており、デバイスIDは機器構成が
        # 変わると更新が必要になる(list_devices.py で新しいIDを確認して書き換える)。
        # その保守がこのメニューから完結するよう、Rapture側と同様に設定を開けるようにする。
        self.menu.addAction("設定", self._open_settings_file)
        self.menu.addSeparator()
        self.menu.addAction("終了", QApplication.instance().quit)

        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self._on_activated)
        # デバイスもミュートも他アプリ(Teams等)から変えられるため、開くたびに読み直す
        self.menu.aboutToShow.connect(self._refresh)
        self._refresh()
        self.tray_icon.setVisible(True)

    def hotkeys(self) -> dict:
        return {"audio_toggle": self.do_toggle, "mic_mute": self.do_mic_toggle}

    def _find_device(self, device_id: str):
        for device in self.devices:
            if device.get("id") == device_id:
                return device
        return None

    def _usable_devices(self) -> list:
        """settings.json のデバイスのうち、このPCに実在するものだけ。
        デバイスIDはPC固有のGUIDなので、設定を他PCへ持ち込むと全部ここで落ちる。"""
        return [d for d in self.devices if _device_exists(d.get("id"))]

    def _notify_unset(self):
        self.tray_icon.showMessage(
            "音声出力", f"{_UNSET_MENU_TEXT}。\n{_SETUP_HINT}", QSystemTrayIcon.Information, _NOTIFY_MS
        )

    def _refresh(self):
        # マイクが無い環境では get_mute() が None を返す。その場合はミュート表示をしない。
        mic_muted = mic_control.get_mute()
        self._mic_action.setEnabled(mic_muted is not None)
        self._mic_action.setChecked(bool(mic_muted))

        usable = self._usable_devices()
        self._unset_action.setVisible(not usable)

        if not usable:
            # 使えるデバイスが1つも無くてもアイコンは必ず出す(起動失敗に見せない)。
            device = {}
            label = "未設定"
        else:
            current_id = _get_current_device_id()
            device = self._find_device(current_id) if current_id else None
            if device is None:
                # 既定デバイスが取れない/設定外のデバイスが選ばれている。どれか1つと
                # 断定せず、色も図柄も持たない中立アイコンにする。
                device = {}
                label = "不明"
            else:
                label = device.get("label", "(名称未設定)")

        self.tray_icon.setIcon(pil_to_qicon(_make_icon_image(device, bool(mic_muted))))
        tooltip = f"音声出力: {label}"
        if mic_muted:
            tooltip += "\nマイク: ミュート中"
        self.tray_icon.setToolTip(tooltip)
        self._current_action.setText(f"現在: {label}")

    def do_toggle(self):
        """現在のデバイスの次の要素へ切り替える(末尾なら先頭へ循環)。3台以上でも動く。"""
        now = time.monotonic()
        if now - self._last_toggle_time < _TOGGLE_COOLDOWN:
            return
        self._last_toggle_time = now

        # 実在しないIDを SetDefaultDevice に渡すと COMError になるので、候補から外す。
        # クールダウンより後に判定するのは、未設定時の通知がキーリピートで連打されないため。
        devices = self._usable_devices()
        if not devices:
            self._notify_unset()
            self._refresh()
            return

        current_id = _get_current_device_id()
        ids = [d["id"] for d in devices]
        try:
            idx = ids.index(current_id)
        except ValueError:
            idx = -1
        self._switch_to(devices[(idx + 1) % len(devices)])

    def do_mic_toggle(self):
        """既定の録音デバイスのミュートを反転する。アイコンにも状態を反映する。"""
        muted = mic_control.toggle_mute()
        if muted is None:
            self.tray_icon.showMessage(
                "マイク", "既定の録音デバイスが見つかりません", QSystemTrayIcon.Information, _NOTIFY_MS
            )
            self._refresh()
            return
        # 成功時は通知を出さない。Windowsのトースト通知は通知システムを経由するため
        # 数秒遅れて出ることがあり、切り替えた後になって鳴るだけで邪魔になる。
        # ミュート状態はトレイアイコンの斜線が即座に変わるので、それが十分な合図。
        self._refresh()

    def _switch_to(self, device: dict):
        if not _set_default_device(device.get("id")):
            label = device.get("label", "(名称未設定)")
            self.tray_icon.showMessage(
                "音声出力", f"{label} に切り替えられませんでした。\n{_SETUP_HINT}",
                QSystemTrayIcon.Information, _NOTIFY_MS,
            )
        self._refresh()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.do_toggle()

    def _open_settings_file(self):
        """設定ダイアログUIは持たないため、settings.jsonを既定アプリ(メモ帳等)で開く。"""
        if self.settings_path and os.path.exists(self.settings_path):
            os.startfile(self.settings_path)
