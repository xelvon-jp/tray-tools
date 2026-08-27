# presenter_overlay.py
# 画面に重ねて使うプレゼン支援。レーザーポインタ・スポットライト・黒画面/白画面の
# 4つを、「何が映っていても効く」形で提供する。
#
# なぜブラウザの中でやらないか。同梱の presenter.html は、資料を about:blank へ
# document.write して親と同一オリジンにしてから子のDOMを直接触る作りで、だからこそ
# スライドを検出できるしレーザーの光点も描ける。逆に言うと、別のサイトを開いた瞬間に
# クロスオリジンになって前提ごと崩れる(DOM操作・スクロール同期・オーバーレイ描画の
# すべてが不可)。「任意のウェブサイトでも使いたい」に応えるには、ブラウザの中で
# 完結させるのをやめて画面の上に重ねるしかない。こちらは対象を選ばず、ウェブでも
# PowerPoint でも PDF でも効く。presenter.html は今のまま残す(ローカル資料用として
# 併存し、こちらが育ったら移行する)。
#
# 窓の作りは capture_overlay / color_picker と同じ
# (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool + WA_TranslucentBackground)。
# 違うのは「下のアプリをそのまま操作できなければならない」こと。発表中はスライドを
# 送り、スクロールし、リンクを踏む。オーバーレイがマウスを奪ったら道具として成立しない。
#
# 開閉の管理(OverlayController)もこのモジュールに置く。窓1つにつき1モジュールの流儀
# からは少し外れるが、4つの窓の排他関係——黒と白は同時に出さない、覆う窓が出たら
# スポットライトは畳む——はここでしか意味を持たない。feature_screen 側にはメニューと
# ホットキーの入口だけを残す。
#
# 座標はすべてQtの論理座標。Win32(物理ピクセル)は一切混ぜないので変換は要らない
# (混ぜるときは capture_grab.device_bounds_to_logical を通すこと)。
import sys
import traceback

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from toast import show_toast

# ---------------------------------------------------------------
# 既定値
#
# settings.py の DEFAULT_SETTINGS["presenter_overlay"] と同じ値を持っている。あちらから
# import しないのは、settings.py をPySide6に依存させないため(設定の読み書きだけの
# モジュールで、setup.py など Qt を起こさない側からも読まれる)。値を変えるときは両方。
# ---------------------------------------------------------------

# カーソル位置のポーリング間隔(ms)。
#
# マウスを透過させた窓にはマウスイベントが来ない(WA_TransparentForMouseEvents を
# 付けた時点で、Qtは自分宛のイベントを配らない)。位置は QCursor.pos() を自分で
# 定期的に読むしかない。
#
# 16ms(60fps相当)にした。常駐アプリなので落とすことも考えたが、実測した費用が
# 十分に小さかったため。2560x1440の窓で repaint() 1回を60回測った中央値:
#   レーザー   全面 1.62ms / 部分(107x107)  0.107ms
#   スポット   全面 2.93ms / 部分(455x455)  0.510ms
#   黒画面     全面 2.50ms (出したとき1回きり。追従しないので周期は無い)
# 既定の部分描画のままなら16ms周期でも1コアの0.7%(レーザー)〜3.2%(スポットライト)で、
# しかもカーソルが止まっている間は描き直し自体を飛ばす(発表者が話している間はゼロ)。
#
# 30fps(33ms)まで落とせば費用は半分になるが、レーザーは速く振ったときの飛びが目に見える。
# 逆に部分描画を切ると全面描き直しになり、16ms周期ではスポットライトが1コアの18%に
# 跳ね上がる。落とすならまず poll_interval_ms を33にするのが順序として正しい。
DEFAULT_POLL_INTERVAL_MS = 16

# 部分描画を使うか。動いた前後の光点/穴を含む矩形だけを update() する。
# 上の実測のとおり効き方が大きい(スポットライトで2.93ms→0.51ms)ので既定は有効。
# 万一この環境で描き残し(尾を引く)が出たら、設定で false にすれば毎回全面を
# 描き直す従来どおりの動きに戻せる。逃げ道を残すために設定に出してある。
DEFAULT_PARTIAL_REPAINT = True

