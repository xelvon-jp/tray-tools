# bg_remove.py
# 画像の背景を透過させ、クリップボードへ戻す(または保存する)前に確かめる窓。
# 主な用途は AI に描かせた画像の背景抜き。
#
# 起動
#   pythonw bg_remove.py --clipboard          クリップボードの画像を読む
#   pythonw bg_remove.py <画像のパス>          ファイルを読む
#   (付箋からは一時PNGに --delete-after-read を付けて渡す。bg_remove_launch.py 参照)
#
# なぜ別プロセスなのか
#   numpy / cv2(さらに AI を使えば rembg と onnxruntime)は重い。常駐本体に持ち込むと
#   使わない日もメモリを食い、本体の起動も遅くなる。付箋プロセスからも同じ窓を出したい
#   ので、どちらから呼んでも同じになるよう独立したプロセスにした。本体側から起こす口は
#   bg_remove_launch.py にあり、そちらは numpy すら import しない。
#
# 構成
#   - 前半は Qt に依存しない純関数(numpy 配列 in / out)。背景色の推定・アルファ計算・
#     色かぶり除去・余白の切り詰め。tests/test_bg_remove.py が合成画像でここを確かめる。
#   - 後半が窓。スライダーを触っている間は縮小画像(長辺 PREVIEW_LONG_SIDE)で計算し、
#     クリップボードへ載せる/保存するときだけ原寸で計算し直す。
#
# 色の距離を RGB ではなく Lab で測る理由
#   RGB のユークリッド距離は、暗い色どうしの差を大きく、明るい色どうしの差を小さく
#   見積もる(人の目とは逆向きに歪む)。白背景の際にある薄いグレーの影と、黒背景の際に
#   ある濃紺の縁取りを同じ「許容量」で扱えるよう、知覚に近い Lab(CIE76 の ΔE)にした。
#   許容量のスライダーはそのまま ΔE の値になっている(2 前後が見分けられる限界)。
import argparse
import ctypes
import importlib.util
import json
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QIcon,
    QImage,
    QImageReader,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import settings as settings_module
from bg_remove_launch import CLIPBOARD_SOURCE, IMAGE_SUFFIXES
from toast import FADE_MS, VISIBLE_MS, show_toast

SCRIPT_DIR = Path(__file__).resolve().parent
ERROR_LOG_PATH = SCRIPT_DIR / "error.log"
ICON_PATH = SCRIPT_DIR / "icons" / "rapture.ico"
APP_USER_MODEL_ID = "traytools.app.1"  # 本体・付箋と同じ。タスクバーで別グループに割れないように

# ---------------------------------------------------------------
# 調整値
# ---------------------------------------------------------------
# スライダー操作中に計算する縮小画像の長辺。2000万画素級の画像を毎回原寸で計算すると
# 1回に1秒近くかかり、つまみに追従しなくなる。1200 なら表示の解像度としても足りる。
PREVIEW_LONG_SIDE = 1200
# スライダーを動かしてから計算を始めるまでの待ち。動かしている間は計算を積まず、
# 止まった(か、ゆっくり動いている)ときだけ描き直す。
DEBOUNCE_MS = 60

# 背景色の自動推定で見る外周の太さ(px)と、数える前の量子化の刻み。
# 量子化しないと、JPEG のノイズや AI のかすかなグラデーションで同じ「白」が数百色に
# 割れて、最頻色が意味を持たなくなる。刻み 16 なら各チャネル 16 段階。
BORDER_PX = 2
QUANT_STEP = 16

# 余白の切り詰めで「中身がある」とみなすアルファ(0〜255)の下限。0 より上を全部数えると、
# AI のマスクが背景の隅に散らす 1〜2 の値(実測で 800×600 に20画素ほど)に引っ張られて
# 外接矩形が画像いっぱいのままになる。8(約3%)以下はどのみち目に見えない。
TRIM_ALPHA_THRESHOLD = 8

TOLERANCE_MAX = 100   # 許容量(ΔE)の上限

# ---- 許容量の自動(auto_tolerance) ----
# 外周の何 px を「背景のムラ」の標本にするか。2px だと JPEG のブロック1列ぶんしか拾えず、
# ムラの上限を低く見積もりがち。
AUTO_BORDER_PX = 4
# 外周に掛かった被写体を標本から外す上限(ΔE)。これより背景色から遠い画素はムラではない。
AUTO_OUTLIER_DE = 20.0
# 測ったムラの上限(p99)に掛ける余裕と足す余裕。p99 ちょうどだと 1% の画素がぽつぽつ
# 残って「ゴミ」に見える。足しすぎると薄いボックスを食う。
AUTO_MARGIN_RATIO = 1.15
AUTO_MARGIN_DE = 1.0
AUTO_TOLERANCE_MIN = 2
AUTO_TOLERANCE_MAX = 40

# ---- 残す色(Ctrl+クリック、色で抜くとき) ----
# 背景色との距離 d_bg と残す色との距離 d_keep から r = d_bg / (d_bg + d_keep) を作る
# (0 = 背景色そのもの、1 = 残す色そのもの、0.5 = ちょうど中間)。境目のアンチエイリアスの
# 画素は Lab でほぼ線形に混ざっているので、r はそのまま「ボックスが占める割合」に近い。
# r が KEEP_R0 以下なら背景、KEEP_R1 以上なら残す色、その間は線形に半透明にする。
# r は画素ごとに揺れる。JPEG の箱の中は平らな塗りでも ΔE 4〜5 ばらつき(色の間引きと
# 文字・縁のまわりのリンギング)、背景との差が ΔE 6〜9 しかない薄い箱では r が 0.25〜0.4
# まで落ちる画素が出た(合成素材で実測)。そこで r を 5×5 の中央値で均してから使う。
# 中央値は段差(箱の縁)を鈍らせずにぽつぽつした外れだけを消すので、縁の半透明は保てる。
# 均したあとでも、背景側は 0.23 以下、箱の内側は 0.49 以上に収まった。
# 上端を 0.5 にしてあるのは「背景色より残す色のほうに近い画素は背景にしない」を
# そのまま守るため。0.55 などにすると、箱の中の文字の縁(濃い文字と箱の色の混ざり。
# どちらの色からも遠く、r が 0.5 をわずかに超える)が、許容量を大きくしたときに
# 半透明になった。背景側を 0.25 まで下げたぶん、境目の画素はやや不透明寄りになるが、
# 色かぶり除去で背景色を差し引くので白く光らない(テストで確かめてある)。
KEEP_R0 = 0.25
KEEP_R1 = 0.5
KEEP_MEDIAN = 5

# ---- 背景のムラ・グラデーションへの追従(fit_background_offset) ----
# 背景の色を「一定の色 + 画像全体でゆるく変わるずれ」とみなし、ずれを Lab の各成分ごとに
# (x, y) の2次多項式で当てはめる。ビネット(中央が明るく四隅が暗い)は2次でほぼ表せる。
# 3次以上は被写体の無い隅で暴れやすいので上げない。
GRADIENT_SAMPLES = 20000     # 当てはめに使う画素の上限(格子状に間引く)
GRADIENT_ITERATIONS = 4      # 外れ値を除いて当て直す回数
GRADIENT_INITIAL_DE = 15.0   # 最初に「背景らしい」とみなす背景色からの距離
GRADIENT_MIN_SAMPLES = 200   # これより標本が少なければ当てはめない(ずれ 0 のまま)
GRADIENT_MAX_DE = 20.0       # ずれの大きさの上限。標本の無い隅で外挿が暴れても、ここで止める
# 最初の当てはめは外周のこの幅(画像に対する割合)の帯だけで行う。画像全体の「背景色に
# 近い画素」から始めると、背景との差が ΔE 5〜8 しかない大きな薄いボックスまで標本に入り、
# 面がボックスの色へ引っ張られた(合成素材で、ボックスとの差が 8.5 → 5.6 に縮んだ)。
# 外周の帯だけでも2次の面(ビネット)は決まるので、そこから内側へ広げる。
GRADIENT_SEED_BAND = 0.08

# ---- AIの結果に対する補正(refine_ai_alpha) ----
# 残す点: AI がこれ以下と判定した画素は「AI が背景と言い切った場所」とみなし、点を打っても
# 効かせない(効かなかった点として報告する)。
AI_PROTECT_FLOOR = 0.05
# 残す点で不透明にする範囲は「点の値のこの割合より濃い部分」の連結成分。a > FLOOR の成分を
# まるごと不透明にすると、実写の接地影(白い牛の足元で 0.2〜0.3 程度)や、トラックの荷台の
# 中の半透明(0.35 前後)まで巻き込んで残ってしまった(テスト素材で実測)。点の値に対する
# 相対値にしてあるのは、AI が被写体全体を薄く(0.5 前後に)判定した場合にも、それより
# さらに薄い影とは分けられるようにするため。
AI_PROTECT_RELATIVE = 0.5
# 残す点で「もう不透明」とみなす値。これ以上の画素は塗り替える必要が無く、成分をつなぐ
# 橋としても使わない(refine_ai_alpha のコメント参照)。
AI_PROTECT_OPAQUE = 0.9
# 抜く点: a がこれ未満の連結成分を抜く(脚の間の隙間などに残った薄い判定を消す)。
AI_SEED_CEILING = 0.95
# 点で塗り替えた範囲の縁をなじませるぼかし(ガウスの σ、px)。縁が1画素の階段になると、
# 拡大したときにそこだけギザギザに見える。
AI_EDGE_SIGMA = 1.0
FEATHER_MAX = 60      # 境界のぼかし(ΔE)の上限
# 境界のぼかしの既定。以前は 10 だったが、AI の図解の薄いボックス(背景との差 ΔE 5〜15)は
# 許容量+ぼかしの範囲に丸ごと入って半透明になった。アンチエイリアスの縁をなじませるには
# 2〜3 で足りる。
DEFAULT_FEATHER = 2

RANGE_CONNECTED = "connected"   # 外周(とスポイトの点)からつながった部分だけ
RANGE_GLOBAL = "global"         # 画像全体の同色

# 左ペインのクリックの種類
PICK_REPLACE = "replace"   # クリック: 背景色を置き換える
PICK_ADD = "add"           # Shift+クリック: 背景色を足し、ここも抜く
PICK_PROTECT = "protect"   # Ctrl+クリック: ここは残す(色は足さない)

METHOD_COLOR = "color"
METHOD_AI = "ai"

# 抜き方のラジオの横に出す「このモードで何が効くか」。効かない項目は隠すので、
# 何で決まっているのかをここで一言で言っておく。
METHOD_NOTES = {
    METHOD_COLOR: "色で抜く：背景色・許容量・ぼかしで決める",
    METHOD_AI: "AIの結果：AIの判定を基本に、スライダーと残す点／抜く点で補正",
}

# rembg のモデル。先頭が既定。どちらも C:\Users\<名前>\.u2net\ にキャッシュされ、
# 無ければ初回に rembg がダウンロードする(birefnet は 900MB 超あるので注意)。
AI_MODELS = [
    ("isnet-general-use", "isnet-general-use（標準）"),
    ("birefnet-general", "birefnet-general（高品質・遅い）"),
]
AI_INSTALL_HINT = "pip install rembg onnxruntime で使えます"

PREVIEW_BACKGROUNDS = [
    ("checker", "市松"),
    ("white", "白"),
    ("black", "黒"),
    ("green", "緑"),
]

# 既定の大きさは作業領域に対する割合で決める(clipboard_preview と同じ考え)。
DEFAULT_WIDTH_RATIO = 0.7
DEFAULT_HEIGHT_RATIO = 0.75
# 右の設定パネルを含めた最小。画像の2ペインがそれぞれ 280px 前後は取れるようにする。
MIN_WIDTH = 960
MIN_HEIGHT = 600
# 右の設定パネルの幅。縦1列に並べるので固定にする(窓を広げたときに、スライダーが
# 横に伸びてラベルと数値が両端に離れるのが「見づらい」の原因の1つだった)。
PANEL_WIDTH = 310

# 文字の大きさ(pt)。以前の 8〜9pt は小さすぎて読みづらいと言われた。
FONT_BASE = 10
FONT_SMALL = 9
FONT_TITLE = 11

SETTINGS_SECTION = "bg_remove"


# ===============================================================
# 純関数(Qt に依存しない)
# ===============================================================
def to_lab(rgb) -> np.ndarray:
    """uint8 の RGB(…×3)を float32 の Lab へ。L は 0〜100、a/b はおよそ ±127。

    cv2 は uint8 のまま渡すと Lab を 0〜255 に詰め直した値を返す(ΔE として読めない)。
    0〜1 の float32 で渡すと本来のスケールで返るので、必ずこちらを通す。"""
    arr = np.ascontiguousarray(np.asarray(rgb, dtype=np.float32) / 255.0)
    if arr.ndim == 2:  # (N, 3) の色の並びも受ける
        return cv2.cvtColor(arr.reshape(1, -1, 3), cv2.COLOR_RGB2Lab).reshape(-1, 3)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2Lab)


