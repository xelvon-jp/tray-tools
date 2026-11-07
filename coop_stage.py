# coop_stage.py
# 進捗パネルの「連携ステージ」の描画。バナー(いま誰待ちか)・ステージ(Muse と Claw'd の
# ドット絵が手紙をやり取りする)・カード3枚を QPainter で描く。
# 何を出すかは coop_view(純関数)、絵とフレームの選び方は chara_sprites(純データ)で、
# ここは「それを座標に落として塗る」だけ。ファイルは読まない(受け取り済みの値と時計だけ)。
# 見た目は動くデモ(anim_template.html)の配色・動き・文言に合わせてある。
import math

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
# ブリッジの大きさ(muse/launcher-bridge-size)。stage_h はステージ部分の高さ、
# muse_cell / cell / item_cell は各ドット絵の1マス(px)。絵は格子データから
# 描き直すので、1/2・1/4 の縮小でも格子の情報は欠けない(画像のリサンプルでは
# ない)。cards は下のカード3枚を出すか。中・小ではカードを落として背を低く
# する。名前(MUSE / CLAW'D の文字)はどの大きさでも描かない(陽太さんの指定)。
SIZES = {
    "large": {"stage_h": 116, "muse_cell": 4, "cell": 5, "item_cell": 2, "cards": True},
    "medium": {"stage_h": 74, "muse_cell": 2, "cell": 2.5, "item_cell": 1, "cards": False},
    "small": {"stage_h": 50, "muse_cell": 1, "cell": 1.25, "item_cell": 1, "cards": False},
}
DEFAULT_SIZE = "large"
STEALTH_W = 20      # ステルス時の帯の幅(px)
STEALTH_H = 96      # ステルス時の帯の高さ(px)


def size_spec(size) -> dict:
    """大きさの指定から描画パラメータを引く。知らない指定は large に倒す。"""
    return SIZES.get(size) or SIZES[DEFAULT_SIZE]


def coop_height(size) -> int:
    """その大きさで広げたときの連携ステージ行の高さ(たたんだときは別)。"""
    spec = size_spec(size)
    height = PAD + BANNER_H + GAP + spec["stage_h"] + PAD
    if spec["cards"]:
        height += GAP + CARDS_H
    return height
TOGGLE_SIZE = 20              # 「たたむ／ひろげる」ボタンの当たり(px)
PAUSE_GAP = 4                 # 休止ボタンとたたむボタンの間(px)
# 全員待機中(all_idle)の明滅。ゆっくり一呼吸ぶん周期で、枠の明るさが上下する。
PULSE_PERIOD_MS = 2800
PULSE_MIN = 40                # 明滅する枠の不透明度(最も暗いとき)
PULSE_MAX = 130               # 明滅する枠の不透明度(最も明るいとき)
PULSE_COLOR = QColor(140, 220, 245)
CELL = 5                      # キャラの1マス(px)。Claw'd は 60x40(12x8)。Muse は下の MUSE_CELL で 52x60(13x15)
MUSE_CELL = 4                 # Muse の1マス(px)。v2 は 13x15 なので 52x60
ITEM_CELL = 2                 # 手紙・受領・火花の1マス(px)
ITEM_LEFT = 72                # 手紙の出発点(ステージ左端から)
CLOSE_ROOM = 44               # 右上のボタン2つ(休止・たたむ)のために空けておく幅

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


def item_position(kind, item, t_ms, stage_w, left=None, right=None):
    """手紙(letter)・受領(ack)の (x, 不透明度)。x はステージ左端から。出さないなら None。
    デモの CSS(fly / flyback / bob / shake)と同じ動き。left / right は飛行の両端で、
    大きさごとにキャラの居場所が変わるので呼び出し側が渡す(省略時は従来の位置)。"""
    if left is None:
        left = ITEM_LEFT
    if right is None:
        right = stage_w - 100
    if kind == "fly":
        if item == "letter":
            p, a, b = (t_ms % 3000) / 3000.0, left, right
        else:   # 受領は1.5秒遅れて逆向きに飛ぶ
            p, a, b = ((t_ms - 1500) % 3000) / 3000.0, right, left
        if p < 0.85:
            return (a + (b - a) * p / 0.85, 1.0)
        return (b, 1.0 - (p - 0.85) / 0.15)
    if kind == "back" and item == "ack":
        p = (t_ms % 2400) / 2400.0
        if p < 0.85:
            return (right + (left - right) * p / 0.85, 1.0)
        return (left, 1.0 - (p - 0.85) / 0.15)
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


def pause_rect(rect) -> QRect:
    """「見張りを休止／再開」ボタンの矩形(たたむボタンの左隣)。"""
    box = toggle_rect(rect)
    return QRect(box.left() - PAUSE_GAP - TOGGLE_SIZE, box.top(),
                 TOGGLE_SIZE, TOGGLE_SIZE)


