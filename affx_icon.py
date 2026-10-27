# あっふ（Affx）のアイコン。本人の指定画(青のセリフ体イタリックで「Affx」、
# 下に明朝の「あ・っ・ふ」、白地)を基準に、QPainter で文字を描いて作る。
# 比率・色は指定画を実測した値: Affx は青 (68,114,195)、幅 0.69・高さ 0.36
# (中心 y=0.29)、仮名は y=0.53..0.94 の帯に あ(左)・っ(小・中央)・ふ(右)。
# バイナリの画像ファイルは置かず、実行時に描くのでどのサイズでも滑らか。
# フォントは環境の明朝/セリフで代用する(Windows は游明朝と Times New Roman)。

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

BLUE = QColor(68, 114, 195)
BLACK = QColor(0, 0, 0)

_ROMAN_FAMILIES = ["Times New Roman", "Georgia", "Liberation Serif",
                   "Noto Serif CJK JP", "serif"]
_KANA_FAMILIES = ["Yu Mincho", "MS Mincho", "Hiragino Mincho ProN",
                  "Noto Serif CJK JP", "serif"]

# 指定画の実測に合わせた配置(キャンバス一辺に対する比率)
_ROMAN = {"size": 0.415, "cx": 0.49, "cy": 0.30}
_KANA_BIG = {"size": 0.494, "y": 0.525}    # あ・ふ の上端と字の大きさ
_KANA_BOX_W = 0.418                         # あ・ふ それぞれの幅
_TSU = {"size": 0.181, "cx": 0.52, "cy": 0.762}


def _font(families, pixel_size, italic=False):
    font = QFont()
    font.setFamilies(families)
    font.setPixelSize(max(4, round(pixel_size)))
    font.setItalic(italic)
    return font


def _draw_centered(painter, text, font, cx, cy, canvas):
    """(cx, cy) が字の中心に来るよう描く"""
    painter.setFont(font)
    metrics = painter.fontMetrics()
    box = metrics.tightBoundingRect(text)
    x = cx * canvas - (box.left() + box.width() / 2)
    y = cy * canvas - (box.top() + box.height() / 2)
    painter.drawText(round(x), round(y), text)