# レーザーの光点。radius は芯の半径、glow_radius はその外に広がる淡い光の半径(論理px)。
DEFAULT_LASER_RADIUS = 9
DEFAULT_LASER_GLOW_RADIUS = 26
DEFAULT_LASER_COLOR = "#ff2d2d"
DEFAULT_LASER_OPACITY = 0.9

# スポットライト。radius は完全に素通しの半径、feather はその外側で減光へ戻すまでの
# 幅(論理px)。dim は周囲の暗さ(0〜1、1で真っ黒)。
DEFAULT_SPOTLIGHT_RADIUS = 140
DEFAULT_SPOTLIGHT_FEATHER = 60
DEFAULT_SPOTLIGHT_DIM = 0.72

# 黒画面/白画面の色。真っ黒が眩しさの点で強すぎるPCもあるので設定に出しておく。
DEFAULT_BLANK_BLACK = "#000000"
DEFAULT_BLANK_WHITE = "#ffffff"
DEFAULT_BLANK_CLICK_TO_CLOSE = True

# 既定でどの画面に出すか。"cursor" ならカーソルのある画面1枚、"all" なら全画面を
# 合算した1枚。既定がカーソルのある画面なのは、マルチディスプレイで全画面を覆うと
# 手元の資料や発表者ツールまで見えなくなるため。
DEFAULT_TARGET_SCREEN = "cursor"

# 最前面へ押し上げ直す間隔(ms)。PowerPoint のスライドショーのように、相手も最前面を
# 主張する窓の上に出す必要がある。押し上げ続けないと裏へ回って何も見えなくなる
# (taskbar_launcher.TOPMOST_INTERVAL_MS と同じ値・同じ理由)。
TOPMOST_INTERVAL_MS = 500

# 通知やメニューに出す名前。kind の文字列はホットキーの設定キー名とも揃えてある。
KIND_LABELS = {
    "laser": "レーザーポインタ",
    "spotlight": "スポットライト",
    "black": "黒画面",
    "white": "白画面",
}

# 同時に出さない組み合わせ。黒と白は互いに排他(両方出しても上の1枚しか見えない)。
# 黒/白を出すときはスポットライトも畳む。画面を覆ってしまえば減光は見えないうえ、
# 全画面の窓が2枚とも最前面を主張し合うことになるため。
# レーザーはどれとも排他にしない(黒画面の上で使いたい場面がありうる)。
EXCLUSIVE_KINDS = {
    "black": ("white", "spotlight"),
    "white": ("black", "spotlight"),
}


def _guard(where: str, notify: bool = True) -> None:
    """スロットの中で起きた例外をここで止め、標準エラー(と必要なら通知)へ回す。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。このオーバーレイは
    タイマーとマウスだけで動くので、1回の失敗で常駐アプリごと消えては割に合わない。
    しかも通常起動は pythonw.exe で標準エラーがどこにも出ず、落ちた理由が残らない。

    notify=False は周期タイマーと描画用。毎周期トーストを出すと画面が埋まる
    (taskbar_launcher._guard と同じ役目・同じ理由)。"""
    traceback.print_exc()
    print(f"[tray-tools] プレゼン支援: {where}に失敗しました", file=sys.stderr)
    if notify:
        show_toast(f"プレゼン支援\n{where}に失敗しました")


