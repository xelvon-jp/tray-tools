# feature_screen.py
# 画面まわり全般のFeature(旧 feature_capture.py)。トレイアイコンを1つ所有し、
# 範囲キャプチャ付箋「Rapture」に加えて、色の吸い取り・画面の測定・全画面への書き込み・
# 任意ウィンドウの最前面固定・スリープ抑止をこのアイコンのメニューから提供する。
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
import screen_ruler
from capture_grab import grab_region, save_image, virtual_geometry
from capture_overlay import CountdownOverlay, SelectionOverlay
from capture_window import CaptureWindow
from keep_awake import set_keep_awake
from qt_image import pil_to_qicon
from window_tools import TopmostTracker

ICON_PATH = Path(__file__).resolve().parent / "icons" / "rapture.png"

# スリープ抑止中の目印。通知領域のアイコンは実質16px相当で、隅の小さなバッジは潰れて
# 見えない。アイコン全周に太いリングを描き、縮小しても輪郭の変化で判別できるようにする。
AWAKE_RING_COLOR = (245, 158, 11, 255)
AWAKE_RING_WIDTH = 4

NOTIFY_MS = 3000


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
        # 範囲選択が終わってから待つ秒数。選択完了まで持ち越す。
        self._pending_delay = 0
        # 開いている付箋ウインドウの参照をリストで保持する(GC回収防止)。
        # ウインドウが閉じられたらリストから取り除く(残し続けるとメモリリークになる)。
        self.capture_windows = []

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

        self.menu = QMenu()
        self.menu.addAction("キャプチャ（今すぐ）", lambda: self.start_capture(0))
        self.menu.addAction("5秒後にキャプチャ", lambda: self.start_capture(5))
        self.menu.addAction("10秒後にキャプチャ", lambda: self.start_capture(10))
        self.menu.addSeparator()
        self.menu.addAction(
            self._with_hotkey("色を吸い取る", hotkey_config.get("color_picker")),
            self.start_color_picker,
        )
        self.menu.addAction("画面を測る", self.start_ruler)
        self.menu.addAction("画面全体に書き込む", self.annotate_screen)
        self.menu.addSeparator()
        self.menu.addAction(
            self._with_hotkey("このウィンドウを最前面に固定", hotkey_config.get("always_on_top")),
            self.toggle_always_on_top,
        )

        self._awake_menu = self.menu.addMenu("スリープ抑止")
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
        self.menu.addAction("設定", self._open_settings_file)
        self.menu.addSeparator()
        self.menu.addAction("終了", QApplication.instance().quit)

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
            "color_picker": self.start_color_picker,
            "always_on_top": self.toggle_always_on_top,
        }

    @staticmethod
    def _with_hotkey(label: str, combo) -> str:
        return f"{label} ({combo})" if combo else label

    def _notify(self, title: str, message: str) -> None:
        self.tray_icon.showMessage(title, message, QSystemTrayIcon.Information, NOTIFY_MS)

    def _on_activated(self, reason):
        # 左クリック(Trigger)で即キャプチャ
        if reason == QSystemTrayIcon.Trigger:
            self.start_capture(0)

    # ---------------------------------------------------------------
    # 範囲キャプチャ
    # ---------------------------------------------------------------
    def start_capture(self, delay_seconds: int):
        # ホットキー・トレイの左クリック・メニューと入口が3つあるため二重起動しやすい。
        # ガードが無いと self.overlay が上書きされ、前のオーバーレイが参照を失って
        # 全画面に貼り付いたまま残る(マウス操作を奪う)。色/定規と同じ作法で弾く。
        if self.overlay is not None or self.countdown is not None:
            return
        # 遅延キャプチャは「範囲を先に選んでから待つ」。待ってから範囲選択させると、
        # 選択操作をしている時点の画面が撮られるだけで待った意味がない。
        # メニューやツールチップなど、操作すると消えるものを撮るのが本来の目的なので、
        # 範囲を確定させたあとの待ち時間にそれらを開いてもらう。
        self._pending_delay = max(int(delay_seconds or 0), 0)
        self._show_overlay()

    def _show_overlay(self):
        self.overlay = SelectionOverlay()
        self.overlay.selection_made.connect(self._on_selection_made)
        self.overlay.canceled.connect(self._on_canceled)
        self.overlay.show()

    def _on_canceled(self):
        # SelectionOverlayはEscでcanceledをemitするだけで自分では閉じない。参照を
        # 捨てるだけだと全画面を覆う半透明ウィジェットが消えるかどうかGC任せになり、
        # マウス操作を奪ったまま残り得る。close()で明示的に閉じ、シグナル発火中の
        # 即時破棄を避けるためdeleteLater()で後始末する。
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None
        self._pending_delay = 0

    def _on_selection_made(self, rect_global: QRect):
        self.overlay.close()
        self.overlay.deleteLater()
        self.overlay = None

        delay = self._pending_delay
        self._pending_delay = 0
        if delay > 0:
            self.countdown = CountdownOverlay(delay)
            self.countdown.finished.connect(lambda: self._capture_after_countdown(rect_global))
            self.countdown.show()
            return

        self._grab_and_show(rect_global)

    def _capture_after_countdown(self, rect_global: QRect):
        if self.countdown is not None:
            self.countdown.close()
            self.countdown.deleteLater()
            self.countdown = None
        # カウントダウン表示が選択範囲に重なっていると写り込む。閉じた直後はOSがまだ
        # 再描画し切っていないことがあるので、少し待ってから撮る(再キャプと同じ理由)。
        QTimer.singleShot(150, lambda: self._grab_and_show(rect_global))

    def _grab_and_show(self, rect_global: QRect):
        capture_settings = self.app_settings.get("capture", {})
        image = grab_region(rect_global)
        save_image(image, capture_settings)  # キャプチャ直後の素の画像を自動保存

        self._open_capture_window(image, rect_global.topLeft())

    def _open_capture_window(self, image, global_pos, close_on_escape=False):
        window = CaptureWindow(
            image,
            global_pos,
            self.app_settings.get("capture", {}),
            settings_path=self.settings_path,
            close_on_escape=close_on_escape,
        )
        window.destroyed.connect(lambda: self._on_window_closed(window))
        self.capture_windows.append(window)
        window.show()

    def _on_window_closed(self, window):
        if window in self.capture_windows:
            self.capture_windows.remove(window)

    # ---------------------------------------------------------------
    # 色を吸い取る
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
    # 画面を測る
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
    # 画面全体に書き込む
    # ---------------------------------------------------------------
    def annotate_screen(self):
        """画面全体を静止画として固定し、その上に書き込めるようにする(ZoomIt相当)。
        描画・ズーム・保存・コピーは付箋ウインドウが既に持っているのでそのまま使う。"""
        geometry = virtual_geometry()
        image = grab_region(geometry)
        self._open_capture_window(image, geometry.topLeft(), close_on_escape=True)

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
            self._awake_menu.setTitle("スリープ抑止")
        elif self._awake_minutes:
            self._awake_menu.setTitle(f"スリープ抑止（残り{self._remaining_minutes()}分）")
        else:
            self._awake_menu.setTitle("スリープ抑止（無期限）")

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
        """設定ダイアログUIは持たないため、settings.jsonを既定アプリ(メモ帳等)で開く。"""
        if self.settings_path and os.path.exists(self.settings_path):
            os.startfile(self.settings_path)
