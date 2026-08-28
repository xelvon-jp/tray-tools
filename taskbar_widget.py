# taskbar_widget.py
# 各ディスプレイのタスクバーに置く「通知領域の代わり」の小さなウィジェット。
#
# Windowsは通知領域(トレイ)をプライマリのタスクバーにしか出さない。ノートPCの画面を
# プライマリ、正面の拡張ディスプレイをセカンダリにしている構成では、tray-tools を使う
# たびに視線と手が正面ではないノート側へ行ってしまう。そこで、タスクバーに
# トレイの代わりを自分で置く。
#
# 既定では「すべてのディスプレイ」に1つずつ出す(all_displays)。プライマリには本物の
# 通知領域があるので重複ではあるが、狙いは「座標をそろえること」ではなく「どの画面でも
# 使えること」なので、全部に出す。置き先は画面の数で決める(タスクバーの数ではない)。
# Windows の「タスクバーをすべてのディスプレイに表示する」がオフの環境ではセカンダリに
# タスクバーが存在せず、タスクバーを数えると2番目以降が1つも出なくなるため。
#
# 位置は「基準の矩形の右端・上端からのオフセット」で持つが、値は画面ごとに独立している
# (settings.json の positions を画面ごとのキーで引く)。1組を全画面で共有していた頃は、
# 1つをドラッグすると他の画面のものまで動き、しかもドラッグ中にタスクバーをまたぐと
# 基準が乗り換わって別の画面へワープしていた。
#
# 見た目はタスクバーの時計そのもの(既存の時計に重ねて隠す)で、マウスを乗せた間だけ
# Rapture と音声のアイコンに入れ替わる。隣に生やさないのは、タスクバーの上に自前の
# ものが常時2つ増えて見えるのが邪魔だからで、「時計の場所に用がある」わけではない。
#
# その幅(実測59px前後)に置けるアイコンは2つが限界なので、残りの機能(画面定規・カラー
# ピッカー・定型文・フォルダブックマーク)へは、マウスを乗せている間だけ真上へ縦一列で
# 出るパネルから届かせる(taskbar_launcher.LauncherPanel)。本体の見た目は従来どおり
# 時計⇄2アイコンのままで、パネルはタスクバーの外へはみ出す別の窓として上に出る。
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

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

import taskbar_launcher
import window_tools
from capture_grab import device_bounds_to_logical, grab_region
from toast import show_toast

ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"

# 位置は「基準の矩形の端からの距離(論理px)」で持つ。絶対座標にしないのは、解像度や
# 拡大率が変わっても右下との位置関係が保たれるため。ただし値は画面ごとに別々に持つ
# (config["positions"][_screen_key(screen)])。設定にその画面のキーが無ければ下の既定値で
# 自動配置するので、別のPCへ settings.json を持って行っても壊れない(画面名が違えば
# 「知らない画面」として既定に落ちるだけ)。
# 値の出どころ: 画面のピクセルから Windows 11 の時計の描画範囲を割り出すと
# 「右端から20px・上端から6px」だったが、実際に2枚のモニタで目視で合わせたら
# 19/10 と 18/9 に落ち着いた。文字のアンチエイリアスで端が薄くなる分、濃淡から
# 測る方法では描画枠より内側に見積もっていたらしい。目で合わせたほうを採る。
DEFAULT_RIGHT_MARGIN = 19
DEFAULT_TOP_MARGIN = 10

# タスクバーが見つからない画面で、代わりに基準にする帯の高さ(論理px)。
# Windows の「タスクバーをすべてのディスプレイに表示する」がオフだと、セカンダリには
# Shell_SecondaryTrayWnd が存在しない。それでも全画面に出すのが狙いなので、画面の
# 下端から逆算した帯を代わりの基準にする。縦位置の基準になるものが他に無く、かつ
# タスクバーがあるとすれば画面の下端に接しているので、そこを上端とみなせば既定の
# 余白(上端からの距離)がそのまま通用する。
# 高さは、実在するタスクバー(たいていはプライマリ)のものを借りるのがいちばんずれない
# (Windows は全ディスプレイで同じ高さのタスクバーを出す)。1つも見つからないときだけ、
# この既定値(Windows 11 の既定のタスクバー高さ)を使う。
FALLBACK_TASKBAR_HEIGHT = 48

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

