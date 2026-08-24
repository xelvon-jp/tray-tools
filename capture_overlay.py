# capture_overlay.py
# 「範囲選択オーバーレイ」(素のものと、画面を凍結してから選ばせるもの)と、
# タイマー起動時の「カウントダウン表示」を担当する。
# (rapture-py/overlay.py から移設)
from PySide6.QtCore import Qt, QRect, QPoint, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QImage, QPen, QFont, QGuiApplication
from PySide6.QtWidgets import QWidget

import window_tools
from capture_grab import device_bounds_to_logical, grab_region, virtual_geometry

# ウィンドウ単位キャプチャ(クリックでカーソル下のウィンドウ全体を選ぶ)の枠。
# ドラッグ中の選択枠 QPen(QColor(0, 200, 255), 2) と一目で区別が付くよう、
# 色も太さも変えている。
WINDOW_FRAME_COLOR = QColor(255, 176, 0)
WINDOW_FRAME_WIDTH = 4
# 枠に添えるラベルの最大文字数。長いタイトル(ブラウザのタブ名など)をそのまま出すと
# 画面を横断してしまうため、これを超えたら省略記号にまとめる。
WINDOW_LABEL_MAX_CHARS = 48


# 物理ピクセル→Qt論理座標の変換(_dpr_at_device_point / _device_bounds_to_logical)は
# capture_grab へ移した。同じ換算をタスクバーウィジェット(taskbar_widget.py)も使うため、
# 座標変換を持っている側(grab_region / virtual_geometry のある capture_grab)へ寄せた。


