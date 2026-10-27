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


_cached = {}


def _icon(glyph_only: bool) -> QIcon:
    key = "glyph" if glyph_only else "full"
    if key not in _cached:
        master = render(512, glyph_only)
        icon = QIcon()
        for size in (16, 24, 32, 48, 256):
            icon.addPixmap(master.scaled(
                size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        _cached[key] = icon
    return _cached[key]


def qicon() -> QIcon:
    """白地つきの QIcon(窓・タスクバー用)。"""
    return _icon(False)


def qicon_glyph() -> QIcon:
    """字だけ(白地なし)の QIcon。メニューの行用で、地の色に馴染む。"""
    return _icon(True)