def render(size: int, glyph_only: bool = False) -> QPixmap:
    """size x size のアイコンを描く。glyph_only は白地なし(メニュー用)。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent if glyph_only else Qt.white)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.TextAntialiasing)

    painter.setPen(BLUE)
    _draw_centered(painter, "Affx",
                   _font(_ROMAN_FAMILIES, size * _ROMAN["size"], italic=True),
                   _ROMAN["cx"], _ROMAN["cy"], size)

    painter.setPen(BLACK)
    # ふ は右への払いが画より広く出る字があるので、少し小さく・左へ。
    for text, cx, scale in (("あ", _KANA_BOX_W / 2, 1.0),
                            ("ふ", 1 - _KANA_BOX_W / 2 - 0.013, 0.96)):
        font = _font(_KANA_FAMILIES, size * _KANA_BIG["size"] * scale)
        painter.setFont(font)
        box = painter.fontMetrics().tightBoundingRect(text)
        x = cx * size - (box.left() + box.width() / 2)
        y = _KANA_BIG["y"] * size - box.top()
        painter.drawText(round(x), round(y), text)
    _draw_centered(painter, "っ",
                   _font(_KANA_FAMILIES, size * _TSU["size"]),
                   _TSU["cx"], _TSU["cy"], size)
    painter.end()
    return pixmap


# 本人が描いた 24x24 のドット絵(2026-10-27、本家ドット絵の画風)。
# 小さい表示ではフォント描画よりこの絵の方が潰れないので、24px 以下と
# 32px はこの絵から作る。PNG そのものは置けないので base85 で埋め込む。
_ART24_B85 = (
    "iBL{Q4GJ0x0000DNk~Le0000O0000O2nGNE0N{5$_y7O^0drDELIAGL9O(c600d`2O+f$vv5yP<V"
    "FdsH010qNS#tmY4#NNd4#NS*Z>VGd00N&$L_t(YOSM$pZW2)t-"
    "WT9Cc!a(|AEI~lrWfAy&PQl4Hu2IXMB<egFK9w4m?p%g0SP2cYt<l9r3>^A%Fn_s`^Pe7W;4e<?6"
    "#<>UozS6%$YOaoH+vu{AVbOj)%s*0+!=}X?v3I{Mvv=i6i)?PN1faiQfz?DJF<}1@JH$qtVf6-"
    "Mlj3>Fe*rKmXL=y8jOXTpbIzvpMm6<r-|)Bi`;#K{H%&W-"
    "xXrE{I5hvEfpJ^4Go)V6iBB`R*J(ooU3etwBTzEG9q<CywC7(_6t2kDs5=TCUwkNTAa*DcEs680o"
    "*0p8oJFM7+wYf;;XUp1k-1Z$EUTJPb*ItE{&tw4O};R$=n@l^c=tS=xVKZXr$-"
    "emmW1Fdab>z}}x-=rFZrwAQCCT-=&q+B09c<qPu-"
    "@q4S;<iogN+Zdr1EuuTLVDIA<9X>f5!J9%SGSOXFD$v(#O6YlW@gw^i2mxK!A)C#@U@(v)mO#@qm"
    "`wcEMM1j3+S(dyZEX?n_xs>^@=Qme*Xz+aG!$Eeg3e4P1C>gJg4Wm9VRLg6R#sNv=;+Vdv9q%yL}"
    "oL8Ko|--"
    "coS`VdmB=z6x8c=Dex{_US5i`jYcDM>rjiZYHSnoLZJ}cCX7ZSX?=KjNU?W;1m^R(bc>*^uCCG};"
    "sCG@Im5Cn;m7x=R;$!*A;2w4r_=OJCX-"
    "S{@pzopwr$fR9v>fvD&||D9UL48uBs|1oEX6yhGB@q_}IM9#o!Hw!y)mFjSYHaUZb_!ZNU{LySux"
    ">$=KiDhgd8IwOUR15W&$*BoZ_brfCXav)P1Vu?XdI85Ep%-"
    "spC_6o>&ldZkiHO3ujTa*)sGiSv21R;wk&h5jlW$5}cVQCJ*LMo8eH@e5+Z0tEn%BZ2?`002ovPD"
    "HLkV1f"
)


def art24() -> QPixmap:
    """本人の 24x24 ドット絵を返す。"""
    import base64

    pixmap = QPixmap()
    pixmap.loadFromData(base64.b85decode(_ART24_B85))
    return pixmap


_cached = {}


def _icon(glyph_only: bool) -> QIcon:
    key = "glyph" if glyph_only else "full"
    if key not in _cached:
        master = render(512, glyph_only)
        icon = QIcon()
        for size in (16, 24, 32, 48, 256):
            if not glyph_only and size <= 32:
                # 小さいサイズは本人のドット絵から(フォント描画は潰れる)
                pixmap = art24().scaled(
                    size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            else:
                pixmap = master.scaled(
                    size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            icon.addPixmap(pixmap)
        _cached[key] = icon
    return _cached[key]


def qicon() -> QIcon:
    """白地つきの QIcon(窓・タスクバー用)。"""
    return _icon(False)


ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)


def ico_bytes() -> bytes:
    """ピン留め用 .ico(複数サイズ・PNG 圧縮)のバイト列を組み立てる。

    QPixmap の ICO 保存は 256px の BMP が1個だけで、小さい表示では
    潰れて汚く見える。各サイズを描画して PNG のまま格納するのが
    Windows の作法。"""
    import struct

    from PySide6.QtCore import QBuffer, QIODevice

    blobs = []
    for size in ICO_SIZES:
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        if size <= 32:
            # 小さいサイズは本人のドット絵から作る(24px はそのまま)
            art24().scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation).save(buf, "PNG")
        else:
            render(size).save(buf, "PNG")
        blobs.append(bytes(buf.data()))
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for size, blob in zip(ICO_SIZES, blobs):
        out += struct.pack("<BBBBHHII",
                           size if size < 256 else 0,
                           size if size < 256 else 0,
                           0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    return bytes(out)
