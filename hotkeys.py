# hotkeys.py
# keyboard ライブラリのコールバックは専用スレッドで実行されるため、そこから直接Qtの
# GUIを操作すると壊れる。コールバックではシグナルをemitするだけにし、実処理はQtの
# メインスレッド側のスロットで行う(Qtがスレッドをまたぐシグナルを自動でキューイングする)。
import sys

import keyboard
from PySide6.QtCore import QObject, Signal


class HotkeyBridge(QObject):
    triggered = Signal(str)


def setup_hotkeys(app_settings: dict, handlers: dict) -> HotkeyBridge:
    """handlers: {"設定キー名": 呼び出す関数} をまとめてホットキー登録する。
    登録に失敗しても(他アプリとの競合等)アプリ全体は起動を続ける。
    戻り値の HotkeyBridge は呼び出し側で参照を保持し続けること(GC対策)。"""
    bridge = HotkeyBridge()
    bridge.triggered.connect(lambda name: handlers[name]())

    hotkey_config = app_settings.get("hotkeys", {})
    for name in handlers:
        combo = hotkey_config.get(name)
        if not combo:
            continue
        try:
            keyboard.add_hotkey(combo, lambda n=name: bridge.triggered.emit(n))
        except Exception as e:
            print(f"[tray-tools] ホットキー登録に失敗しました ({name}: {combo}): {e}", file=sys.stderr)

    return bridge
