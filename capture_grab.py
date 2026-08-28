# capture_grab.py
# 画面取得・注釈込みレンダリング・保存を担当する。
# mssで取得したバイト列から直接QImageを作るため、PIL経由の変換は行わない
# (Pillowはトレイアイコン描画専用に残す。保存もQImage.save()に統一する)。
import datetime
import sys
from pathlib import Path

import mss
from PySide6.QtCore import Qt, QPoint, QRect
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPainterPath, QPen


# mss のインスタンスは使い回す。生成のたびに ctypes で GDI の関数を引き直すため
# 安くはなく、それ以上に「生成中にGCが走ると落ちる」問題がある。
#
# comtypes のCOMオブジェクトは CoInitialize(STA)で作られており、作ったスレッド以外から
# 解放するとプロセスごと落ちる。PythonのGCは任意のスレッドで走るので、mss の初期化中に
# たまたま動くとそこで死ぬ。画面ミラーが30fpsで呼ぶようになって発火が現実的になり、
# crash.log に mss/base.py __init__ → comtypes __del__ → Release という同じスタックが
# 何度も残った(2026-08-28に8回)。作らなければ、その窓は開かない。
#
# mss はスレッドセーフではない。ここを呼ぶのはQtのメインスレッドだけという前提で
# 使い回している(キャプチャもミラーもメインスレッドのタイマーから走る)。
_MSS = None


def _sct():
    """使い回す mss のインスタンス。無ければ作る。"""
    global _MSS
    if _MSS is None:
        _MSS = mss.MSS()
    return _MSS


def release_sct() -> None:
    """使い回している mss を手放す。画面構成が変わったときに呼ぶ。

    mss は生成時にモニタの一覧を読むので、モニタを抜き差ししたあとも古いまま持って
    いると、そのぶんズレたところを撮る。次に必要になった時点で作り直させる。"""
    global _MSS
    sct, _MSS = _MSS, None
    if sct is not None:
        try:
            sct.close()
        except Exception:
            pass


# mss の Windows バックエンド(mss.windows.gdi)。CAPTUREBLT を一時的に外すために掴む。
# import 自体は安いが、毎フレーム走る経路なので1回だけ解決して持っておく
# (_sct を使い回すのと同じ理由)。取れなければ None のままで、その場合は素のまま撮る。
_GDI = None
_GDI_RESOLVED = False


def _gdi_module():
    global _GDI, _GDI_RESOLVED
    if not _GDI_RESOLVED:
        _GDI_RESOLVED = True
        try:
            import mss.windows.gdi as gdi

            # 期待する名前が無いバージョンなら触らない(下の差し替えが意味を成さない)。
            _GDI = gdi if hasattr(gdi, "CAPTUREBLT") else None
        except ImportError:
            _GDI = None
    return _GDI


def _grab(region: dict, include_layered: bool):
    """mss で1枚撮る。include_layered=False のときだけ CAPTUREBLT を外す。

    mss は BitBlt を SRCCOPY | CAPTUREBLT で呼ぶ。CAPTUREBLT は「自分より上に重なって
    いるレイヤードウィンドウも結果に含める」フラグで、そのために Windows は撮る直前に
    画面からマウスカーソルをいったん取り除く。1枚撮るだけなら誰も気付かないが、画面
    ミラーのように毎秒30回撮り続けると、その取り除きが目に見えてカーソルのちらつきに
    なる(gdigrab 等の画面録画でも同じ症状が知られている)。

    ミラーはそもそもレイヤードウィンドウを撮りたくない(自前の枠やツールバーが写ると
    入れ子になる)ので、外して困るものが無い。このPCで実測しても、可視のレイヤード
    ウィンドウ6枚の矩形を CAPTUREBLT あり/なしで撮り比べて1バイトも違わなかった
    (DWM合成の画面からBitBltする以上、フラグの有無で中身が変わらない)。

    差し替えはモジュール変数の一時変更で行う。mss 側は呼び出しのたびにこの名前を
    グローバルとして引くので、これで効く。Qtのメインスレッドからしか呼ばない前提
    (_sct と同じ)なので、戻し忘れが無ければ他へ漏れない。"""
    sct = _sct()
    gdi = None if include_layered else _gdi_module()
    if gdi is None:
        return sct.grab(region)
    saved = gdi.CAPTUREBLT
    gdi.CAPTUREBLT = 0
    try:
        return sct.grab(region)
    finally:
        gdi.CAPTUREBLT = saved


