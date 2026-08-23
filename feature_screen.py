# feature_screen.py
# 画面まわり全般のFeature(旧 feature_capture.py)。トレイアイコンを1つ所有し、
# 範囲キャプチャ付箋「Rapture」に加えて、カラーピッカー・画面定規・定型文・
# フォルダブックマーク・任意ウィンドウの最前面固定・スリープ抑止を
# このアイコンのメニューから提供する。
#
# アイコンを増やさないのは意図的。状態を持つ機能(スリープ抑止)だけがアイコンの見た目を
# 占有し、単発の動作はメニュー項目で足りるという方針。
import math
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw
from PySide6.QtCore import QRect, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import color_picker
import explorer_nav
import launcher
import screen_ruler
import snippets
from capture_grab import new_session_stem, save_image
from capture_overlay import CountdownOverlay, FrozenSelectionOverlay
from capture_window import CaptureWindow
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
        # 開いている付箋ウインドウの参照をリストで保持する(GC回収防止)。
        # ウインドウが閉じられたらリストから取り除く(残し続けるとメモリリークになる)。
        self.capture_windows = []
        # グローバルホットキー(capture_sequence)で撮る対象の付箋。同時に何枚でも開けるので、
        # 最後に作ったものを対象にする。閉じられたら必ずNoneに戻すこと(WA_DeleteOnClose で
        # 実体が消えるため、掴んだまま触るとRuntimeErrorになる)。
        self.active_capture_window = None

        self.topmost = TopmostTracker()

        self._awake_active = False
        self._awake_minutes = None  # None = 無期限
        self._awake_deadline = None
        self._awake_timer = QTimer()
        self._awake_timer.setSingleShot(True)
        self._awake_timer.timeout.connect(self._on_awake_expired)
        self._awake_icon = None

        hotkey_config = app_settings.get("hotkeys", {})
        screen_settings = app_settings.get("screen", {})
        self._awake_choices = screen_settings.get("keep_awake_minutes", [30, 120])

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

        self.menu.addSeparator()
        self.menu.addAction("⚙ 設定", self._open_settings_file)
        self.menu.addSeparator()
        self.menu.addAction("✖ 終了", QApplication.instance().quit)

        # 残り時間は開くたびに変わるので、表示直前に作り直す
        self.menu.aboutToShow.connect(self._refresh_awake_menu)

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
        window = CaptureWindow(
            image,
            global_pos,
            self.app_settings.get("capture", {}),
            settings_path=self.settings_path,
            close_on_escape=close_on_escape,
            session_stem=session_stem,
            session_index=session_index,
            # 連番キャプチャのキーをタイトルバーに出させる。設定で変えられる値なので
            # 付箋側にハードコードさせず、ここで実値を渡す。
            capture_hotkey=self.app_settings.get("hotkeys", {}).get("capture_sequence"),
        )
        window.destroyed.connect(lambda: self._on_window_closed(window))
        self.capture_windows.append(window)
        # 直前に作った付箋をホットキーの対象にする。撮りたいのは今出したものなので、
        # 複数枚並んでいても最後の1枚に向ければ迷わない。
        self.active_capture_window = window
        window.show()

    def _on_window_closed(self, window):
        if window in self.capture_windows:
            self.capture_windows.remove(window)
        # 閉じられた付箋を対象に残すと、次のホットキーで削除済みのC++オブジェクトを
        # 触ってしまう。ここで必ず手放す(is で比べる。実体が消えた後なので中身は見ない)。
        if self.active_capture_window is window:
            self.active_capture_window = None

    def capture_sequence(self):
        """ホットキー(既定 Ctrl+Alt+S): 対象の付箋の連番キャプチャを1枚進める。

        付箋の Space と同じ処理だが、こちらは付箋がアクティブでなくても効く。
        ブラウザ等を操作しながら「操作する → 撮る」を繰り返す使い方が本命で、そのとき
        前面にいるのは操作中のアプリなので、付箋にフォーカスを戻さず撮れる必要がある。
        付箋は常に最前面で位置を保っているため、非アクティブでも狙った範囲が撮れる。"""
        window = self.active_capture_window
        if window is None:
            # 黙って無反応だとホットキーが効いていないのか壊れたのか区別が付かない。
            self._notify("Rapture", "付箋がありません")
            return
        try:
            window.capture_and_save()
        except RuntimeError:
            # 閉じられた付箋を触ると「削除済みのC++オブジェクト」でRuntimeErrorになる。
            # destroyed で手放しているので通常は起きないが、ここで投げ切ると常駐ごと落ちる。
            self.active_capture_window = None
            self._notify("Rapture", "付箋がありません")
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

    def _remaining_minutes(self):
        if self._awake_deadline is None:
            return None
        remaining = self._awake_deadline - time.monotonic()
        # 端数は切り上げる(残り30秒を「0分」と出さない)。ちょうど60分なら60分と出す。
        return max(math.ceil(remaining / 60), 1) if remaining > 0 else 0

    def _refresh_awake_menu(self):
        for key, action in self._awake_actions.items():
            action.setChecked(self._awake_active and key == self._awake_minutes)

        if not self._awake_active:
            self._awake_menu.setTitle("☕ スリープ抑止")
        elif self._awake_minutes:
            self._awake_menu.setTitle(f"☕ スリープ抑止（残り{self._remaining_minutes()}分）")
        else:
            self._awake_menu.setTitle("☕ スリープ抑止（無期限）")

    def _refresh_state(self):
        """スリープ抑止の状態をトレイアイコンとメニューに反映する。"""
        if self._awake_active:
            if self._awake_icon is None and ICON_PATH.exists():
                self._awake_icon = pil_to_qicon(_make_awake_icon_image())
            self.tray_icon.setIcon(self._awake_icon or self._normal_icon)
            self.tray_icon.setToolTip("Rapture（スリープ抑止中）")
        else:
            self.tray_icon.setIcon(self._normal_icon)
            self.tray_icon.setToolTip("Rapture")
        self._refresh_awake_menu()

    def _on_quit(self):
        self._awake_timer.stop()
        set_keep_awake(False)
        self.topmost.release_all()

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