def estimate_background(rgb, alpha=None, border: int = BORDER_PX, step: int = QUANT_STEP):
    """外周 border px の画素の最頻色を背景色と推定し (r, g, b) で返す。

    量子化した箱で数えて最多の箱を選び、返す色はその箱に入った実際の画素の中央値
    (箱の中心値を返すと、真っ白な背景が (248,248,248) のような半端な色になり、
    許容量を小さくしたときに背景が抜け切らない)。
    alpha が与えられたら、すでに透明な画素は数えない(透過PNGを読み直した場合に、
    透明部分に残っている見えない色を背景と取り違えないため)。"""
    rgb = np.asarray(rgb)
    h, w = rgb.shape[:2]
    b = max(1, min(int(border), h, w))
    mask = np.zeros((h, w), dtype=bool)
    mask[:b, :] = True
    mask[-b:, :] = True
    mask[:, :b] = True
    mask[:, -b:] = True
    if alpha is not None:
        mask &= np.asarray(alpha) > 0
    pixels = rgb[mask]
    if len(pixels) == 0:
        return (255, 255, 255)
    q = (pixels // step).astype(np.int32)
    keys = (q[:, 0] << 16) | (q[:, 1] << 8) | q[:, 2]
    _values, inverse, counts = np.unique(keys, return_inverse=True, return_counts=True)
    chosen = pixels[inverse.reshape(-1) == int(np.argmax(counts))]
    median = np.median(chosen, axis=0)
    return tuple(int(round(v)) for v in median)


def color_distance(rgb, colors, offset=None, color_offsets=None):
    """各画素から、いちばん近い色までの ΔE と、その色の番号を返す。

    戻り値は (dist: float32 HxW, index: int32 HxW)。colors が空なら dist は全部 inf
    (=どこも背景ではない)。index は色かぶり除去で「どの背景色が混ざったか」に使う。

    offset(HxWx3、Lab)を渡すと、色は場所ごとに offset だけずれているものとして測る
    (背景のムラへの追従)。color_offsets はその色を拾った場所でのずれで、拾った色から
    差し引いて「ずれの無い元の色」に戻してから、測る場所のずれを足す(残す色に使う)。"""
    rgb = np.asarray(rgb)
    h, w = rgb.shape[:2]
    index = np.zeros((h, w), dtype=np.int32)
    if not colors:
        return np.full((h, w), np.inf, dtype=np.float32), index
    lab = to_lab(rgb)
    if offset is not None:
        lab = lab - offset
    targets = to_lab(np.asarray(colors, dtype=np.uint8).reshape(-1, 3))
    if color_offsets is not None:
        targets = targets - np.asarray(color_offsets, dtype=np.float32).reshape(-1, 3)
    dist = None
    for i, target in enumerate(targets):
        diff = lab - target
        d = np.sqrt(np.einsum("ijk,ijk->ij", diff, diff))
        if dist is None:
            dist = d
        else:
            closer = d < dist
            dist = np.where(closer, d, dist)
            index[closer] = i
    return dist.astype(np.float32), index


def _poly_terms(u, v) -> np.ndarray:
    """2次多項式の項 [1, u, v, u², uv, v²]。u, v は 0〜1 に正規化した座標。"""
    u = np.asarray(u, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)
    return np.stack([np.ones_like(u), u, v, u * u, u * v, v * v], axis=-1)


def _normalized_grid(h: int, w: int):
    """画素の中心を 0〜1 に正規化した座標。縮小プレビューと原寸で同じ係数が使えるよう、
    画素数ではなく画像に対する割合で表す。"""
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    return u, v


def fit_background_offset(rgb, colors, keep_colors=(), threshold: float = GRADIENT_INITIAL_DE,
                          iterations: int = GRADIENT_ITERATIONS,
                          max_samples: int = GRADIENT_SAMPLES):
    """背景のムラ(ビネット・ゆるいグラデーション)を、背景色からの Lab のずれとして
    2次多項式で当てはめ、係数(6×3、float32)を返す。当てはめられなければ None。

    標本は格子状に間引いた画素のうち、いちばん近い背景色から threshold 以内のもの。
    残す色のほうが近い画素(薄いボックス)は最初から外す。当てはめたあと、面からの残差が
    大きい画素(背景に似た被写体)を外して当て直すのを繰り返す。外す線は残差の中央値から
    決める(ノイズの大きい JPEG でも、きれいな PNG でも同じ考えで効くように)。"""
    rgb = np.asarray(rgb)
    if not colors:
        return None
    h, w = rgb.shape[:2]
    step = max(1, int(np.sqrt(h * w / float(max_samples))))
    sub = np.ascontiguousarray(rgb[step // 2::step, step // 2::step])
    sh, sw = sub.shape[:2]
    if sh == 0 or sw == 0:
        return None
    lab = to_lab(sub).reshape(-1, 3)
    ys = (np.arange(sh, dtype=np.float32) * step + step // 2 + 0.5) / h
    xs = (np.arange(sw, dtype=np.float32) * step + step // 2 + 0.5) / w
    uu, vv = np.meshgrid(xs, ys)
    terms = _poly_terms(uu.reshape(-1), vv.reshape(-1))

    dist, index = color_distance(sub, list(colors))
    dist = dist.reshape(-1)
    targets = to_lab(np.asarray(colors, dtype=np.uint8).reshape(-1, 3))
    residual_target = lab - targets[index.reshape(-1)]
    allowed = dist < threshold
    if keep_colors:
        keep_dist, _ = color_distance(sub, list(keep_colors))
        allowed &= dist < keep_dist.reshape(-1)
    uf, vf = uu.reshape(-1), vv.reshape(-1)
    band = ((uf < GRADIENT_SEED_BAND) | (uf > 1 - GRADIENT_SEED_BAND)
            | (vf < GRADIENT_SEED_BAND) | (vf > 1 - GRADIENT_SEED_BAND))
    mask = allowed & band

    coeffs = None
    for _ in range(max(1, iterations)):
        if mask.sum() < GRADIENT_MIN_SAMPLES:
            return coeffs.astype(np.float32) if coeffs is not None else None
        coeffs, *_rest = np.linalg.lstsq(terms[mask], residual_target[mask], rcond=None)
        err = np.linalg.norm(residual_target - terms @ coeffs, axis=1)
        spread = float(np.median(err[mask])) * 1.4826  # 中央値から標準偏差相当へ
        # 2回目からは内側の画素も、面からの残差が小さければ標本に入れる。
        mask = allowed & (err < max(2.0, 3.0 * spread))
    return coeffs.astype(np.float32) if coeffs is not None else None


def background_offset(coeffs, h: int, w: int):
    """係数から、各画素での背景色のずれ(HxWx3、Lab、float32)を作る。coeffs が None なら None。"""
    if coeffs is None:
        return None
    u, v = _normalized_grid(h, w)
    uu, vv = np.meshgrid(u, v)
    offset = (_poly_terms(uu, vv) @ np.asarray(coeffs, dtype=np.float32)).astype(np.float32)
    norm = np.linalg.norm(offset, axis=2, keepdims=True)
    too_far = norm > GRADIENT_MAX_DE
    if too_far.any():
        offset = np.where(too_far, offset * (GRADIENT_MAX_DE / np.maximum(norm, 1e-6)), offset)
    return offset


def offset_at(coeffs, x: float, y: float, h: int, w: int):
    """画像座標 (x, y) でのずれ(Lab の3成分)。coeffs が None なら 0。"""
    if coeffs is None:
        return np.zeros(3, dtype=np.float32)
    terms = _poly_terms(np.float32((x + 0.5) / w), np.float32((y + 0.5) / h))
    offset = terms @ np.asarray(coeffs, dtype=np.float32)
    norm = float(np.linalg.norm(offset))
    if norm > GRADIENT_MAX_DE:
        offset = offset * (GRADIENT_MAX_DE / norm)
    return offset.astype(np.float32)


def local_background_rgb(colors, index, offset):
    """各画素での背景の色(RGB、0〜255 の float32、HxWx3)。色かぶり除去の B に使う。

    ムラに追従しているときは、背景の色そのものが場所ごとに違う。一定の色で差し引くと、
    ビネットで暗くなった隅では縁に暗い筋が、明るい中央では白い筋が出る。"""
    targets = to_lab(np.asarray(colors, dtype=np.uint8).reshape(-1, 3))
    lab = targets[np.asarray(index)] + offset
    rgb = cv2.cvtColor(np.ascontiguousarray(lab, dtype=np.float32), cv2.COLOR_Lab2RGB)
    return np.clip(rgb * 255.0, 0.0, 255.0).astype(np.float32)


def auto_tolerance(rgb, colors, offset=None, alpha=None, border: int = AUTO_BORDER_PX) -> int:
    """外周の画素から背景のムラの上限を測り、許容量の目安(ΔE、整数)を返す。

    外周 border px の画素と背景色との ΔE を集め、AUTO_OUTLIER_DE を超えるもの(外周に
    掛かった被写体)を外してから p99 を取り、少し余裕を足す。AI の図解の白背景は、
    ムラが ΔE 1〜3 程度しかないのに、上に載る薄いボックスとの差も ΔE 5〜15 しかない。
    固定の初期値(以前は 12)では箱ごと抜けてしまうので、画像ごとにムラの実測から決める。"""
    rgb = np.asarray(rgb)
    if not colors:
        return AUTO_TOLERANCE_MIN
    h, w = rgb.shape[:2]
    b = max(1, min(int(border), h // 2 or 1, w // 2 or 1))
    dist, _ = color_distance(rgb, list(colors), offset)
    mask = np.zeros((h, w), dtype=bool)
    mask[:b, :] = True
    mask[-b:, :] = True
    mask[:, :b] = True
    mask[:, -b:] = True
    if alpha is not None:
        mask &= np.asarray(alpha) > 0
    sample = dist[mask]
    sample = sample[sample < AUTO_OUTLIER_DE]
    if len(sample) == 0:
        return AUTO_TOLERANCE_MIN
    value = float(np.percentile(sample, 99)) * AUTO_MARGIN_RATIO + AUTO_MARGIN_DE
    return int(max(AUTO_TOLERANCE_MIN, min(AUTO_TOLERANCE_MAX, np.ceil(value))))


def scale_points(points, scale: float):
    """原寸の画像座標 (x, y) の並びを、縮小プレビューの座標へ換算する。

    画素 (x, y) は [x, x+1) の範囲を占めるので、中心 (x+0.5) を縮めてから切り捨てる。
    左上の角で換算すると、縮小で1画素ぶん左上へずれ、細い隙間に打った点が隣の
    (被写体側の)画素へ落ちることがある。"""
    if scale >= 1.0:
        return [(int(x), int(y)) for x, y in points or ()]
    return [(int((x + 0.5) * scale), int((y + 0.5) * scale)) for x, y in points or ()]


def compute_alpha(dist, tolerance: float, feather: float, mode: str = RANGE_CONNECTED,
                  seeds=(), protect=(), report: bool = False, keep_dist=None):
    """背景からの距離から不透明度(0〜1、float32)を作る。

    距離 ≦ 許容量 は完全に透明、許容量〜許容量+ぼかし は線形に半透明、それより遠ければ
    不透明。透明(半透明を含む)になる画素を「候補」と呼ぶ。

    mode(画像の内側にある背景色に近い色をどうするか)
      connected(残す) … 候補のうち、外周に接している連結成分と、seeds(画像座標の
          (x, y)、Shift+クリックの「ここも抜く」)を含む連結成分だけを抜く。被写体の
          内側にある白(白目・服のハイライト)を守るのが目的。
      global(抜く) … 候補を全部抜く(文字の穴なども抜ける)。

    protect(Ctrl+クリックの「ここは残す」)は、その点を含む候補の連結成分を不透明に
    戻す。どちらのモードでも効く(global では本来連結成分を求めないが、保護点があると
    きだけ求める)。ただし外周に接する成分の上の保護点は効かせない。それを残すと外側の
    背景まで丸ごと残ってしまうため。候補でない画素(もともと不透明)の上の保護点は、
    守るまでもないので黙って無視する。

    keep_dist(Ctrl+クリックの「残す色」までの距離、HxW)を渡すと、背景色より残す色の
    ほうに近い画素を候補から外す(r = d_bg / (d_bg + d_keep) が KEEP_R0〜KEEP_R1 で
    背景→残す色へ線形に移る)。許容量の内側でも効くので、背景との差が ΔE 5 しかない
    枠線の無いボックスでも、外周の背景から切り離して残せる。境目の画素は r がボックスの
    占める割合に近く、そのまま半透明の度合いになる(色かぶり除去で背景色を差し引ける)。

    report が True なら (alpha, 効かなかった保護点の番号のリスト) を返す。番号は
    protect の並びでの位置。外周とつながっていて効かなかったものだけが入る。"""
    dist = np.asarray(dist, dtype=np.float32)
    tol = float(tolerance)
    fea = max(float(feather), 0.0)
    if fea > 0:
        background = np.clip((tol + fea - dist) / fea, 0.0, 1.0)
    else:
        background = (dist <= tol).astype(np.float32)
    if keep_dist is not None:
        keep_dist = np.asarray(keep_dist, dtype=np.float32)
        r = dist / np.maximum(dist + keep_dist, 1e-6)
        # 背景色と残す色が同じ(d_bg = d_keep = 0)なら残す色は区別の役に立たないので、
        # 背景のまま(r = 0)にしておく。
        r = np.where(dist + keep_dist > 1e-6, r, 0.0).astype(np.float32)
        if min(r.shape) >= KEEP_MEDIAN:
            r = cv2.medianBlur(np.ascontiguousarray(r), KEEP_MEDIAN)
        relative = np.clip((KEEP_R1 - r) / (KEEP_R1 - KEEP_R0), 0.0, 1.0)
        background = np.minimum(background, relative).astype(np.float32)

    protect = list(protect or ())
    rejected = []
    if mode == RANGE_CONNECTED or protect:
        candidate = (background > 0).astype(np.uint8)
        count, labels = cv2.connectedComponents(candidate, connectivity=8)
        h, w = labels.shape
        edge_labels = np.unique(np.concatenate(
            (labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1])))
        edge = np.zeros(count, dtype=bool)
        edge[edge_labels] = True
        edge[0] = False  # ラベル0は「候補でない画素」

        if mode == RANGE_CONNECTED:
            keep = edge.copy()
            for x, y in seeds or ():
                xi, yi = int(x), int(y)
                if 0 <= xi < w and 0 <= yi < h:
                    keep[labels[yi, xi]] = True
            keep[0] = False  # ここを残すと被写体まで抜ける
        else:
            keep = np.ones(count, dtype=bool)
            keep[0] = False

        # 保護は「抜く」より後に掛ける(Shift の点と同じ成分に Ctrl の点があれば、残す側が
        # 勝つ。あとから細かく直す操作なので、上書きできるほうが自然)。
        for i, (x, y) in enumerate(protect):
            xi, yi = int(x), int(y)
            if not (0 <= xi < w and 0 <= yi < h):
                continue
            label = labels[yi, xi]
            if label == 0:
                continue
            if edge[label]:
                rejected.append(i)
                continue
            keep[label] = False
        background = background * keep[labels]

    alpha = (1.0 - background).astype(np.float32)
    if report:
        return alpha, rejected
    return alpha


def _soft_mask(mask) -> np.ndarray:
    """0/1 のマスクを AI_EDGE_SIGMA だけぼかした float32(0〜1)。"""
    return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), AI_EDGE_SIGMA)


def refine_ai_alpha(alpha, lo: float = 0.0, hi: float = 1.0, protect=(), seeds=(),
                    report: bool = False):
    """AI が出した不透明度(0〜1)を、一括の調整と点の指定で補正する。

    1. 一括の調整: a' = clip((a − lo) / (hi − lo), 0, 1)。lo 未満は消え、hi 以上は不透明。
       AI が白い被写体を背景と取り違えて半透明にしたとき、点を打たずにまとめて濃くできる。
    2. 抜く点(seeds): a' < AI_SEED_CEILING の連結成分のうち点を含むものを 0 にする。
       画像の外周に接する成分は「外側の背景そのもの」なので触らない(触ると被写体の輪郭の
       なめらかさまで削れる)。外周につながった薄い判定は lo で消す。
    3. 残す点(protect): 点の値 v に対して、a' > max(FLOOR, v·RELATIVE) の連結成分のうち
       点を含むものを 1 にする。v ≦ FLOOR(AI が背景と言い切った場所)と、成分が画像の
       半分を超える(背景ごと拾っている)場合は効かせず報告する。抜く点より後に掛けるので、
       同じ場所なら残す側が勝つ(色で抜くときと同じ)。

    塗り替えた範囲の縁は、マスクを少しぼかしてから max / 掛け算で元の値とつなぐ。

    report が True なら (alpha, 効かなかった残す点の番号, 効かなかった抜く点の番号) を返す。"""
    a = np.asarray(alpha, dtype=np.float32)
    lo = min(max(float(lo), 0.0), 1.0)
    hi = min(max(float(hi), 0.0), 1.0)
    if hi <= lo:
        hi = min(lo + 1e-3, 1.0)
        lo = hi - 1e-3
    out = np.clip((a - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)
    h, w = out.shape

    def inside(x, y):
        return 0 <= int(x) < w and 0 <= int(y) < h

    seeds_rejected = []
    seeds = list(seeds or ())
    if seeds:
        count, labels = cv2.connectedComponents(
            (out < AI_SEED_CEILING).astype(np.uint8), connectivity=8)
        edge = np.zeros(count, dtype=bool)
        edge[np.unique(np.concatenate(
            (labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1])))] = True
        chosen = np.zeros(count, dtype=bool)
        for i, (x, y) in enumerate(seeds):
            if not inside(x, y):
                continue
            label = labels[int(y), int(x)]
            if out[int(y), int(x)] >= AI_SEED_CEILING:
                continue  # 被写体の真ん中に打った点。抜く対象が無い
            if edge[label]:
                seeds_rejected.append(i)
                continue
            chosen[label] = True
        if chosen.any():
            mask = chosen[labels]
            out = out * (1.0 - _soft_mask(mask))

    protect_rejected = []
    protect = list(protect or ())
    if protect:
        limit = h * w / 2.0
        grown = np.zeros((h, w), dtype=bool)
        opaque = out >= AI_PROTECT_OPAQUE
        if opaque.any():
            _n, opaque_labels = cv2.connectedComponents(opaque.astype(np.uint8), connectivity=8)
        for i, (x, y) in enumerate(protect):
            if not inside(x, y):
                continue
            xi, yi = int(x), int(y)
            v = float(out[yi, xi])
            if v <= AI_PROTECT_FLOOR:
                protect_rejected.append(i)
                continue
            # 塗る候補は「薄すぎず、まだ不透明でもない」帯の画素だけ。すでに不透明な画素は
            # つなぎ目に使わない。使うと、隣の黒い牛やトラック(不透明)を橋にして、
            # 離れた所の接地影まで同じ成分になって残ってしまう(テスト素材で実際に起きた)。
            threshold = max(AI_PROTECT_FLOOR, min(v, AI_PROTECT_OPAQUE) * AI_PROTECT_RELATIVE)
            band = (out > threshold) & ~opaque
            _count, labels = cv2.connectedComponents(band.astype(np.uint8), connectivity=8)
            if v >= AI_PROTECT_OPAQUE:
                # すでに不透明な所に打った点。その不透明な塊に接している帯の成分を濃くする
                # (半分だけ透けた被写体の、透けている側を戻す)。
                blob = opaque_labels == opaque_labels[yi, xi]
                ring = cv2.dilate(blob.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
                touching = np.unique(labels[ring & band])
                region = np.isin(labels, touching[touching > 0]) | blob
            else:
                region = labels == labels[yi, xi]
            if region.sum() > limit:
                protect_rejected.append(i)
                continue
            grown |= region
        if grown.any():
            out = np.maximum(out, _soft_mask(grown))

    out = np.clip(out, 0.0, 1.0).astype(np.float32)
    if report:
        return out, protect_rejected, seeds_rejected
    return out


def decontaminate(rgb, alpha, colors, index=None, background=None) -> np.ndarray:
    """半透明の画素から背景色の混入を取り除いた RGB(uint8)を返す。

    境界の画素は「前景 C と背景 B が a : 1−a で混ざった色」なので、C = (観測 − (1−a)·B) / a
    で前景を取り出す。これをしないと、白背景から抜いた髪の毛の縁が白く光り、暗い背景に
    置いたときに輪郭が浮く。背景色が複数あるときは、その画素にいちばん近い色を B にする
    (index は color_distance の戻り値)。background(HxWx3 の RGB)を渡すと、色ではなく
    その場所の背景の色を B にする(ムラに追従しているとき)。不透明・完全透明の画素はそのまま。"""
    rgb = np.asarray(rgb)
    alpha = np.asarray(alpha, dtype=np.float32)
    if not colors:
        return rgb.copy()
    palette = np.asarray(colors, dtype=np.float32).reshape(-1, 3)
    partial = (alpha > 0.0) & (alpha < 1.0)
    if not partial.any():
        return rgb.copy()
    out = rgb.astype(np.float32)
    a = alpha[partial][:, None]
    if background is not None:
        background = np.asarray(background, dtype=np.float32)[partial]
    elif index is None or len(palette) == 1:
        background = palette[0][None, :]
    else:
        background = palette[np.asarray(index)[partial]]
    observed = out[partial]
    # a がごく小さい画素は割り算で値が暴れるが、ほぼ見えない画素なのでクリップで足りる。
    out[partial] = (observed - (1.0 - a) * background) / np.maximum(a, 1e-3)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def trim_box(alpha, threshold: float = 0.0):
    """不透明な部分(alpha > threshold)の外接矩形を (x0, y0, x1, y1)(x1, y1 は含まない)で
    返す。全部透明なら None。"""
    alpha = np.asarray(alpha)
    mask = alpha > threshold
    rows = np.flatnonzero(mask.any(axis=1))
    if len(rows) == 0:
        return None
    cols = np.flatnonzero(mask.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def compose_rgba(rgb, alpha, colors=(), index=None, decontaminate_edges: bool = True,
                 trim: bool = False, src_alpha=None, background=None) -> np.ndarray:
    """RGB と不透明度から、出力する RGBA(uint8)を組み立てる。

    src_alpha は元画像がもともと持っていたアルファ(0〜255)。元から透明な部分は
    透明のまま残したいので掛け合わせる。色かぶり除去は「背景を抜いて生まれた半透明」
    だけが対象なので、掛け合わせる前の alpha で行う。"""
    alpha = np.asarray(alpha, dtype=np.float32)
    color = (decontaminate(rgb, alpha, list(colors), index, background)
             if decontaminate_edges else np.asarray(rgb))
    out_alpha = alpha * 255.0
    if src_alpha is not None:
        out_alpha = out_alpha * (np.asarray(src_alpha, dtype=np.float32) / 255.0)
    alpha8 = np.clip(np.rint(out_alpha), 0, 255).astype(np.uint8)
    rgba = np.dstack((color, alpha8))
    if trim:
        box = trim_box(alpha8, TRIM_ALPHA_THRESHOLD)
        if box is not None:
            x0, y0, x1, y1 = box
            rgba = rgba[y0:y1, x0:x1]
    return np.ascontiguousarray(rgba)


def resize_long_side(array, long_side: int):
    """長辺が long_side を超えていれば縮める。(縮めた配列, 倍率) を返す。"""
    h, w = array.shape[:2]
    scale = min(1.0, float(long_side) / max(h, w))
    if scale >= 1.0:
        return array, 1.0
    size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return cv2.resize(array, size, interpolation=cv2.INTER_AREA), scale


# ---------------------------------------------------------------
# AI(rembg)。import はボタンを押すまで遅らせる
# ---------------------------------------------------------------
def ai_available() -> bool:
    """rembg と onnxruntime が入っているか。import はしない。

    rembg の import は初回 40 秒以上かかった(numba のコンパイルが走る。2回目以降も 3 秒)。
    窓を開くたびにそれを払うわけにはいかないので、ここでは在るかどうかだけを見る。"""
    try:
        return (importlib.util.find_spec("rembg") is not None
                and importlib.util.find_spec("onnxruntime") is not None)
    except (ImportError, ValueError):
        return False


_ai_sessions = {}
_ai_lock = threading.Lock()


def run_ai(rgb, model: str) -> np.ndarray:
    """rembg で前景マスクを作り、不透明度(0〜1、float32、元と同じ大きさ)で返す。
    別スレッドから呼ばれる。セッションは同じモデルなら使い回す(作るのに1秒前後かかる)。"""
    with _ai_lock:
        from rembg import new_session, remove

        session = _ai_sessions.get(model)
        if session is None:
            session = _ai_sessions[model] = new_session(model)
        mask = remove(np.ascontiguousarray(rgb), session=session, only_mask=True)
    mask = np.asarray(mask)
    if mask.ndim == 3:
        mask = mask[..., 0]
    h, w = rgb.shape[:2]
    if mask.shape != (h, w):
        mask = cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)
    return mask.astype(np.float32) / 255.0


# ===============================================================
# Qt との橋渡し
# ===============================================================
def qimage_to_array(image: QImage) -> np.ndarray:
    """QImage → RGBA(uint8、HxWx4、アルファは乗算前)。"""
    img = image.convertToFormat(QImage.Format_RGBA8888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h).reshape(h, bpl)
    return buf[:, : w * 4].reshape(h, w, 4).copy()


def array_to_qimage(rgba) -> QImage:
    """RGBA(uint8)→ QImage。元の配列の寿命から切り離すためにコピーして返す。"""
    rgba = np.ascontiguousarray(rgba)
    h, w = rgba.shape[:2]
    if rgba.shape[2] == 3:
        return QImage(rgba.tobytes(), w, h, w * 3, QImage.Format_RGB888).copy()
    return QImage(rgba.tobytes(), w, h, w * 4, QImage.Format_RGBA8888).copy()


def _read_image_file(path: str) -> QImage:
    reader = QImageReader(path)
    # スマホの写真は EXIF の向きで回して見せるのが普通。そのまま読むと横倒しになる。
    reader.setAutoTransform(True)
    return reader.read()


def load_source(source: str):
    """(QImage, 名前の手がかり) を返す。読めなければ (null の QImage, None)。

    クリップボードは「画像そのもの」を先に見て、無ければエクスプローラでコピーした
    ファイルの一覧から最初の画像を選ぶ。名前の手がかりは保存の既定ファイル名に使う。"""
    if source != CLIPBOARD_SOURCE:
        return _read_image_file(source), Path(source).stem

    clipboard = QGuiApplication.clipboard()
    mime = clipboard.mimeData()
    if mime is not None and mime.hasImage():
        image = clipboard.image()
        if not image.isNull():
            return image, None
    if mime is not None and mime.hasUrls():
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if Path(path).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            image = _read_image_file(path)
            if not image.isNull():
                return image, Path(path).stem
    return QImage(), None


# ---------------------------------------------------------------
# settings.json(bg_remove セクション)
# ---------------------------------------------------------------
def _section(app_settings) -> dict:
    section = (app_settings or {}).get(SETTINGS_SECTION)
    return section if isinstance(section, dict) else {}


def _save_values(app_settings, settings_path, updates: dict, remove=()) -> None:
    """bg_remove セクションの該当キーだけを書き換える。

    clipboard_preview._save_size と同じ作法で、ファイルを読み直して差し替える
    (メモリ上の設定を丸ごと書くと、既定値まで焼き込まれて settings.json の姿が変わる。
    本体が同時に別のキーを書いていても、読み直してから書けば消し合わない)。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = app_settings[SETTINGS_SECTION] = {}
        section.update(updates)
        for key in remove:
            section.pop(key, None)
    if not settings_path:
        return
    try:
        stored = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                stored = json.load(f)
        if not isinstance(stored, dict):
            stored = {}
        section = stored.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = stored[SETTINGS_SECTION] = {}
        section.update(updates)
        for key in remove:
            section.pop(key, None)
        settings_module.save_settings(stored, settings_path)
    except Exception as e:
        # 覚えられないだけ。窓の操作を止めるほどのことではない。
        print(f"[bg_remove] 設定を保存できません: {e}", file=sys.stderr)


def _int_setting(section: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(section.get(key, default))))
    except (TypeError, ValueError):
        return default


# ===============================================================
# 窓
# ===============================================================
# 配色はここで1か所に決める。個別のウィジェットに色を書かないこと。
#
# 以前は窓のスタイルシートで一部だけを暗くしていて、残りの部品(無効のラジオの文字、
# コンボの一覧、ツールチップなど)は Windows の既定の配色=明るい地に黒文字の前提で
# 描かれていた。そのため暗い地に黒文字が沈む所が出た。いまはこのプロセスの
# QApplication を Fusion にして、パレットを Active / Inactive / Disabled の全部について
# 明示的に塗り、OS の配色が入り込む余地を無くしている(apply_theme)。
# 付箋や常駐本体とは別プロセスなので、ここで変えても向こうには影響しない。
THEME = {
    "bg": "#1e1f22",            # 窓の地
    "panel": "#26282c",         # 右の設定パネル(一段明るい)
    "field": "#313338",         # 入力欄・ボタン(さらに一段明るい)
    "field_hover": "#3a3d43",
    "field_pressed": "#2b2d31",
    "border": "#4a4d55",
    "separator": "#3a3d43",
    "shadow": "#121315",
    "text": "#e6e6e6",          # 本文
    "heading": "#ffffff",
    "muted": "#a0a4ab",         # 補足説明。地とのコントラスト 4.5 以上を守る
    "disabled": "#878b93",      # 無効。薄いと分かる程度に、ただし 3:1 以上
    "accent": "#2563eb",        # 強調色は1色の青に揃える(主ボタン・選択中・スライダー・チェック)
    "accent_hover": "#1d4ed8",
    "accent_pressed": "#1e40af",
    "accent_text": "#ffffff",
    "keep": "#15803d",          # 残す点の印(左ペインのマーカーと同じ緑)
    "keep_text": "#ffffff",
    "warning": "#f5a524",
    "error": "#ff7a7a",
    "link": "#8ab4ff",
    "tooltip_bg": "#2b2d31",
}

# 文字色と地の色の組み合わせと、満たすべきコントラスト比。tests/test_bg_remove.py が
# これを機械的に確かめる。本文・補足・警告は 4.5、無効の文字は 3.0(WCAG の基準)。
CONTRAST_REQUIREMENTS = [
    ("text", "bg", 4.5), ("text", "panel", 4.5), ("text", "field", 4.5),
    ("text", "field_hover", 4.5), ("text", "tooltip_bg", 4.5),
    ("heading", "panel", 4.5), ("heading", "bg", 4.5),
    ("muted", "bg", 4.5), ("muted", "panel", 4.5), ("muted", "field", 4.5),
    ("disabled", "panel", 3.0), ("disabled", "field", 3.0), ("disabled", "bg", 3.0),
    ("accent_text", "accent", 4.5), ("accent_text", "accent_hover", 4.5),
    ("accent_text", "accent_pressed", 4.5),
    ("keep_text", "keep", 4.5),
    ("warning", "bg", 4.5), ("error", "bg", 4.5), ("link", "bg", 4.5),
    # 部品の輪郭(入力欄・ボタンの枠)は地から 3:1 は要らないが、見失わない程度に。
    ("border", "panel", 1.6),
]


def _relative_luminance(color: str) -> float:
    value = color.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2 のコントラスト比(1〜21)。"""
    a = _relative_luminance(foreground)
    b = _relative_luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def build_palette() -> QPalette:
    """全グループ(Active / Inactive / Disabled)を明示的に塗ったパレット。

    1つのグループだけ塗ると、窓が非アクティブになった瞬間や無効の部品で、OS の既定
    (明るい地に黒文字)が顔を出す。"""
    t = THEME
    normal = {
        QPalette.Window: t["bg"], QPalette.WindowText: t["text"],
        QPalette.Base: t["field"], QPalette.AlternateBase: t["panel"],
        QPalette.Text: t["text"], QPalette.Button: t["field"],
        QPalette.ButtonText: t["text"], QPalette.BrightText: t["heading"],
        QPalette.Highlight: t["accent"], QPalette.HighlightedText: t["accent_text"],
        QPalette.ToolTipBase: t["tooltip_bg"], QPalette.ToolTipText: t["text"],
        QPalette.PlaceholderText: t["muted"], QPalette.Link: t["link"],
        QPalette.LinkVisited: t["link"],
        # Fusion が枠や溝の陰影に使う色。既定のままだと明るい地向けの灰色が混じる。
        QPalette.Light: t["field_hover"], QPalette.Midlight: t["field"],
        QPalette.Mid: t["border"], QPalette.Dark: t["shadow"], QPalette.Shadow: "#000000",
    }
    disabled = dict(normal)
    disabled.update({
        QPalette.WindowText: t["disabled"], QPalette.Text: t["disabled"],
        QPalette.ButtonText: t["disabled"], QPalette.PlaceholderText: t["disabled"],
        QPalette.Button: t["panel"], QPalette.Base: t["panel"],
        QPalette.Highlight: t["border"], QPalette.HighlightedText: t["text"],
    })
    palette = QPalette()
    for group, roles in ((QPalette.Active, normal), (QPalette.Inactive, normal),
                         (QPalette.Disabled, disabled)):
        for role, color in roles.items():
            palette.setColor(group, role, QColor(color))
    return palette


def build_style() -> str:
    """THEME から組み立てたスタイルシート。"""
    t = THEME
    return f"""
    QWidget#bgWindow {{ background-color: {t['bg']}; }}
    QFrame#bgPanel {{ background-color: {t['panel']}; border: 1px solid {t['separator']};
        border-radius: 8px; }}
    QScrollArea#bgPanelScroll, QWidget#bgPanelContent {{ background-color: {t['panel']};
        border: none; }}
    QWidget#bgOutput {{ background-color: {t['panel']}; border: none;
        border-top: 1px solid {t['separator']}; border-bottom-left-radius: 8px;
        border-bottom-right-radius: 8px; }}
    QLabel {{ color: {t['text']}; background: transparent; }}
    QLabel:disabled {{ color: {t['disabled']}; }}
    QLabel#bgNote {{ color: {t['muted']}; }}
    QLabel#bgHeading {{ color: {t['heading']}; font-weight: bold; }}
    QLabel#bgSectionTitle {{ color: {t['heading']}; font-weight: bold; }}
    QLabel#bgStatus {{ color: {t['muted']}; }}
    QLabel#bgStatus[level="warning"] {{ color: {t['warning']}; }}
    QLabel#bgStatus[level="error"] {{ color: {t['error']}; }}
    QLabel#bgKeepChip {{ background-color: {t['keep']}; color: {t['keep_text']};
        border-radius: 3px; padding: 2px 6px; }}
    QFrame#bgSeparator {{ background-color: {t['separator']}; border: none;
        min-height: 1px; max-height: 1px; }}
    QPushButton {{ background-color: {t['field']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 5px; padding: 6px 12px; }}
    QPushButton:hover {{ background-color: {t['field_hover']}; }}
    QPushButton:pressed {{ background-color: {t['field_pressed']}; }}
    QPushButton:checked {{ background-color: {t['accent']}; border-color: {t['accent']};
        color: {t['accent_text']}; }}
    QPushButton:checked:hover {{ background-color: {t['accent_hover']}; }}
    QPushButton:disabled {{ background-color: {t['panel']}; color: {t['disabled']};
        border-color: {t['separator']}; }}
    QPushButton#bgApply {{ background-color: {t['accent']}; border-color: {t['accent']};
        color: {t['accent_text']}; font-weight: bold; padding: 10px 12px; }}
    QPushButton#bgApply:hover {{ background-color: {t['accent_hover']}; }}
    QPushButton#bgApply:pressed {{ background-color: {t['accent_pressed']}; }}
    QToolButton#bgSwatchRemove {{ border: none; background: transparent; color: {t['muted']};
        padding: 0px 3px; }}
    QToolButton#bgSwatchRemove:hover {{ color: {t['error']}; }}
    QCheckBox, QRadioButton {{ color: {t['text']}; spacing: 6px; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {t['disabled']}; }}
    QToolTip {{ background-color: {t['tooltip_bg']}; color: {t['text']};
        border: 1px solid {t['border']}; padding: 4px 6px; }}
    """


def swatch_style(color) -> str:
    """色見本のチップ。地の色は画像の色(データ)なので、枠だけを配色から取る。"""
    return (f"background-color: {_hex(color)}; border: 1px solid {THEME['border']};"
            " border-radius: 3px;")


def apply_theme(app) -> None:
    """このプロセスの QApplication に配色を当てる。main() から1回だけ呼ぶ。

    ツールチップやコンボの一覧は別のトップレベル窓なので、窓のスタイルシートでは
    届かない。アプリ全体に掛ける必要がある。"""
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setStyleSheet(build_style())
    app.setFont(QFont("Meiryo", FONT_BASE))


def _hex(color) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[int(c) for c in color[:3]])


def _checker_brush() -> QBrush:
    """透明部分の下に敷く市松。画像編集ソフトでおなじみの見た目に揃える。"""
    tile = QPixmap(16, 16)
    tile.fill(QColor("#ffffff"))
    painter = QPainter(tile)
    painter.fillRect(0, 0, 8, 8, QColor("#cccccc"))
    painter.fillRect(8, 8, 8, 8, QColor("#cccccc"))
    painter.end()
    return QBrush(tile)


class ImageView(QWidget):
    """画像を枠いっぱいにフィット表示する欄。クリックとホバーを画像座標で知らせる。

    表示している画像(プレビュー解像度)と、座標を返す基準の大きさ(原寸)を別に持つ。
    スポイトは原寸の画素を読みたいが、表示は縮小版で足りるため。"""

    # 原寸の x, y と、押し方(PICK_REPLACE / PICK_ADD / PICK_PROTECT)
    clicked = Signal(int, int, str)
    hovered = Signal(int, int)         # 原寸の x, y。外れたら -1, -1

    def __init__(self, pickable: bool):
        super().__init__()
        self._image = QImage()
        self._source_size = (0, 0)
        self._background = "checker"
        self._markers = []          # Shift の「ここも抜く」点
        self._protect_markers = []  # Ctrl の「ここは残す」点
        self._pickable = pickable
        self._checker = _checker_brush()
        self.setMinimumSize(200, 160)
        if pickable:
            self.setMouseTracking(True)
            self.setCursor(Qt.CrossCursor)

    def set_image(self, image: QImage, source_size=None) -> None:
        self._image = image
        self._source_size = source_size or (image.width(), image.height())
        self.update()

    def set_background(self, kind: str) -> None:
        self._background = kind
        self.update()

    def set_markers(self, points, protect=()) -> None:
        self._markers = list(points)
        self._protect_markers = list(protect)
        self.update()

    def _target_rect(self) -> QRectF:
        if self._image.isNull():
            return QRectF()
        sw, sh = self._source_size
        if sw <= 0 or sh <= 0:
            return QRectF()
        margin = 4
        avail_w = max(1, self.width() - margin * 2)
        avail_h = max(1, self.height() - margin * 2)
        scale = min(avail_w / sw, avail_h / sh)
        w, h = sw * scale, sh * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(THEME["bg"]))
        rect = self._target_rect()
        if rect.isEmpty():
            painter.end()
            return
        if self._background == "checker":
            painter.fillRect(rect, self._checker)
        else:
            painter.fillRect(rect, QColor({"white": "#ffffff", "black": "#000000",
                                           "green": "#00b140"}.get(self._background, THEME["bg"])))
        # 小さい画像を大きく引き伸ばすときはぼかさない(ドット絵やアイコンの縁を
        # 確かめたいのに、補間で滲むと境界の判断ができない)。
        scale = rect.width() / max(1, self._image.width())
        painter.setRenderHint(QPainter.SmoothPixmapTransform, scale < 2.0)
        painter.drawImage(rect, self._image)
        if self._markers or self._protect_markers:
            sw, sh = self._source_size
            painter.setRenderHint(QPainter.Antialiasing, True)

            def center(x, y):
                return QPointF(rect.left() + (x + 0.5) * rect.width() / sw,
                               rect.top() + (y + 0.5) * rect.height() / sh)

            # 「抜く」点は赤い丸、「残す」点は緑の丸にチェック。どちらも黒い縁取りを
            # 下に敷き、白い背景の上でも黒い被写体の上でも見失わないようにする。
            for x, y in self._markers:
                c = center(x, y)
                painter.setPen(QPen(QColor("#000000"), 3.5))
                painter.drawEllipse(c, 5, 5)
                painter.setPen(QPen(QColor("#ff3b3b"), 2))
                painter.drawEllipse(c, 5, 5)
            for x, y in self._protect_markers:
                c = center(x, y)
                painter.setPen(QPen(QColor("#000000"), 1))
                painter.setBrush(QColor("#16a34a"))
                painter.drawEllipse(c, 7, 7)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor("#ffffff"), 2))
                painter.drawLine(QPointF(c.x() - 3.5, c.y()), QPointF(c.x() - 1, c.y() + 2.8))
                painter.drawLine(QPointF(c.x() - 1, c.y() + 2.8), QPointF(c.x() + 3.8, c.y() - 2.8))
        painter.end()

    def _to_source(self, pos):
        rect = self._target_rect()
        if rect.isEmpty() or not rect.contains(QPointF(pos)):
            return None
        sw, sh = self._source_size
        x = int((pos.x() - rect.left()) * sw / rect.width())
        y = int((pos.y() - rect.top()) * sh / rect.height())
        return min(max(x, 0), sw - 1), min(max(y, 0), sh - 1)

    def mousePressEvent(self, event):
        if not self._pickable or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        point = self._to_source(event.position())
        if point is not None:
            modifiers = event.modifiers()
            # Ctrl を先に見る。Ctrl+Shift のように両方押していたら「残す」として扱う
            # (取り消しの利かない「抜く」より、害の小さい側へ倒す)。
            if modifiers & Qt.ControlModifier:
                kind = PICK_PROTECT
            elif modifiers & Qt.ShiftModifier:
                kind = PICK_ADD
            else:
                kind = PICK_REPLACE
            self.clicked.emit(point[0], point[1], kind)

    def mouseMoveEvent(self, event):
        if self._pickable:
            point = self._to_source(event.position())
            self.hovered.emit(*(point if point is not None else (-1, -1)))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._pickable:
            self.hovered.emit(-1, -1)
        super().leaveEvent(event)


class _AiBridge(QObject):
    """AI のワーカースレッドから窓(メインスレッド)へ結果を戻す器。
    ワーカーから Qt の部品を直に触ると壊れるので、必ずシグナルを経由する
    (feature_screen._PushoverBridge と同じ流儀)。"""

    finished = Signal(object)


def _ai_worker(bridge: _AiBridge, rgb, model: str) -> None:
    """別スレッドで rembg を回す。ここで投げた例外はどこにも捕まらないので全部受ける。"""
    started = time.monotonic()
    result = {"model": model}
    try:
        result["alpha"] = run_ai(rgb, model)
        result["ok"] = True
    except Exception:
        result["ok"] = False
        result["error"] = _log_exception(f"ai model={model}")
    result["seconds"] = time.monotonic() - started
    try:
        bridge.finished.emit(result)
    except RuntimeError:
        pass  # 待っている間に窓が閉じられた。渡す先が無いだけ


class BgRemoveWindow(QWidget):
    """背景を抜いた結果を見ながら調整し、クリップボードへ載せる/保存する窓。"""

    # 窓が閉じた。引数は「プロセスを終えるまでに待つ ms」(閉じ際に出したトーストを
    # 見せ切るため。何も出していなければ 0)。
    finished = Signal(int)

    def __init__(self, rgba, name_hint=None, app_settings=None, settings_path=None):
        super().__init__()
        self._app_settings = app_settings
        self._settings_path = settings_path
        self._name_hint = name_hint
        self._linger_ms = 0

        rgba = np.asarray(rgba)
        self._rgb = np.ascontiguousarray(rgba[..., :3])
        alpha = rgba[..., 3]
        # 元から不透明なら持たない(全画素 255 を毎回掛け合わせても結果は同じで、遅いだけ)。
        self._src_alpha = alpha.copy() if (alpha < 255).any() else None
        self._height, self._width = self._rgb.shape[:2]

        self._p_rgb, self._p_scale = resize_long_side(self._rgb, PREVIEW_LONG_SIDE)
        self._p_src_alpha = None
        if self._src_alpha is not None:
            self._p_src_alpha, _ = resize_long_side(self._src_alpha, PREVIEW_LONG_SIDE)

        # 背景色の一覧。スポイトで指した点(seed)を色と組で持つ。色を消すと、その色を
        # 拾った点も起点から外れてほしいため。自動推定の色は点を持たない。
        self._entries = []
        self._reset_to_estimate(schedule=False)
        # Ctrl+クリックの「ここは残す」点(原寸の座標)。色は持たない。背景色の推定にも
        # 距離の計算にも関わらず、アルファを作る最後の段で成分ごと不透明に戻すだけ。
        self._protect = []
        # 直近のプレビューで効かなかった残す点(外側の背景とつながっていたもの)の番号。
        self._protect_rejected = []

        # 距離の計算は背景色が変わったときだけで済む(スライダーでは変わらない)。
        self._dist_cache = {}
        self._gradient_cache = None
        self._ai_alpha = None
        self._ai_alpha_preview = None
        self._ai_running = False
        self._ai_bridge = _AiBridge()
        self._ai_bridge.finished.connect(self._on_ai_finished)

        section = _section(app_settings)
        self.setWindowTitle("背景を透過")
        self.setObjectName("bgWindow")
        # 最前面に出す。DETACHED で起こされるため、Windows の前面化ロックで後ろに回されると
        # 「押したのに何も出ない」に見える(clipboard_preview も同じ理由で最前面にしている)。
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        # アプリ全体の配色(apply_theme)と同じものを窓にも掛けておく。テストなどで
        # apply_theme を通さずに窓だけ作った場合にも、見た目が崩れないように。
        self.setStyleSheet(build_style())
        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)

        saved_bg = section.get("preview_bg", "checker")
        if saved_bg not in {key for key, _ in PREVIEW_BACKGROUNDS}:
            saved_bg = "checker"
        self._preview_bg = saved_bg

        # 左に画像(元画像｜結果)と状態欄、右に設定の縦1列のパネル。以前は設定を画像の
        # 上に横長の行で並べていて、視線があちこちに飛び、スライダーが窓の幅いっぱいに
        # 伸びてラベルと数値が両端に離れた。作業の順に上から下へ読めるようにしてある。
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(6)
        left.addWidget(self._build_panes(), 1)
        self.status = QLabel("")
        self.status.setObjectName("bgStatus")
        self.status.setFont(self._font(FONT_SMALL))
        # 長い文言(警告など)で窓の最小幅を押し広げさせない。全文はツールチップにも置く。
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        left.addWidget(self.status)
        root.addLayout(left, 1)
        root.addWidget(self._build_panel(section))

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_preview)

        self._install_shortcuts()
        self._resize_to_saved(section)
        self._move_to_center()

        self.source_view.set_image(array_to_qimage(self._preview_source_rgba()),
                                   (self._width, self._height))
        self._rebuild_swatches()
        self._update_method_controls()
        self._apply_auto_tolerance()
        self._refresh_preview()

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------
    def _font(self, size=FONT_BASE, bold=False):
        font = QFont("Meiryo", size)
        font.setBold(bold)
        return font

    def _heading(self, text):
        label = QLabel(text)
        label.setObjectName("bgHeading")
        label.setFont(self._font())
        return label

    def _note(self, text=""):
        """補足説明(グレーの小さい字)。ラベルと見分けがつくよう、必ずこれを通す。"""
        label = QLabel(text)
        label.setObjectName("bgNote")
        label.setFont(self._font(FONT_SMALL))
        label.setWordWrap(True)
        return label

    def _segment_row(self, items, group):
        """セグメント(横に並んだトグルボタン)。items は (key, 表示) の並び。
        ボタンを {key: ボタン} で返す。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        buttons = {}
        for key, label in items:
            button = QPushButton(label)
            button.setFont(self._font())
            button.setCheckable(True)
            group.addButton(button)
            row.addWidget(button, 1)
            buttons[key] = button
        return row, buttons

    # ---- 右の設定パネル -------------------------------------------------
    def _build_panel(self, section):
        panel = QFrame()
        panel.setObjectName("bgPanel")
        panel.setFixedWidth(PANEL_WIDTH)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 窓の高さが足りないときは、パネルの中身だけがスクロールする(出力ボタンは下に固定)。
        scroll = QScrollArea()
        scroll.setObjectName("bgPanelScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("bgPanelContent")
        column = QVBoxLayout(content)
        column.setContentsMargins(14, 14, 14, 14)
        column.setSpacing(6)

        sections = [
            ("抜き方", self._build_method_section(section)),
            ("背景色", self._build_background_section()),
            ("残す色／点", self._build_protect_section()),
            ("調整", self._build_adjust_section(section)),
            ("仕上げ", self._build_finish_section(section)),
            ("表示", self._build_display_section()),
        ]
        for index, (title, layout) in enumerate(sections):
            if index:
                column.addSpacing(8)
                separator = QFrame()
                separator.setObjectName("bgSeparator")
                separator.setFrameShape(QFrame.NoFrame)
                column.addWidget(separator)
                column.addSpacing(6)
            label = QLabel(title)
            label.setObjectName("bgSectionTitle")
            label.setFont(self._font(FONT_TITLE, bold=True))
            if title == "残す色／点":
                self._protect_heading = label
                label.setToolTip(
                    "Ctrl+クリックで指した部分は透明にしない（色で抜く・AIの結果のどちらでも効く）")
            column.addWidget(label)
            column.addLayout(layout)
        column.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        outer.addWidget(self._build_output_section())
        return panel

    def _build_method_section(self, section):
        layout = QVBoxLayout()
        layout.setSpacing(6)
        self._method_group = QButtonGroup(self)
        self._method_group.setExclusive(True)
        row, buttons = self._segment_row(
            [(METHOD_COLOR, "色で抜く"), (METHOD_AI, "AIで抜く")], self._method_group)
        self.color_mode_button = buttons[METHOD_COLOR]
        self.ai_mode_button = buttons[METHOD_AI]
        self.color_mode_button.setChecked(True)
        self.color_mode_button.setToolTip("背景色・許容量・ぼかしで抜く")
        # 以前の「AIの結果」ラジオは、先に別のボタンで AI を走らせるまで押せず、両者の
        # 関係が分かりにくかった。いまは「AIで抜く」を選ぶこと自体が実行の合図になる。
        self._method_group.buttonClicked.connect(self._on_method_clicked)
        layout.addLayout(row)

        # AI を選んだときだけ出す: モデル・実行(やり直す)・所要時間。
        self.ai_box = QWidget()
        ai_layout = QVBoxLayout(self.ai_box)
        ai_layout.setContentsMargins(0, 2, 0, 0)
        ai_layout.setSpacing(6)
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        model_label = QLabel("モデル")
        model_label.setFont(self._font())
        model_row.addWidget(model_label)
        self.model_combo = QComboBox()
        self.model_combo.setFont(self._font())
        for key, label in AI_MODELS:
            self.model_combo.addItem(label, key)
        saved_model = section.get("model")
        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == saved_model:
                self.model_combo.setCurrentIndex(i)
        model_row.addWidget(self.model_combo, 1)
        ai_layout.addLayout(model_row)
        self.ai_button = QPushButton("実行")
        self.ai_button.setFont(self._font())
        self.ai_button.clicked.connect(self._start_ai)
        ai_layout.addWidget(self.ai_button)
        self.ai_status = self._note("")
        ai_layout.addWidget(self.ai_status)
        layout.addWidget(self.ai_box)

        if not ai_available():
            for widget in (self.ai_mode_button, self.ai_button, self.model_combo):
                widget.setEnabled(False)
                widget.setToolTip(AI_INSTALL_HINT)
        else:
            tip = "被写体をAIで切り抜く（初回はライブラリの読み込みに数十秒かかります）"
            self.ai_mode_button.setToolTip(tip)
            self.ai_button.setToolTip(tip)

        self.method_note = self._note(METHOD_NOTES[METHOD_COLOR])
        layout.addWidget(self.method_note)
        return layout

    def _build_background_section(self):
        layout = QVBoxLayout()
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(6)
        # 色が何個あっても横にはみ出さないよう、格子に並べる(パネルの幅は固定)。
        self._swatch_grid = QGridLayout()
        self._swatch_grid.setHorizontalSpacing(4)
        self._swatch_grid.setVerticalSpacing(4)
        row.addLayout(self._swatch_grid)
        row.addStretch(1)
        reset = QPushButton("自動推定に戻す")
        reset.setFont(self._font(FONT_SMALL))
        reset.setToolTip("画像の外周でいちばん多い色を背景色にし直す")
        reset.clicked.connect(lambda: self._reset_to_estimate())
        row.addWidget(reset, 0, Qt.AlignTop)
        layout.addLayout(row)
        # AIの結果では背景色は抜く判定に使わない。書いておかないと「背景色を変えたのに
        # 結果が変わらない」と迷う。
        self.bg_ai_note = self._note("AIで抜くときは、色かぶり除去にだけ使います")
        layout.addWidget(self.bg_ai_note)
        return layout

    def _build_protect_section(self):
        layout = QVBoxLayout()
        layout.setSpacing(4)
        # 残す色は背景色とは別に並べる(混ぜると「この色が背景色に入った」と読み違える)。
        self._protect_list = QVBoxLayout()
        self._protect_list.setSpacing(4)
        layout.addLayout(self._protect_list)
        return layout

    def _make_slider(self, layout, title, maximum, value, tooltip, extra=None):
        """「ラベル……数値」の1行と、その下にパネル幅いっぱいのスライダー。"""
        header = QHBoxLayout()
        header.setSpacing(6)
        label = QLabel(title)
        label.setFont(self._font())
        label.setToolTip(tooltip)
        header.addWidget(label, 1)
        if extra is not None:
            header.addWidget(extra)
        value_label = QLabel(str(value))
        value_label.setFont(self._font(bold=True))
        value_label.setMinimumWidth(30)
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(value_label)
        layout.addLayout(header)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, maximum)
        slider.setValue(value)
        slider.setToolTip(tooltip)
        layout.addWidget(slider)
        slider.valueChanged.connect(lambda v: (value_label.setText(str(v)), self._schedule()))
        return slider, label

    def _build_adjust_section(self, section):
        """モードごとに効く項目だけを出す。効かない項目はグレーにせず隠す。

        グレーにして並べておくと「なぜ動かないのか」「どれを触れば変わるのか」を毎回
        読み解くことになる(実機で、AIの結果を見ながら効かない許容量を触っていた)。"""
        layout = QVBoxLayout()
        layout.setSpacing(0)
        self.mode_pages = QStackedWidget()

        color_page = QWidget()
        color = QVBoxLayout(color_page)
        color.setContentsMargins(0, 0, 0, 0)
        color.setSpacing(4)
        # 許容量は覚えない。画像ごとに背景のムラの大きさが違い、前の画像に合わせた値を
        # 持ち越すと、薄いボックスごと抜ける(以前の既定 12 で実際に起きた)。開くたびに
        # 外周のムラから測った「自動」の値で始める。
        self.auto_tolerance_button = QPushButton("自動")
        self.auto_tolerance_button.setFont(self._font(FONT_SMALL))
        self.auto_tolerance_button.setCheckable(True)
        self.auto_tolerance_button.setChecked(True)
        self.auto_tolerance_button.setToolTip(
            "外周の背景のムラ(ばらつきの上限)を測って許容量を決め直す。\n"
            "押されている間は、背景色を変えたときにも測り直す。つまみを動かすと手動に戻る")
        self.auto_tolerance_button.clicked.connect(self._on_auto_tolerance_clicked)
        self.tolerance_slider, self._tolerance_label = self._make_slider(
            color, "許容量（ΔE）", TOLERANCE_MAX, AUTO_TOLERANCE_MIN,
            "背景色からこの差(ΔE)までを完全に透明にする。2前後が見分けられる限界の差",
            extra=self.auto_tolerance_button,
        )
        self.tolerance_slider.valueChanged.connect(self._on_tolerance_moved)
        color.addSpacing(6)
        # ぼかしは覚える(画像よりも好みで決まる値なので)。キーを feather から edge_feather へ
        # 変えたのは、以前の既定 10 が閉じるたびに書き込まれていて、それを好みとして
        # 引き継ぐと薄いボックスの縁が溶けるため。
        self.feather_slider, self._feather_label = self._make_slider(
            color, "境界のぼかし（ΔE）", FEATHER_MAX,
            _int_setting(section, "edge_feather", DEFAULT_FEATHER, 0, FEATHER_MAX),
            "許容量からさらにこの差までを、離れるほど不透明になる半透明にする",
        )
        color.addSpacing(8)
        self._build_inside_controls(color)
        self.mode_pages.addWidget(color_page)

        ai_page = QWidget()
        ai = QVBoxLayout(ai_page)
        ai.setContentsMargins(0, 0, 0, 0)
        ai.setSpacing(4)
        # AIの結果の一括調整。lo 未満を消し、hi 以上を不透明にする。白い被写体を AI が
        # 背景と取り違えて半透明にしたとき、点を打たずにまとめて濃くする(hi を下げる)。
        # 背景に薄く残ったもや(接地影など)は lo を上げて消す。画像ごとに変わるので覚えない。
        self.ai_lo_slider, _label = self._make_slider(
            ai, "これより薄い部分は消す（%）", 99, 0,
            "AIの判定がこの値より薄い部分を完全に透明にする（背景に残ったもやを消す）")
        ai.addSpacing(6)
        self.ai_hi_slider, _label = self._make_slider(
            ai, "これより濃い部分は不透明に（%）", 100, 100,
            "AIの判定がこの値より濃い部分を完全に不透明にする（半透明になった白い被写体を戻す）")
        self.ai_lo_slider.setMinimum(0)
        self.ai_hi_slider.setMinimum(1)
        self.mode_pages.addWidget(ai_page)

        layout.addWidget(self.mode_pages)
        return layout

    def _build_inside_controls(self, layout):
        """画像の内側にある背景色に近い色を、残すか抜くか(一括の切り替え)と、ムラへの追従。

        以前は「範囲: 外周からつながった部分だけ／画像全体の同色」と書いていたが、
        仕組みの名前で、何のための設定かが読み取れなかった。困りごとの側(被写体の中に
        背景と似た色がある)から名付け直してある。1か所ずつの指定は左ペインの
        Shift+クリック(ここも抜く)/ Ctrl+クリック(ここは残す)で行う。"""
        tooltip = (
            "残す: 外周からつながった背景だけを抜き、被写体の内側にある白目やハイライトなどを守る\n"
            "抜く: 文字の穴や腕と胴の隙間など、外周に接していない部分の同じ色も抜く\n"
            "1か所ずつ決めるなら、左の画像で Shift+クリック（ここも抜く）／Ctrl+クリック（ここは残す）")
        self._range_label = QLabel("画像の内側にある背景色に近い色")
        self._range_label.setFont(self._font())
        self._range_label.setToolTip(tooltip)
        self._range_label.setWordWrap(True)
        layout.addWidget(self._range_label)
        self.range_combo = QComboBox()
        self.range_combo.setFont(self._font())
        self.range_combo.addItem("残す（外周からつながった背景だけ抜く）", RANGE_CONNECTED)
        self.range_combo.addItem("抜く（画像全体の同色を抜く）", RANGE_GLOBAL)
        # この設定は覚えない。「抜く」で閉じた次の画像で被写体の白目が抜けているのに
        # 気付かず載せる、という事故のほうが、毎回選び直す手間より重い。
        self.range_combo.setToolTip(tooltip)
        self.range_combo.currentIndexChanged.connect(lambda _i: self._schedule())
        layout.addWidget(self.range_combo)
        layout.addSpacing(6)
        self.follow_check = QCheckBox("背景のムラに追従")
        self.follow_check.setFont(self._font())
        self.follow_check.setChecked(bool(_section(self._app_settings).get("follow_gradient", True)))
        self.follow_check.setToolTip(
            "背景を一定の色ではなく、画像全体でゆるく変わる面として推定する。\n"
            "AI の画像に多いビネット(四隅が暗い)やグラデーションで、中央と四隅で\n"
            "同じ許容量が合わない問題を防ぐ。ムラが無い画像では何も変わらない")
        self.follow_check.toggled.connect(lambda _c: self._colors_changed())
        layout.addWidget(self.follow_check)

    def _build_finish_section(self, section):
        layout = QVBoxLayout()
        layout.setSpacing(6)
        self.decontam_check = QCheckBox("色かぶり除去")
        self.decontam_check.setFont(self._font())
        self.decontam_check.setChecked(bool(section.get("decontaminate", True)))
        self.decontam_check.setToolTip("半透明の縁から背景色の混ざりを取り除く（白背景の縁が光るのを防ぐ）")
        self.decontam_check.toggled.connect(lambda _c: self._schedule())
        layout.addWidget(self.decontam_check)
        self.trim_check = QCheckBox("余白を切り詰める")
        self.trim_check.setFont(self._font())
        self.trim_check.setChecked(bool(section.get("trim", False)))
        self.trim_check.setToolTip("透明になった余白を落として、残った部分の外接矩形にする")
        self.trim_check.toggled.connect(lambda _c: self._schedule())
        layout.addWidget(self.trim_check)
        return layout

    def _build_display_section(self):
        layout = QVBoxLayout()
        layout.setSpacing(4)
        label = QLabel("結果の下に敷く背景")
        label.setFont(self._font())
        layout.addWidget(label)
        self._bg_group = QButtonGroup(self)
        self._bg_group.setExclusive(True)
        row, buttons = self._segment_row(PREVIEW_BACKGROUNDS, self._bg_group)
        for key, button in buttons.items():
            button.setChecked(key == self._preview_bg)
            button.clicked.connect(lambda _c, k=key: self._set_preview_bg(k))
        layout.addLayout(row)
        layout.addWidget(self._note("抜け残りは黒、縁の白い光りは黒か緑で見ると分かりやすい"))
        return layout

    def _build_output_section(self):
        """パネルの最下部に固定する出力ボタン。キーのヒントはツールチップへ。"""
        box = QWidget()
        box.setObjectName("bgOutput")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(8)
        self.apply_button = QPushButton("クリップボードへ")
        self.apply_button.setObjectName("bgApply")
        self.apply_button.setFont(self._font(FONT_TITLE, bold=True))
        self.apply_button.setDefault(True)
        self.apply_button.setMinimumHeight(40)
        self.apply_button.setToolTip("透過PNGをクリップボードへ載せて閉じる（Enter）")
        self.apply_button.clicked.connect(self._to_clipboard)
        layout.addWidget(self.apply_button)
        row = QHBoxLayout()
        row.setSpacing(8)
        save = QPushButton("保存…")
        save.setFont(self._font())
        save.setToolTip("透過PNGとして保存する（Ctrl+S）")
        save.clicked.connect(self._save)
        row.addWidget(save, 1)
        cancel = QPushButton("キャンセル")
        cancel.setFont(self._font())
        cancel.setToolTip("何もせずに閉じる（Esc）")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel, 1)
        layout.addLayout(row)
        return box

    # ---- 左の画像 -------------------------------------------------------
    def _build_panes(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.source_view = ImageView(pickable=True)
        self.source_view.clicked.connect(self._on_pick)
        self.source_view.hovered.connect(self._on_hover)
        self.result_view = ImageView(pickable=False)
        self.result_view.set_background(self._preview_bg)
        # 操作のヒントは見出しの横に残す。キーの部分だけを強調して短くする。
        key = (f"<span style='background-color:{THEME['field_hover']}; color:{THEME['heading']};'>"
               "&nbsp;{}&nbsp;</span>")
        hint = (f"{key.format('クリック')} 背景色　"
                f"{key.format('Shift')}+クリック 抜く　"
                f"{key.format('Ctrl')}+クリック 残す")
        splitter.addWidget(self._wrap_pane("元の画像", self.source_view, hint))
        splitter.addWidget(self._wrap_pane("結果", self.result_view))
        splitter.setSizes([1, 1])
        return splitter

    def _wrap_pane(self, title, view, hint=None):
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self._heading(title))
        if hint:
            label = QLabel(hint)
            label.setTextFormat(Qt.RichText)
            label.setFont(self._font(FONT_SMALL))
            # 折り返して、文字幅で左ペインの最小幅を押し広げない。
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            header.addWidget(label, 1)
        else:
            header.addStretch(1)
        column.addLayout(header)
        column.addWidget(view, 1)
        return box

    def _install_shortcuts(self):
        """キーは QShortcut で持つ(keyPressEvent だとスライダーやコンボに先に食われる)。"""
        for sequence in ("Return", "Enter"):
            QShortcut(QKeySequence(sequence), self).activated.connect(self._to_clipboard)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._save)
        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

    # ------------------------------------------------------------------
    # 大きさ・位置(clipboard_preview と同じ作法。位置は毎回カーソルの画面の中央)
    # ------------------------------------------------------------------
    def _screen(self):
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def _resize_to_saved(self, section):
        area = self._screen().availableGeometry()
        width = int(area.width() * DEFAULT_WIDTH_RATIO)
        height = int(area.height() * DEFAULT_HEIGHT_RATIO)
        size = section.get("window_size")
        try:
            if isinstance(size, (list, tuple)) and len(size) == 2:
                w, h = int(size[0]), int(size[1])
                if w >= MIN_WIDTH and h >= MIN_HEIGHT:
                    width, height = w, h
        except (TypeError, ValueError):
            pass
        # 覚えてある大きさが、いまの画面に収まるとは限らない(モニタ構成が変わる)。
        width = max(MIN_WIDTH, min(width, area.width()))
        height = max(MIN_HEIGHT, min(height, area.height()))
        self.resize(width, height)

    def _move_to_center(self):
        area = self._screen().availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.center().y() - self.height() // 2
        x = max(area.left(), min(x, area.right() - self.width()))
        y = max(area.top(), min(y, area.bottom() - self.height()))
        self.move(x, y)

    # ------------------------------------------------------------------
    # 背景色(スポイト)
    # ------------------------------------------------------------------
    def _colors(self):
        return [entry["color"] for entry in self._entries]

    def _seeds(self, shift_only=False):
        """抜く起点。shift_only なら Shift+クリックで足した点だけ。

        AIの結果では、ただのクリック(背景色の置き換え)の点は起点にしない。背景色を
        指すクリックはたいてい外側の背景の上で、そこを「抜く」と言われても AI では
        何も変わらず、効かなかったという警告が出るだけになるため。"""
        return [entry["seed"] for entry in self._entries
                if entry["seed"] is not None and (not shift_only or entry.get("kind") == PICK_ADD)]

    def _reset_to_estimate(self, schedule=True):
        color = estimate_background(self._rgb, self._src_alpha)
        self._entries = [{"color": color, "seed": None}]
        if schedule:
            self._colors_changed()

    def _on_pick(self, x, y, kind):
        try:
            if kind == PICK_PROTECT:
                # 残す点は位置と色を組で持つ。色は「色で抜く」で背景から切り離すのに、
                # 位置は点を含む部分を残すのと、AI の結果での補正に使う。
                if all(entry["pos"] != (x, y) for entry in self._protect):
                    color = tuple(int(c) for c in self._rgb[y, x])
                    self._protect.append({"pos": (x, y), "color": color})
                self._colors_changed()
                return
            color = tuple(int(c) for c in self._rgb[y, x])
            entry = {"color": color, "seed": (x, y), "kind": kind}
            if kind == PICK_ADD:
                # 同じ色を2つ持っても距離は変わらない。点だけ足したいので色は重ねてよい。
                self._entries.append(entry)
            else:
                self._entries = [entry]
            self._colors_changed()
        except Exception:
            self._set_status(f"スポイトに失敗しました: {_log_exception('pick')}", "error")

    def _remove_entry(self, index):
        try:
            if 0 <= index < len(self._entries):
                del self._entries[index]
            self._colors_changed()
        except Exception:
            self._set_status(f"背景色を消せませんでした: {_log_exception('remove color')}", "error")

    def _remove_protect(self, index):
        try:
            if 0 <= index < len(self._protect):
                del self._protect[index]
            self._colors_changed()
        except Exception:
            self._set_status(f"残す点を消せませんでした: {_log_exception('remove protect')}", "error")

    def _protect_points(self):
        return [entry["pos"] for entry in self._protect]

    def _keep_colors(self):
        return [entry["color"] for entry in self._protect]

    def _colors_changed(self):
        self._rebuild_swatches()
        # 背景色・残す色・追従の切り替えで背景の推定が変わるので、自動のままなら測り直す。
        # 手で動かした許容量は上書きしない。
        if self.auto_tolerance_button.isChecked():
            self._apply_auto_tolerance()
        self._schedule()

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    SWATCH_COLUMNS = 5

    def _rebuild_swatches(self):
        self._clear_layout(self._swatch_grid)
        if not self._entries:
            self._swatch_grid.addWidget(self._note("（なし）クリックで指定"), 0, 0)
        for index, entry in enumerate(self._entries):
            box = QWidget()
            inner = QHBoxLayout(box)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(0)
            chip = QLabel()
            chip.setFixedSize(22, 22)
            chip.setStyleSheet(swatch_style(entry["color"]))
            where = "自動推定" if entry["seed"] is None else f"({entry['seed'][0]}, {entry['seed'][1]}) から"
            chip.setToolTip(f"{_hex(entry['color'])}  {where}")
            inner.addWidget(chip)
            remove = QToolButton()
            remove.setObjectName("bgSwatchRemove")
            remove.setText("×")
            remove.setToolTip("この背景色を外す")
            remove.clicked.connect(lambda _c=False, i=index: self._remove_entry(i))
            inner.addWidget(remove)
            self._swatch_grid.addWidget(box, index // self.SWATCH_COLUMNS,
                                        index % self.SWATCH_COLUMNS)
        self._rebuild_protect_list()
        self.source_view.set_markers(self._seeds(), self._protect_points())

    def _rebuild_protect_list(self):
        self._clear_layout(self._protect_list)
        if not self._protect:
            self._protect_list.addWidget(self._note("Ctrl+クリックで追加"))
        for index, entry in enumerate(self._protect):
            x, y = entry["pos"]
            box = QWidget()
            inner = QHBoxLayout(box)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(2)
            # 残す色のチップ。緑の ✓ は「残す」印で、その左に実際に残す色を並べる
            # (薄い青と薄い黄を両方登録したとき、どれがどれか見分けるため)。
            swatch = QLabel()
            swatch.setFixedSize(18, 18)
            swatch.setStyleSheet(swatch_style(entry["color"]))
            swatch.setToolTip(f"残す色 {_hex(entry['color'])}")
            inner.addWidget(swatch)
            chip = QLabel(f"✓ 残す　{_hex(entry['color'])}　({x}, {y})")
            chip.setObjectName("bgKeepChip")
            chip.setFont(self._font(FONT_SMALL))
            chip.setToolTip(
                f"色で抜く: {_hex(entry['color'])} に近い色を背景から切り離して残し、"
                f"({x}, {y}) を含む部分も残す\nAIの結果: ({x}, {y}) を含む半透明の部分を不透明にする")
            inner.addWidget(chip)
            remove = QToolButton()
            remove.setObjectName("bgSwatchRemove")
            remove.setText("×")
            remove.setToolTip("この残す点を外す")
            remove.clicked.connect(lambda _c=False, i=index: self._remove_protect(i))
            inner.addWidget(remove)
            inner.addStretch(1)
            self._protect_list.addWidget(box)

    def _set_status(self, text, level=""):
        """状態欄に出す。level は "" / "warning" / "error"(色は配色から)。"""
        self.status.setText(text)
        self.status.setToolTip(text)
        if self.status.property("level") != level:
            self.status.setProperty("level", level)
            self.status.style().unpolish(self.status)
            self.status.style().polish(self.status)

    def _on_hover(self, x, y):
        try:
            if x < 0:
                self._set_status(self._summary, self._summary_level)
                return
            color = tuple(int(c) for c in self._rgb[y, x])
            text = f"({x}, {y})  {_hex(color)}"
            if self._entries:
                coeffs = self._gradient_coeffs()
                offset = offset_at(coeffs, x, y, self._height, self._width).reshape(1, 1, 3)
                dist, _ = color_distance(np.array([[color]], dtype=np.uint8), self._colors(),
                                         offset if coeffs is not None else None)
                text += f"  背景色との差 ΔE {float(dist[0, 0]):.1f}"
            self._set_status(text)
        except Exception:
            pass  # 状態表示が出ないだけ。ホバーごとに例外を積むよりは黙る

    # ------------------------------------------------------------------
    # 抜き方
    # ------------------------------------------------------------------
    def _selected_method(self):
        """パネルで選ばれている抜き方(AI がまだ走っていなくても AI を返す)。"""
        return METHOD_AI if self.ai_mode_button.isChecked() else METHOD_COLOR

    def _method(self):
        """実際に計算に使う抜き方。AI を選んでいても結果がまだ無ければ色で抜く。"""
        if self._selected_method() == METHOD_AI and self._ai_alpha is not None:
            return METHOD_AI
        return METHOD_COLOR

    def _on_method_clicked(self, _button=None):
        try:
            # AI を選んだのに結果がまだ無ければ、そのまま実行を始める(選んだ＝やりたい)。
            if (self._selected_method() == METHOD_AI and self._ai_alpha is None
                    and not self._ai_running):
                self._start_ai()
            self._update_method_controls()
            self._schedule()
        except Exception:
            self._set_status(f"抜き方を切り替えられませんでした: {_log_exception('method')}", "error")

    def _update_method_controls(self):
        selected = self._selected_method()
        page = 1 if selected == METHOD_AI else 0
        self.mode_pages.setCurrentIndex(page)
        # 隠れているページの高さぶん空白が残らないよう、見えていないページは大きさを
        # 主張させない(QStackedWidget は既定で全ページの最大の大きさを取る)。
        for i in range(self.mode_pages.count()):
            self.mode_pages.widget(i).setSizePolicy(
                QSizePolicy.Preferred,
                QSizePolicy.Preferred if i == page else QSizePolicy.Ignored)
        self.mode_pages.adjustSize()
        self.ai_box.setVisible(selected == METHOD_AI)
        self.ai_button.setText("やり直す" if self._ai_alpha is not None else "実行")
        self.method_note.setText(METHOD_NOTES[selected])
        self.bg_ai_note.setVisible(selected == METHOD_AI)

    def _set_preview_bg(self, key):
        self._preview_bg = key
        self.result_view.set_background(key)

    def _start_ai(self):
        if self._ai_running or not ai_available():
            return
        model = self.model_combo.currentData()
        self._ai_running = True
        self.ai_button.setEnabled(False)
        self.model_combo.setEnabled(False)
        self.ai_status.setText("AIで抜いています…（初回はライブラリの読み込みに数十秒かかります）")
        self._update_method_controls()
        thread = threading.Thread(
            target=_ai_worker, args=(self._ai_bridge, self._rgb, model),
            name="bg_remove-ai", daemon=True,
        )
        thread.start()

    def _on_ai_finished(self, result):
        try:
            self._ai_running = False
            self.ai_button.setEnabled(True)
            self.model_combo.setEnabled(True)
            if not result.get("ok"):
                self.ai_status.setText(f"AIで抜けませんでした: {result.get('error', '')}")
                # 一度も結果が無いまま AI を選んだ状態にしておくと、効かない AI の項目が
                # 並んだままになる。色で抜くへ戻す。
                if self._ai_alpha is None:
                    self.color_mode_button.setChecked(True)
                self._update_method_controls()
                self._schedule()
                return
            self._ai_alpha = result["alpha"]
            self._ai_alpha_preview, _ = resize_long_side(self._ai_alpha, PREVIEW_LONG_SIDE)
            self.ai_status.setText(f"{result['model']}　{result['seconds']:.1f}秒")
            self.ai_mode_button.setChecked(True)
            self._update_method_controls()
            self._schedule()
        except Exception:
            self.ai_status.setText(f"AIの結果を扱えませんでした: {_log_exception('ai finished')}")

    # ------------------------------------------------------------------
    # 計算
    # ------------------------------------------------------------------
    def _schedule(self):
        self._debounce.start()

    def _preview_source_rgba(self):
        alpha = self._p_src_alpha
        if alpha is None:
            alpha = np.full(self._p_rgb.shape[:2], 255, dtype=np.uint8)
        return np.dstack((self._p_rgb, alpha))

    def _gradient_key(self):
        return (tuple(self._colors()), tuple(self._keep_colors()), self.follow_check.isChecked())

    def _gradient_coeffs(self):
        """背景のムラの当てはめ(係数)。追従しないときや当てはめられないときは None。

        当てはめは縮小プレビューの画像で1回だけ行い、原寸にも同じ係数を使う(座標を
        画像に対する割合で表してあるので、そのまま使える)。原寸で当てはめ直すと、確定の
        たびに待たされるうえ、プレビューと確定で結果がずれる。"""
        key = self._gradient_key()
        if self._gradient_cache is not None and self._gradient_cache[0] == key:
            return self._gradient_cache[1]
        coeffs = None
        if self.follow_check.isChecked() and self._colors():
            coeffs = fit_background_offset(self._p_rgb, self._colors(), self._keep_colors())
        self._gradient_cache = (key, coeffs)
        return coeffs

    def _cached(self, name, preview, key, make):
        """プレビュー用と原寸用を1つずつだけ持つキャッシュ(背景色を変えるたびに積むと、
        大きな画像でメモリを食い潰す)。"""
        slot = (name, preview)
        cached = self._dist_cache.get(slot)
        if cached is None or cached[0] != key:
            cached = (key, make())
            self._dist_cache[slot] = cached
        return cached[1]

    def _offset(self, preview: bool):
        coeffs = self._gradient_coeffs()
        rgb = self._p_rgb if preview else self._rgb
        return self._cached("offset", preview, self._gradient_key(),
                            lambda: background_offset(coeffs, *rgb.shape[:2]))

    def _distance(self, preview: bool):
        """(背景色までの距離, いちばん近い背景色の番号, 場所ごとのずれ)。"""
        offset = self._offset(preview)
        rgb = self._p_rgb if preview else self._rgb
        dist, index = self._cached("dist", preview, self._gradient_key(),
                                   lambda: color_distance(rgb, self._colors(), offset))
        return dist, index, offset

    def _keep_distance(self, preview: bool):
        """残す色までの距離。残す色が無ければ None。

        残す色は拾った場所でのムラを差し引いて「ずれの無い色」に戻し、測る場所のずれを
        足して比べる。ビネットで四隅が暗い画像では、ボックスの色も四隅ほど暗く写るため。"""
        if not self._protect:
            return None
        coeffs = self._gradient_coeffs()
        offset = self._offset(preview)
        rgb = self._p_rgb if preview else self._rgb
        key = (self._gradient_key(), tuple(self._protect_points()))

        def make():
            color_offsets = None
            if coeffs is not None:
                color_offsets = [offset_at(coeffs, x, y, self._height, self._width)
                                 for x, y in self._protect_points()]
            return color_distance(rgb, self._keep_colors(), offset, color_offsets)[0]

        return self._cached("keep", preview, key, make)

    # ------------------------------------------------------------------
    # 許容量の自動
    # ------------------------------------------------------------------
    _setting_tolerance = False

    def _apply_auto_tolerance(self):
        try:
            value = auto_tolerance(self._p_rgb, self._colors(), self._offset(True),
                                   self._p_src_alpha)
        except Exception:
            _log_exception("auto tolerance")
            return
        self._setting_tolerance = True
        try:
            self.tolerance_slider.setValue(value)
        finally:
            self._setting_tolerance = False
        self.auto_tolerance_button.setChecked(True)

    def _on_auto_tolerance_clicked(self, _checked=False):
        try:
            self._apply_auto_tolerance()
            self._schedule()
        except Exception:
            self._set_status(f"許容量を測れませんでした: {_log_exception('auto tolerance click')}", "error")

    def _on_tolerance_moved(self, _value):
        if not self._setting_tolerance:
            self.auto_tolerance_button.setChecked(False)

    def compute(self, preview: bool) -> np.ndarray:
        """いまの設定で RGBA を作る。preview なら縮小画像で、そうでなければ原寸で。"""
        rgb = self._p_rgb if preview else self._rgb
        src_alpha = self._p_src_alpha if preview else self._src_alpha
        scale = self._p_scale if preview else 1.0
        colors = self._colors()
        decontam = self.decontam_check.isChecked() and bool(colors)

        index = None
        if preview:
            self._protect_rejected = []
            self._seed_rejected = []
        if self._method() == METHOD_AI:
            base = self._ai_alpha_preview if preview else self._ai_alpha
            ai_seeds = self._seeds(shift_only=True)
            alpha, rejected, seed_rejected = refine_ai_alpha(
                base,
                self.ai_lo_slider.value() / 100.0,
                self.ai_hi_slider.value() / 100.0,
                protect=scale_points(self._protect_points(), scale),
                seeds=scale_points(ai_seeds, scale),
                report=True,
            )
            if preview:
                self._protect_rejected = rejected
                self._seed_rejected = [ai_seeds[i] for i in seed_rejected]
            offset = None
            if decontam:
                _dist, index, offset = self._distance(preview)
        else:
            dist, index, offset = self._distance(preview)
            alpha, rejected = compute_alpha(
                dist,
                self.tolerance_slider.value(),
                self.feather_slider.value(),
                self.range_combo.currentData(),
                seeds=scale_points(self._seeds(), scale),
                protect=scale_points(self._protect_points(), scale),
                report=True,
                keep_dist=self._keep_distance(preview),
            )
            if preview:
                self._protect_rejected = rejected
        background = None
        if decontam and offset is not None:
            background = local_background_rgb(colors, index, offset)
        return compose_rgba(
            rgb, alpha, colors, index,
            decontaminate_edges=decontam,
            trim=self.trim_check.isChecked(),
            src_alpha=src_alpha,
            background=background,
        )

    def _refresh_preview(self):
        try:
            rgba = self.compute(preview=True)
            self.result_view.set_image(array_to_qimage(rgba))
            opaque = rgba[..., 3]
            transparent = float((opaque == 0).mean()) * 100.0
            # 出力の大きさは原寸換算で出す(縮小プレビューの大きさを出すと、保存したものと
            # 食い違って見える)。切り詰め時は端数で ±1px ずれうるので「約」を付ける。
            if self.trim_check.isChecked() and self._p_scale < 1.0:
                out = f"約 {round(rgba.shape[1] / self._p_scale)}×{round(rgba.shape[0] / self._p_scale)}"
            elif self.trim_check.isChecked():
                out = f"{rgba.shape[1]}×{rgba.shape[0]}"
            else:
                out = f"{self._width}×{self._height}"
            self._summary = f"透明 {transparent:.0f}%  ·  出力 {out}px"
            warnings = ""
            if (opaque == 0).all():
                hint = ("「これより薄い部分は消す」を下げてください" if self._method() == METHOD_AI
                        else "許容量を下げてください")
                warnings += f"  ·  全部透明になっています（{hint}）"
            if self._selected_method() == METHOD_AI and self._ai_running:
                warnings += "  ·  AIで抜いている間は、色で抜いた結果を出しています"
            warnings += self._point_warnings()
            self._summary += warnings
            # 警告があるときは目立つ色にする(読み流されると、残したつもりの所が抜けたまま
            # 載ってしまう)。
            self._summary_level = "warning" if warnings else ""
            self._set_status(self._summary, self._summary_level)
        except Exception:
            self._summary = ""
            self._summary_level = "error"
            self._set_status(f"計算に失敗しました: {_log_exception('preview')}", "error")

    _summary = ""
    _summary_level = ""
    _seed_rejected = ()

    def _point_warnings(self) -> str:
        """効かなかった点の説明。点のリストは両モード共通なので、モードごとに理由を変える。"""
        text = ""
        points = self._protect_points()
        where = "、".join(f"({points[i][0]}, {points[i][1]})"
                         for i in self._protect_rejected if i < len(points))
        if self._method() == METHOD_AI:
            if where:
                text += (f"  ·  残す点 {where}: AIが背景と判定した部分か、背景まで含む広すぎる範囲の"
                         "ため残せません。「これより濃い部分は不透明にする」を下げてください")
            if self._seed_rejected:
                seeds = "、".join(f"({x}, {y})" for x, y in self._seed_rejected)
                text += (f"  ·  抜く点 {seeds}: 外側の背景とつながっているため効きません。"
                         "「これより薄い部分は消す」を上げてください")
        elif where:
            text += (f"  ·  残す点 {where}: この部分は外側の背景とつながっているため"
                     "残せません。許容量を下げるか、AIで抜いてください")
        return text

    # ------------------------------------------------------------------
    # 出力
    # ------------------------------------------------------------------
    def _final_image(self):
        """原寸で計算し直した QImage。失敗したら None(状態表示に理由を出す)。"""
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                return array_to_qimage(self.compute(preview=False))
            finally:
                try:
                    QApplication.restoreOverrideCursor()
                except Exception:
                    pass
        except Exception:
            self._set_status(f"原寸での計算に失敗しました: {_log_exception('final')}", "error")
            return None

    def _to_clipboard(self):
        try:
            image = self._final_image()
            if image is None:
                return
            set_clipboard_image(image)
            self._linger_ms = VISIBLE_MS + FADE_MS + 100
            show_toast(f"背景を透過\nクリップボードへ載せました（{image.width()}×{image.height()}）")
            self.close()
        except Exception:
            self._set_status(f"クリップボードへ載せられませんでした: {_log_exception('clipboard')}", "error")

    def _default_save_path(self):
        folder = _section(self._app_settings).get("last_dir") or ""
        if not folder or not os.path.isdir(folder):
            folder = str(Path.home() / "Pictures")
        stem = f"{self._name_hint}_透過" if self._name_hint else f"bg_removed_{datetime.now():%Y%m%d_%H%M%S}"
        return os.path.join(folder, stem + ".png")

    def _save(self):
        try:
            path, _filter = QFileDialog.getSaveFileName(
                self, "透過PNGを保存", self._default_save_path(), "PNG 画像 (*.png)")
            if not path:
                return
            if not path.lower().endswith(".png"):
                path += ".png"
            image = self._final_image()
            if image is None:
                return
            if not image.save(path, "PNG"):
                self._set_status(f"保存できませんでした: {path}", "error")
                return
            _save_values(self._app_settings, self._settings_path,
                         {"last_dir": os.path.dirname(path)})
            self._set_status(f"保存しました: {path}")
            show_toast(f"背景を透過\n保存しました\n{os.path.basename(path)}")
        except Exception:
            self._set_status(f"保存に失敗しました: {_log_exception('save')}", "error")

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        try:
            _save_values(self._app_settings, self._settings_path, {
                "window_size": [self.width(), self.height()],
                "edge_feather": self.feather_slider.value(),
                "follow_gradient": self.follow_check.isChecked(),
                "decontaminate": self.decontam_check.isChecked(),
                "trim": self.trim_check.isChecked(),
                "model": self.model_combo.currentData(),
                "preview_bg": self._preview_bg,
            # 以前のキー。tolerance は覚えるのをやめ、feather は edge_feather へ移した
            # (どちらも旧既定値が焼き込まれているだけなので、残すと読み違えのもとになる)。
            }, remove=("tolerance", "feather"))
        except Exception:
            _log_exception("close")
        # AI が走っている最中に閉じられたら、結果はもう受け取らない。器(_AiBridge)は
        # 窓の子ではないので残り続け、壊れた窓のスロットへ届くと例外になる。
        try:
            self._ai_bridge.finished.disconnect(self._on_ai_finished)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)
        self.finished.emit(self._linger_ms)


# PNG を Windows の登録形式 "PNG" として載せるための MIME 名。Qt の Windows 実装は
# この書き方の MIME を「その名前のクリップボード形式」へそのまま対応させる。
# "image/png" という MIME 名のままだと "image/png" という名前の形式で登録され、
# Office やブラウザが探す "PNG" にはならない。
WINDOWS_PNG_MIME = 'application/x-qt-windows-mime;value="PNG"'


def set_clipboard_image(image: QImage, clipboard=None) -> None:
    """アルファを保ったまま画像をクリップボードへ載せる。

    QClipboard.setImage() だけだと Windows では CF_DIB(とせいぜい CF_DIBV5)になり、
    貼り先の多くがアルファを無視して透明部分が黒や白に潰れる。透過PNGを理解する
    貼り先(Office・ブラウザ・多くの画像ソフト)は "PNG" 形式を優先して読むので、
    PNG のバイト列を複数の名前で載せ、DIB 系は Qt の画像変換に任せて併載する。"""
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    payload = QByteArray(buffer.data())
    buffer.close()

    mime = QMimeData()
    mime.setData(WINDOWS_PNG_MIME, payload)
    mime.setData("image/png", payload)
    mime.setImageData(image)
    (clipboard or QGuiApplication.clipboard()).setMimeData(mime)


# ===============================================================
# プロセスの入口
# ===============================================================
def _log_exception(where: str) -> str:
    """直前の例外を error.log に追記し、通知用の短い1行を返す(capture_process と同じ作法)。
    pythonw.exe で動くので標準エラーはどこにも出ない。ファイルに残すのが頼り。"""
    text = traceback.format_exc()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} bg_remove {where} =====\n{text}")
    except OSError:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass
    exc_type, exc_value = sys.exc_info()[:2]
    return f"{exc_type.__name__}: {exc_value}" if exc_type else "unknown error"


def _install_excepthook() -> None:
    """どこにも捕まらなかった例外を error.log に残す(落ちた理由を後から読むための保険)。"""
    original = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(
                    f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} bg_remove uncaught =====\n"
                    + "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
                )
        except OSError:
            pass
        original(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _set_app_user_model_id() -> None:
    try:
        func = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        func.argtypes = [wintypes.LPCWSTR]
        func.restype = ctypes.c_long  # HRESULT
        func(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="画像の背景を透過させる窓(tray-tools)")
    parser.add_argument("source", nargs="?", default=CLIPBOARD_SOURCE,
                        help=f"画像のパス。{CLIPBOARD_SOURCE} ならクリップボードから読む")
    parser.add_argument("--delete-after-read", action="store_true",
                        help="読み終えたら source を消す(付箋からの受け渡し用の一時PNG)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    # argparse は "--clipboard" を未知のオプションとして弾くので、位置引数として先に拾う。
    raw = list(sys.argv[1:] if argv is None else argv)
    source = CLIPBOARD_SOURCE
    if CLIPBOARD_SOURCE in raw:
        raw.remove(CLIPBOARD_SOURCE)
    args = _parse_args(raw)
    if args.source and args.source != CLIPBOARD_SOURCE:
        source = args.source

    _install_excepthook()
    _set_app_user_model_id()

    app = QApplication([sys.argv[0]])
    # 終わり方は自分で決める。閉じ際に出したトースト(Qt.Tool の窓)を見せ切るまで
    # 待ちたいが、既定のままだと本体の窓が閉じた瞬間にプロセスが終わって消える。
    app.setQuitOnLastWindowClosed(False)
    # このプロセスの部品すべてを、OS の配色ではなくこの窓の配色で描く(THEME の説明を参照)。
    apply_theme(app)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    def quit_later(ms):
        QTimer.singleShot(max(0, int(ms)), app.quit)

    try:
        image, name_hint = load_source(source)
    except Exception:
        image, name_hint = QImage(), None
        _log_exception(f"load {source}")
    if args.delete_after_read and source != CLIPBOARD_SOURCE:
        try:
            os.remove(source)
        except OSError:
            pass

    if image.isNull():
        where = "クリップボード" if source == CLIPBOARD_SOURCE else Path(source).name
        show_toast(f"背景を透過\n{where}に画像がありません")
        quit_later(VISIBLE_MS + FADE_MS + 100)
        return app.exec()

    try:
        app_settings = settings_module.load_settings()
        window = BgRemoveWindow(qimage_to_array(image), name_hint, app_settings,
                                str(settings_module.SETTINGS_PATH))
    except Exception:
        summary = _log_exception("open")
        show_toast(f"背景を透過\n開けませんでした\n{summary}")
        quit_later(VISIBLE_MS + FADE_MS + 100)
        return app.exec()

    window.setAttribute(Qt.WA_DeleteOnClose, True)
    window.finished.connect(quit_later)
    window.show()
    window.raise_()
    window.activateWindow()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
