# feature_audio.py
# 音声まわりのFeature。QSystemTrayIconを1つ持ち、出力デバイスのトグル切替(メニュー/
# ホットキー)と、マイクのミュート切替(mic_control.py に委譲)を提供する。
# (audio-switcher/main.py, audio_device.py を統合・一般化して移植)
import json
import os
import sys
import time

import comtypes
from pycaw.constants import DEVICE_STATE, AudioDeviceState, EDataFlow, ERole
from pycaw.utils import AudioUtilities
from PIL import Image, ImageDraw
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

import mic_control
from picker import PickerWindow
from qt_image import pil_to_qpixmap
from toast import show_toast

# eConsole/eMultimedia/eCommunications の全ロールに反映しないと、
# アプリによっては切替後も旧デバイスに音が出続けることがあるため全て設定する。
_ROLES = [ERole.eConsole, ERole.eMultimedia, ERole.eCommunications]

# ホットキー長押し時のOSキーリピートで do_toggle が連続発火するのを防ぐ最小間隔(秒)
_TOGGLE_COOLDOWN = 0.6

_DEFAULT_ICON_COLOR = "#6b7280"

# mic_control._AUDIO_ERRORS と同じ理由。pycaw/comtypes が投げる COMError は Exception 直下で
# OSError のサブクラスではないため、OSError だけを捕まえても素通りしてしまう。
# settings.json のデバイスIDはPCごとに異なるGUIDで、別PCへ持ち込むと必ず実在しないIDになり、
# COMError(-2147023728, '要素が見つかりません') で落ちる。pythonw起動では無言で死ぬため必ず捕まえる。
_AUDIO_ERRORS = (OSError, comtypes.COMError)

_UNSET_MENU_TEXT = "音声デバイスが未設定です"
_REGISTER_MENU_TEXT = "デバイスを登録/解除…"
_SETUP_HINT = f"メニューの「{_REGISTER_MENU_TEXT}」から登録してください"

# 登録時にアイコンの色を順に振る。設定で手直しできるが、既定のままでも
# デバイスごとに色が違う(=通知領域のアイコンで出力先が見分けられる)ようにする。
# setup.py の _ICON_COLORS と同じ並び。両方を触るときは揃えること。
_ICON_COLORS = ["#2563eb", "#ea580c", "#16a34a", "#9333ea", "#0891b2"]

# Windows が各エンドポイントに持たせている「機器の形」(PKEY_AudioEndpoint_FormFactor)。
# pycaw の AudioDevice.properties のキーは "{GUID} PID" を大文字にした文字列
# (AudioDevice.FriendlyName と同じ作り)。
_FORM_FACTOR_KEY = "{1DA5D803-D492-4EDD-8C23-E0C0FFEE7F0E} 0"

# EndpointFormFactor(winapi) → アイコンの図柄。名前から推測するより確実なので、
# 取れるならこちらを使う(0=RemoteNetworkDevice 1=Speakers 2=LineLevel 3=Headphones
# 4=Microphone 5=Headset 6=Handset 7=UnknownDigitalPassthrough 8=SPDIF
# 9=DigitalAudioDisplayDevice 10=UnknownFormFactor)。
#
# 【図柄を3種類に増やした理由】元は headphone / monitor の2つしかなく、
# 卓上スピーカーやUSB DACに当てるものが無かった(monitor にすると「モニタから音が出る」
# という嘘の絵になる)。実際に区別が要るのは「頭に着けるもの/机の上で鳴るもの/
# 画面から鳴るもの」の3つなので speaker を足してここで打ち止めにする。
# Bluetooth用の図柄は足していない。BluetoothイヤホンはFormFactorも Headphones/Headset
# で返ってきて headphone の絵が正しく、「無線かどうか」は出力先を選ぶときの手掛かりに
# ならないため(同じ機器がケーブルを挿すと別の絵になってしまう方が困る)。
_FORM_FACTOR_SHAPES = {
    0: "speaker",   # ネットワーク越しの出力先。机の上で鳴るものとして扱う
    1: "speaker",
    2: "speaker",   # ライン出力。挿した先は分からないのでスピーカー扱い
    3: "headphone",
    5: "headphone",  # ヘッドセット(マイク付き)も頭に着けるものなので同じ絵
    8: "speaker",   # S/PDIF。外部アンプ等
    9: "monitor",
}

