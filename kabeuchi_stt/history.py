# -*- coding: utf-8 -*-
"""履歴と検索（SQLite）。GUI/Qt/COM 非依存。

- 録音ごとに 日時・長さ・先頭の一文・モデル を保存。全文は text（清書の原文）と
  edited_text（修正後）。元の清書は常に残す。
- 検索は空白区切りの AND、text / edited_text / title への部分一致（数百件規模なので全文走査）。
- 削除は DB 行を消し、関連ファイルは Windows のごみ箱へ（移せなかったファイルは残して返す）。
保存先: %LOCALAPPDATA%/kabeuchi_stt/history.db
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from paths import data_dir

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  duration REAL NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  wav_path TEXT NOT NULL DEFAULT '',
  txt_path TEXT NOT NULL DEFAULT '',
  md_path TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  edited_text TEXT,
  edited_at TEXT,
  segments_json TEXT NOT NULL DEFAULT '[]',
  cuts_json TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rec_created ON recordings(created_at DESC);
"""

_SENT = re.compile(r"(.+?[。！？!?\n])")


def first_sentence(text: str, limit: int = 40) -> str:
    """先頭の一文（句点がなければ先頭 limit 字）。"""
    t = (text or "").strip()
    if not t:
        return ""
    m = _SENT.match(t)
    s = (m.group(1) if m else t).strip()
    return s if len(s) <= limit else s[:limit] + "…"


def history_path() -> Path:
    return data_dir() / "history.db"


def send_to_recycle_bin(path: Path) -> None:
    """ファイルをごみ箱へ（SHFileOperationW, ctypes）。失敗したら例外。"""
    p = Path(path)
    if not p.exists():
        return
    if sys.platform != "win32":
        raise OSError("ごみ箱への移動は Windows のみ対応です")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = str(p.resolve()) + "\0"  # LPCWSTR が末尾にもう 1 つヌルを足す＝二重ヌル終端
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    r = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if r != 0 or op.fAnyOperationsAborted:
        raise OSError(f"ごみ箱へ移せませんでした (code={r}): {p}")


@dataclass
class Record:
    id: int
    created_at: str
    duration: float
    title: str
    model: str
    wav_path: str
    txt_path: str
    md_path: str
    text: str
    edited_text: str | None
    edited_at: str | None
    segments: list  # [(start, end, text), ...]
    cuts: list = field(default_factory=list)  # 無音を縮めた場所 [[元の start, 元の end, 残した長さ], ...]（Opus の音声の時刻合わせ用）

    @property
    def current_text(self) -> str:
        return self.edited_text if self.edited_text is not None else self.text


