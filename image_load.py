# image_load.py
# 画像を「箱に収まる大きさで・EXIF の向きを当てて」読む共通部品。
# あっふのプレビュー(filer_preview.py)とスライドショー(slideshow.py)が同じものを使う。
#
#   - 縮めて読む: QImageReader.setScaledSize に渡して、巨大な画像でも全部を展開しない
#     (JPEG はデコーダが DCT の段階で縮めるので速い)。拡大はしない
#   - EXIF の向き: JPEG / TIFF は Qt の自動回転(setAutoTransform)、Qt が EXIF を読まない
#     PNG(eXIf)/ WebP(EXIF チャンク)は自前で読んで(filer_core.container_exif_orientation)
#     読み出した後に回す
#
# どのスレッドから呼んでもよい(QImage はスレッドをまたいで渡してよい。QPixmap にするのは
# UI スレッドで)。Qt に依存しない計算(向きの行列・収める大きさ)は filer_core にある。
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader, QTransform

import filer_core as core


def turned_image(image: QImage, turn) -> QImage:
    """core の向きの行列(TURN_*)を当てた絵。元のままならそのまま返す。
    QImage.transformed は回したぶんの平行移動を自分で補うので、行列だけ渡せばよい。"""
    if tuple(turn) == core.TURN_IDENTITY or image.isNull():
        return image
    m11, m12, m21, m22 = turn
    return image.transformed(QTransform(m11, m12, m21, m22, 0, 0), Qt.FastTransformation)


def unreadable_reason(path: str, size: QSize, qt_message: str = "") -> str:
    """画像が読めなかった理由を、利用者向けの一文にする。

    ヘッダから大きさが分かるのに読めないのは、ほとんどが Qt の読み込み上限
    (QImageReader.allocationLimit。既定 256MB、展開後 4 バイト/画素で数える)。
    PNG / BMP などは縮めながら読めず、全部を展開するので約 64MP(8000×8000)を超えると当たる。"""
    limit_mb = QImageReader.allocationLimit()
    if size.isValid():
        pixels = size.width() * size.height()
        need_mb = pixels * 4 / (1024 * 1024)
        if limit_mb and need_mb > limit_mb:
            return (f"画像が大きすぎて読めません（{size.width()}×{size.height()}"
                    f" = {pixels / 1_000_000:.0f}MP。展開に約 {need_mb:.0f}MB 要り、"
                    f"表示の上限は {limit_mb}MB です）")
        return (f"画像を読めませんでした（{size.width()}×{size.height()}）。"
                "ファイルが壊れているか、途中で切れている可能性があります"
                + (f"［{qt_message}］" if qt_message else ""))
    if not os.path.exists(path):
        return "ファイルが見つかりません"
    return ("画像として読めません。対応していない形式か、ファイルが壊れている可能性があります"
            + (f"［{qt_message}］" if qt_message else ""))


PILLOW_EXTS = frozenset({"heic", "heif", "psd"})    # Qt が読めない形式は Pillow で読む
SVG_EXTS = frozenset({"svg"})
PSD_MAX_PIXELS = 100_000_000            # これを超える PSD は読まない(固まり・メモリ防止)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def _check_psd_header(path: str) -> None:
    """PSD の先頭 26 バイトを見て、Pillow が合成画像を出せない形式なら OSError で断る。"""
    import struct

    with open(path, "rb") as f:
        head = f.read(26)
    if len(head) < 26 or head[:4] != b"8BPS":
        raise OSError("PSD ではありません（ヘッダが違います）")
    version, _res, channels, height, width, depth, mode = struct.unpack(">H6sHIIHH", head[4:26])
    if version == 2:
        raise OSError("プレビュー非対応のPSD（PSB: 大きいドキュメント形式）")
    if depth != 8:
        raise OSError(f"プレビュー非対応のPSD（{depth}bit/チャンネル。8bit のみ対応）")
    if mode not in (0, 1, 2, 3, 4, 7, 8, 9):
        raise OSError(f"プレビュー非対応のPSD（カラーモード {mode}）")
    if mode == 2:
        pass    # インデックスカラー(Pillow は P として読める)
    if width * height > PSD_MAX_PIXELS:
        raise OSError(f"プレビュー非対応のPSD（{width}×{height} は大きすぎます）")