def _as_int(value, default: int) -> int:
    """設定値を整数にする。読めない値が書かれていたら既定に落とす。
    settings.json は手で編集する前提なので、書き損じで機能ごと出ないのは避ける
    (taskbar_launcher._as_int と同じ作法)。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float, minimum: float = 0.0, maximum: float = None) -> float:
    """設定値を小数にして範囲へ丸める。暗さや不透明度は0〜1の外を書かれると
    描画そのものが破綻する(負のアルファ等)ので、ここで必ず収める。"""
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
    """設定値を色にする。"#ff2d2d" のような文字列を想定し、読めなければ既定色。
    QColor は不正な文字列でも例外を投げず無効な色を返すので、isValid()で判定する。"""
    color = QColor(value) if isinstance(value, str) and value else QColor()
    return color if color.isValid() else QColor(default)


def overlay_config(app_settings: dict) -> dict:
    """settings.json の presenter_overlay セクション。無ければ空辞書。"""
    return (app_settings or {}).get("presenter_overlay", {}) or {}


# ---------------------------------------------------------------
# レーザーとスポットライトの描画
#
# 窓のクラスから切り出して関数にしてある。画面ミラー(screen_mirror.py)が同じ光点と
# 同じ穴を「ミラー先の窓」に描くため。あちらは対象が違う(こちらは手元の画面に重ね、
# あちらは別モニタへ映した映像の上に描く)が、絵と設定はまったく同じでよい。
# 2箇所に同じ絵を書くと、片方だけ直したときに見た目が食い違う。
#
# 設定の読み取りも *_params として一緒に出してある。色や半径を2つのセクションで
# 設定させる意味は無いので、画面ミラー側もこの presenter_overlay セクションを読む。
# ---------------------------------------------------------------
def laser_params(app_settings: dict) -> dict:
    """draw_laser にそのまま渡せる形で設定を読む。"""
    cfg = overlay_config(app_settings)
    radius = max(_as_int(cfg.get("laser_radius"), DEFAULT_LASER_RADIUS), 1)
    return {
        "color": _as_color(cfg.get("laser_color"), DEFAULT_LASER_COLOR),
        "radius": radius,
        "glow": max(_as_int(cfg.get("laser_glow_radius"), DEFAULT_LASER_GLOW_RADIUS), radius),
        "opacity": _as_float(cfg.get("laser_opacity"), DEFAULT_LASER_OPACITY, 0.0, 1.0),
    }


def spotlight_params(app_settings: dict) -> dict:
    """draw_spotlight にそのまま渡せる形で設定を読む。"""
    cfg = overlay_config(app_settings)
    dim = _as_float(cfg.get("spotlight_dim"), DEFAULT_SPOTLIGHT_DIM, 0.0, 1.0)
    return {
        "radius": max(_as_int(cfg.get("spotlight_radius"), DEFAULT_SPOTLIGHT_RADIUS), 1),
        "feather": max(_as_int(cfg.get("spotlight_feather"), DEFAULT_SPOTLIGHT_FEATHER), 0),
        "dim_alpha": int(round(dim * 255)),
    }


def draw_laser(painter: QPainter, center: QPointF, color: QColor, radius: int,
               glow: int, opacity: float) -> None:
    """光点を1つ描く。芯(はっきりした円)と暈(そのまわりの淡いにじみ)の2枚重ね。

    暈を付けるのは、投影した画面では芯だけだと小さくて見失うため。実物のレーザー
    ポインタも同じ見え方をするので、見る側にとっても素直。

    呼ぶ側で Antialiasing を立てておくこと(ペンとブラシはここで設定する)。"""
    def alpha_color(ratio: float) -> QColor:
        shade = QColor(color)
        shade.setAlphaF(opacity * ratio)
        return shade

    painter.save()
    painter.setPen(Qt.NoPen)
    if glow > radius:
        core_stop = radius / glow
        gradient = QRadialGradient(center, float(glow))
        gradient.setColorAt(0.0, alpha_color(0.45))
        gradient.setColorAt(core_stop, alpha_color(0.30))
        gradient.setColorAt(1.0, alpha_color(0.0))
        painter.setBrush(gradient)
        painter.drawEllipse(center, float(glow), float(glow))

    painter.setBrush(alpha_color(1.0))
    painter.drawEllipse(center, float(radius), float(radius))
    painter.restore()


def draw_spotlight(painter: QPainter, area, center: QPointF, radius: int,
                   feather: int, dim_alpha: int) -> None:
    """area を減光しつつ、center のまわりだけ抜く。

    以前は「全面を減光してから CompositionMode_DestinationOut で穴を開ける」形だった。
    やめたのは、あれが透過窓(下が透けるので、アルファを削れば素通しになる)でしか
    成立しないため。画面ミラーは不透明な窓に映像を描いた上へ重ねるので、アルファを
    削ると窓そのものに穴が開いてしまう。

    今の形は素の重ね塗り(SourceOver)だけで、どちらの窓でも同じ絵になる。中心が透明・
    外周が減光色の放射グラデーションを1本作り、それで area を塗りつぶす。QGradient は
    最後の止めより外を最後の色で埋める(PadSpread)ので、円の外は自動的に減光色になる。
    塗りが1回なので、減光した部分と抜いた部分の境目に継ぎ目も出ない。"""
    painter.save()
    outer = float(radius + feather)
    if feather > 0:
        gradient = QRadialGradient(center, outer)
        gradient.setColorAt(0.0, QColor(0, 0, 0, 0))
        gradient.setColorAt(radius / outer, QColor(0, 0, 0, 0))
        gradient.setColorAt(1.0, QColor(0, 0, 0, dim_alpha))
        painter.fillRect(area, gradient)
    else:
        # feather=0 をグラデーションで表すと、内側の止め位置(radius/outer)が 1.0 に
        # なって最後の止めと同じ位置で重なり、中心から外へだらだら薄くなる帯に化ける
        # (実測: 中心のアルファが0にならず、半径の半分の位置で既に4割方戻る)。
        # 縁をぼかさないなら、矩形と円の2つを持つパスを偶奇規則で塗れば穴が開く。
        path = QPainterPath()
        path.addRect(QRectF(area))
        path.addEllipse(center, outer, outer)
        painter.fillPath(path, QColor(0, 0, 0, dim_alpha))
    painter.restore()


def cursor_screen():
    """カーソルのある画面(QScreen)。分からなければプライマリ、それも無ければ None。

    taskbar_widget._screen_for() は「重なり面積がいちばん広い画面」で選ぶが、あれは
    タスクバーの矩形が画面の端いっぱいに置かれて隣の画面まで数pxはみ出すため。
    こちらが持っているのは点(カーソル)1つなので、面積で比べる相手がおらず screenAt で
    足りる。screenAt が None を返すのは画面の隙間(解像度違いのモニタを並べたときの
    段差)にカーソルが居る場合で、そのときはプライマリへ落とす。"""
    screen = QGuiApplication.screenAt(QCursor.pos())
    return screen or QGuiApplication.primaryScreen()


def _virtual_geometry() -> QRect:
    """全モニタを合算したQRect(論理座標)。target_screen が "all" のときの表示範囲。
    capture_grab.virtual_geometry と同じものだが、あちらを import すると mss まで
    引っ張ることになるのでここで数える(2行で済む)。"""
    geometry = QRect()
    for screen in QGuiApplication.screens():
        geometry = geometry.united(screen.geometry())
    return geometry


def target_geometry(cfg: dict) -> QRect:
    """オーバーレイを出す範囲(論理座標)。設定 target_screen で決める。

    既定はカーソルのある画面1枚。全画面を覆う("all")のは、1画面しか無いPCや、
    どの画面を指してもレーザーを追わせたいときのための選択肢。"""
    if str(cfg.get("target_screen", DEFAULT_TARGET_SCREEN)).lower() == "all":
        return _virtual_geometry()
    screen = cursor_screen()
    return screen.geometry() if screen is not None else _virtual_geometry()


class _Overlay(QWidget):
    """画面に重ねる窓の共通部分。表示範囲の決定・最前面の維持・後始末を持つ。

    closed は「窓の側から閉じた」ことを OverlayController へ知らせるためのもの
    (黒画面のクリックで閉じる経路)。ホットキーやメニューから閉じるときは
    Controller が直接 close() を呼ぶので、この信号は通らない。"""

    closed = Signal()

    # マウスを透過するか。透過する側(レーザー・スポットライト)は自分ではマウスも
    # キーも受け取れないので、閉じる手段はホットキーとトレイメニューだけになる。
    click_through = True

    def __init__(self, app_settings: dict):
        super().__init__()
        self.cfg = overlay_config(app_settings)

        flags = (
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # 発表中に前面を奪わない。スライドを送っているのは下のアプリで、そちらから
            # キーボードの相手を取り上げたら発表が止まる。
            | Qt.WindowDoesNotAcceptFocus
        )
        if self.click_through:
            # WA_TransparentForMouseEvents だけでは、Qtが自分の中でイベントを配らなく
            # なるだけで、Windowsから見れば依然そこに窓がある(クリックは吸われる)。
            # 下のアプリまでクリックとホイールを通すには WS_EX_TRANSPARENT が要り、
            # Qtでそれを立てるのが WindowTransparentForInput。両方付けるのはそのため。
            flags |= Qt.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        # 出した瞬間に前面を奪わない(上の WindowDoesNotAcceptFocus と対で効く)。
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        if self.click_through:
            self.setAttribute(Qt.WA_TransparentForMouseEvents)

        self._origin = QPoint(0, 0)
        self.place_on_target()

        # 最前面の維持。相手(スライドショー等)も最前面を主張するので、出しっぱなしに
        # せず定期的に押し上げ直す。raise_ は並び順を変えるだけで前面化はしない。
        self._topmost_timer = QTimer(self)
        self._topmost_timer.timeout.connect(self._on_topmost)

    # ---------------------------------------------------------------
    # 表示範囲
    # ---------------------------------------------------------------
    def place_on_target(self) -> None:
        """設定に従って表示範囲を決め直す。self._origin はローカル座標との差分。"""
        geometry = target_geometry(self.cfg)
        if geometry.isEmpty():
            # 画面が1枚も取れないことは通常起きないが、空の矩形で setGeometry すると
            # 大きさ0の窓が残るだけになる。最低限の大きさにしておく。
            geometry = QRect(0, 0, 1, 1)
        self._origin = geometry.topLeft()
        self.setGeometry(geometry)

    def to_local(self, point_global: QPoint) -> QPoint:
        """画面座標を窓の中のローカル座標へ直す。窓の左上は目的の画面の左上であり、
        プライマリの左や上にモニタがあると原点が(0,0)にならない
        (capture_overlay._to_global の逆向き)。"""
        return point_global - self._origin

    # ---------------------------------------------------------------
    # 表示・後始末
    # ---------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self._topmost_timer.start(TOPMOST_INTERVAL_MS)

    def hideEvent(self, event):
        # 見えていない窓を押し上げ続けても意味がない。閉じるときも hideEvent は通る。
        self._topmost_timer.stop()
        super().hideEvent(event)

    def _on_topmost(self):
        try:
            if self.isVisible():
                self.raise_()
        except Exception:
            _guard("最前面の維持", notify=False)


class _CursorOverlay(_Overlay):
    """カーソルを追って描くオーバーレイ(レーザー・スポットライト)の共通部分。

    位置は QCursor.pos() のポーリングで取る。マウスを透過させた窓にはイベントが
    来ないため、これ以外に手が無い(DEFAULT_POLL_INTERVAL_MS のコメントを参照)。"""

    # カーソルが別の画面へ移ったら、そちらへ窓ごと移すか。レーザーとスポットライトは
    # 「カーソルの居るところ」を見せる道具なので追う。黒画面(_BlankOverlay)は追わない。
    follow_cursor = True

    def __init__(self, app_settings: dict):
        super().__init__(app_settings)
        self._partial = bool(self.cfg.get("partial_repaint", DEFAULT_PARTIAL_REPAINT))
        # 直前の位置。前回描いた場所を消すために覚えておく(部分描画のとき必要)。
        self._cursor_global = QCursor.pos()
        self._previous_global = self._cursor_global
        # 窓を移した直後などは部分描画では足りないので、次の1回だけ全面を描き直す。
        self._force_full = True

        interval = max(_as_int(self.cfg.get("poll_interval_ms"), DEFAULT_POLL_INTERVAL_MS), 1)
        self._poll_timer = QTimer(self)
        # 16ms前後を狙う可能性があるので明示する。既定(CoarseTimer)はWindowsで
        # 数msから十数ms単位に丸められ、光点の動きが目に見えて跳ねる。
        self._poll_timer.setTimerType(Qt.PreciseTimer)
        self._poll_timer.setInterval(interval)
        self._poll_timer.timeout.connect(self._on_poll)

    def showEvent(self, event):
        super().showEvent(event)
        self._poll_timer.start()

    def hideEvent(self, event):
        self._poll_timer.stop()
        super().hideEvent(event)

    def cursor_local(self) -> QPoint:
        return self.to_local(self._cursor_global)

    def dirty_radius(self) -> int:
        """描画がカーソルからどれだけ外へ広がるか(論理px)。部分描画の範囲に使う。"""
        return 0

    def _on_poll(self):
        """周期の本体。ここは必ず try で受けること(PySide6はスロットから例外が
        投げ切られるとプロセスごと終わる)。"""
        try:
            position = QCursor.pos()
            if self.follow_cursor and self._move_to_cursor_screen(position):
                # 窓ごと別の画面へ移した。移動後の窓は中身が空なので全面を描き直す。
                self._force_full = True

            if position == self._cursor_global and not self._force_full:
                # 動いていないなら描き直さない。発表者が話している間(カーソルを
                # 止めている間)は、周期が来ても何もしないでいられる。
                return

            self._previous_global = self._cursor_global
            self._cursor_global = position
            self._request_repaint()
        except Exception:
            _guard("カーソルの追従", notify=False)

    def _move_to_cursor_screen(self, position: QPoint) -> bool:
        """カーソルが今の表示範囲の外(＝別の画面)へ出ていたら、窓を移す。移したら True。

        target_screen が "all" のときは全画面を覆っているので出番が無い
        (isVisible な範囲から出ようがない)。"""
        if self.geometry().contains(position):
            return False
        screen = QGuiApplication.screenAt(position)
        if screen is None:
            return False
        if screen.geometry() == self.geometry():
            return False
        self._origin = screen.geometry().topLeft()
        self.setGeometry(screen.geometry())
        return True

    def _request_repaint(self) -> None:
        """動いた前後の周りだけを描き直す。部分描画が無効なら全面。

        全面の描き直しは実測でスポットライトが2.93ms/回(2560x1440)。60fpsで回すと
        1コアの2割近くを使い続けることになるので、既定では動いた矩形の和だけに絞る
        (同じ条件で0.51ms)。数字の出どころは DEFAULT_POLL_INTERVAL_MS のコメント。"""
        if self._force_full or not self._partial:
            self._force_full = False
            self.update()
            return
        margin = self.dirty_radius() + 2  # 縁のアンチエイリアス1px分の余裕
        previous = QRect(self.to_local(self._previous_global), self.to_local(self._previous_global))
        current = QRect(self.to_local(self._cursor_global), self.to_local(self._cursor_global))
        self.update(previous.united(current).adjusted(-margin, -margin, margin, margin))


class LaserOverlay(_CursorOverlay):
    """マウス位置に赤い光点を描くだけの窓。下のアプリはそのまま操作できる。"""

    def __init__(self, app_settings: dict):
        super().__init__(app_settings)
        # 絵と設定は draw_laser / laser_params に置いてある(画面ミラーと共有するため)。
        self._laser = laser_params(app_settings)

    def dirty_radius(self) -> int:
        return self._laser["glow"]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        draw_laser(painter, QPointF(self.cursor_local()), **self._laser)


class SpotlightOverlay(_CursorOverlay):
    """マウス周辺だけを残して周囲を暗くする窓。こちらも下のアプリは操作できる。"""

    def __init__(self, app_settings: dict):
        super().__init__(app_settings)
        # 絵と設定は draw_spotlight / spotlight_params に置いてある(画面ミラーと共有)。
        self._spot = spotlight_params(app_settings)

    def dirty_radius(self) -> int:
        return self._spot["radius"] + self._spot["feather"]

    def paintEvent(self, event):
        """塗るのは self.rect() ではなく event.rect()。部分描画で呼ばれたときに全面を
        塗り直さないため(触っていない場所は前回描いたものがそのまま残る)。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        draw_spotlight(painter, event.rect(), QPointF(self.cursor_local()), **self._spot)


