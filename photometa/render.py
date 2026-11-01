# photometa/render.py
# 補正（crs: メタデータ）を画素に当てる。Qt に依存しない（Pillow）。
#
# 元の写真は書き換えない。フォトフレーム同期とスマホへ送るときに、その場で
# 補正を当てた JPEG を作るために使う。当てる順番は、EXIF の向き → 傾き →
# トリミング → 明るさ・コントラスト・彩度。
#
# 計算は Lightroom と同じではない簡易なもの:
#   明るさ   … Exposure2012 を EV として 2**EV 倍
#   コントラスト・彩度 … (100 + 値) / 100 倍（-100〜100 → 0〜2 倍）
#   傾き     … CropAngle 度だけ反時計回りに回す（Pillow の rotate は反時計が正。
#              Lightroom の CropAngle は「写真を時計回りに回して真っ直ぐにする」
#              向きなので、符号を反転して渡す）。回した四隅の余白は切り落とさず
#              黒で埋まる（トリミングと併用する前提）。
import io

from .xmp import Edits

# 出力 JPEG に残す EXIF（export.build_frame_jpeg の保持タグと同じ考え方。
# 撮影日時（DateTimeOriginal）は、みてね が並び順に使うので必ず残す）
_IFD0_KEEP = (271, 272, 274, 282, 283, 296, 305, 306, 315, 33432)
_EXIF_KEEP = (33434, 33437, 34850, 34855, 36864, 36867, 36868, 37377, 37378,
              37379, 37380, 37381, 37383, 37384, 37385, 37386, 37500, 37510,
              40960, 40961, 40962, 40963, 40964, 40965, 41985, 41986, 41987,
              41988, 41989, 41990, 41991, 41992, 41993, 41994, 41995, 41996)
_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825


def apply_edits(image, edits: Edits | None):
    """PIL Image に補正を当てた新しい画像を返す（元の画像は変えない）。

    EXIF の向きの補正も含むので、開いたばかりの画像を渡すこと。
    補正が無ければ、向きだけ直した複製を返す。
    """
    from PIL import Image, ImageEnhance, ImageOps

    img = ImageOps.exif_transpose(image)
    if img is None:
        img = image.copy()
    if edits is None or edits.is_empty():
        return img
    if edits.angle:
        img = img.rotate(-edits.angle, resample=Image.BICUBIC, expand=False,
                         fillcolor=(0, 0, 0))
    if edits.crop is not None:
        top, left, bottom, right = edits.crop
        w, h = img.size
        box = (round(left * w), round(top * h),
               max(round(left * w) + 1, round(right * w)),
               max(round(top * h) + 1, round(bottom * h)))
        img = img.crop(box)
    if edits.exposure:
        img = ImageEnhance.Brightness(img).enhance(2.0 ** edits.exposure)
    if edits.contrast:
        img = ImageEnhance.Contrast(img).enhance((100.0 + edits.contrast) / 100.0)
    if edits.saturation:
        img = ImageEnhance.Color(img).enhance((100.0 + edits.saturation) / 100.0)
    return img


def _kept_exif(im, strip_gps: bool):
    """元の EXIF から、出力に残すタグだけを持った Exif を作る。向き（274）は
    画素を回して直すので落とす。GPS は strip_gps のとき落とす。"""
    from PIL import Image

    try:
        exif = im.getexif()
    except Exception:  # noqa: BLE001
        return None
    if not exif:
        return None
    out = Image.Exif()
    for tag in _IFD0_KEEP:
        if tag != 274 and tag in exif:
            out[tag] = exif[tag]
    try:
        sub = exif.get_ifd(_EXIF_IFD)
    except Exception:  # noqa: BLE001
        sub = {}
    kept = {tag: sub[tag] for tag in _EXIF_KEEP if tag in sub}
    if kept:
        out.get_ifd(_EXIF_IFD).update(kept)
    if not strip_gps:
        try:
            gps = exif.get_ifd(_GPS_IFD)
        except Exception:  # noqa: BLE001
            gps = {}
        if gps:
            out.get_ifd(_GPS_IFD).update(gps)
    return out


