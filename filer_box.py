"""あっふ(filer.py)の C→P「Box の共有用テキスト」の、Qt に依存しない部分。

Box Drive が手元に持つ同期の記録(sync.db)から、ファイル・フォルダの Box 上の ID を引き、
「/Box/案件/資料/」「フォルダの URL(https://app.box.com/folder/<ID>)」「ファイル名」の
3行をまとめた文字列を作る。

- Box Drive は sync.db を掴んで離さない(mode=ro でも "database is locked")。
  そのため **本体ではなくコピーを開く**。コピーは元の更新日時・大きさが変わらない間は使い回す。
- 共有リンク(/s/<鍵>)は作らないし使わない。フォルダのメンバーとして招待された人は
  /folder/<ID> の URL で開ける(共有リンクを新しく作ると、Box 上の公開範囲を変えてしまう)。
- 画面は filer.py の側。ここは純関数と DB の読み取りだけ。
"""
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass

DEFAULT_HOST = "app.box.com"
ROOT_NAME = "Box"          # 1行目の先頭に付ける固定の見出し(実フォルダ名ではない)
INDENT = "　"          # 全角スペース。習慣の字下げ
NEWLINE = "\r\n"           # core.clip_text と同じ(クリップボードの複数行は CRLF)

FILE = "file"
FOLDER = "folder"

# Box Drive がエクスプローラの右クリックメニューに足す「Box.com で表示」の表示名。
# 表示言語で綴りが変わるので候補を並べる(filer_contextmenu.py --item に渡す)。
# Box 自身の項目なので、こちらで組み立てた URL とは別の、確かな開き方になる。
VIEW_ON_BOX_ITEMS = ("Box.comで表示", "View on Box.com")

# テストから差し替える口(None なら本物の場所)
ROOT_OVERRIDE = None
DB_OVERRIDE = None


class BoxDbError(Exception):
    """Box Drive の記録を読めない(コピー失敗・壊れている・表が無い)。"""


class NotSynced(Exception):
    """同期の記録に見つからない(アップロード前のことがある)。"""


class Ambiguous(NotSynced):
    """同じ名前の記録が複数あり、どれか1つに決められない(古い行・改名の残り・
    大文字小文字だけ違う行・競合コピーなど)。間違ったフォルダのリンクを作るくらいなら作らない。
    NotSynced の子にしてあるので、区別しない呼び出し側では「見つからない」と同じに扱われる。"""


class Mismatch(NotSynced):
    """手元の記録(local_item と対応表)と、Box 側の木(box_item)で、同じパスが同じ ID に
    ならない、または Box 側の木に無い。どちらが正しいか分からないのでリンクを作らない。
    part は食い違った階層の名前。"""

    def __init__(self, part, detail=""):
        super().__init__(f"{part}: {detail}" if detail else part)
        self.part = part


def box_root() -> str:
    """Box Drive の同期フォルダ。既定は %USERPROFILE%\\Box。"""
    if ROOT_OVERRIDE:
        return os.path.normpath(ROOT_OVERRIDE)
    return os.path.join(os.path.expanduser("~"), "Box")


def db_path() -> str:
    if DB_OVERRIDE:
        return DB_OVERRIDE
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "Box", "Box", "data", "sync.db")


def available() -> bool:
    """メニューに出してよいか。同期フォルダと記録のファイルがあるかだけを安く見る。"""
    try:
        return os.path.isdir(box_root()) and os.path.isfile(db_path())
    except OSError:
        return False


def relative_parts(path: str):
    """Box の同期フォルダより下の部品(タプル)。外、または同期フォルダ自身なら None。"""
    root = os.path.normcase(os.path.normpath(box_root()))
    full = os.path.normpath(path)
    low = os.path.normcase(full)
    if not low.startswith(root + os.sep):
        return None
    parts = tuple(p for p in full[len(root):].split(os.sep) if p)
    return parts or None


def all_inside(paths) -> bool:
    paths = list(paths)
    return bool(paths) and all(relative_parts(p) is not None for p in paths)


# ---------------------------------------------------------------
# sync.db のコピー(使い回し)
# ---------------------------------------------------------------
_cache = {"key": None, "dir": None}


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def copy_db(cache_dir: str = None) -> str:
    """sync.db(と sync.db-wal)を一時フォルダへコピーして、コピーの sync.db のパスを返す。

    元の更新日時・大きさが前回と同じならコピーを使い回す。WAL に未反映の更新があっても
    -wal を一緒に写せば読み取り側が重ねて読むので、最新の状態が見える。"""
    src = db_path()
    wal = src + "-wal"
    key = (src, _stamp(src), _stamp(wal))
    if key[1] is None:
        raise BoxDbError("sync.db がありません")
    current = _cache["dir"]
    if (_cache["key"] == key and current
            and os.path.isfile(os.path.join(current, "sync.db"))):
        return os.path.join(current, "sync.db")
    discard_cache()
    try:
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="boxdb_", dir=cache_dir or None)
    except OSError as e:
        raise BoxDbError(str(e)) from e
    try:
        shutil.copyfile(src, os.path.join(folder, "sync.db"))
        if key[2] is not None:
            shutil.copyfile(wal, os.path.join(folder, "sync.db-wal"))
    except OSError as e:
        shutil.rmtree(folder, ignore_errors=True)
        raise BoxDbError(str(e)) from e
    _cache["key"], _cache["dir"] = key, folder
    return os.path.join(folder, "sync.db")


