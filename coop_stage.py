# coop_stage.py
# 進捗パネルの「連携ステージ」の描画。バナー(いま誰待ちか)・ステージ(Muse と Claw'd の
# ドット絵が手紙をやり取りする)・カード3枚を QPainter で描く。
# 何を出すかは coop_view(純関数)、絵とフレームの選び方は chara_sprites(純データ)で、
# ここは「それを座標に落として塗る」だけ。ファイルは読まない(受け取り済みの値と時計だけ)。
# 見た目は動くデモ(anim_template.html)の配色・動き・文言に合わせてある。
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen,
                           QPixmap, QPolygonF)

import chara_sprites as sp

STAGE_H = 116
BANNER_H = 36
CARDS_H = 34
PAD = 8
GAP = 6
COOP_HEIGHT = PAD + BANNER_H + GAP + STAGE_H + GAP + CARDS_H + PAD
COOP_COLLAPSED_HEIGHT = 56    # たたんだとき: バナーだけ(上下に余白)
TOGGLE_SIZE = 20              # 「たたむ／ひろげる」ボタンの当たり(px)
CELL = 5                      # キャラの1マス(px)。Claw'd は 60x40(12x8)。Muse は下の MUSE_CELL で 52x60(13x15)
MUSE_CELL = 4                 # Muse の1マス(px)。v2 は 13x15 なので 52x60
ITEM_CELL = 2                 # 手紙・受領・火花の1マス(px)
ITEM_LEFT = 72                # 手紙の出発点(ステージ左端から)
CLOSE_ROOM = 20               # 右上の ✕ のために空けておく幅

BG = QColor("#0a1118")
EDGE = QColor("#1e6f86")
STAGE_EDGE = QColor("#17485a")
SUB = QColor("#9fd5e4")
LABEL = QColor("#4fb7d0")
CARD_BG = QColor("#0d1a22")
CARD_VALUE = QColor("#e6faff")
TONES = {   # デモの色変数
    "work": QColor("#37e0b0"), "claude": QColor("#6ea8ff"), "approval": QColor("#f0b04a"),
    "alert": QColor("#ff5a5f"), "done": QColor("#4fe0ff"), "sleep": QColor("#8fb4c2"),
}

# 手紙・受領・火花の小さなドット絵(自作。デモの SVG と同じ雰囲気で小さくしたもの)
ITEM_PALETTE = {"a": "#aeb9cc", "w": "#eef3fa", "G": "#23965a", "W": "#ffffff",
                "Y": "#ffd83d"}
ITEM_GRIDS = {
    "letter": ["aaaaaaaaaa", "awwwwwwwwa", "aawwwwwwaa", "awawwwwawa",
               "awwaaaawwa", "awwwwwwwwa", "aaaaaaaaaa"],
    "ack": ["..GGGGGG..", ".GGGGGGGG.", "GGGGGGGWGG", "GGGGGGWWGG", "GWGGGWWGGG",
            "GGWGWWGGGG", "GGGWWGGGGG", ".GGGWGGGG.", "..GGGGGG..", "...GGGG..."],
    "spark": ["....Y....", "....Y....", "...YYY...", "YYYYYYYYY", "...YYY...",
              "....Y....", "....Y...."],
}
_PIX_CACHE = {}