class History:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else history_path()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(recordings)").fetchall()}
            if "cuts_json" not in cols:       # 既存DBの自動マイグレーション
                self._db.execute("ALTER TABLE recordings ADD COLUMN cuts_json TEXT NOT NULL DEFAULT ''")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _rec(r: sqlite3.Row) -> Record:
        try:
            segs = [tuple(s) for s in json.loads(r["segments_json"] or "[]")]
        except Exception:
            segs = []
        try:
            from audio_compress import cuts_from_json
            cuts = cuts_from_json(r["cuts_json"])
        except Exception:
            cuts = []
        return Record(r["id"], r["created_at"], r["duration"], r["title"], r["model"], r["wav_path"],
                      r["txt_path"], r["md_path"], r["text"], r["edited_text"], r["edited_at"], segs, cuts)

    def add(self, *, text: str, segments: list, duration: float, model: str, wav_path="",
            txt_path="", md_path="", created_at: str | None = None) -> int:
        created_at = created_at or _dt.datetime.now().isoformat(timespec="seconds")
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO recordings(created_at,duration,title,model,wav_path,txt_path,md_path,text,segments_json)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (created_at, float(duration), first_sentence(text), model, str(wav_path), str(txt_path),
                 str(md_path), text, json.dumps([list(s) for s in segments], ensure_ascii=False)))
            self._db.commit()
            return int(cur.lastrowid)

    def update_final(self, rec_id: int, *, text: str, segments: list, duration: float, model: str,
                     txt_path="", md_path="") -> None:
        """WAV から清書をやり直した結果で置き換える（修正は破棄＝新しい清書が正）。"""
        with self._lock:
            self._db.execute(
                "UPDATE recordings SET text=?,segments_json=?,duration=?,model=?,title=?,txt_path=?,md_path=?,"
                "edited_text=NULL,edited_at=NULL,cuts_json='' WHERE id=?",
                (text, json.dumps([list(s) for s in segments], ensure_ascii=False), float(duration), model,
                 first_sentence(text), str(txt_path), str(md_path), rec_id))
            self._db.commit()

    def save_edit(self, rec_id: int, edited_text: str) -> None:
        with self._lock:
            self._db.execute("UPDATE recordings SET edited_text=?,edited_at=?,title=? WHERE id=?",
                             (edited_text, _dt.datetime.now().isoformat(timespec="seconds"),
                              first_sentence(edited_text), rec_id))
            self._db.commit()

    def revert_edit(self, rec_id: int) -> None:
        with self._lock:
            r = self._db.execute("SELECT text FROM recordings WHERE id=?", (rec_id,)).fetchone()
            if r:
                self._db.execute("UPDATE recordings SET edited_text=NULL,edited_at=NULL,title=? WHERE id=?",
                                 (first_sentence(r["text"]), rec_id))
                self._db.commit()

    def set_audio_path(self, rec_id: int, path: str | Path) -> None:
        """録音ファイルの場所を差し替える（WAV を Opus に圧縮したとき）。列名は wav_path のまま（WAV 以外も入る）。"""
        with self._lock:
            self._db.execute("UPDATE recordings SET wav_path=? WHERE id=?", (str(path), rec_id))
            self._db.commit()

    def set_cuts(self, rec_id: int, cuts) -> None:
        """無音を縮めた場所 [[start,end,keep],...]（元の時刻）を保存する。空なら記録なし。"""
        from audio_compress import cuts_to_json
        with self._lock:
            self._db.execute("UPDATE recordings SET cuts_json=? WHERE id=?", (cuts_to_json(cuts) if cuts else "", rec_id))
            self._db.commit()

    def get(self, rec_id: int) -> Record | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM recordings WHERE id=?", (rec_id,)).fetchone()
        return self._rec(r) if r else None

    def find_by_wav(self, wav_path: str | Path) -> Record | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM recordings WHERE wav_path=? ORDER BY id DESC LIMIT 1",
                                 (str(wav_path),)).fetchone()
        return self._rec(r) if r else None

    def list(self, limit: int = 500) -> list[Record]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM recordings ORDER BY created_at DESC, id DESC LIMIT ?",
                                    (limit,)).fetchall()
        return [self._rec(r) for r in rows]

    def search(self, query: str, limit: int = 200) -> list[Record]:
        """空白区切りの語をすべて含む録音（AND）。大文字小文字は区別しない。"""
        terms = [t for t in re.split(r"\s+", (query or "").strip()) if t]
        if not terms:
            return self.list(limit)
        out = []
        for r in self.list(100000):
            hay = "\n".join([r.title, r.text, r.edited_text or ""]).lower()
            if all(t.lower() in hay for t in terms):
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def delete(self, rec_id: int, trasher: Callable[[Path], None] = send_to_recycle_bin,
               include_files: bool = True) -> list[str]:
        """DB 行を消し、WAV/txt/md をごみ箱へ。ごみ箱に移せなかったファイルは残し、そのパスを返す。"""
        rec = self.get(rec_id)
        if rec is None:
            return []
        failed: list[str] = []
        if include_files:
            with self._lock:      # 同じ録音を別モデルで清書した別の履歴が使っているファイルは、ごみ箱へ移さず残す
                rows = self._db.execute("SELECT wav_path,txt_path,md_path FROM recordings WHERE id!=?", (rec_id,)).fetchall()
            shared = {str(x) for r in rows for x in (r[0], r[1], r[2]) if x}
            for p in (rec.wav_path, rec.txt_path, rec.md_path):
                if p and str(p) not in shared and Path(p).exists():
                    try:
                        trasher(Path(p))
                    except Exception:
                        failed.append(p)
        with self._lock:
            self._db.execute("DELETE FROM recordings WHERE id=?", (rec_id,))
            self._db.commit()
        return failed


def snippet(text: str, query: str, width: int = 24) -> str:
    """検索語の周辺を切り出す（一覧表示用）。"""
    terms = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    flat = (text or "").replace("\n", " ")
    for t in terms:
        i = flat.lower().find(t.lower())
        if i >= 0:
            a, b = max(0, i - width), min(len(flat), i + len(t) + width)
            return ("…" if a else "") + flat[a:b] + ("…" if b < len(flat) else "")
    return flat[: width * 2]