# 測った色を「タスクバーの地の色として妥当か」で振るう閾値。
# 画面を切り替えた直後の一瞬は、その画面にまだ何も描かれておらず真っ黒を拾う。
# そのまま採用すると背景が黒・文字が白のウィジェットが残る(実際そう報告された)。
# タスクバーが完全な黒や完全な白であることは通常ないので、測り損ねとみなす。
IMPLAUSIBLE_DARK = 8
IMPLAUSIBLE_LIGHT = 248

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
# 音声アイコンの上でホイールを回したときの、音量の反映を待つ間隔(ms)。
# 音量の上下は1回20msほどかかる(COM越しにエンドポイントを引くため)。ホイールは
# 一度に何目盛りも飛んでくるので、そのつど呼ぶと回している間ずっと引っかかる。
# 目盛りを溜めて、止まった時点でまとめて反映する。
VOLUME_APPLY_DELAY_MS = 60

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


def _screens() -> list:
    """いまの画面(QScreen)の一覧。QApplication が無ければ空リスト。

    QGuiApplication.screens() を直に呼ばずここへ通しているのは、画面構成に依存する
    挙動(タスクバーが無い画面へのフォールバックなど)を、実機のモニタを抜き差しせずに
    差し替えて確かめられるようにするため。"""
    if QGuiApplication.instance() is None:
        return []
    return list(QGuiApplication.screens())


def _screen_base_key(screen) -> str:
    """位置を覚えるときの画面の識別子。

    Windows の QScreen.name() はモニタの型番(EX-LDGCQ321HD 等)を返す。ケーブルを
    挿し替えても切替器で他のPCへ渡して戻しても同じ名前が付くので、DISPLAY1/2 のような
    接続順に左右される名前より、位置を覚えておく先として安定している。

    ただし同じ型のモニタを2台並べると型番だけでは区別できない(外付けを2枚使う構成が
    まさにそれ)。EDIDのシリアルを混ぜて別物として扱う。"""
    name = screen.name() or ""
    serial = screen.serialNumber() or ""
    return "%s#%s" % (name, serial) if serial else name


def _screen_key(screen, screens=None) -> str:
    """位置を覚えるときの画面の識別子。同じ名札の画面が複数あるときだけ座標で分ける。

    シリアルを返さないモニタや、同じ値を返してしまうモニタがある。同型を2台並べて
    それに当たると名札が衝突し、2枚とも同じ位置設定と同じタスクバーを見て、片方の
    画面には何も出ないまま同じ場所に重なる。その場合だけ画面の左上座標を混ぜる。

    座標を混ぜるのは衝突したときに限る。モニタを並べ替えると座標は変わるので、
    常用すると「配置を変えたら位置を忘れる」ことになるため。シリアルが読める限りは
    座標に依存しない名札のままにしておく。"""
    base = _screen_base_key(screen)
    if screens and sum(1 for other in screens if _screen_base_key(other) == base) > 1:
        geometry = screen.geometry()
        return "%s@%d,%d" % (base, geometry.x(), geometry.y())
    return base


def _primary_screen_name(screens: list):
    """プライマリの画面名。判別できなければ None。

    QGuiApplication.primaryScreen() を優先し、それが渡された一覧に居ないときは
    「仮想デスクトップの原点(0,0)にある画面」を採る。Windows はプライマリモニタの
    左上を必ず (0,0) に置くので、これで一致する。"""
    primary = QGuiApplication.primaryScreen() if QGuiApplication.instance() else None
    names = [_screen_key(screen, screens) for screen in screens]
    if primary is not None and _screen_key(primary, screens) in names:
        return _screen_key(primary, screens)
    for screen in screens:
        if screen.geometry().topLeft() == QPoint(0, 0):
            return _screen_key(screen, screens)
    return None


