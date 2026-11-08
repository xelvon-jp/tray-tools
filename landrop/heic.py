"""HEIC/HEIF → JPEG 変換（transform フック）。"""

from __future__ import annotations

import logging
import shutil

from .hooks import ReceivedFile
from .storage import move_unique, temp_path

log = logging.getLogger("landrop")

HEIC_EXTS = {".heic", ".heif", ".hif"}
ORIGINALS_DIR = "_heic"
EXIF_ORIENTATION = 0x0112


class HeicToJpeg:
    """HEIC を同じフォルダに JPEG で書き出す。

    keep_original=True なら元の HEIC は保存先の `_heic` フォルダへ移し、False なら消す。
    変換に失敗した場合は例外を投げ、HookRegistry 側で握りつぶされて元の HEIC がそのまま残る。
    """

    def __init__(self, quality: int = 92, keep_original: bool = True) -> None:
        import pillow_heif

        pillow_heif.register_heif_opener()
        self.quality = quality
        self.keep_original = keep_original

    def __call__(self, item: ReceivedFile) -> ReceivedFile:
        src = item.path
        if src.suffix.lower() not in HEIC_EXTS:
            return item

        from PIL import Image, ImageOps

        with Image.open(src) as im:
            exif = im.getexif()
            icc = im.info.get("icc_profile")
            # 向きは画素に焼き込み、EXIF の Orientation は 1 に戻す（二重回転を防ぐ）
            rgb = (ImageOps.exif_transpose(im) or im).convert("RGB")
        exif[EXIF_ORIENTATION] = 1

        tmp = temp_path(src.parent)
        try:
            rgb.save(tmp, "JPEG", quality=self.quality, exif=exif.tobytes(), icc_profile=icc, optimize=True)
            shutil.copystat(src, tmp)
            dest = move_unique(tmp, src.parent, src.stem + ".jpg")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

        if self.keep_original:
            move_unique(src, src.parent / ORIGINALS_DIR, src.name)
        else:
            src.unlink()
        log.info("HEIC→JPEG: %s → %s", src.name, dest.name)
        return ReceivedFile(dest, item.original_name, dest.stat().st_size, item.client)