class SelectionOverlay(QWidget):
    """全モニタを覆う半透明ウインドウ。ドラッグで矩形を選択し、離した位置で選択確定を通知する。
    ドラッグせずにクリックした場合は、その位置にあるウィンドウ全体を選択する。"""

    # rect_global はQtの論理座標系(スクリーン全体, マルチモニタ考慮済み)でのQRect
    selection_made = Signal(QRect)
    canceled = Signal()

    # ウィンドウ単位の選択を使うかどうか。凍結する側(FrozenSelectionOverlay)だけの機能に
    # せず基底に置いているのは、素のオーバーレイでも同じ操作感にしたいのと、列挙を
    # 「オーバーレイを表示する前」に済ませる必要があり、それが基底の __init__ だから。
    # 継承先で切れるようにしてあるのは画面定規(screen_ruler.RulerOverlay)のため
    # (あちらはA→Bのドラッグを測る道具で、クリックでウィンドウを選ばれても測る物が無い)。
    window_pick_enabled = True

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 全モニタ分のQRect(論理座標)を合算し、マルチモニタでも1枚のオーバーレイでカバーする
        virtual_geometry = QRect()
        for screen in QGuiApplication.screens():
            virtual_geometry = virtual_geometry.united(screen.geometry())
        self._origin = virtual_geometry.topLeft()
        self.setGeometry(virtual_geometry)

        self._start = None
        self._current = None
        self._dragging = False
        # ドラッグ前でもカーソル座標を出すため、ドラッグ中かどうかに関わらず追う
        self._hover = None

        # 開いているウィンドウの一覧。ここで1回だけ作る。
        # このオーバーレイは全モニタを覆うので、表示してしまうと WindowFromPoint は
        # 自分自身しか返さない。表示する前(=まだ show() していないこの時点)に列挙して
        # 矩形を控えておき、以降はカーソル位置から引く。hoverのたびに列挙し直すと重い。
        self._windows = self._collect_windows() if self.window_pick_enabled else []
        # カーソルの下にあるウィンドウ。枠の描画にだけ使う(選択確定時は引き直す)。
        self._hover_window = None

    def showEvent(self, event):
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus()

    # ---------------------------------------------------------------
    # ウィンドウ単位の選択
    # ---------------------------------------------------------------
    def _collect_windows(self) -> list:
        """今開いているウィンドウを (QRect(Qt論理座標のグローバル座標), タイトル) の一覧にする。
        前面から順に並ぶので、ある座標を含む最初の1件がその位置の最前面のウィンドウ。

        自分自身のHWNDを除外して渡す。まだ表示していないので列挙されないはずだが、
        自分を撮ってしまうと何も分からない絵になるため念のため。"""
        windows = []
        for _hwnd, title, bounds in window_tools.list_windows(exclude_hwnd=int(self.winId())):
            rect_global = device_bounds_to_logical(bounds)
            if rect_global.isEmpty():
                continue
            windows.append((rect_global, title))
        return windows

    def _window_at(self, point_local):
        """ローカル座標の位置にあるウィンドウ (QRect, タイトル)。無ければ None。"""
        if point_local is None:
            return None
        point_global = self._to_global(point_local)
        for rect_global, title in self._windows:
            if rect_global.contains(point_global):
                return rect_global, title
        return None

    def _window_rect_at(self, point_local):
        """ローカル座標の位置にあるウィンドウの矩形(グローバル座標)。無ければ None。
        画面の外にはみ出している部分は撮れないので、オーバーレイの範囲で切っておく。"""
        window = self._window_at(point_local)
        if window is None:
            return None
        rect_global = window[0].intersected(QRect(self._origin, self.size()))
        return rect_global if not rect_global.isEmpty() else None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._start = event.position().toPoint()
            self._current = self._start
            self._dragging = True
            self.update()

    def mouseMoveEvent(self, event):
        self._hover = event.position().toPoint()
        if self._dragging:
            self._current = self._hover
        else:
            # 範囲を決める前は「クリックすればこのウィンドウが撮れる」を枠で示す。
            # ドラッグ中は引き直さない(枠を描くのはドラッグ前だけなので不要)。
            self._hover_window = self._window_at(self._hover)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            rect_local = QRect(self._start, self._current).normalized()

            # 小さすぎる選択(クリックだけ等)は、その位置にあるウィンドウ全体の選択として扱う。
            # ウィンドウが見つからない位置(壁紙の上など)では従来どおり無視して選択を続ける。
            if rect_local.width() < 4 or rect_local.height() < 4:
                window_rect = self._window_rect_at(self._current)
                if window_rect is not None:
                    self.selection_made.emit(window_rect)
                    return
                self._start = None
                self._current = None
                self.update()
                return

            rect_global = rect_local.translated(self._origin)
            self.selection_made.emit(rect_global)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.canceled.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._draw_background(painter)

        if self._dragging and self._start is not None and self._current is not None:
            rect_local = QRect(self._start, self._current).normalized()

            self._draw_selection_background(painter, rect_local)

            pen = QPen(QColor(0, 200, 255), 2)
            painter.setPen(pen)
            painter.drawRect(rect_local)

            self._draw_info(painter, rect_local)
        else:
            self._draw_window_frame(painter)
            self._draw_hover_info(painter)

    def _draw_background(self, painter):
        """全面の減光。情報表示と同様、サブクラス(FrozenSelectionOverlay)が
        背景の作り方だけ差し替えられるよう切り出してある。"""
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

    def _draw_selection_background(self, painter, rect_local):
        """選択範囲部分だけ塗りを消して見やすくする。"""
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(rect_local, Qt.transparent)
        painter.restore()

    def _draw_window_frame(self, painter):
        """カーソルの下にあるウィンドウを枠で囲む。クリックすればどこが撮れるのかを、
        ドラッグを始める前に見せるためのもの。"""
        if self._hover_window is None:
            return
        rect_global, title = self._hover_window
        # 画面からはみ出した部分は撮れないので、確定時(_window_rect_at)と同じくここでも切る。
        # 見えている枠と実際に撮れる範囲を一致させるため。凍結画像を描き直す側(Frozen)が
        # 画像の外を参照しないようにする意味もある。
        rect_local = rect_global.translated(-self._origin).intersected(self.rect())
        if rect_local.isEmpty():
            return

        # 枠の中だけ減光を解除する。選択範囲のときと同じ見え方にしたいので、
        # 背景の作り方はサブクラスごとの _draw_selection_background に任せる。
        self._draw_selection_background(painter, rect_local)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(WINDOW_FRAME_COLOR, WINDOW_FRAME_WIDTH))
        # 線は矩形の上に中央揃えで乗るため、そのまま描くと半分が外へはみ出す。
        # 最大化ウインドウでは画面外に切られて枠が細く見えるので、内側へ寄せる。
        painter.drawRect(rect_local.adjusted(
            WINDOW_FRAME_WIDTH // 2, WINDOW_FRAME_WIDTH // 2,
            -WINDOW_FRAME_WIDTH // 2, -WINDOW_FRAME_WIDTH // 2,
        ))

        label = title.strip() if title else ""
        if len(label) > WINDOW_LABEL_MAX_CHARS:
            label = label[:WINDOW_LABEL_MAX_CHARS - 1] + "…"
        # 名前の無いウインドウもあるので、その場合は大きさで代用する。
        if not label:
            label = f"{rect_local.width()} x {rect_local.height()}"
        painter.setPen(WINDOW_FRAME_COLOR)
        painter.setFont(QFont("Meiryo", 10))
        painter.drawText(QPoint(rect_local.x() + 4, max(rect_local.y() - 6, 12)), label)

    def _to_global(self, point: QPoint) -> QPoint:
        """オーバーレイ内のローカル座標を実際の画面座標へ直す。
        オーバーレイの左上は仮想デスクトップの左上であり、プライマリの左や上にモニタが
        あると原点が(0,0)にならない。ローカル座標のまま表示すると実際の画面座標とずれる。"""
        return point + self._origin

    def _draw_info(self, painter, rect_local):
        """選択中の情報表示。減光や選択枠の描画を共有したまま表示内容だけ差し替えられるよう、
        サブクラス(screen_ruler.RulerOverlay)のために切り出してある。"""
        origin = self._to_global(rect_local.topLeft())
        info_text = f"{rect_local.width()} x {rect_local.height()}  ({origin.x()}, {origin.y()})"
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        text_pos = QPoint(rect_local.x(), max(rect_local.y() - 6, 12))
        painter.drawText(text_pos, info_text)

    def _draw_hover_info(self, painter):
        """まだドラッグしていないときのカーソル座標表示。範囲を決める前に
        「今どこを指しているか」が分かるようにする。"""
        if self._hover is None:
            return
        point = self._to_global(self._hover)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        painter.drawText(
            QPoint(self._hover.x() + 14, max(self._hover.y() - 8, 12)),
            f"({point.x()}, {point.y()})",
        )


