# explorer_nav.py
# エクスプローラ(Windowsのファイルウインドウ)とのやり取りだけを持つ部品。トレイアイコンは
# 持たず、表示や通知は呼び出し側(launcher.py)が行う(window_tools.py と同じ立ち位置)。
#
# 「今開いているフォルダを知る」「そのウインドウを別のフォルダへ移動させる」の2つができる。
# どちらも Shell.Application の ShellWindows(COM)越しで、user32 だけでは届かない。
#
# この関数群は例外を投げない(取れなければ None / 失敗なら False を返す)。呼び出し元は
# Qtのスロットで、PySide6 はスロットから例外が抜けると常駐アプリごと終了するため。
import ctypes
import os
import urllib.parse

import comtypes
import comtypes.client

import window_tools

# エクスプローラのフォルダウインドウのウインドウクラス名。デスクトップ(Progman)や
# ファイルを開くダイアログは別クラスなので、これだけを対象にする。
EXPLORER_CLASS = "CabinetWClass"

# ShellWindows は エクスプローラと Internet Explorer の両方を列挙する。IEに Navigate すると
# 見当違いのページを開かせてしまうので、実行ファイル名で必ずふるい落とす。
_EXPLORER_EXE = "explorer.exe"

# comtypes が投げる COMError は Exception 直下で OSError のサブクラスではないため、
# OSError だけを捕まえても素通りしてしまう(mic_control._AUDIO_ERRORS と同じ理由)。
# エクスプローラは列挙している最中に閉じられることがあり、そのとき COMError になる。
# AttributeError も併せて捕まえるのは、comtypes の遅延バインディングでは実際に呼ぶまで
# メンバの有無が分からないため(w.Document.Folder.Self.Path はここで落ちる。だから
# パスは LocationURL から組み立てている)。
_SHELL_ERRORS = (OSError, comtypes.COMError, AttributeError)

_user32 = ctypes.windll.user32

_user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int

# クラス名の受け皿。Windowsのクラス名は256文字までと決まっている。
_CLASS_NAME_BUFFER = 256


def _ensure_com_initialized() -> None:
    # feature_audio / mic_control と同じ作法。keyboardライブラリのホットキーコールバックは
    # 専用スレッドで実行され、そのスレッドではCOMが未初期化のまま呼ぶと失敗する。
    # (現状ここへ来るのはQtのメインスレッド経由だけだが、入口が増えても壊れないようにする)
    try:
        comtypes.CoInitialize()
    except OSError:
        pass


def foreground_hwnd() -> int:
    """前面ウィンドウのHWND。取れなければ 0。

    HWNDの取得は window_tools に既にある(64bit環境で戻り値がintに切り詰められる罠の
    対処込み)ので、そちらを使う。c_void_p はNULLを None で返すので 0 に均しておく。"""
    return window_tools.get_foreground_window() or 0


def window_class(hwnd: int) -> str:
    """ウィンドウクラス名。無効なHWNDなら空文字。"""
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(_CLASS_NAME_BUFFER)
    if not _user32.GetClassNameW(hwnd, buffer, _CLASS_NAME_BUFFER):
        return ""
    return buffer.value


def is_explorer(hwnd: int) -> bool:
    """エクスプローラのフォルダウインドウかどうか。"""
    return window_class(hwnd) == EXPLORER_CLASS


def _with_explorer_window(hwnd: int, action):
    """該当HWNDのエクスプローラを探し、見つけたら action(window) の戻り値を返す。
    見つからない・失敗したときは None。

    【COMオブジェクトをこの関数の外に出さないこと】
    ここが肝で、以前は「該当ウインドウを見つけて返す」形にしていたところ、
    Shell.Application を変数に受けずに .Windows() だけを返していたために、
    その行を抜けた瞬間に親オブジェクトの参照カウントが0になって解放され、
    解放済みメモリを触ってアクセス違反(0xC0000005)でプロセスごと落ちていた。
    Pythonの例外ではないので error.log にも何も残らず、しかも解放の間に合い方
    次第で落ちたり落ちなかったりする。
    shell・windows・window をすべてこの関数のローカルに置き、action を呼び終える
    まで生かしておくことで、寿命の逆転を起こさない。

    毎回 CreateObject する。エクスプローラを再起動(タスクマネージャからの再起動や、
    explorer.exe のクラッシュ)すると、それ以前に取った参照は無効になり、以後ずっと
    COMError を返す抜け殻になる。ブックマークを選んだときにしか呼ばない=1操作につき
    1回なので、掴み続けて古くなる危険を負ってまで使い回す価値が無い。"""
    if not hwnd:
        return None
    try:
        _ensure_com_initialized()
        shell = comtypes.client.CreateObject("Shell.Application")
        windows = shell.Windows()
        count = windows.Count
    except _SHELL_ERRORS:
        return None

    for index in range(count):
        # 1枚ずつ受け止める。列挙中に閉じられた1枚で全体を諦めない。
        try:
            window = windows.Item(index)
            if window is None:
                continue
            if not (window.FullName or "").lower().endswith(_EXPLORER_EXE):
                continue  # Internet Explorer 側
            if int(window.HWND) != int(hwnd):
                continue
            # shell / windows はまだこのスコープで生きている。ここで用を済ませる。
            return action(window)
        except _SHELL_ERRORS:
            continue
    return None


def _path_from_location_url(url: str):
    """LocationURL('file:///C:/Users/...')をWindowsのパスに直す。file: 以外なら None。

    日本語やスペースを含むパスは %E3%83%89 のようにURLエンコードされて入っているので、
    unquote を通さないと化けたパスができあがる。
    コントロールパネル・ごみ箱・「PC」などの特殊フォルダは file: ではない(あるいは
    LocationURL が空)。実体のあるフォルダではないので None を返す。"""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "file":
        return None
    path = urllib.parse.unquote(parsed.path)
    if parsed.netloc:
        # ネットワークパス(file://server/share/...)。UNCの \\server\share へ戻す。
        return os.path.normpath("//" + urllib.parse.unquote(parsed.netloc) + path)
    # 'file:///C:/...' の path は '/C:/...' と先頭にスラッシュが付く。落としてから正規化する。
    return os.path.normpath(path.lstrip("/"))


def explorer_path(hwnd: int):
    """そのエクスプローラが今開いているフォルダのパス。取れなければ None。

    パスは LocationURL から組み立てる。w.Document.Folder.Self.Path で直接取る手もあるが、
    comtypes の遅延バインディングでは AttributeError になって届かない。

    取り出すのは文字列だけ。COMオブジェクトは _with_explorer_window の中に置いてくる
    (外へ持ち出すと寿命が逆転してアクセス違反になる。詳細はあちらのコメント)。"""
    return _with_explorer_window(hwnd, lambda window: _path_from_location_url(window.LocationURL))


def navigate(hwnd: int, path: str) -> bool:
    """そのエクスプローラを path へ移動させる。成否を返す。

    新しい窓を開かず、今見ている窓の中身だけを差し替える(あふｗの -s -p と同じ考え方)。"""
    if not path:
        return False

    def go(window):
        window.Navigate(path)
        return True

    return bool(_with_explorer_window(hwnd, go))
