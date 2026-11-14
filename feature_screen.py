# feature_screen.py
# 画面まわり全般のFeature(旧 feature_capture.py)。トレイアイコンを1つ所有し、
# 範囲キャプチャ付箋「Rapture」に加えて、カラーピッカー・画面定規・定型文・
# フォルダブックマーク・任意ウィンドウの最前面固定・スリープ抑止・マウスジグラー・
# 画面に重ねるプレゼン支援をこのアイコンのメニューから提供する。
#
# アイコンを増やさないのは意図的。状態を持つ機能(スリープ抑止)だけがアイコンの見た目を
# 占有し、単発の動作はメニュー項目で足りるという方針。マウスジグラーも状態を持つが、
# 16px相当のアイコンに2つ目の目印は入らないので、こちらはツールチップとメニューの
# 見出し(残り時間)だけで示す。
import json
import math
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QInputDialog, QLineEdit, QMenu, QMessageBox, QSystemTrayIcon,
)

import bg_remove_launch
import calendar_popup
import calculator
import capture_process
import color_picker
import command_launcher
import command_palette
import cursor_finder
from picker import PickerWindow
from settings import update_settings, update_settings_section
import unified_picker
import explorer_nav
import claude_progress
import claude_status
import filer_everything
import filer_launch
import filer_update
import folder_watch
import launcher
import mouse_jiggler
import muse_bridge_status
import presenter_overlay
import threading
from datetime import datetime

import pushover
import screen_mirror
import taskbar_launcher
import window_layout
import screen_ruler
import snippets
import teams_meeting
import tomorrow_memo
import browser_open
import capture_grab
import taskbar_widget
import web_presenter
from capture_grab import new_session_stem, save_image
from capture_overlay import CountdownOverlay, FrozenSelectionOverlay
from keep_awake import hibernate_available, set_keep_awake, suspend
import action_log
import rapture_timing
from sleep_countdown import SleepCountdown
from qt_image import pil_to_qicon
from toast import show_toast
from window_tools import TopmostTracker

ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"
# トレイ自体のアイコンは従来どおり rapture(常駐全体の顔なので変えない。
# 本人の指定)。あっふ の専用アイコンは affx_icon が持ち、窓とタスクバー
# (ピン)で使う。トレイメニューの項目は 📁 の絵文字(本人の指定)。

# 同梱の発表者ツール。HTMLのプレゼン資料にカンペ・次スライド・タイマー・レーザーを
# 付ける単一ファイルのビューアで、サーバー不要でブラウザに投げるだけで動く。
#
# こちらはブラウザの中だけの道具で、資料を about:blank へ書き出して同一オリジンに
# しているからスライドを検出できる。他所のサイトを iframe で直接開くとクロスオリジンで
# その前提が崩れるが、こちら側がブラウザ(QtWebEngine)を持って DOM を取り出してしまえば
# 壁は消える。それが web_presenter.py で、あちらは presenter.html を無改造のまま使う。
# 画面へ重ねる側(presenter_overlay.py)も残す(対象を選ばないのはあちらの取り柄)。
BUNDLED_PRESENTER = Path(__file__).resolve().parent / "presenter.html"

# 起動後にファイルが更新された(pull・ZIP 更新・この PC でのコミット)ことの知らせ。
# 見に行くのはメニューを開いたときと、この間隔ごと(ツールチップはメニューを開かずに見るため)。
DISK_NEWER_TEXT = "ファイルは新しい版です（再起動で反映）"
VERSION_CHECK_INTERVAL_MS = 5 * 60 * 1000

# Claude Code の状態表示(claude_status)。輪の色は状態ごと。確認待ちはスリープ抑止の
# 橙と見分けられるよう桃色にする(止まって待っているのが、いちばん見落としたくない)。
CLAUDE_MENU_TITLE = "💬 AI状態監視"
CLAUDE_STATUS_INTERVAL_MS = 3000
CLAUDE_RING_COLORS = {
    claude_status.ASK: (236, 72, 153, 255),       # 桃
    claude_status.FAIL: (231, 76, 60, 255),       # 赤
    claude_status.RUNNING: (55, 138, 221, 255),   # 青
    claude_status.DONE: (99, 153, 34, 255),       # 緑
}
# Windows の通知領域のツールチップは 127 文字まで(超えた分は切れる)。
TOOLTIP_MAX_CHARS = 127
CLAUDE_TOOLTIP_SESSIONS = 2
CLAUDE_PLAN_CHARS = 18

# スリープ抑止中の目印。通知領域のアイコンは実質16px相当で、隅の小さなバッジは潰れて
# 見えない。アイコン全周に太いリングを描き、縮小しても輪郭の変化で判別できるようにする。
AWAKE_RING_COLOR = (245, 158, 11, 255)
AWAKE_RING_WIDTH = 4

# 監視モード(agent-loop watch)中の目印。スリープ抑止と別の色にする。
# 通常監視: 緑のリング / 応答受信・実行中は同じ緑を太く塗る(状態の変化として感じられる)。
# CLAUDE.md「通知領域のアイコン2つ固定」の方針は既存を差し替えるだけなので守れている。
# Copilot まわりのメニューの見出し。状態は括弧で足す。
# 親を「Copilot」にしてあるのは、この下に性質の違う2つ(見るだけの状態監視バーと、
# コードまで実行するエージェントループ)が並ぶため。片方の名前を親にすると、
# もう片方がその下位機能に見えてしまう。
AGENT_LOOP_MENU_TITLE = "🤖 Copilot"
AGENT_LOOP_MENU_SUFFIX = {
    "idle": "",
    "watching": "（ループ待機中）",
    "busy": "（ループ実行中）",
    "err": "（ループ停止：要確認）",
}

AGENT_LOOP_RING_COLOR = (46, 204, 113, 255)   # 緑
AGENT_LOOP_BUSY_COLOR = (52, 152, 219, 255)   # 青(応答受信・実行中)
AGENT_LOOP_ERR_COLOR = (231, 76, 60, 255)     # 赤(危険停止など)
AGENT_LOOP_RING_WIDTH = 4

# 付箋の待機役(capture_process.py --prewarm)を最初に起こすまでの待ち。
# 起動直後はトレイアイコンの構築とホットキーの登録で忙しく、ここで0.4秒ぶんの
# プロセス起動を重ねると常駐が立ち上がるまでの体感が延びる。最初のキャプチャまでには
# 十分間に合うので、少しずらして出す。
PREWARM_STARTUP_DELAY_MS = 3000


# 予約の時刻になってから実際に寝るまでの猶予(秒)。すぐ寝ないのは、予約したことを
# 忘れて作業している最中に落ちると、開いているものが道連れになるため。この間に
# 「予約を取り消す」を押せば止まる。
SLEEP_WARN_SECONDS = 20

# 予約の何秒前からカウントダウンの窓を出すか。3分あれば、区切りのいいところまで
# 手を動かしてから止めるか寝るかを決められる。
SLEEP_COUNTDOWN_SECONDS = 180


def _format_seconds(seconds: int) -> str:
    """秒を「30秒」「5分」「1時間30分」のように読める形にする。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    return _format_minutes(seconds // 60)


def _format_minutes(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}時間"
    return f"{minutes}分"


def _join_state(head: str, state: str) -> str:
    """「Rapture（スリープ抑止中）」に状態を1つ足す。無ければ「Rapture（…）」にする。"""
    if head.endswith("）"):
        return head[:-1] + "・" + state + "）"
    return f"{head}（{state}）"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _fit_tooltip(lines) -> str:
    """Windows の上限(127文字)に収める。収まらなければ後ろの行から落とす
    (切れ目の分からない途中切れより、行ごと落とすほうが読める)。"""
    lines = list(lines)
    while len(lines) > 1 and len("\n".join(lines)) > TOOLTIP_MAX_CHARS:
        lines.pop()
    return _clip("\n".join(lines), TOOLTIP_MAX_CHARS)


def _read_disk_version() -> dict:
    """いまディスクにある版(filer_update.quick_version)。読めなければ空(表示が出ないだけ)。"""
    try:
        return filer_update.quick_version()
    except Exception:  # noqa: BLE001
        return {}


def _remaining_minutes(deadline):
    """締切(time.monotonic基準)までの残り分数。deadlineがNone(無期限)ならNone。"""
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    # 端数は切り上げる(残り30秒を「0分」と出さない)。ちょうど60分なら60分と出す。
    return max(math.ceil(remaining / 60), 1) if remaining > 0 else 0


def _positive_number(value, default):
    """settings.jsonは手で編集する前提なので、数字でない値や0・負数が入りうる。
    そのままQTimerの間隔に渡すと延々と発火し続けるため、おかしければ既定へ落とす。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _make_awake_icon_image() -> Image.Image:
    """rapture.png(ユーザーの手描きドット絵)にリングを重ねた画像をメモリ上で作る。
    元ファイルは書き換えない。"""
    return _make_ring_icon_image(AWAKE_RING_COLOR, AWAKE_RING_WIDTH)


def _make_ring_icon_image(color, width) -> Image.Image:
    """rapture.png を土台にして、任意の色・太さのリングを重ねる。
    _make_awake_icon_image を一般化したもの(agent-loop でも同じ形を使う)。"""
    base = Image.open(ICON_PATH).convert("RGBA")
    img = base.copy()
    draw = ImageDraw.Draw(img)
    inset = width / 2
    draw.ellipse(
        (inset, inset, img.width - 1 - inset, img.height - 1 - inset),
        outline=color,
        width=width,
    )
    return img


class _PushoverBridge(QObject):
    """送信スレッドからメインスレッドへ結果を渡すための器。

    ScreenFeature 自身は QObject ではないのでシグナルを持てない。トーストはQtの窓で、
    ワーカーから直に作ると壊れるため、シグナル経由で必ずメインスレッドへ戻す
    (hotkeys.HotkeyBridge と同じ流儀)。"""

    finished = Signal(bool, str)


class _AgentLoopBridge(QObject):
    """エージェントループのワーカースレッドからメインスレッドへ状態変化を届ける器。

    _PushoverBridge と同じ理由(ScreenFeature は QObject ではない)で、
    別スレッドで走る run_loop() のイベントを、まずシグナル経由でメインへ渡し直してから
    トレイアイコンを触る。ログ窓側はさらに独自の pyqtSignal で受けている。"""

    state_changed = Signal(dict)


class _TeamsMeetingBridge(QObject):
    """一人会議の立ち上げ(teams_meeting の子プロセス)の進み具合を、読み役のスレッドから
    メインスレッドへ渡す器。_AgentLoopBridge と同じ理由(ScreenFeature は QObject では
    なく、トーストは Qt の窓なのでワーカーから直に作ると壊れる)。"""

    event = Signal(dict)


