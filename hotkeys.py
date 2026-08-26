# hotkeys.py
# keyboard ライブラリのコールバックは専用スレッドで実行されるため、そこから直接Qtの
# GUIを操作すると壊れる。コールバックではシグナルをemitするだけにし、実処理はQtの
# メインスレッド側のスロットで行う(Qtがスレッドをまたぐシグナルを自動でキューイングする)。
#
# 実処理は必ずここで try に入れて呼ぶ。PySide6 はスロットから例外が抜けるとプロセスごと
# 終わらせるので、ホットキー1つの不調で常駐アプリ全体が消えてしまう。しかも通常起動は
# pythonw.exe で標準エラーがどこにも出ないため、落ちた理由が何も残らない。
import gc
import sys

import keyboard
from PySide6.QtCore import QObject, Signal


class HotkeyBridge(QObject):
    triggered = Signal(str)


def init_keyboard() -> None:
    """keyboard の内部初期化(キー名テーブルの構築)を、先に済ませておく。

    keyboard は最初の add_hotkey のときに init() を呼び、その中でCOMを使って
    キー名を引く。このアプリは音声デバイスの操作で pycaw(comtypes)のCOMオブジェクトを
    抱えるので、その解放がこの初期化と重なるとプロセスごと落ちる。実際 crash.log には

        Garbage-collecting
        comtypes/_post_coinit/unknwn.py in Release
        comtypes/_post_coinit/unknwn.py in __del__
        keyboard/_winkeyboard.py in get_event_names
        hotkeys.py in setup_hotkeys

    というスタックで access violation が記録されている。GCがいつ走るか次第なので、
    起きたり起きなかったりする(「時々落ちる」「win+Jが効かないことがある」の正体。
    setup_hotkeys の途中で死ぬので、ホットキーが登録されないまま常駐が消える)。

    対策は2つ重ねてある。COMオブジェクトがまだ無いうちに呼ぶこと(呼び出しは main() の
    早い段階)と、この中だけGCを止めること。前者だけでは、後から作られたCOMオブジェクトを
    たまたまこの最中にGCが片付けにきた場合に防げない。

    呼ぶのは keyboard._os_keyboard.init()。keyboard.init という公開の入口は無く、
    COMを触る名前テーブルの構築はプラットフォーム別モジュール(Windowsでは
    _winkeyboard)の init が持っている。crash.log に出ている keyboard/__init__.py の
    init は、そこへ中継しているリスナークラスのメソッドのほう。private を呼ぶことに
    なるが、この初期化を前倒しする方法が他に無い。

    失敗しても起動は続ける。ここで初期化できなくても、add_hotkey のときに改めて
    試されるだけで、状況が今より悪くなることはない。"""
    gc.disable()
    try:
        keyboard._os_keyboard.init()
    except Exception as e:
        print(f"[tray-tools] keyboard の初期化に失敗しました: {e}", file=sys.stderr)
    finally:
        gc.enable()


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
