# hotkeys.py
# keyboard ライブラリのコールバックは専用スレッドで実行されるため、そこから直接Qtの
# GUIを操作すると壊れる。コールバックではシグナルをemitするだけにし、実処理はQtの
# メインスレッド側のスロットで行う(Qtがスレッドをまたぐシグナルを自動でキューイングする)。
#
# 実処理は必ずここで try に入れて呼ぶ。PySide6 はスロットから例外が抜けるとプロセスごと
# 終わらせるので、ホットキー1つの不調で常駐アプリ全体が消えてしまう。しかも通常起動は
# pythonw.exe で標準エラーがどこにも出ないため、落ちた理由が何も残らない。
#
# ──────────────────────────────────────────────────────────────────────────
# 修飾キーの「押されたまま」問題について
#
# 休止から復帰したあと、W を単打しただけで画面が真っ白になった(ctrl+alt+w = 白画面の
# 暴発)。引き金は休止そのものではなく、復帰後の Ctrl+Alt+Del → ログインである。
# Ctrl+Alt+Del と Win+L は Windows の Secure Attention Sequence で、押し下げは
# 低レベルフックに見えるが、離した合図はセキュアデスクトップ側へ行き、こちらのフックには
# 届かない。休止せずに Ctrl+Alt+Del でログインし直しただけでも同じことが起きる。
#
# keyboard はOSに修飾キーの状態を問い合わせない。自分のフックが見てきたキーの上げ下げ
# だけから「いま何が押されているか」を組み立てている
# (site-packages/keyboard/__init__.py の _pressed_events と、
#  direct_callback / pre_process_event の hotkey = tuple(sorted(_pressed_events)))。
# 離した合図が届かなければ、その記憶は押されたまま残る。keyboard 側にロックや復帰を
# 扱うコードは見当たらない(resume / suspend / WM_POWERBROADCAST のいずれも無い)。
#
# そこで、ホットキーが成立した瞬間に GetAsyncKeyState でOSに訊き直し、要求している
# 修飾キーが本当に押されているかを確かめる。押されていなければ何もしない(暴発を握り潰す)。
# ──────────────────────────────────────────────────────────────────────────
import ctypes
import gc
import sys
import threading

import keyboard
from PySide6.QtCore import QObject, Signal

import action_log

# GetAsyncKeyState の仮想キーコード。最上位ビットが立っていれば「いま押されている」。
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# keyboard の組み合わせ表記 → 確かめる仮想キー。どれか1つ押されていればよい
# (Win は左右で別の仮想キーなので2つ並ぶ。Ctrl / Alt / Shift は左右をまとめた
#  仮想キーがあるので1つで足りる)。
#
# ここに無い名前は確認しない。left alt のような左右を指定した表記や、修飾キーでない
# キー(w や j)がそれに当たる。確認できないものを「押されていない」と決めつけると
# 正当な操作を捨てるので、知らない名前は素通りさせる。
_MODIFIER_VKS = {
    "ctrl": (VK_CONTROL,),
    "control": (VK_CONTROL,),
    "alt": (VK_MENU,),
    "shift": (VK_SHIFT,),
    "win": (VK_LWIN, VK_RWIN),
    "windows": (VK_LWIN, VK_RWIN),
    "cmd": (VK_LWIN, VK_RWIN),
    "super": (VK_LWIN, VK_RWIN),
    "meta": (VK_LWIN, VK_RWIN),
}

# ctypes は argtypes / restype を必ず明示する(このリポジトリの決まり。既定のままだと
# 64bit で値が切り詰められて落ちた実績がある)。GetAsyncKeyState の戻りは SHORT。
try:
    _user32 = ctypes.WinDLL("user32")
    _GetAsyncKeyState = _user32.GetAsyncKeyState
    _GetAsyncKeyState.argtypes = [ctypes.c_int]
    _GetAsyncKeyState.restype = ctypes.c_short
except (AttributeError, OSError) as _e:  # pragma: no cover  Windows以外/取得失敗
    _GetAsyncKeyState = None
    print(f"[tray-tools] GetAsyncKeyState を使えません: {_e}", file=sys.stderr)


def _async_key_state(vk: int) -> int:
    """その仮想キーが「いま押されているか」。押されていれば真。

    GetAsyncKeyState が使えない環境では押されていることにする。確かめられないときに
    「押されていない」と答えると、ホットキーが全部黙って効かなくなる。暴発を1つ
    通してしまうより、そちらのほうが困る。"""
    if _GetAsyncKeyState is None:
        return 1
    return _GetAsyncKeyState(vk) & 0x8000


