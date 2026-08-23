# hotkeys.py
# keyboard ライブラリのコールバックは専用スレッドで実行されるため、そこから直接Qtの
# GUIを操作すると壊れる。コールバックではシグナルをemitするだけにし、実処理はQtの
# メインスレッド側のスロットで行う(Qtがスレッドをまたぐシグナルを自動でキューイングする)。
#
# 実処理は必ずここで try に入れて呼ぶ。PySide6 はスロットから例外が抜けるとプロセスごと
# 終わらせるので、ホットキー1つの不調で常駐アプリ全体が消えてしまう。しかも通常起動は
# pythonw.exe で標準エラーがどこにも出ないため、落ちた理由が何も残らない。
import sys

import keyboard
from PySide6.QtCore import QObject, Signal


class HotkeyBridge(QObject):
    triggered = Signal(str)


def setup_hotkeys(app_settings: dict, handlers: dict, on_error=None) -> HotkeyBridge:
    """handlers: {"設定キー名": 呼び出す関数} をまとめてホットキー登録する。
    登録に失敗しても(他アプリとの競合等)アプリ全体は起動を続ける。
    戻り値の HotkeyBridge は呼び出し側で参照を保持し続けること(GC対策)。

    on_error(場所) を渡すと、ホットキーの実処理で例外が出たときに呼ばれる
    (記録と通知は呼び出し側の担当。ここはQtのスロットなので、投げ返さず必ず飲み込む)。"""
    bridge = HotkeyBridge()

    def dispatch(name):
        handler = handlers.get(name)
        if handler is None:
            return
        try:
            handler()
        except Exception:
            if on_error is not None:
                on_error(f"hotkey={name}")
            else:
                print(f"[tray-tools] ホットキーの実行に失敗しました ({name})", file=sys.stderr)

    bridge.triggered.connect(dispatch)

    hotkey_config = app_settings.get("hotkeys", {})
    for name in handlers:
        combo = hotkey_config.get(name)
        if not combo:
            continue
        try:
            keyboard.add_hotkey(combo, lambda n=name: bridge.triggered.emit(n))
        except Exception as e:
            print(f"[tray-tools] ホットキー登録に失敗しました ({name}: {combo}): {e}", file=sys.stderr)
            if on_error is not None:
                on_error(f"hotkey登録 {name}={combo}")

    return bridge
