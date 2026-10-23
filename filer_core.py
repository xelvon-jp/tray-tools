# filer_core.py
# 簡易2画面ファイラ(filer.py)の、Qt にも Win32 にも依存しない部分。
#
# なぜ分けたのか
#   キー割り当て・パスの解決・マスク・並び順・履歴・マークの対象選びは、どれも
#   「あふｗと同じ指の動きで同じ結果になるか」が肝で、間違えるとファイル操作の
#   対象がずれる(＝事故になる)。窓を作らずに tests/test_filer.py で確かめられるよう、
#   ここには os と標準ライブラリだけで書ける関数を集めてある。
#   ファイル一覧の読み取り(list_entries / read_folder)だけは実ディスクを触るが、
#   これも別スレッドから呼ぶ前提の素の関数にしてある(Qt のオブジェクトを触らない)。
import fnmatch
import os
import re
import stat
import time
from dataclasses import dataclass

# ---------------------------------------------------------------
# キー割り当て
# ---------------------------------------------------------------
# あふｗの AFXW.KEY と同じ「SCAV」4桁表記で持つ。
#   前1桁 … SHIFT=1 / CTRL=2 / ALT=4 の論理和
#   後3桁 … 仮想キーコード(10進)
# あふｗ側の設定と突き合わせるときに、そのまま横に並べて読めるようにするため。
# 中身はユーザー自身の AFXW.key(あふｗ標準＋カスタマイズ)に寄せてある。
# settings.json の filer.keys で上書きできる(build_keymap 参照)。
SHIFT = 1
CTRL = 2
ALT = 4

DEFAULT_KEYMAP = {
    # --- カーソル ---
    "0038": "cursor_up",          # ↑
    "0040": "cursor_down",        # ↓
    "0033": "page_up",            # PgUp
    "0034": "page_down",          # PgDn
    "2033": "cursor_top",         # Ctrl+PgUp
    "2034": "cursor_bottom",      # Ctrl+PgDn
    # --- 窓の大きさ・字の大きさ(あふｗ AFXW.TXT 148-149行と AFXW.key) ---
    "4037": "split_left",         # Alt+← 左右の境界を左へ
    "2037": "split_left",         # Ctrl+← (AFXW.key K0002 "2037:4037")
    "4039": "split_right",        # Alt+→ 左右の境界を右へ
    "2039": "split_right",        # Ctrl+→ (AFXW.key K0005 "2039:4039")
    "4038": "log_grow",           # Alt+↑ メッセージ窓を大きく(境界が上へ)
    "4040": "log_shrink",         # Alt+↓ メッセージ窓を小さく
    "1038": "font_smaller",       # Shift+↑ (AFXW.key K0003 "1038:0045" ＝ INS 縮小)
    "2038": "font_smaller",       # Ctrl+↑  (K0004 "2038:0045")
    "1040": "font_larger",        # Shift+↓ (K0006 "1040:0046" ＝ DEL 拡大)
    "2040": "font_larger",        # Ctrl+↓  (K0007 "2040:0046")
    # --- 窓・フォルダ移動 ---
    "0037": "focus_left",         # ← 左窓をアクティブに(既に左なら何もしない)
    "0039": "focus_right",        # → 右窓をアクティブに(既に右なら何もしない)
    "0008": "parent",             # BS
    "0009": "switch",             # Tab 反対窓へ(あふｗ標準には無いが便利なので)
    "0013": "enter",              # Enter フォルダなら入る/ファイルは関連付け
    "2013": "open_assoc",         # Ctrl+Enter 関連付けで開く
    "1013": "edit",               # Shift+Enter エディタ
    "0069": "edit",               # E
    "0086": "edit",               # V (AFXW.key 0086 &MENU edit.txt の代わり)
    # --- マーク ---
    "0032": "mark_down",          # Space
    "1032": "mark_up",            # Shift+Space
    "2032": "mark_range_up",      # Ctrl+Space 直上のマークからカーソル位置まで全てマーク
    "3032": "mark_range_down",    # Shift+Ctrl+Space 直下のマークからカーソル位置まで
    "0036": "invert_files",       # Home ファイルだけ反転
    "0065": "invert_files",       # A
    "1036": "invert_all",         # Shift+Home フォルダも反転
    "1065": "invert_all",         # Shift+A
    # --- 再読込 ---
    "0035": "reload",             # End (マーククリア)
    "2035": "reload_keep",        # Ctrl+End (マーク保持)
    "1035": "reload_other",       # Shift+End 反対窓
    # --- 左右の窓のパス ---
    "0079": "sync_to_other",      # O 自分を反対窓と同じパスに
    "1079": "sync_other_to_me",   # Shift+O 反対窓をこちらと同じパスに
    "4079": "swap_paths",         # Alt+O 入れ替え(AFXW.key の 4079 &EXCD)
    # --- 履歴・ジャンプ ---
    "0066": "back",               # B
    "0188": "back",               # ,
    "1066": "forward",            # Shift+B
    "0190": "forward",            # .
    "0072": "history_jump",       # H
    "0074": "bookmark_jump",      # J (tray-tools のフォルダブックマーク)
    "1074": "path_jump",          # Shift+J パス入力
    "0078": "desktop",            # N (AFXW.key 0078 &EXCD Desktop)
    "0076": "drive",              # L
    "0070": "isearch",            # F
    "0083": "sort_dialog",        # S ソート
    "1083": "sort_reverse",       # Shift+S 昇順/降順の反転(AFXW.key 1083 &SORT !)
    "1090": "settings_menu",      # Shift+Z 各種設定一時切替(隠し/システムファイルの表示)
    "0087": "compare_mark",       # W 同名ファイル比較マーク
    "4087": "winmerge",           # Alt+W WinMerge で比較(AFXW.key 4087 &MENU hikaku.txt の代わり)
    "0080": "zip_create",         # P ZIP 圧縮(反対窓に作る。AFXW.key 0080 &MENU press.txt の代わり)
    # --- マスク ---
    "0186": "mask_clear",         # :  (VK_OEM_1)
    "1186": "mask_input",         # Shift+: (AFXW.key 1186 &MASK)
    # --- ファイル操作 ---
    "0067": "copy_menu",          # C (menu\copy.txt)
    "4067": "copy_direct",        # Alt+C
    "0077": "move_menu",          # M (menu\move.txt)
    "4077": "move_direct",        # Alt+M
    "0068": "delete",             # D ごみ箱
    "0046": "delete",             # Del
    "1068": "delete_permanent",   # Shift+D 完全削除
    "1046": "delete_permanent",   # Shift+Del
    "0075": "mkdir",              # K
    "0082": "rename",             # R
    "2045": "clip_copy",          # Ctrl+Ins
    "2046": "clip_cut",           # Ctrl+Del
    "1045": "clip_paste",         # Shift+Ins
    # --- 外のアプリで開く ---
    "0120": "open_shell",         # F9 いまのフォルダで PowerShell
    "0121": "open_explorer",      # F10 いまのフォルダをエクスプローラで
    "0118": "context_menu",       # F7 エクスプローラの右クリックメニュー(AFXW.key K0054 "0118:0226")
    # --- 閉じる ---
    # Esc は割り当てない。あふｗの Esc は「各処理の中止」で、ファイル窓を閉じない
    # (サーチ中の Esc はサーチの終了として先に処理される)。
    "0081": "quit",               # Q
    "4115": "quit",               # Alt+F4 (AFXW.key 4115:0081)
}

