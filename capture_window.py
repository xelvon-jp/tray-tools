# capture_window.py
# キャプチャ結果を表示する「枠なし相当・常に最前面」の付箋ウインドウ。
# 描画(ペン/蛍光ペン/テキスト)、ホイールでの透明度変更、Ctrl+ホイールでのズーム、
# ダブルクリック一時非表示、右クリックメニュー、保存(Ctrl+S)/コピー(Ctrl+C)をここで扱う。
# (rapture-py/capture_window.py から移設。不具合1〜5を修正)
import os

from PySide6.QtCore import Qt, QPoint, QPointF, QRect, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QInputDialog,
    QMenu,
    QMessageBox,
    QWidget,
)

from capture_grab import (
    draw_action,
    grab_region,
    new_session_stem,
    render_annotated,
    save_image,
)
from toast import dismiss, show_toast

PEN_WIDTH_CHOICES = [1, 2, 3, 5, 8, 12]
HIGHLIGHTER_WIDTH_MULTIPLIER = 4
HIGHLIGHTER_ALPHA = 90
MIN_ZOOM = 0.2
MAX_ZOOM = 5.0
ZOOM_STEP = 1.1
MIN_OPACITY = 0.2
OPACITY_STEP = 0.1
# 自分自身が写り込まないよう隠してから撮るまでの待ち。OSの再描画待ち。
RECAPTURE_DELAY_MS = 150
# 「何枚目か」バッジ。キャプチャ内容を隠しすぎないよう控えめな大きさ・不透明度にする。
BADGE_FONT = QFont("Meiryo", 9)
BADGE_MARGIN = 4
BADGE_PADDING_X = 6
BADGE_PADDING_Y = 1
BADGE_RADIUS = 5
BADGE_BG_COLOR = QColor(20, 20, 20, 160)
BADGE_TEXT_COLOR = QColor(255, 255, 255, 225)


