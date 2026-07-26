# capture_grab.py
# 画面取得・注釈込みレンダリング・保存を担当する。
# mssで取得したバイト列から直接QImageを作るため、PIL経由の変換は行わない
# (Pillowはトレイアイコン描画専用に残す。保存もQImage.save()に統一する)。
import datetime
import sys
from pathlib import Path

import mss
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen


def grab_region(rect_global: QRect) -> QImage:
    """rect_global: Qt論理座標系(スクリーン全体)でのQRect。
    選択範囲の中心にある画面の devicePixelRatio を使って物理ピクセル座標に変換し、mssでキャプチャする。
    (マルチモニタでスケーリング率が異なる場合のズレを抑えるため、primaryScreen()固定ではなく
    screenAt()でそのつど対象画面を判定する。screenAtがNoneを返す場合はprimaryScreenにフォールバックする)
    """
    screen = QGuiApplication.screenAt(rect_global.center()) or QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() or 1.0

    region = {
        "left": int(rect_global.x() * dpr),
        "top": int(rect_global.y() * dpr),
        "width": int(rect_global.width() * dpr),
        "height": int(rect_global.height() * dpr),
    }

    with mss.MSS() as sct:
        shot = sct.grab(region)

    qimage = QImage(shot.bgra, shot.width, shot.height, shot.width * 4, QImage.Format_ARGB32)
    qimage = qimage.copy()  # shot.bgraのバッファはwithブロックを抜けると無効になるためコピーする
    qimage.setDevicePixelRatio(dpr)
    return qimage


def virtual_geometry() -> QRect:
    """全モニタを合算したQRect(Qt論理座標)。全画面オーバーレイや全画面キャプチャの基準にする。"""
    geometry = QRect()
    for screen in QGuiApplication.screens():
        geometry = geometry.united(screen.geometry())
    return geometry


def draw_stroke(painter: QPainter, stroke: dict) -> None:
    """半透明色の蛍光ペンが自分自身との重なりで濃く見えないよう、線分ごとのdrawLineではなく
    1本のQPainterPathにまとめてstrokePathする(重ね塗りを避けつつ描画負荷も下げる)。"""
    points = stroke["points"]
    if len(points) < 2:
        return

    pen = QPen(stroke["color"])
    pen.setWidthF(stroke["width"])
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)

    path = QPainterPath()
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)

    painter.strokePath(path, pen)


def draw_action(painter: QPainter, action: dict) -> None:
    """actions_history の1件(ストローク or テキスト)を描画する。
    付箋ウインドウのライブ描画と、保存/コピー時の焼き込みレンダリングの両方から使う共通処理。"""
    if action["type"] == "stroke":
        draw_stroke(painter, action)
    elif action["type"] == "text":
        painter.setPen(action["color"])
        painter.setFont(QFont("Meiryo", 14))
        painter.drawText(action["pos"], action["text"])


def render_annotated(base_image: QImage, actions_history: list) -> QImage:
    """元画像 + actions_history(線・テキスト)を1枚に焼き込んだQImageを返す。
    ズーム倍率は無視し、元画像の解像度で描く。座標系はactions_history側がすでに
    「pixmap論理サイズ(devicePixelRatio勘案後)」基準なので、base_imageにdevicePixelRatioが
    設定済みであればQPainterが自動でそれに合わせてスケーリングする(手動のscale補正は不要)。

    QImage(base_image)は暗黙的共有によりバッファをbase_imageと共有するだけの浅いコピーで、
    デタッチされるかはQPainter側の実装依存になってしまう。base_image.copy()で明示的に
    ディープコピーし、元画像に注釈が焼き付いてアンドゥや繰り返し保存で劣化するのを防ぐ。"""
    result = base_image.copy()
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)

    for action in actions_history:
        draw_action(painter, action)

    painter.end()
    return result


def _unique_path(save_folder: Path, stem: str, suffix: str) -> Path:
    """save_folder/stem+suffix が既に存在する場合、_2, _3... と連番を付けて衝突を避ける。
    ファイル名は秒単位のため、自動保存直後のCtrl+Sなど同じ秒に2回保存すると
    連番が無ければ片方が黙って上書きされてしまう。"""
    path = save_folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = save_folder / f"{stem}_{n}{suffix}"
        n += 1
    return path


def save_image(image: QImage, capture_settings: dict):
    """imageを設定フォルダへ日時ベースのファイル名で保存する。戻り値は保存先Pathか、失敗時はNone。
    JPEGはアルファチャンネルを持てないため保存前にRGBへ変換する。"""
    save_folder = Path(capture_settings.get("save_folder", r"C:\bak\rapture"))
    save_folder.mkdir(parents=True, exist_ok=True)

    fmt = capture_settings.get("save_format", "png").lower()
    stem = datetime.datetime.now().strftime("rapture_%Y%m%d_%H%M%S")
    path = _unique_path(save_folder, stem, f".{fmt}")

    if fmt in ("jpg", "jpeg"):
        out_image = image.convertToFormat(QImage.Format_RGB32)
        ok = out_image.save(str(path), "JPG", int(capture_settings.get("jpeg_quality", 90)))
    else:
        ok = image.save(str(path), "PNG")

    if not ok:
        print(f"[tray-tools] 保存に失敗しました: {path}", file=sys.stderr)
        return None

    return path