# ---------------------------------------------------------------
# 窓の配分と字の大きさ(Alt+矢印 / Shift・Ctrl+↑↓)
# ---------------------------------------------------------------
DEFAULT_SPLIT_RATIO = 0.5
SPLIT_STEP = 0.03          # Alt+←→ 1回で動かす割合(窓幅の3%)
SPLIT_MIN = 0.15           # 片方の窓がこれより細くならない
DEFAULT_LOG_LINES = 5
LOG_LINES_MIN = 1
DEFAULT_FONT_SIZE = 10
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 32


def step_split(ratio, direction: int) -> float:
    """左窓の幅の割合を direction(-1 で左へ、+1 で右へ)に1段動かす。端に寄りすぎない。"""
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        ratio = DEFAULT_SPLIT_RATIO
    ratio += SPLIT_STEP * direction
    return round(min(max(ratio, SPLIT_MIN), 1.0 - SPLIT_MIN), 4)


def clamp_split(ratio) -> float:
    return step_split(ratio, 0)


def clamp_log_lines(lines, max_lines: int) -> int:
    """メッセージ窓の行数を 1〜max_lines に収める(max_lines は窓の高さの半分ほど)。"""
    try:
        lines = int(lines)
    except (TypeError, ValueError):
        lines = DEFAULT_LOG_LINES
    return max(LOG_LINES_MIN, min(lines, max(LOG_LINES_MIN, int(max_lines))))


def clamp_font_size(size) -> int:
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = DEFAULT_FONT_SIZE
    return max(FONT_SIZE_MIN, min(size, FONT_SIZE_MAX))

# filer.py が実装しているコマンドの名前。filer.keys で知らない名前を書かれたら捨てる
# (綴り間違い1つで「押したら何か別のことが起きる」より「何も起きない」方が安全)。
COMMANDS = frozenset(DEFAULT_KEYMAP.values())

_SCAV_RE = re.compile(r"^[0-7]\d{3}$")


def scav(vk: int, shift: bool = False, ctrl: bool = False, alt: bool = False) -> str:
    """仮想キーコードと修飾キーから、あふｗの4桁表記を作る。"""
    mods = (SHIFT if shift else 0) | (CTRL if ctrl else 0) | (ALT if alt else 0)
    return f"{mods}{int(vk) % 1000:03d}"


def build_keymap(overrides=None) -> dict:
    """既定の割り当てに settings.json の filer.keys を重ねた辞書を返す。

    overrides は {"0067": "copy_menu", "0099": ""} の形。値が空文字(や null)なら
    その割り当てを外す。形の違うキー・知らないコマンド名は黙って捨てる
    (手で編集するファイルなので、1つの間違いで全部が効かなくなるのを避ける)。"""
    keymap = dict(DEFAULT_KEYMAP)
    if not isinstance(overrides, dict):
        return keymap
    for code, command in overrides.items():
        if not isinstance(code, str) or not _SCAV_RE.match(code):
            continue
        if not command:
            keymap.pop(code, None)
            continue
        if command in COMMANDS:
            keymap[code] = command
    return keymap


def arrow_target(side: str, direction: str):
    """←/→ を押したときにアクティブにする窓。既にその窓なら None(何もしない)。

    あふｗの ←/→ は「その方向の窓をアクティブにする」だけで、親フォルダへは行かない
    (親へ行くのは BS だけ。ユーザー確認済み)。side はいまアクティブな窓、
    direction は押した矢印。どちらも "left" / "right"。"""
    return None if side == direction else direction


# ---------------------------------------------------------------
# 一覧の項目
# ---------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0
    attrs: int = 0
    is_parent: bool = False  # 先頭の ".."

    @property
    def hidden(self) -> bool:
        return bool(self.attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))

    @property
    def color_kind(self) -> str:
        """表示色の種類。あふｗの表示色設定と同じ分け方で、フォルダは属性に関係なく
        フォルダの色(「フォルダは属性に関係無く指定色」にチェックが入った設定)。
        ファイルはシステム > 隠し > 読み込み専用 > 通常 の順に見る。"""
        if self.is_dir:
            return "directory"
        if self.attrs & stat.FILE_ATTRIBUTE_SYSTEM:
            return "system"
        if self.attrs & stat.FILE_ATTRIBUTE_HIDDEN:
            return "hidden"
        if self.attrs & stat.FILE_ATTRIBUTE_READONLY:
            return "readonly"
        return "text"


