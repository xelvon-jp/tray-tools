# tests/test_bg_remove.py
# bg_remove.py の純関数(Qt に依存しない部分)を合成画像で確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_bg_remove.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (pytest があれば `pytest tests/test_bg_remove.py` でもそのまま拾える)。
# Qt の変換だけは QImage を作るので offscreen で動かす(窓は出さない)。
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

import bg_remove as br  # noqa: E402

WHITE = (255, 255, 255)
RED = (200, 30, 30)
BLUE = (30, 60, 200)


def _canvas(h, w, color=WHITE):
    return np.full((h, w, 3), color, dtype=np.uint8)


def _alpha(rgb, colors, tol=10, feather=0, mode=br.RANGE_CONNECTED, seeds=()):
    dist, _ = br.color_distance(rgb, colors)
    return br.compute_alpha(dist, tol, feather, mode, seeds)


def _subject_with_white_inside():
    """白背景に赤い円、その内側に白い円(白目や服のハイライトに相当)。"""
    img = _canvas(120, 160)
    cv2.circle(img, (80, 60), 45, RED, -1)
    cv2.circle(img, (80, 60), 15, WHITE, -1)
    return img


# ---------------------------------------------------------------
def test_estimate_background_picks_border_mode():
    rng = np.random.default_rng(0)
    img = _canvas(100, 100, (250, 250, 248))
    # 外周に JPEG 風のノイズ。量子化してから数えるので、同じ「白」として数えられるはず
    noise = rng.integers(-3, 4, size=img.shape)
    img = np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)
    # 被写体が外周に少し掛かっていても、多数派の背景が勝つ
    img[0:2, 0:30] = BLUE
    color = br.estimate_background(img)
    assert all(abs(c - t) <= 3 for c, t in zip(color, (250, 250, 248))), color


def test_estimate_background_ignores_transparent_pixels():
    img = _canvas(50, 50, (0, 0, 0))     # 透明部分に残っている見えない黒
    img[10:40, 10:40] = WHITE
    alpha = np.zeros((50, 50), dtype=np.uint8)
    alpha[1:49, 1:49] = 255
    img[1:49, 1:49] = (240, 240, 240)
    color = br.estimate_background(img, alpha, border=2)
    assert color == (240, 240, 240), color


def test_connected_keeps_inner_white_global_removes_it():
    img = _subject_with_white_inside()
    connected = _alpha(img, [WHITE], mode=br.RANGE_CONNECTED)
    global_ = _alpha(img, [WHITE], mode=br.RANGE_GLOBAL)
    # 外の背景はどちらでも抜ける
    assert connected[2, 2] == 0.0 and global_[2, 2] == 0.0
    # 被写体(赤)はどちらでも残る
    assert connected[60, 50] == 1.0 and global_[60, 50] == 1.0
    # 内側の白: connected なら残り、global なら抜ける
    assert connected[60, 80] == 1.0
    assert global_[60, 80] == 0.0


def test_seed_opens_enclosed_region_in_connected_mode():
    img = _subject_with_white_inside()
    alpha = _alpha(img, [WHITE], mode=br.RANGE_CONNECTED, seeds=[(80, 60)])
    assert alpha[60, 80] == 0.0       # 点を打った内側の白が抜ける
    assert alpha[60, 50] == 1.0       # 赤は残る
    assert alpha[2, 2] == 0.0         # 外も抜けたまま


def test_protect_keeps_inner_white_in_global_mode():
    """「抜く」(global)でも、Ctrl の残す点を打った内側の白は残る。"""
    img = _subject_with_white_inside()
    dist, _ = br.color_distance(img, [WHITE])
    alpha, rejected = br.compute_alpha(dist, 10, 0, br.RANGE_GLOBAL,
                                       protect=[(80, 60)], report=True)
    assert rejected == []
    assert alpha[60, 80] == 1.0      # 内側の白は残る
    assert alpha[2, 2] == 0.0        # 外の背景は抜けたまま
    assert alpha[60, 50] == 1.0      # 赤も残る


def test_protect_wins_over_seed_in_same_region():
    img = _subject_with_white_inside()
    dist, _ = br.color_distance(img, [WHITE])
    alpha = br.compute_alpha(dist, 10, 0, br.RANGE_CONNECTED,
                             seeds=[(80, 60)], protect=[(82, 60)])
    assert alpha[60, 80] == 1.0


def test_protect_on_outer_background_is_rejected():
    """外周につながる背景の上の残す点は効かせず、効かなかったと報告する。"""
    img = _subject_with_white_inside()
    dist, _ = br.color_distance(img, [WHITE])
    protect = [(80, 60), (3, 3), (60, 60)]   # 内側の白 / 外の背景 / 赤(候補外)
    for mode in (br.RANGE_CONNECTED, br.RANGE_GLOBAL):
        alpha, rejected = br.compute_alpha(dist, 10, 0, mode, protect=protect, report=True)
        assert rejected == [1], (mode, rejected)   # 候補外の (60,60) は黙って無視
        assert alpha[3, 3] == 0.0                  # 外の背景は抜けたまま
        assert alpha[60, 80] == 1.0
    # report を付けなければ従来どおり配列だけが返る
    assert isinstance(br.compute_alpha(dist, 10, 0, protect=protect), np.ndarray)


def test_protect_point_scaled_for_preview():
    """原寸で打った残す点を縮小プレビューの座標へ換算しても、同じ部分を守れること。"""
    big = _canvas(1200, 1600)
    cv2.circle(big, (800, 600), 450, RED, -1)
    cv2.circle(big, (800, 600), 40, WHITE, -1)      # 縮めると半径10pxの小さな白
    small, scale = br.resize_long_side(big, 400)
    assert abs(scale - 0.25) < 1e-9
    assert br.scale_points([(803, 599)], scale) == [(200, 149)]
    assert br.scale_points([(5, 7)], 1.0) == [(5, 7)]
    dist, _ = br.color_distance(small, [WHITE])
    alpha, rejected = br.compute_alpha(
        dist, 10, 0, br.RANGE_GLOBAL,
        protect=br.scale_points([(803, 599)], scale), report=True)
    assert rejected == []
    assert alpha[150, 200] == 1.0 and alpha[2, 2] == 0.0
    # 原寸の座標をそのまま渡すと、縮小画像の外を指して効かない(換算が必要な理由)
    alpha_raw = br.compute_alpha(dist, 10, 0, br.RANGE_GLOBAL, protect=[(803, 599)])
    assert alpha_raw[150, 200] == 0.0


