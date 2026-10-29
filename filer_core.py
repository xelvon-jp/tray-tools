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
import hashlib
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
    "1013": "open_in_other",      # Shift+Enter フォルダは反対窓で開く/ファイルはエディタ
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
    "0116": "reload_keep",        # F5 (エクスプローラの F5 と同じく最新にするだけ。マークは保持)
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
    "0071": "grep",               # G 文字列検索(内容で探して結果をフラット表示)
    "0083": "sort_dialog",        # S ソート
    "1083": "sort_reverse",       # Shift+S 昇順/降順の反転(AFXW.key 1083 &SORT !)
    "1090": "settings_menu",      # Shift+Z 各種設定一時切替(隠し/システムファイルの表示)
    "1072": "frecency_jump",      # Shift+H よく行くフォルダへ(zoxide 風)
    "4066": "flat_view",          # Alt+B フラット表示(Total Commander の Ctrl+B)
    "0114": "preview_toggle",     # F3 プレビュー(反対窓の位置に出す)
    "1082": "batch_rename",       # Shift+R 一括改名(Renemo 風)
    "4082": "rename_menu",        # Alt+R 改名メニュー(AFXW.key の Alt+R と同じ指で)
    "0087": "compare_mark",       # W 同名ファイル比較マーク
    "1087": "sync_copy",          # Shift+W フォルダ同期コピー(コピーのみ)
    "0090": "settings_dialog",    # Z 設定(あふｗの Z ＝設定プログラム起動)
    "0191": "live_filter",        # / ライブ絞り込み(XYplorer の Live Filter / yazi の filter)
    "4087": "winmerge",           # Alt+W WinMerge で比較(AFXW.key 4087 &MENU hikaku.txt の代わり)
    "0080": "zip_create",         # P ZIP 圧縮(反対窓に作る。AFXW.key 0080 &MENU press.txt の代わり)
    # --- マスク ---
    "0186": "mask_clear",         # :  (VK_OEM_1)
    "1186": "mask_input",         # Shift+: (AFXW.key 1186 &MASK)
    # --- ファイル操作 ---
    "0084": "stash_toggle",       # T 一時置き場を開く/戻る
    "1084": "stash_add",          # Shift+T 一時置き場へ登録
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
    "1081": "queue_clear",        # Shift+Q 転送キューの待ちを取り消す
    "4115": "quit",               # Alt+F4 (AFXW.key 4115:0081)
}

# ---------------------------------------------------------------
# 窓の配分と字の大きさ(Alt+矢印 / Shift・Ctrl+↑↓)
# ---------------------------------------------------------------
DEFAULT_SPLIT_RATIO = 0.5
SPLIT_STEP = 0.03          # Alt+←→ 1回で動かす割合(窓幅の3%)
SPLIT_MIN = 0.0            # 境界は窓の端まで寄せられる(片方を畳み切れる。2026-10-29 要望)
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


def folder_signature(path: str):
    """フォルダの中身の指紋。(名前, フォルダか, サイズ, 更新時刻ns) の組を名前順に並べたもの。

    外部で中身が変わったかを安く検めるためのもの(自動再読込の判定に使う)。
    scandir の stat は Windows では検索データのキャッシュが効くので、項目数ぶんの
    追加の読み取りにはならない。読めない項目(走査中に消えた等)は飛ばす。
    フォルダ自体が読めなければ例外(OSError)が上がるので、呼び側で受けること。"""
    items = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
                items.append((entry.name, entry.is_dir(follow_symlinks=False),
                              st.st_size, st.st_mtime_ns))
            except OSError:
                continue
    items.sort(key=lambda x: x[0])
    return tuple(items)


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
# ライブ絞り込み(/)
# ---------------------------------------------------------------
def parse_live_filter(text: str) -> list:
    """絞り込みの語。空白区切りで、すべてを含むものを残す(AND)。大文字小文字は無視。"""
    return [word.casefold() for word in (text or "").split() if word]


def live_filter_entries(entries, words: list) -> list:
    """名前に words をすべて含むものだけ残す。フォルダは常に残す(マスクと同じく、
    絞り込み中でも移動できるように)。フラット表示では名前が相対パスなので、
    フォルダ名で絞ることもできる。"""
    if not words:
        return list(entries)
    found = []
    for e in entries:
        if e.is_dir:
            found.append(e)
            continue
        name = e.name.casefold()
        if all(word in name for word in words):
            found.append(e)
    return found


# ---------------------------------------------------------------
# フラット表示(Alt+B。Total Commander のブランチビュー)
# ---------------------------------------------------------------
FLAT_LIMIT = 200_000