def attr_visible(entry: "Entry", show_hidden: bool = True, show_system: bool = True) -> bool:
    """Shift+Z の「隠しファイル / システムファイルを表示する」に照らして見せるか。
    両方の属性を持つものは、どちらか一方でも非表示ならば見せない。".." は常に見せる。"""
    if entry.is_parent:
        return True
    if not show_hidden and entry.attrs & stat.FILE_ATTRIBUTE_HIDDEN:
        return False
    if not show_system and entry.attrs & stat.FILE_ATTRIBUTE_SYSTEM:
        return False
    return True


def filter_attrs(entries, show_hidden: bool = True, show_system: bool = True) -> list:
    if show_hidden and show_system:
        return list(entries)
    return [e for e in entries if attr_visible(e, show_hidden, show_system)]


PARENT_ENTRY = Entry("..", True, is_parent=True)


# 並べ方。あふｗのソートダイアログ(S)の選択肢と同じ並び。
SORT_KINDS = ("none", "name", "ext", "size", "time", "attr", "natural")
SORT_LABELS = {
    "none": "ソートなし",
    "name": "ファイル名",
    "ext": "拡張子名",
    "size": "サイズ",
    "time": "タイムスタンプ",
    "attr": "属性",
    "natural": "ファイル名(番号)",
}
# ダイアログで押すと即決する文字。
SORT_SHORTCUTS = {"N": "none", "F": "name", "E": "ext", "S": "size", "T": "time",
                  "A": "attr", "X": "natural"}
DEFAULT_SORT = {"kind": "name", "descending": False, "dirs_first": True, "ignore_case": True}


def normalize_sort(spec) -> dict:
    """settings.json から来た並べ方を、知っている値だけに揃える(手で書かれても壊れない)。"""
    result = dict(DEFAULT_SORT)
    if isinstance(spec, dict):
        if spec.get("kind") in SORT_KINDS:
            result["kind"] = spec["kind"]
        for key in ("descending", "dirs_first", "ignore_case"):
            if isinstance(spec.get(key), bool):
                result[key] = spec[key]
    return result


_NATURAL_RE = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """file2 < file10 になる比較キー。数字の並びは数として、それ以外は文字として比べる。
    同じ位置に数と文字が来ても比べられるよう、各部分を (種別, 数, 文字) の形に揃える。"""
    parts = []
    for part in _NATURAL_RE.split(text):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part), part))
        else:
            parts.append((1, 0, part))
    return tuple(parts)


def sort_key(entry: "Entry", kind: str, ignore_case: bool = True):
    """1つの項目の並べ替えキー。同点は名前で決める(並びが実行ごとに揺れないように)。"""
    name = entry.name.casefold() if ignore_case else entry.name
    if kind == "ext":
        return (split_name(entry)[1].casefold() if ignore_case else split_name(entry)[1], name)
    if kind == "size":
        return (entry.size, name)
    if kind == "time":
        return (entry.mtime, name)
    if kind == "attr":
        return (entry.attrs, name)
    if kind == "natural":
        return (natural_key(name), name)
    return (name, entry.name)


def sort_entries(entries, spec=None) -> list:
    """並べ方(spec)に従って並べる。既定はフォルダが先・名前順(大文字小文字を無視)。

    - dirs_first ならフォルダの塊を上に置き、昇順/降順はそれぞれの塊の中で効かせる
      (降順にしてもフォルダが下へ行かない。あふｗと同じ)
    - "none" は OS の列挙順のまま(降順なら逆順)
    - ".." は渡さないこと(呼ぶ側が常に先頭に足す)"""
    spec = normalize_sort(spec)
    entries = list(entries)
    groups = ([[e for e in entries if e.is_dir], [e for e in entries if not e.is_dir]]
              if spec["dirs_first"] else [entries])
    result = []
    for group in groups:
        if spec["kind"] == "none":
            result.extend(reversed(group) if spec["descending"] else group)
        else:
            result.extend(sorted(group, key=lambda e: sort_key(e, spec["kind"], spec["ignore_case"]),
                                 reverse=spec["descending"]))
    return result


def list_entries(path: str) -> list:
    """path 直下の項目を Entry の並び(OS の列挙順のまま)で返す。".." は含めない。
    並べ替えは表示側が窓ごとの設定で行う(「ソートなし」で列挙順を見せるため)。

    フォルダかどうかは属性で見る(ジャンクションの先を辿りに行かない＝止まりうる I/O を
    増やさない。launcher.list_subfolders と同じ考え方)。Windows の scandir は列挙で
    受け取った情報を持っているので、stat(follow_symlinks=False) は改めてディスクを読まない。

    フォルダ自体が読めなければ OSError を投げる(呼ぶ側がメッセージにする)。
    1件だけ読めないものは、名前だけの項目として残す(消えて見えるより分かりやすい)。"""
    found = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
                attrs = getattr(st, "st_file_attributes", 0)
                is_dir = bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY) if attrs else entry.is_dir()
                found.append(Entry(entry.name, is_dir,
                                   0 if is_dir else st.st_size, st.st_mtime, attrs))
            except OSError:
                found.append(Entry(entry.name, False))
    return found


# ---------------------------------------------------------------
# パス
# ---------------------------------------------------------------
def normalize_dir(path: str) -> str:
    """フォルダのパスを1つの形に揃える。ルートは末尾 \\ 付き(C:\\、\\\\host\\share\\)、
    それ以外は末尾 \\ 無し。比較や履歴の重複判定をこの形で行う。"""
    path = os.path.normpath(path)
    drive, rest = os.path.splitdrive(path)
    if rest in ("", "\\", "/"):
        return drive + "\\"
    return path.rstrip("\\/")


