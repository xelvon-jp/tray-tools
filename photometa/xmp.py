# photometa/xmp.py
# 写真の★（xmp:Rating）とキーワード（dc:subject）を、ExifTool を使わずに読む。
#
# 一覧の★列を埋めるために大量のファイルを読むので、ファイル全体は読まない：
#   - JPEG … マーカーをたどって APP1 の XMP セグメント（http://ns.adobe.com/xap/1.0/\0）を探す。
#            SOS（画像データの始まり）に達したら打ち切る（XMP はそれより前にしか無い）
#   - PNG  … チャンクをたどって iTXt の XML:com.adobe.xmp を探す（データ部は seek で飛ばす）
#   - WebP … RIFF のチャンクをたどって "XMP " を探す（ExifTool は末尾に置くので、たどる）
#   - それ以外（HEIC / TIFF など）… 先頭 1MB から <x:xmpmeta 〜 </x:xmpmeta> を探す簡易法。
#            見つからず大きいファイルなら末尾も少し見る
#   - RAW … 同名のサイドカー（拡張子を .xmp に置き換えたもの）を読む。無ければ★なし
#
# 形式は拡張子ではなく先頭のバイト列で見分ける（拡張子と中身が食い違うファイルもあるため）。
#
# 戻り値：
#   PhotoMeta(rating, keywords) … rating は -1〜5（書かれていなければ 0）
#   None                        … 読めない（開けない・壊れている）。フィルタでは「不明」になる
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from xml.etree import ElementTree as ET

PHOTO_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff",
})
RAW_EXTS = frozenset({
    ".arw", ".cr2", ".cr3", ".nef", ".dng", ".orf", ".rw2", ".raf",
})
ALL_EXTS = PHOTO_EXTS | RAW_EXTS

HEAD_BYTES = 1024 * 1024        # 簡易法で先頭から探す量
TAIL_BYTES = 256 * 1024         # 見つからなかったときに末尾から探す量

_JPEG_XMP_SIG = b"http://ns.adobe.com/xap/1.0/\x00"
_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_NS_XMP = "http://ns.adobe.com/xap/1.0/"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
_RATING = f"{{{_NS_XMP}}}Rating"
_SUBJECT = f"{{{_NS_DC}}}subject"
_LI = f"{{{_NS_RDF}}}li"
_DESC = f"{{{_NS_RDF}}}Description"


@dataclass(frozen=True)
class Edits:
    """写真の補正（非破壊編集）。XMP の crs:（Camera Raw）名前空間に持つ。

    crop は (top, left, bottom, right) で、各 0〜1 の比率（crs:CropTop など。
    bottom/right は下端・右端の位置）。傾き angle は度（crs:CropAngle）、
    exposure は EV（crs:Exposure2012）、contrast / saturation は -100〜100
    （crs:Contrast2012 / crs:Saturation）。temperature（色温度）と tint（色合い）は
    色被りの補正で -100〜100（crs:Temperature（相対値。TemperatureUnit=1）/
    crs:Tint）。"""
    crop: tuple[float, float, float, float] | None = None
    angle: float = 0.0
    exposure: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    temperature: float = 0.0
    tint: float = 0.0

    def is_empty(self) -> bool:
        return (self.crop is None and self.angle == 0 and self.exposure == 0
                and self.contrast == 0 and self.saturation == 0
                and self.temperature == 0 and self.tint == 0)

    def sanitized(self) -> "Edits | None":
        """値の範囲を正した補正を返す（文章など外から来た値の検査用）。

        数値は範囲で切りそろえ（NaN・無限大は 0）、切り抜きは 0〜1 に
        切りそろえた上で、幅・高さが 1% 未満なら枠ごと捨てる。
        全部空になれば None。
        """
        import math

        def clamp(v, lo, hi):
            try:
                v = float(v)
            except (TypeError, ValueError):
                return 0.0
            if not math.isfinite(v):
                return 0.0
            return max(lo, min(hi, v))

        crop = self.crop
        if crop is not None:
            try:
                top, left, bottom, right = (clamp(v, 0.0, 1.0) for v in crop)
                crop = ((top, left, bottom, right)
                        if bottom - top >= 0.01 and right - left >= 0.01 else None)
            except (TypeError, ValueError):
                crop = None
        out = Edits(crop=crop,
                    angle=clamp(self.angle, -180.0, 180.0),
                    exposure=clamp(self.exposure, -10.0, 10.0),
                    contrast=clamp(self.contrast, -100.0, 100.0),
                    saturation=clamp(self.saturation, -100.0, 100.0),
                    temperature=clamp(self.temperature, -100.0, 100.0),
                    tint=clamp(self.tint, -100.0, 100.0))
        return None if out.is_empty() else out

    def signature(self) -> str:
        """台帳の「作り方の署名」に入れる安定した文字列。"""
        crop = "" if self.crop is None else ",".join(f"{v:.4f}" for v in self.crop)
        return (f"crop[{crop}]a{self.angle:.2f}e{self.exposure:.2f}"
                f"c{self.contrast:.0f}s{self.saturation:.0f}"
                f"t{self.temperature:.0f}u{self.tint:.0f}")


