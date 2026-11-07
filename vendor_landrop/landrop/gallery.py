"""「PCで表示」の候補を、過去の受信フォルダから選べるようにする（一覧・中身・サムネイル）。"""

from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .storage import SessionDirs
from .viewer import IMAGE_EXTS, VIDEO_EXTS

MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS
THUMB_PX = 320
THUMB_CACHE = 600  # 320px の JPEG は 1枚 15KB 前後なので 10MB 弱


def _is_media(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in MEDIA_EXTS and not p.name.startswith(".")


def folder_label(name: str, seconds: bool = False) -> str:
    """`2026-10-17` → `10/17`。以前のバッチごとのフォルダ `2026-10-17_130544` → `10/17 13:05`
    （seconds=True なら `10/17 13:05:44`）。どちらでもなければそのまま。"""
    for fmt, out in (
        (SessionDirs.DATE_FORMAT, "%m/%d"),
        (SessionDirs.LEGACY_FORMAT, "%m/%d %H:%M:%S" if seconds else "%m/%d %H:%M"),
    ):
        try:
            return datetime.strptime(name, fmt).strftime(out)
        except ValueError:
            continue
    return name


RECENT_NAMES = ("直近", "一つ前")


def recent_entries(sessions: SessionDirs, now: float | None = None) -> list[dict]:
    """「直近」「一つ前」の受信。写真・動画が1つも無い回は出さない。"""
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    out = []
    for i, batch in enumerate(sessions.recent(len(RECENT_NAMES))):
        count = sum(1 for p in batch.files if p.suffix.lower() in MEDIA_EXTS)
        if not count:
            continue
        started = datetime.fromtimestamp(batch.started)
        when = started.strftime("%H:%M") if started.date() == today else started.strftime("%m/%d %H:%M")
        out.append({"key": str(i), "label": f"{RECENT_NAMES[i]}（{when}・{count}件）", "count": count})
    return out


def recent_media(sessions: SessionDirs, index: int) -> list[Path] | None:
    batches = sessions.recent(len(RECENT_NAMES))
    if not 0 <= index < len(batches):
        return None
    return [p for p in batches[index].files if p.suffix.lower() in MEDIA_EXTS]


def list_folders(save_dir: Path, limit: int = 60) -> list[dict]:
    """写真・動画が入っている受信フォルダを新しい順に。保存先の直下にあるものは name="" で最後に。"""
    if not save_dir.is_dir():
        return []
    dirs = [d for d in save_dir.iterdir() if d.is_dir() and not d.name.startswith((".", "_"))]
    dirs.sort(key=lambda d: (d.name, d.stat().st_mtime), reverse=True)
    out = []
    for d in dirs:
        count = sum(1 for p in d.iterdir() if _is_media(p))
        if count:
            out.append({"name": d.name, "label": folder_label(d.name), "count": count})
        if len(out) >= limit:
            break
    # 同じ「分」に複数ある（間を空けて送り直した等）と見分けられないので、そのときだけ秒まで出す
    labels = [f["label"] for f in out]
    for f in out:
        if labels.count(f["label"]) > 1:
            f["label"] = folder_label(f["name"], seconds=True)
    root = sum(1 for p in save_dir.iterdir() if _is_media(p))
    if root:
        out.append({"name": "", "label": "保存先の直下", "count": root})
    return out


def resolve_folder(save_dir: Path, name: str) -> Path | None:
    """iPhone から来たフォルダ名を、保存先の直下のフォルダに限って実体に直す（../ などは通さない）。"""
    if name == "":
        return save_dir
    if "/" in name or "\\" in name or name in (".", "..") or name.startswith((".", "_")):
        return None
    folder = save_dir / name
    return folder if folder.is_dir() and folder.resolve().parent == save_dir.resolve() else None


def list_media(folder: Path) -> list[Path]:
    """受信した順（更新日時順）。"""
    return sorted((p for p in folder.iterdir() if _is_media(p)), key=lambda p: (p.stat().st_mtime, p.name))


class Thumbnails:
    """画像のサムネイル（JPEG）をメモリにキャッシュして返す。保存先フォルダには何も書かない。"""

    def __init__(self, size: int = THUMB_CACHE) -> None:
        self._cache: OrderedDict[tuple[Path, float], bytes] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, path: Path) -> bytes:
        key = (path, path.stat().st_mtime)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        data = self._make(path)
        with self._lock:
            self._cache[key] = data
            while len(self._cache) > self._size:
                self._cache.popitem(last=False)
        return data

    @staticmethod
    def _make(path: Path) -> bytes:
        from PIL import Image, ImageOps

        with Image.open(path) as im:
            im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))  # JPEG は縮小しながら読めるので速い
            im = ImageOps.exif_transpose(im) or im
            im.thumbnail((THUMB_PX, THUMB_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=80)
        return buf.getvalue()