def is_root(path: str) -> bool:
    _drive, rest = os.path.splitdrive(normalize_dir(path))
    return rest in ("", "\\")


def parent_of(path: str):
    """親フォルダ。ルート(ドライブ直下・UNC の共有直下)なら None。"""
    path = normalize_dir(path)
    if is_root(path):
        return None
    return normalize_dir(os.path.dirname(path))


def same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(normalize_dir(a)) == os.path.normcase(normalize_dir(b))


def existing_ancestor(path: str, isdir=os.path.isdir):
    """path か、その親のうち実在する最も深いフォルダ。どれも無ければ None。

    前回のパスを復元するときに使う(保存したフォルダが消えていたら、残っている親まで遡る)。
    ネットワーク越しでは isdir が止まりうるので、UI スレッドからは呼ばないこと。"""
    if not path:
        return None
    current = normalize_dir(path)
    while current:
        try:
            if isdir(current):
                return current
        except OSError:
            pass
        current = parent_of(current)
    return None


def expand_typed_path(text: str):
    """Shift+J で打たれた文字列をパスの形にする。空なら None。

    引用符の囲みを1組外し、環境変数と ~ を展開する(launcher.normalize_typed_text と
    同じ字面の扱い。こちらは comtypes を読み込まないよう同じことを自前で持つ)。"""
    if not text:
        return None
    path = text.strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in ('"', "'"):
        path = path[1:-1].strip()
    if not path:
        return None
    return os.path.expanduser(os.path.expandvars(path))


def read_folder(path: str, focus=None, walk_up: bool = False):
    """別スレッドから呼ぶ一覧の読み取り。
    (実際に開いたフォルダ, 項目, カーソルを合わせる名前, 書庫の情報) を返す。

    - 書庫(.zip/.7z)そのもの、または書庫の中のパス(D:\\x\\foo.zip\\sub)なら書庫の中を
      読む。書庫の情報は (書庫ファイル, 中のパス, 危ないので除いたエントリ数)。
      ふつうのフォルダなら None
    - path がファイルなら、その置き場所を開いてファイル名にカーソルを合わせる
      (Shift+J でファイルのパスを貼ったとき)
    - walk_up なら、無くなっていたフォルダを残っている親まで遡る(前回の復元用)。
      ふだんの移動では遡らない(入れなかったら元のフォルダに留まるのがあふｗの挙動)
    読めなければ OSError(書庫として読めなければ filer_archive.ArchiveError)を投げる。"""
    import filer_archive  # filer_archive が filer_core を読むので、ここで遅れて読む

    path = os.path.normpath(path)
    found = filer_archive.split_archive_path(path)
    if found is not None:
        display, entries, (archive, inner), skipped = filer_archive.read_archive_folder(
            found[0], found[1], walk_up=walk_up)
        return display, entries, focus, (archive, inner, skipped)
    if not walk_up and os.path.isfile(path):
        focus = os.path.basename(path)
        path = os.path.dirname(path)
    if walk_up:
        found = existing_ancestor(path)
        if found is None:
            raise FileNotFoundError(f"フォルダが見つかりません: {path}")
        path = found
    path = normalize_dir(path)
    return path, list_entries(path), focus, None


# ---------------------------------------------------------------
# マスク
# ---------------------------------------------------------------
DEFAULT_MASK = "*.*"


def parse_mask(text: str) -> list:
    """マスク入力を照合用のパターンの並びにする。

    セミコロン(と空白)区切りで複数。ワイルドカード(* ?)を含まない語は「含む」の意味で
    *語* に広げる(あふｗで foo と打つと foo を含むものが残るのと同じ)。
    空・*.*・* だけなら [](＝全部表示)。"""
    patterns = []
    for word in re.split(r"[;\s]+", text or ""):
        word = word.strip()
        if not word:
            continue
        if word in ("*", "*.*"):
            return []
        if "*" not in word and "?" not in word:
            word = f"*{word}*"
        patterns.append(word.casefold())
    return patterns


def is_default_mask(text: str) -> bool:
    return not parse_mask(text)


def mask_matches(name: str, patterns: list) -> bool:
    if not patterns:
        return True
    folded = name.casefold()
    return any(fnmatch.fnmatchcase(folded, p) for p in patterns)


def filter_entries(entries, patterns: list) -> list:
    """マスクで絞る。フォルダは常に残す(絞り込み中でも移動はできるように)。"""
    if not patterns:
        return list(entries)
    return [e for e in entries if e.is_dir or mask_matches(e.name, patterns)]


# ---------------------------------------------------------------
# マーク
# ---------------------------------------------------------------
def drag_paths(folder: str, entries, marks) -> list:
    """マウスのドラッグで外へ運ぶファイルのフルパス。マークしたものだけ(一覧の並び順)。

    あふｗと同じく、マウスでは選ばない。押した位置の行もカーソル行も運ばない。
    マークが無ければ空(＝ドラッグを始めない)。".." は運ばない。"""
    if not marks:
        return []
    return [os.path.join(folder, e.name) for e in entries
            if e.name in marks and not e.is_parent]


def pick_targets(entries, marks, cursor_index: int) -> list:
    """操作の対象。マークがあればマークしたもの(一覧の並び順)、無ければカーソル位置。

    ".." はどちらの場合も対象にしない(親フォルダを消す・コピーする事故を防ぐ)。"""
    if marks:
        return [e for e in entries if e.name in marks and not e.is_parent]
    if 0 <= cursor_index < len(entries):
        entry = entries[cursor_index]
        if not entry.is_parent:
            return [entry]
    return []


