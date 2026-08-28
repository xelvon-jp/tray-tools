# screen_mirror.py
# 手元の画面の一部を切り取り、別のモニタへ全画面でミラーする「画面ミラー」。
#
# 使い方はこうなる。手元(モニタ1)で範囲を選ぶ → 画面共有に出しているモニタ2へ、その
# 範囲だけが全画面で映る。範囲を選んだあとも中は普通に操作できる(ミラーは見ているだけ)
# ので、ブラウザでリンクを辿ろうが PowerPoint を送ろうが、そのまま向こうに映る。
#
# なぜこの方式か。同梱の presenter.html はローカルHTML資料専用で、リンク遷移をすると
# 制御から外れる。WebEngine を内蔵して自前のブラウザにする案もあったが、それは実質
# ブラウザを作ることになる。画面をそのまま映せば操作の問題が丸ごと消える——対象が
# ブラウザでも PowerPoint でも PDF でも関係ない。presenter.html はローカル資料用として
# そのまま残す(併存)。画面へ重ねる側(presenter_overlay.py)も残す。
#
# レーザーとスポットライトの描き方は presenter_overlay.py と共有している
# (presenter_overlay.draw_laser / draw_spotlight / laser_params / spotlight_params)。
# あちらは「カーソルのある画面に重ねる」もの、こちらは「ミラー先に描く」ものだが、
# 光点の暈もスポットの穴も設定キーも同じでよいので、描画と設定読みだけをあちらへ寄せた。
# 2箇所に同じ絵を書くと、片方だけ直したときに見た目が食い違う。
#
# 選ぶ範囲の大きさは自由で、固定されるのは縦横比だけ(既定 16:9)。800x450 でも
# 1600x900 でも 1920x1080 でも選べる。そのぶん、大きく選ぶほど重くなる。
#
# 実測(このPC。設計の根拠)。capture_grab.grab_region() 1枚あたり:
#   1280x720   16.9ms  → 30fpsで回して 29.6fps / CPU 71%(1コア換算)
#   1920x1080  33.5ms  → 30fpsで回して 25.8fps / CPU 108%(コマ落ちする)
# 拡大は QPixmap.scaled() の 2560x1440 で 7.2ms(SmoothTransformation。Fast のほうが
# 遅いので使わない)。既定を30fpsにし、負荷が問題になる環境では設定 fps で落とせる。
# 1920x1080 を映したいなら fps を 20 程度にするのが現実的。
#
# 大きい範囲も選べてしまうので、目安を2か所に出す。範囲選択中はその大きさで出せる
# おおよそのフレームレート(estimated_fps)、ミラー中は実測値を手元の枠の帯に出す
# (帯は範囲の外にあるのでミラーには映り込まない)。止めはしない——止めるほどのことでは
# ないし、重くても映したい場面はある。数字が見えていれば自分で選べる。
#
# 座標はすべてQtの論理座標で扱う。Win32(物理ピクセル)を混ぜるのはマウスのボタン状態を
# 読む GetAsyncKeyState だけで、あれは座標を返さないので変換は要らない
# (座標を混ぜるときは capture_grab.device_bounds_to_logical を通すこと)。
import ctypes
import sys
import time
import traceback

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QPainter,
    QPen,
    QPolygonF,
    QRegion,
)
from PySide6.QtWidgets import QWidget

import presenter_overlay
from capture_grab import grab_region
from capture_overlay import SelectionOverlay
from toast import show_toast

# ---------------------------------------------------------------
# 既定値
#
# settings.py の DEFAULT_SETTINGS["screen_mirror"] と同じ値を持っている。あちらから
# import しないのは、settings.py をPySide6に依存させないため(presenter_overlay と同じ理由)。
# 値を変えるときは両方。
# ---------------------------------------------------------------

# 1秒あたり何枚送るか。上の実測のとおり、1280x720 なら 30fps でほぼ落ちずに回る。
# 上限を60にしてあるのは、これ以上を指定できても1枚17msの取得が追いつかないため
# (指定だけ増えてコマ落ちが増える)。
DEFAULT_FPS = 30
MAX_FPS = 60

# 選択範囲の比率。キー(1/2/3)とホイールで選択中に切り替えられる。
# 値は (設定に書く名前, 画面に出す名前, 幅÷高さ or None)。
ASPECTS = (
    ("free", "自由", None),
    ("4:3", "4:3", 4.0 / 3.0),
    ("16:9", "16:9", 16.0 / 9.0),
)
DEFAULT_ASPECT = "16:9"

# ミラー先に描くカーソル。実カーソル(GetCursorInfo/DrawIconEx)は使わず、矢印を自前で描く。
# 拡大率によらず一定の大きさにする(どの範囲を選んでも見え方が揃う)。少し大きめなのは、
# 画面共有で相手に届くころには縮んでいるため。
DEFAULT_CURSOR_SIZE = 34
DEFAULT_CURSOR_COLOR = "#ffffff"
DEFAULT_CURSOR_OUTLINE = "#101010"

# クリックの可視化。画面共有では「今押した」が伝わりにくいので、押した瞬間に波紋を出す。
DEFAULT_CLICK_RIPPLE = True
DEFAULT_CLICK_RIPPLE_MS = 420
DEFAULT_CLICK_RIPPLE_RADIUS = 62
DEFAULT_CLICK_RIPPLE_COLOR = "#ffd400"

# 手元に出す「いまミラーしている範囲」の枠。
DEFAULT_SOURCE_FRAME = True
DEFAULT_SOURCE_FRAME_COLOR = "#00c8ff"
DEFAULT_SOURCE_FRAME_WIDTH = 3
DEFAULT_SOURCE_FRAME_OPACITY = 0.55

# 手元の枠に添える帯の高さ(論理px)。実測フレームレートと範囲の大きさをここへ出す。
# 帯は選択範囲の外側(枠のさらに外)にあるのでミラーには映り込まない。
SOURCE_FRAME_BAND_HEIGHT = 18
SOURCE_FRAME_BAND_INTERVAL_MS = 1000