class ScreenFeature:
    """Featureの規約: コンストラクタでQSystemTrayIconを1つ構築してself.tray_iconに保持し、
    hotkeys()で{"設定キー名": 関数}を返す。"""

    def __init__(self, app_settings: dict, settings_path=None):
        self.app_settings = app_settings
        self.settings_path = settings_path
        self.countdown = None
        self.overlay = None
        self.picker = None
        self.ruler = None
        self.snippet_picker = None
        self.calendar_popup = None
        self.text_editor = None
        self.tomorrow_memo_window = None
        self._tomorrow_memo_store = None
        self._tomorrow_memo_waiters = []
        self.calculator_window = None
        self.cursor_finder = None
        self._palette_apps_cache = None
        self._palette_es_cache = None
        self._palette_history_cache = None
        self._cmd_history_cache = None
        self._unified_es_dead = False
        self.launcher_picker = None
        # 画面に重ねるプレゼン支援(レーザー・スポットライト・黒/白画面)。窓は最大4枚に
        # なるうえ排他関係もあるので、参照と開閉は presenter_overlay 側の
        # OverlayController にまとめてある(ここが1つだけ持つ。持たないとGCで即消える)。
        self.presenter_overlays = presenter_overlay.OverlayController(app_settings)
        # 手元の画面の一部を別のモニタへ全画面でミラーする「画面ミラー」。範囲選択・
        # ミラー窓・手元の枠の3枚を抱えるので、参照と開閉は screen_mirror 側の
        # MirrorController にまとめてある(ここが1つだけ持つ。持たないとGCで即消える)。
        self.screen_mirror = screen_mirror.MirrorController(
            app_settings, settings_path, self._notify
        )
        # 手元のツールバーからプレゼン支援を押せるようにする。黒画面/白画面の窓を
        # 持っているのはこちら(presenter_overlays)なので、あちらからは触れない。
        # レーザーとスポットライトも、行き先の振り替え・通知・メニューのチェックが
        # toggle_presenter_overlay に揃っているので同じ入口へ回す。
        self.screen_mirror.attach_presenter(
            self.toggle_presenter_overlay, self._presenter_overlay_active
        )
        # ミラーが覆っている画面の名前(覆っていなければ None)。ディスプレイ構成が
        # 変わってウィジェットを作り直したときに、もう一度同じ指示を出すために覚える。
        # 先に用意してから繋ぐ(attach_screen_cover はその場で1回呼び返してくる)。
        self._mirror_covered_screen = None
        # ミラー窓が覆っている画面のタスクバーウィジェットを引っ込めるための連絡口。
        # ウィジェットを持っているのはこちらなので、あちらからは名前だけ受け取る。
        self.screen_mirror.attach_screen_cover(self._set_mirror_covered_screen)
        # 付箋(Rapture)のウインドウはここでは持たない。別プロセス(capture_process.py)に
        # 出したので、参照どころか同じアドレス空間にすら居ない。以前は開いている付箋を
        # self.capture_windows に、ホットキーで撮る対象を self.active_capture_window に
        # 持っていたが、両方とも名前付きパイプの一覧から引き当てる形へ移した
        # (capture_process.list_sticky_pipes / send_to_latest)。
        # そうしたのは、本体が落ちたときと再起動したときの道連れを防ぐため。開いている
        # 付箋を掴んでいる限り、本体の寿命が付箋の寿命になってしまう。
        #
        # 付箋を出すまでの待ちを消すため、画像を渡されるまでウインドウを出さない
        # 「待機役」を1つ飼っておく(capture_process の冒頭を参照)。これも別プロセスなので
        # ここで持つのは参照ではなくタイマーだけ。居場所はやはりパイプの一覧から引く。
        self._prewarm_timer = QTimer()
        self._prewarm_timer.setSingleShot(True)
        self._prewarm_timer.timeout.connect(self._ensure_prewarmed)

        self.topmost = TopmostTracker()

        # 各ディスプレイのタスクバーに置く時計擬態ウィジェット。Featureにはしない
        # (トレイアイコンを増やさない方針)ので、ここが生成と参照の持ち主になる。
        # ローカル変数だけで持つとGCで即消えるため、必ずこの属性で掴んでおくこと。
        # タスクバー1つにつき1つ作るのでリストで持つ(何画面あるかは起動後まで決まらない)。
        # 実体は音声側の参照が要るので、Featureが揃ってから attach_audio_feature で作る。
        self.taskbar_widgets = []
        self._audio_feature = None
        # 自分を起動し直す手段。待ち受けを握る main.py から attach_restart で渡される。
        self._restart_app = None
        # ディスプレイ構成が変わったら作り直す。変化の通知が連続で飛ぶ(1台の抜き差しでも
        # screenRemoved と primaryScreenChanged が続けて来る)ので、シングルショットで
        # 受けて最後の1回にまとめる。
        self._taskbar_rebuild_timer = QTimer()
        self._taskbar_rebuild_timer.setSingleShot(True)
        self._taskbar_rebuild_timer.timeout.connect(self._rebuild_taskbar_widgets)

        self._awake_active = False
        self._awake_minutes = None  # None = 無期限
        self._awake_deadline = None
        # スリープの予約。寝る時刻まで数えるタイマーと、寝る直前の猶予。
        # 猶予を挟むのは、予約したことを忘れて作業している最中に落ちるのを防ぐため。
        # 声を掛けてから寝るまでの間に取り消せる。
        self._sleep_timer = QTimer()
        self._sleep_timer.setSingleShot(True)
        self._sleep_timer.timeout.connect(self._on_sleep_due)
        self._sleep_warn_timer = QTimer()
        self._sleep_warn_timer.setSingleShot(True)
        self._sleep_warn_timer.timeout.connect(self._do_sleep)
        self._sleep_deadline = None
        self._sleep_hibernate = False
        # 予約が近づいたら出すカウントダウンの窓。参照を持たないとGCで消える。
        self._sleep_countdown = None
        self._sleep_tick = QTimer()
        self._sleep_tick.timeout.connect(self._on_sleep_tick)
        self._awake_timer = QTimer()
        self._awake_timer.setSingleShot(True)
        self._awake_timer.timeout.connect(self._on_awake_expired)
        self._awake_icon = None

        # エージェントループ(監視モード)の状態と、そのログ窓の参照。
        # 状態は "idle"(オフ) / "watching"(待機中) / "busy"(応答受信中や実行中) / "err"。
        # ログ窓は監視モードを開始したときだけ開く(参照はここで持たないと GC で消える)。
        # 見た目の状態(トレイアイコンの色・メニューの見出し)。idle / watching /
        # busy / err。**err は止まったあとも残す** —— 危ない止まりかたをしたことに
        # 気づいてほしいので、次に始めるまで色を戻さない。
        self._agent_loop_state = "idle"
        # いま実際に走っているか。**見た目の状態と混ぜないこと。**
        #
        # 【混ぜていて詰んだ】
        # 以前は _agent_loop_state != "idle" を「走っている」の判定に使っていた。
        # 危険パターンで止まると "err" のまま戻らないので、そのあと
        #   - 次のループを開始できない(押しても無視される)
        #   - 停止の項目だけ押せる(走っていないのに)
        #   - 外からの再起動が「道連れになります」で断られ続ける
        # という詰み方をした(実測 2026-09-11)。表示は残したいが、走っているかは
        # 別の話なので、別に持つ。
        self._agent_loop_active = False
        self._agent_loop_viewer = None
        # 監視モードの子プロセス。常駐の中で回すと UIA と pycaw の同居で落ちるので
        # 別プロセスにしてある(start_agent_loop_watch のコメント参照)。
        self._agent_loop_proc = None
        # いま何周目か。状態監視バーへ知らせるために覚えておく(周回数はイベントの
        # round_start にしか載っていないので、実行中のイベントからは拾えない)。
        self._agent_loop_round = 0
        self._agent_loop_max_rounds = 0
        self._agent_loop_ring_icon = None   # 通常監視のリング
        self._agent_loop_busy_icon = None   # 応答/実行中のリング(色違い)
        self._agent_loop_err_icon = None    # 危険停止・エラーのリング(色違い)

        # Copilot の手番の常時表示(業務PC用: Pushover を使えない環境で、Copilot が
        # いま誰の番なのかを画面だけで分かるようにする)。
        # エージェントループが回っている間も出したままにする。以前は休ませていたが、
        # ループは何分も回ることがあり、**様子を知りたい時間帯がまるごと消えていた**。
        # いまは札に周回数(🤖 3/10)が出るので、手番が tray-tools にあることは分かる。
        import copilot_watchdog as _cw
        self._copilot_watchdog = _cw.CopilotWatchdog(
            app_settings=app_settings,
            settings_path=settings_path,
            # 札の右クリックで終了されたら、バーを消したものとして扱う。
            on_child_exit=self._on_copilot_watchdog_exited,
        )
        # 子の起こし直しは、メニュー項目を作る場所で _sync_copilot_watchdog が
        # 行う(パネルの入切も材料になるので、そちらが揃ってから)。
        self._copilot_watchdog_action = None  # メニュー項目(後で作る)

        # Teams の一人会議の立ち上げ(teams_meeting.py)。UIA は常駐の中で使えないので
        # (pycaw と同居すると即死する。copilot_watchdog.py 冒頭)、実処理は子プロセス。
        # ここが持つのは子の参照と「テーマを聞いている最中か」だけ。
        self._teams_meeting_proc = None
        self._teams_meeting_asking = False
        # 子の読み役スレッドからの知らせをメインスレッドで受ける橋。参照を持たないと
        # GC で消え、シグナルの接続ごと失われる。
        self._teams_meeting_bridge = _TeamsMeetingBridge()
        self._teams_meeting_bridge.event.connect(self._on_teams_meeting_event)

        # マウスジグラー。「時限で有効化 → 残り時間を出す → 時間が来たら自動解除」は
        # スリープ抑止とまったく同じ形なので、メニューの組み立ても状態の持ち方も揃えてある。
        # ただしタイマーは2本要る(締切用と、入力を送る周期用)。抑止側はOSに状態を1回
        # 申告するだけで周期の概念が無いため、1本にまとめると片方にしか無い都合を
        # 両方へ持ち込むことになる。無理に共通化せず、素直に並べて置く。
        self._jiggle_active = False
        self._jiggle_minutes = None  # None = 無期限
        self._jiggle_deadline = None
        self._jiggle_expire_timer = QTimer()
        self._jiggle_expire_timer.setSingleShot(True)
        self._jiggle_expire_timer.timeout.connect(self._on_jiggle_expired)
        # 送信用は繰り返し。QTimerは作ったスレッドのイベントループで動くので、
        # メインスレッドで作る=メインスレッドで回る(SendInput自体に縛りは無いが、
        # 一時スレッドから始めても回らないのでここで持つ)。
        self._jiggle_timer = QTimer()
        self._jiggle_timer.timeout.connect(self._on_jiggle_tick)

        # 「ファイルの方が新しい」表示(あっふのタイトルと同じ考え方)。起動したときの版を
        # 覚えておき、メニューを開いたときと数分ごとに、ディスクの版と比べる。
        # quick_version は .git を数ファイル読むだけ(git コマンドは叩かない)なので軽い。
        self._boot_version = _read_disk_version()
        self._disk_newer = False
        self._version_timer = QTimer()
        self._version_timer.timeout.connect(self._check_disk_version)
        self._version_timer.start(VERSION_CHECK_INTERVAL_MS)

        # Claude Code のセッションの状態。フックが書いたファイルを数秒ごとに読む
        # (フックから常駐へ知らせる口は増やさない。CLAUDE.md「外から叩ける口」)。
        self._claude_entries = []
        self._claude_icons = {}
        # 処理中のセッションを並べる進捗パネル(claude_progress)。出すときに作る。
        self._claude_progress = None
        self._claude_progress_enabled = bool(
            claude_progress.load_state().get("enabled", True))
        self._claude_timer = QTimer()
        self._claude_timer.timeout.connect(self._poll_claude_status)
        self._claude_timer.start(CLAUDE_STATUS_INTERVAL_MS)

        # フォルダ監視移動(folder_watch.py)。ルールごとに QTimer を1本持つ。
        # FolderWatcher(安定判定のための前回署名)は「元→先」の鍵で使い回す:
        # 間隔や除外条件を編集したときに作り直すと「初見」の記録が消えて、
        # 移動が1周期ぶん遅れるだけだが、編集のたびに起きるのは避けたい。
        # タイマーの組み立てはメニューが出来てから _rebuild_folder_watch で行う。
        self._folder_watch_timers = []
        self._folder_watchers = {}
        self._folder_watch_warned = set()
        self._folder_watch_dialog = None

        hotkey_config = app_settings.get("hotkeys", {})
        screen_settings = app_settings.get("screen", {})
        self._awake_choices = screen_settings.get("keep_awake_minutes", [30, 120])
        self._jiggle_choices = screen_settings.get("jiggler_minutes", [30, 120])
        self._jiggle_interval_ms = int(
            _positive_number(screen_settings.get("jiggler_interval_seconds"), 60) * 1000
        )
        self._jiggle_idle_seconds = _positive_number(
            screen_settings.get("jiggler_idle_seconds"), 30
        )

        self._normal_icon = QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()
        self.tray_icon = QSystemTrayIcon(self._normal_icon)

        # 項目名の先頭に絵文字を1つ置く。通知領域のメニューは項目が縦に並ぶだけで
        # 手掛かりが少ないため、目的の行を色と形で拾えるようにしている。
        # ホットキー表記は _with_hotkey が末尾に足すので、絵文字込みのラベルを渡す。
        # 束ね方の考え: よく使うもの(定型文・ブックマーク・画面ミラー)はトップに残し、
        # それ以外は性質ごとにサブメニューへ畳む。全部畳むと頻度の高い操作が一段深く
        # なって遅くなるし、全部並べるとトップが20項目を超えて目的の行を探せなくなる。
        # ホットキーのある項目は畳んでも実害が小さい(そちらから呼べるため)。
        self.menu = QMenu()

        capture_menu = self.menu.addMenu("📷 キャプチャ")
        capture_menu.addAction("今すぐ", lambda: self.start_capture(0))
        capture_menu.addAction("5秒後", lambda: self.start_capture(5))
        capture_menu.addAction("10秒後", lambda: self.start_capture(10))

        tools_menu = self.menu.addMenu("🔧 画面の道具")
        tools_menu.addAction(
            self._with_hotkey("💧 カラーピッカー", hotkey_config.get("color_picker")),
            self.start_color_picker,
        )
        tools_menu.addAction("📏 画面定規", self.start_ruler)
        tools_menu.addAction(
            self._with_hotkey(
                "📌 このウィンドウを最前面に固定", hotkey_config.get("always_on_top")
            ),
            self.toggle_always_on_top,
        )

        self.menu.addSeparator()
        # ここから3つはトップに残す。押す頻度が高く、階層を挟むと明らかに遅くなる。
        self.menu.addAction(
            self._with_hotkey("📋 定型文", hotkey_config.get("snippet_picker")),
            self.start_snippet_picker,
        )
        # 定型文(クリップボードの書式を落とす口もここ)の隣に置く。どちらも
        # 「クリップボードの中身を整えて戻す」道具なので、探す場所を揃える。
        self.menu.addAction(
            self._with_hotkey("🪄 背景を透過…", hotkey_config.get("bg_remove")),
            self.start_bg_remove,
        )
        # フォルダブックマークはメニューに出さない(Win+J とあふｗの Alt+J から呼ぶ。
        # メニューから呼ぶことはほぼ無く、隣の「あっふ」と同じ 📁 で紛らわしい)。
        # 機能とホットキーは残してあり、ファイラの J もこのブックマークをそのまま使う。
        # あっふ の項目は、他の項目と同じく絵文字つきの通常の項目にする
        # (本人の指定で 📁)。専用アイコンを行に描く試みは、どう調整しても
        # 絵文字の列と揃わず実機で不評だったためやめた。専用アイコンは
        # 窓とタスクバー(ピン)で使う。
        self.menu.addAction(
            self._with_hotkey("📁 あっふ", hotkey_config.get("filer")),
            self.start_filer,
        )
        # 統合ピッカー。名前を打って実行する道具なので、あっふの隣に置く
        # (フォルダブックマークと登録を一本化してあり、行き来が近いため)。
        self.menu.addAction("⌨ コマンドランチャ", lambda: self.start_launcher())
        # 内蔵テキストエディタ(秀丸の代替)。あっふの隣に置く(ファイルを開く
        # 行き来が近いため)。窓は1つだけ持ち回す(start_text_editor)。
        self.menu.addAction("📝 EDDy", self.start_text_editor)
        # 明日の自分へメモ(昨日の自分から今日の自分への手紙)。就業前に書くのが
        # 本命なので、環境2・3(職場PC)でも出す(ブリッジ等の環境ガードには入れない)。
        self.menu.addAction("🌅 明日の自分へメモ", self.open_tomorrow_memo)
        # スクリプト実行パネル(script_runner.py)。ダウンロードフォルダに
        # 届いたスクリプトをチェックして F5 で実行し、結果をクリップボード
        # へ入れる。窓は常駐とは別のプロセス(script_runner_launch)。
        self.menu.addAction("🐍 スクリプト実行", self.start_script_runner)

        # 画面に重ねるプレゼン支援は、メニューには出さない。画面ミラーを使うように
        # なってからは、レーザーもスポットも「手元のツールバー」から押すのが自然で、
        # トレイまで戻る場面が無くなったため。機能自体は残してあり、ホットキー
        # (ctrl+alt+l/o/b/w)とミラー中のツールバーから使える。
        #
        # 項目(QAction)は作る。チェック状態を持たせておかないと、ホットキーで
        # 切り替えたときの「いま点いているか」を他から引けなくなるため。メニューへ
        # 追加していないので画面には出ない。
        self._presenter_menu = None
        self._presenter_actions = {}
        for kind, label, hotkey_name in (
            ("laser", "🔴 レーザーポインタ", "presenter_laser"),
            ("spotlight", "💡 スポットライト", "presenter_spotlight"),
            ("black", "⬛ 黒画面", "presenter_blackout"),
            ("white", "⬜ 白画面", "presenter_whiteout"),
        ):
            action = QAction(self._with_hotkey(label, hotkey_config.get(hotkey_name)), self.menu)
            action.setCheckable(True)
            # addAction(テキスト, 関数) で登録すると関数は引数なしで呼ばれる。チェック
            # 状態を受け取る項目はこの形で繋ぐこと(タスクバーウィジェットと同じ理由)。
            action.triggered.connect(lambda checked, k=kind: self._set_presenter_overlay(k, checked))
            self._presenter_actions[kind] = action

        # 発表者ツール系はサブメニューへ。画面ミラーだけはトップに残す(いちばん使う)。
        presenter_menu = self.menu.addMenu("📽 発表者ツール")
        presenter_menu.addAction("📽 ローカルの資料を開く", self.start_presenter)
        presenter_menu.addAction("🌐 サイトを取り込んで開く", self.start_web_presenter)

        # 画面ミラー。トップに置く(発表中いちばん使う入口なので畳まない)。
        # 「開始」とミラー先のモニタ選択でサブメニューにする。
        self._mirror_menu = self.menu.addMenu("🖥 画面ミラー（別モニタへ）")
        # 「開始」はチェック項目にしない。Ctrl+Alt+P は開始と範囲の選び直しで、押しても
        # 終わらないため——チェックを外す操作＝終了に見えてしまう。動作中かどうかは
        # 下の「終了」「静止」が押せるかどうかと、見出しの文言で示す(_refresh_mirror_menu)。
        self._mirror_action = self._mirror_menu.addAction(
            self._with_hotkey("▶ 範囲を選んで開始", hotkey_config.get("screen_mirror"))
        )
        self._mirror_action.triggered.connect(lambda _checked=False: self.start_screen_mirror())
        self._mirror_freeze_action = self._mirror_menu.addAction(
            self._with_hotkey("⏸ 静止（一時停止）", hotkey_config.get("screen_mirror_freeze"))
        )
        self._mirror_freeze_action.setCheckable(True)
        # addAction(テキスト, 関数) で登録すると関数は引数なしで呼ばれる。チェック状態を
        # 受け取る項目はこの形で繋ぐこと(プレゼン支援・タスクバーと同じ理由)。
        self._mirror_freeze_action.triggered.connect(self._set_screen_mirror_freeze)
        # 静止に加えて手元の道具も畳む(左下に再開ボタンだけ残る)。検証の合間に
        # 手元で別の作業をするとき用。文面は状態で切り替える(_refresh_mirror_menu)。
        self._mirror_minimize_action = self._mirror_menu.addAction(
            "⏬ 一時停止して最小化"
        )
        self._mirror_minimize_action.triggered.connect(
            lambda _checked=False: self._toggle_screen_mirror_minimized()
        )
        # ホットキーが「選び直し」になったぶん、終わらせる手段をここに必ず置く
        # (手元のツールバーの ✕ と Ctrl+Alt+Q でも終われる)。
        self._mirror_stop_action = self._mirror_menu.addAction(
            self._with_hotkey("⏹ 終了", hotkey_config.get("screen_mirror_stop"))
        )
        self._mirror_stop_action.triggered.connect(lambda _checked=False: self.stop_screen_mirror())
        self._mirror_screen_menu = self._mirror_menu.addMenu("🖵 ミラー先")
        # 中身は開く直前に作り直す(モニタは抜き差しされる)。項目はメニューが持つので
        # GCの心配は無いが、作り直しの前後で状態を見るためにここでも並びを持っておく。
        self._mirror_screen_actions = []
        # 設定の保存・呼び出し(名前を付けた範囲+ミラー先)。中身は保存のたびに
        # 変わるので、ミラー先と同じく開く直前に作り直す(_refresh_mirror_menu)。
        self._mirror_menu.addSeparator()
        self._mirror_save_scene_action = self._mirror_menu.addAction("💾 現在の設定を保存…")
        self._mirror_save_scene_action.triggered.connect(
            lambda _checked=False: self._save_mirror_scene())
        self._mirror_scene_menu = self._mirror_menu.addMenu("📂 保存した設定")
        self._mirror_scene_actions = []
        self._mirror_scene_delete_menu = self._mirror_menu.addMenu("🗑 設定を削除")
        self._mirror_scene_delete_actions = []

        # 写真のスライドショー(slideshow.py)。窓は常駐とは別のプロセス(slideshow_launch)。
        # 画面ミラーの隣に置く: どちらも「別のモニタに全画面で出す」もので、モニタの選び方も
        # 同じ(QScreen.name() を覚える。外れていれば主モニタ)。状態を持つ機能だが、
        # アイコンは増やさない(動いているかは開く直前に見て「停止」の可否で示す)。
        self._slideshow_menu = self.menu.addMenu("🖼 スライドショー")
        self._slideshow_menu.addAction(
            "▶ 開始", lambda: self.start_slideshow())
        self._slideshow_stop_action = self._slideshow_menu.addAction(
            "⏹ 停止", self.stop_slideshow)
        self._slideshow_menu.addSeparator()
        self._slideshow_menu.addAction(
            "📂 フォルダと表示の設定…", self.open_slideshow_settings)
        self._slideshow_screen_menu = self._slideshow_menu.addMenu("🖵 表示するモニタ")
        self._slideshow_screen_actions = []
        self._slideshow_dialog = None
        self._slideshow_menu.aboutToShow.connect(self._refresh_slideshow_menu)

        # 窓の配置(window_layout.py)。モニタの入力を他 PC へ切り替えたときに
        # 見えない画面へ残った窓を、カーソルのある画面へ集める。配置プリセットは
        # 選んだ窓の位置と大きさを手動で出し入れする(自動適用はしない)。
        self._gather_undo = []
        self._window_layout_menu = self.menu.addMenu("🪟 窓の配置")
        self._window_layout_menu.addAction("🎯 この画面に集める", self.gather_windows_here)
        self._undo_gather_action = self._window_layout_menu.addAction(
            "↩ 集めた窓を元に戻す", self.undo_gathered_windows)
        self._presets_menu = self._window_layout_menu.addMenu("📐 配置プリセット")
        self._presets_menu.aboutToShow.connect(self._refresh_presets_menu)
        self._window_layout_menu.aboutToShow.connect(self._refresh_window_layout_menu)

        # フォルダ監視移動(folder_watch.py)。常駐のタイマーで特定フォルダを見て、
        # 入ってきたファイルを別のフォルダへ移す。アイコンは増やさず、状態は
        # メニューの見出し(有効ルール数)と status で示す。
        self._folder_watch_menu = self.menu.addMenu("📥 フォルダ監視移動")
        self._folder_watch_menu.addAction("▶ 今すぐチェック", self.check_folder_watch_now)
        self._folder_watch_rules_menu = self._folder_watch_menu.addMenu("📋 ルール（有効／無効）")
        self._folder_watch_rule_actions = []
        self._folder_watch_menu.addSeparator()
        self._folder_watch_menu.addAction(
            "⚙ ルールの追加・編集…", self.open_folder_watch_settings)
        self._folder_watch_menu.aboutToShow.connect(self._refresh_folder_watch_menu)

        # Copilot まわりのメニュー。中身は性質の違う2つで、区切り線で分けてある。
        #   上: 状態監視バー   … 見るだけ。Copilot の手番を画面に出す
        #   下: エージェントループ … tray-tools が応答を引き取ってコードまで実行する
        #
        # 【親を「エージェントループ」にしない理由】
        # 状態監視バーはループと関係なく単体で使える機能なのに、親がループだと
        # 下位機能に見えてしまう。加えて、以前は「監視モード開始」という項目が
        # 並んでいて「監視」が2つの別物を指していた。親を Copilot にして、
        # ループ側は「エージェントループ」と名乗らせることで重複を解いた。
        # 一人会議の立ち上げ。トップに置く: 続けて何度も使う(毎回新しい会議を立てる)
        # 操作で、Copilot に渡すための前段なので Copilot の傘のすぐ上に並べる。
        # サブメニューに入れないのは、Copilot の傘の中身(状態監視・エージェントループ)
        # とは相手のアプリも性質も違うため。
        self.menu.addAction(
            self._with_hotkey("🎙 一人会議を開始…", hotkey_config.get("teams_meeting")),
            lambda: self.start_teams_meeting("menu"),
        )
        self._agent_loop_menu = self.menu.addMenu(AGENT_LOOP_MENU_TITLE)
        # 状態監視バーを出すかどうか。表示だけの入切で、監視は止めない
        # (監視の生死は _sync_copilot_watchdog がバー/パネルの表示から決める)。
        self._copilot_watchdog_action = self._agent_loop_menu.addAction(
            "🏷 状態監視バーを表示"
        )
        self._copilot_watchdog_action.setCheckable(True)
        self._copilot_watchdog_action.setChecked(self._copilot_watchdog.bar_enabled())
        # addAction(テキスト, 関数) だと checked が渡らないので triggered.connect で受ける。
        self._copilot_watchdog_action.triggered.connect(self._toggle_copilot_bar)
        self._sync_copilot_watchdog()

        self._agent_loop_menu.addSeparator()
        # ここから下は「tray-tools が自分でコードを実行する」側。押すと何が起きるかを
        # 項目名に入れておく(危険パターンで止まる作りとはいえ、実行はする)。
        self._agent_loop_start_action = self._agent_loop_menu.addAction(
            "▶ ループを始める（これから投稿する）",
            lambda _checked=False: self.start_agent_loop_watch(),
        )
        # 始めかたは2つある。混ぜずに別項目で出す(どちらを望むかは押した人にしか
        # 分からない。気を利かせて最後の回答を拾うようにしていた時期があり、前の
        # やり取りが残っている状態で押した瞬間に走り出して意図せず10周回った)。
        self._agent_loop_start_last_action = self._agent_loop_menu.addAction(
            "▶ ループを始める（直前の回答から）",
            lambda _checked=False: self.start_agent_loop_watch(take_last=True),
        )
        self._agent_loop_stop_action = self._agent_loop_menu.addAction(
            "⏹ エージェントループを止める",
            lambda _checked=False: self.stop_agent_loop(),
        )
        self._agent_loop_show_log_action = self._agent_loop_menu.addAction(
            "📜 実行ログを開く",
            lambda _checked=False: self._show_agent_loop_log(),
        )
        # 初期状態を反映
        self._refresh_agent_loop_menu()

        # Claude Code の各セッションの状態(hooks/claude_hook.py が書く)。中身は開くたびに作る。
        self._claude_menu = self.menu.addMenu(CLAUDE_MENU_TITLE)
        self._claude_menu.aboutToShow.connect(self._rebuild_claude_menu)

        # 実機確認チェックリスト (家サーバが正本)。ブリッジと同じ作法で
        # 環境2・3では出さない (未設定は環境1扱い)。
        if self.app_settings.get("environment") not in (2, 3):
            self.menu.addAction("✅ 実機確認チェックリスト", self._open_checklist)

        self.menu.addSeparator()
        # 電源まわりをひとつの傘に。狙いが近い(席を外す・寝かせる・寝させない)ので、
        # 探すときに同じ場所を見れば済む。
        power_menu = self.menu.addMenu("⚡ 電源")

        self._awake_menu = power_menu.addMenu("☕ スリープ抑止")
        self._awake_actions = {}
        for minutes in self._awake_choices:
            action = self._awake_menu.addAction(
                _format_minutes(minutes), lambda m=minutes: self._enable_keep_awake(m)
            )
            action.setCheckable(True)
            self._awake_actions[minutes] = action
        action = self._awake_menu.addAction("無期限", lambda: self._enable_keep_awake(None))
        action.setCheckable(True)
        self._awake_actions[None] = action
        self._awake_menu.addSeparator()
        # 並んでいる長さで足りないとき用。有効中に選び直せば、そこから数え直す。
        self._awake_menu.addAction("時間を指定…", self._ask_keep_awake_minutes)
        self._awake_menu.addAction("解除", self._disable_keep_awake)

        # スリープ抑止の下に、逆向きの操作(寝かせる)を置く。電源まわりでひとかたまり。
        self._sleep_menu = power_menu.addMenu("😴 スリープ・休止")
        for hibernate, head in ((False, "😴 スリープ"), (True, "💤 休止状態")):
            sub = self._sleep_menu.addMenu(head)
            for minutes in (0, 5, 30, 60):
                label = "今すぐ" if minutes == 0 else _format_minutes(minutes) + "後"
                sub.addAction(
                    label,
                    lambda m=minutes, h=hibernate: self.schedule_sleep(m * 60, h),
                )
            sub.addAction(
                "時間を指定…", lambda h=hibernate: self._ask_sleep_minutes(h)
            )
        self._sleep_menu.addSeparator()
        self._sleep_cancel_action = self._sleep_menu.addAction("予約を取り消す", self.cancel_sleep)

        # スリープ抑止のすぐ下に置く。狙いが近い(席を外しても切れないようにする)ので、
        # 探すときに同じ場所を見れば済むようにしている。
        self._jiggle_menu = power_menu.addMenu("🖱 マウスジグラー")
        self._jiggle_actions = {}
        for minutes in self._jiggle_choices:
            action = self._jiggle_menu.addAction(
                _format_minutes(minutes), lambda m=minutes: self._enable_jiggler(m)
            )
            action.setCheckable(True)
            self._jiggle_actions[minutes] = action
        action = self._jiggle_menu.addAction("無期限", lambda: self._enable_jiggler(None))
        action.setCheckable(True)
        self._jiggle_actions[None] = action
        self._jiggle_menu.addSeparator()
        self._jiggle_menu.addAction("解除", self._disable_jiggler)

        self.menu.addSeparator()
        # 設定まわりをひとつの傘に。日々押すものではないので、トップから畳んでよい。
        config_menu = self.menu.addMenu("⚙ 設定")

        # 通知領域はプライマリのタスクバーにしか出ないので、各ディスプレイのタスクバーへ
        # 自前で置く出張所の表示切替。詳細は taskbar_widget.py 冒頭を参照。
        self._taskbar_action = config_menu.addAction("🖥 タスクバーウィジェット")
        self._taskbar_action.setCheckable(True)
        # addAction(テキスト, 関数) は関数を引数なしで呼ぶので、チェック状態を受け取る
        # 項目をその形で繋いではいけない(クリックのたびにTypeErrorで落ちる)。
        self._taskbar_action.triggered.connect(self._toggle_taskbar_widget)
        self._taskbar_bg_action = config_menu.addAction(
            "🎨 背景色を取り直す", self._recapture_taskbar_background
        )

        # スマホへのプッシュ通知(Pushover)。送るのは外からの IPC コマンドだけで、
        # ここに置くのは登録・削除の口だけ。トークンは settings.json には書かず
        # Windows の資格情報マネージャへ預ける(pushover.py 冒頭を参照)。
        # 送信スレッドからの結果を受ける橋。参照を持たないとGCで消え、シグナルの
        # 接続ごと失われる。
        self._pushover_bridge = _PushoverBridge()
        self._pushover_bridge.finished.connect(self._on_pushover_tested)

        # エージェントループの状態変化(agent_loop.run_loop ワーカースレッドから)を
        # メインスレッドで受ける橋。参照を持たないとGCで消え、シグナル接続ごと失われる。
        self._agent_loop_bridge = _AgentLoopBridge()
        self._agent_loop_bridge.state_changed.connect(self._on_agent_loop_state_event)

        # ここに以前 _agent_loop_menu などを None で初期化する行があったが、消した。
        # メニューを組み立てるのはこれより前(上の addMenu)なので、あとから None を
        # 入れると作ったばかりの参照を捨てることになる。_refresh_agent_loop_menu は
        # None なら黙って何もしない作りなので、見出しの状態表示も開始/停止の
        # 有効切り替えも、ずっと効いていなかった。
        # このPCで使う気があるときだけメニューに出す。複数のPCで同じコードを動かして
        # おり、業務用の端末に個人の通知先の入口が並んでいても使い道がない。
        #
        # 判定は「トークンを登録済み」か「設定 tools.pushover が true」。前者だけだと
        # 未登録のPCで登録する手段が無くなるので、設定で明示する道を残してある
        # (書いて再起動すれば出る)。
        self._pushover_menu = None
        self._pushover_register_action = None
        self._pushover_test_action = None
        self._pushover_delete_action = None
        if self._pushover_wanted():
            self._pushover_menu = config_menu.addMenu("📱 スマホ通知（Pushover）")
            self._pushover_register_action = self._pushover_menu.addAction(
                "🔑 トークンを登録…", self._register_pushover
            )
            self._pushover_test_action = self._pushover_menu.addAction(
                "📤 テスト送信", self._test_pushover
            )
            self._pushover_delete_action = self._pushover_menu.addAction(
                "🗑 登録を削除", self._delete_pushover
            )
            self._pushover_menu.aboutToShow.connect(self._refresh_pushover_menu)

        config_menu.addSeparator()
        # 定型文のフォルダを開く口はここ。定型文そのものはトップに置いてあるが、
        # フォルダを開くのは「中身を書き換えるとき」だけなので設定側が似合う。
        config_menu.addAction("📂 定型文フォルダを開く", lambda: snippets.open_folder())
        config_menu.addAction("📝 settings.json を開く", self._open_settings_file)

        self.menu.addSeparator()
        # 落ちた手掛かりをまとめて、テキストと QR で出す(diagnostics.py)。業務PCから
        # データを持ち出せないので、スマホで読み取ってもらうためのもの。まれにしか
        # 使わないのでホットキーは付けない。
        self.menu.addAction("🩺 診断…", self.open_diagnostics)
        self._restart_action = self.menu.addAction("🔄 再起動", self._restart)
        self.menu.addAction("✖ 終了", QApplication.instance().quit)

        # 残り時間は開くたびに変わるので、表示直前に作り直す
        self.menu.aboutToShow.connect(self._check_disk_version)
        self.menu.aboutToShow.connect(self._refresh_awake_menu)
        self.menu.aboutToShow.connect(self._refresh_jiggle_menu)
        self.menu.aboutToShow.connect(self._refresh_taskbar_menu)
        # プレゼン支援はホットキーやクリック(黒画面)でも切り替わるので、メニューの
        # チェックは開く直前に実物へ合わせる。
        self.menu.aboutToShow.connect(self._refresh_presenter_menu)
        self.menu.aboutToShow.connect(self._refresh_mirror_menu)

        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self._on_activated)
        self._refresh_state()
        self.tray_icon.setVisible(True)

        # 抑止したまま/固定したままアプリを終わらせると、解除する手段が無くなる
        QApplication.instance().aboutToQuit.connect(self._on_quit)

        # 待機役は起動時にも起こしておく。「付箋を使った直後」だけにすると、その日の
        # 1枚目——いちばん待たされたと感じる1枚——が必ず従来どおりの355msになる。
        if self._prewarm_enabled():
            self._prewarm_timer.start(PREWARM_STARTUP_DELAY_MS)

        # 画面取得(mss の実体)も先に作っておく。初回のキャプチャで生成コスト
        # (GDI 関数の解決とモニタ列挙)を払うと、押してから撮れるまでが目に見えて
        # 遅れる。待機役を起こすのと同じだけ遅らせて、起動直後の混雑を避ける。
        QTimer.singleShot(PREWARM_STARTUP_DELAY_MS, capture_grab.prewarm_sct)

        # フォルダ監視移動のタイマーを組み立てる(enabled なルールだけ自動で始まる)。
        self._rebuild_folder_watch()

        # 明日の自分へメモ: まだ見ていないメモがあれば、起動が落ち着いてから
        # 表示用に出す(昨日の自分からの手紙)。
        QTimer.singleShot(
            tomorrow_memo.STARTUP_SHOW_DELAY_MS, self._show_unseen_tomorrow_memo)

    def hotkeys(self) -> dict:
        return {
            "capture_now": lambda: self.start_capture(0),
            "capture_sequence": self.capture_sequence,
            "color_picker": self.start_color_picker,
            "always_on_top": self.toggle_always_on_top,
            "snippet_picker": self.start_snippet_picker,
            "bg_remove": self.start_bg_remove,
            "launcher": lambda: self.start_launcher(),
            "filer": self.start_filer,
            "cursor_finder": self.find_cursor,
            # レーザーとスポットライトはマウスを透過する＝自分ではキーもマウスも
            # 受け取れない。ここが実質唯一の畳む手段なので、必ず登録しておくこと。
            "presenter_laser": lambda: self.toggle_presenter_overlay("laser"),
            "presenter_spotlight": lambda: self.toggle_presenter_overlay("spotlight"),
            "presenter_blackout": lambda: self.toggle_presenter_overlay("black"),
            "presenter_whiteout": lambda: self.toggle_presenter_overlay("white"),
            # 画面ミラー。開始と「範囲の選び直し」。ミラー窓はキーを受け取らない
            # (前面を奪わない作りなので)ため、終わらせるのは screen_mirror_stop と
            # トレイメニューと手元のツールバー。
            "screen_mirror": self.start_screen_mirror,
            "screen_mirror_stop": self.stop_screen_mirror,
            "screen_mirror_freeze": self.toggle_screen_mirror_freeze,
            # Teams の一人会議を立ち上げる(テーマを聞いてから子プロセスで進める)。
            "teams_meeting": lambda: self.start_teams_meeting("hotkey"),
        }

    @staticmethod
    def _with_hotkey(label: str, combo) -> str:
        return f"{label} ({combo})" if combo else label

    def _notify(self, title: str, message: str) -> None:
        show_toast(f"{title}\n{message}")

    def _on_activated(self, reason):
        # 中クリック(MiddleClick)で即キャプチャ。左クリックだと通知領域を触ったときに
        # 意図せず撮ってしまうので、押し間違えの少ない中ボタンに寄せている。
        if reason == QSystemTrayIcon.MiddleClick:
            self.start_capture(0)

    # ---------------------------------------------------------------
    # 範囲キャプチャ
    # ---------------------------------------------------------------
    def start_capture(self, delay_seconds: int):
        # ホットキー・トレイの中クリック・メニューと入口が3つあるため二重起動しやすい。
        # ガードが無いと self.overlay が上書きされ、前のオーバーレイが参照を失って
        # 全画面に貼り付いたまま残る(マウス操作を奪う)。色/定規と同じ作法で弾く。
        if self.overlay is not None or self.countdown is not None:
            return
        # 遅延キャプチャは「待ってから画面を凍結し、その静止画の上で範囲を選ぶ」。
        # カウントダウン中にメニューやツールチップを開いておけば、時間が来た瞬間の絵が
        # 固定されるので、選択操作でそれらが消えても撮れる。
        delay = max(int(delay_seconds or 0), 0)
        if delay > 0:
            self.countdown = CountdownOverlay(delay)
            self.countdown.finished.connect(self._on_countdown_finished)
            self.countdown.show()
            return
        self._show_overlay()

    def _on_countdown_finished(self):
        self.countdown.close()
        self.countdown.deleteLater()
        self.countdown = None
        # カウントダウン表示が凍結画像に写り込む。閉じた直後はOSがまだ再描画し切って
        # いないことがあるので、少し待ってから撮る(再キャプと同じ理由)。
        QTimer.singleShot(150, self._show_overlay)

    def _show_overlay(self):
        # 遅延なしでも凍結する。動画などが動いていても選択中に絵が変わらず、挙動が揃う。
        self.overlay = FrozenSelectionOverlay()
        self.overlay.selection_made.connect(self._on_selection_made)
        self.overlay.canceled.connect(self._on_canceled)
        self.overlay.show()

    def pick_region_for_capture(self, on_done, timeout_ms: int) -> bool:
        """外部(MCP の capture --select)用。範囲選択だけして、付箋は出さずに結果を返す。

        ホットキーと同じ FrozenSelectionOverlay を使う(ドラッグ選択もクリックのウィンドウ
        単位も同じ)。on_done(image, rect) を1回だけ呼ぶ。キャンセル・時間切れは (None, None)。
        既に範囲選択中(または遅延カウントダウン中)なら何もせず False を返す。"""
        if self.overlay is not None or self.countdown is not None:
            return False
        overlay = FrozenSelectionOverlay()
        self.overlay = overlay
        finished = []  # 1回だけ呼ぶための印

        def finish(image, rect):
            if finished:
                return
            finished.append(True)
            timer.stop()
            try:
                overlay.close()
                overlay.deleteLater()
            finally:
                self.overlay = None
            on_done(image, rect)

        def on_selected(rect_global):
            # 撮り直さず、ユーザーが見ていた凍結画像から切り出す(_on_selection_made と同じ)。
            finish(overlay.crop(rect_global), rect_global)

        timer = QTimer(overlay)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: finish(None, None))
        overlay.selection_made.connect(on_selected)
        overlay.canceled.connect(lambda: finish(None, None))
        timer.start(timeout_ms)
        overlay.show()
        return True

    def _on_canceled(self):
        # SelectionOverlayはEscでcanceledをemitするだけで自分では閉じない。参照を
        # 捨てるだけだと全画面を覆うウィジェットが消えるかどうかGC任せになり、
        # マウス操作を奪ったまま残り得る。close()で明示的に閉じ、シグナル発火中の
        # 即時破棄を避けるためdeleteLater()で後始末する。
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None

    def _on_selection_made(self, rect_global: QRect):
        # 所要時間の計測(rapture_timing.log)。選んだ時刻を付箋側へも渡し、付箋が描き終わる
        # までを通しで測る。「数秒かかることがある」の原因を見分けるため(2026-11-04)
        timing = {"t0_ms": rapture_timing.now_ms()}
        # 撮り直さず凍結画像から切り出す。参照を捨てる前に取り出しておく。
        image = self.overlay.crop(rect_global)
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None
        timing["cropped_ms"] = rapture_timing.now_ms()

        # 付箋1枚が1つの連番セッション。撮影直後の自動保存がそのセッションの1枚目になるよう、
        # 先にファイル名の頭を決めてから付箋へ引き継ぐ(rapture_20260823_140919-001.png)。
        # 保存に失敗したときは連番0のまま(バッジも出さない)で付箋だけ開く。
        session_stem = new_session_stem()
        saved = save_image(  # 素の画像を自動保存
            image, self.app_settings.get("capture", {}), stem=session_stem, index=1
        )
        timing["saved_ms"] = rapture_timing.now_ms()
        self._open_capture_window(
            image,
            rect_global.topLeft(),
            session_stem=session_stem,
            session_index=1 if saved else 0,
            timing=timing,
        )
        try:
            self._write_capture_timing(image, session_stem, saved, timing)
        except Exception:  # noqa: BLE001  計測の失敗でキャプチャを止めない
            pass

    @staticmethod
    def _write_capture_timing(image, session_stem, saved, timing: dict) -> None:
        """本体側の所要時間を rapture_timing.log へ1行。付箋側の行とは session_stem で対応する。"""
        t0 = timing["t0_ms"]
        size = image.size()
        rapture_timing.write("本体", session_stem, {
            "経路": timing.get("route", "?"),
            "画像": f"{size.width()}x{size.height()}",
            "切出": rapture_timing.span(t0, timing.get("cropped_ms")),
            "保存": rapture_timing.span(timing.get("cropped_ms"), timing.get("saved_ms")),
            "受渡PNG": rapture_timing.span(timing.get("png_start_ms"), timing.get("png_done_ms")),
            "送信": rapture_timing.span(timing.get("png_done_ms"), timing.get("sent_ms")),
            "本体合計": rapture_timing.span(t0, timing.get("sent_ms")),
            "保存先": str(saved.parent) if saved else "失敗",
        })

    def _prewarm_enabled(self) -> bool:
        """待機役を飼うかどうか。既定は有効。

        false にすると1枚ごとにプロセスを起こす従来の形へ戻る。常時1プロセスぶん
        (専有27MB程度)のメモリを使うのが唯一の代償なので、そこを惜しむPC用の逃げ道。"""
        return bool(self.app_settings.get("capture", {}).get("prewarm", True))

    def _ensure_prewarmed(self):
        """待機役が居なければ1人起こす。

        失敗しても黙って諦めてよい。居なければ従来どおり spawn() で出すだけで、
        キャプチャができなくなることは無い(ここで通知を出すと、原因が別にある不調の
        たびにトーストが増えて邪魔になる)。"""
        if not self._prewarm_enabled():
            return
        try:
            capture_process.ensure_prewarmed(self.settings_path)
        except OSError as e:
            print(f"[rapture] 待機役を起こせません: {e}", file=sys.stderr)

    def _open_capture_window(self, image, global_pos, close_on_escape=False,
                             session_stem=None, session_index=0, timing=None):
        """付箋を1枚開く。実体はこのプロセスではなく別プロセス(capture_process.py)。

        本体と同じプロセスに置くと、本体が落ちたときはもちろん、機能追加のたびの
        再起動でも開いている付箋が全部消える。手順書を作るために連番キャプチャで
        何枚も並べている最中にそれをやられると撮り直しになるため、付箋の寿命を
        本体から切り離してある。付箋側は capture_grab と toast にしか依存しておらず、
        元から本体の状態を何も見ていないので、そのまま持ち出せた。

        道は2本ある。既定は待機役へ画像を送るだけの道(show_via_warm)で、範囲を選んでから
        付箋が出るまで実測10ms前後。待機役が居ない・死んでいる・設定で切ってあるときは
        毎回プロセスを起こす道(spawn)へ落ちる。こちらは355msかかるが確実に出る。
        速いほうが使えないせいでキャプチャが失敗する、という形には絶対にしないこと。"""
        capture_hotkey = self.app_settings.get("hotkeys", {}).get("capture_sequence")

        if self._prewarm_enabled():
            used = None
            try:
                used = capture_process.show_via_warm(
                    image,
                    global_pos,
                    settings_path=self.settings_path,
                    close_on_escape=close_on_escape,
                    session_stem=session_stem,
                    session_index=session_index,
                    capture_hotkey=capture_hotkey,
                    timing=timing,
                )
            except OSError as e:
                # 受け渡し用PNGを書けなかった場合。spawn も同じ書き出しをするので
                # まず助からないが、道が2本ある意味が無くなるので下へ落とす。
                print(f"[rapture] 待機役へ渡せません: {e}", file=sys.stderr)

            # 次の1枚のために待機役を補充する。使った待機役はもう付箋なので、
            # 補充しないと次が355msに戻る。付箋を出す合図は送り終えているので、
            # ここでプロセスを起こすぶん(10〜15ms)は表示を遅らせない。
            QTimer.singleShot(0, self._ensure_prewarmed)
            if used is not None:
                if timing is not None:
                    timing["route"] = "warm"
                return

        try:
            capture_process.spawn(
                image,
                global_pos,
                settings_path=self.settings_path,
                close_on_escape=close_on_escape,
                session_stem=session_stem,
                session_index=session_index,
                # 連番キャプチャのキーをタイトルバーに出させる。設定で変えられる値なので
                # 付箋側にハードコードさせず、ここで実値を渡す。
                capture_hotkey=capture_hotkey,
                timing=timing,
            )
            if timing is not None:
                timing["route"] = "spawn" if self._prewarm_enabled() else "spawn(prewarm無効)"
        except OSError as e:
            # 撮ったのに何も出ないと、キャプチャ自体が失敗したように見える。
            # 自動保存だけは済んでいるので、そのことが分かる言い方で知らせる。
            self._notify("Rapture", f"付箋を開けませんでした(保存は済んでいます)\n{e}")

    def capture_sequence(self):
        """ホットキー(既定 Ctrl+Alt+S): 対象の付箋の連番キャプチャを1枚進める。

        付箋の右クリックメニュー「キャプチャ＆保存」と同じ処理だが、こちらは付箋が
        アクティブでなくても効く。ブラウザ等を操作しながら「操作する → 撮る」を
        繰り返す使い方が本命で、そのとき前面にいるのは操作中のアプリなので、付箋に
        フォーカスを戻さず撮れる必要がある。付箋は常に最前面で位置を保っているため、
        非アクティブでも狙った範囲が撮れる。

        対象は別プロセス化の前と同じく「最後に作られた付箋」。違うのは覚え方で、
        以前は生成時に掴んだオブジェクトを持ち続けていたのに対し、今は毎回パイプの
        一覧から選び直す(capture_process.send_to_latest)。本体を再起動しても
        生きている付箋を見つけ直せる。

        この選び直しで挙動が1つ変わっている。以前は対象の付箋を閉じると、他の付箋が
        残っていても対象は無し(「付箋がありません」)になった。今は「生きている中で
        いちばん新しい付箋」なので、閉じると次に新しい付箋へ引き継がれる。掴んだ
        オブジェクトを覚える方式に戻さない限りこうなるし、再起動をまたいで対象を
        保てるほうが今回の目的に適うので、こちらを採った。"""
        try:
            if capture_process.send_to_latest("capture_sequence") is None:
                # 黙って無反応だとホットキーが効いていないのか壊れたのか区別が付かない。
                self._notify("Rapture", "付箋がありません")
        except OSError as e:
            # 付箋は居るのに届かなかった場合。「付箋がありません」と出すと、目の前に
            # 付箋があるユーザーに嘘をつくことになるので分けて知らせる。
            self._notify("Rapture", f"付箋へ送れませんでした\n{e}")

    # ---------------------------------------------------------------
    def start_color_picker(self):
        if self.picker is not None:
            return
        self.picker = color_picker.ColorPickerOverlay()
        self.picker.picked.connect(self._on_color_picked)
        self.picker.canceled.connect(self._close_picker)
        self.picker.show()

    def _close_picker(self):
        if self.picker is None:
            return
        self.picker.close()
        self.picker.deleteLater()
        self.picker = None

    def _on_color_picked(self, hex_color: str):
        self._close_picker()
        color_picker.copy_color(hex_color)
        self._notify("カラーピッカー", f"{hex_color} をコピーしました")

    # ---------------------------------------------------------------
    # 画面定規
    # ---------------------------------------------------------------
    def start_ruler(self):
        if self.ruler is not None:
            return
        self.ruler = screen_ruler.create_overlay()
        self.ruler.selection_made.connect(self._on_measured)
        self.ruler.canceled.connect(self._close_ruler)
        self.ruler.show()

    def _close_ruler(self):
        if self.ruler is None:
            return
        self.ruler.close()
        self.ruler.deleteLater()
        self.ruler = None

    def _on_measured(self, rect_global: QRect):
        # 始点・終点の向きは正規化済みのrect_globalには残らない。オーバーレイ本体が保持して
        # いるので、_close_ruler()で参照を捨てる前に測定結果を取り出す。
        summary = screen_ruler.copy_measurement(self.ruler)
        self._close_ruler()
        if summary:
            self._notify("画面定規", f"{summary} をコピーしました")

    # ---------------------------------------------------------------
    # 定型文
    # ---------------------------------------------------------------
    def start_snippet_picker(self):
        # 開いたウインドウはここで参照を持ち続ける(ローカル変数だけだとGCで消える)。
        if self.snippet_picker is not None:
            # 開いたまま同じホットキーを叩いたときは、開き直さず前面に呼び戻す
            self.snippet_picker.raise_()
            self.snippet_picker.activateWindow()
            return
        picker = snippets.create_picker(self.app_settings, self.settings_path)
        if picker is None:
            return  # テンプレートが1件も無い(通知は snippets 側が出している)
        picker.closed.connect(self._close_snippet_picker)
        self.snippet_picker = picker
        picker.show()

    def _close_snippet_picker(self):
        if self.snippet_picker is None:
            return
        picker = self.snippet_picker
        self.snippet_picker = None
        picker.close()
        picker.deleteLater()

    # ---------------------------------------------------------------
    # テキストエディタ(editor/。秀丸の代替の内蔵エディタ)
    # ---------------------------------------------------------------
    def start_text_editor(self, path=None):
        """内蔵テキストエディタを開く。開いていれば前面に出すだけ
        (パスが渡されていたら、そのファイルをタブで開く。既に
        タブにあれば、そのタブを前に出す)。

        窓は常駐の中で開く(あっふ のビュアと同じ)。セッション
        (控え)が有効なので、未保存の内容も閉じれば戻る。再起動の
        巻き添えには数えない(dirty_window_state が
        restart_loss_count=0 を返す)。"""
        import editor as editor_module  # 使うときに読む(起動を軽く保つ)

        if self.text_editor is not None:
            if path:
                self.text_editor.open_path(path)
            self.text_editor.raise_()
            self.text_editor.activateWindow()
            return
        section = self.app_settings.get("editor") or {}
        geometry = section.get("geometry")
        if not (isinstance(geometry, list) and len(geometry) == 4
                and all(isinstance(v, int) for v in geometry)):
            geometry = None
        window = editor_module.create_window(
            path, geometry=geometry, keymap_overrides=section.get("keymap"),
            editor_settings=section,
            on_settings_change=self._save_editor_settings,
            session_path=editor_module.default_session_path())
        window.closed.connect(self._on_text_editor_closed)
        self.text_editor = window
        window.show()

    def _save_editor_settings(self, updates):
        """エディタの表示・フォント・正規表現の変更を editor 節へ保存する
        (窓側の on_settings_change から差分だけ渡ってくる)。"""
        try:
            update_settings_section("editor", updates, self.settings_path)
            section = self.app_settings.get("editor")
            if isinstance(section, dict):
                section.update(updates)
        except Exception:  # noqa: BLE001
            self._log_failure("テキストエディタの設定の保存")

    def _on_text_editor_closed(self, geometry):
        window = self.text_editor
        self.text_editor = None
        if window is not None:
            window.deleteLater()
        try:
            update_settings_section(
                "editor", {"geometry": geometry}, self.settings_path)
            section = self.app_settings.get("editor")
            if isinstance(section, dict):
                section["geometry"] = geometry
        except Exception:  # noqa: BLE001
            self._log_failure("テキストエディタの位置の保存")

    # ---------------------------------------------------------------
    # 明日の自分へメモ(tomorrow_memo.py)
    # ---------------------------------------------------------------
    def _get_tomorrow_memo_store(self):
        if self._tomorrow_memo_store is None:
            self._tomorrow_memo_store = tomorrow_memo.TomorrowMemoStore()
        return self._tomorrow_memo_store

    def open_tomorrow_memo(self):
        """メモを書く窓を開く(トレイのメニューから)。開いていれば前面に出す。
        開けたら True。"""
        return self._show_tomorrow_memo("write") is not None

    def open_tomorrow_memo_for_call(self, on_closed):
        """外からの呼び出し(PC を落とすスクリプト)用。書く窓を開き、
        窓が閉じたら on_closed を1回呼ぶ。開けなければ False。

        窓の中身は入力のたびに自動保存されるので、常駐の再起動の
        巻き添え(restart_blockers)には数えない。"""
        if self._show_tomorrow_memo("write") is None:
            return False
        self._tomorrow_memo_waiters.append(on_closed)
        return True

    def _show_tomorrow_memo(self, mode):
        window = self.tomorrow_memo_window
        if window is None:
            window = tomorrow_memo.TomorrowMemoWindow(
                self._get_tomorrow_memo_store())
            window.closed.connect(self._on_tomorrow_memo_closed)
            self.tomorrow_memo_window = window
        if mode == "read":
            window.show_read()
        else:
            window.present_write()
        return window

    def _show_unseen_tomorrow_memo(self, today=None):
        """起動時: まだ見ていないメモがあれば表示用に出す。

        出すのは「書いた日が今日より前」で「今日が最後に表示した日より後」
        のときだけ(日付で判定する。PC の起動時刻は長期連続稼働や休止・
        高速スタートアップで当てにならないため使わない)。同じ日の再起動や
        自動反映では出直さない。today はテスト用で、省略時は今日。"""
        try:
            if self.tomorrow_memo_window is not None:
                return
            store = self._get_tomorrow_memo_store()
            if store.has_unseen_for_startup(today):
                self._show_tomorrow_memo("read")
                store.note_shown(today)
        except Exception:  # noqa: BLE001  起動時のおまけ。本体は落とさない
            self._log_failure("明日の自分へメモの表示")

    def _on_tomorrow_memo_closed(self):
        window = self.tomorrow_memo_window
        self.tomorrow_memo_window = None
        if window is not None:
            window.deleteLater()
        waiters, self._tomorrow_memo_waiters = self._tomorrow_memo_waiters, []
        for waiter in waiters:
            try:
                waiter()
            except Exception:  # noqa: BLE001  応答側の都合で落とさない
                self._log_failure("明日の自分へメモの呼び出しへの応答")

    # ---------------------------------------------------------------
    # カレンダー(縦ランチャの項目。見る専用の小窓)
    # ---------------------------------------------------------------
    def show_calendar(self):
        """カレンダーの小窓を出す。出ているときに呼ばれたら閉じる(トグル)。"""
        if self.calendar_popup is not None:
            self._close_calendar()
            return
        popup = calendar_popup.create_popup()
        popup.closed.connect(self._close_calendar)
        self.calendar_popup = popup
        popup.show_at_cursor_screen()

    def _close_calendar(self):
        if self.calendar_popup is None:
            return
        popup = self.calendar_popup
        self.calendar_popup = None
        popup.close()
        popup.deleteLater()

    # ---------------------------------------------------------------
    # 電卓(縦ランチャの項目。旧 Mac 風の小窓、calculator.py)
    # ---------------------------------------------------------------
    def show_calculator(self):
        """電卓の小窓を出す。出ているときに呼ばれたら閉じる(トグル)。"""
        if self.calculator_window is not None:
            self._close_calculator()
            return
        window = calculator.create_window()
        window.closed.connect(self._close_calculator)
        self.calculator_window = window
        window.show_at_cursor_screen()

    def _close_calculator(self):
        if self.calculator_window is None:
            return
        window = self.calculator_window
        self.calculator_window = None
        window.close()
        window.deleteLater()

    # ---------------------------------------------------------------
    # カーソルファインダー(縦ランチャの項目・ホットキー。cursor_finder.py)
    # ---------------------------------------------------------------
    def find_cursor(self):
        """カーソルの位置に波紋の輪を出す。窓は使い回し、呼ばれるたびに
        アニメーションを出し直す(隠すのは窓まかせ)。"""
        if self.cursor_finder is None:
            self.cursor_finder = cursor_finder.CursorFinderOverlay()
        self.cursor_finder.start()

    # ---------------------------------------------------------------
    # 統合ピッカー(Win+J・トレイ・縦ランチャの入口。窓は picker.PickerWindow、
    # 候補生成は unified_picker.py。旧来のコマンドパレット/コマンドランチャの
    # 窓はここに寄せた。あっふ側の J は filer 側で同じ窓を使う)
    # ---------------------------------------------------------------
    def show_unified_picker(self):
        """縦ランチャの項目から開くときの入口(start_launcher と同じ窓)。"""
        self.start_launcher()

    def _palette_es_argv(self):
        """ファイル検索用の es.exe の引数列。探すのは初回だけ(結果は保持)。"""
        if self._palette_es_cache is None:
            argv = filer_everything.find_es(None)
            if argv is None:
                found = shutil.which("es.exe")
                argv = [found] if found else False
            self._palette_es_cache = argv
        return self._palette_es_cache or None

    def _unified_file_search(self, query: str):
        """Everything でファイルを探してパスの列を返す(統合ピッカーの動的候補用)。

        es.exe が無い・Everything が起きていないときは空を返す(候補が出ないだけ)。
        打鍵のたびに同期で呼ばれるので、時間切れは短めにし、一度時間切れしたら
        この窓を開いている間はもう試さない(打ち込みが引っかかり続けるため)。"""
        if self._unified_es_dead:
            return []
        argv = self._palette_es_argv()
        if argv is None:
            self._unified_es_dead = True
            return []
        result = filer_everything.run_search(argv, query, None, limit=5,
                                              timeout=1.5)
        if result.status == "timeout":
            self._unified_es_dead = True
            return []
        if result.status not in ("done", "limit"):
            return []
        return list(result.paths)

    def _record_unified_use(self, data):
        """統合ピッカーでの実行を学習に残す。

        frecency は command_palette の履歴(機能・アプリ・ファイル・Web検索)へ、
        入力の履歴は command_launcher の履歴(登録の名前で再実行できる形)へ。
        学習は添え物なので、失敗しても実行そのものは邪魔しない。"""
        try:
            kind = data.get("kind")
            if kind in ("feature", "app", "file", "web"):
                if kind == "feature":
                    cand = command_palette.Candidate(
                        "feature", "", "", {"item_key": data.get("item_key")})
                elif kind == "web":
                    cand = command_palette.Candidate(
                        "web", "", "", {"service": data.get("service")})
                else:
                    cand = command_palette.Candidate(
                        kind, "", "", {"path": data.get("path")})
                key = command_palette.candidate_key(cand)
                if key is not None:
                    if self._palette_history_cache is None:
                        self._palette_history_cache = command_palette.load_history()
                    self._palette_history_cache = command_palette.record_use(
                        self._palette_history_cache, key, time.time())
                    command_palette.save_history(self._palette_history_cache)
            if data.get("bookmark") or data.get("history_text"):
                raw = data.get("history_text")
                if not raw and data.get("name"):
                    raw = data["name"]
                if raw:
                    if self._cmd_history_cache is None:
                        self._cmd_history_cache = command_launcher.load_history()
                    self._cmd_history_cache = command_launcher.record_history(
                        self._cmd_history_cache, raw)
                    command_launcher.save_history(self._cmd_history_cache)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _unified_picker_state(self):
        """統合ピッカーのピン留め管理(settings の unified_picker 節)。

        pinned_features: `!` で機能の先頭に並べる機能のキー。declined は
        旧い「昇格を聞く」仕組みの名残で、もう書かない(読みもしない)。"""
        state = self.app_settings.get("unified_picker")
        if not isinstance(state, dict):
            state = {}
            self.app_settings["unified_picker"] = state
        if not isinstance(state.get("pinned_features"), list):
            state["pinned_features"] = []
        if not isinstance(state.get("declined"), list):
            state["declined"] = []
        return state

    def _save_unified_picker_state(self):
        try:
            # unified_picker 節だけを差し替える。メモリ上の設定全体を書くと、
            # 起動時の姿のままで他のプロセスの変更を戻してしまう。
            update_settings(
                {"unified_picker": self._unified_picker_state()},
                self.settings_path)
        except Exception:  # noqa: BLE001  保存できなくても実行は続ける
            traceback.print_exc()

    def run_feature_item(self, item_key) -> bool:
        """縦ランチャの機能を項目キーで実行する。実行できたら True。

        統合ピッカーの `!` と、外からの依頼(あっふのピッカー→ IPC の
        feature コマンド)で同じ実行経路を使うための口。"""
        item = taskbar_launcher.ITEMS.get(item_key or "")
        if item is None:
            return False
        target = self._audio_feature if item.get("feature") == "audio" \
            else self
        method = (getattr(target, item.get("method", ""), None)
                  if target is not None else None)
        if not callable(method):
            show_toast(f"統合ピッカー\n{item.get('label')}を呼び出せません")
            return False
        method(*item.get("args", ()))
        return True

    def _on_unified_accept(self, picker, data, hwnd, rebuild, state=None):
        """統合ピッカーで選ばれた候補を実行する。True を返すと窓を開けたままにする。

        入口なので例外は必ず受ける(常駐を落とさない)。"""
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        try:
            if kind == "jump":
                if launcher.jump(data.get("path") or "",
                                 launcher.afxw_path(self.app_settings),
                                 launcher.target_mode(self.app_settings),
                                 hwnd=hwnd):
                    self._record_unified_use(data)
                return None
            if kind == "open":
                os.startfile(data.get("target") or "")  # noqa: S606
                self._record_unified_use(data)
                return None
            if kind == "exec":
                entry = data.get("entry")
                if entry is None:
                    return None
                action, payload = command_launcher.build_command(entry)
                used = False
                if action == "exec":
                    command, workdir = payload
                    used = command_launcher.run_exec(command, workdir)
                elif action == "open":
                    os.startfile(payload)  # noqa: S606
                    used = True
                elif action == "jump":
                    used = launcher.jump(
                        payload, launcher.afxw_path(self.app_settings),
                        launcher.target_mode(self.app_settings), hwnd=hwnd)
                if used:
                    self._record_unified_use(data)
                return None
            if kind == "feature":
                if self.run_feature_item(data.get("item_key") or ""):
                    self._record_unified_use(data)
                return None
            if kind in ("app", "file"):
                path = data.get("path") or ""
                if path:
                    os.startfile(path)  # noqa: S606
                    self._record_unified_use(data)
                return None
            if kind == "more_apps":
                # 未登録アプリをその場で一覧に加える。窓は開けたまま、
                # 同じ入力で一覧を作り直すと動的候補にアプリが出る。
                if state is not None:
                    state["apps_expanded"] = True
                if picker is not None:
                    picker.set_items(rebuild())
                return True
            if kind == "web":
                url = data.get("url") or ""
                if url:
                    os.startfile(url)  # noqa: S606
                    self._record_unified_use(data)
                return None
            if kind == "builtin":
                return self._on_unified_builtin(picker, data.get("id"), rebuild)
            if kind == "add":
                return self._on_unified_add(picker, data, rebuild)
            if kind == "import":
                added, discovered = launcher.import_browser_bookmarks(
                    self.app_settings, self.settings_path)
                if discovered == 0:
                    show_toast("フォルダブックマーク\nお気に入りが見つかりませんでした")
                elif added == 0:
                    show_toast("フォルダブックマーク\n新しいお気に入りはありませんでした")
                else:
                    show_toast(f"フォルダブックマーク\n{added} 件のお気に入りを取り込みました")
                if picker is not None:
                    picker.set_items(rebuild())
                return True
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return None

    def _on_unified_builtin(self, picker, builtin_id, rebuild):
        if builtin_id == "command":
            # 管理窓はモーダルで、保存は窓の側が settings に対して行う。
            # 閉じたら一覧を作り直して、直した結果をその場で見せる。
            command_launcher.open_manager(self.app_settings, self.settings_path,
                                          picker)
            if picker is not None:
                picker.set_items(rebuild())
            return True
        if builtin_id == "config":
            if self.settings_path and os.path.exists(self.settings_path):
                os.startfile(self.settings_path)  # noqa: S606
            else:
                show_toast("統合ピッカー\nsettings.json が見つかりません")
            return None
        if builtin_id == "reload":
            if picker is not None:
                picker.set_items(rebuild())
            show_toast("統合ピッカー\n登録を読み直しました")
            return True
        return None

    def _on_unified_add(self, picker, data, rebuild):
        """「ここを登録」。既にあれば、その行を選んで知らせる(窓は開けたまま)。"""
        path = data.get("path") or ""
        if not path:
            return None
        existing = launcher.find_bookmark_by_path(self.app_settings, path)
        if existing is not None:
            show_toast(f"フォルダブックマーク\n登録済みです\n{existing['name']}")
            if picker is not None:
                items = rebuild()
                picker.set_items(items)
                for display, payload, _match in items:
                    if isinstance(payload, dict) and (
                            payload.get("path") == path
                            or payload.get("target") == path):
                        picker.select_data(payload, display)
                        break
            return True
        name = launcher._ask_name(picker, os.path.basename(path.rstrip("\\/")))
        if not name:
            return True
        if launcher.save_bookmark(self.app_settings, self.settings_path,
                                  name, path):
            show_toast(f"フォルダブックマーク\n登録しました\n{name}")
        else:
            show_toast("フォルダブックマーク\n設定ファイルに保存できませんでした")
        return None

    @staticmethod
    def _unified_bookmark_identity(data):
        """候補データから、ブックマークの同一性(名前, パス)を取る。"""
        kind = data.get("kind")
        if kind == "jump":
            return data.get("name") or "", data.get("path") or ""
        if kind == "open":
            return data.get("name") or "", data.get("target") or ""
        if kind == "exec":
            entry = data.get("entry")
            if entry is not None:
                return entry.name, entry.target
        return "", ""

    def _on_unified_delete(self, picker, _name, data, rebuild):
        """Ctrl+D。登録(ブックマーク)の行と、ピン留め機能の行が対象。

        ピン留めの行ではピンを外す(控えに戻る)。どちらも確認を出してから。"""
        if isinstance(data, dict) and data.get("kind") == "feature" \
                and data.get("pinned"):
            key = data.get("item_key") or ""
            name = data.get("name") or ""
            answer = QMessageBox.question(
                picker, "統合ピッカー",
                f"『{name}』のピン留めを外しますか？\n"
                "（! モードでまた出ます）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            state = self._unified_picker_state()
            if key in state["pinned_features"]:
                state["pinned_features"].remove(key)
                self._save_unified_picker_state()
            picker.set_items(rebuild())
            show_toast(f"統合ピッカー\nピン留めを外しました\n{name}")
            return
        if not isinstance(data, dict) or not data.get("bookmark"):
            return
        name, path = self._unified_bookmark_identity(data)
        if not name:
            return
        answer = QMessageBox.question(
            picker, "フォルダブックマーク", f"{name} を削除しますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        if launcher.delete_bookmark(self.app_settings, self.settings_path,
                                    name, path):
            picker.set_items(rebuild())
            show_toast(f"フォルダブックマーク\n削除しました\n{name}"
                       f"\n（控え: {launcher.DELETED_LOG_NAME}）")
        else:
            show_toast("フォルダブックマーク\n設定ファイルに保存できませんでした")

    def _on_unified_move(self, picker, _name, data, step, rebuild):
        """Ctrl+↑↓。登録の行だけ。確認は出さない(すぐ戻せるので)。"""
        if not isinstance(data, dict) or not data.get("bookmark"):
            return
        name, path = self._unified_bookmark_identity(data)
        if not name:
            return
        if launcher.move_bookmark(self.app_settings, self.settings_path,
                                  name, path, step):
            picker.set_items(rebuild())

    # ---------------------------------------------------------------
    # 背景を透過(bg_remove.py。窓は別プロセス)
    # ---------------------------------------------------------------
    def start_bg_remove(self):
        """クリップボードの画像を背景透過の窓で開く。

        窓は別プロセスなので、ここは起こすだけ。二重に押せば窓も2枚出るが、それぞれ
        読んだ時点のクリップボードを抱えた独立した窓なので害は無い(付箋と同じ扱い)。"""
        try:
            # 画像が無いのに起こすと、numpy/cv2 の読み込みを待たされた末に「無い」と
            # 言われる。Qt の型を見るだけの確認なので、先にここで弾く。
            if not bg_remove_launch.clipboard_has_image():
                self._notify("背景を透過", "クリップボードに画像がありません")
                return
            bg_remove_launch.spawn_from_clipboard()
        except Exception as e:
            # ホットキーからも呼ばれる。投げ切ると常駐ごと落ちるので必ず受ける。
            self._notify("背景を透過", f"起動できませんでした\n{e}")

    # ---------------------------------------------------------------
    # あっふ(filer.py。窓は別プロセス)
    # ---------------------------------------------------------------
    # ---------------------------------------------------------------
    # 写真のスライドショー(slideshow.py。窓は別プロセス)
    # ---------------------------------------------------------------
    def start_script_runner(self) -> None:
        """スクリプト実行パネルを開く。窓は別プロセス(script_runner_launch)。
        動いていれば前面に出してもらう。"""
        try:
            import script_runner_launch
            script_runner_launch.start(self.settings_path)
        except Exception as e:  # noqa: BLE001  メニューのスロット。投げ切ると常駐ごと落ちる
            self._log_failure("スクリプト実行パネルの開始")
            self._notify("スクリプト実行", f"起動できませんでした\n{e}")

    def restart_script_runner(self):
        """スクリプト実行パネルに、新しいコードでの再開を頼む。窓は別
        プロセス(script_runner_launch)。動いていれば "sent"、動いて
        いなければ起こして "spawned"。失敗したら None(外部コマンドの
        応答文を組み立てるのに、結果を返す)。"""
        try:
            import script_runner_launch
            return script_runner_launch.restart(self.settings_path)
        except Exception as e:  # noqa: BLE001  メニューのスロットと同じ作法
            self._log_failure("スクリプト実行パネルの再開")
            self._notify("スクリプト実行", f"再開できませんでした\n{e}")
            return None

    def start_slideshow(self, folders=None) -> None:
        """登録済みのフォルダで再生する。動いていれば、動いているものに再生し直してもらう
        (判定は slideshow_launch が名前付きミューテックスで行う)。"""
        try:
            import slideshow_launch
            slideshow_launch.start(folders, self.settings_path)
        except Exception as e:  # noqa: BLE001  メニューのスロット。投げ切ると常駐ごと落ちる
            self._log_failure("スライドショーの開始")
            self._notify("スライドショー", f"起動できませんでした\n{e}")

    def stop_slideshow(self) -> None:
        try:
            import slideshow_launch
            slideshow_launch.stop()
        except Exception:  # noqa: BLE001
            self._log_failure("スライドショーの停止")

    def open_slideshow_settings(self) -> None:
        """フォルダと表示の設定。開いていれば前に出すだけ。[OK] で書いたら、動いている
        スライドショーにも読み直してもらう(フォルダの変更は次の開始から)。"""
        try:
            dialog = self._slideshow_dialog
            if dialog is not None and dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
                return
            import slideshow_settings
            dialog = slideshow_settings.open_dialog(self.app_settings, self.settings_path)
            dialog.accepted.connect(self._on_slideshow_settings_saved)
            self._slideshow_dialog = dialog
        except Exception as e:  # noqa: BLE001
            self._log_failure("スライドショーの設定を開くの")
            self._notify("スライドショー", f"設定を開けませんでした\n{e}")

    def _on_slideshow_settings_saved(self) -> None:
        try:
            import slideshow_launch
            slideshow_launch.reload_config(self.settings_path)
        except Exception:  # noqa: BLE001
            self._log_failure("スライドショーへの設定の反映")

    def _set_slideshow_monitor(self, name: str) -> None:
        try:
            import slideshow_core
            import slideshow_launch
            slideshow_core.save_config(self.settings_path, {"monitor": name}, self.app_settings)
            slideshow_launch.reload_config(self.settings_path)
        except Exception as e:  # noqa: BLE001
            self._log_failure("スライドショーのモニタの変更")
            self._notify("スライドショー", f"モニタを保存できませんでした\n{e}")

    def _refresh_slideshow_menu(self) -> None:
        """開く直前に「動いているか」とモニタの一覧を作り直す(モニタは抜き差しされる)。"""
        try:
            import slideshow_launch
            self._slideshow_stop_action.setEnabled(slideshow_launch.is_running())
            current = ((self.app_settings.get("slideshow", {}) or {}).get("monitor") or "")
            names = [screen.name() for screen in screen_mirror.available_screens()]
            if current and current not in names:
                current = ""        # 外れている。主モニタに出る
            menu = self._slideshow_screen_menu
            menu.clear()
            self._slideshow_screen_actions = []
            auto = menu.addAction("主モニタ")
            auto.setCheckable(True)
            auto.setChecked(current == "")
            auto.triggered.connect(lambda _checked: self._set_slideshow_monitor(""))
            self._slideshow_screen_actions.append(auto)
            menu.addSeparator()
            for screen in screen_mirror.available_screens():
                name = screen.name()
                action = menu.addAction(screen_mirror.screen_label(screen))
                action.setCheckable(True)
                action.setChecked(name == current)
                action.triggered.connect(
                    lambda _checked, n=name: self._set_slideshow_monitor(n))
                self._slideshow_screen_actions.append(action)
        except Exception:  # noqa: BLE001
            self._log_failure("スライドショーのメニューの更新")

    # --- 窓の配置(集める・元に戻す・配置プリセット) ---
    def _layout_presets(self) -> list:
        section = self.app_settings.get(window_layout.SECTION)
        return window_layout.normalize_presets(
            section.get("presets") if isinstance(section, dict) else None)

    def _save_layout_presets(self, presets) -> None:
        window_layout.save_config(self.settings_path, {"presets": presets},
                                  self.app_settings)

    def _refresh_window_layout_menu(self) -> None:
        try:
            self._undo_gather_action.setEnabled(bool(self._gather_undo))
        except Exception:  # noqa: BLE001
            self._log_failure("窓の配置のメニューの更新")

    def gather_windows_here(self) -> None:
        """他のモニタの窓を、マウスカーソルのあるモニタへ全部集める。

        入力を他 PC へ切り替えたモニタは PC 側には接続されたままに見えるので
        Windows の自動再配置が働かず、窓だけが見えない画面に残る。その回収用。
        集約先に元からある窓は触らない。元に戻すのは直前の1回分だけ。"""
        try:
            import window_tools

            monitors = window_tools.list_monitor_work_areas()
            if len(monitors) < 2:
                self._notify("窓の配置", "モニタが1つしかないため、集める先がありません")
                return
            windows = window_layout.list_layout_windows()
            moves, _target = window_layout.plan_gather(
                windows, monitors, window_tools.get_cursor_pos())
            if not moves:
                self._notify("窓の配置", "他の画面に窓はありませんでした")
                return
            undo = window_layout.undo_moves(windows, moves)
            ok, failed = window_layout.apply_moves(moves)
            self._gather_undo = undo if ok else []
            message = f"{ok}件の窓をこの画面に集めました"
            if failed:
                message += f"\n{len(failed)}件は動かせませんでした（管理者の窓など）"
            self._notify("窓の配置", message)
        except Exception:  # noqa: BLE001
            self._log_failure("窓を集める")
            self._notify("窓の配置", "窓を集められませんでした")

    def undo_gathered_windows(self) -> None:
        """直前の「集める」で動かした窓を、元のモニタ・位置へ戻す。"""
        try:
            if not self._gather_undo:
                self._notify("窓の配置", "戻せる記録がありません")
                return
            ok, failed = window_layout.apply_moves(self._gather_undo)
            self._gather_undo = []
            message = f"{ok}件の窓を元の画面へ戻しました"
            if failed:
                message += f"\n{len(failed)}件は戻せませんでした（閉じた窓など）"
            self._notify("窓の配置", message)
        except Exception:  # noqa: BLE001
            self._log_failure("集めた窓を元に戻す")
            self._notify("窓の配置", "窓を戻せませんでした")

    def _refresh_presets_menu(self) -> None:
        try:
            menu = self._presets_menu
            menu.clear()
            presets = self._layout_presets()
            for preset in presets:
                action = menu.addAction(f"▶ {preset['name']}")
                action.triggered.connect(
                    lambda _checked, n=preset["name"]: self.apply_layout_preset(n))
            if presets:
                menu.addSeparator()
                delete_menu = menu.addMenu("🗑 プリセットを削除")
                for preset in presets:
                    action = delete_menu.addAction(preset["name"])
                    action.triggered.connect(
                        lambda _checked, n=preset["name"]: self.delete_layout_preset(n))
                menu.addSeparator()
            menu.addAction("💾 今の窓から保存…", self.save_layout_preset)
        except Exception:  # noqa: BLE001
            self._log_failure("配置プリセットのメニューの更新")

    def apply_layout_preset(self, name: str) -> None:
        """保存した配置へ、対象の窓を一括で戻す。開いていない窓は飛ばす。"""
        try:
            preset = next((p for p in self._layout_presets() if p["name"] == name), None)
            if preset is None:
                return
            windows = window_layout.list_layout_windows()
            moves, missing = window_layout.match_preset(windows, preset["items"])
            if not moves:
                self._notify("配置プリセット", f"「{name}」の窓は開いていませんでした")
                return
            ok, failed = window_layout.apply_moves(moves)
            message = f"「{name}」: {ok}件の窓を配置しました"
            notes = []
            if missing:
                notes.append(f"開いていない窓 {missing}件")
            if failed:
                notes.append(f"動かせなかった窓 {len(failed)}件")
            if notes:
                message += "\n（" + "・".join(notes) + "）"
            self._notify("配置プリセット", message)
        except Exception:  # noqa: BLE001
            self._log_failure("配置プリセットの適用")
            self._notify("配置プリセット", "配置を適用できませんでした")

    def delete_layout_preset(self, name: str) -> None:
        try:
            presets = [p for p in self._layout_presets() if p["name"] != name]
            self._save_layout_presets(presets)
            self._notify("配置プリセット", f"「{name}」を削除しました")
        except Exception:  # noqa: BLE001
            self._log_failure("配置プリセットの削除")
            self._notify("配置プリセット", "プリセットを削除できませんでした")

    def save_layout_preset(self) -> None:
        """今見えている窓の一覧から、チェックした窓だけを名前付きで保存する。"""
        try:
            from PySide6.QtWidgets import (
                QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem,
                QVBoxLayout,
            )

            windows = [w for w in window_layout.list_layout_windows() if w["title"]]
            if not windows:
                self._notify("配置プリセット", "保存できる窓がありませんでした")
                return
            dialog = QDialog()
            dialog.setWindowTitle("配置プリセットに保存")
            layout = QVBoxLayout(dialog)
            layout.addWidget(QLabel("保存する窓を選んでください（位置と大きさを覚えます）"))
            name_edit = QLineEdit()
            name_edit.setPlaceholderText("プリセットの名前")
            layout.addWidget(name_edit)
            listing = QListWidget()
            for window in windows:
                exe = os.path.basename(window["exe"]) if window["exe"] else "?"
                item = QListWidgetItem(f"{window['title']}  （{exe}）")
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                listing.addItem(item)
            layout.addWidget(listing)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.Accepted:
                return
            name = name_edit.text().strip()
            chosen = [window_layout.preset_item(windows[i])
                      for i in range(listing.count())
                      if listing.item(i).checkState() == Qt.Checked]
            if not name:
                self._notify("配置プリセット", "名前が空なので保存しませんでした")
                return
            if not chosen:
                self._notify("配置プリセット", "窓が選ばれていないので保存しませんでした")
                return
            presets = self._layout_presets()
            existing = next((p for p in presets if p["name"] == name), None)
            if existing is not None:
                answer = QMessageBox.question(
                    None, "配置プリセット",
                    f"「{name}」はすでにあります。上書きしますか？")
                if answer != QMessageBox.Yes:
                    return
                presets = [p for p in presets if p["name"] != name]
            presets.append({"name": name, "items": chosen})
            self._save_layout_presets(presets)
            self._notify("配置プリセット", f"「{name}」に{len(chosen)}件の窓を保存しました")
        except Exception:  # noqa: BLE001
            self._log_failure("配置プリセットの保存")
            self._notify("配置プリセット", "プリセットを保存できませんでした")

    def start_filer(self):
        """あっふを開く。既に開いていれば前面に出すだけ(判定は filer_launch が
        名前付きミューテックスで行う)。窓は DETACHED の別プロセスなので、常駐を
        再起動しても道連れにならず、restart_blockers に足す必要も無い。"""
        try:
            filer_launch.open_filer()
        except Exception as e:
            # ホットキーからも呼ばれる。投げ切ると常駐ごと落ちるので必ず受ける。
            self._notify("あっふ", f"起動できませんでした\n{e}")

    # ---------------------------------------------------------------
    # フォルダ監視移動(folder_watch.py)
    # ---------------------------------------------------------------
    def _folder_watch_rules(self) -> list:
        """いまの設定のルール(正規化済み)。壊れた値は folder_watch 側が落とす。"""
        return folder_watch.normalize_config(
            self.app_settings.get(folder_watch.SECTION))["rules"]

    def _rebuild_folder_watch(self) -> None:
        """設定からタイマーを組み直す。起動時と、設定が書かれたあとに呼ぶ。

        無効なルール(元=先など)はタイマーを作らず、理由を1回だけ知らせる
        (組み直しのたびに鳴ると、設定を開くたびにトーストが出るため)。"""
        try:
            for timer in self._folder_watch_timers:
                timer.stop()
            self._folder_watch_timers = []
            for rule in self._folder_watch_rules():
                if not rule["enabled"]:
                    continue
                problem = folder_watch.validate_rule(rule)
                key = folder_watch.folder_key(rule["source"]) + "->" + folder_watch.folder_key(rule["dest"])
                if problem:
                    if key not in self._folder_watch_warned:
                        self._folder_watch_warned.add(key)
                        self._notify("フォルダ監視移動",
                                     f"ルールを止めています\n{rule['source']}\n{problem}")
                    continue
                self._folder_watch_warned.discard(key)
                watcher = self._folder_watchers.get(key)
                if watcher is None:
                    watcher = self._folder_watchers[key] = folder_watch.FolderWatcher(rule)
                else:
                    watcher.rule = rule
                timer = QTimer()
                timer.timeout.connect(lambda k=key: self._run_folder_watch(k))
                timer.start(rule["interval_seconds"] * 1000)
                self._folder_watch_timers.append(timer)
        except Exception:  # noqa: BLE001
            self._log_failure("フォルダ監視移動の組み立て")

    def _run_folder_watch(self, key: str, source: str = "timer") -> None:
        """1ルールぶんスキャンする。移動したら記録と、まとめて1枚の通知。

        PySide6 はスロット内で例外を投げ切るとプロセスごと終わる。ここは常駐中
        ずっと回り続ける場所なので、必ず受けて常駐を続ける(ジグラーの tick と同じ)。"""
        try:
            watcher = self._folder_watchers.get(key)
            if watcher is None:
                return
            result = watcher.scan_once()
            if result["error"]:
                if key not in self._folder_watch_warned:
                    self._folder_watch_warned.add(key)
                    self._notify("フォルダ監視移動", result["error"])
                return
            self._folder_watch_warned.discard(key)
            for src, dst in result["moved"]:
                action_log.record("フォルダ監視移動", f"{src} → {dst}", source)
            if result["moved"]:
                self._notify("フォルダ監視移動", f"{len(result['moved'])}件移動しました")
        except Exception:  # noqa: BLE001
            self._log_failure("フォルダ監視移動のスキャン")

    def check_folder_watch_now(self) -> None:
        """全ルールをいますぐ1回スキャンする(安定判定はそのまま効く)。"""
        try:
            for rule in self._folder_watch_rules():
                if not rule["enabled"]:
                    continue
                key = (folder_watch.folder_key(rule["source"]) + "->"
                       + folder_watch.folder_key(rule["dest"]))
                if key not in self._folder_watchers:
                    self._folder_watchers[key] = folder_watch.FolderWatcher(rule)
                self._run_folder_watch(key, source="menu")
        except Exception:  # noqa: BLE001
            self._log_failure("フォルダ監視移動の手動チェック")

    def folder_watch_status(self) -> str:
        try:
            rules = self._folder_watch_rules()
            enabled = sum(1 for rule in rules if rule["enabled"])
            return f"フォルダ監視移動: ルール{len(rules)}件（有効{enabled}件）"
        except Exception:  # noqa: BLE001
            return "フォルダ監視移動: 不明"

    def _refresh_folder_watch_menu(self) -> None:
        """開く直前にルールの有効／無効の一覧を作り直す。"""
        try:
            rules = self._folder_watch_rules()
            enabled = sum(1 for rule in rules if rule["enabled"])
            self._folder_watch_menu.setTitle(
                "📥 フォルダ監視移動" if not enabled
                else f"📥 フォルダ監視移動（{enabled}件有効）")
            menu = self._folder_watch_rules_menu
            menu.clear()
            self._folder_watch_rule_actions = []
            if not rules:
                action = menu.addAction("（ルールがありません）")
                action.setEnabled(False)
                self._folder_watch_rule_actions.append(action)
            for index, rule in enumerate(rules):
                action = menu.addAction(
                    f"{rule['source']} → {rule['dest']}（{rule['interval_seconds']}秒）")
                action.setCheckable(True)
                action.setChecked(rule["enabled"])
                action.triggered.connect(
                    lambda checked, i=index: self._set_folder_watch_enabled(i, checked))
                self._folder_watch_rule_actions.append(action)
        except Exception:  # noqa: BLE001
            self._log_failure("フォルダ監視移動のメニューの更新")

    def _set_folder_watch_enabled(self, index: int, enabled: bool) -> None:
        """ルールの有効／無効を settings.json へ書いて、タイマーを組み直す。"""
        try:
            rules = self._folder_watch_rules()
            if not 0 <= index < len(rules):
                return
            rules[index]["enabled"] = bool(enabled)
            folder_watch.save_config(
                self.settings_path, {"rules": rules}, self.app_settings)
            self._rebuild_folder_watch()
        except Exception as e:  # noqa: BLE001
            self._log_failure("フォルダ監視移動の有効／無効の切り替え")
            self._notify("フォルダ監視移動", f"切り替えを保存できませんでした\n{e}")

    def open_folder_watch_settings(self) -> None:
        """ルールの追加・編集ダイアログ。開いていれば前に出すだけ。"""
        try:
            dialog = self._folder_watch_dialog
            if dialog is not None and dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
                return
            import folder_watch_settings
            dialog = folder_watch_settings.open_dialog(
                self.app_settings, self.settings_path)
            dialog.accepted.connect(self._rebuild_folder_watch)
            self._folder_watch_dialog = dialog
        except Exception as e:  # noqa: BLE001
            self._log_failure("フォルダ監視移動の設定を開くの")
            self._notify("フォルダ監視移動", f"設定を開けませんでした\n{e}")

    # ---------------------------------------------------------------
    # Teams の一人会議(teams_meeting.py。UIA の実処理は子プロセス)
    # ---------------------------------------------------------------
    def _teams_meeting_running(self) -> bool:
        proc = self._teams_meeting_proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001  読めないなら「動いているかも」に倒す
            return True

    def start_teams_meeting(self, source: str = "menu") -> None:
        """テーマを聞いて会議名を作り、立ち上げの子プロセスを起こす。

        ホットキーからも呼ばれる。投げ切ると常駐ごと落ちるので、全体を try で受ける。

        【実行中にもう一度呼ばれたら知らせて無視する】
        2本走ると、同じ Teams の同じダイアログを取り合って、会議名が片方の値で
        上書きされたり、片方の「会議を開始」がもう片方の会議を始めたりする。

        【トーストで「キー入力を控えて」と言う理由】
        Teams の「今すぐ会議」のダイアログは開くとフォーカスを取る。別の窓で打っていた
        文字が会議名に混ざった実例がある(「susum」)。こちらは読み戻して入れ直すが、
        打った文字が別の窓へ届かなくなるのはどうにもならないので、先に知らせる。"""
        try:
            if self._teams_meeting_asking:
                return  # テーマの入力欄が既に出ている
            if self._teams_meeting_running():
                show_toast("一人会議\n立ち上げの途中です（実行中です）。終わるまで待ってください")
                return
            config = teams_meeting.load_config(self.app_settings)
            fmt = config["title_format"]
            topic = ""
            if teams_meeting.needs_topic(fmt):
                # 入力欄は定型文の {input} と同じく QInputDialog。会議名は1行なので
                # 複数行ではなく getText にする(Enter で決定できるほうが速い)。
                self._teams_meeting_asking = True
                try:
                    topic, ok = QInputDialog.getText(
                        None, "一人会議を開始",
                        "テーマ（空でも可。会議名の末尾に付きます）",
                    )
                finally:
                    self._teams_meeting_asking = False
                if not ok:
                    return
            title = teams_meeting.format_title(fmt, topic)
            action_log.record("一人会議 開始", title, source)
            show_toast(
                f"一人会議を立ち上げます\n会議名: {title}\n"
                "数秒間キー入力を控えてください（Teams が入力欄にフォーカスを取ります）",
                5000,
            )
            self._teams_meeting_proc = teams_meeting.spawn(
                title, on_event=self._on_teams_meeting_event_thread)
        except Exception as e:  # noqa: BLE001
            self._log_failure("一人会議の開始")
            self._notify("一人会議", f"立ち上げを始められませんでした\n{e}")

    def _on_teams_meeting_event_thread(self, payload: dict) -> None:
        """子の読み役スレッドから呼ばれる。Qt に触らずシグナルで渡すだけ。"""
        try:
            self._teams_meeting_bridge.event.emit(payload)
        except Exception as e:  # noqa: BLE001  ここで投げると読み役が止まる
            print(f"[teams_meeting] 知らせを渡せません: {e}", file=sys.stderr)

    def _on_teams_meeting_event(self, payload: dict) -> None:
        """メインスレッドで受ける。手順ごとに action.log へ残し、終わったら知らせる。

        手順ごとに残すのは、止まったときに「どこまで進んだか」を後から辿れるように
        するため(トーストは数秒で消える)。"""
        try:
            event = payload.get("event")
            if event == "step_end":
                action_log.record(
                    f"一人会議 {payload.get('label', payload.get('key', '?'))}",
                    f"{payload.get('status', '?')} {payload.get('detail', '')}".strip(),
                    "teams_meeting",
                )
            elif event == "done":
                self._teams_meeting_proc = None
                outcome = "成功" if payload.get("ok") else (
                    f"停止: {payload.get('stopped_label') or '?'}")
                action_log.record(
                    "一人会議 終了",
                    f"{outcome} / 文字起こし={payload.get('transcript')} / "
                    f"{payload.get('title', '')}",
                    "teams_meeting",
                )
                # 結果は読んでから手で続けることがあるので、既定より長く出す。
                show_toast(teams_meeting.describe_result(payload), 8000)
        except Exception:  # noqa: BLE001  スロットで投げ切ると常駐ごと落ちる
            self._log_failure("一人会議の知らせ")

    # ---------------------------------------------------------------
    # 統合ピッカーの窓(フォルダブックマークの入口でもある)
    # ---------------------------------------------------------------
    def _open_picker_calculator(self, expr: str, pos=None):
        """統合ピッカーの `=` から電卓(式の作法)を開く。

        ピッカーは既に閉じている。縦ランチャの電卓と窓の持ち方を共有する
        (calculator_window / _close_calculator)。出ていたら作り直す
        (作法が違う窓を使い回すと、種まきした式が残らないため)。
        位置はピッカーのあった場所(実機確認 #1)。"""
        if self.calculator_window is not None:
            self._close_calculator()
        window = calculator.create_window(expression_mode=True,
                                          seed=expr or "")
        window.closed.connect(self._close_calculator)
        self.calculator_window = window
        if pos is not None:
            window.show_at(pos)
        else:
            window.show_at_cursor_screen()

    def _on_unified_ctrl_enter(self, picker, _name, data):
        """常駐側ピッカーの Ctrl+Enter。未登録アプリの行だけが対象で、
        「ランチャ登録する」の小さなメニューをピッカーの上に重ねる。

        登録はその場で settings に書く(名前はアプリ名)。登録済みなら
        メニュー自体を出さない。あっふ側のピッカーでは、同じ行に
        あっふの Ctrl+Enter メニュー(開き先・拡張子メニュー付き)が出る。"""
        if not isinstance(data, dict) or data.get("kind") != "app":
            return
        path = data.get("path") or ""
        name = data.get("name") or ""
        if not path or not name:
            return
        if launcher.find_bookmark_by_path(self.app_settings, path) is not None:
            return
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QMenu
        menu = QMenu(picker)
        action = menu.addAction("ランチャ登録する")
        if menu.exec(QCursor.pos()) is not action:
            return
        if launcher.save_bookmark(self.app_settings, self.settings_path,
                                  name, path):
            show_toast(f"統合ピッカー\nランチャに登録しました\n{name}")
        else:
            show_toast("統合ピッカー\n設定ファイルに保存できませんでした")

    def start_launcher(self, current_path: str = None):
        """統合ピッカーを開く。current_path はあふｗ側の現在のパスで、
        IPC(main.py)から呼ばれたときだけ渡る。メニューやホットキーからは渡らないが、
        前面がエクスプローラならそちらからパスを読めるので「ここを登録」は出せる。"""
        # 何よりも先に前面ウィンドウを掴む。ピッカーを出した時点で前面はこちらに移り、
        # メニュー経由ではその前にメニュー側へ移っているので、後からでは手遅れになる。
        # ホットキー経由は keyboard の別スレッドからシグナルでここへ渡って来るだけなので、
        # この時点ではまだ元のウィンドウが前面のまま。
        # 取れない・エクスプローラでない場合は 0 のまま渡り、従来どおりあふｗへ落ちる。
        hwnd = explorer_nav.foreground_hwnd()

        # 開いたウインドウはここで参照を持ち続ける(ローカル変数だけだとGCで消える)。
        if self.launcher_picker is not None:
            # 開いたまま同じホットキーを叩いたときは、開き直さず前面に呼び戻す
            self.launcher_picker.raise_()
            self.launcher_picker.activateWindow()
            return
        if self._palette_history_cache is None:
            self._palette_history_cache = command_palette.load_history()
        if self._cmd_history_cache is None:
            self._cmd_history_cache = command_launcher.load_history()
        if self._palette_apps_cache is None:
            self._palette_apps_cache = command_palette.scan_app_candidates()
        self._unified_es_dead = False
        features = [(key, item["label"])
                    for key, item in taskbar_launcher.ITEMS.items()]
        add_path = launcher.resolve_add_path(current_path, hwnd)
        # モードと「未登録アプリを広げたか」は、候補を作る側と共有する dict で持つ。
        state = {"mode": unified_picker.INITIAL_MODE, "apps_expanded": False}

        def rebuild():
            # ピン留めは窓を開いている間にも変わる(解除)ので、作るたびに
            # 現在の設定から組み直す。モードは state の現在値を見る。
            picker_state = self._unified_picker_state()
            return unified_picker.build_mode_items(
                state["mode"],
                command_launcher.build_entries(self.app_settings),
                features=features,
                pinned_keys=list(picker_state["pinned_features"]),
                recent=unified_picker.recent_folders(self.app_settings),
                add_path=add_path)

        dynamics = unified_picker.make_mode_dynamics(
            entries=command_launcher.build_entries(self.app_settings),
            apps=self._palette_apps_cache,
            web_searches=(self.app_settings.get("command_palette") or {})
            .get("web_searches"),
            file_search=self._unified_file_search,
            state=state)
        picker = None

        def accept(_name, data):
            return self._on_unified_accept(picker, data, hwnd, rebuild, state)

        def delete(name, data):
            self._on_unified_delete(picker, name, data, rebuild)

        def move(name, data, step):
            self._on_unified_move(picker, name, data, step, rebuild)

        def mode_change(char):
            state["mode"] = char
            if char is not None and picker is not None:
                picker.set_items(rebuild())

        picker = PickerWindow(
            "統合ピッカー", rebuild(), accept,
            **unified_picker.picker_kwargs(
                state, dynamics=dynamics, on_delete=delete, on_move=move,
                on_mode_change=mode_change,
                on_calc=self._open_picker_calculator,
                on_ctrl_enter=lambda name, data: self._on_unified_ctrl_enter(
                    picker, name, data)))
        picker.closed.connect(self._close_launcher)
        self.launcher_picker = picker
        picker.show()

    def _close_launcher(self):
        if self.launcher_picker is None:
            return
        picker = self.launcher_picker
        self.launcher_picker = None
        picker.close()
        picker.deleteLater()

    # ---------------------------------------------------------------
    # 最前面固定
    # ---------------------------------------------------------------
    def toggle_always_on_top(self):
        result = self.topmost.toggle_foreground()
        if result is None:
            return
        title, pinned = result
        title = title or "(名称不明のウィンドウ)"
        self._notify("最前面固定", f"{'固定' if pinned else '解除'}: {title}")

    # ---------------------------------------------------------------
    # タスクバーウィジェット(各ディスプレイのタスクバーに置くトレイの代わり)
    # ---------------------------------------------------------------
    def attach_audio_feature(self, audio_feature) -> None:
        """音声側のFeatureを受け取り、設定が有効ならウィジェットを出す。

        ウィジェットは画面側と音声側の両方(アイコンの絵・デバイス切替・それぞれのメニュー)を
        呼ぶため、Featureが全部そろってからでないと作れない。コンストラクタは1つずつ
        呼ばれるので、組み立ては main.py が済んだところでここに渡してもらう。

        ディスプレイ構成の変化を拾う接続もここで行う。ウィジェットを作れるようになった
        あとでないと、通知が来ても作り直しようがない。"""
        self._audio_feature = audio_feature

        app = QApplication.instance()
        if app is not None:
            # モニタを抜き差しすればタスクバーの数も位置も変わる。掴んでいる矩形は
            # 生成時のもので追従しないので、通知を受けて丸ごと作り直す。
            app.screenAdded.connect(self._on_screens_changed)
            app.screenRemoved.connect(self._on_screens_changed)
            app.primaryScreenChanged.connect(self._on_screens_changed)

        if self.app_settings.get("taskbar_widget", {}).get("enabled"):
            self._show_taskbar_widgets(notify=False)
        self._refresh_taskbar_menu()

    def _show_taskbar_widgets(self, notify: bool = True) -> bool:
        """ディスプレイ1枚につき1つ、ウィジェットを出す。1つも出せなければ False。

        置き先はタスクバーの数ではなく画面の数で決める(taskbar_widget.widget_slots)。
        「タスクバーをすべてのディスプレイに表示する」がオフの環境ではセカンダリに
        タスクバーが無く、タスクバーを数えると2番目以降が1つも出ないため。

        既に出ているものがあれば作り直さない(トグルで消してから出し直したときに、
        画面の顔ぶれが変わっていなければ同じ窓を再利用する)。"""
        if self._audio_feature is None:
            return False

        config = self.app_settings.get("taskbar_widget", {})
        # all_displays が False ならセカンダリだけ(プライマリには本物の通知領域がある)。
        include_primary = config.get("all_displays", True)
        slots = taskbar_widget.widget_slots(include_primary=include_primary)
        if not slots:
            # 出せる画面が1つも無い(all_displays を False にしていて画面が1枚だけ、など)。
            # 起動時に黙って出ないと故障と区別がつかないが、通知は出しっぱなしにしない。
            if notify:
                self._notify("タスクバーウィジェット", "表示できるディスプレイがありません")
            return False

        # 画面の顔ぶれ(名前と順序)が変わっていたら作り直す。数だけを見ると、1枚外して
        # 1枚挿した場合に別の画面の位置を引き継いだ窓が残る。
        names = [name for name, _rect in slots]
        if [widget.screen_name for widget in self.taskbar_widgets] != names:
            self._close_taskbar_widgets()
            self.taskbar_widgets = [
                taskbar_widget.TaskbarWidget(
                    self.app_settings,
                    self.settings_path,
                    self,
                    self._audio_feature,
                    rect,
                    name,
                )
                for name, rect in slots
            ]

        # 先にリストへ落としてから any() に渡す。any(ジェネレータ) にすると最初に True を
        # 返した時点で打ち切られ、2つめ以降の start() が呼ばれない(＝1番目のディスプレイ
        # にしか出ない)。実際にそれで「2番目以降に表示されない」が起きていた。
        started = [widget.start() for widget in self.taskbar_widgets]
        # 出し直した窓は「ミラーが覆っている画面」を知らない状態で出てくるので配り直す。
        self._apply_mirror_cover()
        # 1つでも出せたなら有効として扱う。画面が3枚あって1枚だけ失敗した場合に、
        # 全部消してしまうほうが困る。
        return any(started)

    def _set_mirror_covered_screen(self, screen_name) -> None:
        """画面ミラーが覆っている画面の名前を受け取り、そのウィジェットを引っ込める。

        ウィジェット側の全画面判定(GetForegroundWindow)ではミラー窓を検出できない。
        あれは前面にならない作りだから(taskbar_widget.set_app_covered のコメント参照)。
        押し上げ合いになると、共有側のモニタでウィジェットとミラー窓がチカチカする。"""
        try:
            self._mirror_covered_screen = screen_name or None
            self._apply_mirror_cover()
        except Exception:
            self._log_taskbar_failure("画面ミラーに合わせた引っ込め")

    def _apply_mirror_cover(self) -> None:
        """覚えている「覆われている画面」を、いまあるウィジェット全部へ配り直す。
        作り直した直後にも通すこと(新しい窓は何も知らない状態で出てくる)。"""
        covered = getattr(self, "_mirror_covered_screen", None)
        # 名前をそのまま比べてはいけない。ミラーが知らせてくるのは QScreen.name()
        # (モニタの型番)だが、ウィジェット側の screen_name は位置を覚える都合で
        # 型番にシリアルを繋いだ別物(taskbar_widget._screen_key)。同じ画面でも
        # 'DST' と 'DST#sn-DST' で永久に一致せず、引っ込まないままだった。
        # ウィジェットが乗っている画面の名前を引き直して比べる。
        for widget in getattr(self, "taskbar_widgets", []):
            widget.set_app_covered(_widget_screen_name(widget) == covered)

    def _hide_taskbar_widgets(self) -> None:
        for widget in self.taskbar_widgets:
            widget.stop()

    def _close_taskbar_widgets(self) -> None:
        """作り直しと終了のときに、いまの窓を確実に片付ける。

        TaskbarWidget に WA_DeleteOnClose は付けていないので、close() だけでは
        C++側の実体は残る。参照を手放してPython側の寿命に任せる(deleteLater を使うと、
        メニューを開いたまま画面構成が変わったときに、イベント処理中のウィジェットの
        実体だけ先に消えて落ちる)。"""
        for widget in self.taskbar_widgets:
            widget.close()
        self.taskbar_widgets = []

    def _on_screens_changed(self, _screen=None):
        """screenAdded/screenRemoved/primaryScreenChanged の受け口。

        通知の時点ではWindowsがまだタスクバーを並べ直しておらず、すぐ測ると古い位置を
        拾う。少し待ってから作り直す(連続で飛んでくる通知をまとめる役目も兼ねる)。"""
        try:
            # mss はモニタの一覧を生成時に読むので、構成が変わったら作り直させる
            # (使い回しの詳細は capture_grab._sct を参照)。
            capture_grab.release_sct()
            if not self.taskbar_widgets:
                return
            self._taskbar_rebuild_timer.start(taskbar_widget.REBUILD_DELAY_MS)
        except Exception:
            self._log_taskbar_failure("ディスプレイ構成の変化の受け取り")

    def _rebuild_taskbar_widgets(self):
        """いまのタスクバーに合わせてウィジェットを作り直す。

        矩形を差し替えるのではなく作り直すのは、画面が増減すると窓の数そのものが
        変わるため。数が同じでも位置は変わりうるので、区別せず一律に作り直す。"""
        try:
            self._close_taskbar_widgets()
            self._show_taskbar_widgets(notify=False)
            self._refresh_taskbar_menu()
        except Exception:
            self._log_taskbar_failure("ディスプレイ構成の変化への追従")

    # ドラッグで決まった位置は TaskbarWidget が自分で保存する(画面ごとに独立して持ち、
    # 動かしたもの以外には触らないため、ここで束ねて配る必要がない)。

    def _toggle_taskbar_widget(self, checked: bool):
        """QAction.triggered は checked(bool) を渡してくる。チェック可能な項目なので
        その値をそのまま表示状態として扱う。"""
        shown = self._show_taskbar_widgets() if checked else False
        if not checked:
            self._hide_taskbar_widgets()
        # 出せなかったときは設定まで有効にしない(次の起動でまた失敗するだけ)。
        taskbar_widget.save_config(self.app_settings, self.settings_path, enabled=shown)
        self._refresh_taskbar_menu()

    def _recapture_taskbar_background(self):
        for widget in self.taskbar_widgets:
            widget.refresh_background()

    def _refresh_taskbar_menu(self):
        visible = any(widget.isVisible() for widget in self.taskbar_widgets)
        self._taskbar_action.setChecked(visible)
        # 背景は「自分が乗っている場所」を測るので、出ていないと測りようがない。
        self._taskbar_bg_action.setEnabled(visible)

    def _log_taskbar_failure(self, where: str) -> None:
        """スクリーン変更のスロットで例外を投げ切らせない(PySide6はプロセスごと終わる)。
        ウィジェットが出ないだけの話なので、標準エラーに残して常駐は続ける。"""
        traceback.print_exc()
        print(f"[tray-tools] タスクバーウィジェット: {where}に失敗しました", file=sys.stderr)

    def _log_failure(self, where: str) -> None:
        """上と同じ役目の、ウィジェット以外から呼ぶほう。メニューを組み立てるスロットで
        投げ切ると常駐ごと終わるので、どの入口にも受け皿が要る。"""
        traceback.print_exc()
        print(f"[tray-tools] {where}に失敗しました", file=sys.stderr)

    # ---------------------------------------------------------------
    # スマホ通知(Pushover)のトークン登録
    # ---------------------------------------------------------------
    def _refresh_pushover_menu(self):
        """開く直前に「登録済みかどうか」だけ反映する。値は絶対に出さない。"""
        if self._pushover_menu is None:
            return
        try:
            registered = pushover.is_registered()
        except Exception:
            registered = False
        self._pushover_register_action.setText(
            "🔑 トークンを登録し直す…" if registered else "🔑 トークンを登録…"
        )
        self._pushover_delete_action.setEnabled(registered)
        self._pushover_menu.setTitle(
            "📱 スマホ通知（Pushover：登録済み）" if registered
            else "📱 スマホ通知（Pushover：未登録）"
        )

    def _pushover_wanted(self) -> bool:
        """このPCでスマホ通知のメニューを出すか。

        登録済みなら出す(使っているPCということなので)。まだ登録していないPCでも、
        設定に書けば出せる。どちらでもなければ出さない。判定に失敗したときは
        出さない側へ倒す(出ないことより、業務用の端末に個人の通知先が並ぶほうが困る)。"""
        try:
            if (self.app_settings.get("tools", {}) or {}).get("pushover"):
                return True
            return bool(pushover.is_registered())
        except Exception:
            return False

    def _test_pushover(self):
        """試しに1通送る。届くかどうかをここで確かめられるようにする。

        送信は別スレッドで行う。ネットワークは数秒かかりうるので、メニューから
        呼んだからといってメインスレッドで待つと、その間トレイも付箋もホットキーも
        全部固まる(pushover.send は Qt に触らないので、ワーカーから呼んで安全)。

        結果の通知だけはメインスレッドへ戻す。トーストはQtの窓なので、ワーカーから
        直に作ると壊れる。"""
        try:
            if not pushover.is_registered():
                self._notify(
                    "Pushover",
                    "トークンが登録されていません（先に「トークンを登録…」から）",
                )
                return
            self._notify("Pushover", "テスト送信しています…")
            stamp = datetime.now().strftime("%H:%M:%S")
            message = f"tray-tools からのテスト送信です（{stamp}）"

            def work():
                ok, detail = pushover.send(
                    message, title="tray-tools", sound="magic"
                )
                # ワーカーから直接トーストを出さない。シグナル経由でメインスレッドへ。
                self._pushover_bridge.finished.emit(bool(ok), str(detail or ""))

            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self._log_failure("Pushover のテスト送信")

    def _on_pushover_tested(self, ok: bool, detail: str):
        """テスト送信の結果を知らせる。detail に本文やトークンは入らない。"""
        try:
            if ok:
                action_log.record("Pushover テスト送信", "成功", "menu")
                self._notify("Pushover", "送信しました（スマホをご確認ください）")
            else:
                action_log.record("Pushover テスト送信", "失敗", "menu")
                self._notify("Pushover", f"送れませんでした\n{detail}")
        except Exception:
            self._log_failure("Pushover の結果通知")

    def _register_pushover(self):
        """ユーザーキーとアプリのトークンを尋ねて資格情報マネージャへ預ける。

        入力欄は両方ともマスク表示にする。肩越しに見られる場所で打つことがあるうえ、
        入力欄に平文で残ったまま画面キャプチャを撮る事故もありうる。
        受け取った値はここから先へ持ち出さない(ログにも通知にも出さない)。"""
        try:
            user_key, ok = QInputDialog.getText(
                None, "Pushover",
                "ユーザーキー（Pushover の User Key）",
                QLineEdit.Password,
            )
            if not ok or not user_key.strip():
                return
            app_token, ok = QInputDialog.getText(
                None, "Pushover",
                "アプリのトークン（API Token）",
                QLineEdit.Password,
            )
            if not ok or not app_token.strip():
                return
            if pushover.store(user_key, app_token):
                self._notify("Pushover", "資格情報マネージャに登録しました")
            else:
                self._notify("Pushover", "登録できませんでした")
        except Exception:
            self._log_failure("Pushover トークンの登録")

    def _delete_pushover(self):
        try:
            if pushover.clear():
                self._notify("Pushover", "登録を削除しました")
            else:
                self._notify("Pushover", "登録がありません")
        except Exception:
            self._log_failure("Pushover トークンの削除")

    # ---------------------------------------------------------------
    # スリープ抑止
    # ---------------------------------------------------------------
    def _ask_keep_awake_minutes(self):
        """何分抑止するかを尋ねて有効にする。

        メニューに並べる長さを増やしても、欲しい長さがそこに無いことはある
        (「あと40分だけ」など)。入力を1つ用意しておけば、設定を書き換えずに済む。"""
        try:
            current = self._awake_minutes if self._awake_active else 60
            minutes, ok = QInputDialog.getInt(
                None, "スリープ抑止", "何分間、抑止しますか",
                int(current or 60), 1, 24 * 60, 15,
            )
            if ok:
                self._enable_keep_awake(int(minutes))
        except Exception:
            self._log_failure("スリープ抑止の時間の入力")

    def enable_keep_awake(self, minutes=None, source: str = "external"):
        """外(名前付きパイプ)から呼ばれる入口。minutes が None なら無期限。

        長い処理を外から回すとき、始める前に掛けて終わったら外す、という使い方を
        想定している。中身はメニューから呼ぶものと同じ。"""
        self._enable_keep_awake(minutes, source)

    def disable_keep_awake(self, source: str = "external"):
        """外から呼ばれる入口。掛かっていなければ何もしない。"""
        self._disable_keep_awake(source=source)

    def _enable_keep_awake(self, minutes, source: str = "menu"):
        # SetThreadExecutionStateは呼び出したスレッドに紐づく。ここはメニュー操作か、
        # シグナル経由でメインスレッドに渡されたホットキーからしか呼ばれない。
        if not set_keep_awake(True):
            self._notify("スリープ抑止", "有効化に失敗しました")
            return

        self._awake_timer.stop()
        self._awake_active = True
        self._awake_minutes = minutes
        if minutes:
            self._awake_deadline = time.monotonic() + minutes * 60
            self._awake_timer.start(minutes * 60 * 1000)
            label = _format_minutes(minutes)
        else:
            self._awake_deadline = None
            label = "無期限"
        self._refresh_state()
        action_log.record("スリープ抑止 有効", label, source)
        self._notify("スリープ抑止", f"有効: {label}")

    def _disable_keep_awake(self, notify: bool = True, source: str = "menu"):
        was_active = self._awake_active
        self._awake_timer.stop()
        set_keep_awake(False)
        self._awake_active = False
        self._awake_minutes = None
        self._awake_deadline = None
        self._refresh_state()
        if was_active:
            action_log.record("スリープ抑止 解除", "", source)
        if notify and was_active:
            self._notify("スリープ抑止", "解除しました")

    # ---------------------------------------------------------------
    # スリープさせる
    # ---------------------------------------------------------------
    def schedule_sleep(self, seconds: int, hibernate: bool = False,
                       source: str = "menu") -> None:
        """指定した秒数のあとにPCをスリープさせる。0なら猶予だけ置いてすぐ。

        外(名前付きパイプ)からも呼ばれる。長い処理を回している間だけ起こしておいて、
        終わったら寝かせる、という使い方を想定している。

        寝る直前に SLEEP_WARN_SECONDS の猶予を置いて声を掛ける。予約したことを忘れて
        作業している最中に落ちると、開いているものが道連れになるため。その間に
        「予約を取り消す」を押せば止まる。"""
        try:
            seconds = max(0, int(seconds))
            if hibernate and not hibernate_available():
                # 無効なまま呼んでも何も起きないかスリープに落ちる。押して反応が
                # 無いと故障に見えるので、理由を言って何もしない。
                self._notify(
                    "休止状態",
                    "この環境では使えません（powercfg /hibernate on で有効化できます）",
                )
                return
            self.cancel_sleep(notify=False)
            self._sleep_hibernate = bool(hibernate)
            self._sleep_deadline = time.monotonic() + seconds + SLEEP_WARN_SECONDS
            name = "休止状態" if hibernate else "スリープ"
            # 予告の窓は1秒ごとに残りを見て出す。予約の時点で「何秒後に出すか」を
            # 決め打ちにすると、途中で予約を差し替えたときに古い予定が残る。
            self._sleep_tick.start(1000)
            action_log.record(
                f"{name} 予約", f"{_format_seconds(seconds)}後", source
            )
            if seconds:
                self._sleep_timer.start(seconds * 1000)
                self._notify(name, f"{_format_seconds(seconds)}後に{name}にします")
            else:
                self._on_sleep_due()
            self._refresh_state()
        except Exception:
            self._log_failure("スリープの予約")

    def cancel_sleep(self, notify: bool = True, source: str = "menu") -> None:
        """予約を取り消す。掛かっていなければ何もしない。"""
        try:
            had = self._sleep_deadline is not None
            # 種類は控えを消す前に読む。休止の予約を取り消したのに「スリープ」と
            # 出ては、何を取り消したのか分からない。
            name = "休止状態" if getattr(self, "_sleep_hibernate", False) else "スリープ"
            self._sleep_timer.stop()
            self._sleep_warn_timer.stop()
            self._sleep_deadline = None
            self._close_sleep_countdown()
            self._refresh_state()
            if had:
                action_log.record(f"{name} 予約を取り消し", "", source)
            if notify and had:
                self._notify(name, "予約を取り消しました")
        except Exception:
            self._log_failure("スリープ予約の取り消し")

    def sleep_seconds_left(self):
        """寝るまでの残り秒。予約していなければ None。"""
        if self._sleep_deadline is None:
            return None
        return max(0, int(self._sleep_deadline - time.monotonic()))

    # ---------------------------------------------------------------
    # いまの状態を1行ずつ言う(外からの status コマンド用)
    # ---------------------------------------------------------------
    # 外(名前付きパイプ)から叩く側は、この常駐が何を抱えているかを見る手段が無かった。
    # 掛けたはずの抑止が本当に効いているのか、予約が入ったのかを確かめられるようにする。
    # 状態を数えているのは各機能の側なので、ここは読んで文字にするだけにとどめる
    # (2か所で数えると必ずズレる。_on_sleep_tick と同じ考え方)。
    def keep_awake_status(self) -> str:
        if not self._awake_active:
            return "スリープ抑止: なし"
        left = _remaining_minutes(self._awake_deadline)
        if left is None:
            return "スリープ抑止: 有効（無期限）"
        return f"スリープ抑止: 有効（残り{_format_minutes(left)}）"

    def sleep_status(self) -> str:
        left = self.sleep_seconds_left()
        if left is None:
            return "スリープ予約: なし"
        name = "休止状態" if self._sleep_hibernate else "スリープ"
        return f"スリープ予約: {name}（残り{_format_seconds(left)}）"

    def mirror_status(self) -> str:
        try:
            return "画面ミラー: 動作中" if self.screen_mirror.is_active() else "画面ミラー: 停止中"
        except Exception:
            # 状態を1つ読めないだけで status 全体を落とさない。
            return "画面ミラー: 不明"

    def sticky_status(self) -> str:
        """開いている付箋の枚数。付箋は別プロセスなので、名前付きパイプの一覧から数える
        (capture_process.py 冒頭を参照)。"""
        try:
            return f"付箋: {len(capture_process.list_sticky_pipes())}枚"
        except Exception:
            return "付箋: 不明"

    def status_text(self) -> str:
        """status コマンドの本文。1状態1行。"""
        return "\n".join([
            self.keep_awake_status(),
            self.sleep_status(),
            self.mirror_status(),
            self.sticky_status(),
            self.jiggler_status(),
            self.claude_status_text(),
            self.version_status(),
            self.folder_watch_status(),
        ])

    def claude_status_text(self) -> str:
        """Claude Code の各セッション(トレイの輪・ツールチップと同じ中身)。"""
        entries = claude_status.list_sessions()
        if not entries:
            return "Claude: なし"
        parts = []
        for entry in entries:
            text = claude_status.menu_label(entry)
            plan = claude_status.plan_text(entry.get("plan"))
            if plan and entry.get("state") in (claude_status.RUNNING, claude_status.ASK):
                text += f"（{plan}）"
            parts.append(text)
        return "Claude: " + " / ".join(parts)

    def version_status(self) -> str:
        """起動時の版と、ディスクの方が新しいか(トレイのツールチップと同じ判定)。"""
        self._check_disk_version()   # 聞かれたついでに見直す(.git を読むだけで軽い)
        running = filer_update.describe_version(self._boot_version)
        if self._disk_newer:
            disk = filer_update.describe_version(_read_disk_version())
            return f"版: {running}（{DISK_NEWER_TEXT}: {disk}）"
        return f"版: {running}"

    def jiggler_status(self) -> str:
        if not self._jiggle_active:
            return "マウスジグラー: なし"
        left = _remaining_minutes(self._jiggle_deadline)
        if left is None:
            return "マウスジグラー: 有効（無期限）"
        return f"マウスジグラー: 有効（残り{_format_minutes(left)}）"

    def _ask_sleep_minutes(self, hibernate: bool = False):
        """何分後に寝るかを尋ねて予約する。"""
        try:
            name = "休止状態" if hibernate else "スリープ"
            minutes, ok = QInputDialog.getInt(
                None, name, f"何分後に{name}にしますか", 30, 0, 24 * 60, 5
            )
            if ok:
                self.schedule_sleep(int(minutes) * 60, hibernate)
        except Exception:
            self._log_failure("スリープ時刻の入力")

    def _on_sleep_tick(self):
        """1秒ごとに残りを見て、近づいていれば予告の窓を出す・数字を更新する。

        残りを数えているのはこちら(_sleep_deadline)だけで、窓は見せるだけ。
        2か所で数えると必ずズレる。"""
        try:
            left = self.sleep_seconds_left()
            if left is None:
                self._close_sleep_countdown()
                self._sleep_tick.stop()
                return
            if left > SLEEP_COUNTDOWN_SECONDS:
                return
            name = "休止状態" if self._sleep_hibernate else "スリープ"
            if self._sleep_countdown is None:
                self._sleep_countdown = SleepCountdown(self.cancel_sleep, name)
                self._sleep_countdown.show_for(left, name)
            else:
                self._sleep_countdown.set_seconds(left)
        except Exception:
            self._log_failure("スリープ予告の更新")

    def _close_sleep_countdown(self) -> None:
        """予告の窓を片付ける。出ていなければ何もしない。"""
        try:
            self._sleep_tick.stop()
            if self._sleep_countdown is not None:
                self._sleep_countdown.close()
                self._sleep_countdown = None
        except Exception:
            self._log_failure("スリープ予告の片付け")

    def _on_sleep_due(self):
        """予約の時刻になった。すぐには寝ず、猶予を置いて声を掛ける。"""
        try:
            name = "休止状態" if getattr(self, "_sleep_hibernate", False) else "スリープ"
            self._notify(
                name,
                f"{SLEEP_WARN_SECONDS}秒後に{name}にします（取り消すなら今）",
            )
            self._sleep_warn_timer.start(SLEEP_WARN_SECONDS * 1000)
        except Exception:
            self._log_failure("スリープ前の通知")

    def _do_sleep(self):
        """実際に寝かせる。掛けてある抑止は keep_awake.suspend が外す。"""
        try:
            hibernate = bool(getattr(self, "_sleep_hibernate", False))
            self._sleep_deadline = None
            self._close_sleep_countdown()
            self._refresh_state()
            name = "休止状態" if hibernate else "スリープ"
            action_log.record(f"{name} 実行", "", "timer")
            if not suspend(hibernate):
                action_log.record(f"{name} 失敗", "OSが受け付けなかった", "timer")
                self._notify(name, f"{name}にできませんでした")
        except Exception:
            self._log_failure("スリープの実行")

    def _on_awake_expired(self):
        self._disable_keep_awake(notify=False)
        self._notify("スリープ抑止", "時間切れで自動解除しました")

    def _refresh_awake_menu(self):
        for key, action in self._awake_actions.items():
            action.setChecked(self._awake_active and key == self._awake_minutes)

        if not self._awake_active:
            self._awake_menu.setTitle("☕ スリープ抑止")
        elif self._awake_minutes:
            self._awake_menu.setTitle(
                f"☕ スリープ抑止（残り{_remaining_minutes(self._awake_deadline)}分）"
            )
        else:
            self._awake_menu.setTitle("☕ スリープ抑止（無期限）")

    # ---------------------------------------------------------------
    # マウスジグラー
    # ---------------------------------------------------------------
    def _enable_jiggler(self, minutes):
        # ここで1回送って確かめる、はしない。メニューを操作した直後は当然「操作中」で、
        # 無操作の判定に引っかかって送らないため、成否を確かめようがない。
        # SendInput が弾かれている場合は最初の周期で分かる(_on_jiggle_tick を参照)。
        self._jiggle_expire_timer.stop()
        self._jiggle_active = True
        self._jiggle_minutes = minutes
        self._jiggle_timer.start(self._jiggle_interval_ms)
        if minutes:
            self._jiggle_deadline = time.monotonic() + minutes * 60
            self._jiggle_expire_timer.start(minutes * 60 * 1000)
            label = _format_minutes(minutes)
        else:
            self._jiggle_deadline = None
            label = "無期限"
        self._refresh_state()
        self._notify("マウスジグラー", f"有効: {label}")

    def _disable_jiggler(self, notify: bool = True):
        was_active = self._jiggle_active
        self._jiggle_timer.stop()
        self._jiggle_expire_timer.stop()
        self._jiggle_active = False
        self._jiggle_minutes = None
        self._jiggle_deadline = None
        self._refresh_state()
        if notify and was_active:
            self._notify("マウスジグラー", "解除しました")

    def _on_jiggle_expired(self):
        self._disable_jiggler(notify=False)
        self._notify("マウスジグラー", "時間切れで自動解除しました")

    def _on_jiggle_tick(self):
        """周期実行の受け口。無操作のときだけカーソルを+1px動かして戻す。

        PySide6はスロット内で例外を投げ切るとプロセスごと終わる。ここは席を外している
        間ずっと回り続ける場所なので、投げれば確実に踏む。必ず受けて常駐を続ける。"""
        try:
            result = mouse_jiggler.jiggle_if_idle(self._jiggle_idle_seconds)
            if result is None:
                return  # 操作中(または経過を取れなかった)。跳ねさせず次の周回へ
            if not result:
                # SendInput が弾かれている(管理者権限のウィンドウが前面など)。
                # このまま回しても毎周期失敗するだけなので、止めて1回だけ知らせる。
                self._disable_jiggler(notify=False)
                self._notify("マウスジグラー", "入力を送れないため停止しました")
        except Exception:
            self._disable_jiggler(notify=False)
            traceback.print_exc()
            print("[tray-tools] マウスジグラー: 実行に失敗したため停止しました", file=sys.stderr)

    def _update_tooltip(self):
        """ツールチップを組み直す。Claude の経過時間は刻々と変わるので、状態が変わらなくても
        _poll_claude_status から数秒ごとにここだけ呼ぶ(アイコンは触らない)。"""
        lines = [getattr(self, "_states_text", "Rapture")]
        entries = getattr(self, "_claude_entries", [])
        # 進捗パネルを出しているなら、Claude の中身はパネルに任せて重ねて出さない
        # (本人の声: 内容が被る)。パネルを切っているときだけ、ここに出す。
        if entries and not getattr(self, "_claude_progress_enabled", False):
            lines[0] = _join_state(lines[0], "Claude " + claude_status.LABELS.get(
                entries[0].get("state"), ""))
            for entry in entries[:CLAUDE_TOOLTIP_SESSIONS]:
                described = claude_status.describe(entry)
                lines.append(described[0])
                if len(described) > 1:
                    lines.append(_clip(described[1], CLAUDE_PLAN_CHARS + 4))
            if len(entries) > CLAUDE_TOOLTIP_SESSIONS:
                lines.append(f"ほか {len(entries) - CLAUDE_TOOLTIP_SESSIONS}件（メニューの Claude）")
        if getattr(self, "_disk_newer", False):
            lines.append(DISK_NEWER_TEXT)
        self.tray_icon.setToolTip(_fit_tooltip(lines))

    def _claude_ring_icon(self, state):
        icon = self._claude_icons.get(state)
        if icon is None and ICON_PATH.exists() and state in CLAUDE_RING_COLORS:
            icon = pil_to_qicon(_make_ring_icon_image(
                CLAUDE_RING_COLORS[state], AGENT_LOOP_RING_WIDTH))
            self._claude_icons[state] = icon
        return icon

    def _poll_claude_status(self):
        """フックが書いた状態を読み、輪の色が変わるときだけアイコンまで更新する。
        タイマーのスロットなので例外は受け止める(投げ切ると常駐ごと落ちる)。"""
        try:
            before = claude_status.overall_state(self._claude_entries)
            had_any = bool(self._claude_entries)
            self._claude_entries = claude_status.list_sessions()
            if claude_status.overall_state(self._claude_entries) != before:
                self._refresh_state()
            elif self._claude_entries or had_any:
                self._update_tooltip()
            self._update_claude_progress()
        except Exception:  # noqa: BLE001  表示が出ないだけ
            traceback.print_exc()

    def _read_copilot_turn(self):
        """Copilot の手番の知らせ(子プロセスが書いたファイル)を読む。

        監視が ON のときだけ読む。UIA には触らない(ファイルを読むだけ)。
        読めない・古い・一時停止中なら None(パネルには出さない)。"""
        try:
            if not self._copilot_watchdog.is_enabled():
                return None
            import copilot_watchdog as cw
            return cw.read_turn_status()
        except Exception:  # noqa: BLE001  表示が出ないだけ
            return None

    def _read_muse_bridge(self):
        """Muse 見張りの状態ファイルを読む。無ければ None(パネルに出さない)。

        ツールチップ側の表示と同じファイル(muse_bridge_status)。読むだけ。

        環境2・3ではブリッジ自体を出さない(Claude も Muse も使えない環境の
        ため。環境は setup で指定する settings.json の environment。あっふの
        スマホ送信(QR)が環境1だけ有効なのと同じ作法で、未設定は環境1扱い)。"""
        if self.app_settings.get("environment") in (2, 3):
            return None
        try:
            data = muse_bridge_status.read()
        except Exception:  # noqa: BLE001  表示が出ないだけ
            data = None
        # 書き込みとぶつかって読めない回があっても、連携ステージ行が1周期消えないよう、
        # 直前の良いデータを最大 HOLD_SEC 秒だけ保持する
        data, self._muse_last_good, self._muse_last_good_at = muse_bridge_status.hold_last_good(
            data, getattr(self, "_muse_last_good", None),
            getattr(self, "_muse_last_good_at", None), time.time())
        return data

    def _read_bridge_events(self):
        """やり取りの記録(bridge_journal)を読む。ステージの実イベント飛行と
        履歴テープの材料。環境2・3では読まない(ブリッジ自体を出さない、
        _read_muse_bridge と同じ作法)。

        飛行用に直近60秒、テープ用に直近の数件を取り、重ねて返す
        (重複は受け取り側の bridge_events_view が除く)。読めなければ
        直前の結果を保つ(1周期だけ材料が古くても、絵が消えたりしない)。"""
        if self.app_settings.get("environment") in (2, 3):
            return []
        try:
            import bridge_journal
            now = time.time()
            events = bridge_journal.since(now - 60) + bridge_journal.tail(8)
        except Exception:  # noqa: BLE001  表示の材料が無いだけ
            return getattr(self, "_bridge_events", [])
        self._bridge_events = events
        return events

    def _update_claude_progress(self):
        copilot = self._read_copilot_turn()
        muse = self._read_muse_bridge()
        events = self._read_bridge_events()
        if not self._claude_progress_enabled:
            if self._claude_progress is not None:
                self._claude_progress.set_entries([], None, None)
            return
        if self._claude_progress is None:
            if not self._claude_entries and copilot is None and muse is None:
                return   # 出すものが無いうちは窓を作らない
            self._claude_progress = claude_progress.ClaudeProgressPanel(
                on_open=self._open_claude_session, on_remove=self._remove_claude_session,
                on_open_copilot=self._activate_copilot)
        self._claude_progress.set_entries(self._claude_entries, copilot, muse,
                                          events=events)

    def _open_claude_session(self, entry):
        """そのセッションをデスクトップアプリで開く(メニューとパネルの行から)。"""
        try:
            if not claude_status.open_in_app(entry):
                show_toast("Claude\nこのセッションはアプリで開けません"
                           "（ターミナルの Claude Code か、記録が古い）")
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _remove_claude_session(self, entry):
        """進捗パネルの ✕。そのセッションを一覧から消し、輪とツールチップもすぐ直す。"""
        try:
            claude_status.remove(entry)
            self._claude_entries = claude_status.list_sessions()
            self._refresh_state()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _toggle_claude_progress(self, checked):
        try:
            self._claude_progress_enabled = bool(checked)
            claude_progress.save_state({"enabled": self._claude_progress_enabled})
            self._update_claude_progress()
            self._update_tooltip()
            # パネルが Copilot 監視の子の要否にも関わるので、そちらも合わせる。
            self._sync_copilot_watchdog()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _reset_claude_progress_position(self):
        try:
            if self._claude_progress is not None:
                self._claude_progress.reset_position()
            else:
                claude_progress.save_state({"pos": None})
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _open_checklist(self):
        """トレイのメニューから実機確認チェックリストをブラウザで開く。"""
        try:
            browser_open.open_url(browser_open.checklist_url(self.app_settings),
                                  self._browser_name())
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _show_bridge(self):
        """メニューの「ブリッジを開く」。進捗パネルが無効なら有効にして、
        ブリッジ(連携ステージ)を ひろげて出す。見張りが動いていなくて
        出す行が無いときは、窓は出ない(既存の作法どおり)。"""
        try:
            if not self._claude_progress_enabled:
                self._toggle_claude_progress(True)
            self._update_claude_progress()
            if self._claude_progress is not None:
                self._claude_progress.show_bridge()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _set_bridge_size(self, size):
        """ブリッジの大きさの切り替え(💬メニューから)。パネルがまだ無ければ
        状態ファイルにだけ書いておく(作られたときに読む)。"""
        try:
            if self._claude_progress is not None:
                self._claude_progress.set_bridge_size(size)
            else:
                claude_progress.save_state({"bridge_size": size})
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _rebuild_claude_menu(self):
        try:
            self._claude_entries = claude_status.list_sessions()
            menu = self._claude_menu
            menu.clear()
            # 一覧は進捗パネルと被るので、パネルを切っているときだけ出す(ツールチップと同じ)。
            listed = [] if self._claude_progress_enabled else self._claude_entries
            if not self._claude_progress_enabled and not listed:
                action = menu.addAction("（動いているセッションはありません）")
                action.setEnabled(False)
            for entry in listed:
                # 押すとそのセッションをアプリで開く。開けないもの(ターミナル)は押せなくする。
                action = menu.addAction(
                    claude_status.menu_label(entry),
                    lambda _checked=False, e=entry: self._open_claude_session(e))
                action.setEnabled(bool(claude_status.jump_url(entry)))
                plan = claude_status.plan_text(entry.get("plan"))
                if plan and entry.get("state") in (claude_status.RUNNING, claude_status.ASK):
                    detail = menu.addAction("　　" + _clip(plan, 40))
                    detail.setEnabled(False)
            if listed:
                menu.addSeparator()
            panel = menu.addAction("📊 進捗パネルを表示")
            panel.setCheckable(True)
            panel.setChecked(self._claude_progress_enabled)
            # addAction(文字, 関数) だと checked が渡らないので triggered で受ける。
            panel.triggered.connect(self._toggle_claude_progress)
            menu.addAction("🌉 ブリッジを開く", self._show_bridge)
            size_menu = menu.addMenu("🌉 ブリッジの大きさ")
            current_size = (self._claude_progress.bridge_size()
                            if self._claude_progress is not None
                            else claude_progress.load_bridge_size())
            for key in claude_progress.BRIDGE_SIZES:
                action = size_menu.addAction(claude_progress.BRIDGE_SIZE_LABELS[key])
                action.setCheckable(True)
                action.setChecked(key == current_size)
                action.triggered.connect(
                    lambda _checked=False, k=key: self._set_bridge_size(k))
            menu.addAction("↘ パネルの位置を右下に戻す", self._reset_claude_progress_position)
            self._refresh_state()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _resident_files_changed(self, disk: dict) -> bool:
        """起動時から今のコミットまでに、常駐が読むファイルが変わったか。

        ハッシュが違っても、変わったのがフック・テスト・あっふだけなら再起動は要らない
        (あっふの「常駐も再起動しますか？」と同じ resident_files で判定する)。git で
        調べられない(ZIP 展開・git が無い)ときは「変わった」に倒す。結果はコミットごとに
        覚えておき、同じコミットで git を何度も叩かない。"""
        old_sha = (self._boot_version or {}).get("sha") or ""
        new_sha = (disk or {}).get("sha") or ""
        cache = getattr(self, "_resident_change_cache", None)
        if cache and cache[0] == new_sha:
            return cache[1]
        files = filer_update.changed_files(old_sha, new_sha)
        changed = True if files is None else bool(filer_update.resident_files(files))
        self._resident_change_cache = (new_sha, changed)
        return changed

    def _check_disk_version(self):
        """ディスクの版が起動時より新しければ、ツールチップと 🔄 の項目に出す。
        メニューの aboutToShow とタイマーから呼ばれるスロットなので、例外は受け止める。"""
        try:
            disk = _read_disk_version()
            newer = filer_update.disk_is_newer(self._boot_version, disk)
            if newer:
                newer = self._resident_files_changed(disk)
            self._restart_action.setText(
                f"🔄 再起動（{DISK_NEWER_TEXT}）" if newer else "🔄 再起動")
            if newer != self._disk_newer:
                self._disk_newer = newer
                self._refresh_state()
        except Exception:  # noqa: BLE001  表示が出ないだけ
            traceback.print_exc()

    def _refresh_jiggle_menu(self):
        for key, action in self._jiggle_actions.items():
            action.setChecked(self._jiggle_active and key == self._jiggle_minutes)

        if not self._jiggle_active:
            self._jiggle_menu.setTitle("🖱 マウスジグラー")
        elif self._jiggle_minutes:
            self._jiggle_menu.setTitle(
                f"🖱 マウスジグラー（残り{_remaining_minutes(self._jiggle_deadline)}分）"
            )
        else:
            self._jiggle_menu.setTitle("🖱 マウスジグラー（無期限）")

    def _refresh_state(self):
        """スリープ抑止・監視モード・マウスジグラーの状態をトレイアイコンと
        メニューに反映する。

        アイコンの優先順位: 監視モード(色でさらに細分) > スリープ抑止 > 通常。
        監視モードを最優先にするのは、これがいちばん見落としたときの被害が
        大きいから(裏で Copilot とやり取りしていて実行までしている状態)。"""
        icon = self._normal_icon
        state = self._agent_loop_state
        # Claude の確認待ちだけは監視モードより上に置く(人が承認するまで進まない)。
        # それ以外の Claude の状態は監視モードとスリープ抑止の間。
        claude_state = claude_status.overall_state(getattr(self, "_claude_entries", []))
        if claude_state == claude_status.ASK:
            icon = self._claude_ring_icon(claude_state) or icon
        elif state == "busy":
            if self._agent_loop_busy_icon is None and ICON_PATH.exists():
                self._agent_loop_busy_icon = pil_to_qicon(_make_ring_icon_image(
                    AGENT_LOOP_BUSY_COLOR, AGENT_LOOP_RING_WIDTH))
            icon = self._agent_loop_busy_icon or icon
        elif state == "err":
            if self._agent_loop_err_icon is None and ICON_PATH.exists():
                self._agent_loop_err_icon = pil_to_qicon(_make_ring_icon_image(
                    AGENT_LOOP_ERR_COLOR, AGENT_LOOP_RING_WIDTH))
            icon = self._agent_loop_err_icon or icon
        elif state == "watching":
            if self._agent_loop_ring_icon is None and ICON_PATH.exists():
                self._agent_loop_ring_icon = pil_to_qicon(_make_ring_icon_image(
                    AGENT_LOOP_RING_COLOR, AGENT_LOOP_RING_WIDTH))
            icon = self._agent_loop_ring_icon or icon
        elif claude_state is not None:
            icon = self._claude_ring_icon(claude_state) or icon
        elif self._awake_active:
            if self._awake_icon is None and ICON_PATH.exists():
                self._awake_icon = pil_to_qicon(_make_awake_icon_image())
            icon = self._awake_icon or icon
        self.tray_icon.setIcon(icon)
        # アイコンの見た目(リング)は上の優先順位で埋まる。他の状態はツールチップで示す。
        # 実質16pxのアイコンに2つ目の目印を入れても潰れて読めない。
        states = []
        if state == "busy":
            states.append("エージェントループ 実行中")
        elif state == "watching":
            states.append("エージェントループ 待機中")
        elif state == "err":
            states.append("エージェントループ 停止：要確認")
        if self._awake_active:
            states.append("スリープ抑止中")
        if self._jiggle_active:
            states.append("マウスジグラー動作中")
        self._states_text = f"Rapture（{'・'.join(states)}）" if states else "Rapture"
        self._update_tooltip()
        self._refresh_awake_menu()
        self._refresh_jiggle_menu()
        self._refresh_agent_loop_menu()

    # -- エージェントループ(監視モード)の統合 -----------------------
    # main.py の _agent_loop_command から on_agent_loop_event を getattr で拾って
    # 渡している。ここでは agent_loop のワーカースレッドから直接呼ばれても
    # 安全に受けるため、Qt のシグナル/スロット(LogViewer 内)へ渡すだけにする。
    def on_agent_loop_event(self, payload: dict) -> None:
        """agent_loop.run_loop() の on_event として渡す入口。ワーカースレッド。

        - ログ窓は QueuedConnection で受けるのでスレッド跨ぎ OK
        - 状態(トレイアイコン)の更新は Qt を触るのでシグナル経由でメインへ
        """
        try:
            viewer = self._agent_loop_viewer
            if viewer is not None:
                viewer.on_agent_loop_event(payload)
            # 状態遷移は QMetaObject.invokeMethod でメインスレッドに投げるのが本筋だが、
            # ここでは単純に _agent_loop_bridge.state_changed シグナルを使う。
            self._agent_loop_bridge.state_changed.emit(payload)
        except Exception as e:  # noqa: BLE001  ここで落とすとループ全体が止まる
            print(f"[agent-loop] on_event 失敗: {e}", file=sys.stderr)

    def _on_agent_loop_state_event(self, payload: dict) -> None:
        """メインスレッドで受ける。イベントを見てトレイアイコンの状態を切り替える。"""
        try:
            event = payload.get("event", "")
            new_state = None
            if event == "loop_start":
                new_state = "watching"
                self._agent_loop_active = True
                # ログ窓を出す。無ければここで作る。
                #
                # 【無ければ何もしていなかった】
                # 以前は「既にあれば前面に出す」だけだった。トレイのメニューから
                # 始めたときは、その手前で窓を作っているので問題にならない。
                # ところが**状態監視バーの右クリックや外からの traytools_send で
                # 始めると窓が無く、実況を見る手段が無いまま走っていた**
                # (実測 2026-09-11: バーから始めても窓が出なかった)。
                # どこから始めても同じように見えるべきなので、ここで揃える。
                # 窓が出せなくても、アイコンの更新・周回数の書き出し・通知は
                # 続ける。ここで投げると、この下が丸ごと飛ぶ。
                try:
                    viewer = self._ensure_agent_loop_viewer()
                    viewer.show()
                    viewer.raise_()
                except Exception as e:  # noqa: BLE001
                    print(f"[agent-loop] ログ窓を出せませんでした: {e}",
                          file=sys.stderr)
            elif event in ("response", "snippet", "run"):
                new_state = "busy"
            elif event == "round_end":
                # 実行 → 次周に入るまでの間だけ watching(rest 状態)
                reason = payload.get("reason")
                if not reason:
                    new_state = "watching"
            elif event == "loop_end":
                # 走り終わったことは、止まりかたによらず必ず記録する。
                # 見た目(err)は残しても、走っているかどうかは別。
                self._agent_loop_active = False
                reason = payload.get("reason") or ""
                if reason in ("risky-code", "error", "response-timeout",
                              "loop-timeout", "empty-response", "stuck"):
                    new_state = "err"
                else:
                    new_state = "idle"
            if new_state is not None:
                self._agent_loop_state = new_state
                self._refresh_state()
            self._publish_agent_loop_status(payload)
            self._announce_agent_loop(payload)
        except Exception as e:  # noqa: BLE001
            print(f"[agent-loop] 状態反映に失敗: {e}", file=sys.stderr)

    # -- ループの様子を「離れていても分かる」ようにする ----------------
    def _publish_agent_loop_status(self, payload: dict) -> None:
        """いま何周目かを、状態監視バーが読めるファイルに書き出す。

        バーは Copilot の状態しか知らないので、ループが回している間も
        「応答待ち」としか出ない。生きているのか固まったのかが遠目に分からない。
        周回数が出ていれば、進んでいることが一目で分かる。

        書けなくても作業には影響しないので、失敗は黙って諦める(バーの表示が
        今までどおりになるだけ)。"""
        import copilot_watchdog as cw
        event = payload.get("event", "")
        try:
            if event == "loop_end":
                # 終わったら消す。残すと、次に札が出たときに古い周回数が出る。
                try:
                    os.remove(cw.LOOP_STATUS_FILE)
                except FileNotFoundError:
                    pass
                return
            if event == "round_start":
                self._agent_loop_round = payload.get("round")
            elif event == "loop_start":
                self._agent_loop_round = 0
                self._agent_loop_max_rounds = payload.get("max_rounds") or 0
            elif event not in ("approval_request", "approval_result"):
                # 周の途中の細かいイベント(response / snippet / run)は書かない。
                #
                # 【一度は書いていた。無意味だった】
                # 実ログでは、応答が来てから次の周に入るまでが1秒未満で終わる
                # (01:11:08 に response → snippet → run → round_end → round_start が
                # 全部並ぶ)。書いた文言は数ミリ秒で次の round_start に上書きされ、
                # 札に出る隙が無い。周回数だけが意味を持つ。
                return
            # 例外は「返事待ち」。あれは人が答えるまで最大5分止まるので、
            # 札に出る時間がある —— というより、**そこに出したい情報そのもの**。
            label = "返事待ち" if event == "approval_request" else ""
            with open(cw.LOOP_STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump({"round": self._agent_loop_round or 0,
                           "max_rounds": self._agent_loop_max_rounds or 0,
                           "label": label,
                           "at": time.time()}, f, ensure_ascii=False)
        except OSError:
            pass

    def _announce_agent_loop(self, payload: dict) -> None:
        """人を呼ぶ場面だけ、音と通知を出す。

        出すのは2つだけ。**返事待ち**(誰かが答えないと止まる)と**終了**
        (席を外している間に終わっていることが多い)。周ごとに鳴らすと、
        長い題材では鳴りっぱなしになって意味を失う。"""
        event = payload.get("event", "")
        try:
            import beep
            if event == "approval_request":
                beep.play("ask")
                self._notify("エージェントループ",
                             f"返事待ち: {payload.get('summary') or ''}")
            elif event == "loop_end":
                reason = payload.get("reason") or ""
                bad = reason in ("risky-code", "error", "response-timeout",
                                 "loop-timeout", "stuck")
                beep.play("warn" if bad else "done")
                self._notify(
                    "エージェントループ 終了",
                    f"{reason} — {payload.get('rounds')}周 / "
                    f"{payload.get('elapsed')}秒")
        except Exception as e:  # noqa: BLE001  知らせられなくても作業は続く
            print(f"[agent-loop] 通知に失敗: {e}", file=sys.stderr)

    def start_agent_loop_watch(self, take_last: bool = False) -> None:
        """トレイメニューから呼ぶ「監視モード開始」。

        take_last=True なら、新しい応答を待たず画面に出ている最後の応答を引き取る。
        Copilot と何往復かしてから「この続きを自動で回したい」ときの入口。

        非同期で常駐スレッドを起こす。ここは Qt メインスレッド。実処理は
        main._agent_loop_command と同じ経路を通したいので、traytools_send 経由で
        自プロセスの名前付きパイプに投げるのではなく、直接 run_loop を回すスレッドを
        立てる。ログ窓は先に開いておく(loop_start が届く頃には見える)。"""
        # 実行中なら二重起動しない
        import agent_loop as al
        # 既に走っていれば窓を前面に戻して終わり
        # 見に行くのは「走っているか」だけ。表示の状態(err が残る)と混ぜると、
        # 危ない止まりかたのあと二度と始められなくなる(_agent_loop_active を参照)。
        if self._agent_loop_active:
            if self._agent_loop_viewer is not None:
                self._agent_loop_viewer.show()
                self._agent_loop_viewer.raise_()
            return
        # ログ窓を先に用意
        self._ensure_agent_loop_viewer()
        self._agent_loop_viewer.show()
        self._agent_loop_viewer.raise_()
        self._agent_loop_viewer.append_note(
            "監視モード開始。Copilot に手動でお題を投稿してください。"
            "応答が始まると tray-tools が引き取ります。"
        )

        # 実処理は別プロセスで回す。常駐の中で UIA を使うと pycaw と衝突して
        # 即死するため(copilot_watchdog.py 冒頭)。起こし方は agent_loop.spawn に
        # まとめてあり、IPC 経由(main._agent_loop_command)と同じ道を通る。
        try:
            proc, _reader = al.spawn(
                watch=True, auto=True, max_rounds=10,
                # 定型文が「終わったら <DONE> と書いて」で締めているので、それを
                # 完了の合図として拾う。渡さないと上限まで回るか no-snippet で
                # 止まり、やり切ったのか途中で力尽きたのか区別がつかない。
                finish_word="DONE",
                take_last=take_last,
                on_event=self.on_agent_loop_event,
                parent_pid=os.getpid(),
            )
        except OSError as e:
            self._agent_loop_viewer.append_note(f"起こせませんでした: {e}")
            return
        self._agent_loop_proc = proc
        # loop_start が届くのを待たずに立てる。子が起きて最初のイベントを返すまでの
        # 隙間に二度押しされると、ループが2本走ってしまう。
        self._agent_loop_active = True
        self._agent_loop_state = "watching"
        self._refresh_state()
        self._refresh_agent_loop_menu()

    def stop_agent_loop(self) -> None:
        """トレイメニューから呼ぶ「監視モード停止」。次の周の頭で止まる。

        キャンセルはファイルのフラグで伝える。元からプロセスを跨げる作りだったので、
        子プロセスに出したあともそのまま効く(IPC を足す必要が無かった)。
        止まるのは次の周の頭なので、応答待ちの最中は少し待つことになる。"""
        try:
            import agent_loop as al
            al.request_cancel()
        except Exception as e:  # noqa: BLE001
            print(f"[agent-loop] キャンセル要求失敗: {e}", file=sys.stderr)

    def _kill_agent_loop(self) -> None:
        """常駐を終わらせるときに、監視モードの子を道連れにする。

        残しても止める手段(トレイのメニュー)が無くなるうえ、Copilot に勝手に
        書き込み続けることになる。付箋と違って生き残らせる理由が無い。"""
        proc, self._agent_loop_proc = self._agent_loop_proc, None
        # 殺すのだから、もう走っていない。loop_end は届かないので自分で下ろす。
        self._agent_loop_active = False
        # 周回数の置き手紙も消す。残すと、次に札が出たときに終わったループの
        # 周回数が出る(古すぎれば札の側でも無視するが、待たせる理由が無い)。
        try:
            import copilot_watchdog as cw
            os.remove(cw.LOOP_STATUS_FILE)
        except OSError:
            pass
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except OSError:
                pass

    def _sync_copilot_watchdog(self) -> None:
        """監視の子を、表示(バー/パネル)の状態に合わせて起こす/止める。

        監視そのものに入切は無い。バーか進捗パネルのどちらかが出る設定の
        間だけ子が要る(Claude のフックに開始ボタンが無いのと同じ思想)。
        両方オフなら子も止まる。"""
        try:
            want = (self._copilot_watchdog.bar_enabled()
                    or self._claude_progress_enabled)
            if want != self._copilot_watchdog.is_enabled():
                self._copilot_watchdog.set_enabled(want)
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-watchdog] 同期に失敗: {e}", file=sys.stderr)

    def _on_copilot_watchdog_exited(self) -> None:
        """札の右クリックで「終了」されたときに呼ばれる。バーを消したものとして扱う。

        監視そのものは止めない: パネルが出る設定なら、バー無しで子を起こし直す
        (_sync_copilot_watchdog)。チェックを外しておかないと、次に押したときに
        「出す」操作が逆になってしまう。"""
        try:
            self._copilot_watchdog.set_bar_enabled(False)
            if self._copilot_watchdog_action is not None:
                self._copilot_watchdog_action.setChecked(False)
            self._sync_copilot_watchdog()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-status] メニューの更新に失敗: {e}", file=sys.stderr)

    def copilot_bar_command(self, args) -> str:
        """外から状態監視バーを入切する。on / off / status。

        入切するのはバーの表示だけ(監視はバー/パネルの表示状態に従う)。
        メニューのチェックも合わせて動かす。片方だけ変わると、次に押したときに
        逆の操作になってしまう。"""
        mode = (str(args[0]).strip().lower() if args else "status")
        if mode == "status":
            return ("OK 状態監視バー: "
                    + ("表示中" if self._copilot_watchdog.bar_enabled() else "非表示")
                    + ("（監視中）" if self._copilot_watchdog.is_enabled()
                       else "（監視なし）"))
        if mode in ("on", "1", "true", "start", "show"):
            want = True
        elif mode in ("off", "0", "false", "stop", "hide"):
            want = False
        else:
            return f"ERR 不明な指示: {mode}（on / off / status）"
        try:
            self._copilot_watchdog.set_bar_enabled(want)
            if self._copilot_watchdog_action is not None:
                self._copilot_watchdog_action.setChecked(want)
            self._sync_copilot_watchdog()
        except Exception as e:  # noqa: BLE001
            return f"ERR 切り替えられませんでした: {e}"
        return "OK 状態監視バー: " + ("表示中" if want else "非表示")

    def _toggle_copilot_bar(self, checked: bool) -> None:
        """状態監視バーを出す/消す。監視は止めない(パネル用に続くことがある)。"""
        try:
            self._copilot_watchdog.set_bar_enabled(bool(checked))
            self._sync_copilot_watchdog()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-watchdog] 切替失敗: {e}", file=sys.stderr)

    def _activate_copilot(self, _entry=None):
        """進捗パネルの Copilot 行クリック: Copilot の窓を前面に出す。

        子プロセスが知らせに書いた hwnd へ ctypes の user32 だけで触る
        (UIA/COM は常駐に入れない。copilot_watchdog 冒頭の実測どおり)。
        最小化されていたら戻してから前面へ。

        【ctypes の型は専用の WinDLL で指定する】
        リポジトリの決まり(AGENTS.md: argtypes / restype を必ず指定する)。
        ctypes.windll.user32 の関数オブジェクトへ直接書き込むと、同じ
        オブジェクトを使う他のモジュールにも影響が広がるので、専用の
        WinDLL をここで作ってそちらにだけ型を書く。hwnd は知らせ(JSON)から
        来る int なので wintypes.HWND で渡す(値が大きいと型なしでは変換で
        例外になり、押しても何も起きなくなる)。
        WinDLL は呼ばれたときにだけ作る(import 時に作ると、WinDLL の無い
        Linux ではテストの import で落ちる)。"""
        try:
            turn = self._read_copilot_turn()
            hwnd = (turn or {}).get("hwnd")
            if not hwnd:
                return
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsWindow.restype = wintypes.BOOL
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            handle = wintypes.HWND(hwnd)
            if not user32.IsWindow(handle):
                return
            if user32.IsIconic(handle):
                user32.ShowWindow(handle, 9)  # SW_RESTORE
            user32.SetForegroundWindow(handle)
        except Exception:  # noqa: BLE001  表示側の操作。失敗しても落とさない
            traceback.print_exc()

    def _ensure_agent_loop_viewer(self):
        """ログ窓を1つだけ作って使い回す。

        開始時と「ログ窓を前面に」の2か所から要る。同じ引数を2か所に書いていて、
        片方だけ直す事故が起きやすかったのでここにまとめた。"""
        if self._agent_loop_viewer is None:
            import agent_loop_viewer as av
            self._agent_loop_viewer = av.LogViewer(
                self.app_settings, self.settings_path,
                # 実況を見ている場所から直接止められるようにする。
                # 様子がおかしいと気づくのはたいていこの窓の前なので、
                # そこからトレイまで戻らせない。
                on_start=self.start_agent_loop_watch,
                on_stop=self.stop_agent_loop,
                # 「続けてよいか」への返事。実体は別プロセスなので、返事も
                # ファイル経由で届ける(停止フラグと同じ流儀)。
                on_answer=self._answer_agent_loop,
            )
        return self._agent_loop_viewer

    def _answer_agent_loop(self, token: str, answer: str) -> None:
        """ログ窓の承認ボタンから呼ばれる。子プロセスへ返事を書く。"""
        try:
            import agent_loop as al
            al.answer_approval(token, answer)
        except Exception as e:  # noqa: BLE001  返事を送れなくてもループは時間切れで止まる
            print(f"[agent-loop] 返事の送信に失敗: {e}", file=sys.stderr)

    def _show_agent_loop_log(self) -> None:
        """トレイメニューから呼ぶ「ログ窓を前面に」。実行中でなくても、
        直近の実行結果を振り返るために窓は残しておく。"""
        viewer = self._ensure_agent_loop_viewer()
        viewer.show()
        viewer.raise_()

    def _refresh_agent_loop_menu(self) -> None:
        """メニューの見出しと、開始/停止項目の押せる押せないを更新する。

        見出しに出すのはエージェントループの状態だけ。状態監視バーの入切は
        チェックマークで分かるので、見出しに混ぜると何の状態か読み取れなくなる。"""
        if not hasattr(self, "_agent_loop_menu") or self._agent_loop_menu is None:
            return
        suffix = AGENT_LOOP_MENU_SUFFIX.get(self._agent_loop_state, "")
        self._agent_loop_menu.setTitle(AGENT_LOOP_MENU_TITLE + suffix)
        if self._agent_loop_start_action:
            self._agent_loop_start_action.setEnabled(not self._agent_loop_active)
        if getattr(self, "_agent_loop_start_last_action", None):
            self._agent_loop_start_last_action.setEnabled(not self._agent_loop_active)
        if self._agent_loop_stop_action:
            self._agent_loop_stop_action.setEnabled(self._agent_loop_active)
        if self._agent_loop_show_log_action:
            self._agent_loop_show_log_action.setEnabled(self._agent_loop_viewer is not None)

    def _on_quit(self):
        # 付箋(Rapture)はここで閉じない。別プロセスで動いており、本体を終了・再起動
        # しても生き残るのが狙いだから。見つけて閉じて回る処理を足さないこと。
        #
        # 待機役は別。まだ何も表示していないので残す理由が無く、置いていくと使われない
        # プロセスが1つ居座る。ここで店じまいを頼む(呼べずに落ちた場合の受け皿は
        # 待機役側にもある。capture_process._WarmHost._check_parent 参照)。
        self._prewarm_timer.stop()
        try:
            capture_process.shutdown_warm()
        except OSError as e:
            print(f"[rapture] 待機役を終われませんでした: {e}", file=sys.stderr)
        self._awake_timer.stop()
        set_keep_awake(False)
        # 止め忘れると、終了処理の途中でタイマーが回って入力を送りに行く。
        self._jiggle_timer.stop()
        self._jiggle_expire_timer.stop()
        # フォルダ監視移動のタイマーも止める(終了処理の途中でファイルを動かさない)。
        for timer in self._folder_watch_timers:
            timer.stop()
        self.topmost.release_all()
        # 枠なし・最前面の窓を残したままイベントループを畳むと、画面に貼り付いたまま
        # 消えないことがある。タイマーもここで止まる(hideEvent 参照)。
        self._taskbar_rebuild_timer.stop()
        self._close_taskbar_widgets()
        # プレゼン支援も同じ。黒画面を出したまま終わらせると画面が真っ黒のまま残り、
        # しかも畳む手段(このアプリ)がもう居ない。
        self.presenter_overlays.close_all()
        # 画面ミラーも同じ。全画面の窓と手元の枠を残したまま終わると、映しっぱなしの
        # まま畳む手段(このアプリ)が居なくなる。
        self.screen_mirror.close_all()
        # Copilot まわりの子プロセスも道連れにする。残しても止める手段(トレイの
        # メニュー)が無くなるし、監視モードは Copilot に書き込み続けてしまう。
        try:
            self._copilot_watchdog.close()
        except Exception as e:  # noqa: BLE001
            print(f"[copilot-watchdog] 終了処理に失敗: {e}", file=sys.stderr)
        try:
            self._kill_agent_loop()
        except Exception as e:  # noqa: BLE001
            print(f"[agent-loop] 終了処理に失敗: {e}", file=sys.stderr)

    def attach_restart(self, restart) -> None:
        """自分を起動し直す手段を受け取る。組み立ては main.py が行う。"""
        self._restart_app = restart

    def restart_blockers(self) -> list:
        """いま再起動すると巻き添えになるものを並べる。空なら落としても失われない。

        【何のためにあるか】
        この常駐を落とすと、ぶら下がっているものが道連れになる。付箋は貼ったまま
        作業しているかもしれず、画面ミラーは見ている最中かもしれない。どちらも
        「開き直せばいい」ものではない(付箋の中身は消える)。
        トレイの 🔄 を押すのは本人なので巻き添えを承知で押せるが、**外から叩く口には
        その判断が無い**。だから口の側に、落としてよいかを答えられる材料を持たせる。

        数え落とすより数え過ぎるほうが安全なので、状態が読めないものは
        「あるかもしれない」側に倒す。"""
        blockers = []
        try:
            sticky = len(capture_process.list_sticky_pipes())
            if sticky:
                blockers.append(f"付箋 {sticky}枚")
        except Exception:  # noqa: BLE001  読めないなら「あるかも」に倒す
            blockers.append("付箋(枚数を読めず)")
        try:
            if self.screen_mirror.is_active():
                blockers.append("画面ミラー")
        except Exception:  # noqa: BLE001
            blockers.append("画面ミラー(状態を読めず)")
        if self._agent_loop_active:
            blockers.append("エージェントループ")
        if self._teams_meeting_running():
            # 子は常駐が落ちても最後まで進むが、結果の知らせと記録が受け手ごと消える。
            # 長くても数分(各手順の待ちの合計)で終わるので、待ってもらうほうが安い。
            blockers.append("一人会議の立ち上げ")
        if self.sleep_seconds_left() is not None:
            # 予約は再起動で消える。寝るつもりだったのに寝ないのは、
            # 気づきにくいぶん厄介。
            blockers.append("スリープ予約")
        editor = getattr(self, "text_editor", None)
        if editor is not None:
            # EDDy は控え(ドラフト)を常に取っているので、未保存でも
            # 再起動で内容は消えず、次に開けばタブごと戻る。だから
            # 巻き添えには数えない(dirty_window_state が
            # restart_loss_count を使う。セッション有効な窓は 0)。
            # 状態が読めないときだけ「あるかも」に倒す。
            try:
                import editor as editor_module

                dirty, unreadable = editor_module.dirty_window_state([editor])
                if dirty:
                    blockers.append(f"未保存のEDDy {dirty}窓")
                if unreadable:
                    blockers.append("EDDy(状態を読めず)")
            except Exception:  # noqa: BLE001  読めないなら「あるかも」に倒す
                blockers.append("EDDy(状態を読めず)")
        return blockers

    def restart_from_ipc(self) -> str:
        """外から頼まれた再起動。巻き添えが出るなら断る。

        戻り値は応答の1行。実際に落ちるのはイベントループが一巡してからなので、
        呼んだ側へ先に返事を返せる。"""
        blockers = self.restart_blockers()
        if blockers:
            return ("ERR 再起動しませんでした（"
                    + "・".join(blockers)
                    + " が道連れになります）。トレイの 🔄 から行ってください")
        if self._restart_app is None:
            return "ERR この起動のしかたでは再起動できません"
        if not self._restart_app():
            return "ERR 起動し直せませんでした（このまま動き続けます）"
        # 返事を書いてから終わる。先に quit すると応答が届かない。
        QTimer.singleShot(0, QApplication.instance().quit)
        return "OK 再起動します（数秒で戻ります）"

    def _restart(self):
        """メニューからの再起動。

        設定を書き換えたあとや、ディスプレイの構成を変えたあとに使う。手で終了して
        起動し直すには、この常駐アプリをどう起動しているか(venvのpythonw)を覚えている
        必要があり、それを思い出さずに済ませるための項目。"""
        try:
            if self._restart_app is None:
                self._notify("再起動", "この起動のしかたでは再起動できません")
                return
            editor = getattr(self, "text_editor", None)
            if editor is not None:
                # 未保存のエディタがあるまま再起動すると、編集中の内容が
                # 消えるうえ、「保存しますか」で止まって二重に動く。
                # 先に確かめる(restart_blockers と同じ判定)。
                try:
                    import editor as editor_module

                    dirty, unreadable = editor_module.dirty_window_state(
                        [editor])
                except Exception:  # noqa: BLE001  読めないなら止まる側に倒す
                    dirty, unreadable = 0, 1
                if dirty or unreadable:
                    answer = QMessageBox.question(
                        self.menu, "再起動",
                        "未保存のEDDyがあります。再起動すると編集中の"
                        "内容が失われます。\nそれでも再起動しますか？",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                    if answer != QMessageBox.Yes:
                        return
            if self._restart_app():
                # メニューの exec() が回しているイベントループの中から quit() を呼ぶと、
                # そのループを抜けるだけでアプリ本体が終わらないことがある。実際、
                # 再起動すると古い方がスレッド1つの抜け殻のまま居座っていた。
                # メニューが閉じてイベントループが戻ってから終わらせる。
                QTimer.singleShot(0, QApplication.instance().quit)
            else:
                self._notify("再起動", "起動し直せませんでした")
        except Exception:
            self._log_taskbar_failure("再起動")

    def start_presenter(self):
        """HTMLプレゼン資料の発表者ツールを開く(設定 tools.presenter にパスがあるとき)。

        あちらは単一ファイルの file:// で動くビューアなので、ブラウザに投げるだけでよい。
        tray-tools 側に資料を渡す仕組みは持たせていない(資料は向こうへドラッグ＆
        ドロップする)。

        開くのは Edge(設定 tools.browser で変えられる)。既定のブラウザに任せないのは、
        Firefox だと真っ白になるため。あれは about:blank へ document.write して親と
        同一オリジンにする作りで、Firefox の file:// の扱いと噛み合わない。

        既定では同梱のものを開く。設定 tools.presenter にパスを書けば差し替えられる。"""
        path = (self.app_settings.get("tools", {}) or {}).get("presenter") or ""
        if not path:
            # 同梱してあるものを使う。設定に書くのは別の場所のものを使いたいときだけ。
            path = str(BUNDLED_PRESENTER)
        if not os.path.exists(path):
            self._notify("発表者ツール", f"見つかりません\n{path}")
            return
        try:
            browser_open.open_html(path, self._browser_name())
        except OSError as e:
            # 名指ししたブラウザが無いと既定へ落ちるが、そちらも .html に関連付けが
            # 無ければ投げる。Qtのスロット内で投げ切ると常駐ごと落ちるので受ける。
            self._notify("発表者ツール", f"開けませんでした\n{e}")

    def _browser_name(self) -> str:
        """HTMLを開くのに使うブラウザ。設定 tools.browser、無ければ Edge。"""
        return (self.app_settings.get("tools", {}) or {}).get(
            "browser"
        ) or browser_open.DEFAULT_BROWSER

    def start_web_presenter(self, url: str = None):
        """任意のウェブサイトを取り込んで、発表者ツールとして開く(web_presenter.py)。

        URL を尋ねる → 非表示の QtWebEngine で開く → レンダリング後の DOM を取り出す →
        <base> を挿して presenter.html と一緒に %TEMP% へ書き出す → 既定のブラウザへ、
        という流れ。読み込みは非同期なので、この関数は待たずにすぐ戻る(結果は通知)。

        中身の始末はすべてあちらが持つ。ここは入口と通知だけ(プレゼン支援と同じ形)。
        url を渡せば尋ねずに取り込む。"""
        web_presenter.open_site(
            self.app_settings,
            self.settings_path,
            BUNDLED_PRESENTER,
            self._notify,
            url=url,
        )

    # ---------------------------------------------------------------
    # プレゼン支援（画面に重ねる）
    #
    # 実物は presenter_overlay.py。ここは入口(メニュー・ホットキー・ランチャ)と
    # 通知だけを持つ。開閉と排他はあちらの OverlayController が面倒を見る。
    # ---------------------------------------------------------------
    def toggle_presenter_overlay(self, kind: str) -> bool:
        """レーザー/スポットライト/黒画面/白画面を切り替える。戻り値は切り替え後の状態。

        通知を出すのは「出した」ときだけ。消したときにトーストを出すと、発表を隠すために
        黒画面を畳んだ瞬間に画面の隅で通知が光ることになる。

        画面ミラー中は、4つとも行き先をミラー先へ振り替える。同じキーで同じことが
        起きるようにしたいので、ホットキーを増やさずここで振り分ける。

        レーザーとスポットライトを振り替えるのは、手元の画面に重ねるとそれがそのまま
        撮られて向こうにも映るため——光点は二重に見えるし、スポットライトに至っては
        減光ごと焼き込まれて戻せない。

        黒画面/白画面も振り替える。以前はここだけ振り替えず「撮る範囲ごと覆われれば
        ミラーにも黒が映る」で済ませていたが、それでは手元まで真っ黒になり、次に何を
        見せるか準備できない。覆うのは向こうだけにして、手元には枠とツールバーで
        「いま共有側は黒画面」と出す(静止のときと同じ流儀)。"""
        if self.screen_mirror.is_active() and kind in ("laser", "spotlight", "black", "white"):
            if kind in ("black", "white"):
                active = self.screen_mirror.toggle_blank(kind)
            else:
                active = self.screen_mirror.toggle_light(kind)
            label = presenter_overlay.KIND_LABELS.get(kind, kind)
            if active:
                self._notify("画面ミラー", f"{label}: ON（共有側だけ）")
            return active

        active = self.presenter_overlays.toggle(kind)
        label = presenter_overlay.KIND_LABELS.get(kind, kind)
        if active:
            self._notify("プレゼン支援", f"{label}: ON")
        return active

    def _set_presenter_overlay(self, kind: str, checked: bool):
        """メニューのチェック項目から。QAction.triggered が渡す checked をそのまま
        出す/畳むの指示として扱う(タスクバーウィジェットの項目と同じ作法)。"""
        if checked == self._presenter_overlay_active(kind):
            return
        self.toggle_presenter_overlay(kind)

    def _presenter_overlay_active(self, kind: str) -> bool:
        """その機能が今出ているか。ミラー中は4つともミラー先に居る。"""
        if self.screen_mirror.is_active():
            if kind in ("black", "white"):
                return self.screen_mirror.is_blank_on(kind)
            if kind in ("laser", "spotlight"):
                return self.screen_mirror.is_light_on(kind)
        return self.presenter_overlays.is_active(kind)

    def _refresh_presenter_menu(self):
        for kind, action in self._presenter_actions.items():
            action.setChecked(self._presenter_overlay_active(kind))

    # ---------------------------------------------------------------
    # 画面ミラー（別モニタへ）
    #
    # 実物は screen_mirror.py。ここは入口(メニュー・ホットキー・ランチャ)だけを持つ。
    # 範囲選択からミラー窓・手元の枠までは MirrorController が面倒を見る。
    # ---------------------------------------------------------------
    def start_screen_mirror(self) -> bool:
        """画面ミラーを開始する。ミラー中に呼ぶと、映したまま範囲だけを選び直す。

        以前は押すたびに開始/終了のトグルだった。やめたのは、発表の途中で映す場所を
        変えたいときに、いったんミラーを畳むことになるため——画面共有に出しているモニタが
        一瞬黒くなり、見ている側からは事故に見える。終わらせるのは stop_screen_mirror。

        通知は screen_mirror 側が出す(どのモニタへ何fpsで出したかを知っているのは
        あちらなので、ここで持ち直しても同じことを2回書くだけになる)。"""
        return self.screen_mirror.activate()

    # ランチャ(taskbar_launcher.ITEMS)から名前で呼ばれていた旧名。設定を書き換えずに
    # 済むよう残してある(呼び先が消えると「機能が無い」という通知だけが出る)。
    toggle_screen_mirror = start_screen_mirror

    def stop_screen_mirror(self) -> None:
        """画面ミラーを終了する。ホットキー(既定 Ctrl+Alt+Q)・トレイメニュー・
        手元のツールバーの ✕ から。開始のキーが終了を兼ねなくなったので、ここが
        「必ず終われる」担保になる。"""
        self.screen_mirror.stop()

    def toggle_screen_mirror_freeze(self) -> bool:
        """ミラーを静止させる/戻す。戻り値は切り替え後の状態。

        止めている間は撮らないので、向こうには最後の1枚が出たままになる。静止したことは
        手元(枠の色・帯・ツールバー)にだけ出す。"""
        return self.screen_mirror.toggle_freeze()

    def _set_screen_mirror_freeze(self, checked: bool):
        """メニューのチェック項目から。QAction.triggered が渡す checked をそのまま
        指示として扱う(プレゼン支援の項目と同じ作法)。"""
        if checked == self.screen_mirror.is_frozen():
            return
        self.screen_mirror.set_freeze(checked)

    def _toggle_screen_mirror_minimized(self):
        """一時停止して最小化 ⇔ 再開。実体はコントローラが持つ。"""
        if self.screen_mirror.is_minimized():
            self.screen_mirror.restore()
        else:
            self.screen_mirror.minimize()

    def _save_mirror_scene(self):
        """今のミラー範囲+ミラー先に名前を付けて保存する。同名は上書きを確認する。"""
        if not self.screen_mirror.is_active():
            return
        name, ok = QInputDialog.getText(
            self.menu, "ミラーの設定を保存",
            "名前（例: 会議用、配信用）:", QLineEdit.Normal, "")
        name = (name or "").strip()
        if not ok or not name:
            return
        if any(s["name"] == name for s in self.screen_mirror.saved_scenes()):
            answer = QMessageBox.question(
                self.menu, "ミラーの設定を保存",
                f"「{name}」は既にあります。今の設定で上書きしますか？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.screen_mirror.save_scene(name)

    def _delete_mirror_scene(self, name: str):
        answer = QMessageBox.question(
            self.menu, "ミラーの設定を削除",
            f"「{name}」を削除しますか？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self.screen_mirror.delete_scene(name)

    def _refresh_mirror_menu(self):
        """開く直前に、状態のチェックとミラー先の一覧を作り直す。

        モニタは抜き差しされるし、ミラーはホットキーからも切り替わる。一覧を作りっぱなしに
        すると、繋ぎ直したモニタが選べないまま残る。"""
        try:
            active = self.screen_mirror.is_active()
            hotkey_config = self.app_settings.get("hotkeys", {})
            # 見出しで「今押すと何が起きるか」を示す。ミラー中は選び直しになる。
            self._mirror_action.setText(
                self._with_hotkey(
                    "🔁 範囲を選び直す（映したまま）" if active else "▶ 範囲を選んで開始",
                    hotkey_config.get("screen_mirror"),
                )
            )
            self._mirror_freeze_action.setEnabled(active)
            self._mirror_freeze_action.setChecked(self.screen_mirror.is_frozen())
            minimized = self.screen_mirror.is_minimized()
            self._mirror_minimize_action.setEnabled(active)
            self._mirror_minimize_action.setText(
                "▶ 最小化を解く（再開）" if minimized else "⏬ 一時停止して最小化"
            )
            self._mirror_stop_action.setEnabled(active or self.screen_mirror.is_selecting())

            self._mirror_screen_menu.clear()
            self._mirror_screen_actions = []
            current = str(
                (self.app_settings.get("screen_mirror", {}) or {}).get("target_screen_name") or ""
            )
            auto = self._mirror_screen_menu.addAction("自動（範囲の無いモニタ）")
            auto.setCheckable(True)
            auto.setChecked(not current)
            auto.triggered.connect(lambda _checked: self.screen_mirror.set_target_screen(""))
            self._mirror_screen_actions.append(auto)
            self._mirror_screen_menu.addSeparator()
            for screen in screen_mirror.available_screens():
                name = screen.name()
                action = self._mirror_screen_menu.addAction(screen_mirror.screen_label(screen))
                action.setCheckable(True)
                action.setChecked(name == current)
                # addAction(テキスト, 関数) は引数なしで呼ぶので、ここも triggered で繋ぐ。
                action.triggered.connect(
                    lambda _checked, n=name: self.screen_mirror.set_target_screen(n)
                )
                self._mirror_screen_actions.append(action)

            # 保存した設定の一覧も作り直す(保存・削除のたびに変わるため)。
            self._mirror_save_scene_action.setEnabled(active)
            scenes = self.screen_mirror.saved_scenes()
            self._mirror_scene_menu.clear()
            self._mirror_scene_actions = []
            self._mirror_scene_delete_menu.clear()
            self._mirror_scene_delete_actions = []
            if not scenes:
                empty = self._mirror_scene_menu.addAction("（まだ保存がありません）")
                empty.setEnabled(False)
                self._mirror_scene_actions.append(empty)
                self._mirror_scene_delete_menu.setEnabled(False)
            else:
                self._mirror_scene_delete_menu.setEnabled(True)
                for scene in scenes:
                    label = (f"{scene['name']}"
                             f"（{scene['width']}x{scene['height']}）")
                    action = self._mirror_scene_menu.addAction(label)
                    action.triggered.connect(
                        lambda _checked, n=scene["name"]:
                        self.screen_mirror.apply_scene(n))
                    self._mirror_scene_actions.append(action)
                    del_action = self._mirror_scene_delete_menu.addAction(scene["name"])
                    del_action.triggered.connect(
                        lambda _checked, n=scene["name"]: self._delete_mirror_scene(n))
                    self._mirror_scene_delete_actions.append(del_action)
        except Exception:
            self._log_failure("画面ミラーのメニューの組み立て")

    # ---------------------------------------------------------------
    # 診断(diagnostics.py / diagnostics_window.py)
    # ---------------------------------------------------------------
    def open_diagnostics(self):
        """診断の窓を別プロセスで開く(diagnostics.spawn_window)。

        窓もまとめ作りも常駐の外で行う。常駐の中で QR を作ったら、そのときの GC が
        comtypes のオブジェクトの解放に入って常駐ごと落ちたため(spawn_window の docstring)。
        ここで渡すのは「いま動いているか」と常駐の起動時刻だけ。

        import はここで行う。診断は「ファイルが抜けている・古い」PCで使うものなので、
        診断のファイル自体が抜けていても常駐の起動を巻き添えにしない。"""
        try:
            import diagnostics
            diagnostics.spawn_window(self._diagnostics_runtime(), self.settings_path)
        except Exception as e:  # noqa: BLE001  メニューのスロット。投げ切ると常駐ごと落ちる
            self._log_failure("診断の窓を開くの")
            self._notify("診断", f"開けませんでした\n{e}")

    def _diagnostics_runtime(self) -> dict:
        """診断に載せる「いま動いているか」。設定ではなく状態のもの。読めないものは None。"""
        def read(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return None

        return {
            "jiggler": read(lambda: bool(self._jiggle_active)),
            "keep_awake": read(lambda: bool(self._awake_active)),
            "copilot_bar": read(lambda: bool(self._copilot_watchdog.is_enabled())),
            "agent_loop": read(lambda: bool(self._agent_loop_active)),
            "mirror": read(lambda: bool(self.screen_mirror.is_active())),
            "sticky": read(lambda: len(capture_process.list_sticky_pipes())),
        }

    def _open_settings_file(self):
        """設定ダイアログUIは持たないため、settings.jsonを既定アプリ(メモ帳等)で開く。

        os.startfile は .json に関連付けが無いと例外を投げる。Qtのスロット内で投げ切ると
        常駐アプリごと落ちるので、ここで受けて通知に回す(snippets._open_path と同じ理由)。"""
        if not (self.settings_path and os.path.exists(self.settings_path)):
            return
        try:
            os.startfile(self.settings_path)
        except OSError as e:
            self._notify("設定", f"開けませんでした\n{e}")


def _widget_screen_name(widget):
    """そのウィジェットが乗っている画面の QScreen.name()。分からなければ None。

    screen_name(型番＋シリアル)ではなく素の名前を返す。画面ミラーが「この画面を
    覆っている」と知らせてくるのが QScreen.name() なので、比べる側を揃える。"""
    try:
        screen = taskbar_widget._screen_for(widget.geometry(), taskbar_widget._screens())
    except Exception:
        return None
    return screen.name() if screen is not None else None