def grab_region(rect_global: QRect, include_layered: bool = True) -> QImage:
    """rect_global: Qt論理座標系(スクリーン全体)でのQRect。
    選択範囲の中心にある画面の devicePixelRatio を使って物理ピクセル座標に変換し、mssでキャプチャする。
    (マルチモニタでスケーリング率が異なる場合のズレを抑えるため、primaryScreen()固定ではなく
    screenAt()でそのつど対象画面を判定する。screenAtがNoneを返す場合はprimaryScreenにフォールバックする)

    include_layered=False は「毎フレーム撮り続ける」呼び出し(画面ミラー)用。理由は _grab を参照。
    """
    screen = QGuiApplication.screenAt(rect_global.center()) or QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() or 1.0

    region = {
        "left": int(rect_global.x() * dpr),
        "top": int(rect_global.y() * dpr),
        "width": int(rect_global.width() * dpr),
        "height": int(rect_global.height() * dpr),
    }

    shot = _grab(region, include_layered)

    qimage = QImage(shot.bgra, shot.width, shot.height, shot.width * 4, QImage.Format_ARGB32)
    qimage = qimage.copy()  # shot.bgraのバッファは次のgrabで上書きされるためコピーする
    qimage.setDevicePixelRatio(dpr)
    return qimage


def virtual_geometry() -> QRect:
    """全モニタを合算したQRect(Qt論理座標)。全画面オーバーレイや全画面キャプチャの基準にする。"""
    geometry = QRect()
    for screen in QGuiApplication.screens():
        geometry = geometry.united(screen.geometry())
    return geometry


def dpr_at_device_point(x: int, y: int) -> float:
    """物理ピクセルのその位置にある画面の devicePixelRatio。

    Win32が返すのは物理ピクセル、Qtが使うのは論理座標。どの画面の比率で割るかは、
    grab_region が「選択範囲の中心にある画面で判定する」のと同じ考え方で決める。
    ただし screenAt() は論理座標を要求するので、まずプライマリの比率で仮に割って
    当たりを付ける。全画面が同じ倍率(通常の環境)ならこれで正しい画面に行き着き、
    倍率が混在していても grab_region と同じ精度に収まる。"""
    primary = QGuiApplication.primaryScreen()
    base = (primary.devicePixelRatio() if primary else 1.0) or 1.0
    screen = QGuiApplication.screenAt(QPoint(int(x / base), int(y / base))) or primary
    return (screen.devicePixelRatio() if screen else 1.0) or 1.0


def device_bounds_to_logical(bounds) -> QRect:
    """Win32の (left, top, right, bottom)(物理ピクセル)をQtの論理座標のQRectへ直す。

    Win32のRECTは right/bottom が排他なので、幅は right - left。
    QRect(QPoint(left, top), QPoint(right, bottom)) で作ると縦横が1pxずつ大きくなる。"""
    left, top, right, bottom = bounds
    dpr = dpr_at_device_point((left + right) // 2, (top + bottom) // 2)
    return QRect(
        int(left / dpr),
        int(top / dpr),
        int((right - left) / dpr),
        int((bottom - top) / dpr),
    )


def draw_stroke(painter: QPainter, stroke: dict) -> None:
    """半透明色の蛍光ペンが自分自身との重なりで濃く見えないよう、線分ごとのdrawLineではなく
    1本のQPainterPathにまとめてstrokePathする(重ね塗りを避けつつ描画負荷も下げる)。"""
    points = stroke["points"]
    if len(points) < 2:
        return

    pen = QPen(stroke["color"])
    pen.setWidthF(stroke["width"])
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)

    path = QPainterPath()
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)

    painter.strokePath(path, pen)