def required_modifier_vks(combo: str) -> list:
    """組み合わせの文字列から、確かめるべき仮想キーの組を取り出す。

    返すのは [(VK_CONTROL,), (VK_MENU,)] のような並び。各組は「どれか1つ押されて
    いればよい」。修飾キーを含まない組み合わせ(将来 f13 のような単独キーを割り当てた
    場合)では空になり、確認は何も邪魔をしない。

    keyboard は "ctrl+k, ctrl+d" のような連続押しも書けるので、カンマで区切られて
    いたら最後の段だけを見る。成立した瞬間に押さえているのは最後の段の修飾キーで
    あって、前の段のものは既に離していておかしくない。"""
    last_step = str(combo).split(",")[-1]
    groups = []
    for part in last_step.split("+"):
        vks = _MODIFIER_VKS.get(part.strip().lower())
        if vks and vks not in groups:
            groups.append(vks)
    return groups


def _modifiers_really_held(groups) -> bool:
    """要求している修飾キーが、OSの目から見て全部押されているか。"""
    return all(any(_async_key_state(vk) for vk in group) for group in groups)


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




# 張り直しが同時に2つ走らないようにする。keyboard のコールバックスレッドから呼ぶので、
# 連打すれば重なりうる。重なっても壊れはしないが、記録が無駄に増えるし、片方が
# unhook_all した直後に片方が登録する、という読みにくい順番になる。
_rebind_lock = threading.Lock()


def _clear_stale_pressed_state() -> None:
    """keyboard が覚えている「押されたまま」を白紙に戻す。

    ここが今回の実質的な直しである。unhook_all() のソースを読むと、消しているのは
    フックとホットキーの表(blocking_keys / nonblocking_keys / blocking_hooks /
    handlers / *_hotkeys)だけで、_pressed_events には触っていない。つまり登録を
    張り直しても、「押されたまま」の記憶はそのまま残る。消すには _pressed_events を
    直接空にするしかない。

    放っておくと、暴発の逆向きの実害も出ると考えられる。hotkey は
    tuple(sorted(_pressed_events)) で作られるので、余分な Ctrl と Alt が残っている間は
    win+j のような別の組み合わせを押しても膨らんだ集合になり、登録済みの組み合わせと
    一致しない ── つまりホットキーが黙って効かなくなる。ソースを読んだうえでの推論で
    あって、実際にその状態を作って確かめたわけではない。

    private を触ることになるが、_pressed_events は keyboard のモジュール変数で、
    外から消す公開の入口が無い。残っている記憶を消す代わりに物理キーを送る
    (release / press / send)という手は採らない。あれは入力の合成であり、
    「キーもマウスも送らない」というこのリポジトリの根本方針に正面から反する。

    消したあとに本当に押されているキーがあっても困らない。離したときの処理は
    `if scan_code in _pressed_events` で守られていて、無ければ素通りする。
    次に押し下げればまた入る。

    keyboard の版によって名前が変わっても起動を続けられるよう、どれも getattr で
    確かめてから触る。"""
    lock = getattr(keyboard, "_pressed_events_lock", None)
    pressed = getattr(keyboard, "_pressed_events", None)
    if pressed is not None:
        if lock is not None:
            with lock:
                pressed.clear()
        else:
            pressed.clear()
    # 修飾キーの抑止判定用に別で持っている集合。こちらは suppress=True の
    # ホットキーを登録していないので今は使われないが、残しておく意味も無い。
    listener = getattr(keyboard, "_listener", None)
    active = getattr(listener, "active_modifiers", None)
    if active is not None:
        active.clear()