def invert_marks(entries, marks, include_dirs: bool) -> set:
    """マークを反転した新しい集合。include_dirs が False ならフォルダのマークは触らない。"""
    result = set(marks)
    for e in entries:
        if e.is_parent or (e.is_dir and not include_dirs):
            continue
        if e.name in result:
            result.discard(e.name)
        else:
            result.add(e.name)
    return result


def marks_summary(entries, marks):
    """(マーク数, マークしたファイルの合計バイト数)。フォルダの中身までは数えない
    (数えに行くと大きなフォルダで固まる。あふｗも同じ)。"""
    count = 0
    total = 0
    for e in entries:
        if e.name in marks and not e.is_parent:
            count += 1
            if not e.is_dir:
                total += e.size
    return count, total


# ---------------------------------------------------------------
# 表示の書式
# ---------------------------------------------------------------
def split_name(entry: Entry):
    """(名前, 拡張子)。フォルダと先頭がドットの名前(.gitignore)は分けない。"""
    if entry.is_dir:
        return entry.name, ""
    stem, ext = os.path.splitext(entry.name)
    return stem, ext[1:]


def format_size(entry: Entry) -> str:
    if entry.is_dir:
        return "<DIR>"
    return f"{entry.size:,}"


def format_mtime(mtime: float) -> str:
    if not mtime:
        return ""
    try:
        # あふｗと同じ書式(26/10/23 09:05:00)。秒まで出すのは、同じ分に保存した
        # ファイルの新旧を見分けるため(W の比較マークの前の目視にも効く)。
        return time.strftime("%y/%m/%d %H:%M:%S", time.localtime(mtime))
    except (OSError, OverflowError, ValueError):
        return ""


def format_bytes(total: int) -> str:
    """マーク合計の表示。バイト数を桁区切りで、大きければ単位付きも添える。"""
    text = f"{total:,}"
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if total >= size:
            return f"{text} ({total / size:.1f}{unit})"
    return text


# ---------------------------------------------------------------
# インクリメンタルサーチ
# ---------------------------------------------------------------
def isearch_index(entries, text: str):
    """text で始まる最初の項目、無ければ text を含む最初の項目の添字。無ければ None。
    大文字小文字は区別しない。".." は対象外。"""
    if not text:
        return None
    key = text.casefold()
    contains = None
    for index, e in enumerate(entries):
        if e.is_parent:
            continue
        name = e.name.casefold()
        if name.startswith(key):
            return index
        if contains is None and key in name:
            contains = index
    return contains


# ---------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------
class FolderHistory:
    """窓ごとの「戻る/進む」。ブラウザと同じく、戻った先で別の場所へ行くと進む側は捨てる。"""

    def __init__(self, limit: int = 100):
        self.limit = limit
        self.back_stack = []
        self.forward_stack = []
        self.current = None

    def visit(self, path: str) -> None:
        path = normalize_dir(path)
        if self.current is not None and same_dir(self.current, path):
            return
        if self.current is not None:
            self.back_stack.append(self.current)
            del self.back_stack[:-self.limit]
        self.forward_stack.clear()
        self.current = path

    def back(self):
        """戻る先を返して状態を進める。無ければ None(状態は変えない)。"""
        if not self.back_stack:
            return None
        if self.current is not None:
            self.forward_stack.append(self.current)
        self.current = self.back_stack.pop()
        return self.current

    def forward(self):
        if not self.forward_stack:
            return None
        if self.current is not None:
            self.back_stack.append(self.current)
        self.current = self.forward_stack.pop()
        return self.current

    def undo_step(self, direction: str, previous: str) -> None:
        """戻る/進むの移動に失敗したとき、状態を1手前に巻き戻す(元のフォルダに留まるので)。"""
        if direction == "back" and self.forward_stack:
            self.back_stack.append(self.current)
            self.forward_stack.pop()
        elif direction == "forward" and self.back_stack:
            self.forward_stack.append(self.current)
            self.back_stack.pop()
        self.current = previous


def push_recent(recent: list, path: str, limit: int = 50) -> list:
    """最近使ったフォルダの一覧(新しいものが先頭・重複なし)へ path を足した新しい並び。"""
    path = normalize_dir(path)
    result = [path] + [p for p in recent if not same_dir(p, path)]
    return result[:limit]


# ---------------------------------------------------------------
# 名前
# ---------------------------------------------------------------
_INVALID_NAME_RE = re.compile(r'[\\/:*?"<>|]')


def validate_name(name: str):
    """新しいフォルダ名・改名の入力を確かめる。問題なければ None、あれば理由の文字列。"""
    if not name or not name.strip():
        return "名前が空です"
    if _INVALID_NAME_RE.search(name):
        return '名前に使えない文字が入っています（\\ / : * ? " < > |）'
    if name in (".", ".."):
        return "その名前は使えません"
    if name != name.rstrip(" ."):
        return "末尾に空白やピリオドは置けません"
    return None


def unique_name(existing, base: str) -> str:
    """existing(名前の集まり)と大文字小文字を無視して重ならない名前。
    重なれば "base (2)", "base (3)" … とエクスプローラと同じ形で番号を振る。"""
    taken = {n.casefold() for n in existing}
    if base.casefold() not in taken:
        return base
    stem, ext = os.path.splitext(base)
    number = 2
    while True:
        candidate = f"{stem} ({number}){ext}"
        if candidate.casefold() not in taken:
            return candidate
        number += 1


def rename_selection_length(name: str, is_dir: bool) -> int:
    """改名の入力欄で最初に選んでおく文字数。拡張子の手前まで(エクスプローラと同じ)。"""
    if is_dir:
        return len(name)
    stem, _ext = os.path.splitext(name)
    return len(stem)


