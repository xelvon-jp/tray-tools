# taskbar_widget.py
# 各ディスプレイのタスクバーに置く「通知領域の代わり」の小さなウィジェット。
#
# Windowsは通知領域(トレイ)をプライマリのタスクバーにしか出さない。ノートPCの画面を
# プライマリ、正面の拡張ディスプレイをセカンダリにしている構成では、tray-tools を使う
# たびに視線と手が正面ではないノート側へ行ってしまう。そこで、タスクバーに
# トレイの代わりを自分で置く。
#
# 既定では「すべてのディスプレイ」に1つずつ出す(all_displays)。プライマリには本物の
# 通知領域があるので重複ではあるが、どの画面でも同じ場所に同じUIがあるほうが操作を
# 覚え直さずに済む。位置は「そのタスクバーの右端からのオフセット」として全画面で
# 共通に持つので、1つをドラッグすれば全部が同じ位置へそろって動く。
#
# 見た目はタスクバーの時計そのもの(既存の時計に重ねて隠す)で、マウスを乗せた間だけ
# Rapture と音声のアイコンに入れ替わる。隣に生やさないのは、タスクバーの上に自前の
# ものが常時2つ増えて見えるのが邪魔だからで、「時計の場所に用がある」わけではない。
#
# Featureではない(トレイアイコンを持たない)。main.py 冒頭の方針どおり、アイコンを
# 増やさずに ScreenFeature が生成・保持する(複数出すのでリストで持つ)。
import json
import locale
import sys
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

import window_tools
from capture_grab import device_bounds_to_logical, grab_region
from toast import show_toast

ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"

# 位置は「そのタスクバーの端からの距離(論理px)」で持つ。ディスプレイごとの絶対座標に
# しないのは、どの画面でも同じ場所に同じものがあってほしいから(操作性の統一が目的で、
# 画面ごとに置き場所が違うと結局どこにあるか探すことになる)。
# 実測値: 2560x48 のタスクバーで、Windows 11 の時計の描画範囲は x 2481〜2540 /
# y はタスクバー上端から6px下。この2つを margin の既定にすると、実測位置がそのまま
# 再現できる(2560 - 20 - 59 = 2481、-48 + 6 = -42)。
DEFAULT_RIGHT_MARGIN = 20
DEFAULT_TOP_MARGIN = 6

# ディスプレイ構成が変わってから作り直すまでの待ち(ms)。screenAdded/screenRemoved が
# 飛んだ時点ではWindowsがまだタスクバーを並べ直しておらず、すぐ測ると古い位置を拾う。
REBUILD_DELAY_MS = 1000

# タスクバーは常に最前面なので、その上に居続けるには押し上げ続けるしかない。
TOPMOST_INTERVAL_MS = 500
# 分単位の表示でも毎秒描き直す。秒がずれた時計は見た目ですぐ分かるので気持ち悪く、
# 59x31 の再描画1回の負荷は無視できる。
CLOCK_INTERVAL_MS = 1000

# アイコンを出している間だけ、音声デバイスの状態を読み直す間隔(ms)。ホットキーや
# 通知領域から切り替えられると、こちらが持っている絵は古いままになるため。
# COM越しの問い合わせが入るので、時計の更新(毎秒)には相乗りさせず、専用のタイマーで
# 「アイコンが見えている間だけ」に限る(enterEvent で開始・leaveEvent で停止)。
AUDIO_POLL_INTERVAL_MS = 2000

# 背景色を測り直すとき、自分を隠してから撮るまでの待ち(ms)。hide() した直後はOSがまだ
# 下を描き直しておらず、自分の色をもう一度測ってしまう(キャプチャのカウントダウンを
# 閉じてから撮るまでに置いている間合いと同じ理由・同じ値)。
REDRAW_WAIT_MS = 150

# 背景を測れなかったときの色。Windows 11 のダークなタスクバーに一番近い無難な値。
FALLBACK_BACKGROUND = "#202020"

# 文字色を自動で決めるときの明るさのしきい値(0〜255)。タスクバーの透明効果で壁紙が
# 透けるため、背景は明るくも暗くもなりうる。白固定だと明るい壁紙で読めなくなる。
BRIGHTNESS_THRESHOLD = 140
LIGHT_TEXT = "#ffffff"
DARK_TEXT = "#000000"