def _rebind(bridge: "HotkeyBridge", app_settings: dict, handlers: dict,
            on_error=None, reason: str = "") -> None:
    """「押されたまま」の記憶を捨てて、ホットキーを登録し直す。

    keyboard のコールバックスレッドから呼ばれる。ここから例外を投げ返すと、呼び元は
    keyboard の中(pre_process_event のループ)なので何が起きるか読めない。必ず飲み込む。

    GCを止めるのは init_keyboard の docstring にある事故と同じ型を踏むため。
    add_hotkey は keyboard の初期化(COMでキー名を引く)に入ることがあり、その最中に
    GCが comtypes のオブジェクトを片付けにくると access violation でプロセスが即死する
    (crash.log に実際のスタックがある)。名前テーブルは起動時に出来ているので通らない
    道かもしれないが、通らないと確かめたわけではないので、同じ手当てを置いておく。
    止めるのは数ミリ秒なので害はない。"""
    if not _rebind_lock.acquire(blocking=False):
        return
    try:
        gc.disable()
        try:
            _clear_stale_pressed_state()
            keyboard.unhook_all()
            registered = _register_hotkeys(bridge, app_settings, handlers, on_error)
        finally:
            gc.enable()
    except Exception as e:
        print(f"[tray-tools] ホットキーの張り直しに失敗しました: {e}", file=sys.stderr)
        _record("ホットキー張り直し失敗", f"{reason}  {e}"[:120])
        if on_error is not None:
            try:
                on_error("hotkey張り直し")
            except Exception:
                pass
        return
    finally:
        _rebind_lock.release()

    # 成功しても残す。何も残らないと、次にロックを跨いだあとで「ちゃんと効いたのか」
    # 「そもそも出番が無かったのか」を区別できない。
    _record("ホットキー張り直し",
            f"{reason} の修飾キーが実際には押されていない  {len(registered)}件: "
            + ",".join(registered))


def _record(action: str, detail: str) -> None:
    """1行残す。残せなくても呼び元の流れは止めない。"""
    try:
        action_log.record(action, detail, "hotkey")
    except Exception as e:
        print(f"[tray-tools] {action}: {detail} (記録できません: {e})", file=sys.stderr)


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
    _register_hotkeys(bridge, app_settings, handlers, on_error)
    return bridge


def _make_trigger(bridge: HotkeyBridge, app_settings: dict, handlers: dict,
                  on_error, name: str, combo: str):
    """keyboard に渡すコールバックを作る。

    中身は keyboard のコールバックスレッドで走る。修飾キーの確認をここで済ませるのが
    肝心で、Qtのスロット(dispatch)まで持ち越してはいけない。スレッドをまたぐシグナルは
    キューイングされるので、そこで訊き直すと、待っている間に利用者が指を離していた
    ぶんまで「押されていない」と判断し、正当な操作を捨ててしまう。"""
    groups = required_modifier_vks(combo)

    def trigger():
        try:
            # 修飾キーを含まない組み合わせ(f13 のような単独キー)は確かめようがない。
            ok = (not groups) or _modifiers_really_held(groups)
        except Exception as e:
            print(f"[tray-tools] 修飾キーの確認に失敗しました ({combo}): {e}", file=sys.stderr)
            # 確かめられないときは通す。ここで止めるとホットキーが全部黙って
            # 効かなくなるほうに倒れる。
            ok = True
        if not ok:
            # keyboard の記憶とOSの実際が食い違っている。暴発なので握り潰し、
            # ついでに記憶を白紙に戻す(逆向きの「効かない」も同じ食い違いが原因)。
            _rebind(bridge, app_settings, handlers, on_error, f"{name}={combo}")
            return
        try:
            bridge.triggered.emit(name)
        except Exception as e:
            print(f"[tray-tools] ホットキーの受け渡しに失敗しました ({name}): {e}",
                  file=sys.stderr)

    return trigger


def _register_hotkeys(bridge: HotkeyBridge, app_settings: dict, handlers: dict,
                      on_error=None) -> list:
    """設定にある組み合わせを keyboard に登録する。登録できた設定キー名を返す。

    空文字(= その機能のホットキーをOFFにしている)は登録しない。設定ファイル側で
    意図して空にしてあるものを拾うと、押していないキーで機能が動いてしまう。

    初回の登録と、張り直しの両方から呼ぶ。片方だけ直すと食い違うので、登録の手順は
    ここ1箇所に置く。"""
    registered = []
    hotkey_config = app_settings.get("hotkeys", {})
    for name in handlers:
        combo = hotkey_config.get(name)
        if not combo:
            continue
        try:
            keyboard.add_hotkey(
                combo, _make_trigger(bridge, app_settings, handlers, on_error, name, combo))
            registered.append(name)
        except Exception as e:
            print(f"[tray-tools] ホットキー登録に失敗しました ({name}: {combo}): {e}",
                  file=sys.stderr)
            if on_error is not None:
                on_error(f"hotkey登録 {name}={combo}")
    return registered