def name_conflicts(existing, new_name: str, old_name: str = None) -> bool:
    """new_name が既にある別の項目と(大文字小文字を無視して)重なるか。
    old_name 自身との一致は除く(大文字小文字だけを変える改名を通すため)。"""
    folded = new_name.casefold()
    old = old_name.casefold() if old_name else None
    return any(n.casefold() == folded and n.casefold() != old for n in existing)


# ---------------------------------------------------------------
# クリップボード(エクスプローラ互換のファイルのコピー/切り取り)
# ---------------------------------------------------------------
DROPEFFECT_COPY = 1
DROPEFFECT_MOVE = 2


def encode_drop_effect(move: bool) -> bytes:
    """"Preferred DropEffect" 形式の中身(DWORD リトルエンディアン)。"""
    return (DROPEFFECT_MOVE if move else DROPEFFECT_COPY).to_bytes(4, "little")


def decode_drop_effect(data) -> bool:
    """切り取り(移動)なら True。読めなければ False(＝コピー扱い。元を消さない側に倒す)。"""
    try:
        raw = bytes(data)
    except TypeError:
        return False
    if len(raw) < 4:
        return False
    return bool(int.from_bytes(raw[:4], "little") & DROPEFFECT_MOVE)


def double_null_join(paths) -> str:
    """SHFileOperationW の pFrom/pTo の形。NUL 区切り、末尾は NUL 2つ。"""
    return "".join(f"{p}\0" for p in paths) + "\0"


def clip_text(paths, names_only: bool = False) -> str:
    """C メニューの「フルパス/ファイル名のみをクリップボードへ」。改行(CRLF)区切り。"""
    items = [os.path.basename(p.rstrip("\\")) if names_only else p for p in paths]
    return "\r\n".join(items)


# ---------------------------------------------------------------
# 同名ファイル比較マーク(W)
# ---------------------------------------------------------------
# AFXW.TXT「同名ファイル比較マークについて」に合わせてある。
# 条件の値。「小さい/古い」はアクティブ窓の側が反対側より小さい/古い、の意味。
SIZE_CHOICES = ("smaller", "larger", "different", "any", "same")
TIME_CHOICES = ("older", "newer", "different", "any", "same")
SIZE_LABELS = ("小さい", "大きい", "異なる", "無関係", "同じ")
TIME_LABELS = ("古い", "新しい", "異なる", "無関係", "同じ")
DEFAULT_TIME_TOLERANCE = 2.0

# ダイアログのショートカット。(サイズ, タイムスタンプ, 拡張子を除くか)。押した時点で決定する。
COMPARE_SHORTCUTS = {
    "A": ("same", "same", False),
    "N": ("different", "different", False),
    "S": ("same", "any", False),
    "T": ("any", "same", False),
    "E": ("any", "any", True),
}


def size_matches(cond: str, mine: int, theirs: int) -> bool:
    if cond == "smaller":
        return mine < theirs
    if cond == "larger":
        return mine > theirs
    if cond == "different":
        return mine != theirs
    if cond == "same":
        return mine == theirs
    return True


def time_matches(cond: str, mine: float, theirs: float, tolerance: float) -> bool:
    """時刻の条件。許容誤差(秒)以内の差は「同じ」とみなす(FAT は2秒刻み、ネットワーク越しでも
    ずれる)。古い/新しい/異なるも、誤差を超えたときだけ成り立つ。"""
    diff = mine - theirs
    tolerance = abs(tolerance or 0.0)
    if cond == "older":
        return diff < -tolerance
    if cond == "newer":
        return diff > tolerance
    if cond == "different":
        return abs(diff) > tolerance
    if cond == "same":
        return abs(diff) <= tolerance
    return True


def _compare_key(entry: Entry, strip_ext: bool) -> str:
    name = split_name(entry)[0] if strip_ext else entry.name
    return name.casefold()


def compare_marks(mine, theirs, size_cond="any", time_cond="any", include_dirs=False,
                  strip_ext=False, tolerance=DEFAULT_TIME_TOLERANCE) -> set:
    """反対窓(theirs)に同名があるものの名前の集合(mine の側の名前)。

    - 名前は大文字小文字を無視して比べる
    - ファイルとフォルダの組み合わせでも同名ならマーク(条件は見ない)
    - フォルダ同士は include_dirs のときだけ。そのときは条件を見ずにマーク
    - strip_ext なら拡張子を除いた名前で比べる。反対側に候補が複数ありうるので
      サイズとタイムスタンプの条件は使わない(どれか1つと同名なら成り立つ)
    - ".." は対象外"""
    others = {}
    for o in theirs:
        if o.is_parent:
            continue
        others.setdefault(_compare_key(o, strip_ext), []).append(o)
    found = set()
    for e in mine:
        if e.is_parent:
            continue
        for o in others.get(_compare_key(e, strip_ext), ()):
            if e.is_dir and o.is_dir:
                ok = include_dirs
            elif e.is_dir != o.is_dir or strip_ext:
                ok = True
            else:
                ok = (size_matches(size_cond, e.size, o.size)
                      and time_matches(time_cond, e.mtime, o.mtime, tolerance))
            if ok:
                found.add(e.name)
                break
    return found


# ---------------------------------------------------------------
# エクスプローラの右クリックメニュー(F7)
# ---------------------------------------------------------------
# メニューそのものは COM(IContextMenu)が要るので、ファイラ本体ではなく子プロセス
# (filer_contextmenu.py)が出す。ここには、どちらのプロセスでも使う「何を・どこに」を
# 決める部分だけを置く(COM も Qt も使わない)。

# コマンドラインに載せる長さの上限(文字)。Windows の上限は 32767 文字だが、余裕を
# 見てこれを超えたら一時 JSON で渡す(マークを何百件もしたとき)。
CONTEXT_ARGS_MAX = 8000