def discard_cache() -> None:
    """コピーを消す(窓を閉じるとき・作り直すとき)。"""
    folder = _cache["dir"]
    _cache["key"] = _cache["dir"] = None
    if folder:
        shutil.rmtree(folder, ignore_errors=True)


def open_db(cache_dir: str = None) -> sqlite3.Connection:
    path = copy_db(cache_dir)
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.execute("SELECT 1 FROM local_item LIMIT 1")
        conn.execute("SELECT 1 FROM item_mapping_between_fs LIMIT 1")
        conn.execute("SELECT 1 FROM box_item LIMIT 1")
        return conn
    except sqlite3.Error as e:
        if conn is not None:
            conn.close()        # 開いたままだと Windows ではコピーを消せなくなる
        raise BoxDbError(str(e)) from e


# ---------------------------------------------------------------
# パス → Box の ID
# ---------------------------------------------------------------
@dataclass(frozen=True)
class BoxItem:
    kind: str            # FILE か FOLDER
    rel_parts: tuple     # Box の同期フォルダより下の名前(DB 上の綴り)
    ids: tuple           # rel_parts の各階層の Box ID(同じ長さ)

    @property
    def box_id(self) -> str:
        return self.ids[-1]

    @property
    def folder_ids(self) -> tuple:
        """このものを含むフォルダ(フォルダなら自分も)の ID を浅い方から。"""
        return self.ids if self.kind == FOLDER else self.ids[:-1]

    @property
    def folder_parts(self) -> tuple:
        return self.rel_parts if self.kind == FOLDER else self.rel_parts[:-1]

    @property
    def name(self) -> str:
        return self.rel_parts[-1]


def _box_id_of(conn, local_id):
    row = conn.execute(
        "SELECT box_id, box_item_type FROM item_mapping_between_fs WHERE local_id=?",
        (local_id,)).fetchone()
    return row


def _box_side_ids(conn, parts) -> list:
    """Box 側の木(box_item。根の box_id は "0"、parent_item_id は親の box_id)を、
    手元の記録とは無関係に名前でたどって、各階層の box_id を返す。見つからなければ Mismatch、
    同じ名前が複数あって決められなければ Ambiguous(同じ規則: 綴りが完全一致する1つがあればそれ)。"""
    parent = "0"
    ids = []
    for part in parts:
        rows = conn.execute("SELECT box_id, name FROM box_item WHERE parent_item_id=? AND name=?",
                            (parent, part)).fetchall()
        if not rows:
            raise Mismatch(part, "Box 側の記録に見つかりません")
        if len(rows) > 1:
            exact = [r for r in rows if r[1] == part]
            if len(exact) != 1:
                raise Ambiguous(part)
            rows = exact
        parent = str(rows[0][0])
        ids.append(parent)
    return ids


def resolve(path: str, conn: sqlite3.Connection = None, cache_dir: str = None):
    """(box_id, kind, rel_parts)。見つからなければ NotSynced、読めなければ BoxDbError。"""
    item = resolve_item(path, conn, cache_dir)
    return item.box_id, item.kind, item.rel_parts