# FormFactor が取れない/UnknownFormFactor のときの保険。名前に手掛かりがあれば拾う。
_HEADPHONE_HINTS = ["ヘッドホン", "ヘッドフォン", "ヘッドセット", "イヤホン", "イヤフォン",
                    "headphone", "headset", "earphone", "earbud", "airpods", "buds"]
_MONITOR_HINTS = ["モニター", "ディスプレイ", "monitor", "display", "hdmi", "displayport"]

# 一覧の各行に付ける印。同名のデバイスがあるときは、行にIDの末尾も出して区別できるようにする
# (「Digital Output」や「3 - EX-LDGCQ321HD」のような同名は珍しくない)。
_SHORT_ID_LENGTH = 12

_PICKER_TITLE = "音声デバイスの登録"
_PICKER_PLACEHOLDER = "絞り込み（↑↓で選択 / Enterで登録⇔解除 / Escで閉じる）"
_PICKER_HINT = (
    "Enter … 未登録なら登録し、登録済みなら登録を外します（Escで閉じる）\n"
    "切り替えの巡回順は［登録N］の番号順です。順を変えたいときは、外してから登録し直してください"
)


def _step_volume(up: bool, steps: int = 1) -> bool:
    """既定の出力デバイスの音量を、Windowsの音量キーと同じ刻みで上下させる。

    VolumeStepUp/Down を使うのは、刻み幅を自分で決めずに済むため。この環境では
    51段階で、音量キーやメディアキーを押したときと同じ動きになる。

    COMオブジェクトはこの関数の外へ出さない。持ち出すと親が先に解放され、
    解放済みのメモリを触って 0xC0000005 で落ちる(explorer_nav._with_explorer_window と
    同じ作法。このアプリの持病で、実際に何度も落ちている)。

    失敗しても呼ぶ側は何もできないので、真偽値を返すだけで例外は投げない。"""
    try:
        _ensure_com_initialized()
        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        for _ in range(max(1, int(steps))):
            if up:
                endpoint.VolumeStepUp(None)
            else:
                endpoint.VolumeStepDown(None)
        return True
    except _AUDIO_ERRORS:
        return False


def _current_volume():
    """(音量0.0〜1.0, ミュートか)。取れなければ (None, None)。

    COMオブジェクトを持ち出さないのは _step_volume と同じ理由。"""
    try:
        _ensure_com_initialized()
        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        return endpoint.GetMasterVolumeLevelScalar(), bool(endpoint.GetMute())
    except _AUDIO_ERRORS:
        return None, None


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


def _friendly_name(device_id: str):
    """IDに対応するデバイスのFriendlyName。取れなければ None。
    プロパティを読む分 _device_exists より重いが、読むのは指定した1台分だけなので
    全デバイス列挙(GetAllDevices)とは桁が違う。"""
    try:
        _ensure_com_initialized()
        device = AudioUtilities.GetDeviceEnumerator().GetDevice(device_id)
        return AudioUtilities.CreateDevice(device).FriendlyName
    except _AUDIO_ERRORS:
        return None


def _find_active_device_id(name: str):
    """FriendlyNameが完全一致する有効なデバイスがちょうど1台のときだけ、そのIDを返す。
    このPCには "Digital Output" や "3 - EX-LDGCQ321HD" のような同名デバイスが複数あり、
    曖昧なまま推測すると意図しない出力先へ切り替わって原因の分かりにくい事故になる。
    0台でも2台以上でも推測せず None を返す(前方一致・部分一致も使わない)。"""
    if not name:
        return None
    try:
        _ensure_com_initialized()
        matched = [d for d in AudioUtilities.GetAllDevices()
                   if d.state == AudioDeviceState.Active and d.FriendlyName == name]
    except _AUDIO_ERRORS:
        return None
    return matched[0].id if len(matched) == 1 else None


