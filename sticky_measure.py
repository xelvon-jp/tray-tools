# sticky_measure.py
# 付箋(capture_window.CaptureWindow)の上で使うスポイトと定規。トレイアイコンは持たない部品。
#
# color_picker / screen_ruler は「画面全体を撮って覆う」作りだが、付箋ではすでに手元にある
# base_image の上で測るほうが自然なので、こちらは画像の画素だけを相手にする。
# 結果は actions_history の1件(type が "color" / "ruler")として残し、描画は draw_action から
# ここへ回ってくる。だから Ctrl+Z で消え、保存・コピーにも(選べば)そのまま焼き込まれる。
#
# 座標の約束: actions_history の座標は「base_image の論理座標」(画素 ÷ devicePixelRatio)。
# 表示しているズーム倍率はここには入らない。色・距離はいつも元画像の画素で出す。
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainterPath, QPen

MEASURE_TYPES = ("color", "ruler")

LABEL_FONT_PX = 12
LABEL_PADDING = 4
# 印と札の間を空け、引出線でつなぐ。札が画像の端で押し戻されたり、札同士が重なったり
# しても、どの印の札かを線で辿れるようにする(◯と札の対応が分かりにくいという声から)。
LABEL_GAP = 22
LEADER_DOT_RADIUS = 2
LABEL_BG = QColor(20, 20, 20, 225)
LABEL_TEXT = QColor(255, 255, 255)
SWATCH_PX = 12
MARKER_RADIUS = 5
RULER_COLOR = QColor(255, 0, 170)
TICK_HALF = 5


def is_measurement(action: dict) -> bool:
    return action.get("type") in MEASURE_TYPES


def snap_to_pixel(logical: QPointF, dpr: float, width_px: int, height_px: int):
    """論理座標を、それが乗っている元画像の画素へ寄せる。

    返すのは (画素 x, 画素 y, その画素の中心の論理座標)。印を画素の真ん中に置くので、
    ズームしても「どの画素を測ったか」が見た目とずれない。画像の外は端の画素に留める。"""
    dpr = dpr or 1.0
    px = max(0, min(int(math.floor(logical.x() * dpr)), width_px - 1))
    py = max(0, min(int(math.floor(logical.y() * dpr)), height_px - 1))
    return px, py, QPointF((px + 0.5) / dpr, (py + 0.5) / dpr)


def color_text(color: QColor) -> str:
    return f"{color.name().upper()}  RGB({color.red()},{color.green()},{color.blue()})"


def ruler_metrics(start_px, end_px):
    """(幅, 高さ, 対角線)。幅・高さは両端の画素を含めて数える。

    screen_ruler._metrics と同じ流儀(+1)。表記も summary_text に揃える。同じアプリの中で、同じ区間を測った数字が
    画面定規と付箋とで1pxずれて見えないようにする。"""
    width = abs(end_px[0] - start_px[0]) + 1
    height = abs(end_px[1] - start_px[1]) + 1
    return width, height, math.hypot(width, height)


def ruler_text(metrics) -> str:
    width, height, diagonal = metrics
    # 水平・垂直に引いたときは、もう片方の「1」は読む人にとって雑音なので出さない。
    if height == 1:
        return f"{width} px"
    if width == 1:
        return f"{height} px"
    return f"{width} × {height} / 対角線 {diagonal:.1f} px"


def constrain_axis(start_px, end_px):
    """Shift を押しながら引いたとき用。動きの大きい方の軸だけを残す(水平か垂直に揃える)。"""
    dx = end_px[0] - start_px[0]
    dy = end_px[1] - start_px[1]
    if abs(dx) >= abs(dy):
        return end_px[0], start_px[1]
    return start_px[0], end_px[1]


def _label_font() -> QFont:
    font = QFont("Meiryo")
    # ポイントではなくピクセルで指定する。焼き込み先の QImage と画面とで dpi の解釈が
    # 違っても、論理座標で同じ大きさに描かれる。
    font.setPixelSize(LABEL_FONT_PX)
    return font


def _leader_end(rect: QRectF, anchor: QPointF) -> QPointF:
    """札の縁のうち anchor にいちばん近い点。引出線はここで札に届く。"""
    return QPointF(min(max(anchor.x(), rect.left()), rect.right()),
                   min(max(anchor.y(), rect.top()), rect.bottom()))


