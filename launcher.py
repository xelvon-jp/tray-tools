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
import ctypes
import json
import os
import re
import stat
import subprocess
import sys
import time

from PySide6.QtWidgets import QInputDialog, QMessageBox

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

# 入力欄にフルパスが打たれたときに出す項目。ADD_ITEM_PREFIX と同じく、ブックマーク名と
# 見分けが付く記号で始める。
TYPED_FOLDER_PREFIX = "→ このパスを開く"
TYPED_FILE_PREFIX = "→ このファイルの場所を開く"

# 打たれたパスの直下にあるフォルダ。名前だけを出す(フルパスは入力欄に見えている)。
SUBFOLDER_ITEM_PREFIX = "📁 "

# 一覧に出すサブフォルダの上限。理由は list_subfolders のコメントを参照。
MAX_SUBFOLDERS = 200

PLACEHOLDER = "絞り込み（フルパス可 / ↑↓選択 / Enter移動 / Esc閉じる）"

# 窓の下に出す早見表。Tab と編集キーはここに寄せる(PLACEHOLDER に全部を詰めると
# 入力欄の幅に収まらず、打ち始めた時点で消えてしまう)。
HINT = "Tab 下の階層へ / Ctrl+D 削除 / Ctrl+↑↓ 並べ替え"

# 消したブックマークの控え。1行1件の JSON Lines で追記していく。
#
# 【なぜ要るか】
# Ctrl+D を入れた当日の試用中に、実際に1件(soft → C:\soft)が誤って消えた。
# settings.json は世代バックアップを取っていないので、たまたま手元にあった控えと
# 突き合わせるまで「何が消えたか」すら分からなかった。設定ファイルを丸ごと控える
# 代わりに、消したものだけを位置(index)ごと残せば、手で元に戻せる。
#
# .gitignore の *.log で追跡外になる(このリポジトリは public で、中身は個人の履歴)。
DELETED_LOG_NAME = "bookmark_deleted.log"
DELETED_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), DELETED_LOG_NAME)

# ドライブ文字で始まるフルパス(C:\... / C:/...)。os.path.isabs は Windows では
# "\foo" のような「カレントドライブのルートからの相対」も True にするため、
# 「打った文字がフルパスか」の判定には使えない。
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

# GetDriveTypeW の戻り値のうち、ここで要るのはネットワークドライブだけ。
DRIVE_REMOTE = 4

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


def _find_bookmark(bookmarks: list, name: str, path: str):
    """(name, path) が一致する最初の要素の添字。無ければ None。

    添字で指さず中身で探すのは、settings.json が手で編集されるファイルだから。
    メモリ上の一覧とファイルの並びが食い違っていても、狙ったものだけを触れる。
    同じ (name, path) が複数あるときは最初の1件だけを対象にする。"""
    for index, entry in enumerate(bookmarks):
        if not isinstance(entry, dict):
            continue
        if entry.get("name") == name and entry.get("path") == path:
            return index
    return None


def _rewrite_stored_bookmarks(settings_path, change) -> bool:
    """settings.json を読み直して launcher.bookmarks だけを差し替える。成否を返す。

    メモリ上の app_settings はデフォルト値をマージ済みなので、それを丸ごと書き出すと
    未設定の既定値まで明示的に書かれてファイルの姿が変わってしまう。feature_audio の
    _save_device_identity と同じく、ファイルを読み直して要るところだけを書き換える
    (書き出し自体は settings.save_settings を使う)。

    change(ファイル側のブックマーク一覧) は新しい一覧を返す。None を返したときは
    「対象が無かった」とみなして何も書かない(黙って別の行を触らないため)。
    追加・削除・並べ替えの3つが同じ読み書きを別々に持たないよう、ここに寄せてある。"""
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
        updated = change(stored_launcher["bookmarks"])
        if updated is None:
            return False
        stored_launcher["bookmarks"] = updated
        settings_module.save_settings(stored, settings_path)
        return True
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"[tray-tools] ブックマークを保存できません: {e}", file=sys.stderr)
        return False


def _rewrite_memory_bookmarks(app_settings: dict, change) -> None:
    """メモリ上の app_settings にも同じ変更を反映する。

    アプリを再起動するまで settings.json を読み直さないので、ファイルだけ直しても
    開いている間はずっと古い一覧が出てしまう。"""
    launcher_settings = app_settings.setdefault("launcher", {})
    bookmarks = launcher_settings.get("bookmarks")
    if not isinstance(bookmarks, list):
        bookmarks = []
    updated = change(bookmarks)
    if updated is not None:
        launcher_settings["bookmarks"] = updated