# 範囲の大きさから所要時間を見積もるための係数。上の実測2点を直線で結んだもの。
#   1280x720  (921,600px) → 921600*GRAB_MS_PER_PIXEL + GRAB_MS_BASE = 16.9ms
#   1920x1080(2,073,600px) →                                          33.4ms
# 拡大のぶん(SCALE_MS)は範囲の大きさではなくミラー先の大きさで決まるので定数で置く。
# あくまで「このPCでの目安」で、正確さは求めていない(選ぶときに桁が分かればよい)。
GRAB_MS_PER_PIXEL = 1.43e-5
GRAB_MS_BASE = 3.7
SCALE_MS = 7.2

# 最前面へ押し上げ直す間隔(ms)。ミラー先のモニタには会議アプリの窓も居るので、
# 押し上げ続けないと裏へ回って何も見えなくなる
# (presenter_overlay.TOPMOST_INTERVAL_MS と同じ値・同じ理由)。
TOPMOST_INTERVAL_MS = 500

# 矢印カーソルの形。高さ1.0・幅0.60 に正規化した多角形で、描くときに cursor_size 倍する。
# Windowsの標準カーソルと同じ「左上が尖った矢印」の輪郭。数字を正規化して持つのは、
# 大きさを設定で変えても形が崩れないようにするため。
_CURSOR_POLYGON = (
    (0.00, 0.00),
    (0.00, 0.85),
    (0.20, 0.66),
    (0.34, 1.00),
    (0.48, 0.94),
    (0.34, 0.62),
    (0.60, 0.61),
)

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

_user32 = ctypes.windll.user32
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short


def _guard(where: str, notify: bool = True) -> None:
    """スロットの中で起きた例外をここで止め、標準エラー(と必要なら通知)へ回す。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。ミラーは
    毎フレーム走るタイマーで動くので、1回の失敗で常駐アプリごと消えては割に合わない。
    しかも通常起動は pythonw.exe で標準エラーがどこにも出ず、落ちた理由が残らない。

    notify=False は周期タイマーと描画用。毎フレームトーストを出すと画面が埋まる
    (presenter_overlay._guard と同じ役目・同じ理由)。"""
    traceback.print_exc()
    print(f"[tray-tools] 画面ミラー: {where}に失敗しました", file=sys.stderr)
    if notify:
        show_toast(f"画面ミラー\n{where}に失敗しました")


def _as_int(value, default: int) -> int:
    """設定値を整数にする。読めない値が書かれていたら既定に落とす
    (settings.json は手で編集する前提。書き損じで機能ごと出ないのは避ける)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float, minimum: float = 0.0, maximum: float = None) -> float:
    """設定値を小数にして範囲へ丸める。不透明度は0〜1の外を書かれると描画が破綻する。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    if maximum is not None and number > maximum:
        return maximum
    return number


def _as_color(value, default: str) -> QColor:
    """設定値を色にする。QColor は不正な文字列でも例外を投げず無効な色を返す。"""
    color = QColor(value) if isinstance(value, str) and value else QColor()
    return color if color.isValid() else QColor(default)


def mirror_config(app_settings: dict) -> dict:
    """settings.json の screen_mirror セクション。無ければ空辞書。"""
    return (app_settings or {}).get("screen_mirror", {}) or {}


def cursor_pos() -> QPoint:
    """カーソルの位置(Qt論理座標)。

    関数にしてあるのは検証のため。実際にカーソルを動かさずに確かめたいので、
    テスト側は screen_mirror.cursor_pos を差し替える(呼び出しは毎回この名前を
    モジュールから引くので、差し替えがそのまま効く)。"""
    return QCursor.pos()


def available_screens() -> list:
    """今つながっているモニタ(QScreen)の一覧。

    cursor_pos と同じで、関数にしてあるのは検証のため。モニタが3枚以上ある構成の
    挙動を、実機を繋がずに確かめられるようにしてある(名前と矩形だけを持つ偽物を
    並べて差し替える)。呼ぶ側は毎回この名前をモジュールから引くこと。"""
    return QGuiApplication.screens()