def _screen_for(rect: QRect, screens: list):
    """その矩形がいちばん広く重なっている画面。どれとも重ならなければ None。

    中心点で screenAt() を引くのではなく重なりの面積で選ぶ。タスクバーの矩形は画面の
    端いっぱいに置かれ、環境によっては数px はみ出して隣の画面まで届くため。"""
    best = None
    best_area = 0
    for screen in screens:
        overlap = screen.geometry().intersected(rect)
        area = overlap.width() * overlap.height()
        if area > best_area:
            best_area = area
            best = screen
    return best


def _fallback_taskbar(screen_geometry: QRect, height: int) -> QRect:
    """タスクバーが見つからない画面で代わりに基準にする、画面下端の帯。

    「タスクバーがあるとすればここ」という位置。横は画面いっぱい、縦は画面の下端から
    height ぶん上。高さの根拠は FALLBACK_TASKBAR_HEIGHT のコメントを参照。"""
    height = max(min(height, screen_geometry.height()), MIN_SIZE)
    return QRect(
        screen_geometry.x(),
        screen_geometry.y() + screen_geometry.height() - height,
        screen_geometry.width(),
        height,
    )


def widget_slots(include_primary: bool = True) -> list:
    """ウィジェットの置き先を (画面名, 基準にする矩形) の並びで返す。画面1つにつき1つ。

    タスクバーの数ではなく画面の数で決める。Windows の「タスクバーをすべての
    ディスプレイに表示する」がオフの環境ではセカンダリに Shell_SecondaryTrayWnd が
    存在せず、タスクバーを数えると2番目以降が1つも出ないため(実際にそう報告された)。
    タスクバーが見つからない画面には、その画面の下端の帯を基準として渡す。

    include_primary=False ならプライマリの画面を外す(そちらには本物の通知領域がある)。"""
    screens = _screens()
    if not screens:
        return []

    rects = taskbar_rects(include_primary=True)
    # 代用の帯の高さは、実在するタスクバーから借りる(いちばん厚いものを採る。自動的に
    # 隠れる設定などで潰れかけた矩形を掴んでも薄くならないように)。
    fallback_height = max(
        (rect.height() for rect in rects), default=FALLBACK_TASKBAR_HEIGHT
    )

    taskbar_by_screen = {}
    for rect in rects:
        screen = _screen_for(rect, screens)
        if screen is None:
            continue
        name = _screen_key(screen, screens)
        current = taskbar_by_screen.get(name)
        # 1画面に複数見つかったら面積の大きいほうを採る。
        if current is None or rect.width() * rect.height() > current.width() * current.height():
            taskbar_by_screen[name] = rect

    primary_name = _primary_screen_name(screens)

    slots = []
    for screen in screens:
        name = _screen_key(screen, screens)
        if not include_primary and name == primary_name:
            continue
        rect = taskbar_by_screen.get(name)
        if rect is None:
            rect = _fallback_taskbar(screen.geometry(), fallback_height)
        slots.append((name, rect))
    return slots


