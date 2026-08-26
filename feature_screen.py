# feature_screen.py
# 画面まわり全般のFeature(旧 feature_capture.py)。トレイアイコンを1つ所有し、
# 範囲キャプチャ付箋「Rapture」に加えて、カラーピッカー・画面定規・定型文・
# フォルダブックマーク・任意ウィンドウの最前面固定・スリープ抑止・マウスジグラーを
# このアイコンのメニューから提供する。
#
# アイコンを増やさないのは意図的。状態を持つ機能(スリープ抑止)だけがアイコンの見た目を
# 占有し、単発の動作はメニュー項目で足りるという方針。マウスジグラーも状態を持つが、
# 16px相当のアイコンに2つ目の目印は入らないので、こちらはツールチップとメニューの
# 見出し(残り時間)だけで示す。
import math
import os
import sys
import time
import traceback
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import capture_process
import color_picker
import explorer_nav
import launcher
import mouse_jiggler
import screen_ruler
import snippets
import taskbar_widget
from capture_grab import new_session_stem, save_image
from capture_overlay import CountdownOverlay, FrozenSelectionOverlay
from keep_awake import set_keep_awake
from qt_image import pil_to_qicon
from toast import show_toast
from window_tools import TopmostTracker

ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"

# スリープ抑止中の目印。通知領域のアイコンは実質16px相当で、隅の小さなバッジは潰れて
# 見えない。アイコン全周に太いリングを描き、縮小しても輪郭の変化で判別できるようにする。
AWAKE_RING_COLOR = (245, 158, 11, 255)
AWAKE_RING_WIDTH = 4


