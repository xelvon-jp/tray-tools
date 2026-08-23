# launcher.py
# フォルダブックマークと、選んだフォルダへの移動指示。トレイアイコンは持たない
# 部品で、開閉の管理は feature_screen 側が行う(snippets.py と同じ形)。
#
# 移動先は二画面ファイラ「あふｗ」と、Windowsのエクスプローラの2つ(settings.json の
# launcher.target で選ぶ。既定の auto は、ブックマークを開いた時に前面だったウインドウが
# エクスプローラならそこを移動させ、それ以外ならあふｗへ送る)。エクスプローラ側の操作は
# explorer_nav.py に閉じ込めてあり、ここは「どちらへ送るか」だけを決める。
#
# 選択ウインドウは picker.PickerWindow をそのまま使う。ブックマークは settings.json の
# launcher.bookmarks に貯める(専用の編集UIは持たず、並べ替えや削除は settings.json を
# テキストエディタで直すという想定。定型文をフォルダに置くのと同じ考え方)。
#
# あふｗ側からは traytools_send.py 経由で叩ける(main.py のIPCを参照)。あふｗは現在の
# カレントパスを $P で渡せるので、「今見ているフォルダをその場で登録する」ができる。
# 前面がエクスプローラなら $P が無くても同じことができる(そちらはパスを自分で読める)。
import json
import os
import subprocess
import sys

from PySide6.QtWidgets import QInputDialog

import explorer_nav
import settings as settings_module
from picker import PickerWindow
from toast import show_toast

DEFAULT_AFXW_PATH = r"C:\soft\afxw\AFXW.EXE"

# 移動先の指定(settings.json の launcher.target)。
TARGET_AUTO = "auto"
TARGET_AFXW = "afxw"
TARGET_EXPLORER = "explorer"
TARGETS = (TARGET_AUTO, TARGET_AFXW, TARGET_EXPLORER)

# 「ここを登録」の項目は全角の ＋ で始める。絞り込みは前方一致なので、英字を打った時点で
# 候補から外れる(ブックマークを選ぶつもりでEnterを押して誤登録する事故を避ける)。
ADD_ITEM_PREFIX = "＋ ここを登録"

PLACEHOLDER = "絞り込み（↑↓で選択 / Enterで移動 / Escで閉じる）"

# 項目データの種別。picker には (表示名, データ) の形で渡す。
KIND_JUMP = "jump"
KIND_ADD = "add"


def afxw_path(app_settings: dict) -> str:
    return app_settings.get("launcher", {}).get("afxw_path") or DEFAULT_AFXW_PATH


def target_mode(app_settings: dict) -> str:
    """settings.json の launcher.target。手で編集されるファイルなので、知らない値が
    書かれていたら既定(auto)として扱う(綴り間違い1つで移動しなくなる方が困る)。"""
    target = app_settings.get("launcher", {}).get("target") or TARGET_AUTO
    return target if target in TARGETS else TARGET_AUTO


def load_bookmarks(app_settings: dict) -> list:
    """settings.json の launcher.bookmarks を返す。各要素は {"name": 表示名, "path": パス}。

    手で編集されるファイルなので、name か path が欠けた要素は黙って捨てる(壊れた1件で
    ブックマーク全体が開かなくなる方が困る)。"""
    bookmarks = app_settings.get("launcher", {}).get("bookmarks", [])
    if not isinstance(bookmarks, list):
        return []
    return [
        entry
        for entry in bookmarks
        if isinstance(entry, dict) and entry.get("name") and entry.get("path")
    ]


def save_bookmark(app_settings: dict, settings_path, name: str, path: str) -> bool:
    """ブックマークを1件追記して settings.json に保存する。成否を返す。

    メモリ上の app_settings はデフォルト値をマージ済みなので、それを丸ごと書き出すと
    未設定の既定値まで明示的に書かれてファイルの姿が変わってしまう。feature_audio の
    _save_device_identity と同じく、保存はファイルを読み直して launcher.bookmarks だけを
    足す形にする(書き出し自体は settings.save_settings を使う)。
    メモリ上の app_settings にも同じ要素を足しておく。次に開いたときすぐ出したいのと、
    アプリを再起動するまで settings.json を読み直さないため。"""
    entry = {"name": name, "path": path}

    launcher = app_settings.setdefault("launcher", {})
    if not isinstance(launcher.get("bookmarks"), list):
        launcher["bookmarks"] = []
    launcher["bookmarks"].append(entry)

    if not settings_path:
        return False
    try:
        stored = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
        stored_launcher = stored.setdefault("launcher", {})
        if not isinstance(stored_launcher.get("bookmarks"), list):
            stored_launcher["bookmarks"] = []
        stored_launcher["bookmarks"].append(entry)
        settings_module.save_settings(stored, settings_path)
        return True
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"[tray-tools] ブックマークを保存できません: {e}", file=sys.stderr)
        return False


def jump(path: str, exe_path: str = None, target: str = TARGET_AFXW, hwnd: int = None) -> bool:
    """指定フォルダを開く。移動先は target で選ぶ。成否を返す。

    target の既定が auto ではなく afxw なのは、引数が増える前からの呼び出し
    (jump(path) / jump(path, exe))をそのまま「あふｗへ移動」として動かし続けるため。
    どこへ送るかは呼び出し側が明示する。

    hwnd は「ブックマークを開いた時点で前面だったウインドウ」。auto の判定と、
    エクスプローラを移動させる対象の指定に使う。"""
    if target == TARGET_AUTO:
        # hwnd を掴んだのはピッカーを開く前だが、エクスプローラかどうかを見るのは今
        # (選ばれた瞬間)。開いている間にその窓が閉じられていれば is_explorer が False に
        # なり、無い窓を探しに行かずあふｗへ落ちる。
        #
        # あふｗが入っていない環境(他PCへ持っていった場合)では、前面がエクスプローラ
        # でないときに「あふｗが見つかりません」で行き止まりになる。auto は「使える方を
        # 選ぶ」ための値なので、実在を確かめてからでないとあふｗへ倒さない。
        if explorer_nav.is_explorer(hwnd):
            target = TARGET_EXPLORER
        elif os.path.exists(exe_path or DEFAULT_AFXW_PATH):
            target = TARGET_AFXW
        else:
            target = TARGET_EXPLORER
    if target == TARGET_EXPLORER:
        return _jump_explorer(path, hwnd)
    return _jump_afxw(path, exe_path)