FONT_FAMILY = "Meiryo"
# 2行を上下半分ずつに割り付けるので、行の高さは height/2。そこから余白分を引く。
FONT_PADDING = 3
FONT_PIXEL_SIZE_MAX = 12
FONT_PIXEL_SIZE_MIN = 7

# 幅を文字から決めるときに、文字の左右へ空ける余白(px)。
TEXT_PADDING = 4

# アイコンの周囲に空ける余白(px)。
ICON_MARGIN = 3
ICON_SIZE_MIN = 8

# これより小さくは作らない(論理px)。0や負の値を設定に書かれても壊れないように。
MIN_SIZE = 8

DEFAULT_FORMAT_TOP = "%m/%d(%a)"
DEFAULT_FORMAT_BOTTOM = "%H:%M:%S"

# ロケール設定は1回で足りる。プロセス全体に効く操作なので、実際に日時を使うまで遅らせる
# (snippets._ensure_time_locale と同じ作法)。
_locale_ready = False


def _ensure_time_locale() -> None:
    """%a(曜日)などを日本語で出すため、環境のロケールに合わせる。
    失敗しても時計の表示自体は続けられる(英語表記になるだけ)ので握りつぶす。"""
    global _locale_ready
    if _locale_ready:
        return
    _locale_ready = True
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass


def _guard(where: str, notify: bool = True) -> None:
    """スロットの中で起きた例外をここで止め、標準エラー(と必要なら通知)へ回す。

    PySide6 はスロットから例外が投げ切られるとプロセスごと終了する。このウィジェットは
    タイマーとマウスイベントで動き続けるため、1回の失敗で常駐アプリが消えてしまう。

    main.log_exception と同じ役目だが、import が main → feature_screen → ここ の
    一方通行なので、あちらを呼ぶと循環参照になる。

    notify=False は周期タイマー用。毎周期トーストを出すと画面が埋まる。"""
    traceback.print_exc()
    print(f"[tray-tools] タスクバーウィジェット: {where}に失敗しました", file=sys.stderr)
    if notify:
        show_toast(f"タスクバーウィジェット\n{where}に失敗しました")


def save_config(app_settings: dict, settings_path, **values) -> None:
    """settings.json を読み直し、taskbar_widget の指定キーだけを書き戻す。

    メモリ上の設定はデフォルト値をマージ済みで、丸ごと書き出すと未設定の既定値まで
    明示的に書かれてファイルの姿が変わってしまう(feature_audio._save_device_identity と
    同じ理由)。書けなくても動作は続ける(次に保存できたときに揃う)。

    ウィジェットが未生成でも呼べるようモジュール関数にしてある。表示のON/OFFを保存する
    のは ScreenFeature 側で、OFFにするときにはウィジェットが無いこともある。"""
    app_settings.setdefault("taskbar_widget", {}).update(values)
    if not settings_path:
        return
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        stored.setdefault("taskbar_widget", {}).update(values)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(stored, f, ensure_ascii=False, indent=2)
    except (OSError, ValueError, TypeError, AttributeError) as e:
        print(f"[tray-tools] タスクバーウィジェットの設定を保存できません: {e}", file=sys.stderr)


def taskbar_rects(include_primary: bool = True) -> list:
    """ウィジェットを置くタスクバーの矩形(Qt論理座標)を並べて返す。無ければ空リスト。

    Win32が返すのは物理ピクセルなので、必ず device_bounds_to_logical を通す
    (このPCは dpr=1.0 だが、拡大率を変えた環境ではそのまま使うと位置がずれる)。

    大きさ0の矩形は捨てる。タスクバーが自動的に隠れる設定などで潰れた矩形が返ることが
    あり、そこへ出しても何も見えないため。"""
    rects = []
    for bounds in window_tools.get_taskbar_bounds(include_primary=include_primary):
        rect = device_bounds_to_logical(bounds)
        if not rect.isEmpty():
            rects.append(rect)
    return rects


def _taskbar_at(point: QPoint):
    """その点を含むタスクバーの矩形(Qt論理座標)。無ければ None。

    ドラッグで動かしたあと「どのタスクバーからのオフセットか」を決めるのに使う。
    all_displays の設定に関わらずプライマリも含めて探す。設定でプライマリに出していなくても、
    ドラッグしてそちらへ持っていくことはできるため。"""
    for rect in taskbar_rects(include_primary=True):
        if rect.contains(point):
            return rect
    return None