def save_bookmark(app_settings: dict, settings_path, name: str, path: str) -> bool:
    """ブックマークを1件追記して settings.json に保存する。成否を返す。

    メモリ側は保存の成否によらず足す(前からの挙動)。設定ファイルに書けなくても、
    アプリを開いている間は使えた方がよいため。"""
    entry = {"name": name, "path": path}
    _rewrite_memory_bookmarks(app_settings, lambda bookmarks: bookmarks + [entry])
    return _rewrite_stored_bookmarks(settings_path, lambda bookmarks: bookmarks + [entry])


def _log_deleted(name: str, path: str, index: int) -> None:
    """消したブックマークを1行1件で控える(DELETED_LOG_PATH)。

    書き方は agent_loop._log に揃える(1行1件の JSON Lines・ensure_ascii=False・
    newline="" で開く)。向こうは失敗を黙って捨てているが、こちらは理由を標準エラーに
    出す。控えが残らないこと自体がこの機能の目的に反するので、気づけるようにしておく。
    それでも記録のために削除は止めない(呼び出し側は戻り値を見ない)。"""
    record = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "name": name,
        "path": path,
        "index": index,
    }
    try:
        with open(DELETED_LOG_PATH, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[tray-tools] 削除の控えを残せません: {e}", file=sys.stderr)


def delete_bookmark(app_settings: dict, settings_path, name: str, path: str) -> bool:
    """ブックマークを1件消す。成否を返す。消した中身は DELETED_LOG_PATH に控える。

    ファイル側に (name, path) が見つからなければ False で、ファイルもメモリも変えない。
    手で編集されて既に消えている、といった食い違いのときに、別の行を巻き込まないため。"""

    def change(bookmarks, log: bool = False):
        index = _find_bookmark(bookmarks, name, path)
        if index is None:
            return None
        if log:
            # 控えは**書き換える前**に残す。後に回すと、書き換えの途中で落ちたときに
            # 「消えたのに記録が無い」という、いちばん困る形になりうる。この順なら
            # 最悪でも「記録はあるが消えていない」で済み、それは実害が無い。
            _log_deleted(name, path, index)
        return bookmarks[:index] + bookmarks[index + 1:]

    # 控えを残すのはファイル側の1回だけ。change はメモリ側にも同じものを使うので、
    # log を付けずに呼ばないと同じ削除が2行に増える。
    if not _rewrite_stored_bookmarks(settings_path, lambda b: change(b, log=True)):
        return False
    _rewrite_memory_bookmarks(app_settings, change)
    return True


def move_bookmark(app_settings: dict, settings_path, name: str, path: str, step: int) -> bool:
    """ブックマークを step のぶん動かす(-1で1つ上、+1で1つ下)。成否を返す。

    端で押されたときは False。並びには触らない(何も起きないのが正しい)。"""

    def change(bookmarks):
        index = _find_bookmark(bookmarks, name, path)
        if index is None:
            return None
        target = index + step
        if not (0 <= target < len(bookmarks)):
            return None
        moved = list(bookmarks)
        moved.insert(target, moved.pop(index))
        return moved

    if not _rewrite_stored_bookmarks(settings_path, change):
        return False
    _rewrite_memory_bookmarks(app_settings, change)
    return True


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


def typed_items(text: str) -> list:
    """入力欄の文字列から項目を作る。ピッカーが1文字打つごとに呼ぶ。

    [打たれたパスへの移動(あれば)] + [その直下のフォルダ] の順。移動用を先に置くのは、
    打ち切ったパスでそのまま Enter を押せるようにするため(初期選択は先頭行)。

    データはどれもブックマークと同じ (KIND_JUMP, パス) にしてある。移動先の判定
    (あふｗ/エクスプローラ/auto)を二重に持たず、ブックマークを選んだときと
    まったく同じ経路を通すため。"""
    found = []
    resolved = resolve_typed_path(text)
    if resolved is not None:
        path, is_file = resolved
        prefix = TYPED_FILE_PREFIX if is_file else TYPED_FOLDER_PREFIX
        found.append((f"{prefix}   {path}", (KIND_JUMP, path)))
    listing = resolve_listing_context(text)
    if listing is not None:
        parent, keyword = listing
        for name, full_path in list_subfolders(parent, keyword):
            found.append((f"{SUBFOLDER_ITEM_PREFIX}{name}", (KIND_JUMP, full_path)))
    return found


def tab_text(_name: str, data):
    """Tab を押したときに入力欄へ入れる文字列。掘り下げる先が無ければ None。

    末尾に区切りを足すのは、その中身が一覧に出るようにするため(区切りが無いと
    「同じ名前で始まる兄弟フォルダ」の絞り込みになってしまう)。

    移動用のデータなら効くので、打ったパスやサブフォルダだけでなく**ブックマークの行でも
    効く**。分けていないのは意図してのこと。ブックマークを選んで Tab でその中へ掘れる方が、
    いったん移動してから探すより早い。"""
    kind, path = data
    if kind != KIND_JUMP:
        return None
    return path.rstrip("\\/") + "\\"


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


def is_network_drive(path: str) -> bool:
    """path のドライブ文字がネットワークドライブ(割り当てたドライブ)なら True。

    GetDriveTypeW は Windows が持っているドライブの割り当て表を引くだけで、その先の
    サーバへは触りにいかない。だから相手が落ちていても即座に返る(1文字打つごとに
    呼んでも止まらない)。存在確認の isdir/isfile とはそこが違う。

    判定できなかったときは False(＝ローカル扱い)に倒す。分からないことを理由に
    機能を止めない。最悪でも「これまでどおり存在確認をする」に戻るだけ。"""
    try:
        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW
        # 64bit では既定の戻り値(int)で切り詰められて落ちた実績があるので、
        # argtypes / restype は必ず明示する。
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        return get_drive_type(f"{path[0]}:\\") == DRIVE_REMOTE
    except Exception as e:  # noqa: BLE001  打鍵のたびに通る道なので、ここで落とさない
        print(f"[tray-tools] ドライブの種別を調べられません: {e}", file=sys.stderr)
        return False


def normalize_typed_text(text: str):
    """入力欄の生の文字列を、パスとして見るための形に整える。整うものが無ければ None。

    「フルパスか」「実在するか」は見ない。ここでやるのは字面の整えだけ。
    resolve_typed_path と resolve_listing_context の両方が同じ字面を見る必要があるので、
    切り出してある(片方だけ直して食い違うのを避ける)。"""
    if not text:
        return None
    path = text.strip()
    # エクスプローラの「パスのコピー」は "C:\..." の形で引用符ごと渡ってくる。
    # 囲みは1組だけ外す(パス自体に引用符を含む場合まで面倒は見ない)。
    if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
        path = path[1:-1].strip()
    if not path:
        return None
    # %USERPROFILE%\Desktop や ~\Desktop をそのまま打てるようにする。
    return os.path.expanduser(os.path.expandvars(path))


def resolve_typed_path(text: str):
    """入力欄に打たれた文字列がフルパスなら (開くフォルダ, 入力がファイルだったか) を返す。

    フルパスに見えなければ None(＝項目を出さない)。打っている途中や打ち間違いでは
    候補が出ず、Enterを押しても空振りするだけ、という形にしてある。

    ピッカーは1文字打つごとにこれを呼ぶ。重い処理を入れないこと。"""
    path = normalize_typed_text(text)
    if path is None:
        return None

    if path.startswith("\\\\"):
        # UNC(\\host\share)。ここだけは存在を確かめない。切断されたホストへの
        # isdir/isfile は数秒ブロックすることがあり、1文字打つたびに呼ばれるこの関数が
        # 止まると入力そのものが固まる。項目はそのまま出し、実在するかは移動先
        # (あふｗ/エクスプローラ)に判断させる。ファイルかどうかも見ないのでフォルダ扱い。
        return path, False
    if not _DRIVE_PATH_RE.match(path):
        return None

    path = os.path.normpath(path)
    if is_network_drive(path):
        # 割り当てたドライブ(U:\ など)も先はネットワーク。UNC と同じ理由(上を参照)で
        # 存在確認をせずにそのまま返す。ドライブ文字で書けても止まり方は変わらない。
        return path, False
    if os.path.isdir(path):
        return path, False
    if os.path.isfile(path):
        # ファイルを打たれた(貼られた)ときは、その置き場所へ移動する。
        return os.path.dirname(path), True
    return None


def resolve_listing_context(text: str):
    """入力から「どのフォルダの直下を出すか」を決める。(親フォルダ, 絞り込み文字列)。

    出すものが無ければ None。
    - 打たれた文字がそのまま実在するフォルダ → そのフォルダの全部
    - そうでなければ、その親が実在するフォルダ → 親の直下を、打ちかけの名前で絞る
    - ネットワークドライブと UNC は None。この先で scandir を呼ぶことになるが、
      それは isdir と同じく相手が落ちていればブロックする(resolve_typed_path の
      UNC の項を参照)。掘り下げは諦めて、フルパスを打ち切っての移動だけを残す。"""
    path = normalize_typed_text(text)
    if path is None:
        return None
    if path.startswith("\\\\") or not _DRIVE_PATH_RE.match(path):
        return None
    path = os.path.normpath(path)
    if is_network_drive(path):
        return None
    if os.path.isdir(path):
        return path, ""
    parent = os.path.dirname(path)
    if parent and os.path.isdir(parent):
        return parent, os.path.basename(path)
    return None


def list_subfolders(parent: str, prefix: str = "") -> list:
    """parent 直下のフォルダを [(名前, フルパス), ...] で返す。名前順(大文字小文字を無視)。

    prefix があれば名前の前方一致(大文字小文字を無視)で絞る。

    隠し属性・システム属性は外す。C:\\ の直下の System Volume Information や、
    リポジトリの .git のような「普段は見たくないもの」で一覧が埋まるのを防ぐため。
    見えないだけで、フルパスを打ち切れば移動自体はできる。

    属性は entry.stat(follow_symlinks=False) から見る。Windows の scandir は
    列挙のときに受け取った情報を持っているので、この呼び出しで改めてディスクを
    読みにいくことにはならない。フォルダかどうかも同じ属性から見ているので、
    ジャンクションや接合点でも余計な追跡(＝止まりうる I/O)をしない。

    アクセス拒否や、開いている間に消えたフォルダでは空リストを返す。例外を外へ出さない
    (PySide6 はスロット内で投げ切るとプロセスごと落ちる)。"""
    keyword = (prefix or "").lower()
    found = []
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if keyword and not entry.name.lower().startswith(keyword):
                    continue
                try:
                    attributes = entry.stat(follow_symlinks=False).st_file_attributes
                except OSError:
                    # 1件読めないだけ。一覧そのものは出す。
                    continue
                if not attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                    continue
                if attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                    continue
                found.append((entry.name, entry.path))
                if len(found) >= MAX_SUBFOLDERS:
                    # 上限に達したら列挙をやめる。これは1文字打つごとに走る処理で、
                    # C:\Windows\WinSxS のように万を超える項目を持つフォルダでも
                    # 毎回最後まで舐めることになるため。200 は「一覧に出しても
                    # 選ぶ気にならない数」として置いた区切りで、測って決めた値ではない。
                    # 打ち切った場合、出てくる200件が名前順で先頭の200件になるとは限らない
                    # (ディレクトリの列挙順がどうなるかはここでは当てにしていない)。
                    break
    except OSError as e:
        print(f"[tray-tools] フォルダを読めません ({parent}): {e}", file=sys.stderr)
        return []
    found.sort(key=lambda pair: pair[0].lower())
    return found


def create_picker(app_settings: dict, settings_path=None, current_path: str = None, hwnd: int = None):
    """選択ウインドウを作って返す。何も出すものが無ければ通知だけ出して None を返す。

    current_path(あふｗ側の現在のパス)は IPC から呼ばれたときだけ渡る。
    hwnd は「このウインドウを開く直前に前面だったウインドウ」で、移動先の判定
    (launcher.target が auto のとき)と「ここを登録」のパス取得に使う。ピッカーを出すと
    前面はこちらに移ってしまうので、掴むのは開く前でなければならない(feature_screen 側)。

    「ここを登録」は current_path か前面のエクスプローラのどちらかでパスが分かれば出る。
    どちらも無い(前面が無関係なアプリ)場合だけ付かない。"""
    exe = afxw_path(app_settings)
    target = target_mode(app_settings)
    add_path = resolve_add_path(current_path, hwnd)

    def _build_items() -> list:
        """一覧の元データを組み立てる。初回も、削除・並べ替えの後の組み直しも同じものを使う。

        「ここを登録」の行も毎回付け直す。ここを分けて書くと、組み直したときにだけ
        その行が消える、といった食い違いになる。"""
        found = [
            (entry["name"], (KIND_JUMP, entry["path"]))
            for entry in load_bookmarks(app_settings)
        ]
        if add_path:
            found.append((f"{ADD_ITEM_PREFIX}   {add_path}", (KIND_ADD, add_path)))
        return found

    items = _build_items()
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

    def _delete(name: str, data) -> None:
        """Ctrl+D。ブックマークの行だけ。確認を1回出してから消す。"""
        kind, path = data
        if kind != KIND_JUMP:
            # 「ここを登録」の行。消せる実体が無い(キーは picker 側が食べている)。
            return
        answer = QMessageBox.question(
            picker, "フォルダブックマーク", f"{name} を削除しますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if delete_bookmark(app_settings, settings_path, name, path):
            picker.set_items(_build_items())
            # 「控えがある」ことを出すのは、消してすぐ気づいたときに探す先が分かるように。
            show_toast(f"フォルダブックマーク\n削除しました\n{name}\n（控え: {DELETED_LOG_NAME}）")
        else:
            show_toast("フォルダブックマーク\n設定ファイルに保存できませんでした")

    def _move(name: str, data, step: int) -> None:
        """Ctrl+↑↓。ブックマークの行だけ。確認は出さない(すぐ戻せるので)。"""
        kind, path = data
        if kind != KIND_JUMP:
            return
        if move_bookmark(app_settings, settings_path, name, path, step):
            picker.set_items(_build_items())

    picker = PickerWindow(
        "フォルダブックマーク", items, _accept,
        placeholder=PLACEHOLDER, hint=HINT, dynamic_items=typed_items, tab_text=tab_text,
        on_delete=_delete, on_move=_move,
    )
    return picker
