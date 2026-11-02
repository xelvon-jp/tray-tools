# filer_rating_bar.py
# あっふの写真フォルダモードで、左右の窓それぞれの一覧の真下に出す「★の範囲」の段。
#
#   ★ [範囲スライダー] ベストショット以上   表示 12 / 340枚  [✕]
#
# - 範囲スライダーはつまみ2つ・7段(-1〜5)。目盛りは photometa.config.rating_short
#   (❌ / 未 / 1 / 2 / ベスト / フレーム / みてね)。値は整数にスナップし、下限 <= 上限を保つ
# - キーワードのチップ: 共通設定のキーワードごとにトグルのボタンを並べ、押したものが
#   付いている写真だけに絞る(複数は AND)。状態(どのキーワードで絞っているか)は窓(Pane)
#   が持ち、この段は表示とマウス操作だけ
# - あっふで★で絞り込む入口はこの段だけ。状態(★の範囲)はその窓(Pane)が持つ。この段は
#   表示とマウス操作だけで、式への変換と一覧の絞り込みは filer.py(Pane)が行う
# - 窓の幅は左右の境界で決まるので、段は窓を押し広げない。入りきらないときは
#   範囲の名前 → 枚数 の順に隠す(★・スライダー・✕ は残す)
# - どの部品もフォーカスを取らない(ファイル窓のキー操作を奪わない)
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QPushButton, QSizePolicy, QWidget

from photometa import config as photo_config

RATING_MIN, RATING_MAX = photo_config.RATING_MIN, photo_config.RATING_MAX