def mouse_buttons_down() -> tuple:
    """左・右ボタンが今押されているか (left, right)。

    グローバルなマウスフックは使わない。keyboard ライブラリのフックはCOMとGCが衝突して
    プロセスごと落ちる持病があり(hotkeys.init_keyboard のコメント参照)、同じ轍を踏みたく
    ない。ミラーは既にフレームごとのループを持っているので、そこで GetAsyncKeyState を
    読めば足りる(30fpsなら33ms以内に必ず気付く)。

    下位ビット(0x0001)は「前回呼んでから押されたか」で、他のアプリが同じAPIを呼ぶと
    そちらに取られる。今の状態を示す上位ビット(0x8000)だけを見て、押した瞬間の判定は
    こちらで前フレームと比べて出す。"""
    try:
        left = bool(_user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        right = bool(_user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
    except OSError:
        # ctypes の呼び出しが失敗することは通常無いが、ここで投げるとフレームごとに
        # 落ちることになる。クリック波紋が出ないだけにしておく。
        return False, False
    return left, right


def aspect_ratio(key: str):
    """比率の名前("16:9"等)から 幅÷高さ を引く。自由なら None。"""
    for name, _label, ratio in ASPECTS:
        if name == key:
            return ratio
    return None


def aspect_label(key: str) -> str:
    for name, label, _ratio in ASPECTS:
        if name == key:
            return label
    return key


def estimated_fps(width: int, height: int, limit: int = DEFAULT_FPS) -> float:
    """その大きさを映したときに出せそうなフレームレートの目安。

    範囲は自由に選べるので、大きく選べば重い範囲も選べてしまう。選んでいる最中に
    「この大きさだと何fpsくらいか」が見えていれば、自分で加減できる。
    係数の出どころは GRAB_MS_PER_PIXEL のコメント(このPCでの実測2点)。"""
    frame_ms = (width * height) * GRAB_MS_PER_PIXEL + GRAB_MS_BASE + SCALE_MS
    if frame_ms <= 0:
        return float(limit)
    return min(float(limit), 1000.0 / frame_ms)


def pick_target_screen(screens, wanted_name: str, source_rect: QRect = None):
    """ミラーを出すモニタを選ぶ。決められなければ None。

    screens を引数で受けるのは、モニタが3枚以上ある構成の挙動を確かめられるようにする
    ため(実機を繋がずに、名前と矩形だけを持つ偽物を並べて呼べる)。選び方そのものは
    QGuiApplication を見なくても決まるので、外から渡すほうが素直でもある。

    優先するのは設定で指定された名前。無い/見つからないときは「選択範囲が乗っていない
    モニタ」の先頭に落とす(自分を撮ると無限に入れ子になるので、範囲のあるモニタは
    候補から外す)。"""
    if not screens:
        return None

    if wanted_name:
        for screen in screens:
            if screen.name() == wanted_name:
                return screen

    if source_rect is None:
        return screens[0]
    for screen in screens:
        if not screen.geometry().intersects(source_rect):
            return screen
    return None


def screen_label(screen) -> str:
    """モニタ選択メニューに出す名前。QScreen.name() だけでは "\\\\.\\DISPLAY1" のような
    文字列で見分けが付かないので、解像度と主モニタかどうかを添える。"""
    geometry = screen.geometry()
    parts = [f"{screen.name()}", f"{geometry.width()}x{geometry.height()}"]
    if screen is QGuiApplication.primaryScreen():
        parts.append("主")
    return "  ".join(parts)


# ---------------------------------------------------------------
# 範囲選択(比率固定つき)
# ---------------------------------------------------------------
class AspectSelectionOverlay(SelectionOverlay):
    """比率を選べる範囲選択オーバーレイ。

    減光・選択枠・小さすぎる選択の扱いは capture_overlay.SelectionOverlay のままで、
    今回の差分は「比率固定」だけ(画面定規が情報表示だけを差し替えているのと同じ流儀)。

    固定のしかたは、ドラッグ中の終点 self._current を比率に合う位置へ寄せるだけにした。
    paintEvent も mouseReleaseEvent も基底が self._current から矩形を作るので、1箇所を
    直せば見えている枠と確定する矩形が必ず一致する(両方に同じ計算を書くと、片方だけ
    直したときにズレる)。"""

    # ウィンドウ単位の選択は使わない。あれはキャプチャのために「クリックした窓を丸ごと
    # 撮る」機能で、非ドラッグ中は対象の窓を黄色い枠で予告する。こちらが選ぶのは
    # 「画面のこの範囲」であって窓ではないうえ、比率を保った矩形を作りたいので、
    # 窓の形に引っぱられては困る(画面定規が同じ理由で切っている)。
    window_pick_enabled = False

    def __init__(self, aspect: str = DEFAULT_ASPECT, fps_limit: int = DEFAULT_FPS):
        super().__init__()
        self.aspect = aspect if aspect_ratio(aspect) is not None or aspect == "free" else DEFAULT_ASPECT
        # 選んでいる大きさで出せそうなフレームレートを添えるために持つ(estimated_fps)。
        self.fps_limit = fps_limit

    # ---------------------------------------------------------------
    # 比率
    # ---------------------------------------------------------------
    def _ratio(self):
        return aspect_ratio(self.aspect)

    def _cycle_aspect(self, step: int) -> None:
        names = [name for name, _label, _ratio in ASPECTS]
        index = names.index(self.aspect) if self.aspect in names else 0
        self.aspect = names[(index + step) % len(names)]
        self._reconstrain()

    def _reconstrain(self) -> None:
        """比率を変えたときに、いま引いている枠をその比率へ合わせ直す。
        ドラッグの途中で切り替えられるようにするための処理。"""
        if self._dragging and self._start is not None and self._current is not None:
            self._current = self._constrained_corner(self._start, self._current)
        self.update()

    def _constrained_corner(self, start: QPoint, current: QPoint) -> QPoint:
        """終点を比率に合う位置へ寄せる。自由なら素通し。

        幅・高さは QRect(start, current) の大きさ、つまり両端を含む(差 +1)。差のまま
        計算すると1pxぶんずれて、確定した矩形の比率が狙いから外れる。
        どちらの辺に合わせるかは「大きいほうへ合わせる」。引いた面積が減る向きに
        合わせると、対角へ引いているのに枠が縮んで操作感が悪い。"""
        ratio = self._ratio()
        if ratio is None:
            return current

        dx = current.x() - start.x()
        dy = current.y() - start.y()
        sign_x = 1 if dx >= 0 else -1
        sign_y = 1 if dy >= 0 else -1
        width = abs(dx) + 1
        height = abs(dy) + 1

        if width >= height * ratio:
            height = max(int(round(width / ratio)), 1)
        else:
            width = max(int(round(height * ratio)), 1)

        return QPoint(
            start.x() + sign_x * (width - 1),
            start.y() + sign_y * (height - 1),
        )

    def _window_rect_at(self, point_local):
        """クリックでウィンドウ全体を選んだときも比率へ合わせる。

        基底はウィンドウの矩形をそのまま返すが、比率を固定しているのにここだけ
        素通しでは、確定した瞬間に枠の形が変わることになる。ウィンドウの中央を保って
        内側へ収める(はみ出す向きに広げると画面外や隣の窓まで映してしまう)。"""
        rect_global = super()._window_rect_at(point_local)
        ratio = self._ratio()
        if rect_global is None or ratio is None:
            return rect_global

        width = rect_global.width()
        height = rect_global.height()
        if width > height * ratio:
            width = max(int(round(height * ratio)), 1)
        else:
            height = max(int(round(width / ratio)), 1)
        fitted = QRect(0, 0, width, height)
        fitted.moveCenter(rect_global.center())
        return fitted

    # ---------------------------------------------------------------
    # 入力
    # ---------------------------------------------------------------
    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self._dragging and self._start is not None:
            self._current = self._constrained_corner(self._start, self._current)

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        # 基底が self._current にカーソル位置をそのまま入れたあとで寄せ直す。
        if self._dragging and self._start is not None and self._current is not None:
            self._current = self._constrained_corner(self._start, self._current)
            self.update()

    def wheelEvent(self, event):
        """ホイールで比率を回す。ドラッグしながら左手を使わずに切り替えられる。"""
        try:
            self._cycle_aspect(1 if event.angleDelta().y() < 0 else -1)
        except Exception:
            _guard("比率の切り替え", notify=False)

    def keyPressEvent(self, event):
        # 1/2/3 で直接、Space/Tab で順送り。Esc は基底(キャンセル)へ渡す。
        key = event.key()
        if key in (Qt.Key_1, Qt.Key_2, Qt.Key_3):
            self.aspect = ASPECTS[key - Qt.Key_1][0]
            self._reconstrain()
            return
        if key in (Qt.Key_Space, Qt.Key_Tab):
            self._cycle_aspect(1)
            return
        super().keyPressEvent(event)

    # ---------------------------------------------------------------
    # 表示
    # ---------------------------------------------------------------
    def _draw_info(self, painter, rect_local):
        """基底の「幅x高さ (始点)」に、比率と負荷の目安を1行足す。

        目安を出すのは、大きさを自由に選べるから。大きく選ぶほど重くなるが、それは
        選び終えて映してみるまで分からない。選んでいる最中に数字が見えていれば、
        重いと思った時点で引き直せる。"""
        super()._draw_info(painter, rect_local)
        fps = estimated_fps(rect_local.width(), rect_local.height(), self.fps_limit)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 10))
        painter.drawText(
            QPoint(rect_local.x(), min(rect_local.bottom() + 18, self.height() - 4)),
            f"比率: {aspect_label(self.aspect)}　目安 {fps:.0f}fps",
        )

    def _draw_hover_info(self, painter):
        super()._draw_hover_info(painter)
        if self._hover is None:
            return
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 9))
        painter.drawText(
            QPoint(self._hover.x() + 14, self._hover.y() + 16),
            f"比率: {aspect_label(self.aspect)}  （1=自由 2=4:3 3=16:9 / ホイールで切替）",
        )