def suggest_crop(img_w: int, img_h: int, faces, aspect: float):
    """顔の位置から、おまかせのトリミング枠を提案する。

    faces は (x, y, w, h) の比率（表示向き基準）の並び、aspect は幅/高さ。
    顔全体の束に余白（左右は束の幅の 0.6 倍、上は顔の高さの 0.8 倍、
    下は 1.6 倍で胸元まで）を足し、縦横比に合わせて広げて写真の中に収める。
    戻り値は Edits.crop と同じ (top, left, bottom, right) の比率。
    顔が無ければ None。
    """
    if not faces or aspect <= 0 or img_w <= 0 or img_h <= 0:
        return None
    x1 = min(f[0] for f in faces) * img_w
    y1 = min(f[1] for f in faces) * img_h
    x2 = max((f[0] + f[2]) for f in faces) * img_w
    y2 = max((f[1] + f[3]) for f in faces) * img_h
    uw, uh = x2 - x1, y2 - y1
    bx1, bx2 = x1 - uw * 0.6, x2 + uw * 0.6
    by1, by2 = y1 - uh * 0.8, y2 + uh * 1.6
    bw, bh = bx2 - bx1, by2 - by1
    cx, cy = (bx1 + bx2) / 2, (by1 + by2) / 2
    if bw / bh < aspect:      # 横が足りない → 広げる
        bw = bh * aspect
    else:                     # 縦が足りない → 上に多めに広げる
        bh = bw / aspect
    bx1, bx2 = cx - bw / 2, cx + bw / 2
    by1, by2 = cy - bh * 0.6, cy + bh * 0.4
    # 写真の中に収める（寄せて入らなければ、その辺は写真いっぱいにして他方を絞る）
    if bw >= img_w:
        bx1, bx2 = 0.0, float(img_w)
        bh = img_w / aspect
        by1, by2 = cy - bh * 0.6, cy + bh * 0.4
    else:
        if bx1 < 0:
            bx2 -= bx1
            bx1 = 0.0
        if bx2 > img_w:
            bx1 -= bx2 - img_w
            bx2 = float(img_w)
    if bh >= img_h:
        by1, by2 = 0.0, float(img_h)
        bw = img_h * aspect
        bx1, bx2 = max(0.0, min(cx - bw / 2, img_w - bw)), 0.0
        bx2 = bx1 + bw
    else:
        if by1 < 0:
            by2 -= by1
            by1 = 0.0
        if by2 > img_h:
            by1 -= by2 - img_h
            by2 = float(img_h)
    # 顔の束がはみ出すときは、縦横比より顔を優先して束まで広げる
    bx1, by1 = min(bx1, x1), min(by1, y1)
    bx2, by2 = max(bx2, x2), max(by2, y2)
    return (by1 / img_h, bx1 / img_w, by2 / img_h, bx2 / img_w)


def render_to_jpeg(src: str, out_path: str, edits: Edits | None,
                   quality: int = 92, strip_gps: bool = True) -> None:
    """補正を当てた JPEG を out_path に作る。撮影日時は元の EXIF から残す。"""
    from PIL import Image

    with Image.open(src) as im:
        icc = im.info.get("icc_profile")
        out_exif = _kept_exif(im, strip_gps)
        img = apply_edits(im, edits)
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        kwargs = {"quality": quality, "optimize": True}
        if out_exif is not None:
            kwargs["exif"] = out_exif.tobytes()
        if icc:
            kwargs["icc_profile"] = icc
        img.save(out_path, "JPEG", **kwargs)


def render_bytes(src: str, edits: Edits | None, quality: int = 92,
                 strip_gps: bool = True) -> bytes:
    """補正を当てた JPEG のバイト列（スマホへ送る用）。"""
    buf = io.BytesIO()
    from PIL import Image

    with Image.open(src) as im:
        icc = im.info.get("icc_profile")
        out_exif = _kept_exif(im, strip_gps)
        img = apply_edits(im, edits)
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        kwargs = {"quality": quality, "optimize": True}
        if out_exif is not None:
            kwargs["exif"] = out_exif.tobytes()
        if icc:
            kwargs["icc_profile"] = icc
        img.save(buf, "JPEG", **kwargs)
    return buf.getvalue()