def collect_flat(root: str, show_hidden: bool = True, show_system: bool = True,
                 limit: int = FLAT_LIMIT, cancel=None, progress=None):
    """root より下のファイルを全部集める(フォルダ自体は入れない)。読み取りスレッドで呼ぶ。

    (項目, 状態) を返す。項目の名前は root からの相対パス("2026-10\\log_001.txt")。
    状態は "done" / "limit"(limit 件で打ち切った) / "cancel"(cancel が立った)。
    - シンボリックリンク・ジャンクション(再解析ポイント)のフォルダはたどらない(ループ防止)
    - 隠し・システムは show_hidden / show_system(Shift+Z)に従う。隠しフォルダの中にも入らない
    - 読めないフォルダは飛ばす(全体を止めない)
    - progress(件数) をフォルダごとに呼ぶ"""
    found = []
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            return found, "cancel"
        folder = stack.pop()
        subdirs = []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    attrs = getattr(st, "st_file_attributes", 0)
                    if not show_hidden and attrs & stat.FILE_ATTRIBUTE_HIDDEN:
                        continue
                    if not show_system and attrs & stat.FILE_ATTRIBUTE_SYSTEM:
                        continue
                    if attrs & stat.FILE_ATTRIBUTE_DIRECTORY:
                        if not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            subdirs.append(entry.path)
                        continue
                    found.append(Entry(os.path.relpath(entry.path, root), False,
                                       st.st_size, st.st_mtime, attrs))
                    if len(found) >= limit:
                        return found, "limit"
        except OSError:
            continue
        stack.extend(sorted(subdirs, key=str.casefold, reverse=True))
        if progress is not None:
            progress(len(found))
    return found, "done"


def flat_footer_text(files: int, total: int, mark_count: int = 0, mark_bytes: int = 0) -> str:
    """フラット表示のときの窓の下の行。件数と合計サイズ。"""
    text = f"{files} {'Files' if files >= 2 else 'File'}   {total:,} bytes"
    if mark_count:
        text += f"   Mark {mark_count} ({mark_bytes:,})"
    return text


# ---------------------------------------------------------------
# 文字列検索(Grep。G キー)
# ---------------------------------------------------------------
GREP_LIMIT = 2_000            # ヒット件数の上限(結果が一覧にならない量は実用でないため)
GREP_FILE_MAX = 32 * 1024 * 1024   # これより大きいファイルは中身を見ない
GREP_LINE_MAX = 200           # ログに出すヒット行の長さ

def _decode_text(data: bytes):
    """UTF-8(BOM付き含む)→ cp932 の順に試す。どちらでもなければ None(バイナリ扱い)。"""
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def grep_in_bytes(data: bytes, needle: str, case_sensitive: bool = False):
    """1ファイル分のバイト列から needle を探す。(ヒット行数, 最初のヒット行) か None。

    テキストとして読めたら行ごとに探す(ヒット行の文字列も返る)。読めなければ
    バイト列のまま探し、ヒット行は空文字(行番号が分からないため件数だけ数える)。
    大文字小文字の区別なしは casefold で比べる(バイト列のときは ASCII 範囲だけ実質効く)。"""
    text = _decode_text(data)
    if text is not None:
        probe = needle if case_sensitive else needle.casefold()
        count = 0
        first = ""
        for line in text.splitlines():
            hay = line if case_sensitive else line.casefold()
            if probe in hay:
                count += 1
                if not first:
                    first = line.strip()[:GREP_LINE_MAX]
        return (count, first) if count else None
    needles = {needle.encode("utf-8"), needle.encode("cp932", "ignore")}
    needles.discard(b"")
    if not needles:
        return None
    count = 0
    for chunk in data.split(b"\n"):
        hay = chunk if case_sensitive else chunk.lower()
        if any((n if case_sensitive else n.lower()) in hay for n in needles):
            count += 1
    return (count, "") if count else None