def _format_minutes(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}時間"
    return f"{minutes}分"


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
    base = Image.open(ICON_PATH).convert("RGBA")
    img = base.copy()
    draw = ImageDraw.Draw(img)
    inset = AWAKE_RING_WIDTH / 2
    draw.ellipse(
        (inset, inset, img.width - 1 - inset, img.height - 1 - inset),
        outline=AWAKE_RING_COLOR,
        width=AWAKE_RING_WIDTH,
    )
    return img


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
        self.launcher_picker = None
        # 付箋(Rapture)のウインドウはここでは持たない。別プロセス(capture_process.py)に
        # 出したので、参照どころか同じアドレス空間にすら居ない。以前は開いている付箋を
        # self.capture_windows に、ホットキーで撮る対象を self.active_capture_window に
        # 持っていたが、両方とも名前付きパイプの一覧から引き当てる形へ移した
        # (capture_process.list_sticky_pipes / send_to_latest)。
        # そうしたのは、本体が落ちたときと再起動したときの道連れを防ぐため。開いている
        # 付箋を掴んでいる限り、本体の寿命が付箋の寿命になってしまう。

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
        self._awake_timer = QTimer()
        self._awake_timer.setSingleShot(True)
        self._awake_timer.timeout.connect(self._on_awake_expired)
        self._awake_icon = None

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
        self.menu = QMenu()
        self.menu.addAction("📷 キャプチャ（今すぐ）", lambda: self.start_capture(0))
        self.menu.addAction("⏱ 5秒後にキャプチャ", lambda: self.start_capture(5))
        self.menu.addAction("⏱ 10秒後にキャプチャ", lambda: self.start_capture(10))
        self.menu.addSeparator()
        self.menu.addAction(
            self._with_hotkey("💧 カラーピッカー", hotkey_config.get("color_picker")),
            self.start_color_picker,
        )
        self.menu.addAction("📏 画面定規", self.start_ruler)
        self.menu.addSeparator()
        self.menu.addAction(
            self._with_hotkey("📋 定型文", hotkey_config.get("snippet_picker")),
            self.start_snippet_picker,
        )
        # QAction.triggered は checked(bool) を渡してくる。引数を取れる関数を直接繋ぐと
        # current_path に False が入るので、ここは引数なしのラムダで包む。
        self.menu.addAction("📂 定型文フォルダを開く", lambda: snippets.open_folder())
        self.menu.addAction(
            self._with_hotkey("📁 フォルダブックマーク", hotkey_config.get("launcher")),
            lambda: self.start_launcher(),
        )
        self.menu.addSeparator()
        self.menu.addAction(
            self._with_hotkey("📌 このウィンドウを最前面に固定", hotkey_config.get("always_on_top")),
            self.toggle_always_on_top,
        )

        self._awake_menu = self.menu.addMenu("☕ スリープ抑止")
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
        self._awake_menu.addAction("解除", self._disable_keep_awake)

        # スリープ抑止のすぐ下に置く。狙いが近い(席を外しても切れないようにする)ので、
        # 探すときに同じ場所を見れば済むようにしている。
        self._jiggle_menu = self.menu.addMenu("🖱 マウスジグラー")
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
        # 通知領域はプライマリのタスクバーにしか出ないので、各ディスプレイのタスクバーへ
        # 自前で置く出張所の表示切替。詳細は taskbar_widget.py 冒頭を参照。
        self._taskbar_action = self.menu.addAction("🖥 タスクバーウィジェット")
        self._taskbar_action.setCheckable(True)
        # addAction(テキスト, 関数) は関数を引数なしで呼ぶので、チェック状態を受け取る
        # 項目をその形で繋いではいけない(クリックのたびにTypeErrorで落ちる)。
        self._taskbar_action.triggered.connect(self._toggle_taskbar_widget)
        self._taskbar_bg_action = self.menu.addAction(
            "🎨 背景色を取り直す", self._recapture_taskbar_background
        )

        self.menu.addSeparator()
        self.menu.addAction("⚙ 設定", self._open_settings_file)
        self.menu.addSeparator()
        self.menu.addAction("🔄 再起動", self._restart)
        self.menu.addAction("✖ 終了", QApplication.instance().quit)

        # 残り時間は開くたびに変わるので、表示直前に作り直す
        self.menu.aboutToShow.connect(self._refresh_awake_menu)
        self.menu.aboutToShow.connect(self._refresh_jiggle_menu)
        self.menu.aboutToShow.connect(self._refresh_taskbar_menu)

        self.tray_icon.setContextMenu(self.menu)
        self.tray_icon.activated.connect(self._on_activated)
        self._refresh_state()
        self.tray_icon.setVisible(True)

        # 抑止したまま/固定したままアプリを終わらせると、解除する手段が無くなる
        QApplication.instance().aboutToQuit.connect(self._on_quit)

    def hotkeys(self) -> dict:
        return {
            "capture_now": lambda: self.start_capture(0),
            "capture_sequence": self.capture_sequence,
            "color_picker": self.start_color_picker,
            "always_on_top": self.toggle_always_on_top,
            "snippet_picker": self.start_snippet_picker,
            "launcher": lambda: self.start_launcher(),
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

    def _on_canceled(self):
        # SelectionOverlayはEscでcanceledをemitするだけで自分では閉じない。参照を
        # 捨てるだけだと全画面を覆うウィジェットが消えるかどうかGC任せになり、
        # マウス操作を奪ったまま残り得る。close()で明示的に閉じ、シグナル発火中の
        # 即時破棄を避けるためdeleteLater()で後始末する。
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None

    def _on_selection_made(self, rect_global: QRect):
        # 撮り直さず凍結画像から切り出す。参照を捨てる前に取り出しておく。
        image = self.overlay.crop(rect_global)
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None

        # 付箋1枚が1つの連番セッション。撮影直後の自動保存がそのセッションの1枚目になるよう、
        # 先にファイル名の頭を決めてから付箋へ引き継ぐ(rapture_20260823_140919-001.png)。
        # 保存に失敗したときは連番0のまま(バッジも出さない)で付箋だけ開く。
        session_stem = new_session_stem()
        saved = save_image(  # 素の画像を自動保存
            image, self.app_settings.get("capture", {}), stem=session_stem, index=1
        )
        self._open_capture_window(
            image,
            rect_global.topLeft(),
            session_stem=session_stem,
            session_index=1 if saved else 0,
        )

    def _open_capture_window(self, image, global_pos, close_on_escape=False,
                             session_stem=None, session_index=0):
        """付箋を1枚開く。実体はこのプロセスではなく別プロセス(capture_process.py)。

        本体と同じプロセスに置くと、本体が落ちたときはもちろん、機能追加のたびの
        再起動でも開いている付箋が全部消える。手順書を作るために連番キャプチャで
        何枚も並べている最中にそれをやられると撮り直しになるため、付箋の寿命を
        本体から切り離してある。付箋側は capture_grab と toast にしか依存しておらず、
        元から本体の状態を何も見ていないので、そのまま持ち出せた。

        起動には0.37秒ほどかかる(PythonとQtの立ち上がりぶん。実測値と代案は
        capture_process.spawn のコメント参照)。同じプロセスで開いていた頃の
        「押した瞬間に出る」よりは遅いが、ここで止まるのは10ms程度でこちらは
        固まらないため、範囲選択の操作感は変わらない。"""
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
                capture_hotkey=self.app_settings.get("hotkeys", {}).get("capture_sequence"),
            )
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
    # フォルダブックマーク
    # ---------------------------------------------------------------
    def start_launcher(self, current_path: str = None):
        """フォルダブックマークを開く。current_path はあふｗ側の現在のパスで、
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
        picker = launcher.create_picker(
            self.app_settings, self.settings_path, current_path=current_path, hwnd=hwnd
        )
        if picker is None:
            return  # ブックマークが1件も無い(通知は launcher 側が出している)
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
        # 1つでも出せたなら有効として扱う。画面が3枚あって1枚だけ失敗した場合に、
        # 全部消してしまうほうが困る。
        return any(started)

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

    # ---------------------------------------------------------------
    # スリープ抑止
    # ---------------------------------------------------------------
    def _enable_keep_awake(self, minutes):
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
        self._notify("スリープ抑止", f"有効: {label}")

    def _disable_keep_awake(self, notify: bool = True):
        was_active = self._awake_active
        self._awake_timer.stop()
        set_keep_awake(False)
        self._awake_active = False
        self._awake_minutes = None
        self._awake_deadline = None
        self._refresh_state()
        if notify and was_active:
            self._notify("スリープ抑止", "解除しました")

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
        """スリープ抑止とマウスジグラーの状態をトレイアイコンとメニューに反映する。"""
        if self._awake_active:
            if self._awake_icon is None and ICON_PATH.exists():
                self._awake_icon = pil_to_qicon(_make_awake_icon_image())
            self.tray_icon.setIcon(self._awake_icon or self._normal_icon)
        else:
            self.tray_icon.setIcon(self._normal_icon)
        # アイコンの見た目(リング)はスリープ抑止が占有しているので、ジグラーの状態は
        # ツールチップで示す。実質16pxのアイコンに2つ目の目印を入れても潰れて読めない。
        states = []
        if self._awake_active:
            states.append("スリープ抑止中")
        if self._jiggle_active:
            states.append("マウスジグラー動作中")
        self.tray_icon.setToolTip(f"Rapture（{'・'.join(states)}）" if states else "Rapture")
        self._refresh_awake_menu()
        self._refresh_jiggle_menu()

    def _on_quit(self):
        # 付箋(Rapture)はここで閉じない。別プロセスで動いており、本体を終了・再起動
        # しても生き残るのが狙いだから。見つけて閉じて回る処理を足さないこと。
        self._awake_timer.stop()
        set_keep_awake(False)
        # 止め忘れると、終了処理の途中でタイマーが回って入力を送りに行く。
        self._jiggle_timer.stop()
        self._jiggle_expire_timer.stop()
        self.topmost.release_all()
        # 枠なし・最前面の窓を残したままイベントループを畳むと、画面に貼り付いたまま
        # 消えないことがある。タイマーもここで止まる(hideEvent 参照)。
        self._taskbar_rebuild_timer.stop()
        self._close_taskbar_widgets()

    def attach_restart(self, restart) -> None:
        """自分を起動し直す手段を受け取る。組み立ては main.py が行う。"""
        self._restart_app = restart

    def _restart(self):
        """メニューからの再起動。

        設定を書き換えたあとや、ディスプレイの構成を変えたあとに使う。手で終了して
        起動し直すには、この常駐アプリをどう起動しているか(venvのpythonw)を覚えている
        必要があり、それを思い出さずに済ませるための項目。"""
        try:
            if self._restart_app is None:
                self._notify("再起動", "この起動のしかたでは再起動できません")
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