# ---------------------------------------------------------------
# 最前面に居続ける枠なし窓の共通部分
# ---------------------------------------------------------------
class _TopmostWindow(QWidget):
    """ミラー窓と手元の枠に共通の作り。枠なし・最前面・前面を奪わない。

    presenter_overlay._Overlay と作りは同じだが、あちらは「設定 target_screen で出す先を
    決める」ところまで持っている。こちらは出す先が呼び出し側の都合(ミラー先のモニタ /
    選択範囲のまわり)で決まるので、共通なのは最前面の維持だけ。それだけのために
    継承すると place_on_target を無効化して回ることになるので、ここに小さく持つ。"""

    closed = Signal()

    # マウスを透過するか。透過する側は自分ではマウスもキーも受け取れない。
    click_through = False

    def __init__(self, geometry: QRect):
        super().__init__()
        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # 発表中に前面を奪わない。操作しているのは手元のアプリで、そちらから
            # キーボードの相手を取り上げたら発表が止まる。
            | Qt.WindowDoesNotAcceptFocus
        )
        if self.click_through:
            # WA_TransparentForMouseEvents だけではQtが自分の中でイベントを配らなく
            # なるだけで、Windowsから見れば依然そこに窓がある(クリックは吸われる)。
            # 下のアプリまで通すには WS_EX_TRANSPARENT(＝WindowTransparentForInput)が要る。
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if self.click_through:
            self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self.setGeometry(geometry)

        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._on_topmost)

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self._topmost_timer.start(TOPMOST_INTERVAL_MS)

    def hideEvent(self, event):
        self._topmost_timer.stop()
        super().hideEvent(event)

    def _on_topmost(self):
        try:
            if self.isVisible():
                self.raise_()
        except Exception:
            _guard("最前面の維持", notify=False)


