"""受信ファイルの保存。ファイル名の無害化・重複回避・サイズ上限を担当。"""

from __future__ import annotations

import filecmp
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

CHUNK = 1024 * 1024

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class FileTooLarge(Exception):
    pass


def safe_filename(name: str | None, fallback: str = "file") -> str:
    """クライアントから来たファイル名を Windows で安全に使える形にする。

    ディレクトリ部分は捨てる（../ によるパストラバーサル対策）。
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = _INVALID_CHARS.sub("_", name).strip().rstrip(". ")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    if not stem or stem.upper() in _RESERVED:
        stem = fallback if not stem else f"_{stem}"
    # NTFS のファイル名上限 255 に余裕を持たせる
    ext = ext[:16]
    stem = stem[: 200 - len(ext)]
    return f"{stem}.{ext}" if ext else stem


def _candidates(name: str) -> Iterator[str]:
    yield name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    for i in range(1, 10_000):
        yield f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})"
    raise FileExistsError(name)


def temp_path(save_dir: Path) -> Path:
    """保存先フォルダ内の一時ファイル名（同じドライブなので確定時の rename が一瞬で済む）。"""
    save_dir.mkdir(parents=True, exist_ok=True)
    return save_dir / f".{uuid.uuid4().hex}.part"


def move_unique(src: Path, save_dir: Path, name: str) -> Path:
    """src を save_dir/name に移す。既にあれば `name (1).ext` …と番号を付け、上書きはしない。

    Windows の rename は移動先が既にあると失敗するので、同時受信でも取り違えない。
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    for candidate in _candidates(name):
        dest = save_dir / candidate
        try:
            os.rename(src, dest)
            return dest
        except FileExistsError:
            continue
    raise AssertionError("unreachable")


class PartFile:
    """保存先フォルダ内の一時ファイル（.part）に書き、最後に本来の名前へ確定する。

    途中で失敗・上限超過したら一時ファイルは消える。受信途中のファイルが
    保存フォルダに中途半端な名前で残らないようにするため。
    """

    def __init__(self, save_dir: Path, name: str, max_bytes: int) -> None:
        self.save_dir = save_dir
        self.name = name
        self.max_bytes = max_bytes
        self.size = 0
        self.part = temp_path(save_dir)
        self._f = open(self.part, "xb")

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            raise FileTooLarge(self.name)
        self._f.write(chunk)

    def commit(self) -> Path:
        self._f.close()
        return move_unique(self.part, self.save_dir, self.name)

    def discard(self) -> None:
        self._f.close()
        self.part.unlink(missing_ok=True)


def save_fileobj(src: BinaryIO, save_dir: Path, name: str, max_bytes: int) -> tuple[Path, int]:
    """ファイルオブジェクトを保存する（同期。スレッドプールから呼ぶ想定）。"""
    pf = PartFile(save_dir, name, max_bytes)
    try:
        while chunk := src.read(CHUNK):
            pf.write(chunk)
        return pf.commit(), pf.size
    except BaseException:
        pf.discard()
        raise


@dataclass
class Batch:
    """1回分の受信（送る側が続けて送ったひとまとまり）。"""

    id: str
    started: float  # UNIX 時刻
    files: list[Path] = field(default_factory=list)