class CaptureWindow(QWidget):
    def __init__(self, image: QImage, global_pos: QPoint, capture_settings: dict,
                 settings_path=None, close_on_escape: bool = False,
                 session_stem: str = None, session_index: int = 0,
                 capture_hotkey: str = None):
        super().__init__()
        # OS標準のタイトルバーを表示するため枠なし(FramelessWindowHint)は使わない。
        # 自由リサイズを許すと画像とウインドウサイズがズレて描画が崩れるため、
        # サイズはsetFixedSizeで固定し、リサイズはCtrl+ホイールのズームに任せる。
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        # タイトルバーを操作の手引きにする。連番キャプチャは付箋を触らずに使うものなので、
        # 「次の1枚をどのキーで撮るのか」を常に目に入る場所へ置いておきたい。
        # キーは設定で変えられるため、実際の値を受け取って出す(ハードコードしない)。
        self.setWindowTitle(
            f"Rapture － {capture_hotkey} で次の1枚" if capture_hotkey else "Rapture"
        )
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

        # 連番セッション: この付箋1枚が1つのセッション。タイムスタンプは付箋を作った時刻で
        # 固定し、撮るたびに連番だけを増やす(rapture_20260823_140919-001.png)。
        # 手順書のように同じ範囲を何度も撮るとき、一連の作業がファイル名でまとまる。
        # session_index は「このセッションで何枚保存済みか」。撮影直後の自動保存を1枚目として
        # 呼び出し側から渡されるため、通常は1から始まる(単独で作られた場合は0)。
        self.session_stem = session_stem or new_session_stem()
        self.session_index = session_index
        # 直前に「保存した」画像。次のキャプチャと突き合わせ、変化が無ければ保存しない。
        # QImageは暗黙的共有なので参照のまま持つと後の処理で中身が変わり得る。必ずcopy()で持つ。
        self._last_saved_image = image.copy() if session_index > 0 else None

        self.zoom_factor = 1.0
        self.always_on_top = True
        # 描画・テキストの操作履歴(アンドゥ用)。要件は直近3回程度で十分だが、
        # 実装を単純にするため件数制限はせずスタックとして扱う。
        self.actions_history = []

        self.pen_color = QColor(capture_settings.get("pen_color", "#ff0000"))
        self.pen_width = capture_settings.get("pen_width", 3)
        self.highlighter_enabled = bool(capture_settings.get("highlighter_enabled", False))

        self._drawing = False
        self._current_stroke = None
        # キャプチャ＆保存の実行中フラグ。hide() → 待つ → 撮る、の非同期処理なので、
        # 待っている間に(連打やホットキー経由で)もう一度入られると hide()/show() の
        # 順序が乱れ、同じ絵を二重に保存してしまう。ScreenFeature.start_capture() が
        # self.overlay で二重起動を弾いているのと同じ作法で弾く。
        self._capturing = False

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

        # バッジはウィジェットへの直接描画で済ませる。保存/コピーが使う render_annotated() は
        # base_image と actions_history からしか絵を作らないため、これで保存画像には混ざらない。
        painter.resetTransform()
        self._draw_session_badge(painter)

    def _draw_session_badge(self, painter: QPainter):
        """左上の隅に「このセッションで何枚目か」を小さく出す。撮ったつもりで撮れていない、
        逆に連打で増えすぎている、といったズレにその場で気付けるようにするためのもの。
        ウィジェット座標で描くのでCtrl+ホイールのズームでは大きさが変わらない。"""
        if self.session_index <= 0:
            return  # まだ1枚も保存していないセッションでは出さない

        painter.setFont(BADGE_FONT)
        text = str(self.session_index)
        metrics = painter.fontMetrics()
        rect = QRect(
            BADGE_MARGIN,
            BADGE_MARGIN,
            metrics.horizontalAdvance(text) + BADGE_PADDING_X * 2,
            metrics.height() + BADGE_PADDING_Y * 2,
        )

        painter.setPen(Qt.NoPen)
        painter.setBrush(BADGE_BG_COLOR)
        painter.drawRoundedRect(rect, BADGE_RADIUS, BADGE_RADIUS)
        painter.setPen(BADGE_TEXT_COLOR)
        painter.drawText(rect, Qt.AlignCenter, text)

    def _to_image_coords(self, pos) -> QPointF:
        """ウインドウローカル座標(ズーム後の見た目座標)を、元画像座標系に変換する。"""
        return QPointF(pos.x() / self.zoom_factor, pos.y() / self.zoom_factor)

    # ---------------------------------------------------------------
    # マウス操作: Ctrl+左ドラッグ=ペン描画 / 移動はタイトルバーのドラッグ(OS任せ)
    # ---------------------------------------------------------------
    def mousePressEvent(self, event):
        # 画像部分の左ドラッグでウインドウを動かす機能は廃止した。描き込もうとして
        # (Ctrlを押し損ねて)付箋ごと動かしてしまう事故が多かったため。この付箋は
        # FramelessWindowHint を使っておらずOS標準のタイトルバーがあるので、移動手段は
        # そちらのドラッグとして残っている。
        if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
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

    def mouseMoveEvent(self, event):
        if self._drawing and self._current_stroke is not None:
            self._current_stroke["points"].append(self._to_image_coords(event.position()))
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._drawing and self._current_stroke is not None:
                if len(self._current_stroke["points"]) > 1:
                    self.actions_history.append(self._current_stroke)
                self._current_stroke = None
                self._drawing = False
                self.update()

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
        # 修飾キーは & ではなく == で見る。& だと「Ctrlが含まれていれば真」なので、
        # 連番キャプチャのホットキー(既定 Ctrl+Alt+S)を付箋にフォーカスがある状態で
        # 押したときに Ctrl+S とも解釈され、同じ絵が単発の名前と連番の名前の両方で
        # 保存されていた。
        ctrl_only = event.modifiers() == Qt.ControlModifier
        if event.key() == Qt.Key_Z and ctrl_only:
            self.undo()
        elif event.key() == Qt.Key_S and ctrl_only:
            self._save()
        elif event.key() == Qt.Key_C and ctrl_only:
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

    def capture_and_save(self):
        """同じ場所を撮り直し、変化があれば連番セッションの次の1枚として保存する。

        手順書を作るときの「操作する → 撮る → 操作する → 撮る」を1キーで回すための入口。
        右クリックメニューと、グローバルホットキー(ScreenFeature.capture_sequence)の
        2つの入口から呼ばれる共通の実処理。付箋を触らずに撮れるよう、キー入力は
        ホットキー側だけに置いている(付箋にキーを割り当てると、文字を打つつもりの
        操作で意図せず撮ってしまう)。
        自分自身が写り込まないよう、_recapture() と同じく一旦隠してから撮る。"""
        if self._capturing:
            return  # 撮っている最中。連打で二重に走らせない
        # フラグは何かする前に立てる。dismiss() や hide() の途中でイベントループが
        # 回って再入しうるため、後回しにすると二重に走る隙ができる。
        self._capturing = True
        # 直前のトーストが画面隅に残っていると、撮影範囲がそこに掛かる場合に
        # 「変化なし」の判定が狂う。撮る前に消しておく。
        dismiss()
        self.hide()
        QTimer.singleShot(RECAPTURE_DELAY_MS, self._perform_capture_and_save)

    def _perform_capture_and_save(self):
        try:
            new_image = self._grab_self_region()
            # 画面が変わっていないのに連写しても同じ絵が溜まるだけ。QImageの==は内容比較
            # (サイズ違いは即False)なので、これで直前に保存した1枚と突き合わせる。
            # 押したのに無反応だと壊れたように見えるため、見送ったことは必ず知らせる。
            if self._last_saved_image is not None and new_image == self._last_saved_image:
                show_toast("Rapture\n変化がないので保存しませんでした")
                return  # 表示も差し替えない(同じ絵なので描き込みを捨てるだけ損になる)

            path = save_image(
                new_image,
                self.capture_settings,
                stem=self.session_stem,
                index=self.session_index + 1,
            )
            if path is None:
                show_toast("Rapture\n保存に失敗しました")
                return

            self.session_index += 1
            # 撮れたことを必ず知らせる。バッジの数字も増えるが、操作対象のアプリを
            # 見ている最中は付箋の隅まで目が行かず、撮り逃しに気付けないため。
            show_toast(f"Rapture\n{self.session_index}枚目を保存しました\n{path.name}")
            # 比較用の控えは必ずディープコピーで持つ(QImageは暗黙的共有)。
            self._last_saved_image = new_image.copy()
            self._set_base_image(new_image)
            self.actions_history.clear()
            self._apply_zoom(1.0)
        except Exception as e:
            # Qtのスロット内で例外を投げ切ると常駐アプリごと落ちる。保存はOSError、
            # 画面取得はmss側の独自例外を投げ得るので、まとめて受けて通知に回す。
            show_toast(f"Rapture\nキャプチャ＆保存に失敗しました\n{e}")
        finally:
            # フラグを戻し損ねると以後キャプチャ＆保存が一切効かなくなるので、必ずここで戻す。
            self._capturing = False
            # 隠して待っている150msの間に付箋が閉じられると、WA_DeleteOnClose で
            # C++側の実体が消えており show() が RuntimeError になる。finally の中で
            # 投げ切るとどこにも捕まらず常駐アプリごと落ちるので、ここで受け止める。
            try:
                self.show()
            except RuntimeError:
                pass

    def _open_save_folder(self):
        """保存先フォルダをエクスプローラで開く。

        保存しても「どこへ入ったか」がすぐには分からないため。まだ1枚も保存していない
        段階でも開けるよう、フォルダが無ければ作ってから開く。"""
        folder = (self.capture_settings or {}).get("save_folder") or ""
        if not folder:
            show_toast("Rapture\n保存先が設定されていません")
            return
        try:
            os.makedirs(folder, exist_ok=True)
            os.startfile(folder)
        except OSError as e:
            # Qtのスロット内で例外を投げ切ると常駐アプリごと落ちるので必ず受ける。
            show_toast(f"Rapture\n保存先を開けませんでした\n{folder}\n{e}")

    def _copy(self):
        rendered = render_annotated(self.base_image, self.actions_history)
        QGuiApplication.clipboard().setImage(rendered)

    # ---------------------------------------------------------------
    # 右クリックメニュー
    # ---------------------------------------------------------------
    def contextMenuEvent(self, event):
        context_pos_image = self._to_image_coords(event.pos())
        menu = QMenu(self)

        # 「コピー」はこのメニューで最も使う操作なので、カーソルの真下に来る先頭に置く。
        # 直後にセパレータを入れて、描画設定(色・線幅…)の並びとは別物だと分かるようにする。
        act_copy = menu.addAction("コピー (Ctrl+C)")
        menu.addSeparator()

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
        act_capture_save = menu.addAction("キャプチャ＆保存")
        act_save = menu.addAction("保存 (Ctrl+S)")
        act_open_folder = menu.addAction("保存フォルダを開く")
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
        elif chosen == act_capture_save:
            self.capture_and_save()
        elif chosen == act_save:
            self._save()
        elif chosen == act_copy:
            self._copy()
        elif chosen == act_open_folder:
            self._open_save_folder()
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
        QTimer.singleShot(RECAPTURE_DELAY_MS, self._perform_recapture)

    def _grab_self_region(self) -> QImage:
        """今この付箋が乗っている位置・大きさの範囲を撮る。再キャプとキャプチャ＆保存の共通処理。"""
        # ズーム中でも元画像の論理サイズを基準にする
        logical_size = self.base_image.size() / (self.base_image.devicePixelRatio() or 1.0)
        # pos()はトップレベルウインドウでは「ウインドウ枠(タイトルバー込み)の左上」を
        # 返すため、そのまま使うと画像の中身(タイトルバーの下から始まる)より
        # タイトルバーの高さ分上をキャプチャしてしまう。_align_to_target_pos と同じ考え方で、
        # クライアント領域の左上であるgeometry().topLeft()を使う。
        return grab_region(QRect(self.geometry().topLeft(), logical_size))

    def _perform_recapture(self):
        try:
            new_image = self._grab_self_region()
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
