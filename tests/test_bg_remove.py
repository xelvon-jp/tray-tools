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