def _default_top_left(taskbar: QRect, width: int, height: int) -> QPoint:
    """設定にその画面の位置が無いときの置き場所(タスクバーの時計に重なる位置)。

    QRect.right() は「最後のピクセル」を指す(幅は right - left + 1)。タスクバーの
    排他的な右端は right() + 1 なので、そこから余白と自分の幅を引く。
    縦は上端から DEFAULT_TOP_MARGIN。ただしタスクバーが実測より薄い環境でははみ出すので、
    そのときだけ中央寄せに落とす。

    この中央寄せは「既定の自動配置」にだけかかる調整で、ユーザーがドラッグして決めた
    位置には一切かけない(かけると置ける場所が制限され、ある高さより下に置けなくなる)。"""
    x = taskbar.right() + 1 - DEFAULT_RIGHT_MARGIN - width
    if taskbar.height() >= DEFAULT_TOP_MARGIN + height:
        y = taskbar.top() + DEFAULT_TOP_MARGIN
    else:
        y = taskbar.top() + max((taskbar.height() - height) // 2, 0)
    return QPoint(x, y)


def _is_reachable(rect: QRect) -> bool:
    """その矩形が仮想デスクトップと少しでも重なっているか。

    掴めない場所に窓を出さないための、最小限の歯止め。完全に画面外へ出てしまった窓は
    ドラッグで戻せず、settings.json を手で直すしかなくなる。逆に言えば少しでも重なって
    いれば掴めるので、そのときは何もしない(タスクバーの外や画面の隅に置く使い方を
    妨げないため。置ける場所を狭める用途にこの判定を使ってはいけない)。

    仮想デスクトップは capture_grab.virtual_geometry ではなく _screens() から組み立てる。
    画面の一覧をこのモジュール内の1か所(_screens)に統一しておかないと、置き先を決めた
    画面構成と、掴めるかを判定する画面構成が食い違いうるため。"""
    virtual = QRect()
    for screen in _screens():
        virtual = virtual.united(screen.geometry())
    return virtual.isEmpty() or virtual.intersects(rect)


def _as_int(value, default: int) -> int:
    """設定値を整数にする。数として読めない値が書かれていたら既定に落とす。
    設定は手で書き換えられるので、書き損じでウィジェットが1つも出ないのは避ける。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_plausible_background(color: QColor) -> bool:
    """測った色が地の色として妥当か。真っ黒・真っ白は測り損ねとみなす。"""
    if not color.isValid():
        return False
    channels = (color.red(), color.green(), color.blue())
    if all(value <= IMPLAUSIBLE_DARK for value in channels):
        return False
    if all(value >= IMPLAUSIBLE_LIGHT for value in channels):
        return False
    return True


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
    """タスクバーの時計に重ねる、枠なし・不透明の小さな窓。ディスプレイ1枚につき1つ。

    普段は時計を描き、マウスを乗せている間だけ Rapture と音声のアイコンに入れ替わる。
    クリックの割り当ては通知領域のアイコンに合わせてある(Rapture=中クリックで即キャプチャ・
    右クリックでメニュー / 音声=左クリックで切替・右クリックでメニュー)。

    位置の基準にする矩形(その画面のタスクバー、無ければ画面下端の帯)と、その画面の名前は
    生成時に受け取る。矩形はディスプレイ構成が変われば変わるが、追従は作り直し
    (ScreenFeature)に任せる。動いたタスクバーを掴み直す仕組みをここに持たせても、画面が
    増減したときには結局作り直しが要るため。

    基準の矩形は生成時のものを最後まで使い、途中で乗り換えない。ドラッグ中に「いま乗って
    いるタスクバー」へ基準を切り替えると、画面をまたいだ瞬間に基準が1画面ぶんずれて、
    保存した位置が別の画面へワープする。

    WA_TranslucentBackground は使わない。下にある本物の時計を隠すのが仕事なので、
    背景は必ず不透明に塗る。"""

    def __init__(self, app_settings: dict, settings_path, screen_feature, audio_feature,
                 taskbar: QRect, screen_name: str):
        super().__init__()
        self.app_settings = app_settings
        self.settings_path = settings_path
        self._taskbar = QRect(taskbar)
        # 設定から自分の位置を引くキー。Windows では "\\.\DISPLAY1" のような名前になる。
        # ScreenFeature が作り直すときの同一性判定にも使うので公開しておく。
        self.screen_name = screen_name
        # ScreenFeature / AudioFeature の参照をそのまま持つ。アイコンの絵だけでなく
        # start_capture・do_toggle・既存の self.menu も呼ぶ必要があり、絵を返す口だけ
        # 足しても足りないため(メニューは別に作らず、通知領域と同じものを出す)。
        self._screen = screen_feature
        self._audio = audio_feature

        self._config = app_settings.setdefault("taskbar_widget", {})
        self._hover = False
        # 全画面のアプリに譲ってZオーダーの後ろへ回っている最中か。
        self._behind = False
        # この画面を自前の全画面窓(画面ミラー)が覆っているか。set_app_covered で立てる。
        # _hidden_by_fullscreen とは別に持つ理由は、あちらのコメントを参照。
        self._app_covered = False
        # メニューを出している間だけ True。カーソルがメニューへ移ると leaveEvent が
        # 飛んでくるが、その間もアイコンを出したままにするために使う。
        self._menu_open = False
        self._drag_offset = None
        self._audio_pixmap = None
        # ホイールで溜めた音量の目盛り。止まったところでまとめて反映する。
        self._volume_steps = 0
        self._volume_timer = QTimer(self)
        self._volume_timer.setSingleShot(True)
        self._volume_timer.timeout.connect(self._apply_volume)
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

        # マウスを乗せている間だけ真上に出る縦一列のランチャ。参照はここで持ち続ける
        # (ローカル変数だけだとGCで即消える)。窓を作るだけで表示はしないので、
        # ランチャを使わない設定でも作っておいて構わない(出すかどうかは
        # taskbar_launcher.resolve_items が設定を見て決める)。
        self._launcher = taskbar_launcher.LauncherPanel(
            self, screen_feature, audio_feature, self._config
        )

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

    def refresh_background(self) -> None:
        """背景色をいま画面にある色で測り直す(壁紙を変えたとき用)。

        自分が乗っている場所を測るので、いったん隠してから撮る。hide() の直後はOSが
        まだ下を描き直していないため、少し待ってから測る。"""
        if not self.isVisible():
            return
        self.hide()
        QTimer.singleShot(REDRAW_WAIT_MS, self._finish_refresh_background)

    def _finish_refresh_background(self):
        """測り直して settings.json に控える。

        起動のたびに測る形はやめた。画面を切り替えた直後は、その画面にまだ何も
        描かれておらず真っ黒を拾うため(_apply_colors のコメント参照)。壁紙を変えた
        ときはこのメニュー項目で更新する。"""
        try:
            self._apply_colors(measure=True)
            self.show()
        except Exception:
            _guard("背景色の取り直し")
            self.show()  # 測り直しに失敗しても消えたままにはしない

    def _apply_colors(self, measure: bool) -> None:
        """背景色と文字色を決めて保持する。measure=True のときだけ画面を実測する。

        実測するのは、設定に色が無いとき(初回)と、メニューから取り直したときだけ。
        測れたら settings.json に書いて、以後はそれを使う。

        毎回測る形にしていたのをやめたのは、画面を切り替えた直後にウィジェットが
        作り直され、まだ何も描かれていない画面から真っ黒を拾っていたため。壁紙を
        変えたときは「背景色を取り直す」で更新する。"""
        configured = self._config.get("background_color")
        background = None
        if measure or not configured:
            measured = QColor(_dominant_color(self.geometry()))
            if _is_plausible_background(measured):
                background = measured
                self._remember_background(measured)
            elif configured:
                # 測り損ねたときは、覚えている色があればそれを守る。
                background = QColor(configured)
        elif configured:
            background = QColor(configured)
        if background is None or not background.isValid():
            background = QColor(FALLBACK_BACKGROUND)
        self._background = background

        text = self._config.get("text_color")
        color = QColor(text) if text else QColor(_auto_text_color(background))
        self._text_color = color if color.isValid() else QColor(_auto_text_color(background))
        # ランチャのパネルは自分の位置を実測できない(下にあるのは他のアプリの窓)ので、
        # ここで決めた色を借りる。1つの部品に見せるため、色は必ず本体と同じにする。
        self._launcher.refresh_colors()
        self.update()

    def _remember_background(self, color: QColor) -> None:
        """測れた背景色を settings.json に控える。次からは測らずにこれを使う。"""
        name = color.name()
        if self._config.get("background_color") == name:
            return
        save_config(self.app_settings, self.settings_path, background_color=name)

    def current_colors(self):
        """いま使っている (背景色, 文字色)。ランチャのパネルが自分と同じ色で描くために使う。

        コピーを返す。パネル側で調整(強調色や枠の色を作る)ときに、渡した QColor を
        書き換えられても本体の色が変わらないようにしておく。"""
        return QColor(self._background), QColor(self._text_color)

    def _saved_offset(self):
        """設定に入っているこの画面ぶんの (右端からの余白, 上端からの余白)。無ければ None。

        位置は画面ごとに独立して持つ(キーは _screen_key)。知らない画面名しか入って
        いない settings.json(別のPCから持ってきたもの)は「この画面の分は無い」となって
        既定位置に落ちるだけで、壊れない。

        設定は手で書き換えられるので、形が違えば黙って既定へ落とす(書き損じでウィジェットが
        1つも出ないほうが困る)。"""
        positions = self._config.get("positions")
        if not isinstance(positions, dict):
            return None
        entry = positions.get(self.screen_name)
        if not isinstance(entry, dict) or "right" not in entry or "top" not in entry:
            return None
        return (
            _as_int(entry.get("right"), DEFAULT_RIGHT_MARGIN),
            _as_int(entry.get("top"), DEFAULT_TOP_MARGIN),
        )

    def _resolve_geometry(self):
        """表示すべき矩形(Qt論理座標)。出せる場所が無ければ None。

        生成時に渡された基準の矩形と、この画面ぶんの余白だけで決まる。基準を後から
        乗り換えないので、画面をまたいでドラッグしても位置が飛ばない。

        保存済みの余白はそのまま使う。作業領域(availableGeometry)へのクランプも、
        タスクバーの中へ引き戻す補正もしない。この窓の居場所であるタスクバーの上は
        そもそも作業領域から除外されているうえ、補正を入れると「ある高さより下には
        置けない」ことになる(実際そうなっていた)。

        唯一の例外は、保存した位置がどの画面にも掛からなくなったとき(モニタを外した等)。
        そのまま出すと二度と掴めないので、そのときだけ既定位置へ戻す。"""
        if self._taskbar.isEmpty():
            return None

        height = max(int(self._config.get("height") or 0), MIN_SIZE)
        # 幅は書式で変わる(日付＋曜日と秒付きの時刻では倍近く違う)ので、設定に無ければ
        # 実際に描く文字を測って決める。固定値だと書式を変えたときに端が切れる。
        configured = self._config.get("width")
        width = max(int(configured), MIN_SIZE) if configured else self._measure_width(height)

        offset = self._saved_offset()
        if offset is None:
            return QRect(_default_top_left(self._taskbar, width, height), QSize(width, height))

        right_margin, top_margin = offset
        rect = QRect(
            self._taskbar.right() + 1 - right_margin - width,
            self._taskbar.top() + top_margin,
            width,
            height,
        )
        if not _is_reachable(rect):
            return QRect(_default_top_left(self._taskbar, width, height), QSize(width, height))
        return rect

    def showEvent(self, event):
        super().showEvent(event)
        self._clock_timer.start()
        self._topmost_timer.start()
        self._on_topmost_tick()  # 出した直後にタスクバーの上へ回す(500ms待たせない)

    def hideEvent(self, event):
        # 本体が消えるならパネルも道連れ。別の窓なので放っておくと、本体だけ消えて
        # アイコンの列がタスクバーの上に取り残される(表示のトグルや、ディスプレイ構成が
        # 変わったときの作り直しでここを通る)。
        self._launcher.close_panel()
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
            hwnd = int(self.winId())
            if self._app_covered or self._hidden_by_fullscreen():
                # 動画の全画面表示などの上に出続けると邪魔でしかない。hide() ではなく
                # Zオーダーを下げる。隠すと hideEvent でこのタイマーごと止まり、全画面が
                # 終わったことに気づけなくなる(下げるだけならタイマーは回り続ける)。
                #
                # 下げるのは後ろへ回る1回だけ。毎周期やると、全画面の間ずっと他の窓の
                # Zオーダーを触り続けることになる。
                if not self._behind:
                    self._behind = True
                    window_tools.send_to_back(hwnd)
                    self._launcher.close_panel()
                return
            self._behind = False
            window_tools.push_topmost(hwnd)
        except Exception:
            _guard("最前面への押し上げ", notify=False)

    def set_app_covered(self, covered: bool) -> None:
        """自分の画面を、このアプリ自身の全画面窓(画面ミラー)が覆っているかを受け取る。

        _hidden_by_fullscreen では検出できないので外から教えてもらう。あちらは
        GetForegroundWindow を見るが、ミラー窓は WS_EX_NOACTIVATE(WindowDoesNotAcceptFocus)
        ＋WA_ShowWithoutActivating で作ってあり、発表の邪魔をしないために「決して前面に
        ならない」窓だからである。前面になるのは発表者が操作している手元のアプリのままで、
        その矩形はミラー先の画面を覆わない。結果ここが常に False を返し、ウィジェットは
        500msごとに push_topmost を続け、同じく500msごとに raise_ するミラー窓と最前面を
        奪い合って、共有側のモニタでチカチカしていた。

        引っ込め方は全画面アプリのときと同じ(hide せずZオーダーだけ下げる)。hide すると
        hideEvent でこのタイマーごと止まり、ミラーが終わったことに気付けなくなる。"""
        try:
            covered = bool(covered)
            if covered == self._app_covered:
                return
            self._app_covered = covered
            if not covered:
                # 戻すときは次の周期を待たずに押し上げ直す。ミラーを終えた瞬間に
                # ウィジェットが消えたままだと、壊れたようにしか見えない。
                self._behind = False
            if self.isVisible():
                self._on_topmost_tick()
        except Exception:
            _guard("全画面窓への譲り合いの切り替え", notify=False)

    def _hidden_by_fullscreen(self) -> bool:
        """自分が乗っている画面を、前面のウィンドウが丸ごと覆っているか。

        判定に使うのは自分の画面だけ。2枚目で動画を全画面にしているときに、1枚目の
        ウィジェットまで引っ込む必要はない。

        ここで拾えるのは「前面(GetForegroundWindow)になる」窓だけ。前面にならない窓に
        覆われる場合は set_app_covered で外から教えてもらう。"""
        screen = _screen_for(self.geometry(), _screens())
        if screen is None:
            return False
        bounds = window_tools.foreground_bounds()
        if bounds is None:
            return False
        # Win32が返すのは物理px。画面の矩形はQtの論理座標なので、換算してから比べる。
        return device_bounds_to_logical(bounds).contains(screen.geometry())

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
        # パネルにも同じ絵が並んでいる。あちらは描くたびに AudioFeature から貰うので、
        # 描き直しの合図だけ送れば足りる。
        self._launcher.notify_audio_changed()
        self.update()

    # ---------------------------------------------------------------
    # 描画
    # ---------------------------------------------------------------
    def wheelEvent(self, event):
        """音声アイコンの上で回したら音量を上下させる。

        トレイアイコン側では受けられない。QSystemTrayIcon は QWidget ではないので
        wheelEvent を持たず、Windows もトレイアイコンへホイールを転送しないため。
        こちらは自前の窓なので普通に届く。"""
        try:
            _rapture_zone, audio_zone = self._zones()
            if not audio_zone.contains(event.position().toPoint()):
                event.ignore()
                return
            notches = event.angleDelta().y()
            if not notches:
                return
            self._volume_steps += 1 if notches > 0 else -1
            self._volume_timer.start(VOLUME_APPLY_DELAY_MS)
            event.accept()
        except Exception:
            _guard("音量の変更", notify=False)

    def _apply_volume(self):
        """溜めた目盛りをまとめて反映し、結果を知らせる。"""
        try:
            steps, self._volume_steps = self._volume_steps, 0
            if not steps or self._audio is None:
                return
            level = self._audio.step_volume(steps > 0, abs(steps))
            if level is None:
                return
            show_toast("音量 %d%%" % round(level * 100))
        except Exception:
            _guard("音量の変更", notify=False)

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
            # 本体に入りきらない機能(画面定規・カラーピッカー・定型文・ブックマーク)へ
            # 届くように、真上へアイコンの列を出す。
            self._launcher.notify_widget_enter()
        except Exception:
            _guard("アイコン表示への切り替え")

    def leaveEvent(self, event):
        try:
            # メニューを出すとカーソルがそちらへ移ってここへ来るが、その間もアイコンを
            # 出したままにする(メニューを開いた瞬間に時計へ戻るのが見た目に落ち着かない)。
            # 閉じたあとの戻し判定は _popup がまとめて行う。
            if self._menu_open:
                return
            # ランチャのパネルへカーソルが移る途中かもしれない。本体とパネルは接して
            # いても別の窓なので、境目をまたぐ一瞬は必ずここへ来る。閉じる判断
            # (どちらにも乗っていないか)は猶予を置いてパネル側がまとめて行う。
            self._launcher.notify_widget_leave()
            if self._launcher.is_open():
                # パネルが開いている間はアイコンのままにする。メニューを出したときと
                # 同じ扱いで(_menu_open のコメント参照)、ここで時計へ戻すと、項目を
                # 選びに行く途中で本体だけ表示が変わって落ち着かない。戻す判断は
                # パネルが閉じたときに on_launcher_closed がまとめて行う。
                return
            self._hover = False
            self._audio_timer.stop()
            self.update()
        except Exception:
            _guard("時計表示への切り替え")

    def on_launcher_closed(self) -> None:
        """ランチャのパネルが閉じたときに、本体の見た目を現実に合わせ直す。

        パネルを開いている間は leaveEvent が来てもアイコン表示のままにしているので、
        閉じた時点で改めてカーソルの位置を見て、離れていれば時計へ戻す
        (メニューを閉じたあとに _popup がしているのと同じ後始末)。"""
        try:
            self._hover = self._cursor_inside()
            if not self._hover:
                self._audio_timer.stop()
            self.update()
        except Exception:
            _guard("ランチャを閉じたあとの表示の戻し", notify=False)

    def mousePressEvent(self, event):
        try:
            # Ctrl+左ドラッグだけを移動にする。素の左ドラッグは音声の切替(クリック)判定に
            # 使うため、そちらと取り合いにならないようにしている(付箋のペンと同じ作法)。
            if event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier:
                # パネルの位置は出したときの本体の位置から決まる。動かしている間ずっと
                # 置いてきぼりの列が残るのは邪魔なだけなので、掴んだ時点で畳む。
                self._launcher.close_panel()
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
            self._save_position()
        except Exception:
            _guard("位置の保存")

    def _save_position(self) -> None:
        """いまの位置を「基準の矩形の端からの距離」に直し、この画面ぶんだけ保存する。

        保存するのは自分の分だけで、他のウィジェットには一切触らない。画面ごとに独立した
        位置を持つのが狙いなので、1つ動かしたら全部動く、では画面ごとの調整ができない。

        基準は常に生成時の矩形(self._taskbar)。「いま乗っているタスクバー」を探し直すと、
        画面をまたいだ瞬間に基準が乗り換わって1画面ぶんずれ、次の再配置でワープする。

        タスクバーから離れた位置でも、そのぶん大きい(あるいは負の)余白として素直に
        保存される。クランプも丸めもしない。"""
        rect = self.geometry()
        positions = self._config.get("positions")
        positions = dict(positions) if isinstance(positions, dict) else {}
        positions[self.screen_name] = {
            # 右端どうし・上端どうしの距離。QRect.right() は最後のピクセルを指すので、
            # 排他的な右端(right() + 1 / x + width)どうしで引く。
            "right": self._taskbar.right() + 1 - (rect.x() + rect.width()),
            "top": rect.y() - self._taskbar.top(),
        }
        # save_config は settings.json を読み直して taskbar_widget の指定キーだけを
        # 書き戻す。positions は丸ごと1つのキーなので、他のPCの画面名が入っていても
        # このプロセスが読み込んだ内容ごと保たれる(_config には読み込み済みの値がある)。
        save_config(self.app_settings, self.settings_path, positions=positions)

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
        # メニューはカーソルの上へ開く(タスクバーが画面の下端にあるため下には出せない)。
        # つまりパネルとちょうど同じ場所を取り合う。しかもパネルは最前面に居続けるので、
        # 開いたままにすると項目の上に乗り上げて隠してしまう。右クリックはメニュー、と
        # 割り切って畳む。
        self._launcher.close_panel()
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
