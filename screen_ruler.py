# screen_ruler.py
# 画面上の距離(ピクセル数)を測る部品(PowerToys Screen Ruler相当)。トレイアイコンは持たない。
#
# 範囲選択UIは capture_overlay.SelectionOverlay を継承して作る。減光・選択枠・小さすぎる選択の
# 破棄といった作法はキャプチャと共通なので、情報表示(_draw_info)だけを差し替える。
import math

from PySide6.QtCore import Qt, QPoint, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication

from capture_overlay import SelectionOverlay

PANEL_WIDTH = 220
PANEL_LINE_HEIGHT = 22
PANEL_PADDING = 8
PANEL_GAP = 20


class RulerOverlay(SelectionOverlay):
    """測定用の範囲選択オーバーレイ。ドラッグ中は幅・高さ・始点・終点・対角線をパネル表示する。

    selection_made が渡すQRectは正規化済みでドラッグの向きが失われるため、押した点と離した点を
    グローバル座標のまま別に保持する(定規はAからBを測る道具で、右から左へ引いたなら始点は右)。"""

    # キャプチャ側の「クリックでカーソル下のウィンドウ全体を選ぶ」は使わない。定規はAからBへ
    # ドラッグした距離を測る道具で、クリックで矩形だけ渡されても始点・終点が無く測定値を
    # 作れない(何もコピーされないまま定規が閉じることになる)。
    window_pick_enabled = False

    def __init__(self):
        super().__init__()
        self._start_global = None
        self._end_global = None

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self._start is not None:
            self._start_global = self._start + self._origin
            self._end_global = self._start_global

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self._dragging and self._current is not None:
            self._end_global = self._current + self._origin

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging and self._current is not None:
            self._end_global = self._current + self._origin
        super().mouseReleaseEvent(event)
        # 小さすぎる選択は基底クラスが破棄して選択を続行する。保持中の始点・終点も捨てないと
        # 次のドラッグまで古い測定値が残ってしまう。
        if self._start is None:
            self._start_global = None
            self._end_global = None

    def _metrics(self):
        """(幅, 高さ, 対角線)。未測定ならNone。

        幅・高さは両端のピクセルを含めて数える(QRect.width()と同じ +1 する流儀)。
        単純な座標の差にすると、まったく同じドラッグでもキャプチャ側の表示と1pxずれてしまい、
        同じアプリの中で数字が食い違って見える。選択範囲をそのまま撮ったときの画像サイズとも一致する。
        対角線も、その幅・高さで張られる矩形の対角線として揃える。"""
        if self._start_global is None or self._end_global is None:
            return None
        width = abs(self._end_global.x() - self._start_global.x()) + 1
        height = abs(self._end_global.y() - self._start_global.y()) + 1
        return width, height, math.hypot(width, height)

    def measurement_lines(self) -> list:
        metrics = self._metrics()
        if metrics is None:
            return []
        width, height, diagonal = metrics
        start, end = self._start_global, self._end_global
        return [
            f"幅 x 高さ: {width} x {height}",
            f"始点: ({start.x()}, {start.y()})",
            f"終点: ({end.x()}, {end.y()})",
            f"対角線: {diagonal:.1f} px",
        ]

    def measurement_text(self):
        """クリップボード用のラベル付き複数行テキスト。未測定ならNone。"""
        lines = self.measurement_lines()
        return "\n".join(lines) if lines else None

    def summary_text(self):
        """トレイ通知用の1行要約。未測定ならNone。"""
        metrics = self._metrics()
        if metrics is None:
            return None
        width, height, diagonal = metrics
        return f"{width} x {height} / 対角線 {diagonal:.1f} px"

    def _draw_hover_info(self, painter):
        """ドラッグ前でも今どこを指しているかが分かるよう、カーソル座標をパネルで出す。
        測定中と見た目を揃えたいので、同じパネル描画を使う。"""
        if self._hover is None:
            return
        point = self._to_global(self._hover)
        self._draw_panel(painter, [f"カーソル: ({point.x()}, {point.y()})"])

    def _draw_info(self, painter, rect_local):
        self._draw_panel(painter, self.measurement_lines())

    def _draw_panel(self, painter, lines):
        if not lines:
            return

        panel = self._panel_rect(PANEL_WIDTH, PANEL_PADDING * 2 + PANEL_LINE_HEIGHT * len(lines))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 20, 20, 230))
        painter.drawRoundedRect(panel, 6, 6)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        for index, line in enumerate(lines):
            row = QRect(
                panel.x() + PANEL_PADDING,
                panel.y() + PANEL_PADDING + PANEL_LINE_HEIGHT * index,
                panel.width() - PANEL_PADDING * 2,
                PANEL_LINE_HEIGHT,
            )
            painter.drawText(row, Qt.AlignVCenter | Qt.AlignLeft, line)

    def _panel_rect(self, width: int, height: int) -> QRect:
        """情報パネルの表示位置。画面端でははみ出さないよう反対側へ寄せる。"""
        # ドラッグ前は _current が無いので、常に追っている _hover を基準にする
        anchor = self._hover or self._current or QPoint(0, 0)
        x = anchor.x() + PANEL_GAP
        y = anchor.y() + PANEL_GAP
        if x + width > self.width():
            x = anchor.x() - PANEL_GAP - width
        if y + height > self.height():
            y = anchor.y() - PANEL_GAP - height
        return QRect(max(x, 0), max(y, 0), width, height)


def create_overlay() -> RulerOverlay:
    return RulerOverlay()


def copy_measurement(overlay: RulerOverlay):
    """測定結果をクリップボードへコピーし、通知用の要約を返す。未測定ならNone。"""
    text = overlay.measurement_text()
    if text is None:
        return None
    QGuiApplication.clipboard().setText(text)
    return overlay.summary_text()
