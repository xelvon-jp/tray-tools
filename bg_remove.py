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
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSlider,
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
FEATHER_MAX = 60      # 境界のぼかし(ΔE)の上限

RANGE_CONNECTED = "connected"   # 外周(とスポイトの点)からつながった部分だけ
RANGE_GLOBAL = "global"         # 画像全体の同色

# 左ペインのクリックの種類
PICK_REPLACE = "replace"   # クリック: 背景色を置き換える
PICK_ADD = "add"           # Shift+クリック: 背景色を足し、ここも抜く
PICK_PROTECT = "protect"   # Ctrl+クリック: ここは残す(色は足さない)

METHOD_COLOR = "color"
METHOD_AI = "ai"

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
MIN_WIDTH = 820
MIN_HEIGHT = 520

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


def color_distance(rgb, colors):
    """各画素から、いちばん近い背景色までの ΔE と、その色の番号を返す。

    戻り値は (dist: float32 HxW, index: int32 HxW)。colors が空なら dist は全部 inf
    (=どこも背景ではない)。index は色かぶり除去で「どの背景色が混ざったか」に使う。"""
    rgb = np.asarray(rgb)
    h, w = rgb.shape[:2]
    index = np.zeros((h, w), dtype=np.int32)
    if not colors:
        return np.full((h, w), np.inf, dtype=np.float32), index
    lab = to_lab(rgb)
    targets = to_lab(np.asarray(colors, dtype=np.uint8).reshape(-1, 3))
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


def scale_points(points, scale: float):
    """原寸の画像座標 (x, y) の並びを、縮小プレビューの座標へ換算する。

    画素 (x, y) は [x, x+1) の範囲を占めるので、中心 (x+0.5) を縮めてから切り捨てる。
    左上の角で換算すると、縮小で1画素ぶん左上へずれ、細い隙間に打った点が隣の
    (被写体側の)画素へ落ちることがある。"""
    if scale >= 1.0:
        return [(int(x), int(y)) for x, y in points or ()]
    return [(int((x + 0.5) * scale), int((y + 0.5) * scale)) for x, y in points or ()]