def draw_action(painter: QPainter, action: dict) -> None:
    """actions_history の1件(ストローク or テキスト)を描画する。
    付箋ウインドウのライブ描画と、保存/コピー時の焼き込みレンダリングの両方から使う共通処理。"""
    if action["type"] == "stroke":
        draw_stroke(painter, action)
    elif action["type"] == "text":
        painter.setPen(action["color"])
        painter.setFont(QFont("Meiryo", 14))
        painter.drawText(action["pos"], action["text"])


def render_annotated(base_image: QImage, actions_history: list) -> QImage:
    """元画像 + actions_history(線・テキスト)を1枚に焼き込んだQImageを返す。
    ズーム倍率は無視し、元画像の解像度で描く。座標系はactions_history側がすでに
    「pixmap論理サイズ(devicePixelRatio勘案後)」基準なので、base_imageにdevicePixelRatioが
    設定済みであればQPainterが自動でそれに合わせてスケーリングする(手動のscale補正は不要)。

    QImage(base_image)は暗黙的共有によりバッファをbase_imageと共有するだけの浅いコピーで、
    デタッチされるかはQPainter側の実装依存になってしまう。base_image.copy()で明示的に
    ディープコピーし、元画像に注釈が焼き付いてアンドゥや繰り返し保存で劣化するのを防ぐ。"""
    result = base_image.copy()
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing)

    for action in actions_history:
        draw_action(painter, action)

    painter.end()
    return result


def _unique_path(save_folder: Path, stem: str, suffix: str) -> Path:
    """save_folder/stem+suffix が既に存在する場合、_2, _3... と連番を付けて衝突を避ける。
    ファイル名は秒単位のため、自動保存直後のCtrl+Sなど同じ秒に2回保存すると
    連番が無ければ片方が黙って上書きされてしまう。
    連番セッション(-001形式)でも、同じ秒に別の付箋を開いてしまった場合の保険として残す。"""
    path = save_folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = save_folder / f"{stem}_{n}{suffix}"
        n += 1
    return path


def new_session_stem() -> str:
    """連番セッション(1枚の付箋 = 1セッション)のファイル名の頭を作る。
    付箋を作った時刻で固定し、以降は連番だけを増やして
    rapture_20260823_140919-001.png のように一連の作業を名前でまとめる。"""
    return datetime.datetime.now().strftime("rapture_%Y%m%d_%H%M%S")


def save_image(image: QImage, capture_settings: dict, stem: str = None, index: int = None):
    """imageを設定フォルダへ保存する。戻り値は保存先Pathか、失敗時はNone。

    stem/index を渡すと連番セッションのファイル名(stem-001)になる。省略した場合は
    従来どおり「今の時刻」から単発のファイル名を作る(引数2つの既存呼び出し互換)。
    JPEGはアルファチャンネルを持てないため保存前にRGBへ変換する。"""
    save_folder = Path(capture_settings.get("save_folder", r"C:\bak\rapture"))
    save_folder.mkdir(parents=True, exist_ok=True)

    fmt = capture_settings.get("save_format", "png").lower()
    name = stem or new_session_stem()
    if index is not None:
        name = f"{name}-{int(index):03d}"
    path = _unique_path(save_folder, name, f".{fmt}")

    if fmt in ("jpg", "jpeg"):
        out_image = image.convertToFormat(QImage.Format_RGB32)
        ok = out_image.save(str(path), "JPG", int(capture_settings.get("jpeg_quality", 90)))
    else:
        ok = image.save(str(path), "PNG")

    if not ok:
        print(f"[tray-tools] 保存に失敗しました: {path}", file=sys.stderr)
        return None

    return path