def _jump_afxw(path: str, exe_path: str = None) -> bool:
    """あふｗに指定フォルダを開かせる。成否を返す。

    exe_path を省略できるようにしてあるのは、呼び出し側が設定を持たない場面でも
    使えるようにするため(既定は DEFAULT_AFXW_PATH)。

    -s は二重起動せず既存インスタンスへ渡す指定。小文字の -p は「そのフォルダの中を
    表示」で、大文字の -P だと親フォルダを開いてカーソルを合わせる別の動作になる。

    コマンドラインを文字列で渡しているのは意図的。リストで渡すと subprocess が
    list2cmdline で引用符を組み直してしまい、あふｗ独自のコマンドライン解析に
    -p"パス" の形がそのまま届かない。"""
    exe = exe_path or DEFAULT_AFXW_PATH
    if not os.path.exists(exe):
        show_toast(f"フォルダブックマーク\nあふｗが見つかりません\n{exe}")
        return False
    try:
        subprocess.Popen(f'"{exe}" -s -p"{path}"')
        return True
    except OSError as e:
        show_toast(f"フォルダブックマーク\n起動に失敗しました\n{e}")
        return False


def _jump_explorer(path: str, hwnd: int = None) -> bool:
    """エクスプローラで指定フォルダを開く。成否を返す。

    hwnd がエクスプローラなら、その窓の中身を差し替える(窓を増やさない)。そうでない
    場合と、差し替えに失敗した場合(窓が閉じられた・COMが応じない)は新しい窓を開く。
    黙って何も起きないのが一番困るので、最後は必ず開く側へ倒す。"""
    if explorer_nav.is_explorer(hwnd) and explorer_nav.navigate(hwnd, path):
        return True
    try:
        os.startfile(path)
        return True
    except OSError as e:
        # 削除済みのフォルダを登録したままだとここへ来る。Qtのスロット内で例外を投げ切ると
        # 常駐アプリごと落ちるので、必ず受けて通知に回す。
        show_toast(f"フォルダブックマーク\nフォルダを開けませんでした\n{path}\n{e}")
        return False


def _ask_name(parent, default_name: str):
    """登録する名前を尋ねる。キャンセルなら None を返す。"""
    name, ok = QInputDialog.getText(
        parent, "フォルダブックマーク", "登録する名前", text=default_name
    )
    name = name.strip() if ok else None
    return name or None


def resolve_add_path(current_path: str = None, hwnd: int = None):
    """「ここを登録」に使うパス。分からなければ None。

    優先順位は IPC で渡されたパス(あふｗのカレント) > 前面のエクスプローラが開いている
    パス。あふｗから呼ばれたときは相手が自分のカレントを $P で明示してきているので、
    その言い分をエクスプローラの推測より上に置く。"""
    if current_path:
        return current_path
    return explorer_nav.explorer_path(hwnd)


def create_picker(app_settings: dict, settings_path=None, current_path: str = None, hwnd: int = None):
    """選択ウインドウを作って返す。何も出すものが無ければ通知だけ出して None を返す。

    current_path(あふｗ側の現在のパス)は IPC から呼ばれたときだけ渡る。
    hwnd は「このウインドウを開く直前に前面だったウインドウ」で、移動先の判定
    (launcher.target が auto のとき)と「ここを登録」のパス取得に使う。ピッカーを出すと
    前面はこちらに移ってしまうので、掴むのは開く前でなければならない(feature_screen 側)。

    「ここを登録」は current_path か前面のエクスプローラのどちらかでパスが分かれば出る。
    どちらも無い(前面が無関係なアプリ)場合だけ付かない。"""
    bookmarks = load_bookmarks(app_settings)
    exe = afxw_path(app_settings)
    target = target_mode(app_settings)
    add_path = resolve_add_path(current_path, hwnd)

    items = [(entry["name"], (KIND_JUMP, entry["path"])) for entry in bookmarks]
    if add_path:
        items.append((f"{ADD_ITEM_PREFIX}   {add_path}", (KIND_ADD, add_path)))

    if not items:
        show_toast("フォルダブックマーク\nブックマークがありません")
        return None

    picker = None

    def _accept(_name: str, data) -> None:
        kind, path = data
        if kind == KIND_JUMP:
            jump(path, exe, target=target, hwnd=hwnd)
            return
        # 「ここを登録」。既定値はフォルダ名。ルート直下(C:\)だと basename が空になるので、
        # その場合はパスそのものを初期値にする。
        default_name = os.path.basename(path.rstrip("\\/")) or path
        name = _ask_name(picker, default_name)
        if name is None:
            return
        if save_bookmark(app_settings, settings_path, name, path):
            show_toast(f"フォルダブックマーク\n登録しました\n{name}")
        else:
            show_toast("フォルダブックマーク\n設定ファイルに保存できませんでした")

    picker = PickerWindow("フォルダブックマーク", items, _accept, placeholder=PLACEHOLDER)
    return picker