def _guess_shape(name: str, form_factor) -> str:
    """アイコンの図柄を機器の種類から決める。FormFactor が取れればそれを信じ、
    取れないときだけ名前の手掛かりで拾う。どちらも駄目なら speaker(音は出るので、
    少なくとも「何かから鳴る」絵にはなる)。"""
    shape = _FORM_FACTOR_SHAPES.get(form_factor)
    if shape:
        return shape
    lowered = (name or "").lower()
    if any(hint in lowered for hint in _HEADPHONE_HINTS):
        return "headphone"
    if any(hint in lowered for hint in _MONITOR_HINTS):
        return "monitor"
    return "speaker"


def _list_output_devices() -> list:
    """いま繋がっている音声出力デバイスを [{"name", "id", "shape"}, ...] で返す。
    取得できなければ空リスト。

    【重い。呼ぶのは「デバイスを登録/解除」を選んだときだけにすること】
    GetAllDevices は列挙した1台ごとにプロパティストアを総なめするため、メニューを
    開くたびに呼ぶとメニューが引っかかる。このPCでの実測は、有効(Active)な出力
    (eRender)だけに絞って 78ms、絞らない GetAllDevices() が 312ms。絞り込みを
    引数で渡しているのはこの差のため(無効なデバイスも録音デバイスも登録候補には
    ならないので、読む必要がない)。

    【COMオブジェクトをこの関数の外に出さないこと】
    explorer_nav._with_explorer_window と同じ理由。comtypes のオブジェクトは
    CoInitialize したスレッドに紐づいており、関数の外へ持ち出すと別スレッドのGCが
    解放してプロセスごと落ちる(アクセス違反でPythonの例外にならず、error.log にも
    何も残らない)。ここでは文字列だけを抜き出して返し、AudioDevice はこの関数の
    ローカル変数のまま捨てる。"""
    found = []
    try:
        _ensure_com_initialized()
        devices = AudioUtilities.GetAllDevices(
            EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value
        )
        for device in devices:
            name = device.FriendlyName
            if not name or not device.id:
                continue
            found.append({
                "name": str(name),
                "id": str(device.id),
                "shape": _guess_shape(str(name), device.properties.get(_FORM_FACTOR_KEY)),
            })
    except _AUDIO_ERRORS:
        return []
    return found


def _short_id(device_id: str) -> str:
    """エンドポイントIDの末尾だけ。同名のデバイスを行の上で見分けるための札。
    IDは "{0.0.0.00000000}.{GUID}" 形式で、違うのは後ろのGUIDだけなので末尾を採る。"""
    return (device_id or "").rstrip("}")[-_SHORT_ID_LENGTH:]


def _draw_headphone(draw: ImageDraw.ImageDraw, color: str) -> None:
    draw.arc((14, 10, 50, 44), start=180, end=360, fill=color, width=5)
    draw.rounded_rectangle((11, 30, 23, 50), radius=4, fill=color)
    draw.rounded_rectangle((41, 30, 53, 50), radius=4, fill=color)