# ---------------------------------------------------------------
# 手元に出す「いまミラーしている範囲」の枠
# ---------------------------------------------------------------
class SourceFrameWindow(_TopmostWindow):
    """選択範囲のまわりに薄い枠を出しっぱなしにする窓。手元のどこを映しているかを
    見失わないためのもの。

    枠がミラーに映り込んではいけない(自分を撮ると入れ子になる)。方法は3つ考えられる。
      1. 範囲の外側に描く
      2. キャプチャの直前に隠して、撮り終えたら出す
      3. 透明(アルファ0)で範囲を覆い、そこは何も足さないことに賭ける
    採ったのは 1。2 は毎フレーム show/hide することになり、30fpsでちらつくうえ
    キャプチャ1枚あたりの時間に窓の表示切替が乗る(実測17msの予算に対して割に合わない)。
    3 は合成結果が元のピクセルと寸分違わない前提に乗ることになり、確かめようが無い。
    1 なら「撮る範囲には最初から居ない」ことが形で保証できる。

    そのうえで setMask で範囲のぶんを窓から切り抜いている。枠の内側を透明に塗るのでは
    なく、窓そのものに穴を空ける。こうすると撮る範囲にこの窓のピクセルが1枚も無いことが
    Windows から見ても確かで、ついでに穴の部分のクリックは何もしなくても下へ抜ける。

    枠の外側に細い帯を1本足し、そこへ実測フレームレートを出す(status を渡したとき)。
    範囲は自由に選べるので大きく選べば重くなるが、映している最中にそれが分かる場所が
    どこにも無かった。帯も範囲の外なので、これもミラーには映り込まない。"""

    click_through = True

    def __init__(self, source_rect: QRect, app_settings: dict, status=None):
        cfg = mirror_config(app_settings)
        self._width = max(_as_int(cfg.get("source_frame_width"), DEFAULT_SOURCE_FRAME_WIDTH), 1)
        self._color = _as_color(cfg.get("source_frame_color"), DEFAULT_SOURCE_FRAME_COLOR)
        opacity = _as_float(
            cfg.get("source_frame_opacity"), DEFAULT_SOURCE_FRAME_OPACITY, 0.0, 1.0
        )
        self._color.setAlphaF(opacity)

        # 呼ぶと1行の文字列を返すもの(実測fps)。無ければ帯そのものを作らない。
        self._status = status
        self._status_text = ""
        band = SOURCE_FRAME_BAND_HEIGHT if status is not None else 0

        # 帯は範囲の上に出す。上に置けない(画面の端に寄せて選んだ)ときだけ下へ回す。
        # 上を既定にするのは、下端はタスクバーと重なりやすいため。
        above = band if band and self._fits_above(source_rect, band) else 0
        below = band - above
        outer = source_rect.adjusted(
            -self._width, -self._width - above, self._width, self._width + below
        )
        super().__init__(outer)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 窓の中の、撮られる範囲にあたる矩形。ここを窓から切り抜く。
        self._hole = QRect(
            self._width, self._width + above, source_rect.width(), source_rect.height()
        )
        self.setMask(QRegion(self.rect()).subtracted(QRegion(self._hole)))

        self._band = QRect()
        if band:
            self._band = QRect(0, 0 if above else self.height() - band, self.width(), band)
            self._status_timer = QTimer(self)
            self._status_timer.timeout.connect(self._on_status)
            self._status_timer.start(SOURCE_FRAME_BAND_INTERVAL_MS)

    @staticmethod
    def _fits_above(source_rect: QRect, band: int) -> bool:
        """範囲の上に帯を置けるか。置き先の画面の上端よりはみ出すなら置けない。"""
        screen = QGuiApplication.screenAt(source_rect.topLeft())
        top = screen.geometry().top() if screen is not None else 0
        return source_rect.top() - band >= top

    def _on_status(self):
        try:
            text = self._status() if self._status is not None else ""
            if text != self._status_text:
                self._status_text = text
                self.update(self._band)
        except Exception:
            _guard("枠の表示の更新", notify=False)

    def paintEvent(self, event):
        # マスクで穴が空いているので、全面を塗っても枠と帯の部分にしか色は乗らない。
        painter = QPainter(self)
        painter.fillRect(event.rect(), self._color)
        if self._band.isEmpty() or not self._status_text:
            return
        # 帯は枠と同じ色のままだと文字が読めない。下地を敷いてから白で書く。
        painter.fillRect(self._band, QColor(20, 20, 20, 200))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Meiryo", 9))
        painter.drawText(
            self._band.adjusted(6, 0, -6, 0),
            Qt.AlignVCenter | Qt.AlignLeft,
            self._status_text,
        )


