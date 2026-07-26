# capture_window.py
# キャプチャ結果を表示する「枠なし相当・常に最前面」の付箋ウインドウ。
# 描画(ペン/蛍光ペン/テキスト)、ホイールでの透明度変更、Ctrl+ホイールでのズーム、
# ダブルクリック一時非表示、右クリックメニュー、保存(Ctrl+S)/コピー(Ctrl+C)をここで扱う。
# (rapture-py/capture_window.py から移設。不具合1〜5を修正)
import os

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QWidget,
)

from capture_grab import draw_action, grab_region, render_annotated, save_image

PEN_WIDTH_CHOICES = [1, 2, 3, 5, 8, 12]
HIGHLIGHTER_WIDTH_MULTIPLIER = 4
HIGHLIGHTER_ALPHA = 90
MIN_ZOOM = 0.2
MAX_ZOOM = 5.0
ZOOM_STEP = 1.1
MIN_OPACITY = 0.2
OPACITY_STEP = 0.1


class CaptureWindow(QWidget):
    def __init__(self, image: QImage, global_pos: QPoint, capture_settings: dict,
                 settings_path=None, close_on_escape: bool = False):
        super().__init__()
        # OS標準のタイトルバーを表示するため枠なし(FramelessWindowHint)は使わない。
        # 自由リサイズを許すと画像とウインドウサイズがズレて描画が崩れるため、
        # サイズはsetFixedSizeで固定し、リサイズはCtrl+ホイールのズームに任せる。
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Rapture")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # close()時に確実にQObjectを破棄する。常駐アプリで複数ウインドウの参照を
        # リストで持つ都合上、これが無いと閉じても参照が残りメモリリークになる。
        self.setAttribute(Qt.WA_DeleteOnClose)

        self.capture_settings = capture_settings
        self.settings_path = settings_path
        # 「画面全体に書き込む」で使う全画面の付箋はタイトルバーの×が画面外に出るため、
        # Escで閉じられないと詰む。通常のキャプチャ付箋の挙動は変えたくないのでオプトインにする。
        self.close_on_escape = close_on_escape

        self.zoom_factor = 1.0
        self.always_on_top = True
        # 描画・テキストの操作履歴(アンドゥ用)。要件は直近3回程度で十分だが、
        # 実装を単純にするため件数制限はせずスタックとして扱う。
        self.actions_history = []

        self.pen_color = QColor(capture_settings.get("pen_color", "#ff0000"))
        self.pen_width = capture_settings.get("pen_width", 3)
        self.highlighter_enabled = bool(capture_settings.get("highlighter_enabled", False))

        self._dragging_window = False
        self._drag_offset = QPoint()
        self._drawing = False
        self._current_stroke = None

        # 不具合1対策: タイトルバー分の位置補正は初回表示時のみ行う。毎回やると
        # ダブルクリック非表示からの復帰・再キャプ・最前面トグルのたびにユーザーが
        # ドラッグで動かした位置が失われてしまう。
        self._aligned_once = False

        # 中身(画像)の左上をキャプチャした画面位置に一致させたい値。タイトルバー分の
        # 補正はshowEvent側で初回のみ行うため、ここでは目標位置として保持するだけにする。
        self._target_global_pos = QPoint(global_pos)

        self._set_base_image(image)
        logical_size = self.base_image.size() / (self.base_image.devicePixelRatio() or 1.0)
        self.setFixedSize(logical_size)
        self.move(global_pos)

    def _set_base_image(self, image: QImage):
        """元画像(QImage, 保存/焼き込み用の正)と、表示用QPixmapを両方更新する。"""
        self.base_image = image
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(image.devicePixelRatio() or 1.0)
        self.base_pixmap = pixmap

    def showEvent(self, event):
        super().showEvent(event)
        # move()はウインドウ枠(タイトルバー込み)の左上を指定するため、タイトルバーが
        # 付いた状態だと中身がその分だけ下にずれる。実際に表示されてOSが確定した
        # frameGeometry と geometry(中身の座標)との差分を使って位置を補正する。
        # 表示直後はOSがまだ装飾サイズを確定していないことがあるため次のイベント
        # ループで補正する。不具合1対策でこれは初回表示時のみ行う。
        if not self._aligned_once:
            self._aligned_once = True
            QTimer.singleShot(0, self._align_to_target_pos)

    def _align_to_target_pos(self):
        frame_offset = self.geometry().topLeft() - self.frameGeometry().topLeft()
        self.move(self._target_global_pos - frame_offset)

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self.zoom_factor, self.zoom_factor)
        painter.drawPixmap(0, 0, self.base_pixmap)

        for action in self.actions_history:
            draw_action(painter, action)

        # 描画中のストロークはまだactions_historyに入っていないため、ここで別途描画する。
        # これが無いとマウスボタンを離すまで線が見えないことになる。
        if self._current_stroke is not None:
            draw_action(painter, self._current_stroke)

    def _to_image_coords(self, pos) -> QPointF:
        """ウインドウローカル座標(ズーム後の見た目座標)を、元画像座標系に変換する。"""
        return QPointF(pos.x() / self.zoom_factor, pos.y() / self.zoom_factor)

    # ---------------------------------------------------------------
    # マウス操作: Ctrl+左ドラッグ=ペン描画 / 通常左ドラッグ=ウインドウ移動
    # ---------------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if event.modifiers() & Qt.ControlModifier:
                self._drawing = True
                width = self.pen_width * (
                    HIGHLIGHTER_WIDTH_MULTIPLIER if self.highlighter_enabled else 1
                )
                color = QColor(self.pen_color)
                if self.highlighter_enabled:
                    color.setAlpha(HIGHLIGHTER_ALPHA)
                self._current_stroke = {
                    "type": "stroke",
                    "color": color,
                    "width": width,
                    "points": [self._to_image_coords(event.position())],
                }
            else:
                self._dragging_window = True
                self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event):
        if self._drawing and self._current_stroke is not None:
            self._current_stroke["points"].append(self._to_image_coords(event.position()))
            self.update()
        elif self._dragging_window:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._drawing and self._current_stroke is not None:
                if len(self._current_stroke["points"]) > 1:
                    self.actions_history.append(self._current_stroke)
                self._current_stroke = None
                self._drawing = False
                self.update()
            self._dragging_window = False

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.hide()
            hide_ms = int(self.capture_settings.get("hide_duration_ms", 2000))
            QTimer.singleShot(hide_ms, self.show)

    # ---------------------------------------------------------------
    # ホイール: 通常=不透明度変更 / Ctrl+ホイール=ズーム
    # ---------------------------------------------------------------
    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if delta == 0:
            return

        if event.modifiers() & Qt.ControlModifier:
            factor = ZOOM_STEP if delta > 0 else (1 / ZOOM_STEP)
            self._apply_zoom(self.zoom_factor * factor)
        else:
            step = OPACITY_STEP if delta > 0 else -OPACITY_STEP
            new_opacity = min(1.0, max(MIN_OPACITY, self.windowOpacity() + step))
            self.setWindowOpacity(new_opacity)

    def _apply_zoom(self, new_zoom):
        new_zoom = max(MIN_ZOOM, min(new_zoom, MAX_ZOOM))
        self.zoom_factor = new_zoom
        base_size = self.base_image.size() / (self.base_image.devicePixelRatio() or 1.0)
        self.setFixedSize(int(base_size.width() * new_zoom), int(base_size.height() * new_zoom))
        self.update()

    # ---------------------------------------------------------------
    # キーボード: Ctrl+Z でアンドゥ / Ctrl+S で保存 / Ctrl+C でコピー
    # ---------------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Z and event.modifiers() & Qt.ControlModifier:
            self.undo()
        elif event.key() == Qt.Key_S and event.modifiers() & Qt.ControlModifier:
            self._save()
        elif event.key() == Qt.Key_C and event.modifiers() & Qt.ControlModifier:
            self._copy()
        elif event.key() == Qt.Key_Escape and self.close_on_escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def undo(self):
        if self.actions_history:
            self.actions_history.pop()
            self.update()

    # ---------------------------------------------------------------
    # 保存・コピー(不具合2対策): 元画像 + actions_history を焼き込んで永続化する
    # ---------------------------------------------------------------
    def _save(self):
        rendered = render_annotated(self.base_image, self.actions_history)
        save_image(rendered, self.capture_settings)

    def _copy(self):
        rendered = render_annotated(self.base_image, self.actions_history)
        QGuiApplication.clipboard().setImage(rendered)

    # ---------------------------------------------------------------
    # 右クリックメニュー
    # ---------------------------------------------------------------
    def contextMenuEvent(self, event):
        context_pos_image = self._to_image_coords(event.pos())
        menu = QMenu(self)

        act_color = menu.addAction("色...")

        width_menu = menu.addMenu("線幅")
        width_actions = {}
        for w in PEN_WIDTH_CHOICES:
            action = width_menu.addAction(f"{w}px")
            action.setCheckable(True)
            action.setChecked(self.pen_width == w)
            width_actions[action] = w

        act_highlighter = menu.addAction("蛍光ペン")
        act_highlighter.setCheckable(True)
        act_highlighter.setChecked(self.highlighter_enabled)

        act_text = menu.addAction("テキスト")
        menu.addSeparator()
        act_recapture = menu.addAction("再キャプ")
        act_save = menu.addAction("保存 (Ctrl+S)")
        act_copy = menu.addAction("コピー (Ctrl+C)")
        act_ontop = menu.addAction("常に手前に表示")
        act_ontop.setCheckable(True)
        act_ontop.setChecked(self.always_on_top)
        act_opacity_reset = menu.addAction("透明度リセット")
        menu.addSeparator()
        act_settings = menu.addAction("設定")
        act_close = menu.addAction("閉じる")

        chosen = menu.exec(event.globalPos())
        if chosen is None:
            return

        if chosen == act_color:
            self._choose_color()
        elif chosen in width_actions:
            self.pen_width = width_actions[chosen]
        elif chosen == act_highlighter:
            self.highlighter_enabled = act_highlighter.isChecked()
        elif chosen == act_text:
            self._add_text_at(context_pos_image)
        elif chosen == act_recapture:
            self._recapture()
        elif chosen == act_save:
            self._save()
        elif chosen == act_copy:
            self._copy()
        elif chosen == act_ontop:
            self._toggle_always_on_top(act_ontop.isChecked())
        elif chosen == act_opacity_reset:
            self.setWindowOpacity(1.0)
        elif chosen == act_settings:
            self._open_settings_file()
        elif chosen == act_close:
            # 常駐アプリでは「終了」はトレイメニューの役目。ここではこのウインドウだけを閉じる。
            self.close()

    def _choose_color(self):
        color = QColorDialog.getColor(self.pen_color, self, "ペンの色を選択")
        if color.isValid():
            self.pen_color = color

    def _add_text_at(self, image_pos: QPointF):
        text, ok = QInputDialog.getText(self, "テキスト入力", "テキスト:")
        if ok and text:
            self.actions_history.append({
                "type": "text",
                "pos": image_pos,
                "text": text,
                "color": QColor(self.pen_color),
            })
            self.update()

    def _recapture(self):
        """右クリック「再キャプ」: 自分自身が写り込まないよう一旦隠してから、
        現在のウインドウ位置の直下を撮り直す(不具合5対策。以前は最初の選択範囲を
        閉じ込めたクロージャを撮り直していたため、ウインドウを移動しても元の場所が
        撮られてしまっていた)。描画内容はクリアする。"""
        self.hide()
        # hide()直後はOSがまだ画面を再描画し切っていないことがあるため、
        # 少し待ってから撮り直す。
        QTimer.singleShot(150, self._perform_recapture)

    def _perform_recapture(self):
        try:
            # ズーム中でも元画像の論理サイズを基準にする
            logical_size = self.base_image.size() / (self.base_image.devicePixelRatio() or 1.0)
            # pos()はトップレベルウインドウでは「ウインドウ枠(タイトルバー込み)の左上」を
            # 返すため、そのまま使うと画像の中身(タイトルバーの下から始まる)より
            # タイトルバーの高さ分上をキャプチャしてしまう。_align_to_target_pos と同じ考え方で、
            # クライアント領域の左上であるgeometry().topLeft()を使う。
            rect_global = QRect(self.geometry().topLeft(), logical_size)
            new_image = grab_region(rect_global)
            self._set_base_image(new_image)
            self.actions_history.clear()
            self._apply_zoom(1.0)
        finally:
            self.show()

    def _toggle_always_on_top(self, enabled):
        """不具合1関連対策: setWindowFlags()はWindowsで位置がリセットされることがあるため、
        呼び出しの前後でウインドウ座標を明示的に保存・復元する。"""
        self.always_on_top = enabled
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint

        was_visible = self.isVisible()
        pos_before = self.pos()
        self.setWindowFlags(flags)
        self.move(pos_before)
        if was_visible:
            self.show()

    def _open_settings_file(self):
        """設定ダイアログUIは持たないため、settings.jsonを既定アプリ(メモ帳等)で開く。"""
        if self.settings_path and os.path.exists(self.settings_path):
            try:
                os.startfile(self.settings_path)
            except OSError:
                QMessageBox.information(self, "設定", f"設定ファイル: {self.settings_path}")
        else:
            QMessageBox.information(self, "設定", "設定ファイルが見つかりません。")