def grep_files(root: str, needle: str, case_sensitive: bool = False,
               show_hidden: bool = True, show_system: bool = True,
               limit: int = GREP_LIMIT, cancel=None, progress=None):
    """root より下のファイルの中身から needle を探す。読み取りスレッドで呼ぶ。

    (項目, ヒット情報, 状態) を返す。項目は collect_flat と同じ相対パス名の Entry。
    ヒット情報は {相対パス: (ヒット行数, 最初のヒット行)}。状態は "done" /
    "limit" / "cancel"。たどる規則(再解析ポイントは入らない・隠し/システムの
    扱い・読めないフォルダは飛ばす)は collect_flat と同じ。書庫の中は見ない。
    progress(走査したファイル数) をフォルダごとに呼ぶ。"""
    found = []
    matches = {}
    scanned = 0
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            return found, matches, "cancel"
        folder = stack.pop()
        subdirs = []
        try:
            with os.scandir(folder) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            if cancel is not None and cancel.is_set():
                return found, matches, "cancel"
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attrs = getattr(st, "st_file_attributes", 0)
            if not show_hidden and attrs & stat.FILE_ATTRIBUTE_HIDDEN:
                continue
            if not show_system and attrs & stat.FILE_ATTRIBUTE_SYSTEM:
                continue
            if attrs & stat.FILE_ATTRIBUTE_DIRECTORY:
                if not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    subdirs.append(entry.path)
                continue
            scanned += 1
            if st.st_size > GREP_FILE_MAX:
                continue
            try:
                with open(entry.path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            hit = grep_in_bytes(data, needle, case_sensitive)
            if hit is None:
                continue
            rel = os.path.relpath(entry.path, root)
            found.append(Entry(rel, False, st.st_size, st.st_mtime, attrs))
            matches[rel] = hit
            if len(found) >= limit:
                return found, matches, "limit"
        stack.extend(sorted(subdirs, key=str.casefold, reverse=True))
        if progress is not None:
            progress(scanned)
    return found, matches, "done"


def stash_entries(paths):
    """一時置き場のパス一覧から (項目, 消えていたパス) を作る。

    項目の名前はフルパスそのものにする。そうしておくと、窓側の
    os.path.join(窓のパス, 名前) が絶対パスの規則でそのまま実パスに
    なるので、コピー・移動・プレビュー等の既存の操作が置き場でも
    そのまま動く(フラット表示が相対パスでやっていることの絶対版)。"""
    entries, missing = [], []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            missing.append(path)
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        entries.append(Entry(path, is_dir, 0 if is_dir else st.st_size,
                             st.st_mtime,
                             getattr(st, "st_file_attributes", 0)))
    return entries, missing


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
# 窓の下の行・ステータスバー(あふｗの画面の書式)
# ---------------------------------------------------------------
def format_gb(size: int) -> str:
    """ドライブの容量の表示。GB(2^30)で小数2桁(あふｗと同じ "475.69 GB")。"""
    return f"{size / (1 << 30):.2f} GB"


def footer_text(dirs: int, files: int, total=None, free=None,
                mark_count: int = 0, mark_bytes: int = 0) -> str:
    """ファイル窓の下の行。あふｗの "58 Dirs   0 File   475.69 GB ( 100.38 GB)"。

    ファイルは1件以下なら "File"、2件以上なら "Files"(あふｗの表示どおり)。フォルダは
    あふｗの画面では常に "Dirs" なので合わせる。容量(total)と空き(free)は分かったとき
    だけ出す(書庫の中・読めないドライブでは出さない)。マークがあれば後ろに件数と合計。"""
    text = f"{dirs} Dirs   {files} {'Files' if files >= 2 else 'File'}"
    if total is not None and free is not None:
        text += f"   {format_gb(total)} ( {format_gb(free)})"
    if mark_count:
        text += f"   Mark {mark_count} ({mark_bytes:,})"
    return text


def count_dirs_files(entries):
    """(フォルダ数, ファイル数)。".." は数えない。"""
    dirs = sum(1 for e in entries if e.is_dir and not e.is_parent)
    files = sum(1 for e in entries if not e.is_dir)
    return dirs, files


def attr_letters(entry: "Entry") -> str:
    """ステータスバーの属性の文字。D=フォルダ R=読み込み専用 H=隠し S=システム
    A=アーカイブ(あふｗの並び)。"""
    attrs = entry.attrs
    letters = ""
    if entry.is_dir:
        letters += "D"
    for flag, letter in ((stat.FILE_ATTRIBUTE_READONLY, "R"), (stat.FILE_ATTRIBUTE_HIDDEN, "H"),
                         (stat.FILE_ATTRIBUTE_SYSTEM, "S"), (stat.FILE_ATTRIBUTE_ARCHIVE, "A")):
        if attrs & flag:
            letters += letter
    return letters


def status_fields(entry) -> tuple:
    """ステータスバーのカーソルの欄。(名前, サイズか <DIR>, 日時, 属性)。
    ".." は名前だけ。カーソルが無ければ全部空。"""
    if entry is None:
        return ("", "", "", "")
    if entry.is_parent:
        return ("..", "", "", "")
    return (entry.name, format_size(entry), format_mtime(entry.mtime), attr_letters(entry))


def drive_root(path: str):
    """path のドライブの根。"C:\\" や "\\\\server\\share\\"。分からなければ None。"""
    if not path:
        return None
    drive, _rest = os.path.splitdrive(os.path.normpath(path))
    if not drive:
        return None
    return drive.rstrip("\\/") + "\\"


def unc_label(root: str):
    """UNC の根なら "server\\share"(窓の2行目にラベルの代わりに出す)。でなければ None。"""
    if root and root.startswith("\\\\"):
        return root.strip("\\")
    return None


# ---------------------------------------------------------------
# ファンクションキーの一覧(窓の下の段)
# ---------------------------------------------------------------
# コマンドの短い表示名。ボタンの幅は窓の 1/12 なので、なるべく短く。
# 書いていないコマンドはコマンド名をそのまま出す。settings の filer.fkey_labels で
# キーごとに上書きできる(fkey_labels を参照)。
COMMAND_SHORT_NAMES = {
    "context_menu": "Menu",
    "open_shell": "PowerShell",
    "open_explorer": "Explorer",
    "quit": "Quit",
    "reload": "Reload",
    "reload_keep": "Reload",
    "reload_other": "Reload他",
    "enter": "Open",
    "open_assoc": "Assoc",
    "edit": "Edit",
    "open_in_other": "Open他",
    "copy_menu": "Copy",
    "copy_direct": "Copy",
    "move_menu": "Move",
    "move_direct": "Move",
    "delete": "Delete",
    "delete_permanent": "Del!",
    "mkdir": "MkDir",
    "rename": "Rename",
    "isearch": "Search",
    "grep": "Grep",
    "sort_dialog": "Sort",
    "sort_reverse": "Sort逆",
    "compare_mark": "Compare",
    "sync_copy": "Sync",
    "queue_clear": "QueueClear",
    "winmerge": "WinMerge",
    "zip_create": "Zip",
    "mask_input": "Mask",
    "mask_clear": "Mask*.*",
    "history_jump": "History",
    "bookmark_jump": "Bookmark",
    "stash_toggle": "Stash",
    "stash_add": "Stash+",
    "path_jump": "Jump",
    "desktop": "Desktop",
    "drive": "Drive",
    "live_filter": "Filter",
    "frecency_jump": "Frequent",
    "flat_view": "Flat",
    "preview_toggle": "Preview",
    "batch_rename": "Renemo",
    "rename_menu": "RenameMenu",
    "settings_menu": "Settings",
    "settings_dialog": "Config",
    "swap_paths": "Swap",
    "sync_to_other": "Sync",
    "sync_other_to_me": "Sync他",
    "parent": "Parent",
    "switch": "Switch",
}

FKEY_COUNT = 12
_FKEY_VK_BASE = 112  # VK_F1


def fkey_command(keymap: dict, modifiers: int, number: int):
    """F<number>(1〜12)に、修飾(SHIFT=1 / CTRL=2 / ALT=4 の和)付きで割り当てた
    コマンド名。無ければ None。"""
    return keymap.get(f"{modifiers}{_FKEY_VK_BASE + number - 1:03d}") or None


def _fkey_override_names(modifiers: int, number: int) -> list:
    """filer.fkey_labels で探す名前。"F7" / "Shift+F7" / "Ctrl+F7" / "Ctrl+Shift+F7" と、
    あふｗの4桁("1118")のどちらでも書ける。"""
    parts = []
    if modifiers & CTRL:
        parts.append("Ctrl")
    if modifiers & SHIFT:
        parts.append("Shift")
    if modifiers & ALT:
        parts.append("Alt")
    name = "+".join(parts + [f"F{number}"])
    return [name, name.lower(), f"{modifiers}{_FKEY_VK_BASE + number - 1:03d}"]


def fkey_labels(keymap: dict, modifiers: int = 0, overrides=None) -> list:
    """F1〜F12 の区画に出す12個の表示名(割り当ての無いキーは空)。

    キー表(filer.keys の上書き込み)から作るので、割り当てを変えれば表示も変わる。
    overrides(filer.fkey_labels)は表示名だけを差し替える。"""
    overrides = overrides if isinstance(overrides, dict) else {}
    labels = []
    for number in range(1, FKEY_COUNT + 1):
        command = fkey_command(keymap, modifiers, number)
        label = ""
        if command:
            label = COMMAND_SHORT_NAMES.get(command, command)
        for name in _fkey_override_names(modifiers, number):
            if name in overrides and isinstance(overrides[name], str):
                label = overrides[name]
                break
        labels.append(label)
    return labels


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


# ---------------------------------------------------------------
# ファイル情報の編集(R。あふｗの「ファイル情報の編集 / 一括変更」)
# ---------------------------------------------------------------
def caret_position(name: str, is_dir: bool) -> int:
    """ファイル名の入力欄を開いたときのキャレットの位置。拡張子の手前(".webp" の "." の前)。
    拡張子の無い名前・フォルダ・先頭がドットの名前(.gitignore)は末尾。何も選ばない。"""
    return rename_selection_length(name, is_dir)


CASE_MODES = ("lower", "swap", "upper")


def _convert(text: str, mode: str) -> str:
    if mode == "lower":
        return text.lower()
    if mode == "upper":
        return text.upper()
    if mode == "swap":
        return text.swapcase()
    return text


def convert_case(name: str, part: str, mode: str, is_dir: bool = False) -> str:
    """「変換」の枠。part は "all"(名前全体)か "ext"(拡張子だけ)、mode は
    "lower" / "upper" / "swap"(大小の入れ替え)。フォルダの「拡張子」は変えない。"""
    if part == "all":
        return _convert(name, mode)
    if is_dir:
        return name
    stem, ext = os.path.splitext(name)
    return stem + _convert(ext, mode)


# SetFileAttributesW で変えられる属性のうち、ダイアログで扱う4つ。
ATTR_FLAGS = {
    "readonly": stat.FILE_ATTRIBUTE_READONLY,
    "system": stat.FILE_ATTRIBUTE_SYSTEM,
    "hidden": stat.FILE_ATTRIBUTE_HIDDEN,
    "archive": stat.FILE_ATTRIBUTE_ARCHIVE,
}
# SetFileAttributesW に渡してよい属性(これ以外のビット、例えばフォルダの印は渡さない)。
SETTABLE_ATTRS = (stat.FILE_ATTRIBUTE_READONLY | stat.FILE_ATTRIBUTE_HIDDEN
                  | stat.FILE_ATTRIBUTE_SYSTEM | stat.FILE_ATTRIBUTE_ARCHIVE
                  | stat.FILE_ATTRIBUTE_TEMPORARY | stat.FILE_ATTRIBUTE_OFFLINE
                  | stat.FILE_ATTRIBUTE_NOT_CONTENT_INDEXED)
FILE_ATTRIBUTE_NORMAL = stat.FILE_ATTRIBUTE_NORMAL


def compose_attrs(current: int, changes: dict) -> int:
    """今の属性に changes({"readonly": True/False, ...}。書いていないものは変えない)を
    重ねた、SetFileAttributesW に渡す値。何も立たなければ FILE_ATTRIBUTE_NORMAL。"""
    value = current & SETTABLE_ATTRS
    for key, on in changes.items():
        flag = ATTR_FLAGS[key]
        value = (value | flag) if on else (value & ~flag)
    return value or FILE_ATTRIBUTE_NORMAL


def attrs_to_flags(attrs: int) -> dict:
    return {key: bool(attrs & flag) for key, flag in ATTR_FLAGS.items()}


def make_timestamp(year, month, day, hour, minute, second, base=None):
    """入力された年月日時分秒を時刻(秒)にする。(時刻, None) か (None, 理由)。

    一括変更であふｗと同じく年・月・日をすべて 0 にしたときは、base(そのファイルの
    元の日時)の年月日のまま時分秒だけを変える。base が無ければ誤り。"""
    from datetime import datetime

    try:
        values = [int(v) for v in (year, month, day, hour, minute, second)]
    except (TypeError, ValueError):
        return None, "日時が数字ではありません"
    year, month, day, hour, minute, second = values
    if year == month == day == 0:
        if base is None:
            return None, "年月日が 0 です"
        original = datetime.fromtimestamp(base)
        year, month, day = original.year, original.month, original.day
    try:
        stamp = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None, (f"存在しない日時です: {year:04d}/{month:02d}/{day:02d} "
                      f"{hour:02d}:{minute:02d}:{second:02d}")
    if stamp.year < 1980:
        # FAT や ZIP が扱えない古い日付。NTFS では持てるが、事故の元なので止める。
        return None, "1980年より前の日時は設定できません"
    try:
        return time.mktime(stamp.timetuple()), None
    except (OverflowError, ValueError) as e:
        return None, f"日時を設定できません: {e}"


def plan_file_info(entry, new_name=None, mtime=None, attr_changes=None) -> dict:
    """1件ぶんの変更内容。変わらないものは入れない(何も無ければ空の dict)。

    {"old": 元の名前, "new": 新しい名前, "mtime": 新しい日時, "attrs": 新しい属性}。
    名前は大文字小文字だけの違いも「変わった」とみなす。日時は1秒未満の差を無視する。"""
    plan = {}
    if new_name is not None and new_name != entry.name:
        plan["new"] = new_name
    if mtime is not None and abs(mtime - entry.mtime) >= 1:
        plan["mtime"] = mtime
    if attr_changes:
        current = entry.attrs & SETTABLE_ATTRS
        wanted = compose_attrs(entry.attrs, attr_changes)
        if (wanted & ~FILE_ATTRIBUTE_NORMAL) != current:
            plan["attrs"] = wanted
    if plan:
        plan["old"] = entry.name
    return plan


def plan_bulk_info(entries, case_all=None, case_ext=None, stamp_fields=None,
                   attr_changes=None):
    """一括変更の計画。([1件ごとの変更], [(名前, 理由)])。

    case_all / case_ext は変換("lower" / "upper" / "swap" か None)、stamp_fields は
    (年, 月, 日, 時, 分, 秒) か None(変えない)、attr_changes は変えるものだけの dict。
    年月日が 0/0/0 なら、それぞれのファイルの年月日のまま時分秒だけ変える(あふｗと同じ)。"""
    plans, problems = [], []
    for entry in entries:
        if entry.is_parent:
            continue
        name = entry.name
        if case_all:
            name = convert_case(name, "all", case_all, entry.is_dir)
        if case_ext:
            name = convert_case(name, "ext", case_ext, entry.is_dir)
        mtime = None
        if stamp_fields is not None:
            mtime, problem = make_timestamp(*stamp_fields, base=entry.mtime)
            if problem:
                problems.append((entry.name, problem))
                continue
        plan = plan_file_info(entry, name, mtime, attr_changes)
        if plan:
            plans.append(plan)
    return plans, problems


def rename_collision(existing, renames: dict):
    """改名したあとの名前が、フォルダの中で(大文字小文字を無視して)重なるか。
    重なる名前を返す。無ければ None。

    existing はフォルダの中の名前、renames は {元の名前: 新しい名前}。自分自身の
    大文字小文字だけの改名(a.txt → A.TXT)は重なりにしない。一括で小文字にしたら
    2つが同じ名前になった、も見つける。"""
    final = {name: renames.get(name, name) for name in existing}
    for old, new in renames.items():
        final.setdefault(old, new)
    seen = {}
    for old, name in final.items():
        key = name.casefold()
        if key in seen and seen[key] != old:
            # 知らせるのは「付けようとした名前」の側(どれと重なったかが分かるように)。
            other = seen[key]
            return name if old in renames else final[other]
        seen[key] = old
    return None


def describe_file_info(plan: dict) -> str:
    """メッセージ窓に出す1件の変更点の要約。"""
    parts = []
    if "new" in plan:
        parts.append(f"{plan['old']} → {plan['new']}")
    else:
        parts.append(plan["old"])
    if "mtime" in plan:
        parts.append("日時 " + time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(plan["mtime"])))
    if "attrs" in plan:
        flags = attrs_to_flags(plan["attrs"])
        letters = "".join(letter for key, letter in (("readonly", "R"), ("hidden", "H"),
                                                     ("system", "S"), ("archive", "A"))
                          if flags[key])
        parts.append(f"属性 {letters or '-'}")
    return "  ".join(parts)


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