@dataclass(frozen=True)
class PhotoMeta:
    rating: int = 0                       # -1（削除候補）〜 5。書かれていなければ 0
    keywords: tuple[str, ...] = ()        # dc:subject の並び（書かれている順）
    edits: Edits | None = None            # 補正（crs:）。無ければ None


class _Broken(Exception):
    """ファイルの構造が壊れている（読み取りを諦めて None を返す）。"""


def _ext(name) -> str:
    return os.path.splitext(os.fspath(name))[1].lower()


def is_photo(name) -> bool:
    """★を付けられる写真か（拡張子で判定。ファイルは開かない）。"""
    return _ext(name) in ALL_EXTS


def is_raw(name) -> bool:
    return _ext(name) in RAW_EXTS


def sidecar_path(path) -> str:
    """RAW のサイドカーの場所（拡張子を .xmp に置き換えたもの）。"""
    return os.path.splitext(os.fspath(path))[0] + ".xmp"


# ------------------------------------------------------------------ XMP パケットの解釈

def parse_xmp(packet: bytes | str) -> PhotoMeta | None:
    """XMP パケット（<x:xmpmeta> または <rdf:RDF> を含むもの）から★とキーワードを取り出す。

    xmp:Rating は属性形式（xmp:Rating="3"）と要素形式（<xmp:Rating>3</xmp:Rating>）の両方を受ける。
    "3.0" のような小数も受ける。XML として壊れている・Rating が数でないときは None。
    """
    if isinstance(packet, str):
        packet = packet.encode("utf-8")
    body = _cut(packet, b"<x:xmpmeta", b"</x:xmpmeta>") \
        or _cut(packet, b"<x:xapmeta", b"</x:xapmeta>") \
        or _cut(packet, b"<rdf:RDF", b"</rdf:RDF>")
    if body is None:
        return PhotoMeta()
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    rating_text = None
    keywords: list[str] = []
    for el in root.iter():
        if el.tag == _DESC and _RATING in el.attrib and rating_text is None:
            rating_text = el.attrib[_RATING]
        elif el.tag == _RATING and rating_text is None:
            rating_text = el.text or ""
        elif el.tag == _SUBJECT:
            for li in el.iter(_LI):
                kw = (li.text or "").strip()
                if kw and kw not in keywords:
                    keywords.append(kw)
    rating = 0
    if rating_text is not None and rating_text.strip():
        try:
            rating = int(round(float(rating_text.strip())))
        except ValueError:
            return None
        rating = max(-1, min(5, rating))
    return PhotoMeta(rating, tuple(keywords), _parse_edits(root))


def _parse_edits(root) -> Edits | None:
    """crs: の補正を読む（属性形式・要素形式の両方）。壊れた値は無視して None 側に倒す。"""
    values: dict[str, str] = {}
    for el in root.iter():
        if el.tag == _DESC:
            for key, val in el.attrib.items():
                if key.startswith(f"{{{_NS_CRS}}}"):
                    values[key[len(_NS_CRS) + 2:]] = val
        elif el.tag.startswith(f"{{{_NS_CRS}}}"):
            name = el.tag[len(_NS_CRS) + 2:]
            if name not in values and el.text:
                values[name] = el.text
    if not values:
        return None

    def num(name: str):
        try:
            return float(values[name])
        except (KeyError, ValueError):
            return None

    crop = None
    if (values.get("HasCrop", "").strip().lower() == "true"
            or all(k in values for k in ("CropTop", "CropLeft", "CropBottom", "CropRight"))):
        parts = [num(k) for k in ("CropTop", "CropLeft", "CropBottom", "CropRight")]
        if all(v is not None for v in parts):
            crop = tuple(max(0.0, min(1.0, v)) for v in parts)
    # 色温度は相対値（TemperatureUnit=1。JPEG など非 RAW で Lightroom が使う形）の
    # ときだけ読む。絶対値（ケルビン）のものは基準が分からないので読み替えない
    temperature = 0.0
    if (values.get("TemperatureUnit", "").strip() == "1"):
        temperature = num("Temperature") or 0.0
    edits = Edits(crop=crop,
                  angle=num("CropAngle") or 0.0,
                  exposure=num("Exposure2012") or 0.0,
                  contrast=num("Contrast2012") or 0.0,
                  saturation=num("Saturation") or 0.0,
                  temperature=temperature,
                  tint=num("Tint") or 0.0)
    return None if edits.is_empty() else edits


def _cut(data: bytes, start: bytes, end: bytes) -> bytes | None:
    i = data.find(start)
    if i < 0:
        return None
    j = data.find(end, i)
    if j < 0:
        return None
    return data[i:j + len(end)]