class BlankOverlay(_Overlay):
    """画面全体を黒(または白)で覆う窓。話に集中させたいときに使う。

    こちらはマウスを透過しない。覆ってしまえば下のアプリは見えず、どのみち狙って
    操作できないので、クリックを吸っても失うものが無い。むしろ「透過すると閉じる手段が
    無くなる」ほうが問題で、クリック一発で解除できる出口を用意する価値のほうが大きい
    (ホットキーとトレイメニューは覆っている間も効くが、覆われて驚いた人が最初に
    するのはクリックのはず)。

    ただし前面は奪わない。WindowDoesNotAcceptFocus(＝WS_EX_NOACTIVATE)を付けてあるので、
    クリックしてもこの窓はアクティブにならず、下のスライドショーがキーボードの相手の
    ままでいられる。解除したあとそのまま矢印キーでスライドを送れる。"""

    click_through = False

    def __init__(self, app_settings: dict, kind: str = "black"):
        super().__init__(app_settings)
        self.kind = kind
        default = DEFAULT_BLANK_WHITE if kind == "white" else DEFAULT_BLANK_BLACK
        key = "blank_white_color" if kind == "white" else "blank_black_color"
        self._color = _as_color(self.cfg.get(key), default)
        self._click_to_close = bool(
            self.cfg.get("blank_click_to_close", DEFAULT_BLANK_CLICK_TO_CLOSE)
        )
        # 透けると意味が無いので、translucent は付いていても常に不透明で塗る。
        self._color.setAlpha(255)

    def mousePressEvent(self, event):
        try:
            if self._click_to_close:
                self.closed.emit()
            else:
                super().mousePressEvent(event)
        except Exception:
            _guard("黒画面の解除", notify=False)

    def keyPressEvent(self, event):
        """Escでも解除する。上記のとおりこの窓は前面にならないので普段ここへは
        キーが来ないが、環境によっては来ることもある(来たら受ける、という保険)。"""
        try:
            if event.key() == Qt.Key_Escape:
                self.closed.emit()
                return
        except Exception:
            _guard("黒画面の解除", notify=False)
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), self._color)