class RatingRangeSlider(QWidget):
    """★の範囲を選ぶつまみ2つのスライダー(Qt に標準部品が無いので自作)。

    - マウスで近い方のつまみをドラッグ(クリックした所へ飛ぶ)。重なっているときは
      動かした向きで下限か上限かを決める
    - 押したら pressed(窓をアクティブにするため)、値が変わったときだけ range_changed、
      ボタンを離したら released
    """

    pressed = Signal()
    range_changed = Signal(int, int)
    released = Signal()

    _KNOB_R = 5

    def __init__(self, colors: dict, font, parent=None):
        super().__init__(parent)
        self.colors = colors
        self._lo, self._hi = RATING_MIN, RATING_MAX
        self._drag = None       # "lo" / "hi" / "both"(重なったつまみを掴んで、向きが未定)
        self._ticks = [str(v) for v in range(RATING_MIN, RATING_MAX + 1)]
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        # 目盛りは段の字より1段小さく(左右の窓に1本ずつ出すので、横幅を詰める)
        small = QFont(font)
        if small.pointSize() > 0:
            small.setPointSize(max(small.pointSize() - 1, 7))
        self.setFont(small)
        self._fit_size()

    # --- 値 ---
    def values(self) -> tuple:
        return self._lo, self._hi

    def dragging(self) -> bool:
        return self._drag is not None

    def set_values(self, lo: int, hi: int, emit: bool = False) -> None:
        """範囲を差し替える(範囲外は端へ寄せ、lo > hi なら入れ替える)。
        emit=True なら変わったときに range_changed を出す。"""
        lo = min(max(int(lo), RATING_MIN), RATING_MAX)
        hi = min(max(int(hi), RATING_MIN), RATING_MAX)
        if lo > hi:
            lo, hi = hi, lo
        if (lo, hi) == (self._lo, self._hi):
            return
        self._lo, self._hi = lo, hi
        self.update()
        if emit:
            self.range_changed.emit(lo, hi)

    def set_tick_texts(self, texts) -> None:
        """目盛りの文字(-1〜5 の7つ)。字の幅に合わせて大きさを決め直す。"""
        texts = list(texts)
        if texts != self._ticks:
            self._ticks = texts
            self._fit_size()
            self.update()

    def _fit_size(self) -> None:
        # 目盛りの間隔は「隣どうしの文字の幅の半分の和」の一番大きいもの(長い名前どうしが
        # 重ならない最小の等間隔)。両端は端の文字の半分とつまみが収まる余白。
        fm = QFontMetrics(self.font())
        widths = [fm.horizontalAdvance(t) for t in self._ticks]
        self._step = max((a + b) / 2 for a, b in zip(widths, widths[1:])) + 8
        self._pad = max(widths[0], widths[-1], 2 * self._KNOB_R) / 2 + 2
        self._track_y = self._KNOB_R + 2
        steps = RATING_MAX - RATING_MIN
        self.setFixedSize(int(self._step * steps + 2 * self._pad),
                          self._track_y + self._KNOB_R + fm.height())

    # --- 座標と値 ---
    def x_of(self, v: int) -> float:
        return self._pad + self._step * (v - RATING_MIN)

    def _value_at(self, x: float) -> int:
        v = round((x - self._pad) / self._step) + RATING_MIN
        return min(max(v, RATING_MIN), RATING_MAX)

    # --- マウス ---
    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        self.pressed.emit()
        x = event.position().x()
        v = self._value_at(x)
        if self._lo == self._hi and v == self._lo:
            self._drag = "both"     # 重なったつまみ: 動かした向きで lo / hi を決める
            return
        d_lo, d_hi = abs(x - self.x_of(self._lo)), abs(x - self.x_of(self._hi))
        if d_lo < d_hi or (d_lo == d_hi and v < self._lo):
            self._drag = "lo"
        else:
            self._drag = "hi"
        self._move_drag(v)

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        v = self._value_at(event.position().x())
        if self._drag == "both":
            if v == self._lo:
                return
            self._drag = "lo" if v < self._lo else "hi"
        self._move_drag(v)

    def mouseReleaseEvent(self, event):
        if self._drag is None:
            return
        self._drag = None
        self.released.emit()

    def _move_drag(self, v: int) -> None:
        if self._drag == "lo":
            self.set_values(min(v, self._hi), self._hi, emit=True)
        elif self._drag == "hi":
            self.set_values(self._lo, max(v, self._lo), emit=True)

    # --- 描画 ---
    def paintEvent(self, _event):
        c = self.colors
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y = self._track_y
        x0, x1 = self.x_of(RATING_MIN), self.x_of(RATING_MAX)
        xl, xh = self.x_of(self._lo), self.x_of(self._hi)
        full = photo_config.is_full_range(self._lo, self._hi)
        accent = QColor(c["rating_accent"])
        # トラック(全体)と選んだ範囲。「すべて」のときは控えめな色
        p.setPen(QPen(QColor(c["fkey_border"]), 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
        p.setPen(QPen(QColor(c["footer_text"]) if full else accent, 3, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(xl, y), QPointF(xh, y))
        # 目盛りの文字(範囲の中は明るく)
        fm = QFontMetrics(self.font())
        top = y + self._KNOB_R
        for v, text in zip(range(RATING_MIN, RATING_MAX + 1), self._ticks):
            inside = self._lo <= v <= self._hi and not full
            p.setPen(accent if inside else QColor(c["footer_text"]))
            x = self.x_of(v)
            w = fm.horizontalAdvance(text) + 2
            p.drawText(QRectF(x - w / 2, top, w, fm.height()), Qt.AlignCenter, text)
        # つまみ(重なっていれば1つに見える)
        p.setPen(QPen(QColor(c["footer_text"]) if full else accent, 1.5))
        p.setBrush(QColor(c["status_background"]))
        r = self._KNOB_R
        for x in ((xl,) if self._lo == self._hi else (xl, xh)):
            p.drawEllipse(QPointF(x, y), r, r)
        p.end()


class RatingBar(QWidget):
    """★の範囲スライダー・範囲の名前・キーワードのチップ・表示枚数・解除(✕)を並べた1段。"""

    pressed = Signal()                  # スライダーか ✕・チップを押した(窓をアクティブにする)
    range_changed = Signal(int, int)    # スライダーを動かした(lo, hi)
    released = Signal()                 # スライダーのボタンを離した
    clear_requested = Signal()          # ✕
    keyword_toggled = Signal(str, bool)  # キーワードのチップ(名前, 絞るなら True)

    def __init__(self, colors: dict, font, parent=None):
        super().__init__(parent)
        c = colors
        self.setObjectName("filerRatingBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#filerRatingBar {{ background-color: {c['status_background']}; }}"
            f" #filerRatingBar QLabel {{ color: {c['status_text']}; }}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(6)
        # 中身の幅を窓の最小幅にしない(左右の境界を寄せられなくなるため)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        fm = QFontMetrics(font)

        def label(text=""):
            w = QLabel(text, self)
            w.setTextFormat(Qt.PlainText)
            w.setFont(font)
            return w

        star = label("★")
        star.setStyleSheet(f"color: {c['rating_accent']};")
        self.slider = RatingRangeSlider(c, font, self)
        self.slider.setToolTip("★の範囲で絞り込む(つまみ2つ。重ねると「〜だけ」)")
        self.slider.pressed.connect(self.pressed)
        self.slider.range_changed.connect(self.range_changed)
        self.slider.released.connect(self.released)
        self.range_label = label()
        # キーワードのチップの並び(中身は set_keywords で差し替える)
        self._colors = c
        self._font = font
        self.keyword_box = QWidget(self)
        self.keyword_box.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        kw_layout = QHBoxLayout(self.keyword_box)
        kw_layout.setContentsMargins(0, 0, 0, 0)
        kw_layout.setSpacing(4)
        self.keyword_buttons: dict[str, QPushButton] = {}
        self._any_keyword_on = False
        self.count_label = label()
        self.clear_button = QPushButton("✕", self)
        self.clear_button.setFocusPolicy(Qt.NoFocus)   # ファイル窓からフォーカスを奪わない
        self.clear_button.setFont(font)
        self.clear_button.setToolTip("★の範囲を「すべて」に戻す")
        self.clear_button.setFixedSize(fm.height() + 6, fm.height() + 2)
        self.clear_button.setStyleSheet(
            f"QPushButton {{ background-color: {c['status_background']}; color: {c['status_text']};"
            f" border: 1px solid {c['fkey_border']}; padding: 0px; }}"
            f" QPushButton:disabled {{ color: {c['fkey_border']}; }}")
        self.clear_button.pressed.connect(self.pressed)
        self.clear_button.clicked.connect(self.clear_requested)
        self.star = star
        layout.addWidget(star)
        layout.addWidget(self.slider)
        layout.addWidget(self.range_label)
        layout.addWidget(self.keyword_box)
        layout.addStretch()     # 枚数と ✕ は右端に寄せる
        layout.addWidget(self.count_label)
        layout.addWidget(self.clear_button)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_items()

    def _fit_items(self) -> None:
        """幅に入るものだけ出す。★・スライダー・✕ は必ず、残りは 枚数 → 範囲の名前 の順に、
        入るなら出す。"""
        layout = self.layout()
        margins = layout.contentsMargins()
        gap = layout.spacing()
        room = self.width() - margins.left() - margins.right()
        def width_of(w):
            # スライダーは大きさ固定で sizeHint を持たない(素の QWidget)ので最小幅で測る
            return max(w.sizeHint().width(), w.minimumWidth())

        room -= sum(width_of(w) + gap for w in (self.star, self.slider, self.clear_button)) - gap
        for w in (self.count_label, self.range_label, self.keyword_box):
            if w is self.keyword_box and not self.keyword_buttons:
                w.setVisible(False)
                continue
            need = width_of(w) + gap
            w.setVisible(room >= need)
            if room >= need:
                room -= need

    # --- キーワードのチップ ---

    def _make_chip(self, name: str) -> QPushButton:
        c = self._colors
        b = QPushButton(name, self.keyword_box)
        b.setCheckable(True)
        b.setFocusPolicy(Qt.NoFocus)   # ファイル窓からフォーカスを奪わない
        b.setFont(self._font)
        b.setToolTip(f"「{name}」が付いている写真だけ出す(もう一度押すと解除)")
        b.setStyleSheet(
            f"QPushButton {{ background-color: {c['status_background']}; color: {c['status_text']};"
            f" border: 1px solid {c['fkey_border']}; border-radius: 8px; padding: 1px 8px; }}"
            f" QPushButton:checked {{ background-color: {c['rating_accent']}; color: {c['background']};"
            f" border: 1px solid {c['rating_accent']}; }}")
        b.pressed.connect(self.pressed)
        b.toggled.connect(lambda on, n=name: self.keyword_toggled.emit(n, on))
        return b

    def set_keywords(self, keywords) -> None:
        """キーワードのチップを並べ直す。keywords は (名前, 絞り込み中か) の列。

        名前の並びが変わったときだけボタンを作り直し、印だけなら付け直す
        (信号は出さない)。"""
        names = [name for name, _on in keywords]
        if names != list(self.keyword_buttons):
            layout = self.keyword_box.layout()
            for b in self.keyword_buttons.values():
                layout.removeWidget(b)
                b.setParent(None)
                b.deleteLater()
            self.keyword_buttons = {}
            for name in names:
                b = self._make_chip(name)
                self.keyword_buttons[name] = b
                layout.addWidget(b)
        self._any_keyword_on = False
        for name, on in keywords:
            self._any_keyword_on = self._any_keyword_on or bool(on)
            b = self.keyword_buttons[name]
            if b.isChecked() != bool(on):
                b.blockSignals(True)
                b.setChecked(bool(on))
                b.blockSignals(False)
        self.keyword_box.setVisible(bool(names))
        self._fit_items()

    def show_state(self, lo: int, hi: int, shown: int, total: int,
                   config: dict, move_slider: bool = True, keywords=()) -> None:
        """表示を差し替える(range_changed は出さない)。move_slider=False ならつまみは
        動かさない(ドラッグ中で、窓の側がまだ追い付いていないとき)。
        keywords は (名前, 絞り込み中か) の列で、チップの並びと印になる。"""
        self.slider.set_tick_texts(photo_config.rating_short(v, config)
                                   for v in range(RATING_MIN, RATING_MAX + 1))
        if move_slider:
            self.slider.set_values(lo, hi)
        self.count_label.setText(f"表示 {shown:,} / {total:,}枚")
        self.set_keywords(keywords)
        self.show_range(lo, hi, config)
        self._fit_items()

    def show_range(self, lo: int, hi: int, config: dict) -> None:
        """範囲の名前だけ差し替える(ドラッグ中、絞り込みを掛け直す前に先に出す)。"""
        text = photo_config.range_label(lo, hi, config)
        if text != self.range_label.text():
            self.range_label.setText(text)
            self._fit_items()
        # ✕ は★の範囲だけでなくキーワードの絞り込みも解くので、どちらか効いていれば押せる
        self.clear_button.setEnabled(
            not photo_config.is_full_range(lo, hi) or self._any_keyword_on)