# ------------------------------------------------------------------ 形式ごとの XMP の取り出し

def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise _Broken("途中で終わっている")
    return data


def _jpeg_xmp(f) -> bytes | None:
    """JPEG の APP1 XMP セグメントの中身。無ければ None。f は SOI の直後を指していること。"""
    while True:
        b = _read_exact(f, 1)
        if b != b"\xff":
            raise _Broken("マーカーがない")
        marker = _read_exact(f, 1)[0]
        while marker == 0xFF:              # 詰め物の FF
            marker = _read_exact(f, 1)[0]
        if marker == 0xD9 or marker == 0xDA:   # EOI / SOS: ここから先に XMP は無い
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:   # 長さを持たないマーカー
            continue
        (length,) = struct.unpack(">H", _read_exact(f, 2))
        if length < 2:
            raise _Broken("セグメント長が不正")
        if marker == 0xE1:
            payload = _read_exact(f, length - 2)
            if payload.startswith(_JPEG_XMP_SIG):
                return payload[len(_JPEG_XMP_SIG):]
        else:
            f.seek(length - 2, os.SEEK_CUR)


def _png_xmp(f) -> bytes | None:
    """PNG の iTXt XML:com.adobe.xmp の中身。f はシグネチャの直後を指していること。"""
    while True:
        head = f.read(8)
        if len(head) == 0:
            return None
        if len(head) != 8:
            raise _Broken("チャンクの途中で終わっている")
        length, ctype = struct.unpack(">I4s", head)
        if ctype == b"IEND":
            return None
        if ctype == b"iTXt" and length >= 18:
            data = _read_exact(f, length)
            f.seek(4, os.SEEK_CUR)       # CRC
            key, _, rest = data.partition(b"\x00")
            if key != b"XML:com.adobe.xmp" or len(rest) < 2:
                continue
            compressed, rest = rest[0], rest[2:]
            _lang, _, rest = rest.partition(b"\x00")
            _trans, _, text = rest.partition(b"\x00")
            if compressed:
                try:
                    text = zlib.decompress(text)
                except zlib.error:
                    raise _Broken("iTXt の展開に失敗") from None
            return text
        f.seek(length + 4, os.SEEK_CUR)


def _webp_xmp(f, end: int) -> bytes | None:
    """WebP（RIFF）の "XMP " チャンクの中身。f は "WEBP" の直後、end は RIFF の終わり。"""
    while f.tell() + 8 <= end:
        fourcc, length = struct.unpack("<4sI", _read_exact(f, 8))
        if fourcc == b"XMP ":
            return _read_exact(f, length)
        f.seek(length + (length & 1), os.SEEK_CUR)
    return None


def _scan_xmp(f, size: int) -> bytes | None:
    """先頭（と末尾）から XMP パケットを探す簡易法。"""
    f.seek(0)
    head = f.read(HEAD_BYTES)
    found = _cut(head, b"<x:xmpmeta", b"</x:xmpmeta>")
    if found is not None:
        return found
    if size > HEAD_BYTES:
        f.seek(max(HEAD_BYTES, size - TAIL_BYTES))
        tail = f.read(TAIL_BYTES)
        found = _cut(tail, b"<x:xmpmeta", b"</x:xmpmeta>")
        if found is not None:
            return found
    return None


def read_xmp_packet(path) -> bytes | None:
    """ファイルに埋め込まれた XMP パケットのバイト列。無ければ b""、読めなければ None。"""
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            sig = f.read(12)
            try:
                if sig[:2] == b"\xff\xd8":
                    f.seek(2)
                    data = _jpeg_xmp(f)
                elif sig[:8] == b"\x89PNG\r\n\x1a\n":
                    f.seek(8)
                    data = _png_xmp(f)
                elif sig[:4] == b"RIFF" and sig[8:12] == b"WEBP":
                    riff_end = 8 + struct.unpack("<I", sig[4:8])[0]
                    data = _webp_xmp(f, min(size, riff_end))
                elif size == 0:
                    return None
                else:
                    data = _scan_xmp(f, size)
            except (_Broken, struct.error):
                return None
    except OSError:
        return None
    return data if data is not None else b""


def read_meta(path) -> PhotoMeta | None:
    """写真の★とキーワード。読めないとき（開けない・壊れている）は None（＝不明）。

    RAW はサイドカー（名前.xmp）を読む。RAW 本体が無ければ None、サイドカーが無ければ★なし。
    """
    path = os.fspath(path)
    if is_raw(path):
        if not os.path.isfile(path):
            return None
        side = sidecar_path(path)
        try:
            with open(side, "rb") as f:
                data = f.read(4 * 1024 * 1024)
        except FileNotFoundError:
            return PhotoMeta()
        except OSError:
            return None
        return parse_xmp(data)
    data = read_xmp_packet(path)
    if data is None:
        return None
    if not data:
        return PhotoMeta()
    return parse_xmp(data)