# ---- AIの結果の補正(refine_ai_alpha) ----
def _ai_scene():
    """AIの判定を模した不透明度: 外は0、半透明(0.5)の白い被写体、その足元に薄い影(0.2)、
    隣に不透明(1.0)の被写体。半透明の被写体と不透明の被写体は接していて、影は不透明の
    被写体の下にも続いている(不透明な被写体を橋にして影まで残らないことを見る)。"""
    a = np.zeros((100, 160), dtype=np.float32)
    a[20:60, 20:70] = 0.5      # 半透明になった白い牛
    a[20:60, 70:110] = 1.0     # 不透明な黒い牛
    a[60:70, 20:110] = 0.2     # 足元の影
    a[30:40, 120:140] = 0.5    # 離れた所にある別の半透明
    return a


def test_ai_lo_hi_makes_translucent_subject_opaque():
    a = _ai_scene()
    out = br.refine_ai_alpha(a, lo=0.3, hi=0.5)
    assert out[40, 40] >= 0.999        # 0.5 → 不透明(割り算の丸めで 0.99999994 になりうる)
    assert out[65, 40] == 0.0          # 影 0.2 < lo → 消える
    assert out[5, 5] == 0.0
    same = br.refine_ai_alpha(a)       # 既定(0〜1)なら何もしない
    assert np.array_equal(same, a)


def test_ai_protect_point_makes_component_opaque_without_shadow():
    a = _ai_scene()
    out, rejected, _ = br.refine_ai_alpha(a, protect=[(40, 40)], report=True)
    assert rejected == []
    assert out[40, 40] == 1.0 and out[25, 25] == 1.0   # 成分の内側は不透明
    assert out[66, 40] < 0.3          # 影は(縁のなじませ以外)残らない
    assert out[66, 90] < 0.3          # 不透明な牛を橋にして影が残ることもない
    assert out[35, 130] == 0.5        # 離れた別の半透明は触らない
    # 不透明な所に打った点でも、そこに接している半透明の部分が戻る
    out2 = br.refine_ai_alpha(a, protect=[(90, 40)])
    assert out2[40, 40] == 1.0 and out2[66, 40] < 0.3


def test_ai_protect_on_background_is_rejected():
    a = _ai_scene()
    out, rejected, _ = br.refine_ai_alpha(a, protect=[(5, 5), (40, 40)], report=True)
    assert rejected == [0]             # a ≈ 0 の点は効かない
    assert out[5, 5] == 0.0


def test_ai_protect_huge_component_is_rejected():
    a = np.full((100, 100), 0.3, dtype=np.float32)   # 画面いっぱいのもや
    a[40:60, 40:60] = 0.6
    out, rejected, _ = br.refine_ai_alpha(a, protect=[(5, 5)], report=True)
    assert rejected == [0]
    assert np.array_equal(out, a)


def test_ai_seed_clears_enclosed_gap():
    a = np.zeros((100, 100), dtype=np.float32)
    a[20:80, 20:80] = 1.0
    a[40:60, 40:60] = 0.6              # 脚の間などに残った薄い判定(被写体に囲まれている)
    out, _, seed_rejected = br.refine_ai_alpha(a, seeds=[(50, 50), (5, 5)], report=True)
    assert seed_rejected == [1]        # 外側の背景の上の抜く点は効かない(と報告)
    assert out[50, 50] < 0.01
    assert out[30, 30] == 1.0          # 被写体本体は残る
    assert 0.5 < out[40, 39] < 1.0     # 隙間に接する縁はなじませる(削りすぎない)


def test_ai_protect_wins_over_seed():
    a = np.zeros((100, 100), dtype=np.float32)
    a[20:80, 20:80] = 1.0
    a[40:60, 40:60] = 0.6
    out = br.refine_ai_alpha(a, seeds=[(50, 50)], protect=[(52, 52)])
    # 抜く点で 0 になった後に残す点が来ると、そこは a ≈ 0 なので効かない。
    # 同じ場所に両方打つのは矛盾した指定なので、どちらかに倒れて壊れなければよい。
    assert out[30, 30] == 1.0


def test_ai_points_scaled_for_preview():
    big = np.zeros((800, 1600), dtype=np.float32)
    big[200:600, 200:700] = 0.5
    small, scale = br.resize_long_side(big, 400)
    out, rejected, _ = br.refine_ai_alpha(
        small, protect=br.scale_points([(450, 400)], scale), report=True)
    assert rejected == []
    assert out[100, 112] == 1.0


# ---- AI の図解: 白っぽい背景に枠線の無い薄いボックス(残す色・自動の許容量・ムラへの追従) ----
DIAGRAM_BOXES = {
    # 名前: (x0, y0, x1, y1, 角の半径, 塗り, 中の点)
    "blue": (150, 150, 650, 400, 36, (231, 238, 250), (400, 300)),
    "yellow": (720, 420, 1100, 680, 30, (253, 246, 214), (900, 450)),
    "faint": (760, 120, 1080, 330, 28, (240, 243, 250), (940, 160)),
}
_diagram_cache = {}