def _draw_speaker(draw: ImageDraw.ImageDraw, color: str) -> None:
    """卓上スピーカー/USB DAC 用。通知領域では実質16px相当まで縮むので、
    音波は2本までにして線を太く保つ(3本にすると潰れて塊に見える)。"""
    draw.polygon([(14, 26), (24, 26), (36, 14), (36, 50), (24, 38), (14, 38)], fill=color)
    draw.arc((32, 20, 46, 44), start=300, end=60, fill=color, width=4)
    draw.arc((36, 13, 56, 51), start=300, end=60, fill=color, width=4)


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
    elif shape == "speaker":
        _draw_speaker(draw, "white")
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
        # 直近に作ったアイコン図柄。タスクバーウィジェットが同じ絵を描くために読む
        # (current_icon_pixmap を参照)。_refresh が必ず入れ替える。
        self._icon_pixmap = None
        # 登録/解除の選択ウインドウ。開いている間はここで参照を持つ
        # (ローカル変数だけだとGCで即消える。feature_screen の定型文ピッカーと同じ)。
        self._device_picker = None
        # デバイス行のQAction。登録/解除のたびに作り直すので、消す対象を覚えておく。
        self._device_actions = []

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
        # デバイス行はこの区切り線の直前に差し込む。登録/解除で作り直すので、
        # 差し込む位置の目印としてQActionを覚えておく(addActionは末尾に足すため、
        # 作り直すときに元の位置へ戻せない)。
        self._devices_end = self.menu.addSeparator()
        self._rebuild_device_actions()
        self._mic_action = self.menu.addAction(mic_label, self.do_mic_toggle)
        self._mic_action.setCheckable(True)
        self.menu.addSeparator()
        # 繋がっているデバイスを選んで登録する窓を開く。IDは自動で埋まるので、
        # 別のPCへ移ったときも settings.json を手で書かなくて済む。
        self.menu.addAction(_REGISTER_MENU_TEXT, self._open_device_picker)
        # 音声とキャプチャで settings.json を共有しており、ラベルや色の手直しなど
        # 上の窓では届かない調整もある。Rapture側と同様に設定ファイルも開けるようにする。
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

    def _rebuild_device_actions(self):
        """メニューのデバイス行を今の self.devices から作り直す。

        登録/解除でデバイスが増減するため、コンストラクタで並べたきりにできない。
        呼ぶのはメニューが閉じている間だけ(登録/解除はメニューの項目を選んだ後に
        起きるので、閉じた後になる)。

        triggered.connect に渡すハンドラは checked(bool) を受け取ることに注意。
        menu.addAction(テキスト, 関数) は引数なしで呼ぶが、こちらは違う。引数の数を
        間違えると押すたびに TypeError で落ちる(実績あり)ので、既定値付きで受ける。"""
        for action in self._device_actions:
            self.menu.removeAction(action)
            action.deleteLater()
        self._device_actions = []
        for device in self.devices:
            action = QAction(device.get("label", "(名称未設定)"), self.menu)
            action.triggered.connect(lambda checked=False, d=device: self._switch_to(d))
            self.menu.insertAction(self._devices_end, action)
            self._device_actions.append(action)

    def _find_device(self, device_id: str):
        for device in self.devices:
            if device.get("id") == device_id:
                return device
        return None

    def _usable_devices(self) -> list:
        """settings.json のデバイスのうち、このPCに実在するものだけ。
        デバイスIDはPC固有のGUIDなので、設定を他PCへ持ち込むと全部ここで落ちる。"""
        return [d for i, d in enumerate(self.devices) if self._resolve_device(i, d)]

    def _resolve_device(self, index: int, device: dict) -> bool:
        """設定のデバイスを、このPCの実デバイスへ結び付ける。使えるなら True。

        IDが実在する間はそのまま使い、ついでに現在のFriendlyNameを match_name に控える。
        label はユーザーがメニュー用に付けた名前でWindowsの名前とは一致しない(実例:
        "ヘッドホン (Loop120)" と "ヘッドホン (Loop120 by Shokz)")ため照合には使えず、
        IDが変わってから名前を調べる術は無いので、使えているうちに控えておく。"""
        if _device_exists(device.get("id")):
            name = _friendly_name(device.get("id"))
            if name and name != device.get("match_name"):
                device["match_name"] = name
                self._save_device_identity(index, device)
            return True

        # ここへ来るのはIDが実在しないときだけ。全デバイス列挙は重いので、
        # メニューを開くたびに通る通常経路には入れない。
        new_id = _find_active_device_id(device.get("match_name"))
        if not new_id:
            return False
        device["id"] = new_id
        self._save_device_identity(index, device)
        label = device.get("label", "(名称未設定)")
        # IDが黙って書き換わると挙動が読めなくなるので、追従したことは必ず見せる。
        show_toast(f"音声出力\n{label} のIDが変わっていたため追従しました")
        return True

    def _save_device_identity(self, index: int, device: dict) -> None:
        """settings.json を読み直し、該当デバイスの id と match_name だけを書き戻す。
        メモリ上の設定はデフォルト値をマージ済みで、丸ごと書き出すと未設定の既定値まで
        明示的に書かれてファイルの姿が変わってしまう。label は重複しうるので、対象は
        配列のインデックスで特定する。書けなくても動作は続ける(次回また解決すればよい)。"""
        if not self.settings_path:
            return
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            entry = stored["audio"]["devices"][index]
            entry["id"] = device.get("id")
            if device.get("match_name"):
                entry["match_name"] = device["match_name"]
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(stored, f, ensure_ascii=False, indent=2)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            pass

    # ---------------------------------------------------------------
    # デバイスの登録/解除
    # ---------------------------------------------------------------
    def _rewrite_devices(self, mutate) -> bool:
        """settings.json を読み直し、audio.devices の配列だけを差し替えて書き戻す。
        成否を返す。_save_device_identity と同じ作法で、他のキーには一切触らない。

        mutate は「ファイル上の配列」を受け取り、書き戻す配列を返す関数。メモリ上の
        設定ではなくファイルから読んだ側を編集するのが肝で、メモリ上のものは
        デフォルト値をマージ済みなので、丸ごと書き出すと未設定の既定値まで明示的に
        書かれてファイルの姿が変わってしまう。ファイル側を編集すれば、ユーザーが手で
        直した label も、ここが知らない独自のキーも、触らなかった項目はそのまま残る。
        書き戻せないときは None を返せば中止できる。

        メモリ上の配列とファイル上の配列は添字で対応している(load_settings の
        deep merge は辞書だけを再帰的に重ね、配列は丸ごと置き換えるため)。長さが
        食い違うのは、起動後に人がファイルを書き換えたということ。その状態で添字を
        頼りに消すと別のデバイスを消しかねないので、何もせず False を返す。"""
        if not self.settings_path:
            return False
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if not isinstance(stored, dict):
                return False
            audio = stored.setdefault("audio", {})
            entries = audio.get("devices")
            if not isinstance(entries, list):
                entries = []
            if len(entries) != len(self.devices):
                return False
            new_entries = mutate(entries)
            if new_entries is None:
                return False
            audio["devices"] = new_entries
            # 改行の扱いは _save_device_identity に合わせる(このファイルは既に
            # そちらが書いており、ここだけ流儀を変えると差分が全行に出る)。
            with open(self.settings_path, "w", encoding="utf-8") as f:
                json.dump(stored, f, ensure_ascii=False, indent=2)
            return True
        except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError):
            return False

    def _next_color(self) -> str:
        """新しく登録するデバイスの色。まだ使っていない色を先頭から選ぶ。
        通知領域のアイコンは色で出力先を見分けるので、同じ色が並ぶと意味を失う。"""
        used = {str(d.get("color", "")).lower() for d in self.devices}
        for color in _ICON_COLORS:
            if color not in used:
                return color
        return _ICON_COLORS[len(self.devices) % len(_ICON_COLORS)]

    def _register_device(self, info: dict) -> bool:
        """繋がっているデバイス1台を settings.json の末尾へ登録する。成否を返す。

        label はWindowsが報告する名前をそのまま入れる。手で短くしたくなる場所だが、
        こちらで勝手に切り詰めると別のデバイスと見分けが付かなくなることがあるので、
        まずは正確な名前を置く(短くするのはユーザーの仕事で、直しても match_name が
        別に控えてあるので照合は壊れない)。
        id と match_name はここで自動的に埋まる。これが手で書くと苦行だった部分。"""
        entry = {
            "label": info["name"],
            "id": info["id"],
            "icon": info["shape"],
            "color": self._next_color(),
            "match_name": info["name"],
        }

        def mutate(entries):
            # ファイル側とメモリ側で別の辞書にする(同じ辞書を共有すると、以後の
            # match_name の書き戻しが片方だけ更新されたように見えて混乱する)。
            entries.append(dict(entry))
            return entries

        if not self._rewrite_devices(mutate):
            return False
        self.devices.append(entry)
        return True

    def _unregister_device(self, index: int) -> bool:
        """登録済みデバイス1台を settings.json から外す。成否を返す。"""
        if not 0 <= index < len(self.devices):
            return False
        device_id = self.devices[index].get("id")

        def mutate(entries):
            entry = entries[index]
            # 添字だけを頼りにしない。同名のデバイスが複数ある環境なので、消す前に
            # IDが一致することを必ず確かめる(食い違ったら中止)。
            if not isinstance(entry, dict) or entry.get("id") != device_id:
                return None
            del entries[index]
            return entries

        if not self._rewrite_devices(mutate):
            return False
        del self.devices[index]
        return True

    def _picker_rows(self) -> list:
        """選択ウインドウに並べる行を作る。全デバイスの列挙はここで一度だけ走る。

        並べるのは「いま繋がっている出力デバイス」＋「登録済みだが今は繋がっていない
        もの」。後者を出さないと、電源を切ったヘッドホンの登録を外せなくなる。"""
        connected = _list_output_devices()
        connected_ids = {info["id"] for info in connected}
        index_by_id = {d.get("id"): i for i, d in enumerate(self.devices) if d.get("id")}
        current_id = _get_current_device_id()

        rows = []
        for info in connected:
            index = index_by_id.get(info["id"])
            rows.append({
                "name": info["name"],
                "id": info["id"],
                "shape": info["shape"],
                "index": index,
                "device": self.devices[index] if index is not None else None,
                "connected": True,
                "current": bool(current_id) and info["id"] == current_id,
                "info": info,
            })
        for index, device in enumerate(self.devices):
            if device.get("id") in connected_ids:
                continue
            rows.append({
                # 繋がっていないのでWindows側の名前は今は聞けない。控えてある
                # match_name を優先し、無ければメニュー用のラベルで代用する。
                "name": device.get("match_name") or device.get("label") or "(名称未設定)",
                "id": device.get("id") or "",
                "shape": device.get("icon"),
                "index": index,
                "device": device,
                "connected": False,
                "current": False,
                "info": None,
            })
        return rows

    def _picker_items(self, rows: list) -> list:
        """行を PickerWindow 用の [(表示名, データ), ...] に整える。

        表示名の先頭はデバイス名そのものにする。ピッカーの絞り込みは前方一致なので、
        印を頭に付けると名前で絞り込めなくなる。印は後ろに回す。

        同名のデバイスがあるときは、その行にだけIDの末尾を出す。"Digital Output" や
        "3 - EX-LDGCQ321HD" のような同名は珍しくなく、どちらを登録したのか分からない
        まま選ぶと、意図しない出力先が登録されて原因の分かりにくい事故になる。
        常に出さないのは、普段は名前だけの方が読みやすいため(IDの全体はプレビューに
        必ず出しているので、確かめる手段は常にある)。"""
        names = [row["name"] for row in rows]
        duplicated = {name for name in names if names.count(name) > 1}

        items = []
        for row in rows:
            marks = []
            if row["index"] is not None:
                marks.append(f"登録{row['index'] + 1}")
            else:
                marks.append("未登録")
            if row["current"]:
                marks.append("使用中")
            if not row["connected"]:
                marks.append("未接続")
            text = row["name"]
            if row["name"] in duplicated:
                text += f"  ID:…{_short_id(row['id'])}"
            text += "  ［" + "・".join(marks) + "］"
            items.append((text, row))
        return items

    def _picker_preview(self, row: dict) -> str:
        """選択中の行の内訳。IDの全体をここに必ず出す(同名のデバイスを最後に
        見分ける手段であり、行の表示だけでは足りない場合があるため)。"""
        device = row["device"]
        if not row["connected"]:
            state = "未接続（登録だけが残っています）"
        elif row["current"]:
            state = "接続中（いまの出力先）"
        else:
            state = "接続中"
        lines = [
            f"Windows上の名前: {row['name']}",
            f"エンドポイントID: {row['id'] or '(不明)'}",
            f"状態: {state}",
        ]
        if device is None:
            lines.append("登録: なし")
            lines.append(f"アイコン(自動判定): {row['shape']}")
            lines.append("")
            lines.append(f"Enterで登録します（切り替えの巡回順は {len(self.devices) + 1} 番目になります）")
        else:
            lines.append(f"登録: {row['index'] + 1} 番目（切り替えはこの順に巡回します）")
            lines.append(f"メニューのラベル: {device.get('label', '(名称未設定)')}")
            lines.append(f"アイコン: {device.get('icon', '(なし)')} / 色: {device.get('color', '(なし)')}")
            lines.append("")
            lines.append("Enterで登録を外します（ラベルや色の手直しも一緒に消えます）")
        return "\n".join(lines)

    def _open_device_picker(self):
        """繋がっているデバイスを一覧から選んで登録/解除する窓を開く。

        【全デバイスの列挙が走るのはここだけ】_list_output_devices は重い。
        この項目を選んだときに一度だけ読み、開いている間は読み直さない。
        メニューを開くたびに走る _refresh の側からは絶対に呼ばないこと。

        UIにチェック付きのメニュー項目ではなくこの窓を選んだ理由:
        デバイスは同名のものが複数あり、IDの末尾やプレビューといった手掛かりを
        添えないと選べない。メニュー項目1行にはそれだけの情報が入らない。
        絞り込みも効くので、10台以上並ぶPCでも探せる。"""
        if self._device_picker is not None:
            # 開いたまま同じ項目を選んだときは、開き直さず前面に呼び戻す
            self._device_picker.raise_()
            self._device_picker.activateWindow()
            return
        try:
            rows = self._picker_rows()
        except Exception as e:
            # Qtのスロット内で例外を投げ切ると常駐ごと落ちる。ここで止める。
            print(f"[tray-tools] 音声デバイスを列挙できません: {e}", file=sys.stderr)
            show_toast("音声出力\nデバイスの一覧を取得できませんでした")
            return
        if not rows:
            show_toast("音声出力\n出力デバイスが見つかりませんでした")
            return

        window = PickerWindow(
            _PICKER_TITLE,
            self._picker_items(rows),
            self._on_picker_accept,
            placeholder=_PICKER_PLACEHOLDER,
            preview_provider=self._picker_preview,
            hint=_PICKER_HINT,
        )
        window.closed.connect(self._close_device_picker)
        self._device_picker = window
        window.show()

    def _close_device_picker(self):
        if self._device_picker is None:
            return
        window = self._device_picker
        self._device_picker = None
        window.close()
        window.deleteLater()

    def _on_picker_accept(self, _name: str, row: dict):
        """一覧で決定したときの処理。未登録なら登録し、登録済みなら外す。

        PickerWindow は決定すると必ず閉じるので、続けて何台も登録できるよう
        開き直す。閉じ切ってから作らないと、閉じかけの窓と入れ替わって前面が
        定まらないため、singleShot(0) で今のイベント処理の後ろへ回す。"""
        if row["index"] is None:
            changed = self._register_device(row["info"])
            failed = "登録できませんでした"
        else:
            changed = self._unregister_device(row["index"])
            failed = "登録を外せませんでした"
        if not changed:
            show_toast(f"音声出力\n{failed}。設定ファイルを確認してください")
            return
        self._rebuild_device_actions()
        self._refresh()
        QTimer.singleShot(0, self._open_device_picker)

    def _notify_unset(self):
        show_toast(f"音声出力\n{_UNSET_MENU_TEXT}。{_SETUP_HINT}")

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

        # QPixmapを経由するのは、同じ図柄をタスクバーウィジェットにも描かせるため。
        # QIconからは元の絵を取り戻せない(pixmap(size)で作り直すと縮尺が変わる)。
        self._icon_pixmap = pil_to_qpixmap(_make_icon_image(device, bool(mic_muted)))
        self.tray_icon.setIcon(QIcon(self._icon_pixmap))
        tooltip = f"音声出力: {label}"
        if mic_muted:
            tooltip += "\nマイク: ミュート中"
        self.tray_icon.setToolTip(tooltip)
        self._current_action.setText(f"現在: {label}")

    def current_icon_pixmap(self, refresh: bool = False):
        """いまトレイに出しているのと同じ図柄を QPixmap で返す。まだ無ければ None。

        セカンダリのタスクバーウィジェット(taskbar_widget.py)が、通知領域の代わりに
        このアイコンを自前で描くために使う。「トレイの代わり」である以上、色も形も
        本物と食い違ってはいけないので、あちらで描き直さずここで作ったものを渡す。

        refresh=True で状態を読み直してから返す。デバイスは他アプリ(Teams等)からも
        変えられるので、ウィジェットにマウスを乗せた瞬間だけ読み直させる用。COM越しの
        問い合わせが入るため、描画のたびに呼ばないこと(メニューを開くのと同じ重さ)。"""
        if refresh:
            self._refresh()
        return self._icon_pixmap

    def step_volume(self, up: bool, steps: int = 1):
        """音量を上下させて、いまの音量を返す(取れなければ None)。

        タスクバーウィジェットの音声アイコンでホイールを回したときに呼ばれる。
        トレイアイコン側では受けられない(QSystemTrayIcon は QWidget ではないので
        wheelEvent を持たず、Windowsもトレイへホイールを転送しない)。"""
        if not _step_volume(up, steps):
            return None
        level, _muted = _current_volume()
        return level

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
        if len(devices) < 2:
            # 候補が1台だと「次のデバイス」が自分自身になり、押しても何も起きない。
            # 黙って無反応だと故障と区別がつかないので、消えている登録を名指しで知らせる。
            # 主な原因は、USBドングルを挿すポートが変わる・ドライバが更新される等で
            # エンドポイントIDが振り直され、設定に書いたIDが実在しなくなること
            # (Bluetoothの再ペアリングでも同様に起きる)。match_name があれば
            # _resolve_device が名前で追従するので、ここまで来るのは追従できない場合。
            missing = [d.get("label", "(名称未設定)") for d in self.devices
                       if not _device_exists(d.get("id"))]
            detail = "、".join(missing) if missing else "他のデバイス"
            show_toast(
                f"音声出力\n切り替え先がありません（{detail} が見つかりません）。\n"
                f"接続を確認するか、メニューの「{_REGISTER_MENU_TEXT}」で登録し直してください"
            )
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
            show_toast("マイク\n既定の録音デバイスが見つかりません")
            self._refresh()
            return
        # 成功時は通知を出さない。ミュート状態はトレイアイコンの斜線が即座に変わるので、
        # それが十分な合図であり、いちいち通知が出る方が冗長になる。
        self._refresh()

    def _switch_to(self, device: dict):
        if not _set_default_device(device.get("id")):
            label = device.get("label", "(名称未設定)")
            show_toast(f"音声出力\n{label} に切り替えられませんでした。{_SETUP_HINT}")
        self._refresh()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.do_toggle()

    def _open_settings_file(self):
        """設定ダイアログUIは持たないため、settings.jsonを既定アプリ(メモ帳等)で開く。"""
        if self.settings_path and os.path.exists(self.settings_path):
            os.startfile(self.settings_path)
