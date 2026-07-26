# qt_image.py
# PIL Image と Qt(QIcon/QPixmap)の変換ヘルパ。
# トレイアイコンの図柄はPillowで描き、表示だけQt側の型に変換して使う。
from PIL import Image
from PySide6.QtGui import QIcon, QImage, QPixmap


def pil_to_qimage(image: Image.Image) -> QImage:
    rgba = image.convert("RGBA")
    data = rgba.tobytes("raw", "RGBA")
    qimage = QImage(data, rgba.width, rgba.height, QImage.Format_RGBA8888)
    # data はこの関数のローカル変数(すぐ解放される)なので、コピーしてQImageの寿命を独立させる
    return qimage.copy()


def pil_to_qpixmap(image: Image.Image) -> QPixmap:
    return QPixmap.fromImage(pil_to_qimage(image))


def pil_to_qicon(image: Image.Image) -> QIcon:
    return QIcon(pil_to_qpixmap(image))