def context_targets(folder: str, entries, marks, cursor_index: int) -> list:
    """F7 のメニューの対象(フルパス)。

    マークがあればマークしたもの、無ければカーソル行。カーソルが ".." でマークも
    無ければ、いま見ているフォルダ自身(あふｗで「..」の上から出すと「.」が対象になる
    のと同じ)。"""
    targets = pick_targets(entries, marks, cursor_index)
    if targets:
        return [os.path.join(folder, e.name) for e in targets]
    return [folder] if folder else []


def to_native_point(x: float, y: float, origin_x: int, origin_y: int, dpr: float):
    """Qt の論理座標(画面の左上は物理座標のまま、その中が拡大率で縮んでいる)を、
    Win32 の物理座標にする。拡大率の違う画面が並んでいても、その画面の左上を基準に
    戻せば合う(Qt 6 の Windows での座標の持ち方)。"""
    dpr = dpr or 1.0
    return (int(round(origin_x + (x - origin_x) * dpr)),
            int(round(origin_y + (y - origin_y) * dpr)))


def context_menu_args(paths, x: int, y: int, owner: int = 0):
    """子プロセスへ渡す引数。長すぎるなら None(呼ぶ側が一時 JSON で渡す)。

    パスは "--" の後ろに並べる("-" で始まる名前を指定と取り違えないように)。"""
    args = ["--x", str(int(x)), "--y", str(int(y)), "--owner", str(int(owner or 0)), "--"]
    args += [str(p) for p in paths]
    if sum(len(a) + 3 for a in args) > CONTEXT_ARGS_MAX:
        return None
    return args


def context_menu_payload(paths, x: int, y: int, owner: int = 0) -> dict:
    """一時 JSON で渡すときの中身(context_menu_args と同じ情報)。"""
    return {"paths": [str(p) for p in paths], "x": int(x), "y": int(y),
            "owner": int(owner or 0)}


def parse_context_args(argv) -> dict:
    """子プロセス側の引数の読み取り。{"paths", "x", "y", "owner", "list", "repeat", "json"}。

    --json があればそのファイルから paths/x/y/owner を読む(読んだら消すのは呼ぶ側)。"""
    import argparse

    parser = argparse.ArgumentParser(prog="filer_contextmenu.py", add_help=False)
    parser.add_argument("--x", type=int, default=0)
    parser.add_argument("--y", type=int, default=0)
    parser.add_argument("--owner", type=int, default=0)
    parser.add_argument("--json")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("paths", nargs="*")
    ns = parser.parse_args(list(argv))
    result = {"paths": list(ns.paths), "x": ns.x, "y": ns.y, "owner": ns.owner,
              "list": ns.list, "repeat": max(1, ns.repeat), "json": ns.json}
    if ns.json:
        import json

        with open(ns.json, "r", encoding="utf-8") as f:
            data = json.load(f)
        result.update(paths=[str(p) for p in data.get("paths", [])],
                      x=int(data.get("x", 0)), y=int(data.get("y", 0)),
                      owner=int(data.get("owner", 0)))
    return result


# ---------------------------------------------------------------
# 頭文字ジャンプ(Ctrl+英字 / Ctrl+数字。あふｗ AFXW.TXT 290-291行)
# ---------------------------------------------------------------
def initial_jump_char(vk: int, shift: bool = False, ctrl: bool = False, alt: bool = False):
    """Ctrl だけを押した英数字キーなら、その文字(英字は小文字)。それ以外は None。

    テンキーの 0〜9(VK 96〜105)も数字として扱う。Ctrl+Shift / Ctrl+Alt は対象外。"""
    if not ctrl or shift or alt:
        return None
    if 65 <= vk <= 90:
        return chr(vk).lower()
    if 48 <= vk <= 57:
        return chr(vk)
    if 96 <= vk <= 105:
        return chr(vk - 48)
    return None


def disabled_codes(overrides) -> set:
    """settings.json の filer.keys で空文字(や null)にして外したキー。
    頭文字ジャンプのように一括で割り当てるものも、ここに挙がったキーでは動かさない。"""
    if not isinstance(overrides, dict):
        return set()
    return {code for code, command in overrides.items()
            if isinstance(code, str) and _SCAV_RE.match(code) and not command}


def initial_jump_index(entries, char: str, current: int):
    """名前が char で始まる(大文字小文字を無視)次の項目の添字。無ければ None。

    カーソルの次の行から下へ探し、末尾まで無ければ先頭から巡回する(押すたびに次へ)。
    ".." は対象外。一致がカーソル行1つだけなら、そこに留まる。"""
    if not char or not entries:
        return None
    key = char.casefold()
    count = len(entries)
    start = current if 0 <= current < count else -1
    for step in range(1, count + 1):
        index = (start + step) % count
        entry = entries[index]
        if not entry.is_parent and entry.name.casefold().startswith(key):
            return index
    return None


# ---------------------------------------------------------------
# 範囲マーク(Ctrl+Space / Shift+Ctrl+Space。AFXW.TXT「直上/直下のマークから
# カーソル位置まで全てマーク」)
# ---------------------------------------------------------------
def range_mark_names(entries, marks, cursor: int, direction: int) -> set:
    """新しくマークする名前の集まり。

    direction が -1 ならカーソルより上で一番近いマーク行から、+1 なら下で一番近い
    マーク行から、カーソル行までを全部(フォルダも)。カーソル行自身のマークは数えない
    (「直上/直下のマーク」なので、その行から外側を探す)。その方向にマークが無ければ
    空(何もしない)。".." はマークしない。カーソルは動かさない(呼ぶ側も動かさない)。"""
    if not (0 <= cursor < len(entries)) or not marks:
        return set()
    row = cursor + direction
    while 0 <= row < len(entries):
        if entries[row].name in marks and not entries[row].is_parent:
            low, high = sorted((row, cursor))
            return {e.name for e in entries[low:high + 1] if not e.is_parent}
        row += direction
    return set()


