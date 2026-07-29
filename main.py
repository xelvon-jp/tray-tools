# main.py
# tray-tools エントリポイント。QApplication起動・設定読込・Feature登録・ホットキー登録を行う。
#
# Featureの定義(ダックタイピングで十分なので抽象基底クラスは作らない):
#   Feature = トレイアイコンを1つ所有するもの。
#   - コンストラクタ __init__(self, app_settings, settings_path) で QSystemTrayIcon を
#     1つ構築して self.tray_icon に保持する
#   - hotkeys() メソッドで {"設定キー名": 呼び出す関数} の辞書を返す(不要なら空辞書)
#
# 機能を足すときにFeatureを増やすのは「アイコンの見た目を占有する状態」を持つ場合だけに限る。
# 単発の動作はアイコンを増やさず、既存Featureのメニュー項目にする。アイコンを持たない能力は
# 普通のモジュール(color_picker.py / keep_awake.py など)として書き、Featureがそれを呼ぶ。
import ctypes
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

import settings as settings_module
from feature_audio import AudioFeature
from feature_screen import ScreenFeature
from hotkeys import setup_hotkeys
from toast import show_toast

# トレイアイコンは音声用と画面用の2つだけ。増やさない方針(上のFeatureの定義を参照)。
FEATURE_CLASSES = [AudioFeature, ScreenFeature]

# 多重起動の検出に使うローカルソケット名。スタートアップからの自動起動に加えて
# ショートカットを手動で叩くと二重に立ち上がり、トレイアイコンが2組並ぶうえ
# グローバルホットキーが二重登録されて競合するため防ぐ。
SINGLE_INSTANCE_KEY = "traytools.single-instance"


def _is_already_running() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SINGLE_INSTANCE_KEY)
    connected = socket.waitForConnected(300)
    if connected:
        socket.disconnectFromServer()
    return connected


def _hold_single_instance_lock():
    """先着プロセスの目印となるサーバを立てる。戻り値は呼び出し側で参照を保持すること。
    listenに失敗しても起動は止めない(誰も待ち受けていないことは確認済みで、
    ロック機構の不調でアプリ自体が使えなくなる方が困る)。"""
    # 前回クラッシュで終わるとソケットが残り、以後ずっと起動できなくなる。
    # 接続できなかった＝誰も待ち受けていないので、残骸を消してからlistenする。
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)
    return server


def _notify_already_running(server):
    server.nextPendingConnection()  # 接続を回収するだけ。中身は使わない
    show_toast("tray-tools\nすでに起動しています")


def main():
    # このIDを設定しないと、他のPythonツールとタスクバー/通知領域で同一アプリ扱いされ
    # アイコンが混線することがある。QApplication生成前に呼ぶ必要がある。
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("traytools.app.1")
    except (AttributeError, OSError):
        pass

    app = QApplication(sys.argv)
    # 付箋ウインドウを全部閉じても常駐アプリごと終了しないようにする(必須)。
    app.setQuitOnLastWindowClosed(False)

    if _is_already_running():
        # 常駐中の側が「すでに起動しています」と知らせるので、こちらは黙って終わる。
        return
    instance_lock = _hold_single_instance_lock()

    icon_path = Path(__file__).resolve().parent / "icons" / "rapture.ico"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    app_settings = settings_module.load_settings()
    settings_module.cleanup_old_captures(app_settings.get("capture", {}))

    features = [cls(app_settings, settings_module.SETTINGS_PATH) for cls in FEATURE_CLASSES]

    # 2つ目の起動が黙って終わるだけだと「クリックしたのに何も起きない」と見えるので、
    # 常駐中のこちら側から知らせる。
    instance_lock.newConnection.connect(lambda: _notify_already_running(instance_lock))

    handlers = {}
    for feature in features:
        handlers.update(feature.hotkeys())
    # 戻り値のHotkeyBridgeはローカル変数として保持し続ける必要がある
    # (参照が無くなるとQObjectがGCされ、シグナル接続ごと消えてしまう)。
    hotkey_bridge = setup_hotkeys(app_settings, handlers)  # noqa: F841

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
