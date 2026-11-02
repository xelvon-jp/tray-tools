# photometa/metacache.py
# 写真の★（xmp:Rating）とキーワードの読み取り結果を、ディスクに覚えておく永続キャッシュ。
#
# 写真ドライブ（USB の SSD）では1枚読むのに約17.5ms かかり、12,069枚で約3.5分になる。
# 一覧を開くたびに全部読み直していたのを、SQLite に覚えて2回目以降は stat だけで済ませる。
#
# - 置き場所は %LOCALAPPDATA%\traytools\photometa_cache.sqlite（config.data_dir() の下）。
#   テストでは MetaCache(db_path) で差し替えられる（環境変数は差し替えない）。
# - キーは正規化したフルパス。値はファイルのサイズ・更新日時（ns）と rating・keywords。
#   読むときに stat のサイズと更新日時が一致すれば、ファイルを開かずにキャッシュを返す。
#   違えば読み直して書き換える。
# - RAW は★がサイドカー（名前.xmp）に書かれるので、サイドカーのサイズ・更新日時も見る。
#   サイドカーが無いときは -1 を覚え、後から作られたら不一致で読み直す。
# - 読めなかったもの（None＝不明）は覚えない。一時的な読み取り失敗（ドライブの
#   抜き差しなど）を固定してしまわないため。
# - ★を書く処理（writer.ExifToolSession）は、書いたあと更新日時を元に戻すので、stat では
#   変化が分からない。書き込みの成功時に update() でキャッシュを新しい値に直す
#   （open_session で作ったセッションが自動でやる）。変更記録（journal）を読んで
#   反映する側も、同じ update() で直せる。
# - あっふと PhotoCuller が同時に使っても壊れないように、SQLite の WAL と
#   busy_timeout を使い、トランザクションは1文ごとに短く済ませる。
# - Qt には依存しない（PhotoCuller からもそのまま import できる）。
from __future__ import annotations

import json
import os
import sqlite3
import threading
import weakref
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import config as _config
from .xmp import Edits, PhotoMeta, is_raw, read_meta, sidecar_path

CACHE_PATH = os.path.join(_config.data_dir(), "photometa_cache.sqlite")
READ_WORKERS = 8                 # キャッシュに無い写真を並行して読む本数
_BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    side_size INTEGER NOT NULL,
    side_mtime_ns INTEGER NOT NULL,
    rating INTEGER NOT NULL,
    keywords TEXT NOT NULL
)
"""


def normalize_key(path) -> str:
    """キャッシュのキー（大文字小文字・区切りをそろえたフルパス。filer.photo_key と同じ）。"""
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _edits_to_text(edits: Edits | None) -> str | None:
    """補正を覚える形（JSON）。無ければ None。"""
    if edits is None:
        return None
    return json.dumps({
        "crop": list(edits.crop) if edits.crop is not None else None,
        "angle": edits.angle, "exposure": edits.exposure,
        "contrast": edits.contrast, "saturation": edits.saturation,
        "temperature": edits.temperature, "tint": edits.tint,
    }, ensure_ascii=False)


def _text_to_edits(text) -> Edits | None:
    if not text:
        return None
    try:
        d = json.loads(text)
        crop = d.get("crop")
        edits = Edits(
            crop=tuple(float(v) for v in crop) if crop else None,
            angle=float(d.get("angle") or 0.0),
            exposure=float(d.get("exposure") or 0.0),
            contrast=float(d.get("contrast") or 0.0),
            saturation=float(d.get("saturation") or 0.0),
            temperature=float(d.get("temperature") or 0.0),
            tint=float(d.get("tint") or 0.0))
    except (ValueError, TypeError, AttributeError):
        return None
    return edits.sanitized()


def _stat_key(path: str):
    """照合用の (サイズ, 更新日時ns, サイドカーのサイズ, サイドカーの更新日時ns)。

    本体が無ければ None。サイドカーが無いときは (-1, -1)。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    side_size, side_mtime_ns = -1, -1
    if is_raw(path):
        try:
            ss = os.stat(sidecar_path(path))
            side_size, side_mtime_ns = ss.st_size, ss.st_mtime_ns
        except OSError:
            pass
    return (st.st_size, st.st_mtime_ns, side_size, side_mtime_ns)