# ---------------------------------------------------------------
# 簡易テキストビュア(Enter の拡張子判別)
# ---------------------------------------------------------------
DEFAULT_VIEWER_EXTENSIONS = (
    "log txt csv tsv md json xml ini cfg conf yaml yml py ps1 bat cmd sql html htm css js "
    "ts c h cpp java sh"
).split()


def normalize_viewer_extensions(value) -> frozenset:
    """settings.json の filer.viewer_extensions(配列か空白区切りの文字列)を、点なし・
    小文字の集合にする。書かれていなければ既定。"""
    if value is None:
        value = DEFAULT_VIEWER_EXTENSIONS
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, (list, tuple, set, frozenset)):
        value = DEFAULT_VIEWER_EXTENSIONS
    return frozenset(str(v).strip().lstrip(".").lower() for v in value if str(v).strip())


def opens_in_viewer(name: str, extensions) -> bool:
    """Enter でビュアに回すファイルか(拡張子で判別。あふｗの「拡張子判別実行」)。"""
    ext = os.path.splitext(name)[1].lstrip(".").lower()
    return bool(ext) and ext in extensions


# ---------------------------------------------------------------
# ZIP 圧縮(P)
# ---------------------------------------------------------------
def default_zip_name(folder: str, targets) -> str:
    """作る ZIP の名前の既定。対象が1件ならその名前(拡張子を除く)、複数ならいまの
    フォルダ名。ドライブ直下(C:\\)なら "C"。"""
    if len(targets) == 1:
        entry = targets[0]
        stem = entry.name if entry.is_dir else os.path.splitext(entry.name)[0]
        return f"{stem or entry.name}.zip"
    base = os.path.basename(normalize_dir(folder).rstrip("\\/"))
    if not base:
        base = os.path.splitdrive(folder)[0].rstrip(":") or "archive"
    return f"{base}.zip"


def zip_plan(folder: str, names, exclude=None, walk=os.walk) -> list:
    """folder の names を ZIP に入れる順の [(実パス, 書庫内の名前, フォルダか)]。

    フォルダは中身ごと(空のフォルダも入れる)。書庫内の名前は folder からの相対で、
    区切りは "/"。exclude(作ろうとしている ZIP 自身)は入れない。隠し・システム属性の
    ものも入れる(Shift+Z の表示の絞り込みは「見え方」の設定で、中身を選ぶものではない)。"""
    skip = os.path.normcase(os.path.abspath(exclude)) if exclude else None
    plan = []

    def add(path, arc, is_dir):
        if skip and os.path.normcase(os.path.abspath(path)) == skip:
            return
        plan.append((path, arc, is_dir))

    for name in names:
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            add(path, name + "/", True)
            for root, dirs, files in walk(path):
                dirs.sort(key=str.casefold)
                rel_root = os.path.relpath(root, folder).replace(os.sep, "/")
                for d in dirs:
                    add(os.path.join(root, d), f"{rel_root}/{d}/", True)
                for f in sorted(files, key=str.casefold):
                    add(os.path.join(root, f), f"{rel_root}/{f}", False)
        else:
            add(path, name, False)
    return plan


# ---------------------------------------------------------------
# WinMerge で比較(Alt+W)
# ---------------------------------------------------------------
WINMERGE_HINT = "左右に1件ずつ（または片側に2件）マークしてください"


def winmerge_pair(left_marked, right_marked):
    """比べる2つ。[(側, 名前), (側, 名前)] か、規則に合わなければ None。

    - 左に1件・右に1件 → その2つ(左が先)
    - 片側だけに2件(もう片側は0件) → その2件
    - それ以外(0件、3件以上、1件と0件 など)は None"""
    left, right = list(left_marked), list(right_marked)
    if len(left) == 1 and len(right) == 1:
        return [("left", left[0]), ("right", right[0])]
    if len(left) == 2 and not right:
        return [("left", left[0]), ("left", left[1])]
    if len(right) == 2 and not left:
        return [("right", right[0]), ("right", right[1])]
    return None


WINMERGE_APP_PATH_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WinMergeU.exe"


def winmerge_candidates(setting, environ) -> list:
    """WinMergeU.exe を探す順のうち、ファイルの場所として決まるもの(レジストリは別)。"""
    found = []
    if setting:
        found.append(os.path.expandvars(str(setting)))
    for var, rel in (("ProgramFiles", r"WinMerge\WinMergeU.exe"),
                     ("ProgramFiles(x86)", r"WinMerge\WinMergeU.exe"),
                     ("LOCALAPPDATA", r"Programs\WinMerge\WinMergeU.exe")):
        base = environ.get(var)
        if base:
            found.append(os.path.join(base, rel))
    return found


def find_winmerge(setting=None, environ=None, exists=os.path.isfile, registry=None):
    """WinMergeU.exe のパス。見つからなければ None。

    探す順は settings の filer.winmerge → レジストリの App Paths(registry() が返す
    パスの並び。HKLM・HKCU) → Program Files / Program Files (x86) / LOCALAPPDATA。
    exists と registry は差し替えられる(テストで実際のディスクやレジストリを見ないため)。"""
    environ = os.environ if environ is None else environ
    candidates = []
    if setting:
        candidates.append(os.path.expandvars(str(setting)))
    if registry is not None:
        try:
            candidates.extend(p for p in registry() if p)
        except OSError:
            pass
    candidates.extend(winmerge_candidates(None, environ))
    for path in candidates:
        path = path.strip().strip('"')
        if path and exists(path):
            return path
    return None


def registry_winmerge_paths() -> list:
    """レジストリの App Paths に登録された WinMergeU.exe(HKLM, HKCU の順)。"""
    import winreg

    paths = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, WINMERGE_APP_PATH_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, "")
                if value:
                    paths.append(os.path.expandvars(str(value)))
        except OSError:
            continue
    return paths