def sha256_of_file(path) -> str:
    """ファイルの SHA-256 ハッシュ(16進小文字)。大きいファイルでも少しずつ読む。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def log_selection_text(selected: str) -> str:
    """ログの選択範囲(QTextCursor.selectedText)をクリップボード用に直す。

    Qt は段落の区切りを U+2029 で返すので、通常の改行(CRLF)へ置き換える。"""
    return selected.replace("\u2029", "\r\n")


def hash_clip_text(results) -> str:
    """(名前, ハッシュ) の並び → クリップボード用テキスト。

    1 件ならハッシュだけ(照合先の入力欄にそのまま貼れる)。
    複数なら sha256sum と同じ「ハッシュ␣␣名前」の行を改行(CRLF)区切りにする。"""
    if len(results) == 1:
        return results[0][1]
    return "\r\n".join(f"{digest}  {name}" for name, digest in results)


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


def sync_sources(mine, theirs, tolerance=DEFAULT_TIME_TOLERANCE) -> list:
    """フォルダ同期コピーで、送る側(mine)から反対窓(theirs)へ送るべきエントリ。

    - 相手に同名が無いもの(ファイルもフォルダも。フォルダは中身ごと
      シェルのコピーに任せる)
    - 同名のファイルがあり、サイズが違うか、こちらが新しいもの
      (許容誤差を超えたときだけ。こちらが古いものでは上書きしない)
    両方にあるフォルダの中身は対象にしない(再帰しない。今いるフォルダ
    同士だけを見る W の比較と同じ範囲)。名前の衝突するファイル⇔フォルダ
    の組も送らない。削除は一切しない。"""
    others = {}
    for o in theirs:
        if o.is_parent:
            continue
        others.setdefault(o.name.casefold(), o)
    result = []
    for e in mine:
        if e.is_parent:
            continue
        other = others.get(e.name.casefold())
        if other is None:
            result.append(e)
        elif not e.is_dir and not other.is_dir:
            tol = abs(tolerance or 0.0)
            newer = (e.mtime - other.mtime) > tol      # こちらがはっきり新しい
            older = (other.mtime - e.mtime) > tol      # 相手がはっきり新しい
            # サイズが違うだけでは送らない。相手のほうが新しいものを
            # 古いこちらで上書きしてしまうため(以前はサイズ違いなら
            # 無条件で送っていた)。時刻が誤差の範囲内なら、サイズ違いを
            # 「中身が変わった」とみなして送る。
            if newer or (e.size != other.size and not older):
                result.append(e)
    return result


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
# プレビュー(F3)
# ---------------------------------------------------------------
PREVIEW_IMAGE_EXTS = frozenset("png jpg jpeg gif bmp webp ico".split())
PREVIEW_TEXT_LINES = 200
PREVIEW_TEXT_BYTES = 256 * 1024
PREVIEW_ARCHIVE_MAX = 4 * 1024 * 1024     # 書庫の中はこれ以下だけ取り出して見せる
PREVIEW_FOLDER_NAMES = 40


def preview_kind(entry, text_extensions) -> str:
    """プレビューで何を出すか。"none"(何も無い・"..")/ "folder" / "text" / "image" / "info"。
    テキストかどうかはビュアと同じ拡張子の表(filer.viewer_extensions)で決める。"""
    if entry is None or entry.is_parent:
        return "none"
    if entry.is_dir:
        return "folder"
    ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
    if ext in PREVIEW_IMAGE_EXTS:
        return "image"
    if ext and ext in text_extensions:
        return "text"
    return "info"


def fit_size(width: int, height: int, box_w: int, box_h: int):
    """(width, height) を縦横比を保って box に収めた大きさ。拡大はしない。"""
    if width <= 0 or height <= 0 or box_w <= 0 or box_h <= 0:
        return max(width, 0), max(height, 0)
    scale = min(box_w / width, box_h / height, 1.0)
    return max(1, int(width * scale)), max(1, int(height * scale))


def info_lines(name: str, entry) -> list:
    """「その他」のプレビュー(名前・サイズ・日時・属性)。"""
    return [name, "",
            f"サイズ  {format_size(entry)}" + ("" if entry.is_dir else " バイト"),
            f"日時    {format_mtime(entry.mtime)}",
            f"属性    {attr_letters(entry) or '-'}"]


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
            # フラット表示では名前が "sub\file.txt" のような相対パスなので、書庫の中の
            # 区切りは "/" に揃える(フォルダ構成を保ったまま入る)。
            add(path, name.replace("\\", "/"), False)
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


# ---------------------------------------------------------------
# 拡張子別表示色(あふｗの「各種登録 > 拡張子別表示色」)
# ---------------------------------------------------------------
# 色はユーザーのあふｗの設定画面の色の帯から拾った値。拡張子の並びは本人と決めたもの。
# 先に一致した行が勝つ(あふｗと同じ)。settings の filer.ext_colors で丸ごと差し替わる。
DEFAULT_EXT_COLORS = [
    {"color": "#0000ff", "exts": "zip 7z rar lzh cab tar gz tgz bz2 xz zst iso".split()},
    {"color": "#ff0000", "exts": "exe msi msix bat cmd ps1 vbs".split()},
    {"color": "#804000",
     "exts": "jpg jpeg png gif bmp webp heic svg tif tiff ico psd ai eps".split()},
    {"color": "#00ff00", "exts": "mp3 m4a aac flac wav ogg opus wma mid".split()},
    {"color": "#0080ff", "exts": "html htm css js ts json xml yaml yml py sh ipynb".split()},
    {"color": "#800040", "exts": "txt md log csv tsv ini inf cfg conf toml".split()},
    {"color": "#008000", "exts": "reg db sqlite accdb".split()},
    {"color": "#800080", "exts": "docx xlsx xlsm pptx doc xls ppt pdf vsdx msg".split()},
    {"color": "#b5b500", "exts": "mp4 mov mkv webm avi wmv m4v mpg mpeg".split()},
    {"color": "#808080", "exts": "dll sys ocx drv cpl".split()},
    {"color": "#808080", "exts": "lnk url".split()},
]

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_exts(value) -> list:
    """拡張子の並びを、点や * を除いた小文字に揃える(重複は最初の1つだけ)。
    文字列なら空白・カンマ・セミコロンで区切る("*.Zip .7z rar" → ["zip", "7z", "rar"])。"""
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    found = []
    for item in value:
        ext = str(item).strip().lstrip("*").lstrip(".").lower()
        if ext and ext not in found:
            found.append(ext)
    return found


def normalize_ext_colors(value) -> list:
    """settings.json の filer.ext_colors を [{"color", "exts"}] に揃える。

    手で書かれた設定なので、色の書式が違う行・拡張子の無い行は黙って捨てる。値が
    配列でなければ(書かれていなければ)既定。空の配列は「色を付けない」として尊重する。"""
    if not isinstance(value, list):
        value = DEFAULT_EXT_COLORS
    rules = []
    for row in value:
        if not isinstance(row, dict):
            continue
        color = str(row.get("color", "")).strip()
        exts = normalize_exts(row.get("exts"))
        if _COLOR_RE.match(color) and exts:
            rules.append({"color": color.lower(), "exts": exts})
    return rules


def ext_color_map(rules) -> dict:
    """{拡張子: 色}。同じ拡張子が複数の行にあれば、上の行(先に一致したもの)が勝つ。"""
    found = {}
    for row in rules:
        for ext in row["exts"]:
            found.setdefault(ext, row["color"])
    return found


def ext_color_for(entry, color_map: dict):
    """一覧の拡張子の列に付ける色。付けないなら None。

    フォルダには付けない。隠し・システム・読み込み専用の属性色の行も付けない(行全体の
    属性色を優先する。あふｗで desktop.ini が行全体灰色だったのと同じ)。名前の列には
    付けない(呼ぶ側が拡張子の列だけに使う)。"""
    if entry is None or entry.is_dir or entry.is_parent or entry.color_kind != "text":
        return None
    ext = os.path.splitext(entry.name)[1].lstrip(".").lower()
    return color_map.get(ext) if ext else None


def ext_duplicates(rules, row: int) -> dict:
    """row 行目の拡張子のうち、ほかの行にもあるもの。{拡張子: [その行の番号, ...]}。"""
    if not (0 <= row < len(rules)):
        return {}
    found = {}
    for ext in rules[row]["exts"]:
        others = [i for i, r in enumerate(rules) if i != row and ext in r["exts"]]
        if others:
            found[ext] = others
    return found


def remove_exts_from_others(rules, row: int, exts) -> list:
    """row 以外の行から exts を外した新しい並び(空になった行は消す)。"""
    exts = set(exts)
    result = []
    for i, r in enumerate(rules):
        if i == row:
            result.append(dict(r, exts=list(r["exts"])))
            continue
        kept = [e for e in r["exts"] if e not in exts]
        if kept:
            result.append(dict(r, exts=kept))
    return result


def ext_colors_to_settings(rules) -> list:
    """settings.json に書く形(拡張子は配列のまま)。"""
    return [{"color": r["color"], "exts": list(r["exts"])} for r in rules]


# ---------------------------------------------------------------
# 使った記録の置き場所(よく行くフォルダ・一括改名の履歴)
# ---------------------------------------------------------------
# リポジトリの外(%LOCALAPPDATA%\traytools)に置く。このリポジトリは公開なので、
# 訪れたフォルダの一覧や改名の記録をうっかりコミットしないように、そもそも
# 作業ツリーの中に作らない(.gitignore に頼らない)。テストでは環境変数で差し替える。
DATA_DIR_ENV = "TRAYTOOLS_DATA_DIR"


def data_dir() -> str:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "traytools")


def load_store(name: str, default):
    """記録を読む。無い・壊れている・形が違うなら default。"""
    import json

    try:
        with open(os.path.join(data_dir(), name), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def save_store(name: str, data) -> None:
    """記録を書く。一時ファイルに書いてから置き換える(途中で落ちても壊れた記録を残さない)。"""
    import json

    folder = data_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(temp, path)


# ---------------------------------------------------------------
# よく行くフォルダ(Shift+H。zoxide の frecency)
# ---------------------------------------------------------------
FRECENCY_STORE = "filer_frecency.json"
FRECENCY_LIMIT = 500


def frecency_weight(age_seconds: float) -> float:
    """最後に訪れてからの時間による重み(zoxide と同じ段の付け方)。"""
    if age_seconds < 3600:
        return 4.0
    if age_seconds < 86400:
        return 2.0
    if age_seconds < 7 * 86400:
        return 0.5
    return 0.25


def frecency_score(count: int, last: float, now: float) -> float:
    return count * frecency_weight(now - last)


def _frecency_key(data: dict, path: str):
    folded = os.path.normcase(path)
    return next((k for k in data if os.path.normcase(k) == folded), None)


def frecency_add(data: dict, path: str, now: float, limit: int = FRECENCY_LIMIT) -> dict:
    """path を訪れたことを記録する。data は {パス: [回数, 最後に訪れた時刻]}。

    大文字小文字だけ違うパスは同じものとして数える。件数が limit を超えたら、
    スコアの低いものから間引く(訪れたばかりのものは残る)。"""
    path = normalize_dir(path)
    key = _frecency_key(data, path)
    count = data.pop(key)[0] if key is not None else 0
    data[path] = [count + 1, now]
    if len(data) > limit:
        # 同じスコアなら最近訪れた方を残す。
        ranked = sorted(data, key=lambda p: (frecency_score(data[p][0], data[p][1], now),
                                             data[p][1]), reverse=True)
        for stale in ranked[limit:]:
            del data[stale]
    return data


def frecency_remove(data: dict, path: str) -> dict:
    key = _frecency_key(data, normalize_dir(path))
    if key is not None:
        del data[key]
    return data


def frecency_query(data: dict, text: str, now: float) -> list:
    """入力した語(空白区切り)をすべて含むパスを、スコアの高い順に。

    最後の語がフォルダ名(パスの末尾の部分)に含まれるものを先に出す(zoxide と同じく、
    最後に打つのは行き先のフォルダ名であることが多いため)。"""
    words = [w.casefold() for w in (text or "").split()]
    found = []
    for path, (count, last) in data.items():
        folded = path.casefold()
        if not all(w in folded for w in words):
            continue
        tail = os.path.basename(path.rstrip("\\/")).casefold()
        in_tail = (not words) or words[-1] in tail
        found.append((not in_tail, -frecency_score(count, last, now), path))
    found.sort()
    return [path for _a, _b, path in found]
