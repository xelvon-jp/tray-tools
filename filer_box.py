"""あっふ(filer.py)の C→L「Box の共有用テキスト」の、Qt に依存しない部分。

Box Drive が手元に持つ同期の記録(sync.db)から、ファイル・フォルダの Box 上の ID を引き、
「/Box/案件/資料/」「共有リンク付きの URL」「ファイル名」の3行をまとめた文字列を作る。

- Box Drive は sync.db を掴んで離さない(mode=ro でも "database is locked")。
  そのため **本体ではなくコピーを開く**。コピーは元の更新日時・大きさが変わらない間は使い回す。
- 共有リンクの鍵(トークン)は、祖先フォルダのものが子孫すべてに効く。案件フォルダに
  1回だけ登録しておけば、その下のファイルは同じ鍵で開ける(ログイン不要)。
- 設定(box_links)の読み書きや画面は filer.py の側。ここは純関数と DB の読み取りだけ。
"""
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_HOST = "app.box.com"
ROOT_NAME = "Box"          # 1行目の先頭に付ける固定の見出し(実フォルダ名ではない)
INDENT = "　"          # 全角スペース。習慣の字下げ
NEWLINE = "\r\n"           # core.clip_text と同じ(クリップボードの複数行は CRLF)

FILE = "file"
FOLDER = "folder"

# テストから差し替える口(None なら本物の場所)
ROOT_OVERRIDE = None
DB_OVERRIDE = None


class BoxDbError(Exception):
    """Box Drive の記録を読めない(コピー失敗・壊れている・表が無い)。"""


class NotSynced(Exception):
    """同期の記録に見つからない(アップロード前のことがある)。"""


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
        row = conn.execute("SELECT local_id FROM local_item WHERE parent_item_id IS NULL"
                           ).fetchone()
        if row is None:
            raise BoxDbError("根の行がありません")
        local_id = row[0]
        names, ids, kind = [], [], FOLDER
        for part in parts:
            row = conn.execute(
                "SELECT local_id, name FROM local_item WHERE parent_item_id=? AND name=?",
                (local_id, part)).fetchone()
            if row is None:
                raise NotSynced(part)
            local_id = row[0]
            mapped = _box_id_of(conn, local_id)
            if mapped is None:
                raise NotSynced(part)
            names.append(row[1] or part)
            ids.append(str(mapped[0]))
            kind = FOLDER if mapped[1] == 1 else FILE
        return BoxItem(kind, tuple(names), tuple(ids))
    except sqlite3.Error as e:
        raise BoxDbError(str(e)) from e
    finally:
        if own:
            conn.close()


def resolve_many(paths, cache_dir: str = None):
    """(items, 見つからなかった名前の一覧)。記録が読めなければ BoxDbError。"""
    conn = open_db(cache_dir)
    items, missing = [], []
    try:
        for path in paths:
            try:
                items.append(resolve_item(path, conn))
            except NotSynced as e:
                missing.append(str(e))
    finally:
        conn.close()
    return items, missing


# ---------------------------------------------------------------
# 共有リンクの登録
# ---------------------------------------------------------------
_HOST_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.box\.com$", re.I)
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")


def parse_shared_link(url: str):
    """共有リンク https://<host>/s/<鍵>(末尾の / ・クエリ・/folder/.. が付いても可)
    → (host, 鍵)。それ以外は None。host は *.box.com だけ受ける(会社の Box は
    <company>.app.box.com)。"""
    if not isinstance(url, str):
        return None
    try:
        split = urlsplit(url.strip())
    except ValueError:
        return None
    if split.scheme.lower() != "https" or not split.hostname:
        return None
    host = split.hostname.lower()
    if not _HOST_RE.match(host):
        return None
    segments = [s for s in split.path.split("/") if s]
    if len(segments) < 2 or segments[0] != "s":
        return None
    token = segments[1]
    if not _TOKEN_RE.match(token):
        return None
    return host, token


def make_link(folder_id: str, path: str, host: str, token: str) -> dict:
    return {"folder_id": str(folder_id), "path": path, "host": host, "token": token}


def valid_links(links) -> list:
    """設定から読んだ box_links のうち、形の合うものだけ。"""
    out = []
    for link in links if isinstance(links, list) else []:
        if (isinstance(link, dict)
                and all(isinstance(link.get(k), str) and link.get(k)
                        for k in ("folder_id", "host", "token"))):
            out.append(link)
    return out


def with_link(links, new_link: dict) -> list:
    """同じフォルダの登録があれば置き換え、無ければ足す(新しい一覧を返す)。"""
    kept = [l for l in valid_links(links) if l["folder_id"] != new_link["folder_id"]]
    kept.append(new_link)
    return kept


def find_link(item: BoxItem, links):
    """item を含むフォルダ(フォルダ自身も)のうち、登録のある一番近いものの登録。
    パスの文字列ではなく Box の ID で照合するので、改名・移動をしても外れない。"""
    by_id = {l["folder_id"]: l for l in valid_links(links)}
    for folder_id in reversed(item.folder_ids):
        if folder_id in by_id:
            return by_id[folder_id]
    return None


def candidate_folders(item: BoxItem):
    """登録先の候補。[(Box/案件 のような表示, folder_id, 保存用の path)]、浅い方(案件の根)から。
    Box の根自身は含めない。"""
    out = []
    for depth in range(1, len(item.folder_parts) + 1):
        shown = "/".join((ROOT_NAME,) + item.folder_parts[:depth])
        out.append((shown, item.folder_ids[depth - 1], shown))
    return out


# ---------------------------------------------------------------
# 共有用テキスト
# ---------------------------------------------------------------
def folder_url(folder_id: str, link=None) -> str:
    if link:
        return f"https://{link['host']}/s/{link['token']}/folder/{folder_id}"
    return f"https://{DEFAULT_HOST}/folder/{folder_id}"


def _header(parts, folder_id, link) -> list:
    shown = "/" + "/".join((ROOT_NAME,) + tuple(parts)) + "/"
    return [shown, INDENT + folder_url(folder_id, link)]


def build_share_text(items, links) -> str:
    """BoxItem の並び → クリップボード用テキスト(CRLF)。

    ファイルは入っているフォルダごとに束ね(初めて出た順)、フォルダの見出しの下に
    名前を並べる。フォルダを対象にしたものは、1つずつそれ自身の見出しだけの塊にする。
    塊と塊の間は空行。登録が無ければ /folder/<ID> の形(招待メンバー用)。"""
    blocks = []          # [(キー, 行の並び)]
    index = {}
    for item in items:
        if item.kind == FOLDER:
            # フォルダはそれぞれ別の塊にする(見出しが空行なしで続くと、どの URL が
            # どのフォルダのものか読みにくいため)
            key = ("folder", item.box_id)
            lines = _header(item.rel_parts, item.box_id, find_link(item, links))
        else:
            key = ("files", item.folder_ids[-1] if item.folder_ids else "0")
            lines = None
        if key not in index:
            index[key] = len(blocks)
            blocks.append([key, []])
        block = blocks[index[key]][1]
        if item.kind == FOLDER:
            block.extend(lines)
        else:
            if not block:
                parent_id = item.folder_ids[-1] if item.folder_ids else "0"
                # 根直下のファイルは根の ID "0" を使う(鍵は付けられない)
                block.extend(_header(item.folder_parts, parent_id, find_link(item, links)))
            block.append(INDENT * 2 + item.name)
    return (NEWLINE * 2).join(NEWLINE.join(lines) for _key, lines in blocks if lines)