def compute_alpha(dist, tolerance: float, feather: float, mode: str = RANGE_CONNECTED,
                  seeds=(), protect=(), report: bool = False):
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

    report が True なら (alpha, 効かなかった保護点の番号のリスト) を返す。番号は
    protect の並びでの位置。外周とつながっていて効かなかったものだけが入る。"""
    dist = np.asarray(dist, dtype=np.float32)
    tol = float(tolerance)
    fea = max(float(feather), 0.0)
    if fea > 0:
        background = np.clip((tol + fea - dist) / fea, 0.0, 1.0)
    else:
        background = (dist <= tol).astype(np.float32)

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


def decontaminate(rgb, alpha, colors, index=None) -> np.ndarray:
    """半透明の画素から背景色の混入を取り除いた RGB(uint8)を返す。

    境界の画素は「前景 C と背景 B が a : 1−a で混ざった色」なので、C = (観測 − (1−a)·B) / a
    で前景を取り出す。これをしないと、白背景から抜いた髪の毛の縁が白く光り、暗い背景に
    置いたときに輪郭が浮く。背景色が複数あるときは、その画素にいちばん近い色を B にする
    (index は color_distance の戻り値)。不透明・完全透明の画素はそのまま。"""
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
    if index is None or len(palette) == 1:
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
                 trim: bool = False, src_alpha=None) -> np.ndarray:
    """RGB と不透明度から、出力する RGBA(uint8)を組み立てる。

    src_alpha は元画像がもともと持っていたアルファ(0〜255)。元から透明な部分は
    透明のまま残したいので掛け合わせる。色かぶり除去は「背景を抜いて生まれた半透明」
    だけが対象なので、掛け合わせる前の alpha で行う。"""
    alpha = np.asarray(alpha, dtype=np.float32)
    color = decontaminate(rgb, alpha, list(colors), index) if decontaminate_edges else np.asarray(rgb)
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


def _save_values(app_settings, settings_path, updates: dict) -> None:
    """bg_remove セクションの該当キーだけを書き換える。

    clipboard_preview._save_size と同じ作法で、ファイルを読み直して差し替える
    (メモリ上の設定を丸ごと書くと、既定値まで焼き込まれて settings.json の姿が変わる。
    本体が同時に別のキーを書いていても、読み直してから書けば消し合わない)。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = app_settings[SETTINGS_SECTION] = {}
        section.update(updates)
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
STYLE = (
    "#bgWindow { background-color: #141414; color: #ffffff; }"
    "#bgWindow QLabel, #bgWindow QCheckBox, #bgWindow QRadioButton { color: #d4d4d4; }"
    "#bgNote { color: #8a8a8a; }"
    "#bgHeading { color: #ffffff; font-weight: bold; }"
    "#bgWindow QPushButton, #bgWindow QToolButton { background-color: #262626; color: #ffffff;"
    " border: 1px solid #3c3c3c; border-radius: 4px; padding: 5px 10px; }"
    "#bgWindow QPushButton:hover { background-color: #303030; }"
    "#bgWindow QPushButton:checked { background-color: #2563eb; border-color: #2563eb; }"
    "#bgWindow QPushButton:disabled { color: #6a6a6a; border-color: #2a2a2a; }"
    "#bgWindow QComboBox { background-color: #262626; color: #ffffff;"
    " border: 1px solid #3c3c3c; border-radius: 4px; padding: 3px 8px; }"
    "#bgWindow QComboBox:disabled { color: #6a6a6a; }"
    "#bgApply { background-color: #2563eb; border-color: #2563eb; }"
    "#bgApply:hover { background-color: #3b7dff; }"
    "#bgWindow QLabel:disabled { color: #5a5a5a; }"
    # 「#bgWindow QToolButton」より強くするため親の名前から書く(id 1つだけの指定だと
    # 詳細度で負けて、×が普通のボタンの枠を被ってしまう)。
    "#bgWindow #bgSwatchRemove { padding: 0px 3px; border: none; background: transparent;"
    " color: #a0a0a0; }"
    "#bgWindow #bgSwatchRemove:hover { color: #ff6b6b; }"
)


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
        painter.fillRect(self.rect(), QColor("#1c1c1c"))
        rect = self._target_rect()
        if rect.isEmpty():
            painter.end()
            return
        if self._background == "checker":
            painter.fillRect(rect, self._checker)
        else:
            painter.fillRect(rect, QColor({"white": "#ffffff", "black": "#000000",
                                           "green": "#00b140"}.get(self._background, "#1c1c1c")))
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
        self.setStyleSheet(STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(self._build_color_row())
        layout.addLayout(self._build_method_row(section))
        layout.addLayout(self._build_range_row(section))
        layout.addLayout(self._build_inside_row())
        layout.addLayout(self._build_option_row(section))
        layout.addWidget(self._build_panes(), 1)

        self.status = QLabel("")
        self.status.setObjectName("bgNote")
        self.status.setFont(QFont("Meiryo", 8))
        # ai_status と同じく、長い文言で窓の最小幅を押し広げさせない。
        self.status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.status)
        layout.addLayout(self._build_button_row())

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
        self._refresh_preview()

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------
    def _font(self, size=9):
        return QFont("Meiryo", size)

    def _heading(self, text):
        label = QLabel(text)
        label.setObjectName("bgHeading")
        label.setFont(self._font())
        return label

    def _build_color_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._heading("背景色"))
        self._swatch_row = QHBoxLayout()
        self._swatch_row.setSpacing(4)
        row.addLayout(self._swatch_row)
        row.addSpacing(10)
        # 「残す点」の一覧。背景色とは別に並べる(残す点は色を足さないので、色の
        # スウォッチに混ぜると「この色が背景色に入った」と読み違えられる)。
        self._protect_heading = self._heading("残す点")
        self._protect_heading.setToolTip(
            "Ctrl+クリックで指した部分は、背景色に近くても透明にしない（「色で抜く」専用）")
        row.addWidget(self._protect_heading)
        self._protect_row = QHBoxLayout()
        self._protect_row.setSpacing(4)
        row.addLayout(self._protect_row)
        row.addStretch(1)
        reset = QPushButton("自動推定に戻す")
        reset.setFont(self._font())
        reset.setToolTip("画像の外周でいちばん多い色を背景色にし直す")
        reset.clicked.connect(lambda: self._reset_to_estimate())
        row.addWidget(reset)
        return row

    def _build_method_row(self, section):
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._heading("抜き方"))
        self._method_group = QButtonGroup(self)
        self.color_radio = QRadioButton("色で抜く")
        self.ai_radio = QRadioButton("AIの結果")
        for radio in (self.color_radio, self.ai_radio):
            radio.setFont(self._font())
            self._method_group.addButton(radio)
            row.addWidget(radio)
        self.color_radio.setChecked(True)
        self.ai_radio.setEnabled(False)
        self.ai_radio.setToolTip("先に［AIで抜く］を押してください")
        self.color_radio.toggled.connect(self._on_method_changed)

        row.addSpacing(16)
        self.ai_button = QPushButton("AIで抜く")
        self.ai_button.setFont(self._font())
        self.ai_button.clicked.connect(self._start_ai)
        row.addWidget(self.ai_button)
        self.model_combo = QComboBox()
        self.model_combo.setFont(self._font())
        for key, label in AI_MODELS:
            self.model_combo.addItem(label, key)
        saved_model = section.get("model")
        for i in range(self.model_combo.count()):
            if self.model_combo.itemData(i) == saved_model:
                self.model_combo.setCurrentIndex(i)
        row.addWidget(self.model_combo)
        if not ai_available():
            for widget in (self.ai_button, self.model_combo):
                widget.setEnabled(False)
                widget.setToolTip(AI_INSTALL_HINT)
        else:
            self.ai_button.setToolTip(
                "被写体をAIで切り抜く（初回はライブラリの読み込みに数十秒かかります）")
        self.ai_status = QLabel("")
        self.ai_status.setObjectName("bgNote")
        self.ai_status.setFont(self._font(8))
        # 長い文言(例外の要約など)で窓の最小幅が押し広げられないよう、幅は主張させない。
        self.ai_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.ai_status, 1)
        return row

    def _make_slider(self, row, title, maximum, value, tooltip):
        label = QLabel(title)
        label.setFont(self._font())
        label.setToolTip(tooltip)
        row.addWidget(label)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, maximum)
        slider.setValue(value)
        slider.setMinimumWidth(140)
        slider.setToolTip(tooltip)
        row.addWidget(slider, 1)
        value_label = QLabel(str(value))
        value_label.setFont(self._font())
        value_label.setMinimumWidth(28)
        row.addWidget(value_label)
        slider.valueChanged.connect(lambda v: (value_label.setText(str(v)), self._schedule()))
        return slider, label

    def _build_range_row(self, section):
        row = QHBoxLayout()
        row.setSpacing(6)
        self.tolerance_slider, self._tolerance_label = self._make_slider(
            row, "許容量", TOLERANCE_MAX,
            _int_setting(section, "tolerance", 12, 0, TOLERANCE_MAX),
            "背景色からこの差(ΔE)までを完全に透明にする。2前後が見分けられる限界の差",
        )
        row.addSpacing(12)
        self.feather_slider, self._feather_label = self._make_slider(
            row, "境界のぼかし", FEATHER_MAX,
            _int_setting(section, "feather", 10, 0, FEATHER_MAX),
            "許容量からさらにこの差までを、離れるほど不透明になる半透明にする",
        )
        return row

    def _build_inside_row(self):
        """画像の内側にある背景色に近い色を、残すか抜くか(一括の切り替え)。

        以前は「範囲: 外周からつながった部分だけ／画像全体の同色」と書いていたが、
        仕組みの名前で、何のための設定かが読み取れなかった。困りごとの側(被写体の中に
        背景と似た色がある)から名付け直してある。1か所ずつの指定は左ペインの
        Shift+クリック(ここも抜く)/ Ctrl+クリック(ここは残す)で行う。"""
        row = QHBoxLayout()
        row.setSpacing(6)
        tooltip = (
            "残す: 外周からつながった背景だけを抜き、被写体の内側にある白目やハイライトなどを守る\n"
            "抜く: 文字の穴や腕と胴の隙間など、外周に接していない部分の同じ色も抜く\n"
            "1か所ずつ決めるなら、左の画像で Shift+クリック（ここも抜く）／Ctrl+クリック（ここは残す）")
        self._range_label = QLabel("画像の内側にある背景色に近い色")
        self._range_label.setFont(self._font())
        self._range_label.setToolTip(tooltip)
        row.addWidget(self._range_label)
        self.range_combo = QComboBox()
        self.range_combo.setFont(self._font())
        self.range_combo.addItem("残す（外周からつながった背景だけ抜く）", RANGE_CONNECTED)
        self.range_combo.addItem("抜く（画像全体の同色を抜く）", RANGE_GLOBAL)
        # この設定は覚えない。「抜く」で閉じた次の画像で被写体の白目が抜けているのに
        # 気付かず載せる、という事故のほうが、毎回選び直す手間より重い。
        self.range_combo.setToolTip(tooltip)
        self.range_combo.currentIndexChanged.connect(lambda _i: self._schedule())
        row.addWidget(self.range_combo)
        row.addStretch(1)
        return row

    def _build_option_row(self, section):
        row = QHBoxLayout()
        row.setSpacing(12)
        self.decontam_check = QCheckBox("色かぶり除去")
        self.decontam_check.setFont(self._font())
        self.decontam_check.setChecked(bool(section.get("decontaminate", True)))
        self.decontam_check.setToolTip("半透明の縁から背景色の混ざりを取り除く（白背景の縁が光るのを防ぐ）")
        self.decontam_check.toggled.connect(lambda _c: self._schedule())
        row.addWidget(self.decontam_check)
        self.trim_check = QCheckBox("余白を切り詰める")
        self.trim_check.setFont(self._font())
        self.trim_check.setChecked(bool(section.get("trim", False)))
        self.trim_check.setToolTip("透明になった余白を落として、残った部分の外接矩形にする")
        self.trim_check.toggled.connect(lambda _c: self._schedule())
        row.addWidget(self.trim_check)
        row.addStretch(1)

        row.addWidget(self._heading("右の背景"))
        self._bg_group = QButtonGroup(self)
        self._bg_group.setExclusive(True)
        saved_bg = section.get("preview_bg", "checker")
        if saved_bg not in {key for key, _ in PREVIEW_BACKGROUNDS}:
            saved_bg = "checker"
        self._preview_bg = saved_bg
        for key, label in PREVIEW_BACKGROUNDS:
            button = QPushButton(label)
            button.setFont(self._font())
            button.setCheckable(True)
            button.setChecked(key == saved_bg)
            button.clicked.connect(lambda _c, k=key: self._set_preview_bg(k))
            self._bg_group.addButton(button)
            row.addWidget(button)
        return row

    def _build_panes(self):
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        self.source_view = ImageView(pickable=True)
        self.source_view.clicked.connect(self._on_pick)
        self.source_view.hovered.connect(self._on_hover)
        self.result_view = ImageView(pickable=False)
        self.result_view.set_background(self._preview_bg)
        splitter.addWidget(self._wrap_pane(
            "元の画像　　クリック: 背景色　Shift+クリック: ここも抜く　Ctrl+クリック: ここは残す",
            self.source_view))
        splitter.addWidget(self._wrap_pane("結果", self.result_view))
        splitter.setSizes([1, 1])
        return splitter

    def _wrap_pane(self, title, view):
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        heading = self._heading(title)
        # 左の見出しは操作の案内を兼ねていて長い。折り返さないと、その文字幅が左ペインの
        # 最小幅になって分割の境目が右へ押しやられ、結果のペインが細くなる。
        heading.setWordWrap(True)
        column.addWidget(heading)
        column.addWidget(view, 1)
        return box

    def _build_button_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)
        note = QLabel("Enter クリップボードへ  ·  Ctrl+S 保存  ·  Esc キャンセル")
        note.setObjectName("bgNote")
        note.setFont(self._font(8))
        row.addWidget(note)
        row.addStretch(1)
        cancel = QPushButton("キャンセル")
        cancel.setFont(self._font())
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)
        save = QPushButton("保存…")
        save.setFont(self._font())
        save.clicked.connect(self._save)
        row.addWidget(save)
        self.apply_button = QPushButton("クリップボードへ")
        self.apply_button.setObjectName("bgApply")
        self.apply_button.setFont(self._font())
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self._to_clipboard)
        row.addWidget(self.apply_button)
        return row

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

    def _seeds(self):
        return [entry["seed"] for entry in self._entries if entry["seed"] is not None]

    def _reset_to_estimate(self, schedule=True):
        color = estimate_background(self._rgb, self._src_alpha)
        self._entries = [{"color": color, "seed": None}]
        if schedule:
            self._colors_changed()

    def _on_pick(self, x, y, kind):
        try:
            if kind == PICK_PROTECT:
                if (x, y) not in self._protect:
                    self._protect.append((x, y))
                self._colors_changed()
                return
            color = tuple(int(c) for c in self._rgb[y, x])
            entry = {"color": color, "seed": (x, y)}
            if kind == PICK_ADD:
                # 同じ色を2つ持っても距離は変わらない。点だけ足したいので色は重ねてよい。
                self._entries.append(entry)
            else:
                self._entries = [entry]
            self._colors_changed()
        except Exception:
            self.status.setText(f"スポイトに失敗しました: {_log_exception('pick')}")

    def _remove_entry(self, index):
        try:
            if 0 <= index < len(self._entries):
                del self._entries[index]
            self._colors_changed()
        except Exception:
            self.status.setText(f"背景色を消せませんでした: {_log_exception('remove color')}")

    def _remove_protect(self, index):
        try:
            if 0 <= index < len(self._protect):
                del self._protect[index]
            self._colors_changed()
        except Exception:
            self.status.setText(f"残す点を消せませんでした: {_log_exception('remove protect')}")

    def _colors_changed(self):
        self._rebuild_swatches()
        self._schedule()

    def _rebuild_swatches(self):
        while self._swatch_row.count():
            item = self._swatch_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        if not self._entries:
            empty = QLabel("（なし）")
            empty.setObjectName("bgNote")
            empty.setFont(self._font(8))
            self._swatch_row.addWidget(empty)
        for index, entry in enumerate(self._entries):
            box = QWidget()
            inner = QHBoxLayout(box)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(0)
            chip = QLabel()
            chip.setFixedSize(22, 22)
            chip.setStyleSheet(
                f"background-color: {_hex(entry['color'])}; border: 1px solid #808080;"
                " border-radius: 3px;")
            where = "自動推定" if entry["seed"] is None else f"({entry['seed'][0]}, {entry['seed'][1]}) から"
            chip.setToolTip(f"{_hex(entry['color'])}  {where}")
            inner.addWidget(chip)
            remove = QToolButton()
            remove.setObjectName("bgSwatchRemove")
            remove.setText("×")
            remove.setToolTip("この背景色を外す")
            remove.clicked.connect(lambda _c=False, i=index: self._remove_entry(i))
            inner.addWidget(remove)
            self._swatch_row.addWidget(box)
        self._rebuild_protect_list()
        self.source_view.set_markers(self._seeds(), self._protect)

    def _rebuild_protect_list(self):
        while self._protect_row.count():
            item = self._protect_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        # 1つも無いときは見出しごと隠す(使わない人には行を短く見せたい)。
        self._protect_heading.setVisible(bool(self._protect))
        for index, (x, y) in enumerate(self._protect):
            box = QWidget()
            inner = QHBoxLayout(box)
            inner.setContentsMargins(0, 0, 0, 0)
            inner.setSpacing(0)
            chip = QLabel(f"✓ {x},{y}")
            chip.setFont(self._font(8))
            chip.setStyleSheet(
                "background-color: #16a34a; color: #ffffff; border-radius: 3px; padding: 2px 5px;")
            chip.setToolTip(f"({x}, {y}) を含む部分は透明にしない")
            inner.addWidget(chip)
            remove = QToolButton()
            remove.setObjectName("bgSwatchRemove")
            remove.setText("×")
            remove.setToolTip("この残す点を外す")
            remove.clicked.connect(lambda _c=False, i=index: self._remove_protect(i))
            inner.addWidget(remove)
            self._protect_row.addWidget(box)

    def _on_hover(self, x, y):
        try:
            if x < 0:
                self.status.setText(self._summary)
                return
            color = tuple(int(c) for c in self._rgb[y, x])
            text = f"({x}, {y})  {_hex(color)}"
            if self._entries:
                dist, _ = color_distance(np.array([[color]], dtype=np.uint8), self._colors())
                text += f"  背景色との差 ΔE {float(dist[0, 0]):.1f}"
            self.status.setText(text)
        except Exception:
            pass  # 状態表示が出ないだけ。ホバーごとに例外を積むよりは黙る

    # ------------------------------------------------------------------
    # 抜き方
    # ------------------------------------------------------------------
    def _method(self):
        if self.ai_radio.isChecked() and self._ai_alpha is not None:
            return METHOD_AI
        return METHOD_COLOR

    def _on_method_changed(self, _checked=None):
        self._update_method_controls()
        self._schedule()

    def _update_method_controls(self):
        # AI の結果を使っている間は、色の距離に関わるつまみは効かない。効かないつまみを
        # 触れるままにすると「動かしても変わらない」で壊れたように見えるので止める。
        color_mode = self._method() == METHOD_COLOR
        for widget in (self.tolerance_slider, self.feather_slider, self.range_combo,
                       self._tolerance_label, self._feather_label, self._range_label):
            widget.setEnabled(color_mode)

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
                return
            self._ai_alpha = result["alpha"]
            self._ai_alpha_preview, _ = resize_long_side(self._ai_alpha, PREVIEW_LONG_SIDE)
            self.ai_status.setText(f"{result['model']}  {result['seconds']:.1f}秒")
            self.ai_radio.setEnabled(True)
            self.ai_radio.setToolTip("")
            self.ai_radio.setChecked(True)  # toggled 経由で描き直しが走る
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

    def _distance(self, preview: bool):
        key = (preview, tuple(self._colors()))
        cached = self._dist_cache.get(key)
        if cached is None:
            # 原寸のぶんは確定のときにしか使わないので、プレビュー用と並べて1つずつだけ持つ
            # (背景色を変えるたびに積むと、大きな画像でメモリを食い潰す)。
            self._dist_cache = {k: v for k, v in self._dist_cache.items() if k[0] != preview}
            cached = color_distance(self._p_rgb if preview else self._rgb, self._colors())
            self._dist_cache[key] = cached
        return cached

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
        if self._method() == METHOD_AI:
            alpha = self._ai_alpha_preview if preview else self._ai_alpha
            if decontam:
                _dist, index = self._distance(preview)
        else:
            dist, index = self._distance(preview)
            alpha, rejected = compute_alpha(
                dist,
                self.tolerance_slider.value(),
                self.feather_slider.value(),
                self.range_combo.currentData(),
                seeds=scale_points(self._seeds(), scale),
                protect=scale_points(self._protect, scale),
                report=True,
            )
            if preview:
                self._protect_rejected = rejected
        return compose_rgba(
            rgb, alpha, colors, index,
            decontaminate_edges=decontam,
            trim=self.trim_check.isChecked(),
            src_alpha=src_alpha,
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
            if (opaque == 0).all():
                self._summary += "  ·  全部透明になっています（許容量を下げてください）"
            if self._protect and self._method() == METHOD_AI:
                self._summary += "  ·  残す点は「色で抜く」専用です（AIの結果には効きません）"
            elif self._protect_rejected:
                where = "、".join(f"({self._protect[i][0]}, {self._protect[i][1]})"
                                 for i in self._protect_rejected if i < len(self._protect))
                self._summary += (f"  ·  残す点 {where}: この部分は外側の背景とつながっているため"
                                  "残せません。許容量を下げるか、AIで抜いてください")
            self.status.setText(self._summary)
            # 窓が狭いと状態欄の末尾(残す点の警告など)が切れるので、全文をツールチップにも置く。
            self.status.setToolTip(self._summary)
        except Exception:
            self._summary = ""
            self.status.setText(f"計算に失敗しました: {_log_exception('preview')}")

    _summary = ""

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
            self.status.setText(f"原寸での計算に失敗しました: {_log_exception('final')}")
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
            self.status.setText(f"クリップボードへ載せられませんでした: {_log_exception('clipboard')}")

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
                self.status.setText(f"保存できませんでした: {path}")
                return
            _save_values(self._app_settings, self._settings_path,
                         {"last_dir": os.path.dirname(path)})
            self.status.setText(f"保存しました: {path}")
            show_toast(f"背景を透過\n保存しました\n{os.path.basename(path)}")
        except Exception:
            self.status.setText(f"保存に失敗しました: {_log_exception('save')}")

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        try:
            _save_values(self._app_settings, self._settings_path, {
                "window_size": [self.width(), self.height()],
                "tolerance": self.tolerance_slider.value(),
                "feather": self.feather_slider.value(),
                "decontaminate": self.decontam_check.isChecked(),
                "trim": self.trim_check.isChecked(),
                "model": self.model_combo.currentData(),
                "preview_bg": self._preview_bg,
            })
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