def _close_connection(conn) -> None:
    try:
        conn.close()
    except sqlite3.Error:
        pass


def _finalize_close(conn) -> None:
    """finalize 用（_close_connection を呼ぶ時点で引き直す）。"""
    _close_connection(conn)


class MetaCache:
    """★の読み取り結果の永続キャッシュ（SQLite）。スレッドセーフ。

    cache = MetaCache()                      # 既定の場所
    cache = MetaCache(db_path)               # テストなどでの差し替え
    meta = cache.read_cached(path)           # キャッシュが効けばファイルを開かない
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = os.fspath(db_path) if db_path else CACHE_PATH
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------ 接続

    def _connect(self) -> sqlite3.Connection | None:
        """接続を遅延して作る。作れなければ None（キャッシュ無しとして動き続ける）。"""
        if self._conn is not None:
            return self._conn
        try:
            folder = os.path.dirname(self.db_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(_SCHEMA)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(meta)")}
            if "edits" not in cols:
                # 補正（crs:）も覚える（段階2で追加。既存のキャッシュには列を足す）
                conn.execute("ALTER TABLE meta ADD COLUMN edits TEXT")
            if "description" not in cols:
                # メモ（dc:description）も覚える（既存のキャッシュには列を足す）
                conn.execute("ALTER TABLE meta ADD COLUMN description TEXT")
            conn.commit()
        except (OSError, sqlite3.Error):
            return None
        self._conn = conn
        # 使い終わって参照が無くなったら、接続も閉じる（Windows では開いた
        # ままの SQLite ファイルが消せなくなるため。close() 済みなら何もしない）
        self._finalizer = weakref.finalize(self, _finalize_close, conn)
        return conn

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        finalizer = getattr(self, "_finalizer", None)
        if finalizer is not None:
            finalizer.detach()
        if conn is not None:
            _close_connection(conn)

    def __enter__(self) -> "MetaCache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 読み書き

    def get(self, path) -> PhotoMeta | None:
        """キャッシュが効くときだけ PhotoMeta を返す。 miss（未登録・変化あり・本体なし）は None。"""
        path = os.fspath(path)
        stat_key = _stat_key(path)
        if stat_key is None:
            return None
        with self._lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT size, mtime_ns, side_size, side_mtime_ns, rating, keywords,"
                    " edits, description FROM meta WHERE path = ?", (normalize_key(path),)).fetchone()
            except sqlite3.Error:
                return None
        if row is None or tuple(row[:4]) != stat_key:
            return None
        try:
            keywords = tuple(k for k in json.loads(row[5]) if isinstance(k, str))
        except (ValueError, TypeError):
            keywords = ()
        return PhotoMeta(int(row[4]), keywords,
                         _text_to_edits(row[6] if len(row) > 6 else None),
                         (row[7] if len(row) > 7 and isinstance(row[7], str) else ""))

    def put(self, path, meta: PhotoMeta | None) -> None:
        """読み取り結果を覚える。meta が None（不明）なら覚えず、古い値があれば消す。"""
        if meta is None:
            self.delete(path)
            return
        path = os.fspath(path)
        stat_key = _stat_key(path)
        if stat_key is None:
            self.delete(path)
            return
        with self._lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO meta"
                    " (path, size, mtime_ns, side_size, side_mtime_ns, rating, keywords,"
                    " edits, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (normalize_key(path), *stat_key, int(meta.rating),
                     json.dumps(list(meta.keywords), ensure_ascii=False),
                     _edits_to_text(meta.edits), meta.description or ""))
                conn.commit()
            except sqlite3.Error:
                pass

    def update(self, path, meta: PhotoMeta | None) -> None:
        """書き込みの成功後に、キャッシュを新しい値で上書きする（stat が同じでも効く）。

        ★を書く処理は更新日時を元に戻すため、put と同じく今の stat で覚え直す。
        meta が None（読み直せなかった）なら古い値を消す。
        """
        self.put(path, meta)

    def delete(self, path) -> None:
        with self._lock:
            conn = self._connect()
            if conn is None:
                return
            try:
                conn.execute("DELETE FROM meta WHERE path = ?",
                             (normalize_key(path),))
                conn.commit()
            except sqlite3.Error:
                pass

    def read_cached(self, path, reader=read_meta) -> PhotoMeta | None:
        """キャッシュを通して読む。ヒットすればファイルを開かない。

        reader は読み取り関数（テストで差し替えられるように。既定は xmp.read_meta）。
        """
        hit = self.get(path)
        if hit is not None:
            return hit
        meta = reader(path)
        if meta is not None:
            self.put(path, meta)
        return meta


# ------------------------------------------------------------------ 既定のキャッシュ

_default_cache: MetaCache | None = None
_default_lock = threading.Lock()


def default_cache() -> MetaCache:
    """アプリで共有する既定のキャッシュ（初回に使うときに作る）。"""
    global _default_cache
    with _default_lock:
        if _default_cache is None:
            _default_cache = MetaCache()
        return _default_cache


def read_meta_cached(path, cache: MetaCache | None = None, reader=read_meta) -> PhotoMeta | None:
    """既定の（または指定した）キャッシュを通して★を読む。"""
    return (cache or default_cache()).read_cached(path, reader)


def read_metas(paths, cache: MetaCache | None = None, workers: int = READ_WORKERS,
               cancel=None, progress=None, reader=read_meta, on_result=None):
    """複数枚をキャッシュ＋並行読みで読む。

    戻り値: (results, hits, reads, cancelled)
        results  … {入力のパス: PhotoMeta | None}（読めた順ではなく入力のパスがキー）
        hits     … キャッシュで済んだ枚数
        reads    … 実際にファイルを読んだ枚数
        cancelled… cancel（threading.Event）が立って途中でやめたら True（results は途中まで）
    progress(done, total) があれば、1枚終わるごとに呼ぶ（呼ぶのはこの関数のスレッド）。
    on_result(path, meta) があれば、1枚終わるごとに結果も渡す（キャッシュで済んだ分は
    先にまとめて渡す）。
    """
    cache = cache or default_cache()
    todo = []
    seen = set()
    for p in paths:
        p = os.fspath(p)
        if p not in seen:
            seen.add(p)
            todo.append(p)
    total = len(todo)
    results: dict[str, PhotoMeta | None] = {}
    hits = 0
    done = 0
    pending = []
    for p in todo:
        if cancel is not None and cancel.is_set():
            return results, hits, 0, True
        hit = cache.get(p)
        if hit is not None:
            results[p] = hit
            hits += 1
            done += 1
            if on_result is not None:
                on_result(p, hit)
            if progress is not None:
                progress(done, total)
        else:
            pending.append(p)
    reads = 0
    if not pending:
        return results, hits, reads, False
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {executor.submit(cache.read_cached, p, reader): p for p in pending}
        waiting = set(futures)
        while waiting:
            finished, waiting = wait(waiting, timeout=0.05,
                                     return_when=FIRST_COMPLETED)
            for fut in finished:
                p = futures[fut]
                try:
                    results[p] = fut.result()
                except Exception:  # noqa: BLE001  1枚読めないだけ。不明として返す
                    results[p] = None
                reads += 1
                done += 1
                if on_result is not None:
                    on_result(p, results[p])
                if progress is not None:
                    progress(done, total)
            if cancel is not None and cancel.is_set():
                cancelled = True
                for fut in waiting:
                    fut.cancel()
                break
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results, hits, reads, cancelled