class FrozenSelectionOverlay(SelectionOverlay):
    """画面を凍結してから範囲を選ばせるオーバーレイ。

    開いた時点で全モニタ分を1枚のQImageとして撮り、その静止画を全画面に表示する。選択が
    確定しても撮り直さず crop() でこの画像から切り出すので、選択中に見えていた絵と保存される
    絵が必ず一致する(色を吸い取るオーバーレイと同じ発想)。カウントダウン中に開いたメニューや
    ツールチップは、選択操作で消えても撮れる。"""

    def __init__(self):
        super().__init__()
        geometry = virtual_geometry()
        self._image = grab_region(geometry)
        self._dpr = self._image.devicePixelRatio() or 1.0

    def _draw_background(self, painter):
        painter.drawImage(0, 0, self._image)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))

    def _draw_selection_background(self, painter, rect_local):
        # 素のCompositionMode_Clearで抜くと下の静止画ごと消えて穴が空く。
        # 選択範囲だけ静止画を描き直して、減光していない状態に戻す。
        painter.drawImage(rect_local, self._image, self._to_device(rect_local))

    def _to_device(self, rect_local: QRect) -> QRect:
        """オーバーレイ内のローカル座標(Qt論理座標)を、凍結画像の物理ピクセル座標へ直す。"""
        return QRect(
            int(rect_local.x() * self._dpr),
            int(rect_local.y() * self._dpr),
            int(rect_local.width() * self._dpr),
            int(rect_local.height() * self._dpr),
        )

    def crop(self, rect_global: QRect) -> QImage:
        """凍結画像から rect_global(Qt論理座標のグローバル座標)の範囲を切り出す。
        オーバーレイの左上は仮想デスクトップの左上なので、原点を引いてから物理ピクセルへ直す。"""
        rect_device = self._to_device(rect_global.translated(-self._origin))
        # 画面構成の端数などで画像からはみ出しうる。はみ出した領域は未定義になるためクランプする。
        rect_device = rect_device.intersected(self._image.rect())
        if rect_device.isEmpty():
            rect_device = QRect(0, 0, 1, 1)

        cropped = self._image.copy(rect_device)
        # 付箋ウインドウは論理サイズの算出にdevicePixelRatioを使う。設定し直さないと
        # 高DPI環境で2倍の大きさのウインドウが開いてしまう。
        cropped.setDevicePixelRatio(self._dpr)
        return cropped


class CountdownOverlay(QWidget):
    """タイマー起動時、画面隅に小さくカウントダウンを表示するウインドウ。"""

    finished = Signal()

    def __init__(self, seconds: int):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        self._remaining = seconds
        self.resize(120, 80)

        screen_geo = QGuiApplication.primaryScreen().geometry()
        self.move(screen_geo.right() - 140, screen_geo.top() + 20)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self._timer.stop()
            self.finished.emit()
        else:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(self.rect(), 12, 12)

        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 28, QFont.Bold))
        painter.drawText(self.rect(), Qt.AlignCenter, str(max(self._remaining, 0)))