def _auto_top_left(taskbar: QRect, width: int, height: int,
                   right_margin: int, top_margin: int) -> QPoint:
    """タスクバーの矩形と余白から、時計に重なる位置を割り出す。

    QRect.right() は「最後のピクセル」を指す(幅は right - left + 1)。タスクバーの
    排他的な右端は right() + 1 なので、そこから余白と自分の幅を引く。
    縦は上端から top_margin。ただしタスクバーが実測より薄い環境でははみ出すので、
    その場合だけ中央寄せに落とす。"""
    x = taskbar.right() + 1 - right_margin - width
    if taskbar.height() >= top_margin + height:
        y = taskbar.top() + top_margin
    else:
        y = taskbar.top() + max((taskbar.height() - height) // 2, 0)
    return QPoint(x, y)


def _as_int(value, default: int) -> int:
    """設定値を整数にする。数として読めない値が書かれていたら既定に落とす。
    設定は手で書き換えられるので、書き損じでウィジェットが1つも出ないのは避ける。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _auto_text_color(background: QColor) -> str:
    """背景の明るさから文字色を決める。ITU-R BT.601 の輝度で見る。

    タスクバーの透明効果で壁紙が透けるため、背景は環境と壁紙次第で明るくも暗くもなる
    (このPCの実測は #DDF2B9 で、白文字ではまったく読めない)。設定の text_color を
    書けばこの判定より優先される。"""
    brightness = (
        background.red() * 299 + background.green() * 587 + background.blue() * 114
    ) / 1000
    return DARK_TEXT if brightness >= BRIGHTNESS_THRESHOLD else LIGHT_TEXT


def _dominant_color(rect: QRect) -> str:
    """rect(Qt論理座標)の位置の画面を撮って、いちばん多い色を #rrggbb で返す。

    必ず「自分を表示する前」に呼ぶこと。表示してから撮ると自分の背景色を測ることになる。

    平均ではなく最頻色にするのは、時計の文字(白や黒)が混ざった平均だと本来の地の色から
    ずれるため。文字はごく一部なので、最頻色なら地の色が残る。

    撮影は mss 越しで、環境によっては OSError 以外も飛んでくる(mss 独自の例外)。
    背景色が測れないことは「既定色で出す」で済む話なので、種類を問わず受け止める。"""
    try:
        image = grab_region(rect)
    except Exception:
        _guard("背景色の取得", notify=False)
        return FALLBACK_BACKGROUND
    if image.isNull() or image.width() == 0 or image.height() == 0:
        return FALLBACK_BACKGROUND

    counter = Counter()
    for y in range(image.height()):
        for x in range(image.width()):
            counter[image.pixel(x, y)] += 1
    value, _count = counter.most_common(1)[0]
    return QColor.fromRgb(value).name()


class TaskbarWidget(QWidget):
    """タスクバーの時計に重ねる、枠なし・不透明の小さな窓。1つのタスクバーにつき1つ。

    普段は時計を描き、マウスを乗せている間だけ Rapture と音声のアイコンに入れ替わる。
    クリックの割り当ては通知領域のアイコンに合わせてある(Rapture=中クリックで即キャプチャ・
    右クリックでメニュー / 音声=左クリックで切替・右クリックでメニュー)。

    自分が乗るタスクバーの矩形は生成時に受け取る。矩形はディスプレイ構成が変われば
    変わるが、追従は作り直し(ScreenFeature)に任せる。動いたタスクバーを掴み直す仕組みを
    ここに持たせても、画面が増減したときには結局作り直しが要るため。

    WA_TranslucentBackground は使わない。下にある本物の時計を隠すのが仕事なので、
    背景は必ず不透明に塗る。"""

    def __init__(self, app_settings: dict, settings_path, screen_feature, audio_feature,
                 taskbar: QRect):
        super().__init__()
        self.app_settings = app_settings
        self.settings_path = settings_path
        self._taskbar = QRect(taskbar)
        # ScreenFeature / AudioFeature の参照をそのまま持つ。アイコンの絵だけでなく
        # start_capture・do_toggle・既存の self.menu も呼ぶ必要があり、絵を返す口だけ
        # 足しても足りないため(メニューは別に作らず、通知領域と同じものを出す)。
        self._screen = screen_feature
        self._audio = audio_feature

        self._config = app_settings.setdefault("taskbar_widget", {})
        self._hover = False
        # メニューを出している間だけ True。カーソルがメニューへ移ると leaveEvent が
        # 飛んでくるが、その間もアイコンを出したままにするために使う。
        self._menu_open = False
        self._drag_offset = None
        self._audio_pixmap = None
        self._format_warned = False

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            # タスクバーを触ったつもりでここを押しても、作業中のウィンドウから
            # フォーカスを奪わない。マウスイベント自体は通常どおり届く
            # (WA_TransparentForMouseEvents は付けない。付けるとクリックできなくなる)。
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setToolTip("tray-tools（マウスを乗せるとアイコンが出ます / Ctrl+ドラッグで移動）")

        self._rapture_pixmap = QPixmap(str(ICON_PATH)) if ICON_PATH.exists() else QPixmap()

        self._background = QColor(FALLBACK_BACKGROUND)
        self._text_color = QColor(LIGHT_TEXT)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(CLOCK_INTERVAL_MS)
        self._clock_timer.timeout.connect(self._on_clock_tick)

        # タスクバーは常に最前面なので、押し上げ続けないとすぐ裏へ回る。
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(TOPMOST_INTERVAL_MS)
        self._topmost_timer.timeout.connect(self._on_topmost_tick)

        # アイコンを出している間だけ動かす(enterEvent で開始・leaveEvent で停止)。
        self._audio_timer = QTimer(self)
        self._audio_timer.setInterval(AUDIO_POLL_INTERVAL_MS)
        self._audio_timer.timeout.connect(self._on_audio_tick)

    # ---------------------------------------------------------------
    # 表示・非表示
    # ---------------------------------------------------------------
    def start(self) -> bool:
        """位置を決めて表示する。出せる場所が無ければ False を返す(表示しない)。"""
        rect = self._resolve_geometry()
        if rect is None:
            return False
        self.setGeometry(rect)
        self._apply_colors(measure=self._config.get("background_color") in (None, ""))
        self.show()
        return True

    def stop(self) -> None:
        self.hide()

    def reposition(self) -> None:
        """設定の余白から位置を計算し直す。

        どれか1つをドラッグすると全ウィジェットに対して呼ばれる(位置は画面ごとではなく
        「タスクバーの右端からのオフセット」1組で持つため)。背景色は測り直さない。
        ここで毎回測ると、動かすたびに全画面のキャプチャが走る。壁紙に合わせたいときは
        メニューの「背景色を取り直す」で明示的にやる、という既存の切り分けに合わせる。"""
        rect = self._resolve_geometry()
        if rect is not None:
            self.setGeometry(rect)

    def refresh_background(self) -> None:
        """背景色をいま画面にある色で測り直す(壁紙を変えたとき用)。

        自分が乗っている場所を測るので、いったん隠してから撮る。hide() の直後はOSが
        まだ下を描き直していないため、少し待ってから測る。"""
        if not self.isVisible():
            return
        self.hide()
        QTimer.singleShot(REDRAW_WAIT_MS, self._finish_refresh_background)

    def _finish_refresh_background(self):
        """測り直した色は settings.json に書かない。

        background_color を空のままにしておけば起動のたびに実測されるので、壁紙が
        変わっても放っておいて合う。ここで書き込むと以後その色に固定され、次に壁紙を
        変えたときにまた手で取り直す羽目になる(色を固定したい人は自分で書く)。"""
        try:
            self._apply_colors(measure=True)
            self.show()
        except Exception:
            _guard("背景色の取り直し")
            self.show()  # 測り直しに失敗しても消えたままにはしない

    def _apply_colors(self, measure: bool) -> None:
        """背景色と文字色を決めて保持する。measure=True のときだけ画面を実測する。"""
        configured = self._config.get("background_color")
        if measure or not configured:
            background = QColor(_dominant_color(self.geometry()))
        else:
            background = QColor(configured)
        if not background.isValid():
            background = QColor(FALLBACK_BACKGROUND)
        self._background = background

        text = self._config.get("text_color")
        color = QColor(text) if text else QColor(_auto_text_color(background))
        self._text_color = color if color.isValid() else QColor(_auto_text_color(background))
        self.update()

    def _margins(self):
        """(右端からの余白, 上端からの余白)。全ウィジェットで共通の1組。"""
        return (
            _as_int(self._config.get("right_margin"), DEFAULT_RIGHT_MARGIN),
            _as_int(self._config.get("top_margin"), DEFAULT_TOP_MARGIN),
        )

    def _resolve_geometry(self):
        """表示すべき矩形(Qt論理座標)。出せる場所が無ければ None。

        自分が乗るタスクバーの矩形と、設定の余白(右端・上端からの距離)だけで決まる。
        ディスプレイごとの絶対座標は持たない(どの画面でも右端から同じ距離に置きたい)。

        作業領域(availableGeometry)へクランプしないのは、この窓の居場所であるタスクバーの
        上がそもそも作業領域から除外されているためで、クランプすると必ずタスクバーの外へ
        弾き出されてしまう。"""
        if self._taskbar.isEmpty():
            return None

        height = max(int(self._config.get("height") or 0), MIN_SIZE)
        # 幅は書式で変わる(日付＋曜日と秒付きの時刻では倍近く違う)ので、設定に無ければ
        # 実際に描く文字を測って決める。固定値だと書式を変えたときに端が切れる。
        configured = self._config.get("width")
        width = max(int(configured), MIN_SIZE) if configured else self._measure_width(height)

        right_margin, top_margin = self._margins()
        top_left = _auto_top_left(self._taskbar, width, height, right_margin, top_margin)
        return QRect(top_left.x(), top_left.y(), width, height)

    def showEvent(self, event):
        super().showEvent(event)
        self._clock_timer.start()
        self._topmost_timer.start()
        self._on_topmost_tick()  # 出した直後にタスクバーの上へ回す(500ms待たせない)

    def hideEvent(self, event):
        # 見えていない間はどのタイマーも無駄でしかない。特に最前面への押し上げは
        # 他のウィンドウのZオーダーを触る操作なので、止めておく。
        self._clock_timer.stop()
        self._topmost_timer.stop()
        self._audio_timer.stop()
        super().hideEvent(event)

    # ---------------------------------------------------------------
    # タイマー
    # ---------------------------------------------------------------
    def _on_clock_tick(self):
        try:
            if not self._hover:
                self.update()
        except Exception:
            _guard("時計の更新", notify=False)

    def _on_topmost_tick(self):
        try:
            # winId() はネイティブハンドルを必要なら作ってから返す。
            # ctypes へ渡すので int にしておく(HWNDは c_void_p で受ける側の約束)。
            window_tools.push_topmost(int(self.winId()))
        except Exception:
            _guard("最前面への押し上げ", notify=False)

    def _on_audio_tick(self):
        try:
            self._sync_audio_icon(refresh=True)
        except Exception:
            _guard("音声アイコンの更新", notify=False)

    def _sync_audio_icon(self, refresh: bool) -> None:
        """音声アイコンの絵を取り直し、変わっていれば描き直す。

        refresh=True は AudioFeature に状態を読み直させる(COM越しの問い合わせが入る)。
        自分のクリックで切り替えた直後は、do_toggle が中で必ず状態を更新しているので
        refresh=False で足りる(クールダウンで何も起きなかった場合も、絵は同じまま)。

        同じ QPixmap がそのまま返ってきたときは描き直さない。読み直しても状態が
        変わっていなければ、AudioFeature は前と同じオブジェクトを返す。"""
        pixmap = self._audio.current_icon_pixmap(refresh=refresh)
        if pixmap is self._audio_pixmap:
            return
        self._audio_pixmap = pixmap
        self.update()

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def _zones(self):
        """(Raptureの当たり判定, 音声の当たり判定)。左右half分けにする。

        アイコンの絵の矩形そのものを当たり判定にすると、59x31 の中に20px級の絵が2つ
        並ぶだけなので周りが死に領域になり、「押したのに何も起きない」が頻発する。"""
        half = self.width() // 2
        return (
            QRect(0, 0, half, self.height()),
            QRect(half, 0, self.width() - half, self.height()),
        )

    def _icon_rect(self, zone: QRect) -> QRect:
        size = max(min(zone.width(), zone.height()) - ICON_MARGIN * 2, ICON_SIZE_MIN)
        return QRect(
            zone.x() + (zone.width() - size) // 2,
            zone.y() + (zone.height() - size) // 2,
            size,
            size,
        )

    def _clock_lines(self):
        _ensure_time_locale()
        now = datetime.now()
        return (
            self._strftime(now, self._config.get("clock_format_top"), DEFAULT_FORMAT_TOP),
            self._strftime(now, self._config.get("clock_format_bottom"), DEFAULT_FORMAT_BOTTOM),
        )

    def _strftime(self, now: datetime, fmt, default: str) -> str:
        """Windowsのstrftimeは未知の書式指定子で例外になる(先頭ゼロ落としは %#H で、
        %-H はLinux系のもの)。設定を書き損じても時計が消えないよう既定へ落とす。
        警告は1回だけ出す(毎秒呼ばれるため)。"""
        try:
            return now.strftime(fmt or default)
        except ValueError as e:
            if not self._format_warned:
                self._format_warned = True
                print(f"[tray-tools] 日時書式が不正です ({fmt}): {e}", file=sys.stderr)
            return now.strftime(default)

    def paintEvent(self, event):
        painter = QPainter(self)
        # 下にある本物の時計を隠すのが仕事なので、まず不透明に塗り潰す。
        painter.fillRect(self.rect(), self._background)

        if self._hover:
            self._paint_icons(painter)
        else:
            self._paint_clock(painter)

    def _paint_icons(self, painter: QPainter):
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        rapture_zone, audio_zone = self._zones()
        if not self._rapture_pixmap.isNull():
            painter.drawPixmap(self._icon_rect(rapture_zone), self._rapture_pixmap)
        if self._audio_pixmap is not None and not self._audio_pixmap.isNull():
            painter.drawPixmap(self._icon_rect(audio_zone), self._audio_pixmap)

    def _clock_font(self, height: int) -> QFont:
        """枠の高さに収まる時計のフォント。描画と幅の実測で同じものを使う。

        ポイント指定はDPI次第で高さが変わり、31pxの枠に収まる保証が無い。
        枠の高さから決めた値をそのままピクセルで指定する。"""
        half = height // 2
        size = max(min(half - FONT_PADDING, FONT_PIXEL_SIZE_MAX), FONT_PIXEL_SIZE_MIN)
        font = QFont(FONT_FAMILY)
        font.setPixelSize(size)
        return font

    def _measure_width(self, height: int) -> int:
        """いまの書式で2行が収まる幅。長いほうの行に合わせる。"""
        metrics = QFontMetrics(self._clock_font(height))
        top_text, bottom_text = self._clock_lines()
        longest = max(
            metrics.horizontalAdvance(top_text), metrics.horizontalAdvance(bottom_text)
        )
        return longest + TEXT_PADDING * 2

    def _paint_clock(self, painter: QPainter):
        top_text, bottom_text = self._clock_lines()
        half = self.height() // 2
        painter.setFont(self._clock_font(self.height()))
        painter.setPen(self._text_color)
        painter.drawText(QRect(0, 0, self.width(), half), Qt.AlignCenter, top_text)
        painter.drawText(
            QRect(0, half, self.width(), self.height() - half), Qt.AlignCenter, bottom_text
        )

    # ---------------------------------------------------------------
    # マウス
    # ---------------------------------------------------------------
    def enterEvent(self, event):
        try:
            self._hover = True
            # デバイスは他アプリ(Teams等)からも変えられる。乗せた瞬間に読み直して、
            # 通知領域のアイコンと必ず同じ絵にする(描画のたびには読み直さない)。
            self._sync_audio_icon(refresh=True)
            # 乗せている間はホットキーや通知領域からの切り替えにも追従させる。
            self._audio_timer.start()
            self.update()
        except Exception:
            _guard("アイコン表示への切り替え")

    def leaveEvent(self, event):
        try:
            # メニューを出すとカーソルがそちらへ移ってここへ来るが、その間もアイコンを
            # 出したままにする(メニューを開いた瞬間に時計へ戻るのが見た目に落ち着かない)。
            # 閉じたあとの戻し判定は _popup がまとめて行う。
            if self._menu_open:
                return
            self._hover = False
            self._audio_timer.stop()
            self.update()
        except Exception:
            _guard("時計表示への切り替え")

    def mousePressEvent(self, event):
        try:
            # Ctrl+左ドラッグだけを移動にする。素の左ドラッグは音声の切替(クリック)判定に
            # 使うため、そちらと取り合いにならないようにしている(付箋のペンと同じ作法)。
            if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
                # トップレベルの pos() は枠込みの左上を返す。中身の位置は geometry() 側。
                self._drag_offset = (
                    event.globalPosition().toPoint() - self.geometry().topLeft()
                )
                return

            rapture_zone, _audio_zone = self._zones()
            # アイコンが出ていないとき(時計として見えているとき)は、画面側のメニューを出す。
            on_audio = self._hover and not rapture_zone.contains(event.position().toPoint())
            if on_audio:
                if event.button() == Qt.LeftButton:
                    self._audio.do_toggle()
                    # 切り替えた絵をその場で反映する。読み直さないと、マウスを乗せた
                    # ときに読んだ古い絵のまま(離して乗せ直すまで直らない)。
                    self._sync_audio_icon(refresh=False)
                elif event.button() == Qt.RightButton:
                    self._popup(self._audio.menu)
                return
            if event.button() == Qt.MiddleButton:
                self._screen.start_capture(0)
            elif event.button() == Qt.RightButton:
                self._popup(self._screen.menu)
        except Exception:
            _guard("クリックの処理")

    def mouseMoveEvent(self, event):
        try:
            if self._drag_offset is None:
                return
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        except Exception:
            _guard("ドラッグ移動")

    def mouseReleaseEvent(self, event):
        try:
            if self._drag_offset is None or event.button() != Qt.LeftButton:
                return
            self._drag_offset = None
            self._save_offset()
        except Exception:
            _guard("位置の保存")

    def _save_offset(self) -> None:
        """いまの位置を「タスクバーの端からの距離」に直して保存し、全ウィジェットへ配る。

        絶対座標ではなく余白で持つので、1つ動かすと全画面のウィジェットが同じ場所へ
        そろって動く(どの画面でも同じ位置にある、が狙い)。

        基準は「いま自分が乗っているタスクバー」。画面をまたいでドラッグされたときに
        生成時のタスクバーを基準にすると、画面1つ分ずれた余白が保存されてしまう。
        どのタスクバーにも乗っていない位置(タスクバーの外)へ置かれたときは、生成時の
        ものを基準にする(その位置なりの余白として素直に保存される)。"""
        rect = self.geometry()
        taskbar = _taskbar_at(rect.center()) or self._taskbar
        self._screen.apply_taskbar_widget_offset(
            taskbar.right() + 1 - (rect.x() + rect.width()),
            rect.y() - taskbar.top(),
        )

    def _popup(self, menu) -> None:
        """通知領域と同じメニューをカーソル位置に出す。

        メニューは新しく作らず、それぞれのFeatureが持っているものをそのまま使う。
        別に組み立てると、項目やホットキー表記が片方だけ古くなる。

        開いている間は「最前面への押し上げを止める」「アイコン表示を維持する」の2つを
        面倒みる。どちらもメニューの開閉に紐づく話なので1か所にまとめる。
        - 押し上げ: exec() はメニューを閉じるまで戻らずその間もタイマーは動き続けるので、
          止めないと0.5秒ごとに自分がメニューの上へ乗り上げて、カーソル直下の項目を
          隠してしまう(メニューも最前面のため)。
        - アイコン: カーソルがメニューへ移ると leaveEvent が飛んでくる。素直に従うと
          メニューを出した瞬間に時計へ戻ってしまう。

        exec() の中で例外が出てもフラグが立ちっぱなしにならないよう finally で戻す
        (立ったままだと leaveEvent が効かなくなり、以後ずっとアイコンのままになる)。"""
        if menu is None:
            return
        self._topmost_timer.stop()
        self._menu_open = True
        try:
            menu.exec(QCursor.pos())
        finally:
            self._menu_open = False
            # 閉じた時点でカーソルが載っていなければ時計へ戻す。メニューの外を
            # クリックして閉じた場合など、離れたまま閉じることのほうが多い。
            self._hover = self._cursor_inside()
            if not self._hover:
                self._audio_timer.stop()
            self.update()
            if self.isVisible():
                self._topmost_timer.start()
                self._on_topmost_tick()  # 閉じた直後に押し上げ直す(500ms待たせない)

    def _cursor_inside(self) -> bool:
        """カーソルが自分の上にあるか。

        underMouse() ではなく座標で見る。この窓はフォーカスを取らない Qt.Tool で、
        メニューが閉じた直後は Qt 側の「マウスが乗っている」状態が実際とずれることが
        あるため、いまの矩形とカーソル位置から自分で判定する。"""
        return self.geometry().contains(QCursor.pos())