def grid_pixmap(key, rows, palette, cell, dpr) -> QPixmap:
    """格子から QPixmap を作る(作ったものは使い回す)。画面の倍率に合わせて実ピクセルで
    塗るので、ドットがにじまない。"""
    cache_key = (key, cell, dpr)
    pm = _PIX_CACHE.get(cache_key)
    if pm is not None:
        return pm
    w, h = len(rows[0]), len(rows)
    pm = QPixmap(int(round(w * cell * dpr)), int(round(h * cell * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    q = QPainter(pm)
    try:
        q.setRenderHint(QPainter.Antialiasing, False)
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                color = palette.get(ch)
                if color:
                    x0, y0 = round(i * cell * dpr), round(j * cell * dpr)
                    x1, y1 = round((i + 1) * cell * dpr), round((j + 1) * cell * dpr)
                    q.fillRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0), QColor(color))
    finally:
        q.end()
    _PIX_CACHE[cache_key] = pm
    return pm


def item_position(kind, item, t_ms, stage_w):
    """手紙(letter)・受領(ack)の (x, 不透明度)。x はステージ左端から。出さないなら None。
    デモの CSS(fly / flyback / bob / shake)と同じ動き。"""
    right = stage_w - 100
    if kind == "fly":
        if item == "letter":
            p, a, b = (t_ms % 3000) / 3000.0, ITEM_LEFT, right
        else:   # 受領は1.5秒遅れて逆向きに飛ぶ
            p, a, b = ((t_ms - 1500) % 3000) / 3000.0, right, ITEM_LEFT
        if p < 0.85:
            return (a + (b - a) * p / 0.85, 1.0)
        return (b, 1.0 - (p - 0.85) / 0.15)
    if kind == "back" and item == "ack":
        p = (t_ms % 2400) / 2400.0
        if p < 0.85:
            return (right + (ITEM_LEFT - right) * p / 0.85, 1.0)
        return (ITEM_LEFT, 1.0 - (p - 0.85) / 0.15)
    if kind == "parked" and item == "letter":
        return (right, 1.0)
    if kind == "wobble" and item == "letter":
        return (stage_w * 0.46 + (-2 if t_ms % 400 < 200 else 2), 1.0)
    return None


def _sprite(p, dpr, key, rows, palette, x, y, cell=CELL):
    p.drawPixmap(QPoint(int(round(x)), int(round(y))),
                 grid_pixmap(key, rows, palette, cell, dpr))


def _blink_text(p, text, rect, color, pixel_size, on):
    font = QFont("Consolas")
    font.setPixelSize(pixel_size)
    font.setBold(True)
    p.setFont(font)
    p.setPen(color)
    p.setOpacity(1.0 if on else 0.15)
    p.drawText(rect, Qt.AlignCenter, text)
    p.setOpacity(1.0)


def toggle_rect(rect) -> QRect:
    """「たたむ／ひろげる」ボタンの矩形(行の右上。バナーの右端)。広げた状態でもたたんだ
    状態でも、バナーの右端に同じ大きさで置く。"""
    top = rect.top() + _banner_top_offset(rect.height())
    return QRect(rect.right() - PAD - TOGGLE_SIZE + 1, top + (BANNER_H - TOGGLE_SIZE) // 2,
                 TOGGLE_SIZE, TOGGLE_SIZE)


def _banner_top_offset(height) -> int:
    return PAD if height >= COOP_HEIGHT else (height - BANNER_H) // 2


def _paint_toggle(p, rect, collapsed, hot):
    """小さなシェブロン。▾=広げている(押すとたたむ)、▴=たたんでいる(押すと広げる)。"""
    box = toggle_rect(rect)
    c = QPointF(box.center().x() + 0.5, box.center().y() + 0.5)
    if hot:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(QRectF(box), 3, 3)
    pen = QPen(QColor("#bfe9f5") if hot else QColor("#4a8da0"), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    k = -2.0 if collapsed else 2.0   # 頂点の向き(▴ は上、▾ は下)
    p.drawPolyline(QPolygonF([QPointF(c.x() - 4, c.y() - k), QPointF(c.x(), c.y() + k),
                              QPointF(c.x() + 4, c.y() - k)]))


def paint_coop(p, rect, view, now, dpr, sub_font, collapsed=False, hot=False):
    """連携ステージの行を rect に描く。view は coop_view.coop_view の戻り、now は epoch 秒。
    collapsed ならバナーだけ(ステージとカードは描かない)。hot はボタンにマウスが乗っている。"""
    accent = TONES.get(view["tone"], TONES["sleep"])
    t = now * 1000.0
    # 枠(ごく小さな丸みの長方形)
    box = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(EDGE, 1))
    p.setBrush(BG)
    p.drawRoundedRect(box, 3, 3)
    p.setRenderHint(QPainter.Antialiasing, False)
    top_pad = _banner_top_offset(rect.height())
    inner = QRect(rect.left() + PAD, rect.top() + top_pad,
                  rect.width() - PAD * 2, rect.height() - top_pad - PAD)

    # バナー: 左に太線、右へ薄れるグラデーション、右端に経過(✕の分は空ける)
    ban = QRect(inner.left(), inner.top(), inner.width(), BANNER_H)
    grad = QLinearGradient(ban.left(), 0, ban.right(), 0)
    grad.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 41))
    grad.setColorAt(1, QColor(accent.red(), accent.green(), accent.blue(), 0))
    p.fillRect(ban, grad)
    p.fillRect(QRect(ban.left(), ban.top(), 3, ban.height()), accent)
    text_l = ban.left() + 12
    title_font = QFont("Meiryo", 10)
    title_font.setBold(True)
    tfm = QFontMetrics(title_font)
    time_w = tfm.horizontalAdvance(view["banner_time"]) if view["banner_time"] else 0
    text_w = max(0, ban.right() - CLOSE_ROOM - text_l - time_w - 6)
    p.setFont(title_font)
    p.setPen(accent)
    p.drawText(QRect(text_l, ban.top() + 2, text_w, tfm.height()),
               Qt.AlignLeft | Qt.AlignVCenter,
               tfm.elidedText(view["banner_title"], Qt.ElideRight, text_w))
    if view["banner_time"]:
        p.drawText(QRect(ban.right() - CLOSE_ROOM - time_w, ban.top() + 2, time_w + 2,
                         tfm.height()), Qt.AlignRight | Qt.AlignVCenter,
                   view["banner_time"])
    sfm = QFontMetrics(sub_font)
    p.setFont(sub_font)
    p.setPen(SUB)
    sub_w = max(0, ban.right() - CLOSE_ROOM - text_l)
    p.drawText(QRect(text_l, ban.top() + 2 + tfm.height(), sub_w, sfm.height()),
               Qt.AlignLeft | Qt.AlignVCenter,
               sfm.elidedText(view["banner_sub"], Qt.ElideRight, sub_w))

    if collapsed:
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_toggle(p, rect, True, hot)
        return
    # ステージ
    st = QRect(inner.left(), ban.bottom() + 1 + GAP, inner.width(), STAGE_H)
    grad = QLinearGradient(0, st.top(), 0, st.bottom())
    grad.setColorAt(0, QColor("#08121a"))
    grad.setColorAt(1, BG)
    p.fillRect(st, grad)
    p.setPen(QPen(STAGE_EDGE, 1))
    p.setBrush(Qt.NoBrush)
    p.drawRect(st.adjusted(0, 0, -1, -1))
    p.save()
    try:
        p.setClipRect(st.adjusted(1, 1, -1, -1))
        _paint_stage(p, dpr, view, st, accent, t)
    finally:
        p.restore()

    # カード3枚
    top = st.bottom() + 1 + GAP
    gap = 5
    cw = (inner.width() - gap * 2) // 3
    label_font = QFont("Meiryo", 7)
    value_font = QFont("Meiryo", 8)
    value_font.setBold(True)
    lfm, vfm = QFontMetrics(label_font), QFontMetrics(value_font)
    for i, (label, value) in enumerate(view["cards"]):
        card = QRect(inner.left() + i * (cw + gap), top, cw, CARDS_H)
        p.setPen(QPen(STAGE_EDGE, 1))
        p.setBrush(CARD_BG)
        p.drawRect(card.adjusted(0, 0, -1, -1))
        body = card.adjusted(6, 3, -4, -3)
        p.setFont(label_font)
        p.setPen(LABEL)
        p.drawText(QRect(body.left(), body.top(), body.width(), lfm.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, label)
        p.setFont(value_font)
        p.setPen(CARD_VALUE)
        p.drawText(QRect(body.left(), body.top() + lfm.height(), body.width(),
                         vfm.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   vfm.elidedText(value, Qt.ElideRight, body.width()))
    p.setRenderHint(QPainter.Antialiasing, True)   # 呼び出し元(他の行)は丸みを使う
    _paint_toggle(p, rect, False, hot)


def _paint_stage(p, dpr, view, st, accent, t):
    """ステージの中身。st は枠の矩形、t は ms。キャラ・手紙・点・「!」などを動かす。"""
    W, H = st.width(), st.height()
    ox, oy = st.left(), st.top()
    feet = oy + H - 31                       # 足元(この下に名札、さらに下に地面)
    pen = QPen(EDGE, 1)
    pen.setDashPattern([6, 6])
    p.setPen(pen)
    gy = oy + H - 18
    p.drawLine(ox, gy, ox + W, gy)
    props = view["props"]

    # Muse(左)と Claw'd(右)
    mdx, mdy = sp.move_offset(sp.MUSE_MOVE.get(view["muse_anim"], ""), t)
    mname = sp.muse_frame(view["muse_anim"], t)
    mrows = sp.muse_rows(mname)
    mw, mh = len(mrows[0]) * MUSE_CELL, len(mrows) * MUSE_CELL
    mtop = feet - mh                          # Claw'd と足元(feet)を揃える
    _sprite(p, dpr, ("muse", mname), mrows, sp.MUSE_PALETTE,
            ox + 8 + mdx, mtop + mdy, MUSE_CELL)
    cdx, cdy = sp.move_offset(sp.CLAWD_MOVE.get(view["clawd_anim"], ""), t)
    cname = sp.clawd_frame(view["clawd_anim"], t)
    cx, cy = ox + W - 70 + cdx, feet - 8 * CELL + cdy
    _sprite(p, dpr, ("clawd", cname), sp.CLAWD_FRAMES[cname], sp.CLAWD_PALETTE, cx, cy)
    if "hat" in props:
        _sprite(p, dpr, ("hat",), sp.HAT, sp.CLAWD_PALETTE, cx, cy - 6 * CELL)

    # 名札
    tag_font = QFont("Consolas", 7)
    tag_font.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
    p.setFont(tag_font)
    p.setPen(accent)
    th = QFontMetrics(tag_font).height()
    p.drawText(QRect(ox + 8 + mw // 2 - 32, feet + 2, 64, th), Qt.AlignCenter, "MUSE")
    p.drawText(QRect(ox + W - 72, feet + 2, 68, th), Qt.AlignCenter, "CLAW'D")

    # 手紙・受領のチェック(座標で飛ばす)
    kind = view["item"]
    if kind:
        for name in ("letter", "ack"):
            pos = item_position(kind, name, t, W)
            if pos is None:
                continue
            p.setOpacity(max(0.0, min(1.0, pos[1])))
            dy = 0 if name == "ack" else 4 + (-2 if kind == "parked" and t % 1000 >= 500 else 0)
            _sprite(p, dpr, ("item", name), ITEM_GRIDS[name], ITEM_PALETTE,
                    ox + pos[0] + 1, oy + H - 74 + dy, ITEM_CELL)
            p.setOpacity(1.0)
    if kind in ("parked", "wobble"):
        _blink_text(p, "?", QRect(ox + W // 2 - 10, oy + H - 94, 20, 20), accent, 16,
                    t % 800 < 400)

    # 火花(Muse の頭の上)
    if "spark" in props:
        p.setOpacity(1.0 if t % 500 < 250 else 0.15)
        _sprite(p, dpr, ("item", "spark"), ITEM_GRIDS["spark"], ITEM_PALETTE,
                ox + 8 + (mw - 18) // 2, mtop - 16, ITEM_CELL)
        p.setOpacity(1.0)
    # Claw'd の頭の上と体まわり
    if "dots" in props:
        for i in range(3):
            phase = ((t - i * 250) % 1000) / 1000.0
            p.fillRect(cx + 20 + i * 8, cy - 10, 4, 4,
                       QColor(79, 224, 255, 255 if phase >= 0.3 else 38))
    if "bang" in props:
        _blink_text(p, "!", QRect(cx + 27, cy - 26, 14, 20), QColor("#ff5a5f"), 20,
                    t % 500 < 250)
    if "sweat" in props:
        k = int((t % 1200) / 1200.0 * 4) / 4.0
        p.setOpacity(1.0 - k)
        p.fillRect(cx + 52, int(cy + 10 * k), 4, 6, QColor("#6ec8ff"))
        p.setOpacity(1.0)
    if "zz" in props:
        k = int((t % 2400) / 2400.0 * 6) / 6.0
        alpha = k / 0.15 if k < 0.15 else 1.0 - (k - 0.15) / 0.85
        p.setOpacity(max(0.0, min(1.0, alpha)))
        font = QFont("Consolas", 9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#8fb4c2"))
        p.drawText(QRect(int(cx + 48 + 10 * k), int(cy - 16 - 22 * k), 40, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, "z z Z")
        p.setOpacity(1.0)
    if "confetti" in props:
        p.setRenderHint(QPainter.Antialiasing, True)
        for x, y, color, angle, alpha in sp.confetti_particles(t, W):
            p.save()
            p.setOpacity(max(0.0, min(1.0, alpha)))
            p.translate(ox + x + 2, oy + y + 2)
            p.rotate(angle)
            p.fillRect(-2, -2, 4, 4, QColor(color))
            p.restore()
        p.setRenderHint(QPainter.Antialiasing, False)