# kind から窓を作る表。OverlayController から引く。
_FACTORIES = {
    "laser": lambda app_settings: LaserOverlay(app_settings),
    "spotlight": lambda app_settings: SpotlightOverlay(app_settings),
    "black": lambda app_settings: BlankOverlay(app_settings, "black"),
    "white": lambda app_settings: BlankOverlay(app_settings, "white"),
}


class OverlayController:
    """4つのオーバーレイの参照と排他関係を1か所で持つ。

    参照をここで抱えるのは、開いた窓の参照を持たないとGCで即消えるため(このアプリで
    何度も踏んでいる)。ScreenFeature がこれを1つだけ属性で持つ。

    どのメソッドも例外を投げない。呼び元はメニューのスロットとホットキーで、どちらも
    投げ切ると常駐ごと終わる場所だから。"""

    def __init__(self, app_settings: dict):
        self.app_settings = app_settings
        self._overlays = {}

    def is_active(self, kind: str) -> bool:
        return kind in self._overlays

    def toggle(self, kind: str) -> bool:
        """切り替えて、切り替えた後に出ているかどうかを返す(通知とメニューの
        チェック状態に使う)。"""
        if self.is_active(kind):
            self.close(kind)
            return False
        self.open(kind)
        return self.is_active(kind)

    def open(self, kind: str) -> bool:
        factory = _FACTORIES.get(kind)
        if factory is None or self.is_active(kind):
            return self.is_active(kind)
        for other in EXCLUSIVE_KINDS.get(kind, ()):
            self.close(other)
        try:
            overlay = factory(self.app_settings)
            # 窓の側から閉じたとき(黒画面のクリック)に、こちらの参照も片付ける。
            # 既定引数で kind を束縛する(ループではないが、後から kind を変えられても
            # 巻き込まれないようにしておく)。
            overlay.closed.connect(lambda k=kind: self._on_self_closed(k))
            overlay.show()
        except Exception:
            _guard(f"{KIND_LABELS.get(kind, kind)}の表示")
            return False
        self._overlays[kind] = overlay
        return True

    def close(self, kind: str) -> None:
        overlay = self._overlays.pop(kind, None)
        if overlay is None:
            return
        try:
            overlay.close()
            overlay.deleteLater()
        except RuntimeError:
            # C++側が先に消えている場合(deleteLater のあとにもう一度来た等)。
            # 参照はもう外したので、ここは何もしなくてよい。
            pass
        except Exception:
            _guard(f"{KIND_LABELS.get(kind, kind)}を閉じる処理", notify=False)

    def close_all(self) -> None:
        """終了時の後始末。枠なし・最前面の窓を残したままイベントループを畳むと、
        画面に貼り付いたまま消えないことがある(taskbar_widget と同じ理由)。"""
        for kind in list(self._overlays):
            self.close(kind)

    def _on_self_closed(self, kind: str) -> None:
        try:
            self.close(kind)
        except Exception:
            _guard("オーバーレイの後始末", notify=False)
