# capture_grab.py
# 画面取得・注釈込みレンダリング・保存を担当する。
# mssで取得したバイト列から直接QImageを作るため、PIL経由の変換は行わない
# (Pillowはトレイアイコン描画専用に残す。保存もQImage.save()に統一する)。
import datetime
import sys
from pathlib import Path

import mss
from PySide6.QtCore import Qt, QPoint, QRect
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


def dpr_at_device_point(x: int, y: int) -> float:
    """物理ピクセルのその位置にある画面の devicePixelRatio。

    Win32が返すのは物理ピクセル、Qtが使うのは論理座標。どの画面の比率で割るかは、
    grab_region が「選択範囲の中心にある画面で判定する」のと同じ考え方で決める。
    ただし screenAt() は論理座標を要求するので、まずプライマリの比率で仮に割って
    当たりを付ける。全画面が同じ倍率(通常の環境)ならこれで正しい画面に行き着き、
    倍率が混在していても grab_region と同じ精度に収まる。"""
    primary = QGuiApplication.primaryScreen()
    base = (primary.devicePixelRatio() if primary else 1.0) or 1.0
    screen = QGuiApplication.screenAt(QPoint(int(x / base), int(y / base))) or primary
    return (screen.devicePixelRatio() if screen else 1.0) or 1.0


def device_bounds_to_logical(bounds) -> QRect:
    """Win32の (left, top, right, bottom)(物理ピクセル)をQtの論理座標のQRectへ直す。

    Win32のRECTは right/bottom が排他なので、幅は right - left。
    QRect(QPoint(left, top), QPoint(right, bottom)) で作ると縦横が1pxずつ大きくなる。"""
    left, top, right, bottom = bounds
    dpr = dpr_at_device_point((left + right) // 2, (top + bottom) // 2)
    return QRect(
        int(left / dpr),
        int(top / dpr),
        int((right - left) / dpr),
        int((bottom - top) / dpr),
    )


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
    連番が無ければ片方が黙って上書きされてしまう。
    連番セッション(-001形式)でも、同じ秒に別の付箋を開いてしまった場合の保険として残す。"""
    path = save_folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = save_folder / f"{stem}_{n}{suffix}"
        n += 1
    return path


def new_session_stem() -> str:
    """連番セッション(1枚の付箋 = 1セッション)のファイル名の頭を作る。
    付箋を作った時刻で固定し、以降は連番だけを増やして
    rapture_20260823_140919-001.png のように一連の作業を名前でまとめる。"""
    return datetime.datetime.now().strftime("rapture_%Y%m%d_%H%M%S")


def save_image(image: QImage, capture_settings: dict, stem: str = None, index: int = None):
    """imageを設定フォルダへ保存する。戻り値は保存先Pathか、失敗時はNone。

    stem/index を渡すと連番セッションのファイル名(stem-001)になる。省略した場合は
    従来どおり「今の時刻」から単発のファイル名を作る(引数2つの既存呼び出し互換)。
    JPEGはアルファチャンネルを持てないため保存前にRGBへ変換する。"""
    save_folder = Path(capture_settings.get("save_folder", r"C:\bak\rapture"))
    save_folder.mkdir(parents=True, exist_ok=True)

    fmt = capture_settings.get("save_format", "png").lower()
    name = stem or new_session_stem()
    if index is not None:
        name = f"{name}-{int(index):03d}"
    path = _unique_path(save_folder, name, f".{fmt}")

    if fmt in ("jpg", "jpeg"):
        out_image = image.convertToFormat(QImage.Format_RGB32)
        ok = out_image.save(str(path), "JPG", int(capture_settings.get("jpeg_quality", 90)))
    else:
        ok = image.save(str(path), "PNG")

    if not ok:
        print(f"[tray-tools] 保存に失敗しました: {path}", file=sys.stderr)
        return None

    return path
