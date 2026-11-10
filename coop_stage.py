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

import bridge_events_view as bev
import chara_sprites as sp

STAGE_H = 148   # 大きさ「大」のステージ高さ(SIZES['large'] と一致させる)
BANNER_H = 36
CARDS_H = 34
PAD = 8
GAP = 6
# 履歴テープ(ステージの下・カードの上)。行数は bridge_events_view の
# TAPE_LINES と揃える。出すのはカードのある大きさ(large)だけ。
TAPE_LINE_H = 13
TAPE_H = TAPE_LINE_H * bev.TAPE_LINES
COOP_HEIGHT = PAD + BANNER_H + GAP + STAGE_H + GAP + TAPE_H + GAP + CARDS_H + PAD
COOP_COLLAPSED_HEIGHT = 56    # たたんだとき: バナーだけ(上下に余白)
# ブリッジの大きさ(muse/launcher-bridge-size)。stage_h はステージ部分の高さ、
# muse_cell / cell / item_cell は各ドット絵の1マス(px)。絵は格子データから
# 描き直すので、1/2・1/4 の縮小でも格子の情報は欠けない(画像のリサンプルでは
# ない)。cards は下のカード3枚を出すか。中・小ではカードを落として背を低く
# する。名前(MUSE / CLAW'D の文字)はどの大きさでも描かない(陽太さんの指定)。
SIZES = {
    "large": {"stage_h": 148, "muse_cell": 4, "cell": 5, "item_cell": 2, "cards": True},
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
        height += GAP + TAPE_H + GAP + CARDS_H
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
CAUTION = QColor("#f0c44c")   # 自己申告と判定の食い違いの注意表示
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
    sub_w = max(0, ban.right() - CLOSE_ROOM - text_l)
    if view.get("caution"):
        # 自己申告と見張りの判定の食い違いは、sub の代わりに注意を出す(色も変える)
        p.setPen(CAUTION)
        sub_text = "⚠ " + view["caution"]
    else:
        p.setPen(SUB)
        sub_text = view["banner_sub"]
        rep = view.get("self")
        if rep:
            sub_text += f"(自己申告: {rep['label']})"
    p.drawText(QRect(text_l, ban.top() + 2 + tfm.height(), sub_w, sfm.height()),
               Qt.AlignLeft | Qt.AlignVCenter,
               sfm.elidedText(sub_text, Qt.ElideRight, sub_w))

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
        if spec["cards"]:
            # 大きさ「大」は最終イメージの場面(家=PC を中心にした接続図)。
            # 中・小は従来の二人のステージのまま。
            _paint_scene(p, dpr, view, st, accent, t, spec)
        else:
            _paint_stage(p, dpr, view, st, accent, t, spec)
    finally:
        p.restore()

    if not spec["cards"]:
        p.setRenderHint(QPainter.Antialiasing, True)
        _paint_pause(p, rect, bool(view.get("paused")), hot_pause)
        _paint_toggle(p, rect, False, hot)
        return

    # 履歴テープ(直近のやり取りを新しい順に)。ステージの下・カードの上。
    tape_top = st.bottom() + 1 + GAP
    tape_font = QFont("Meiryo", 8)
    tape_fm = QFontMetrics(tape_font)
    p.setFont(tape_font)
    p.setPen(SUB)
    for i, line in enumerate((view.get("tape") or [])[:bev.TAPE_LINES]):
        r = QRect(inner.left() + 2, tape_top + i * TAPE_LINE_H,
                  inner.width() - 4, TAPE_LINE_H)
        p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter,
                   tape_fm.elidedText(line, Qt.ElideRight, r.width()))

    # カード(枚数は view の cards に合わせる。いまは5枚)
    top = tape_top + TAPE_H + GAP
    gap = 5
    n_cards = max(1, len(view["cards"]))
    cw = (inner.width() - gap * (n_cards - 1)) // n_cards
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
    # 実イベントの飛行(bridge_events_view)。approval の直後は Claw'd が
    # 「押す動き」をする(ハサミを一瞬閉じるように、少しだけ沈む)。
    flights = view.get("flights") or []
    press = any(f.get("press") for f in flights)
    cdx, cdy = sp.move_offset(sp.CLAWD_MOVE.get(view["clawd_anim"], ""), t)
    cname = sp.clawd_frame(view["clawd_anim"], t)
    cw = 12 * cell
    cx, cy = ox + W - 10 - cw + cdx, feet - 8 * cell + cdy + (2 if press else 0)
    _sprite(p, dpr, ("clawd", cname), sp.CLAWD_FRAMES[cname], sp.CLAWD_PALETTE,
            cx, cy, cell)
    if "hat" in props:
        _sprite(p, dpr, ("hat",), sp.HAT, sp.CLAWD_PALETTE, cx, cy - 6 * cell, cell)

    # 手紙・受領のチェック(座標で飛ばす)。高さは Muse の胸のあたり。
    item_top = feet - max(6, round(mh * 0.72))
    fly_left = 8 + mw + 6
    fly_right = W - 10 - cw - 10 * item_cell - 4
    kind = view["item"]
    if kind in ("parked", "wobble"):
        # 状態の表現(承認待ちで止まった手紙・異常で揺れる手紙)は実態の
        # 表現なので残す。飛ぶのは手紙だけ。
        pos = item_position(kind, "letter", t, W, fly_left, fly_right)
        if pos is not None:
            p.setOpacity(max(0.0, min(1.0, pos[1])))
            dy = 4 + (-2 if kind == "parked" and t % 1000 >= 500 else 0)
            _sprite(p, dpr, ("item", "letter"), ITEM_GRIDS["letter"], ITEM_PALETTE,
                    ox + pos[0] + 1, item_top + dy, item_cell)
            p.setOpacity(1.0)
        _blink_text(p, "?", QRect(ox + W // 2 - 10, item_top - 22, 20, 20), accent, 16,
                    t % 800 < 400)
    # 実イベントの飛行。直近のイベントが無ければ何も飛ばない。同時に
    # 複数あるときは縦にずらして重ならないようにする。
    for i, flight in enumerate(flights):
        x, alpha = bev.flight_position(flight["progress"], flight["from"],
                                       fly_left, fly_right)
        grid_name = "letter" if flight["item"] == "letter" else "ack"
        p.setOpacity(max(0.0, min(1.0, alpha)))
        _sprite(p, dpr, ("item", grid_name), ITEM_GRIDS[grid_name], ITEM_PALETTE,
                ox + x + 1, item_top + i * 7 * item_cell, item_cell)
        p.setOpacity(1.0)

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


def _path_point(points, progress):
    """折れ線(points は (x, y) の列)の上を progress(0..1)だけ進んだ点。
    最終イメージのドットの移動(Manhattan 経路を等速で)と同じ考え方。"""
    if not points:
        return (0.0, 0.0)
    if len(points) == 1 or progress <= 0:
        return points[0]
    total = 0.0
    segs = []
    for a, b in zip(points, points[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        segs.append((a, b, d))
        total += d
    if total <= 0:
        return points[-1]
    rest = min(1.0, progress) * total
    for a, b, d in segs:
        if rest <= d:
            k = rest / d if d else 0.0
            return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k)
        rest -= d
    return points[-1]


# 最終イメージの場面(大きさ「大」専用)。座標はステージに対する割合で持つ
# (モック bridge-final-image.html のアンカー配置に対応)。
SCENE_ANCHORS = {
    "github": (0.50, 0.13), "post": (0.70, 0.13), "muse": (0.15, 0.42),
    "pc": (0.85, 0.42), "desk": (0.68, 0.70), "user": (0.38, 0.85),
}
WATCH_BASE = (0.83, 0.54)       # 見張り(フクロウ)の定位置(PC の足元)
WATCH_FAR = (0.17, 0.54)        # 見張りの往復の向こう側(Muse の足元)
WATCH_LOW = 0.62                # 往復路の下段の y(割合)


def _watcher_point(t, active, alert):
    """フクロウの居場所(割合座標)。動くのは Muse が動いている間だけ:
    PC の足元 → 下の点線路 → Muse の足元、をゆっくり往復する。
    異常のときは Muse 側で小刻みに揺れる。待機中は PC の足元で止まる。"""
    if alert:
        dx = -0.004 if t % 160 < 80 else 0.004
        return (WATCH_FAR[0] + dx, WATCH_FAR[1])
    if not active:
        return WATCH_BASE
    route = [WATCH_BASE, (WATCH_BASE[0], WATCH_LOW),
             (WATCH_FAR[0], WATCH_LOW), WATCH_FAR]
    c = t % 12000
    if c < 3000:
        return WATCH_BASE
    if c < 4500:
        return _path_frac(route, (c - 3000) / 1500.0)
    if c < 7500:
        return WATCH_FAR
    if c < 9000:
        return _path_frac(list(reversed(route)), (c - 7500) / 1500.0)
    return WATCH_BASE


def _path_frac(points, progress):
    """割合座標の折れ線上の点。長さは割合のまま測る(縦横比は無視する)。"""
    return _path_point([(float(x), float(y)) for x, y in points], progress)


def _paint_scene(p, dpr, view, st, accent, t, spec):
    """最終イメージの場面。家=PC(右のモニタ。家サーバはこの中)が中心で、
    Muse(左)・GitHub(上)・私書箱(上・観測)・窓口(右下)・あなたの席(下)と
    線でつながる。動きは実イベント駆動: 手紙の飛行は bridge_journal の記録、
    窓口のドットはプレイブック実行中だけ、見張りの往復は Muse の活動中だけ。"""
    W, H = st.width(), st.height()
    ox, oy = st.left(), st.top()

    def pt(frac):
        return (ox + W * frac[0], oy + H * frac[1])

    A = SCENE_ANCHORS
    # --- 線(実線=やり取りの経路、点線=鏡写しと見張りの往復路) ---
    solid = [
        [A["user"], (0.15, 0.85), A["muse"]],
        [A["user"], (0.85, 0.85), A["pc"]],
        [A["muse"], A["pc"]],
        [A["pc"], (0.85, 0.70), A["desk"]],
        [A["muse"], (0.15, 0.13), A["github"]],
        [A["pc"], (0.85, 0.13), A["github"]],
    ]
    p.setPen(QPen(QColor("#5a5d66"), 1))
    p.setBrush(Qt.NoBrush)
    for line in solid:
        p.drawPolyline(QPolygonF([QPointF(*pt(f)) for f in line]))
    mirror = [(0.85, 0.375), (0.64, 0.375), (0.64, 0.13), A["post"]]
    pen = QPen(QColor("#7a7e89"), 1)
    pen.setDashPattern([1, 2])
    p.setPen(pen)
    p.drawPolyline(QPolygonF([QPointF(*pt(f)) for f in mirror]))
    route = [WATCH_FAR, (WATCH_FAR[0], WATCH_LOW),
             (WATCH_BASE[0], WATCH_LOW), WATCH_BASE]
    p.drawPolyline(QPolygonF([QPointF(*pt(f)) for f in route]))
    p.setPen(Qt.NoPen)

    props = view["props"]
    flights = view.get("flights") or []
    press = any(f.get("press") for f in flights)

    # --- GitHub(上・中央) ---
    gh_cell = 1.6
    gx, gy = pt(A["github"])
    _sprite(p, dpr, ("scene", "github"), sp.GITHUB_GRID, sp.GITHUB_PALETTE,
            gx - 10 * gh_cell, gy - 10 * gh_cell, gh_cell)

    # --- 私書箱(上・GitHub の右)。旗は承認待ちの合図があるときだけ上がる ---
    post_cell = 2.0
    px_, py_ = pt(A["post"])
    post_left, post_top = px_ - 6 * post_cell, py_ - 6 * post_cell
    _sprite(p, dpr, ("scene", "post"), sp.POST_GRID, sp.POST_PALETTE,
            post_left, post_top, post_cell)
    if view.get("flag_up"):
        wave = 0 if t % 800 < 400 else 1
        p.fillRect(int(post_left + 15 + wave), int(post_top + 2), 7, 4,
                   QColor("#FF7847"))

    # --- Muse(左) ---
    muse_cell = 2.2
    mname = sp.muse_frame(view["muse_anim"], t)
    mrows = sp.muse_rows(mname)
    mw, mh = len(mrows[0]) * muse_cell, len(mrows) * muse_cell
    mdx, mdy = sp.move_offset(sp.MUSE_MOVE.get(view["muse_anim"], ""), t)
    mx, my = pt(A["muse"])
    m_left, m_top = mx - mw / 2 + mdx, my - mh / 2 + mdy
    _sprite(p, dpr, ("muse", mname), mrows, sp.MUSE_PALETTE,
            m_left, m_top, muse_cell)
    if "spark" in props:
        p.setOpacity(1.0 if t % 500 < 250 else 0.15)
        _sprite(p, dpr, ("item", "spark"), ITEM_GRIDS["spark"], ITEM_PALETTE,
                mx + (mw - 18) // 2, m_top - 7 * 2 - 2, 2)
        p.setOpacity(1.0)

    # --- PC=家(右)。モニタの中に Claw'd、左上に家の印 ---
    pc_cell = 2.2
    pcw, pch = len(sp.PC_GRID[0]) * pc_cell, len(sp.PC_GRID) * pc_cell
    pcx, pcy = pt(A["pc"])
    pc_left, pc_top = pcx - pcw / 2, pcy - pch / 2
    _sprite(p, dpr, ("scene", "pc"), sp.PC_GRID, sp.PC_PALETTE,
            pc_left, pc_top, pc_cell)
    _sprite(p, dpr, ("scene", "home"), sp.HOME_GRID, sp.HOME_PALETTE,
            pc_left - 9, pc_top - 9, 1.1)
    cname = sp.clawd_frame(view["clawd_anim"], t)
    c_cell = 1.6
    cdx, cdy = sp.move_offset(sp.CLAWD_MOVE.get(view["clawd_anim"], ""), t)
    claw_left = pc_left + 4 * pc_cell + (18 * pc_cell - 12 * c_cell) / 2 + cdx
    claw_top = pc_top + 2 * pc_cell + (10 * pc_cell - 8 * c_cell) / 2 + cdy \
        + (2 if press else 0)
    _sprite(p, dpr, ("clawd", cname), sp.CLAWD_FRAMES[cname], sp.CLAWD_PALETTE,
            claw_left, claw_top, c_cell)
    if "hat" in props:
        _sprite(p, dpr, ("hat",), sp.HAT, sp.CLAWD_PALETTE,
                claw_left, claw_top - 6 * c_cell, c_cell)
    if "dots" in props:
        for i in range(3):
            phase = ((t - i * 250) % 1000) / 1000.0
            p.fillRect(int(pcx - 12 + i * 9), int(pc_top - 12), 4, 4,
                       QColor(79, 224, 255, 255 if phase >= 0.3 else 38))
    if "bang" in props:
        _blink_text(p, "!", QRect(int(pc_left + pcw - 4), int(pc_top - 22), 14, 20),
                    QColor("#ff5a5f"), 16, t % 500 < 250)
    if "zz" in props:
        font = QFont("Consolas", 8)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#8fb4c2"))
        p.drawText(QRect(int(pc_left + pcw - 2), int(pc_top - 16), 40, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, "z z")

    # --- 窓口(右下)。プレイブック実行中だけ金のドットが往復する ---
    # ハートビートが古い (窓口が応答なし) ときはベルを薄くして出す。
    desk_cell = 2.2
    dx_, dy_ = pt(A["desk"])
    if view.get("desk_stale"):
        p.setOpacity(0.35)
    _sprite(p, dpr, ("scene", "desk"), sp.DESK_GRID, sp.DESK_PALETTE,
            dx_ - 6 * desk_cell, dy_ - 5 * desk_cell, desk_cell)
    p.setOpacity(1.0)
    if view.get("pb_active") and not view.get("desk_stale"):
        ph = (t % 1800) / 1800.0
        prog = 1.0 - abs(1.0 - 2.0 * ph)
        dot = _path_point([pt(A["pc"]), pt((0.85, 0.70)), pt(A["desk"])], prog)
        p.fillRect(int(dot[0] - 2), int(dot[1] - 2), 5, 5, QColor("#FFD166"))

    # --- 鏡写しのドット(家→私書箱。家経由のときだけ、ゆっくり) ---
    if view.get("route") == "home" and view["tone"] != "alert":
        prog = (t % 6000) / 6000.0
        if prog <= 0.4:
            dot = _path_point([pt(f) for f in mirror], prog / 0.4)
            p.fillRect(int(dot[0] - 2), int(dot[1] - 2), 4, 4,
                       QColor(79, 224, 255, 140))

    # --- 配備係(PC の足元に待機。PB 実行中は足踏み) ---
    dep_cell = 1.8
    dep_x, dep_y = pt((0.60, 0.575))
    if view.get("pb_active"):
        dep_rows = sp.DEPLOY_WALK_A if int(t // 320) % 2 else sp.DEPLOY_WALK_B
        dep_key = ("scene", "deploy-walk", dep_rows is sp.DEPLOY_WALK_A)
    else:
        dep_rows, dep_key = sp.DEPLOY_GRID, ("scene", "deploy")
    dep_dy = 0 if view.get("pb_active") else (0 if t % 1200 < 600 else -1)
    _sprite(p, dpr, dep_key, dep_rows, sp.DEPLOY_PALETTE,
            dep_x - 6 * dep_cell, dep_y - 5 * dep_cell + dep_dy, dep_cell)

    # --- 見張り(フクロウ)。下の点線路を往復する ---
    owl_cell = 1.8
    active = view["tone"] in ("work", "claude", "approval")
    ofrac = _watcher_point(t, active, view["tone"] == "alert")
    owx, owy = pt(ofrac)
    blink = 4900 < t % 5200 < 5060
    owl_rows = sp.WATCH_BLINK_GRID if blink else sp.WATCH_GRID
    _sprite(p, dpr, ("scene", "owl", blink), owl_rows, sp.WATCH_PALETTE,
            owx - 6 * owl_cell, owy - 5.5 * owl_cell, owl_cell)

    # --- あなたの席(下) ---
    yota_cell = 2.2
    ux, uy = pt(A["user"])
    _sprite(p, dpr, ("scene", "yota"), sp.YOTA_GRID, sp.YOTA_PALETTE,
            ux - 8 * yota_cell, uy - 7.5 * yota_cell, yota_cell)

    # --- 手紙(状態の表現と実イベントの飛行)。Muse と PC の間の線の上 ---
    item_cell = 2
    fly_left = W * 0.15 + 18
    fly_right = W * 0.85 - 48
    item_top = oy + H * 0.42 - 8
    kind = view["item"]
    if kind in ("parked", "wobble"):
        pos = item_position(kind, "letter", t, W, fly_left, fly_right)
        if pos is not None:
            p.setOpacity(max(0.0, min(1.0, pos[1])))
            dy2 = 4 + (-2 if kind == "parked" and t % 1000 >= 500 else 0)
            _sprite(p, dpr, ("item", "letter"), ITEM_GRIDS["letter"], ITEM_PALETTE,
                    ox + pos[0] + 1, item_top + dy2, item_cell)
            p.setOpacity(1.0)
        _blink_text(p, "?", QRect(ox + W // 2 - 10, int(item_top) - 22, 20, 20),
                    accent, 16, t % 800 < 400)
    for i, flight in enumerate(flights):
        x, alpha = bev.flight_position(flight["progress"], flight["from"],
                                       fly_left, fly_right)
        grid_name = "letter" if flight["item"] == "letter" else "ack"
        p.setOpacity(max(0.0, min(1.0, alpha)))
        _sprite(p, dpr, ("item", grid_name), ITEM_GRIDS[grid_name], ITEM_PALETTE,
                ox + x + 1, item_top + i * 7 * item_cell, item_cell)
        p.setOpacity(1.0)

    # --- 紙吹雪(完了) ---
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