def resolve_item(path: str, conn: sqlite3.Connection = None, cache_dir: str = None) -> BoxItem:
    parts = relative_parts(path)
    if parts is None:
        raise NotSynced(os.path.basename(path.rstrip("\\/")) or path)
    own = conn is None
    if own:
        conn = open_db(cache_dir)
    try:
        roots = conn.execute("SELECT local_id FROM local_item WHERE parent_item_id IS NULL"
                             ).fetchall()
        if not roots:
            raise BoxDbError("根の行がありません")
        if len(roots) > 1:
            raise Ambiguous("Box の根")
        local_id = roots[0][0]
        names, ids, kind = [], [], FOLDER
        for part in parts:
            # name は大文字小文字を区別しない照合なので、綴りの違う行も一緒に拾う
            rows = conn.execute(
                "SELECT local_id, name FROM local_item WHERE parent_item_id=? AND name=?",
                (local_id, part)).fetchall()
            found = []          # (行, 対応表の行)。Box 側との対応が付いているものだけ
            for row in rows:
                mapped = _box_id_of(conn, row[0])
                if mapped is not None:
                    found.append((row, mapped))
            if not found:
                raise NotSynced(part)
            if len(found) > 1:
                exact = [f for f in found if f[0][1] == part]
                if len(exact) != 1:
                    raise Ambiguous(part)
                found = exact
            row, mapped = found[0]
            local_id = row[0]
            names.append(row[1] or part)
            ids.append(str(mapped[0]))
            kind = FOLDER if mapped[1] == 1 else FILE
        # 手元の記録とは別に、Box 側の木を同じパスでたどり、すべての階層で同じ ID になることを
        # 確かめる(古い対応が残っていて別のフォルダを指している、を防ぐ)
        box_ids = _box_side_ids(conn, parts)
        for part, mine, theirs in zip(parts, ids, box_ids):
            if mine != theirs:
                raise Mismatch(part, f"手元の記録の ID({mine})と Box 側の ID({theirs})が違います")
        return BoxItem(kind, tuple(names), tuple(ids))
    except sqlite3.Error as e:
        raise BoxDbError(str(e)) from e
    finally:
        if own:
            conn.close()


def resolve_many(paths, cache_dir: str = None, ambiguous: list = None, mismatched: list = None):
    """(items, 見つからなかった名前の一覧)。記録が読めなければ BoxDbError。

    ambiguous にリストを渡すと、同じ名前の記録が複数あって決められなかったもの(Ambiguous)の
    名前はそちらへ入れる(渡さなければ「見つからなかった名前」の一覧に混ぜる)。mismatched も同じ
    作法で、手元の記録と Box 側の記録が食い違った(Mismatch)ものの名前(対象の名前)を受ける。"""
    conn = open_db(cache_dir)
    items, missing = [], []
    try:
        for path in paths:
            try:
                items.append(resolve_item(path, conn))
            except Mismatch:
                name = os.path.basename(path.rstrip("\\/")) or path
                (mismatched if mismatched is not None else missing).append(name)
            except Ambiguous as e:
                (ambiguous if ambiguous is not None else missing).append(str(e))
            except NotSynced as e:
                missing.append(str(e))
    finally:
        conn.close()
    return items, missing


# ---------------------------------------------------------------
# 共有用テキスト
# ---------------------------------------------------------------
def folder_url(folder_id: str) -> str:
    return f"https://{DEFAULT_HOST}/folder/{folder_id}"


def _header(parts, folder_id) -> list:
    shown = "/" + "/".join((ROOT_NAME,) + tuple(parts)) + "/"
    return [shown, INDENT + folder_url(folder_id)]


def _blocks(items) -> list:
    """塊の並び。[(行の並び, 見出しのパス "/Box/案件/", URL)]。build_share_text と
    share_blocks が同じ並び・同じ束ね方を使うための元。"""
    blocks = []          # [キー, 行の並び, 見出し, URL]
    index = {}
    for item in items:
        if item.kind == FOLDER:
            # フォルダはそれぞれ別の塊にする(見出しが空行なしで続くと、どの URL が
            # どのフォルダのものか読みにくいため)
            key = ("folder", item.box_id)
            lines = _header(item.rel_parts, item.box_id)
        else:
            key = ("files", item.folder_ids[-1] if item.folder_ids else "0")
            lines = None
        if key not in index:
            index[key] = len(blocks)
            blocks.append([key, [], "", ""])
        entry = blocks[index[key]]
        block = entry[1]
        if item.kind == FOLDER:
            block.extend(lines)
        else:
            if not block:
                parent_id = item.folder_ids[-1] if item.folder_ids else "0"
                # 根直下のファイルは根の ID "0" を使う
                block.extend(_header(item.folder_parts, parent_id))
            block.append(INDENT * 2 + item.name)
        if not entry[2] and len(block) >= 2:
            entry[2], entry[3] = block[0], block[1][len(INDENT):]
    return [(lines, shown, url) for _key, lines, shown, url in blocks if lines]


def build_share_text(items) -> str:
    """BoxItem の並び → クリップボード用テキスト(CRLF)。

    ファイルは入っているフォルダごとに束ね(初めて出た順)、フォルダの見出しの下に
    名前を並べる。フォルダを対象にしたものは、1つずつそれ自身の見出しだけの塊にする。
    塊と塊の間は空行。URL は /folder/<ID> の形(招待されたメンバー用)。"""
    return (NEWLINE * 2).join(NEWLINE.join(lines) for lines, _shown, _url in _blocks(items))


def share_blocks(items) -> list:
    """build_share_text の塊ごとの [(見出しのパス "/Box/ProjA/資料/", URL)]。同じ並び・同じ束ね方。
    画面が「この URL をブラウザで開いて確かめる」ボタンを塊ごとに出すのに使う。"""
    return [(shown, url) for _lines, shown, url in _blocks(items)]