def _diagram():
    """オフホワイト(#f6f5f1)にノイズ(±ΔE 1〜2)と四隅で10%暗くなるビネットを乗せ、
    枠線の無い角丸のボックス(薄い青・薄い黄・ごく薄い青)に濃い文字を入れて、JPEG で
    圧縮してから読み直した画像。AI が生成した図解の背景を模している。"""
    if "rgb" in _diagram_cache:
        return _diagram_cache["rgb"]
    import io

    from PIL import Image, ImageDraw, ImageFont

    w, h = 1200, 800
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / np.sqrt(2)
    base = np.array([246, 245, 241], np.float32)[None, None, :] * (1.0 - 0.10 * r ** 2)[..., None]
    noisy = base + rng.normal(0, 1.2, (h, w, 1)) + rng.normal(0, 0.6, (h, w, 3))
    image = Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    for name, (x0, y0, x1, y1, radius, fill, _pt) in DIAGRAM_BOXES.items():
        draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill)
        draw.text((x0 + 50, (y0 + y1) // 2 - 30), name.capitalize(), fill=(30, 40, 70), font=font)
    draw.text((160, 600), "plain text", fill=(40, 40, 40), font=font)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    buffer.seek(0)
    rgb = np.array(Image.open(buffer).convert("RGB"))
    _diagram_cache["rgb"] = rgb
    return rgb


def _box_mask(shape, box, inset):
    """角丸のボックスの内側(inset > 0)/外側に広げた範囲(inset < 0)のマスク。"""
    x0, y0, x1, y1, radius = box[:5]
    mask = np.zeros(shape[:2], np.uint8)
    rr = max(radius - inset, 1)
    cv2.rectangle(mask, (x0 + inset + radius, y0 + inset), (x1 - inset - radius, y1 - inset), 1, -1)
    cv2.rectangle(mask, (x0 + inset, y0 + inset + radius), (x1 - inset, y1 - inset - radius), 1, -1)
    for cx, cy in ((x0 + radius, y0 + radius), (x1 - radius, y1 - radius),
                   (x0 + radius, y1 - radius), (x1 - radius, y0 + radius)):
        cv2.circle(mask, (cx, cy), rr, 1, -1)
    return mask.astype(bool)


def _text_mask(rgb):
    """濃い文字の画素(とその周り)。背景の判定から外すのに使う。"""
    dark = (rgb.astype(int).sum(axis=2) < 450).astype(np.uint8)
    return cv2.dilate(dark, np.ones((9, 9), np.uint8)).astype(bool)


def _diagram_alpha(keep_names=(), follow=True, tol=None, feather=2):
    rgb = _diagram()
    h, w = rgb.shape[:2]
    bg = br.estimate_background(rgb)
    points = [DIAGRAM_BOXES[n][6] for n in keep_names]
    keep = [tuple(int(c) for c in rgb[y, x]) for x, y in points]
    coeffs = br.fit_background_offset(rgb, [bg], keep) if follow else None
    offset = br.background_offset(coeffs, h, w)
    if tol is None:
        tol = br.auto_tolerance(rgb, [bg], offset)
    dist, index = br.color_distance(rgb, [bg], offset)
    keep_dist = None
    if keep:
        offsets = ([br.offset_at(coeffs, x, y, h, w) for x, y in points]
                   if coeffs is not None else None)
        keep_dist, _ = br.color_distance(rgb, keep, offset, offsets)
    alpha = br.compute_alpha(dist, tol, feather, keep_dist=keep_dist,
                             protect=points, report=False)
    background = br.local_background_rgb([bg], index, offset) if offset is not None else None
    out = br.compose_rgba(rgb, alpha, [bg], index, True, background=background)
    return rgb, out, tol, bg


def test_auto_tolerance_measures_border_noise():
    rgb = _diagram()
    bg = br.estimate_background(rgb)
    h, w = rgb.shape[:2]
    offset = br.background_offset(br.fit_background_offset(rgb, [bg]), h, w)
    tol = br.auto_tolerance(rgb, [bg], offset)
    # ムラ(ノイズ ±ΔE 1〜2 + JPEG)の上限に少し足した程度。以前の固定値 12 よりずっと小さい
    assert 2 <= tol <= 8, tol
    # 外周に被写体が掛かっていても、外れ値として除かれて大きく振れない
    covered = rgb.copy()
    covered[:, :40] = (20, 30, 160)
    assert abs(br.auto_tolerance(covered, [bg], offset) - tol) <= 2


def test_gradient_fit_follows_vignette_without_being_pulled_by_boxes():
    rgb = _diagram()
    bg = br.estimate_background(rgb)
    h, w = rgb.shape[:2]
    flat, _ = br.color_distance(rgb, [bg])
    offset = br.background_offset(br.fit_background_offset(rgb, [bg]), h, w)
    fitted, _ = br.color_distance(rgb, [bg], offset)
    center = (slice(380, 420), slice(670, 710))      # 箱の無い中央の背景
    corner = (slice(5, 45), slice(5, 45))
    # 一定の色では、外周から推定した背景色に対して中央が ΔE 4 以上離れる(ビネット)
    assert np.percentile(flat[center], 50) > 4
    # 追従すると中央も四隅も、ノイズ程度の差に収まる
    assert np.percentile(fitted[center], 99) < 4
    assert np.percentile(fitted[corner], 99) < 4
    # 大きな薄い箱に面が引っ張られていない(箱と背景の差が保たれる)
    x, y = DIAGRAM_BOXES["blue"][6]
    assert fitted[y, x] > 7, fitted[y, x]


def test_keep_colors_make_boxes_opaque_and_background_clear():
    rgb, out, _tol, _bg = _diagram_alpha(keep_names=("blue", "yellow", "faint"))
    text = _text_mask(rgb)
    for name, box in DIAGRAM_BOXES.items():
        inside = _box_mask(rgb.shape, box, 6)
        assert out[inside, 3].min() == 255, name          # 箱の内側は完全に不透明(文字も)
    background = np.ones(rgb.shape[:2], bool)
    for box in DIAGRAM_BOXES.values():
        background &= ~_box_mask(rgb.shape, box, -12)
    background &= ~text
    assert (out[background, 3] == 0).mean() > 0.9999       # 背景は 0(JPEG のごく一部の点を除き)
    # 箱の外の文字も残る
    assert out[620, 175:400, 3].max() == 255


def test_keep_colors_no_white_halo_on_box_edges():
    """箱の縁に白いハローが出ない(黒地に置いたときに、縁が箱の色より明るく光らない)。"""
    rgb, out, _tol, _bg = _diagram_alpha(keep_names=("blue", "yellow", "faint"))
    pre = out[..., :3].astype(np.float32) * (out[..., 3:] / 255.0)   # 黒地に置いた見た目
    for name, box in DIAGRAM_BOXES.items():
        x, y = box[6]
        box_lum = rgb[y, x].astype(np.float32).mean()
        ring = _box_mask(rgb.shape, box, -4) & ~_box_mask(rgb.shape, box, 6)
        ideal = box_lum * out[ring, 3] / 255.0
        excess = pre[ring].mean(axis=1) - ideal
        # 0〜255 の明るさで 12 以内(JPEG のリンギングとノイズのぶん)。背景色(白)が縁に
        # 不透明で残ると、ここが 20〜30 になる。
        assert excess.max() < 12, (name, float(excess.max()))


def test_auto_tolerance_alone_keeps_visible_boxes():
    """残す色を指定しなくても、自動の許容量だけで ΔE 7 以上の箱は残る。"""
    rgb, out, tol, _bg = _diagram_alpha(keep_names=())
    for name in ("blue", "yellow", "faint"):
        inside = _box_mask(rgb.shape, DIAGRAM_BOXES[name], 6)
        assert (out[inside, 3] == 255).mean() > 0.99, (name, tol)


def test_keep_color_separates_box_connected_to_background():
    """許容量が大きくて箱が背景とつながっていても、残す色で切り離せる。そのとき
    「外側の背景とつながっている」という警告(rejected)は出さない。"""
    rgb = _diagram()
    bg = br.estimate_background(rgb)
    h, w = rgb.shape[:2]
    offset = br.background_offset(br.fit_background_offset(rgb, [bg]), h, w)
    dist, _ = br.color_distance(rgb, [bg], offset)
    point = DIAGRAM_BOXES["blue"][6]
    # 以前の既定(許容量12・ぼかし10)では箱ごと候補になり、点は外周とつながって効かない
    alpha, rejected = br.compute_alpha(dist, 12, 10, protect=[point], report=True)
    assert rejected == [0]
    assert alpha[point[1], point[0]] < 0.5
    keep = [tuple(int(c) for c in rgb[point[1], point[0]])]
    keep_dist, _ = br.color_distance(rgb, keep, offset)
    alpha, rejected = br.compute_alpha(dist, 12, 10, protect=[point], report=True,
                                       keep_dist=keep_dist)
    assert rejected == []
    inside = _box_mask(rgb.shape, DIAGRAM_BOXES["blue"], 6)
    assert alpha[inside].min() == 1.0
    for mode in (br.RANGE_CONNECTED, br.RANGE_GLOBAL):
        alpha = br.compute_alpha(dist, 12, 10, mode, keep_dist=keep_dist)
        # 許容量+ぼかしが ΔE 22 もあると、箱の中の文字の縁が数画素だけ 0.997 になる
        # (中央値で均した r が 0.5 をわずかに下回る)。8bit にすれば 254 で見えない。
        assert alpha[inside].min() >= 0.99, mode
        assert alpha[30, 600] == 0.0, mode


def test_keep_color_ramp_matches_mixture_ratio():
    """背景と残す色が t : 1−t で混ざった境目の画素は、およそ t の不透明度になる。"""
    bg = np.array([246, 245, 241], np.float32)
    box = np.array([200, 215, 245], np.float32)
    t = np.linspace(0, 1, 21, dtype=np.float32)
    row = np.rint(t[:, None] * box + (1 - t[:, None]) * bg).astype(np.uint8)[None, :, :]
    row = np.repeat(row, 7, axis=0)       # 中央値フィルタ(5×5)が掛かる高さにする
    dist, _ = br.color_distance(row, [tuple(int(c) for c in bg)])
    keep_dist, _ = br.color_distance(row, [tuple(int(c) for c in box)])
    alpha = br.compute_alpha(dist, 100, 0, br.RANGE_GLOBAL, keep_dist=keep_dist)[3]
    assert alpha[0] == 0.0 and alpha[-1] == 1.0
    assert (np.diff(alpha) >= -1e-6).all()                 # 単調に増える
    partial = np.flatnonzero((alpha > 0) & (alpha < 1))
    assert len(partial) >= 3                               # 境目は段差ではなく半透明でつなぐ
    assert 0.2 <= t[partial[0]] and t[partial[-1]] <= 0.6  # 半透明になるのは中間の混ざりだけ
    assert alpha[3] == 0.0 and alpha[15] == 1.0            # 端の近くは揺れても振り切る


def _keep_from_point(rgb, point, tol, follow=True):
    """UI と同じ手順: 拾った色が使えるか判定し、使える残す色だけで抜く。"""
    h, w = rgb.shape[:2]
    bg = br.estimate_background(rgb)
    base = br.fit_background_offset(rgb, [bg]) if follow else None
    noise = br.background_noise(rgb, [bg], br.background_offset(base, h, w))
    keep = [tuple(int(c) for c in rgb[point[1], point[0]])]
    usable, distances = br.keep_color_usable(keep, [point], [bg], noise, base, h, w)
    active = [c for c, ok in zip(keep, usable) if ok]
    coeffs = br.fit_background_offset(rgb, [bg], active) if follow else None
    offset = br.background_offset(coeffs, h, w)
    dist, _ = br.color_distance(rgb, [bg], offset)
    keep_dist = None
    if active:
        offsets = [br.offset_at(coeffs, *point, h, w)] if coeffs is not None else None
        keep_dist, _ = br.color_distance(rgb, active, offset, offsets)
    alpha = br.compute_alpha(dist, tol, 2, keep_dist=keep_dist,
                             protect=[point] if active else [])
    return usable, distances, alpha


def _plain_background_mask(rgb):
    background = np.ones(rgb.shape[:2], bool)
    for box in DIAGRAM_BOXES.values():
        background &= ~_box_mask(rgb.shape, box, -12)
    return background & ~_text_mask(rgb)


def test_keep_point_on_background_is_ignored_at_any_tolerance():
    """背景そのものを Ctrl+クリックしても、許容量によらず使わない(背景がまだらに残らない)。"""
    rgb = _diagram()
    background = _plain_background_mask(rgb)
    for point in ((600, 60), (30, 30), (1150, 760)):     # 中央の上・四隅(ビネットで暗い所)
        for tol in (0, 2, 12, 40):
            usable, distances, alpha = _keep_from_point(rgb, point, tol)
            assert usable == [False], (point, tol, distances)
            if tol >= 2:
                assert (alpha[background] == 0.0).mean() > 0.9999, (point, tol)
    # 比べ: 判定を通さずにそのまま残す色にすると、背景の画素が点々と残る
    # (r を中央値で均してあるのでこの素材では 1% 前後。ノイズの多い実画像ではもっと多い)
    h, w = rgb.shape[:2]
    bg = br.estimate_background(rgb)
    offset = br.background_offset(br.fit_background_offset(rgb, [bg]), h, w)
    dist, _ = br.color_distance(rgb, [bg], offset)
    keep_dist, _ = br.color_distance(rgb, [tuple(int(c) for c in rgb[60, 600])], offset)
    raw = br.compute_alpha(dist, 2, 2, keep_dist=keep_dist)
    assert (raw[background] > 0).mean() > 0.005


def test_keep_color_of_faint_box_stays_usable_at_high_tolerance():
    """残す色は許容量の内側に入った薄い箱を救うためのもの。許容量を上げても無効にしない。"""
    rgb = _diagram()
    point = DIAGRAM_BOXES["faint"][6]
    inside = _box_mask(rgb.shape, DIAGRAM_BOXES["faint"], 6)
    background = _plain_background_mask(rgb)
    for tol in (2, 6, 12):
        usable, distances, alpha = _keep_from_point(rgb, point, tol)
        assert usable == [True], (tol, distances)
        assert alpha[inside].min() == 1.0, tol
        assert (alpha[background] == 0.0).mean() > 0.9999, tol
    # 残す色が無ければ、許容量 12 ではこの箱(背景との差 ΔE 6 前後)は抜けてしまう
    _usable, _d, bare = _keep_from_point(rgb, (600, 60), 12)
    assert bare[inside].mean() < 0.5


def test_keep_color_threshold_uses_noise_not_tolerance():
    bg = (246, 245, 241)
    keep = (232, 236, 246)                                 # 背景から ΔE 6〜7 の薄い青
    assert br.keep_color_usable([keep], [(0, 0)], [bg], noise=1.5)[0] == [True]
    # ムラが大きい画像では、その内側の色は背景と見分けられないので使わない
    assert br.keep_color_usable([keep], [(0, 0)], [bg], noise=8.0)[0] == [False]
    # 下限 KEEP_MIN_DE: ムラが測れない(0)ときでも、背景から ΔE 3 以内の色は使わない
    near = (244, 244, 241)
    usable, d = br.keep_color_usable([near], [(0, 0)], [bg], noise=0.0)
    assert d[0] <= br.KEEP_MIN_DE and usable == [False]


def test_keep_color_equal_to_background_does_nothing():
    rgb = _canvas(20, 20, (240, 240, 240))
    dist, _ = br.color_distance(rgb, [(240, 240, 240)])
    keep_dist, _ = br.color_distance(rgb, [(240, 240, 240)])
    alpha = br.compute_alpha(dist, 5, 2, keep_dist=keep_dist)
    assert (alpha == 0.0).all()


# ---- アイコンへの分割と GVML ----
def _icon_sheet():
    """透過済みのアイコン一覧を模した RGBA。2行×3列のアイコン、そのうち1つは下に離れた
    ラベル(本体から 8px 下)付き、1つは右隣へ少しはみ出す飾り付き。ほかに 2×2 の点(ゴミ)。"""
    rgba = np.zeros((300, 420, 4), np.uint8)
    def blob(x0, y0, x1, y1, color):
        rgba[y0:y1, x0:x1, :3] = color
        rgba[y0:y1, x0:x1, 3] = 255
    # 1行目(上端をずらしても同じ行として扱われること)
    blob(20, 30, 100, 110, (200, 30, 30))      # 1
    blob(160, 20, 240, 100, (30, 160, 30))     # 2
    blob(300, 40, 380, 120, (30, 30, 200))     # 3
    blob(160, 108, 240, 118, (30, 160, 30))    # 2 のラベル(本体の 8px 下)
    # 2行目
    blob(20, 180, 100, 260, (200, 200, 30))    # 4
    blob(160, 180, 240, 260, (30, 200, 200))   # 5
    blob(300, 180, 380, 260, (200, 30, 200))   # 6
    blob(240, 200, 256, 210, (30, 200, 200))   # 5 の飾り(6 の箱の左端には届かない)
    blob(400, 290, 402, 292, (0, 0, 0))        # ゴミ
    return rgba


def test_split_icons_groups_parts_drops_noise_and_orders():
    rgba = _icon_sheet()
    icons = br.split_icons(rgba[..., 3], merge_px=12, min_area=16)
    assert len(icons) == 6, [i["box"] for i in icons]            # ゴミは捨てる
    boxes = [i["box"] for i in icons]
    # 読み順: 1行目が左から 1, 2, 3、2行目が 4, 5, 6
    assert [b[0] for b in boxes] == [20, 160, 300, 20, 160, 300]
    assert boxes[0][1] == 30 and boxes[3][1] == 180
    # 離れたラベルは本体と1つにまとまり、外接矩形は膨らませる前の画素で決まる
    assert boxes[1] == (160, 20, 240, 118)
    # 飾りを含む 5 の箱
    assert boxes[4] == (160, 180, 256, 260)
    # まとめる距離が 0 なら、離れたラベルは別のアイコンになる(5 の飾りは本体に接しているので別にならない)
    separate = br.split_icons(rgba[..., 3], merge_px=0, min_area=16)
    assert len(separate) == 7


def test_split_icons_clears_neighbor_overhang():
    """外接矩形に隣のアイコンが入り込んでいても、切り出すとそこは透明になる。"""
    rgba = np.zeros((100, 100, 4), np.uint8)
    rgba[..., :3] = 128
    rgba[10:90, 10:30, 3] = 255             # A: L字の縦棒
    rgba[70:90, 10:90, 3] = 255             # A: L字の足
    rgba[10:50, 50:90, 3] = 200             # B: L字のくぼみに置いた四角(A から 20px 離す)
    icons = br.split_icons(rgba[..., 3], merge_px=5, min_area=16)
    assert len(icons) == 2
    a, b = icons
    assert a["box"] == (10, 10, 90, 90)     # A の外接矩形は B を丸ごと含む
    assert b["box"] == (50, 10, 90, 50)
    (ax, ay, piece_a), (bx, by, piece_b) = br.cut_icons(rgba, icons)
    assert (ax, ay) == (10, 10) and piece_a.shape[:2] == (80, 80)
    assert piece_a[0:40, 40:80, 3].max() == 0      # A の切り出しに B の画素は入らない
    assert piece_a[0:80, 0:20, 3].min() == 255     # A 自身(縦棒)は残る
    assert piece_a[60:80, 0:80, 3].min() == 255    # A 自身(足)も残る
    assert piece_b[..., 3].min() == 200            # B はそのまま(半透明も保つ)


def test_split_icons_handles_empty_and_threshold():
    assert br.split_icons(np.zeros((10, 10), np.uint8)) == []
    faint = np.full((40, 40), 8, np.uint8)   # 8 以下は「中身」とみなさない
    assert br.split_icons(faint) == []


def test_build_gvml_structure():
    import zipfile
    import io as _io
    import re as _re

    rgba = _icon_sheet()
    icons = br.split_icons(rgba[..., 3], merge_px=12, min_area=16)
    data = br.gvml_from_rgba(rgba, icons)
    archive = zipfile.ZipFile(_io.BytesIO(data))
    names = archive.namelist()
    for required in ("[Content_Types].xml", "_rels/.rels", "clipboard/drawings/drawing1.xml",
                     "clipboard/drawings/_rels/drawing1.xml.rels"):
        assert required in names, names
    media = sorted(n for n in names if n.startswith("clipboard/media/"))
    assert media == [f"clipboard/media/image{i}.png" for i in range(1, 7)]

    types = archive.read("[Content_Types].xml").decode()
    assert 'Extension="png"' in types and 'PartName="/clipboard/drawings/drawing1.xml"' in types
    root = archive.read("_rels/.rels").decode()
    assert 'Target="clipboard/drawings/drawing1.xml"' in root and "relationships/drawing" in root

    drawing = archive.read("clipboard/drawings/drawing1.xml").decode()
    assert "drawingml/2006/lockedCanvas" in drawing
    ids = [int(v) for v in _re.findall(r'<a:cNvPr id="(\d+)"', drawing)]
    assert ids == [0, 2, 3, 4, 5, 6, 7]                      # グループが 0、図は 2 からの連番
    names_in = _re.findall(r'<a:cNvPr id="\d+" name="([^"]*)"', drawing)[1:]
    assert names_in == [f"icon {i}" for i in range(1, 7)]
    embeds = _re.findall(r'r:embed="(rId\d+)"', drawing)
    assert embeds == [f"rId{i}" for i in range(1, 7)]

    rels = archive.read("clipboard/drawings/_rels/drawing1.xml.rels").decode()
    pairs = dict(_re.findall(r'Id="(rId\d+)"[^>]*Target="\.\./media/(image\d+\.png)"', rels))
    assert pairs == {f"rId{i}": f"image{i}.png" for i in range(1, 7)}

    # EMU の換算: 1px = 9525 EMU。位置は左上のアイコン(20, 20)を原点にずらす
    offs = [tuple(int(v) for v in m) for m in _re.findall(r'<a:off x="(\d+)" y="(\d+)"/>', drawing)]
    exts = [tuple(int(v) for v in m) for m in _re.findall(r'<a:ext cx="(\d+)" cy="(\d+)"/>', drawing)]
    assert offs[0] == (0, 0)                                 # グループ
    boxes = [i["box"] for i in icons]
    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    for k, (x0, y0, x1, y1) in enumerate(boxes, 1):
        assert offs[k] == ((x0 - left) * 9525, (y0 - top) * 9525)
        assert exts[k] == ((x1 - x0) * 9525, (y1 - y0) * 9525)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)
    assert exts[0] == ((right - left) * 9525, (bottom - top) * 9525)

    # 中の PNG はアルファ付きで、切り出したアイコンと同じ大きさ
    png = np.frombuffer(archive.read("clipboard/media/image2.png"), np.uint8)
    decoded = cv2.imdecode(png, cv2.IMREAD_UNCHANGED)
    assert decoded.shape == (98, 80, 4)
    assert decoded[..., 3].max() == 255 and decoded[..., 3].min() == 0


def test_theme_contrast_ratios():
    """配色の文字色と地の色の組み合わせが、決めたコントラスト比を満たす。
    本文・補足・警告は 4.5 以上、無効の文字は 3.0 以上(bg_remove.CONTRAST_REQUIREMENTS)。"""
    assert abs(br.contrast_ratio("#ffffff", "#000000") - 21.0) < 1e-6
    assert abs(br.contrast_ratio("#777777", "#777777") - 1.0) < 1e-6
    failures = []
    for fg, bg, minimum in br.CONTRAST_REQUIREMENTS:
        ratio = br.contrast_ratio(br.THEME[fg], br.THEME[bg])
        if ratio < minimum:
            failures.append(f"{fg} on {bg}: {ratio:.2f} < {minimum}")
    assert not failures, failures
    # 補足説明・無効は、どの地(窓・パネル・入力欄)に置かれても基準を満たすこと
    for fg, minimum in (("muted", 4.5), ("disabled", 3.0)):
        for bg in ("bg", "panel", "field"):
            assert (fg, bg, minimum) in br.CONTRAST_REQUIREMENTS, (fg, bg)


def test_fake_checkerboard_needs_both_colors():
    """AI が「透過背景」のつもりで描き込んだ偽の市松模様(2色)。"""
    h, w, tile = 96, 128, 8
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((yy // tile + xx // tile) % 2).astype(bool)
    img = _canvas(h, w, WHITE)
    img[checker] = (204, 204, 204)
    img[30:70, 40:90] = BLUE

    one = _alpha(img, [WHITE], tol=5)
    # 白だけ指定しても灰色のマスが残る(ここが偽市松の困るところ)
    background = np.ones((h, w), dtype=bool)
    background[30:70, 40:90] = False
    assert (one[background] == 1.0).any()

    both = _alpha(img, [WHITE, (204, 204, 204)], tol=5)
    assert (both[background] == 0.0).all()
    assert (both[30:70, 40:90] == 1.0).all()


def test_feather_makes_linear_ramp():
    dist = np.array([[0.0, 10.0, 15.0, 20.0, 30.0]], dtype=np.float32)
    alpha = br.compute_alpha(dist, tolerance=10, feather=10, mode=br.RANGE_GLOBAL)
    np.testing.assert_allclose(alpha[0], [0.0, 0.0, 0.5, 1.0, 1.0], atol=1e-6)


def test_decontaminate_recovers_exact_mixture():
    fg = np.array(RED, dtype=np.float32)
    bg = np.array(WHITE, dtype=np.float32)
    a = np.array([[0.25, 0.5, 0.75, 1.0]], dtype=np.float32)
    observed = np.rint(a[..., None] * fg + (1 - a[..., None]) * bg).astype(np.uint8)
    fixed = br.decontaminate(observed, a, [WHITE])
    # 半透明の画素は前景色に戻る(丸めのぶん ±2 まで)
    assert np.abs(fixed[0, :3].astype(int) - np.array(RED)).max() <= 2, fixed
    # 不透明の画素は触らない
    assert tuple(fixed[0, 3]) == tuple(observed[0, 3])


def test_decontaminate_uses_nearest_of_multiple_colors():
    a = np.array([[0.5, 0.5]], dtype=np.float32)
    observed = np.array([[[128, 15, 15], [100, 15, 115]]], dtype=np.uint8)
    # 1つ目は 赤(200,30,30)と黒の半々 → 黒が近い。2つ目は 赤と青(0,0,200)の半々
    colors = [(0, 0, 0), (0, 0, 200)]
    index = np.array([[0, 1]], dtype=np.int32)
    fixed = br.decontaminate(observed, a, colors, index)
    assert np.abs(fixed[0, 0].astype(int) - np.array([255, 30, 30])).max() <= 2, fixed
    assert np.abs(fixed[0, 1].astype(int) - np.array([200, 30, 30])).max() <= 2, fixed


def test_decontaminate_reduces_halo_on_antialiased_edge():
    """アンチエイリアスの縁で、色かぶり除去が白い縁(ハロー)を減らすこと。"""
    big = _canvas(400, 400)
    cv2.circle(big, (200, 200), 120, RED, -1, lineType=cv2.LINE_AA)
    img = cv2.resize(big, (100, 100), interpolation=cv2.INTER_AREA)   # 縁が中間色になる
    dist, index = br.color_distance(img, [WHITE])
    alpha = br.compute_alpha(dist, tolerance=5, feather=60, mode=br.RANGE_CONNECTED)
    partial = (alpha > 0.05) & (alpha < 0.95)
    assert partial.sum() > 20, partial.sum()

    raw = br.compose_rgba(img, alpha, [WHITE], index, decontaminate_edges=False)
    clean = br.compose_rgba(img, alpha, [WHITE], index, decontaminate_edges=True)
    target = np.array(RED, dtype=float)
    err_raw = np.abs(raw[..., :3][partial].astype(float) - target).mean()
    err_clean = np.abs(clean[..., :3][partial].astype(float) - target).mean()
    assert err_clean < err_raw * 0.5, (err_raw, err_clean)


def test_trim_box_and_compose_trim():
    alpha = np.zeros((50, 60), dtype=np.float32)
    alpha[10:20, 5:45] = 1.0
    assert br.trim_box(alpha) == (5, 10, 45, 20)
    assert br.trim_box(np.zeros((5, 5))) is None
    rgb = _canvas(50, 60)
    out = br.compose_rgba(rgb, alpha, [WHITE], trim=True, decontaminate_edges=False)
    assert out.shape == (10, 40, 4)
    # 全部透明のときは切り詰めずに元の大きさのまま返す(0×0 の画像は作らない)
    empty = br.compose_rgba(rgb, np.zeros((50, 60), np.float32), [WHITE], trim=True)
    assert empty.shape == (50, 60, 4)


def test_compose_keeps_existing_transparency():
    rgb = _canvas(10, 10, RED)
    src_alpha = np.full((10, 10), 255, dtype=np.uint8)
    src_alpha[:, :5] = 0
    alpha = np.ones((10, 10), dtype=np.float32)
    out = br.compose_rgba(rgb, alpha, [WHITE], src_alpha=src_alpha)
    assert (out[:, :5, 3] == 0).all() and (out[:, 5:, 3] == 255).all()


def test_no_colors_means_nothing_removed():
    img = _subject_with_white_inside()
    dist, _ = br.color_distance(img, [])
    alpha = br.compute_alpha(dist, 20, 10, br.RANGE_GLOBAL)
    assert (alpha == 1.0).all()


def test_resize_long_side():
    img = _canvas(300, 2400)
    small, scale = br.resize_long_side(img, 1200)
    assert small.shape[:2] == (150, 1200) and abs(scale - 0.5) < 1e-9
    same, scale1 = br.resize_long_side(_canvas(10, 10), 1200)
    assert same.shape[:2] == (10, 10) and scale1 == 1.0


def test_qimage_roundtrip():
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    rgba = np.zeros((7, 13, 4), dtype=np.uint8)   # 幅13: 行の詰め物があっても崩れないこと
    rgba[..., 0] = np.arange(13, dtype=np.uint8)[None, :] * 10
    rgba[..., 1] = np.arange(7, dtype=np.uint8)[:, None] * 20
    rgba[..., 2] = 77
    rgba[..., 3] = 128
    back = br.qimage_to_array(br.array_to_qimage(rgba))
    assert np.array_equal(back, rgba)


# ---------------------------------------------------------------
# 精密モード(均した面で領域を決め、境目は混ざり具合から計算する)
# ---------------------------------------------------------------
NEAR_BG = (246, 245, 241)
# 背景との差が ΔE 3〜5 しかない淡い図形。人の目でははっきり別物に見える。
NEAR_SHAPES = [((236, 236, 234), (30, 30, 170, 130)),   # 明るいグレー(明るさだけ違う)
               ((250, 238, 238), (210, 30, 370, 130)),  # 淡いピンク
               ((238, 246, 236), (30, 170, 170, 270))]  # 淡い緑


def _near_color_scene(noise, seed=3):
    """オフホワイトの背景に淡い図形を置き、ノイズを足して JPEG で圧縮し直した画像と、
    図形ごとのマスク(縁はアンチエイリアス)を返す。"""
    import io
    from PIL import Image, ImageDraw

    h, w = 300, 400
    canvas = np.empty((h, w, 3), np.float32)
    canvas[:] = NEAR_BG
    masks = []
    for color, box in NEAR_SHAPES:
        big = Image.new("L", (w * 4, h * 4), 0)
        ImageDraw.Draw(big).rounded_rectangle([v * 4 for v in box], radius=60, fill=255)
        m = np.asarray(big.resize((w, h), Image.BOX), np.float32) / 255.0
        canvas = canvas * (1 - m[..., None]) + np.array(color, np.float32) * m[..., None]
        masks.append(m)
    canvas += np.random.default_rng(seed).normal(0, noise, canvas.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=85)
    rgb = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    return np.ascontiguousarray(rgb), masks


def _inside(mask, inset=4):
    return cv2.erode((mask > 0.99).astype(np.uint8), np.ones((2 * inset + 1,) * 2, np.uint8)) > 0


def test_pixel_noise_and_sigma_grow_with_noise():
    quiet, _ = _near_color_scene(0.5)
    loud, _ = _near_color_scene(6.0)
    n_quiet, n_loud = br.estimate_pixel_noise(quiet), br.estimate_pixel_noise(loud)
    assert n_loud > 2 * n_quiet, (n_quiet, n_loud)
    s_quiet, s_loud = br.smoothing_sigma(n_quiet), br.smoothing_sigma(n_loud)
    assert br.SMOOTH_SIGMA_MIN <= s_quiet < s_loud <= br.SMOOTH_SIGMA_MAX
    assert br.smoothing_sigma(1000.0) == br.SMOOTH_SIGMA_MAX
    assert br.smoothing_sigma(0.0) == br.SMOOTH_SIGMA_MIN


def test_lab_distance_matches_color_distance():
    rgb, _ = _near_color_scene(2.0)
    colors = [NEAR_BG, (250, 238, 238)]
    d1, i1 = br.color_distance(rgb, colors)
    d2, i2 = br.lab_distance(br.to_lab(rgb), br.to_lab(np.asarray(colors, np.uint8)))
    assert np.allclose(d1, d2, atol=1e-3)
    assert np.array_equal(i1, i2)
    d3, _ = br.lab_distance(br.to_lab(rgb), np.zeros((0, 3), np.float32))
    assert np.isinf(d3).all()


def test_precise_tolerance_steps_and_floor():
    assert br.precise_tolerance(None) == br.PRECISE_TOLERANCE_MIN
    assert br.precise_tolerance(0.0) == br.PRECISE_TOLERANCE_MIN
    value = br.precise_tolerance(1.91)  # σ6 の合成図解で測れた背景のムラ
    assert value == 2.5, value          # 整数に切り上げた 4 では明るいグレーの箱が抜けた
    assert (value / br.TOLERANCE_STEP) == int(value / br.TOLERANCE_STEP)


def test_matte_edges_recovers_mixture_ratio():
    """背景 B と前景 F が線形に混ざった縁で、不透明度がその割合になる。"""
    h, w = 40, 60
    B = np.array([90.0, 0.0, 2.0], np.float32)
    F = np.array([80.0, 6.0, -12.0], np.float32)
    ramp = np.clip((np.arange(w, dtype=np.float32) - 28.0) / 4.0, 0.0, 1.0)  # 4px の縁
    t = np.broadcast_to(ramp, (h, w))
    flat = (B * (1 - t[..., None]) + F * t[..., None]).astype(np.float32)
    region = (t >= 0.5).astype(np.float32)
    background = np.broadcast_to(B, (h, w, 3)).astype(np.float32)
    alpha = br.matte_edges(flat, background, region, sigma=1.5)
    assert np.abs(alpha - t).max() < 0.05, np.abs(alpha - t).max()


def test_matte_edges_low_contrast_noisy_edge_is_not_ragged():
    """差が小さくノイズが大きい縁でも、均した値から読むことで縁が毛羽立たない。"""
    h, w = 60, 80
    rng = np.random.default_rng(1)
    B = np.array([90.0, 0.0, 2.0], np.float32)
    F = np.array([87.0, 0.0, 0.0], np.float32)  # ΔE 3.6
    truth = (np.arange(w) >= 40).astype(np.float32)[None, :].repeat(h, 0)
    clean = B * (1 - truth[..., None]) + F * truth[..., None]
    flat = (clean + rng.normal(0, 1.5, clean.shape)).astype(np.float32)
    sigma = 2.0
    smooth = br.smooth_lab(flat, sigma)
    background = np.broadcast_to(B, (h, w, 3)).astype(np.float32)
    raw = br.matte_edges(flat, background, truth, sigma)
    fixed = br.matte_edges(flat, background, truth, sigma, smooth=smooth, noise=1.5)
    err_raw = float(np.abs(raw - truth).mean())
    err_fixed = float(np.abs(fixed - truth).mean())
    assert err_fixed < err_raw, (err_raw, err_fixed)
    assert err_fixed < 0.015, err_fixed


def test_matte_edges_keeps_outline_and_drops_ringing_outside():
    """白い塗りを灰色の細い輪郭線が囲む図形で、輪郭線は残り、線の外の明るいにじみ
    (JPEG のリンギング)は透明になる。輪郭線が「白と背景の混ざり」と読まれて消え、
    線の外の白いにじみが残っていた(実機のアイコン一覧)。"""
    h, w = 60, 80
    B = np.array([96.0, 0.0, 2.0], np.float32)
    white = np.array([99.5, 0.0, 0.0], np.float32)
    gray = np.array([60.0, 0.0, -4.0], np.float32)
    flat = np.broadcast_to(B, (h, w, 3)).copy()
    yy, xx = np.mgrid[0:h, 0:w]
    inside = (np.abs(xx - 40) <= 20) & (np.abs(yy - 30) <= 15)
    outline = inside & ~((np.abs(xx - 40) <= 18) & (np.abs(yy - 30) <= 13))
    ring = ~inside & (np.abs(xx - 40) <= 21) & (np.abs(yy - 30) <= 16)  # 線のすぐ外のにじみ
    flat[inside] = white
    flat[outline] = gray
    flat[ring] = B + (white - B) * 0.8  # 背景より明るい
    region = inside.astype(np.float32)
    region[ring] = 1.0  # 均した判定では、にじみも背景と違う色として残っている
    background = np.broadcast_to(B, (h, w, 3)).astype(np.float32)
    alpha = br.matte_edges(flat.astype(np.float32), background, region, sigma=1.5)
    assert (alpha[outline] > 0.9).mean() > 0.95, (alpha[outline] > 0.9).mean()
    assert (alpha[inside & ~outline] > 0.9).all()
    assert (alpha[ring] < 0.2).mean() > 0.9, (alpha[ring] < 0.2).mean()


def _window_alpha(rgb, precise):
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    rgba = np.dstack((rgb, np.full(rgb.shape[:2], 255, np.uint8)))
    win = br.BgRemoveWindow(rgba)
    try:
        win.precise_check.setChecked(precise)
        win._apply_auto_tolerance()
        return win.compute(preview=False)[..., 3].astype(np.float32) / 255.0
    finally:
        win.deleteLater()


def test_precise_window_keeps_faint_shapes_that_pixel_mode_loses():
    """窓の経路そのもので、淡い図形が精密モードでは残り、以前の方式では抜けること。"""
    rgb, masks = _near_color_scene(4.0)
    union = np.max(np.stack(masks), axis=0)
    background = _inside(1.0 - union, 6) & (union < 0.01)
    old = _window_alpha(rgb, precise=False)
    new = _window_alpha(rgb, precise=True)
    for m in masks:
        inside = _inside(m)
        assert (new[inside] > 0.9).mean() > 0.95, (new[inside] > 0.9).mean()
    gray = _inside(masks[0])
    assert (old[gray] > 0.9).mean() < 0.5  # 以前の方式では明るいグレーがほぼ抜ける
    assert (new[background] < 0.1).mean() > 0.99


# ---------------------------------------------------------------
def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    # QApplication を作ったまま終わると offscreen で終了時に落ちることがあるので、
    # 結果を出し切ってから os._exit で抜ける(テストの判定には影響しない)。
    code = main()
    sys.stdout.flush()
    os._exit(code)