def _draw_leader(painter, anchor: QPointF, rect: QRectF, start_offset: float):
    """anchor(印の中心)から札までの引出線。印の輪の外側から引き始める。

    地の色を選ばないよう、印と同じく黒の縁に白の芯を重ねる。札が印に重なって
    しまう(画像が札より小さい)ときは線を引かない。"""
    end = _leader_end(rect, anchor)
    dx, dy = end.x() - anchor.x(), end.y() - anchor.y()
    length = math.hypot(dx, dy)
    if length <= start_offset + 1:
        return
    start = QPointF(anchor.x() + dx / length * start_offset,
                    anchor.y() + dy / length * start_offset)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 200), 3, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(start, end)
    painter.setPen(QPen(QColor(255, 255, 255), 1.2, Qt.SolidLine, Qt.RoundCap))
    painter.drawLine(start, end)


def _draw_label(painter, anchor: QPointF, text: str, bounds: QRectF, swatch: QColor = None,
                leader_offset: float = 0.0):
    """anchor の右下に札を出し、anchor から引出線を引く。画像の外へはみ出すときは
    反対側へ寄せる。leader_offset は引出線を anchor からどれだけ離して始めるか(印の半径)。"""
    painter.setFont(_label_font())
    metrics = painter.fontMetrics()
    swatch_w = (SWATCH_PX + LABEL_PADDING) if swatch is not None else 0
    width = metrics.horizontalAdvance(text) + LABEL_PADDING * 2 + swatch_w
    height = metrics.height() + LABEL_PADDING * 2

    x = anchor.x() + LABEL_GAP
    y = anchor.y() + LABEL_GAP
    if bounds is not None:
        if x + width > bounds.right():
            x = anchor.x() - LABEL_GAP - width
        if y + height > bounds.bottom():
            y = anchor.y() - LABEL_GAP - height
        x = max(bounds.left(), x)
        y = max(bounds.top(), y)
    rect = QRectF(x, y, width, height)

    # 線を先に引き、札で線の端を覆う(札の角に線が刺さって見えないように)。
    _draw_leader(painter, anchor, rect, leader_offset)
    painter.setPen(Qt.NoPen)
    painter.setBrush(LABEL_BG)
    painter.drawRoundedRect(rect, 4, 4)
    text_rect = rect.adjusted(LABEL_PADDING + swatch_w, 0, -LABEL_PADDING, 0)
    if swatch is not None:
        swatch_rect = QRectF(rect.x() + LABEL_PADDING,
                             rect.center().y() - SWATCH_PX / 2, SWATCH_PX, SWATCH_PX)
        painter.setBrush(swatch)
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        painter.drawRect(swatch_rect)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(LABEL_TEXT)
    painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)


def _draw_color(painter, action: dict):
    pos = action["pos"]
    # どんな地の色の上でも見えるよう、黒の縁に白の輪を重ねる。
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(0, 0, 0, 200), 3))
    painter.drawEllipse(pos, MARKER_RADIUS, MARKER_RADIUS)
    painter.setPen(QPen(QColor(255, 255, 255), 1.5))
    painter.drawEllipse(pos, MARKER_RADIUS, MARKER_RADIUS)
    _draw_label(painter, pos, color_text(action["color"]), action.get("bounds"),
                swatch=action["color"], leader_offset=MARKER_RADIUS + 1)


def _draw_ruler(painter, action: dict):
    start, end = action["start"], action["end"]
    path = QPainterPath()
    path.moveTo(start)
    path.lineTo(end)
    # 両端に、線と直交する短い目盛りを付ける(どこからどこまでかを見せる)。
    dx, dy = end.x() - start.x(), end.y() - start.y()
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * TICK_HALF, dx / length * TICK_HALF
    for point in (start, end):
        path.moveTo(point.x() - nx, point.y() - ny)
        path.lineTo(point.x() + nx, point.y() + ny)

    painter.setBrush(Qt.NoBrush)
    painter.strokePath(path, QPen(QColor(255, 255, 255, 220), 3.5, Qt.SolidLine, Qt.RoundCap))
    painter.strokePath(path, QPen(RULER_COLOR, 1.5, Qt.SolidLine, Qt.RoundCap))
    middle = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
    _draw_label(painter, middle, ruler_text(action["metrics"]), action.get("bounds"))
    # 引出線の根元(線の中点)に小さな点を打ち、どの線の札かを示す。
    painter.setPen(QPen(QColor(255, 255, 255), 1))
    painter.setBrush(RULER_COLOR)
    painter.drawEllipse(middle, LEADER_DOT_RADIUS + 0.5, LEADER_DOT_RADIUS + 0.5)


def draw_measurement(painter, action: dict) -> None:
    """actions_history の測定1件を描く(capture_grab.draw_action から呼ばれる)。"""
    painter.save()
    try:
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        if action["type"] == "color":
            _draw_color(painter, action)
        elif action["type"] == "ruler":
            _draw_ruler(painter, action)
    finally:
        painter.restore()