# ---------------------------------------------------------------
# ミラー先の窓
# ---------------------------------------------------------------
class MirrorWindow(_TopmostWindow):
    """選んだモニタに全画面で出し、手元の選択範囲を毎フレーム映す窓。

    マウスは透過しない。透過すると、全画面で覆っているせいで発表者からは見えない
    「下のアプリ」へクリックが素通しで届く。見えないものを誤って操作するくらいなら、
    クリックを吸って何も起きないほうが安全(presenter_overlay.BlankOverlay が透過しない
    のと同じ判断)。閉じる手段はホットキーとトレイメニューにある。"""

    click_through = False

    def __init__(self, source_rect: QRect, screen, app_settings: dict):
        super().__init__(screen.geometry())
        self.source_rect = QRect(source_rect)
        self.screen_name = screen.name()
        cfg = mirror_config(app_settings)

        fps = min(max(_as_int(cfg.get("fps"), DEFAULT_FPS), 1), MAX_FPS)
        self.fps = fps

        self._cursor_size = max(_as_int(cfg.get("cursor_size"), DEFAULT_CURSOR_SIZE), 8)
        self._cursor_color = _as_color(cfg.get("cursor_color"), DEFAULT_CURSOR_COLOR)
        self._cursor_outline = _as_color(cfg.get("cursor_outline"), DEFAULT_CURSOR_OUTLINE)

        self._ripple_enabled = bool(cfg.get("click_ripple", DEFAULT_CLICK_RIPPLE))
        self._ripple_ms = max(_as_int(cfg.get("click_ripple_ms"), DEFAULT_CLICK_RIPPLE_MS), 1)
        self._ripple_radius = max(
            _as_int(cfg.get("click_ripple_radius"), DEFAULT_CLICK_RIPPLE_RADIUS), 4
        )
        self._ripple_color = _as_color(cfg.get("click_ripple_color"), DEFAULT_CLICK_RIPPLE_COLOR)

        # レーザーとスポットライトの見た目は presenter_overlay の設定
        # (presenter_overlay セクション)をそのまま使う。同じ道具の同じ光点なので、
        # 色や大きさを2箇所で設定させる意味が無い。
        self._laser = presenter_overlay.laser_params(app_settings)
        self._spot = presenter_overlay.spotlight_params(app_settings)
        self.laser_on = False
        self.spotlight_on = False

        # 直近のフレーム。QImage は暗黙的共有なので、grab_region が返す copy() 済みの
        # ものをそのまま持つ(こちらでさらに触らない)。
        self._frame = None
        self._cursor_global = cursor_pos()
        self._buttons = (False, False)
        # (押した位置(グローバル), 押した時刻) の並び。古いものから消えていく。
        self._ripples = []

        # 実測のフレームレート。手元の枠の帯に出すのと、検証で数字を見るために数える。
        # current_fps は直近1秒ぶん(始めてからの平均だと、重い範囲に変えても数字が
        # なかなか動かない)。measured_fps() は始めてからの通算。
        self.frame_count = 0
        self.start_time = None
        self.current_fps = 0.0
        self._fps_mark_time = None
        self._fps_mark_count = 0

        self._timer = QTimer(self)
        # 既定(CoarseTimer)はWindowsで十数ms単位に丸められ、30fpsを狙っても実測が
        # 目に見えて落ちる(presenter_overlay の poll と同じ理由)。
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(max(int(round(1000.0 / fps)), 1))
        self._timer.timeout.connect(self._on_tick)

    # ---------------------------------------------------------------
    # 座標の対応
    # ---------------------------------------------------------------
    def video_rect(self) -> QRectF:
        """窓の中で実際に映像が出る矩形(ローカル座標)。

        アスペクト比は保ち、余った部分は黒のまま残す(引き伸ばすと文字が歪む)。"""
        source = self.source_rect
        if source.width() <= 0 or source.height() <= 0:
            return QRectF(0, 0, 0, 0)
        scale = min(self.width() / source.width(), self.height() / source.height())
        width = source.width() * scale
        height = source.height() * scale
        return QRectF(
            (self.width() - width) / 2.0,
            (self.height() - height) / 2.0,
            width,
            height,
        )

    def scale(self) -> float:
        """手元1pxがミラー先で何pxになるか。"""
        source = self.source_rect
        if source.width() <= 0 or source.height() <= 0:
            return 1.0
        return min(self.width() / source.width(), self.height() / source.height())

    def map_to_window(self, point_global: QPoint) -> QPointF:
        """手元のグローバル座標(論理)を、この窓の中の座標へ直す。
        カーソル・レーザー・スポット・クリック波紋の位置はすべてここを通す。"""
        video = self.video_rect()
        scale = self.scale()
        return QPointF(
            video.x() + (point_global.x() - self.source_rect.x()) * scale,
            video.y() + (point_global.y() - self.source_rect.y()) * scale,
        )

    def contains_source(self, point_global: QPoint) -> bool:
        return self.source_rect.contains(point_global)

    # ---------------------------------------------------------------
    # 表示・周期
    # ---------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.start_time = time.perf_counter()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _on_tick(self):
        """周期の本体。ここは必ず try で受けること(PySide6はスロットから例外が
        投げ切られるとプロセスごと終わる)。1枚撮って、入力を読んで、描き直す。"""
        try:
            self.grab_frame()
            self._update_fps()
            self._poll_input()
            self.update()
        except Exception:
            _guard("フレームの更新", notify=False)

    def _update_fps(self) -> None:
        """直近1秒で何枚出せたかを数え直す。"""
        now = time.perf_counter()
        if self._fps_mark_time is None:
            self._fps_mark_time = now
            self._fps_mark_count = self.frame_count
            return
        elapsed = now - self._fps_mark_time
        if elapsed >= 1.0:
            self.current_fps = (self.frame_count - self._fps_mark_count) / elapsed
            self._fps_mark_time = now
            self._fps_mark_count = self.frame_count

    def grab_frame(self) -> None:
        """手元の選択範囲を1枚撮る。

        devicePixelRatio を1に落としておく。grab_region は「この画像は物理ピクセルで
        持っているが論理サイズはこれ」という印を付けて返すが、こちらは映像を枠へ
        引き伸ばして描くだけで、論理サイズを使う場面が無い。印が付いたままだと
        drawImage の転送元がどちらの単位なのか曖昧になるので、生のピクセルとして扱う。
        grab_region が返すのは copy() 済みの独立した画像なので、書き換えてよい。"""
        frame = grab_region(self.source_rect)
        frame.setDevicePixelRatio(1.0)
        self._frame = frame
        self.frame_count += 1

    def _poll_input(self) -> None:
        """カーソル位置とマウスのボタンを読む。押した瞬間だけ波紋を1つ足す。"""
        self._cursor_global = cursor_pos()

        if not self._ripple_enabled:
            return
        buttons = mouse_buttons_down()
        now = time.perf_counter()
        for index in (0, 1):
            if buttons[index] and not self._buttons[index]:
                self._ripples.append((QPoint(self._cursor_global), now))
        self._buttons = buttons

        # 出し終えたものを捨てる。放っておくと発表中ずっと溜まる。
        limit = self._ripple_ms / 1000.0
        self._ripples = [item for item in self._ripples if now - item[1] <= limit]

    def measured_fps(self) -> float:
        """始めてからの通算フレームレート。診断用(設定した fps と実際が違うことがある)。"""
        if not self.start_time:
            return 0.0
        elapsed = time.perf_counter() - self.start_time
        return self.frame_count / elapsed if elapsed > 0 else 0.0

    def status_text(self) -> str:
        """手元の枠の帯に出す1行。範囲の大きさと、出せている実測フレームレート。

        設定値も併記するのは、数字が低いときに「設定で落としてある」のか
        「重くて出ていない」のかが区別できないと、手の打ちようが無いため。"""
        return (
            f"ミラー中 {self.source_rect.width()}x{self.source_rect.height()}"
            f"　{self.current_fps:.0f}fps / 設定 {self.fps}fps"
        )

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def paintEvent(self, event):
        painter = QPainter(self)
        # 余白は黒。映像より先に全面を塗る(前のフレームの端が残らないように)。
        painter.fillRect(event.rect(), QColor(0, 0, 0))
        if self._frame is None:
            return

        video = self.video_rect()
        # 拡大は滑らかに。実測で SmoothTransformation のほうが FastTransformation より
        # 速く、しかも綺麗(2560x1440への拡大で7.2ms)。
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawImage(video, self._frame, QRectF(self._frame.rect()))

        painter.setRenderHint(QPainter.Antialiasing)
        inside = self.contains_source(self._cursor_global)
        center = self.map_to_window(self._cursor_global)

        # スポットライトは映像の上・カーソルの下。減光でカーソルまで暗くしては本末転倒。
        if self.spotlight_on and inside:
            presenter_overlay.draw_spotlight(painter, video, center, **self._spot)

        self._draw_ripples(painter)

        if not inside:
            # 範囲の外にカーソルが出たら何も描かない。
            #
            # 端に貼り付けて見せる案もあったが採らない。ミラーは「この範囲を見せている」
            # という約束で、実際には指していない場所に光点や矢印が居座るのは嘘になる
            # (見ている側は指されたところを見る)。消えること自体が「発表者はいま範囲の
            # 外を触っている」という正しい情報になるし、戻ってくれば同じ位置に現れる。
            return
        if self.laser_on:
            # レーザー中はカーソルを描かない。同じ位置に2つ重ねると、光点の芯に矢印が
            # 刺さって何を指しているのか分からなくなる。
            presenter_overlay.draw_laser(painter, center, **self._laser)
        else:
            self._draw_cursor(painter, center)

    def _draw_cursor(self, painter: QPainter, center: QPointF) -> None:
        """矢印を1つ描く。実カーソルの絵は使わない(GetCursorInfo/DrawIconEx を持ち込むと
        ハンドルの後始末が増えるうえ、拡大率によって大きさが変わる)。

        大きさは拡大率によらず一定。選んだ範囲が狭いほど拡大率は上がるが、それに合わせて
        カーソルまで大きくすると、範囲ごとに見え方が変わって落ち着かない。

        先端が center。白で塗って濃い色で縁取るのは、明るい資料でも暗い資料でも輪郭が
        残るようにするため。"""
        size = float(self._cursor_size)
        polygon = QPolygonF(
            [QPointF(center.x() + x * size, center.y() + y * size) for x, y in _CURSOR_POLYGON]
        )
        pen = QPen(self._cursor_outline)
        pen.setWidthF(max(size * 0.06, 1.5))
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(self._cursor_color)
        painter.drawPolygon(polygon)

    def _draw_ripples(self, painter: QPainter) -> None:
        """クリックした瞬間の波紋。押した位置から輪が広がって薄くなる。

        画面共有では「今押した」が伝わらない(カーソルの形は変わらないし、押した音も
        向こうには届かない)。左右どちらのボタンでも同じ絵を出す——見ている側に必要なのは
        「操作した」ことで、どちらのボタンかではない。"""
        if not self._ripples:
            return
        now = time.perf_counter()
        duration = self._ripple_ms / 1000.0
        painter.setBrush(Qt.NoBrush)
        for point_global, started in self._ripples:
            if not self.contains_source(point_global):
                continue
            progress = min(max((now - started) / duration, 0.0), 1.0)
            # 大きさは拡大率によらず一定(カーソルと同じ理由)。
            radius = self._ripple_radius * (0.25 + 0.75 * progress)
            color = QColor(self._ripple_color)
            color.setAlphaF(0.85 * (1.0 - progress))
            pen = QPen(color)
            pen.setWidthF(max(self._ripple_radius * 0.08, 2.0))
            painter.setPen(pen)
            painter.drawEllipse(self.map_to_window(point_global), radius, radius)


