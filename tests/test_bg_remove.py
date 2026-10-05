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


def test_keep_color_equal_to_background_does_nothing():
    rgb = _canvas(20, 20, (240, 240, 240))
    dist, _ = br.color_distance(rgb, [(240, 240, 240)])
    keep_dist, _ = br.color_distance(rgb, [(240, 240, 240)])
    alpha = br.compute_alpha(dist, 5, 2, keep_dist=keep_dist)
    assert (alpha == 0.0).all()


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