class SessionDirs:
    """受信をまとまり（バッチ）ごとに数え、保存先フォルダを決める。

    - 保存先は `保存先/2026-10-17` の日付フォルダ（date_folders=False なら保存先そのもの）。
      以前はバッチごとに `受信日時` のフォルダを作っていたが、フォルダが増えすぎて選びにくかった
    - 直近のバッチはどのファイルが届いたかを覚えておき、「直近」「一つ前」として取り出せる。
      state_file を渡すと再起動をまたいで覚えている
    - 受信が `gap_sec` 秒途切れたら次の受信は新しいバッチ。判定はリクエストの開始時（本文の受信前）で、
      受信中のリクエストがある間は同じバッチのまま（大きな動画の受信中に分かれないように）
    """

    DATE_FORMAT = "%Y-%m-%d"
    LEGACY_FORMAT = "%Y-%m-%d_%H%M%S"  # 以前のバッチごとのフォルダ名（一覧の表示用に残す）
    KEEP = 5

    def __init__(
        self,
        base: Path,
        gap_sec: float = 60,
        date_folders: bool = True,
        clock=time.time,
        state_file: Path | None = None,
    ) -> None:
        self.base = base
        self.gap_sec = gap_sec
        self.date_folders = date_folders
        self._clock = clock
        self._lock = threading.Lock()
        self._active = 0
        self._last_end = float("-inf")
        self._state_file = state_file
        self._batches: list[Batch] = self._load()  # 新しい順
        self._current: Batch | None = None  # 今のバッチ（再起動をまたいでは続けない）

    def begin(self) -> None:
        with self._lock:
            now = self._clock()
            if self._active == 0 and (self._current is None or now - self._last_end > self.gap_sec):
                self._current = Batch(uuid.uuid4().hex[:8], now)
            self._active += 1

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_end = self._clock()

    def folder_for(self, ts: float) -> Path:
        if not self.date_folders:
            return self.base
        return self.base / datetime.fromtimestamp(ts).strftime(self.DATE_FORMAT)

    @property
    def current(self) -> Path:
        """今のバッチの保存先フォルダ（フォルダは保存時に作られる）。"""
        return self.folder_for(self._current.started if self._current else self._clock())

    @property
    def current_batch_id(self) -> str:
        return self._current.id if self._current else ""

    def add(self, paths: list[Path]) -> str:
        """保存し終えたファイルを今のバッチに記録する。バッチの id を返す。"""
        with self._lock:
            batch = self._current
            if batch is None:
                return ""
            if not batch.files:
                self._batches.insert(0, batch)
                del self._batches[self.KEEP:]
            batch.files.extend(p for p in paths if p not in batch.files)  # 同じ回に同じファイルを送り直しても1つ
            snapshot = [Batch(b.id, b.started, list(b.files)) for b in self._batches]
        self._save(snapshot)
        return batch.id

    def recent(self, n: int = 2) -> list[Batch]:
        """新しい順。消されたファイルは除き、1つも残っていないバッチは飛ばす。"""
        with self._lock:
            batches = [Batch(b.id, b.started, list(b.files)) for b in self._batches]
        out = []
        for b in batches:
            b.files = [p for p in b.files if p.is_file()]
            if b.files:
                out.append(b)
            if len(out) >= n:
                break
        return out

    # ---- 再起動をまたいで覚えておく（保存先に小さな JSON を置く） ----

    def _load(self) -> list[Batch]:
        if not self._state_file or not self._state_file.is_file():
            return []
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            return [Batch(b["id"], b["started"], [self.base / f for f in b["files"]]) for b in data["batches"]]
        except (ValueError, KeyError, TypeError, OSError):
            return []  # 壊れていたら覚えていないだけ

    def _save(self, batches: list[Batch]) -> None:
        if not self._state_file:
            return
        def rel(p: Path) -> str:
            try:
                return p.relative_to(self.base).as_posix()
            except ValueError:
                return str(p)
        data = {"batches": [{"id": b.id, "started": b.started, "files": [rel(p) for p in b.files]} for b in batches]}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_file)
            _hide(self._state_file)
        except OSError:
            pass


def _hide(path: Path) -> None:
    """Explorer で目障りにならないよう隠しファイルにする（Windows のみ）。"""
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN


def reuse_same_file(path: Path) -> tuple[Path, bool]:
    """保存したばかりのファイルと中身が同じものが、同じフォルダ（同じ日）に既にあれば、新しい方を消して
    既存のファイルを返す（2つ目は True）。同じ写真を送り直してもファイルが増えないように。

    iPhone は送るたびに名前を変えることがある（image.jpg など）ので、名前ではなく中身で比べる。
    HEIC→JPEG 変換済みなら元の HEIC は `_heic` にあるので、そちらと一致したら変換後の JPEG を返す。
    """
    size = path.stat().st_size
    folder = path.parent
    for d in (folder, folder / "_heic"):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p == path or p.name.startswith(".") or not p.is_file():
                continue
            try:
                same = p.stat().st_size == size and filecmp.cmp(p, path, shallow=False)
            except OSError:
                continue
            if not same:
                continue
            if d is not folder:
                jpg = folder / (p.stem + ".jpg")
                if not jpg.is_file():
                    continue  # 変換後の JPEG が消されている → 新しい方を残して変換し直す
                p = jpg
            path.unlink()
            return p, True
    return path, False