# ---------------------------------------------------------------
# 開始・終了の管理
# ---------------------------------------------------------------
class MirrorController:
    """範囲選択 → ミラー窓 → 手元の枠、の3つの参照と開閉を1か所で持つ。

    参照をここで抱えるのは、開いた窓の参照を持たないとGCで即消えるため(このアプリで
    何度も踏んでいる)。ScreenFeature がこれを1つだけ属性で持つ。

    どのメソッドも例外を投げない。呼び元はメニューのスロットとホットキーで、どちらも
    投げ切ると常駐ごと終わる場所だから(presenter_overlay.OverlayController と同じ作法)。"""

    def __init__(self, app_settings: dict, settings_path=None, notify=None):
        self.app_settings = app_settings
        self.settings_path = settings_path
        self._notify = notify
        self._overlay = None       # 範囲選択中のオーバーレイ
        self._mirror = None        # ミラー先の窓
        self._frame = None         # 手元の枠
        # 前回選んだ比率。次に始めるときの初期値にする(設定ファイルには書かない。
        # 発表ごとに選び直す性質の値で、残すほどのものではない)。
        self.aspect = str(mirror_config(app_settings).get("aspect") or DEFAULT_ASPECT)

    # ---------------------------------------------------------------
    # 状態
    # ---------------------------------------------------------------
    def is_active(self) -> bool:
        """ミラーが出ているか。範囲を選んでいる最中は含まない。"""
        return self._mirror is not None

    def is_selecting(self) -> bool:
        return self._overlay is not None

    def notify(self, message: str) -> None:
        if self._notify is not None:
            self._notify("画面ミラー", message)

    # ---------------------------------------------------------------
    # ミラー先のモニタ
    # ---------------------------------------------------------------
    def target_screen(self, source_rect: QRect = None):
        """ミラーを出すモニタ(QScreen)。決められなければ None。

        設定 target_screen_name に QScreen.name() が書いてあればそれ。無い/見つからない
        ときは「選択範囲が乗っていないモニタ」の先頭を選ぶ。モニタが3枚以上ある構成では
        どれに出すかを選べないと困るので、メニューから選んだ名前をここで引く形にしてある
        (選び方の本体は pick_target_screen)。"""
        wanted = str(mirror_config(self.app_settings).get("target_screen_name") or "")
        return pick_target_screen(available_screens(), wanted, source_rect)

    def set_target_screen(self, name: str) -> None:
        """ミラー先のモニタを選び直して保存する。動作中なら出し直す。"""
        try:
            self.app_settings.setdefault("screen_mirror", {})["target_screen_name"] = name
            self._save_keys({"target_screen_name": name})
            if self.is_active():
                source_rect = QRect(self._mirror.source_rect)
                self.stop()
                self._start_mirror(source_rect)
        except Exception:
            _guard("ミラー先の変更")

    def _save_keys(self, values: dict) -> None:
        """settings.json の screen_mirror セクションへ、指定したキーだけを書き戻す。

        メモリ上の app_settings は既定値をマージ済みなので、それを丸ごと書き出すと
        未設定の既定値まで明示的に書かれてファイルの姿が変わってしまう
        (launcher.save_bookmark と同じ作法)。"""
        if not self.settings_path:
            return
        try:
            import json
            import os

            import settings as settings_module

            stored = {}
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
            section = stored.setdefault("screen_mirror", {})
            if not isinstance(section, dict):
                section = {}
                stored["screen_mirror"] = section
            section.update(values)
            settings_module.save_settings(stored, self.settings_path)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            print(f"[tray-tools] 画面ミラーの設定を保存できません: {e}", file=sys.stderr)

    # ---------------------------------------------------------------
    # 開始・終了
    # ---------------------------------------------------------------
    def toggle(self) -> bool:
        """出ていれば畳み、出ていなければ範囲選択から始める。戻り値は「これから出す」か。"""
        try:
            if self.is_active() or self.is_selecting():
                self.stop()
                return False
            return self.start_selection()
        except Exception:
            _guard("画面ミラーの切り替え")
            return False

    def start_selection(self) -> bool:
        """範囲選択オーバーレイを出す。選び終えたらミラーが始まる。"""
        if self._overlay is not None or self._mirror is not None:
            return False
        if len(available_screens()) < 2:
            # 1枚しか無いPCでは、映した先が撮る対象そのものになって入れ子になる。
            self.notify("モニタが1台しかありません")
            return False
        try:
            fps_limit = min(
                max(_as_int(mirror_config(self.app_settings).get("fps"), DEFAULT_FPS), 1), MAX_FPS
            )
            self._overlay = AspectSelectionOverlay(self.aspect, fps_limit)
            self._overlay.selection_made.connect(self._on_selection_made)
            self._overlay.canceled.connect(self._on_canceled)
            self._overlay.show()
        except Exception:
            self._overlay = None
            _guard("範囲選択の表示")
            return False
        return True

    def _close_selection(self) -> None:
        """オーバーレイは canceled をemitするだけで自分では閉じない。参照を捨てるだけだと
        全画面を覆うウィジェットが消えるかどうかGC任せになり、マウス操作を奪ったまま
        残り得る(feature_screen._on_canceled と同じ作法)。"""
        overlay = self._overlay
        self._overlay = None
        if overlay is None:
            return
        try:
            overlay.close()
            overlay.deleteLater()
        except RuntimeError:
            pass

    def _on_canceled(self) -> None:
        try:
            if self._overlay is not None:
                self.aspect = self._overlay.aspect
            self._close_selection()
        except Exception:
            _guard("範囲選択の後始末", notify=False)

    def _on_selection_made(self, rect_global: QRect) -> None:
        try:
            if self._overlay is not None:
                self.aspect = self._overlay.aspect
            source_rect = QRect(rect_global)
            self._close_selection()
            self._start_mirror(source_rect)
        except Exception:
            _guard("画面ミラーの開始")

    def _start_mirror(self, source_rect: QRect) -> bool:
        screen = self.target_screen(source_rect)
        if screen is None:
            self.notify("ミラー先のモニタが見つかりません")
            return False
        if screen.geometry().intersects(source_rect):
            # 自分を撮ると無限に入れ子になる。設定でミラー先を指定していると起こりうる。
            self.notify(
                f"選択範囲がミラー先({screen.name()})に重なっています\n"
                "別のモニタを選ぶか、範囲を選び直してください"
            )
            return False

        try:
            self._mirror = MirrorWindow(source_rect, screen, self.app_settings)
            self._mirror.show()
        except Exception:
            self._mirror = None
            _guard("ミラー窓の表示")
            return False

        if bool(mirror_config(self.app_settings).get("source_frame", DEFAULT_SOURCE_FRAME)):
            try:
                self._frame = SourceFrameWindow(
                    source_rect, self.app_settings, status=self._mirror.status_text
                )
                self._frame.show()
            except Exception:
                # 枠が出ないだけならミラーは続けられる。ここで畳むほうが損。
                self._frame = None
                _guard("手元の枠の表示", notify=False)

        self.notify(
            f"{screen.name()} へ {source_rect.width()}x{source_rect.height()} を"
            f"{self._mirror.fps}fpsで表示中"
        )
        return True

    def stop(self) -> None:
        """ミラーも枠も選択も畳む。終了時の後始末からも呼ぶ。"""
        self._close_selection()
        for name in ("_frame", "_mirror"):
            window = getattr(self, name, None)
            setattr(self, name, None)
            if window is None:
                continue
            try:
                window.close()
                window.deleteLater()
            except RuntimeError:
                # C++側が先に消えている場合。参照はもう外したので何もしなくてよい。
                pass
            except Exception:
                _guard("画面ミラーの後始末", notify=False)

    def close_all(self) -> None:
        """終了時の後始末。枠なし・最前面の窓を残したままイベントループを畳むと、
        画面に貼り付いたまま消えないことがある(taskbar_widget と同じ理由)。"""
        self.stop()

    # ---------------------------------------------------------------
    # ミラー先に描くレーザー・スポットライト
    # ---------------------------------------------------------------
    def toggle_light(self, kind: str) -> bool:
        """ミラー先のレーザー/スポットライトを切り替える。戻り値は切り替え後の状態。

        ミラー中はレーザーもスポットも「ミラー先に描く」ものになる。手元の画面に
        重ねてしまうと、それがそのまま撮られて向こうにも映り、二重に見えるうえ
        減光まで焼き込まれる(スポットライトは映像が暗くなって戻せない)。"""
        if self._mirror is None:
            return False
        try:
            if kind == "laser":
                self._mirror.laser_on = not self._mirror.laser_on
                return self._mirror.laser_on
            if kind == "spotlight":
                self._mirror.spotlight_on = not self._mirror.spotlight_on
                return self._mirror.spotlight_on
        except Exception:
            _guard("ミラー先の光点の切り替え", notify=False)
        return False

    def is_light_on(self, kind: str) -> bool:
        if self._mirror is None:
            return False
        if kind == "laser":
            return bool(self._mirror.laser_on)
        if kind == "spotlight":
            return bool(self._mirror.spotlight_on)
        return False