def read_pillow_image(path: str, box_w: int, box_h: int):
    """HEIC / HEIF(pillow_heif)・PSD(Pillow の合成画像)を読む。read_image と同じ返し方。
    EXIF の向きは当てる。読めなければ OSError(理由つき)。"""
    ext = _ext(path)
    try:
        from PIL import Image, ImageOps

        if ext in ("heic", "heif"):
            import pillow_heif

            pillow_heif.register_heif_opener()
        elif ext == "psd":
            _check_psd_header(path)
        with Image.open(path) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            original = im.size
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            scaled = False
            w, h = core.fit_size(im.size[0], im.size[1], box_w, box_h)
            if (w, h) != im.size:
                im = im.resize((w, h), Image.LANCZOS)
                scaled = True
            has_alpha = im.mode == "RGBA"
            data = im.tobytes()
            qimg = QImage(data, im.size[0], im.size[1], im.size[0] * (4 if has_alpha else 3),
                          QImage.Format_RGBA8888 if has_alpha else QImage.Format_RGB888).copy()
    except OSError:
        raise
    except ImportError as e:
        raise OSError(f"{ext.upper()} を読むための部品がありません（{e}）") from e
    except Exception as e:  # noqa: BLE001  Pillow の SyntaxError など、読めない形式は理由つきで
        if ext == "psd":
            raise OSError(f"プレビュー非対応のPSD（{e}）") from e
        raise OSError(f"画像として読めません（{e}）") from e
    return qimg, original, scaled


def read_svg_image(path: str, box_w: int, box_h: int):
    """SVG を QtSvg で白背景に描く(箱に収まる大きさ。小さい絵は箱いっぱいまで広げる)。"""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPainter

    try:
        from PySide6.QtSvg import QSvgRenderer
    except ImportError as e:
        raise OSError("SVG を表示できません（PySide6 の QtSvg がありません）") from e
    renderer = QSvgRenderer(path)
    if not renderer.isValid():
        raise OSError("SVG として読めません（壊れている・非対応の記述）")
    default = renderer.defaultSize()
    vb = renderer.viewBoxF()
    if default.isEmpty():
        default = vb.size().toSize() if not vb.isEmpty() else QSize(box_w, box_h)
    dw, dh = max(default.width(), 1), max(default.height(), 1)
    scale = min(max(box_w, 16) / dw, max(box_h, 16) / dh)
    w, h = max(1, int(dw * scale)), max(1, int(dh * scale))
    image = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, w, h))
    painter.end()
    return image, (dw, dh), (w, h) != (dw, dh)


def read_image(path: str, box_w: int, box_h: int):
    """(絵, 表示の向きでの元の大きさ (幅, 高さ) or None, 縮めて読んだか)。

    EXIF の向き(カメラ・スマホの縦撮り)は当ててから返す。JPEG / TIFF は Qt の
    自動回転(setAutoTransform)で、Qt が EXIF を読まない WebP / PNG は自前で読んで
    (core.container_exif_orientation)当てる。読めなければ OSError。
    HEIC / HEIF / PSD は Pillow、SVG は QtSvg で読む(上の read_pillow_image / read_svg_image)。"""
    ext = _ext(path)
    if ext in PILLOW_EXTS:
        return read_pillow_image(path, box_w, box_h)
    if ext in SVG_EXTS:
        return read_svg_image(path, box_w, box_h)
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    transformation = reader.transformation()
    qt_rotated = bool(transformation & QImageIOHandler.TransformationRotate90)
    own_turn = core.TURN_IDENTITY
    if transformation == QImageIOHandler.TransformationNone:
        own_turn = core.EXIF_ORIENTATION_TURNS.get(
            core.container_exif_orientation(path), core.TURN_IDENTITY)
    rotated = qt_rotated or core.turn_swaps_size(own_turn)
    scaled = False
    if size.isValid():
        # EXIF の回転(スマホの縦撮り写真など)があると、読み出した後に
        # 縦横が入れ替わる。縮小の大きさは「表示後の縦横」で決めないと、
        # 回転したぶん箱からはみ出し、窓の大きさまで変わってしまう。
        # setScaledSize は回転の前(保存されている向き)の大きさで渡す。
        shown = QSize(size.height(), size.width()) if rotated else size
        w, h = core.fit_size(shown.width(), shown.height(), box_w, box_h)
        target = shown.scaled(w, h, Qt.KeepAspectRatio)
        if rotated:
            target = QSize(target.height(), target.width())
        if target != size:
            reader.setScaledSize(target)
            scaled = True
    image = reader.read()
    if image.isNull():
        raise OSError(unreadable_reason(path, size, reader.errorString()))
    image = turned_image(image, own_turn)
    if size.isValid():
        original = (size.height(), size.width()) if rotated else (size.width(), size.height())
    else:
        original = None
    return image, original, scaled