def pulse_alpha(t_ms) -> int:
    """全員待機中の明滅の不透明度。周期 PULSE_PERIOD_MS で PULSE_MIN〜PULSE_MAX を
    なめらかに往復する(頂点で跳ねないよう余弦でつなぐ)。"""
    phase = (t_ms % PULSE_PERIOD_MS) / PULSE_PERIOD_MS
    wave = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
    return round(PULSE_MIN + (PULSE_MAX - PULSE_MIN) * wave)


def _paint_pause(p, rect, paused, hot):
    """休止ボタン: 2本の縦棒。休止中は橙(止まっているのが見て分かるように)、
    動いている間は控えめな水色。押すと合図ファイルが切り替わる(bridge_control)。"""
    box = pause_rect(rect)
    if hot:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 30))
        p.drawRoundedRect(QRectF(box), 3, 3)
    color = QColor("#f0b04a") if paused else (QColor("#bfe9f5") if hot
                                              else QColor("#4a8da0"))
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    cx, cy = box.center().x(), box.center().y()
    for dx in (-3.5, 1.5):
        p.drawRect(QRectF(cx + dx, cy - 5, 2.6, 10))


def _banner_top_offset(height) -> int:
    # 広げた状態(どの大きさでも)は上詰め、たたんだ状態だけ上下中央。
    # 大きさが増えたので「最大の高さと比べる」判定は使えない(中・小が
    # たたんだ扱いになってしまう)。たたんだ高さは固定なので、それ以下かで分ける。
    return PAD if height > COOP_COLLAPSED_HEIGHT else (height - BANNER_H) // 2


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


def paint_coop(p, rect, view, now, dpr, sub_font, collapsed=False, hot=False,
               hot_pause=False, size=DEFAULT_SIZE):
    """連携ステージの行を rect に描く。view は coop_view.coop_view の戻り、now は epoch 秒。
    collapsed ならバナーだけ(ステージとカードは描かない)。size は大きさ
    (large / medium / small。SIZES 参照)。hot / hot_pause は各ボタンに
    マウスが乗っているか。全員待機中(view の all_idle)なら、枠をゆっくり明滅させる
    (二人とも陽太さん待ちなのが、離れていても目に入るように)。"""
    spec = size_spec(size)
    accent = TONES.get(view["tone"], TONES["sleep"])
    t = now * 1000.0
    # 枠(ごく小さな丸みの長方形)
    box = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(EDGE, 1))
    p.setBrush(BG)
    p.drawRoundedRect(box, 3, 3)
    if view.get("all_idle"):
        glow = QColor(PULSE_COLOR)
        glow.setAlpha(pulse_alpha(t))
        p.setPen(QPen(glow, 2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(box.adjusted(1, 1, -1, -1), 3, 3)
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
        _paint_pause(p, rect, bool(view.get("paused")), hot_pause)
        _paint_toggle(p, rect, True, hot)
        return
    # ステージ
    st = QRect(inner.left(), ban.bottom() + 1 + GAP, inner.width(), spec["stage_h"])
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
        _paint_stage(p, dpr, view, st, accent, t, spec)
    finally:
        p.restore()

    if not spec["cards"]:
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_pause(p, rect, bool(view.get("paused")), hot_pause)
        _paint_toggle(p, rect, False, hot)
        return

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
    _paint_pause(p, rect, bool(view.get("paused")), hot_pause)
    _paint_toggle(p, rect, False, hot)


def _paint_stage(p, dpr, view, st, accent, t, spec):
    """ステージの中身。st は枠の矩形、t は ms。キャラ・手紙・点・「!」などを動かす。

    配置は全部「足元」と「キャラの大きさ」から組み立てる(大きさごとに変わる)。
    名札(MUSE / CLAW'D の文字)は描かないので、足元は地面のすぐ上まで使える。"""
    W, H = st.width(), st.height()
    ox, oy = st.left(), st.top()
    muse_cell = spec["muse_cell"]
    cell = spec["cell"]
    item_cell = spec["item_cell"]
    feet = oy + H - 8                        # 足元(地面の少し上)
    pen = QPen(EDGE, 1)
    pen.setDashPattern([6, 6])
    p.setPen(pen)
    gy = oy + H - 5
    p.drawLine(ox, gy, ox + W, gy)
    props = view["props"]

    # Muse(左)と Claw'd(右)
    mdx, mdy = sp.move_offset(sp.MUSE_MOVE.get(view["muse_anim"], ""), t)
    mname = sp.muse_frame(view["muse_anim"], t)
    mrows = sp.muse_rows(mname)
    mw, mh = len(mrows[0]) * muse_cell, len(mrows) * muse_cell
    mtop = feet - mh                          # Claw'd と足元(feet)を揃える
    _sprite(p, dpr, ("muse", mname), mrows, sp.MUSE_PALETTE,
            ox + 8 + mdx, mtop + mdy, muse_cell)
    cdx, cdy = sp.move_offset(sp.CLAWD_MOVE.get(view["clawd_anim"], ""), t)
    cname = sp.clawd_frame(view["clawd_anim"], t)
    cw = 12 * cell
    cx, cy = ox + W - 10 - cw + cdx, feet - 8 * cell + cdy
    _sprite(p, dpr, ("clawd", cname), sp.CLAWD_FRAMES[cname], sp.CLAWD_PALETTE,
            cx, cy, cell)
    if "hat" in props:
        _sprite(p, dpr, ("hat",), sp.HAT, sp.CLAWD_PALETTE, cx, cy - 6 * cell, cell)

    # 手紙・受領のチェック(座標で飛ばす)。高さは Muse の胸のあたり。
    item_top = feet - max(6, round(mh * 0.72))
    fly_left = 8 + mw + 6
    fly_right = W - 10 - cw - 10 * item_cell - 4
    kind = view["item"]
    if kind:
        for name in ("letter", "ack"):
            pos = item_position(kind, name, t, W, fly_left, fly_right)
            if pos is None:
                continue
            p.setOpacity(max(0.0, min(1.0, pos[1])))
            dy = 0 if name == "ack" else 4 + (-2 if kind == "parked" and t % 1000 >= 500 else 0)
            _sprite(p, dpr, ("item", name), ITEM_GRIDS[name], ITEM_PALETTE,
                    ox + pos[0] + 1, item_top + dy, item_cell)
            p.setOpacity(1.0)
    if kind in ("parked", "wobble"):
        _blink_text(p, "?", QRect(ox + W // 2 - 10, item_top - 22, 20, 20), accent, 16,
                    t % 800 < 400)

    # 火花(Muse の頭の上)
    if "spark" in props:
        p.setOpacity(1.0 if t % 500 < 250 else 0.15)
        _sprite(p, dpr, ("item", "spark"), ITEM_GRIDS["spark"], ITEM_PALETTE,
                ox + 8 + (mw - 18) // 2, mtop - 7 * item_cell - 2, item_cell)
        p.setOpacity(1.0)
    # Claw'd の頭の上と体まわり。オフセットは Claw'd の大きさ(CELL 比)で縮める。
    f = cell / CELL
    if "dots" in props:
        dot = max(2, round(4 * f))
        for i in range(3):
            phase = ((t - i * 250) % 1000) / 1000.0
            p.fillRect(int(cx + (20 + i * 8) * f), int(cy - 10 * f), dot, dot,
                       QColor(79, 224, 255, 255 if phase >= 0.3 else 38))
    if "bang" in props:
        _blink_text(p, "!", QRect(int(cx + 27 * f), int(cy - 26 * f), 14, 20),
                    QColor("#ff5a5f"), max(11, round(20 * f)),
                    t % 500 < 250)
    if "sweat" in props:
        k = int((t % 1200) / 1200.0 * 4) / 4.0
        p.setOpacity(1.0 - k)
        p.fillRect(int(cx + 52 * f), int(cy + 10 * k * f),
                   max(2, round(4 * f)), max(3, round(6 * f)), QColor("#6ec8ff"))
        p.setOpacity(1.0)
    if "zz" in props:
        k = int((t % 2400) / 2400.0 * 6) / 6.0
        alpha = k / 0.15 if k < 0.15 else 1.0 - (k - 0.15) / 0.85
        p.setOpacity(max(0.0, min(1.0, alpha)))
        font = QFont("Consolas", max(7, round(9 * f)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#8fb4c2"))
        p.drawText(QRect(int(cx + 48 * f + 10 * k * f), int(cy - 16 * f - 22 * k * f), 40, 16),
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


def paint_stealth(p, rect, view, now):
    """ステルス時の帯(幅 STEALTH_W)を描く。

    残すのはバナーの左端にある太線だけ。色は「いま誰待ちか」(tone)で、
    全員待機中は枠と同じ呼吸で明滅し、異常のときは点滅して知らせる。
    休止中は帯の真ん中に小さな休止印(橙の縦棒2本)を足す。view が無ければ
    待機色の帯だけにする。"""
    accent = TONES.get(view["tone"], TONES["sleep"]) if view else TONES["sleep"]
    t = now * 1000.0
    box = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(EDGE, 1))
    p.setBrush(BG)
    p.drawRoundedRect(box, 3, 3)
    alpha = 255
    if view and view.get("all_idle"):
        alpha = min(255, pulse_alpha(t) + 70)   # 帯は細いので下限を持ち上げる
    elif view and view["tone"] == "alert":
        alpha = 255 if t % 1000 < 550 else 110
    bar = QColor(accent)
    bar.setAlpha(alpha)
    p.setPen(Qt.NoPen)
    p.setBrush(bar)
    p.drawRoundedRect(QRectF(rect.left() + 6, rect.top() + 8, 3,
                             rect.height() - 16), 1.5, 1.5)
    if view and view.get("paused"):
        p.setBrush(QColor("#f0b04a"))
        cy = rect.center().y()
        for dx in (-3.0, 1.0):
            p.drawRect(QRectF(rect.center().x() + dx, cy - 5, 2.2, 10))
