# diagnostics_window.py
# 「🩺 診断」の窓。まとめ(diagnostics.collect)をテキストと QR コードで出す。
#
# 業務PCからはデータを持ち出せないので、スマホのカメラで QR を読み取ってチャットに
# 貼ってもらう。まとめを作る部分は Qt に依存しない diagnostics.py にあり、ここは窓だけ。
#
# 起動
#   常駐のトレイメニュー「🩺 診断…」から、別プロセスとして起こされる
#   (diagnostics.spawn_window。pythonw.exe + DETACHED_PROCESS。背景を透過と同じ形)。
#   手で開くなら:
#     C:\Users\<名前>\.venvs\tray-tools\Scripts\pythonw.exe diagnostics_window.py
#
# なぜ常駐の中に窓を出さないのか
#   最初はそうしていて、実機で開いた直後に常駐ごと落ちた。QR を作る最中の GC が、常駐の
#   抱える comtypes のオブジェクトの __del__ → Release に入って access violation になった
#   (crash.log とイベントログの両方に残った)。常駐に COM オブジェクトが居る限り、大量に
#   オブジェクトを作る処理は GC を呼び込んで同じ事故を起こしうる。このプロセスは COM を
#   使わないので、ワーカースレッドも GC も気にしなくてよい。
#
# - 閉じれば消える(プロセスごと終わる)
# - イベントログの読み取りは数秒かかることがあるので、まとめはワーカースレッドで作り、
#   結果はシグナルで返す(ワーカーから Qt を触ると壊れる)
# - 大きさは settings.json の diagnostics.window_size に覚える(clipboard_preview と同じ作法で、
#   ファイルを読み直して該当キーだけ差し替える)。位置は覚えず、毎回カーソルのある画面の中央
# - 配色は bg_remove.THEME と同じ値を使う。bg_remove は cv2 / numpy を読み込むので import
#   しない(値だけ写してあり、tests/test_diagnostics.py が食い違いとコントラスト比を確かめる)
import argparse
import json
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFontDatabase,
    QGuiApplication,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import diagnostics
import settings as settings_module

SETTINGS_SECTION = "diagnostics"
SETTINGS_KEY = "window_size"
DEFAULT_WIDTH = 760
DEFAULT_HEIGHT = 940
MIN_WIDTH = 520
MIN_HEIGHT = 560

ERROR_LOG_PATH = Path(__file__).resolve().parent / "error.log"

# bg_remove.THEME から、この窓で使うものだけを写したもの(値は同じであること)。
THEME = {
    "bg": "#1e1f22",
    "panel": "#26282c",
    "field": "#313338",
    "field_hover": "#3a3d43",
    "field_pressed": "#2b2d31",
    "border": "#4a4d55",
    "separator": "#3a3d43",
    "text": "#e6e6e6",
    "heading": "#ffffff",
    "muted": "#a0a4ab",
    "disabled": "#878b93",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_pressed": "#1e40af",
    "accent_text": "#ffffff",
    "warning": "#f5a524",
    "error": "#ff7a7a",
}

# この窓での文字色と地の組み合わせ(tests/test_diagnostics.py が比を確かめる)。
CONTRAST_REQUIREMENTS = [
    ("text", "bg", 4.5), ("text", "field", 4.5), ("text", "field_hover", 4.5),
    ("muted", "bg", 4.5), ("warning", "bg", 4.5), ("error", "bg", 4.5),
    ("heading", "bg", 4.5), ("disabled", "panel", 3.0),
    ("accent_text", "accent", 4.5), ("accent_text", "accent_hover", 4.5),
]

NOTICE = "中身を確認してから読み取ってください（技術情報のみ。個人名・パスは伏せています）"


def build_style() -> str:
    t = THEME
    return f"""
    QWidget#diagWindow {{ background-color: {t['bg']}; }}
    QWidget#diagWindow QLabel {{ color: {t['text']}; background: transparent; }}
    QLabel#diagStatus {{ color: {t['muted']}; }}
    QLabel#diagStatus[level="error"] {{ color: {t['error']}; }}
    QLabel#diagNotice {{ color: {t['warning']}; font-weight: bold; }}
    QLabel#diagPage {{ color: {t['heading']}; }}
    QPlainTextEdit#diagText {{ background-color: {t['field']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 4px;
        selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
    QWidget#diagWindow QPushButton {{ background-color: {t['field']}; color: {t['text']};
        border: 1px solid {t['border']}; border-radius: 5px; padding: 6px 12px; }}
    QWidget#diagWindow QPushButton:hover {{ background-color: {t['field_hover']}; }}
    QWidget#diagWindow QPushButton:pressed {{ background-color: {t['field_pressed']}; }}
    QWidget#diagWindow QPushButton:disabled {{ background-color: {t['panel']};
        color: {t['disabled']}; border-color: {t['separator']}; }}
    QPushButton#diagCopy {{ background-color: {t['accent']}; border-color: {t['accent']};
        color: {t['accent_text']}; }}
    QPushButton#diagCopy:hover {{ background-color: {t['accent_hover']}; }}
    QPushButton#diagCopy:pressed {{ background-color: {t['accent_pressed']}; }}
    QSplitter::handle {{ background-color: {t['separator']}; }}
    """


# ------------------------------------------------------------------
# 窓の大きさ(clipboard_preview と同じ作法)
# ------------------------------------------------------------------
def _load_size(app_settings):
    try:
        section = (app_settings or {}).get(SETTINGS_SECTION)
        size = section.get(SETTINGS_KEY) if isinstance(section, dict) else None
        if not (isinstance(size, (list, tuple)) and len(size) == 2):
            return None
        width, height = int(size[0]), int(size[1])
        if width < MIN_WIDTH or height < MIN_HEIGHT:
            return None
        return width, height
    except Exception:  # noqa: BLE001
        return None


def _save_size(app_settings, settings_path, width: int, height: int) -> None:
    """メモリ上の設定を丸ごと書き戻さず、ファイルを読み直して該当キーだけ差し替える。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = app_settings[SETTINGS_SECTION] = {}
        section[SETTINGS_KEY] = [width, height]
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
        section[SETTINGS_KEY] = [width, height]
        settings_module.save_settings(stored, settings_path)
    except Exception as e:  # noqa: BLE001
        print(f"[tray-tools] 診断の窓の大きさを保存できません: {e}", file=sys.stderr)


# ------------------------------------------------------------------
# ワーカー → メインスレッドの橋
# ------------------------------------------------------------------
class _DiagBridge(QObject):
    finished = Signal(int, object, object)  # 通し番号, まとめ(str|None), エラー(str|None)


def _work(bridge, generation: int, app_settings: dict, runtime: dict, process_start) -> None:
    """ワーカースレッドの本体。Qt には触らない(emit だけ)。"""
    text, error = None, None
    try:
        text = diagnostics.collect(app_settings, runtime, process_start=process_start)
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    try:
        bridge.finished.emit(generation, text, error)
    except RuntimeError:
        pass  # 窓が先に閉じて消えた


# ------------------------------------------------------------------
# QR を出す部品
# ------------------------------------------------------------------
class QrView(QWidget):
    """QR を描ける大きさいっぱいに真ん中へ描く。拡大は補間なし(セルの境目をぼかさない)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self.setMinimumSize(200, 200)

    def set_image(self, image) -> None:
        self._image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor("#ffffff"))
            if self._image is None or self._image.isNull():
                return
            # 整数倍に丸めると、窓によっては 2 倍(1セル2px)まで縮んでスマホで読みにくい。
            # 描ける大きさいっぱいに広げる。補間はしないので、セルの幅が1pxずつ揃わない
            # だけで境目はぼけない(読み取りには差し支えない)。4倍以上取れるときだけ
            # 整数倍に揃える。
            side = min(self.width(), self.height())
            cells = self._image.width()
            size = side if side < cells * 4 else (side // cells) * cells
            x = (self.width() - size) // 2
            y = (self.height() - size) // 2
            painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
            painter.drawImage(QRect(x, y, size, size), self._image)
        finally:
            painter.end()


class DiagnosticsWindow(QWidget):
    def __init__(self, app_settings=None, settings_path=None, runtime=None, process_start=None):
        super().__init__(None, Qt.Window)
        self._app_settings = app_settings if isinstance(app_settings, dict) else {}
        self._settings_path = settings_path
        self._runtime = dict(runtime or {})
        self._process_start = process_start
        self._text = ""
        self._pages = []
        self._page = 0
        self._generation = 0
        self._busy = False
        self._bridge = _DiagBridge()
        self._bridge.finished.connect(self._on_finished)

        self.setObjectName("diagWindow")
        self.setWindowTitle("tray-tools 診断")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(build_style())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self._status = QLabel("")
        self._status.setObjectName("diagStatus")
        layout.addWidget(self._status)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)

        self._view = QPlainTextEdit()
        self._view.setObjectName("diagText")
        self._view.setReadOnly(True)
        self._view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._view.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        font.setFamilies(["Consolas", "MS Gothic", font.family()])
        font.setPointSize(10)
        self._view.setFont(font)
        splitter.addWidget(self._view)

        qr_area = QWidget()
        qr_layout = QVBoxLayout(qr_area)
        qr_layout.setContentsMargins(0, 0, 0, 0)
        qr_layout.setSpacing(6)
        notice = QLabel(NOTICE)
        notice.setObjectName("diagNotice")
        notice.setWordWrap(True)
        qr_layout.addWidget(notice)
        self._qr = QrView()
        qr_layout.addWidget(self._qr, 1)

        nav = QHBoxLayout()
        self._prev = QPushButton("◀ 前へ")
        self._prev.clicked.connect(lambda: self._go(-1))
        self._page_label = QLabel("")
        self._page_label.setObjectName("diagPage")
        self._page_label.setAlignment(Qt.AlignCenter)
        self._next = QPushButton("次へ ▶")
        self._next.clicked.connect(lambda: self._go(1))
        nav.addWidget(self._prev)
        nav.addWidget(self._page_label, 1)
        nav.addWidget(self._next)
        qr_layout.addLayout(nav)
        splitter.addWidget(qr_area)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        self._splitter = splitter
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self._refresh_button = QPushButton("🔄 再取得")
        self._refresh_button.clicked.connect(self.refresh)
        self._copy_button = QPushButton("📋 コピー")
        self._copy_button.setObjectName("diagCopy")
        self._copy_button.clicked.connect(self._copy)
        self._save_button = QPushButton("💾 保存…")
        self._save_button.clicked.connect(self._save)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self._refresh_button)
        buttons.addStretch(1)
        buttons.addWidget(self._copy_button)
        buttons.addWidget(self._save_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        QShortcut(QKeySequence("Esc"), self).activated.connect(self.close)

        self._resize_to_saved()
        self._move_to_center()
        # まとめは25行前後。全文が見える高さを先に取り、残りを QR に回す。
        text_height = min(self.height() * 2 // 5, 26 * self._view.fontMetrics().lineSpacing() + 16)
        self._splitter.setSizes([text_height, max(200, self.height() - text_height)])
        self._update_pages()

    # ------------------------------------------------------------------
    def _screen(self):
        return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()

    def _resize_to_saved(self):
        area = self._screen().availableGeometry()
        # 既定の高さは画面に合わせる(QR を大きく出したいので、取れるだけ取る)。
        default = (DEFAULT_WIDTH, min(DEFAULT_HEIGHT, int(area.height() * 0.92)))
        width, height = _load_size(self._app_settings) or default
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
    def _set_status(self, text: str, level: str = "") -> None:
        self._status.setText(text)
        self._status.setProperty("level", level)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    def refresh(self) -> None:
        """まとめを作り直す。ワーカーで作り、終わったら _on_finished に返る。"""
        try:
            self._generation += 1
            self._busy = True
            self._set_status("取得中…（イベントログの読み取りに数秒かかることがあります）")
            self._view.setPlainText("取得中…")
            self._text = ""
            self._pages = []
            self._update_pages()
            # ワーカーへはコピーを渡す(メインスレッドで書き換わる辞書を共有しない)。
            snapshot = json.loads(json.dumps(self._app_settings, default=str))
            thread = threading.Thread(
                target=_work,
                args=(self._bridge, self._generation, snapshot, dict(self._runtime), self._process_start),
                name="diagnostics", daemon=True)
            thread.start()
        except Exception as e:  # noqa: BLE001  ボタンのスロット。投げ切るとプロセスごと落ちる
            traceback.print_exc()
            self._busy = False
            self._set_status(f"取得を始められませんでした: {e}", "error")
            self._update_pages()

    def _on_finished(self, generation, text, error) -> None:
        try:
            if generation != self._generation:
                return  # 古い取得の結果(再取得を続けて押したとき)
            self._busy = False
            if text is None:
                self._text = ""
                self._view.setPlainText("")
                self._pages = []
                self._set_status(f"取得できませんでした: {error}", "error")
            else:
                self._text = text
                self._view.setPlainText(text)
                self._pages = diagnostics.split_pages(text)
                self._page = 0
                size = len(text.encode("utf-8"))
                self._set_status(f"{datetime.now():%H:%M:%S} 取得　{size} バイト・QR {len(self._pages)} 枚")
            self._update_pages()
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._set_status(f"表示できませんでした: {e}", "error")

    def _update_pages(self) -> None:
        total = len(self._pages)
        busy = self._busy
        self._refresh_button.setEnabled(not busy)
        self._copy_button.setEnabled(bool(self._text) and not busy)
        self._save_button.setEnabled(bool(self._text) and not busy)
        if not total:
            self._qr.set_image(None)
            self._page_label.setText("")
            self._prev.setVisible(False)
            self._next.setVisible(False)
            return
        self._page = max(0, min(self._page, total - 1))
        multi = total > 1
        self._prev.setVisible(multi)
        self._next.setVisible(multi)
        self._prev.setEnabled(self._page > 0)
        self._next.setEnabled(self._page < total - 1)
        self._page_label.setText(f"{self._page + 1}/{total}" if multi else "")
        try:
            qr = diagnostics.make_qr(self._pages[self._page])
            self._qr.set_image(diagnostics.qr_to_qimage(qr))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._qr.set_image(None)
            self._set_status(f"QR を作れませんでした: {e}", "error")

    def _go(self, step: int) -> None:
        try:
            self._page += step
            self._update_pages()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _copy(self) -> None:
        try:
            if self._text:
                QApplication.clipboard().setText(self._text)
                self._set_status("全文をコピーしました")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"コピーできませんでした: {e}", "error")

    def _save(self) -> None:
        try:
            if not self._text:
                return
            documents = Path.home() / "Documents"
            folder = documents if documents.is_dir() else Path.home()
            default = folder / f"tray-tools-diag-{datetime.now():%Y%m%d-%H%M}.txt"
            path, _ = QFileDialog.getSaveFileName(self, "診断のまとめを保存", str(default),
                                                  "テキスト (*.txt)")
            if not path:
                return
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(self._text + "\n")
            self._set_status(f"保存しました: {Path(path).name}")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"保存できませんでした: {e}", "error")

    def closeEvent(self, event):
        try:
            _save_size(self._app_settings, self._settings_path, self.width(), self.height())
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


# ------------------------------------------------------------------
# プロセスの入口
# ------------------------------------------------------------------
def _install_excepthook() -> None:
    """どこにも捕まらなかった例外を error.log に残す(pythonw では標準エラーがどこにも出ない)。"""
    original = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        try:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} diagnostics uncaught =====\n"
                        + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        except OSError:
            pass
        original(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="tray-tools の診断の窓")
    parser.add_argument("--runtime", default="{}", help="常駐が渡す「いま動いているか」(JSON)")
    parser.add_argument("--parent-start", default="", help="常駐の起動時刻 YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--settings", default="", help="settings.json の場所")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    _install_excepthook()
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        runtime = json.loads(args.runtime)
        if not isinstance(runtime, dict):
            runtime = {}
    except ValueError:
        runtime = {}
    process_start = None
    if args.parent_start:
        try:
            process_start = datetime.strptime(args.parent_start, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            process_start = None
    settings_path = Path(args.settings) if args.settings else settings_module.SETTINGS_PATH
    app_settings = diagnostics.load_settings_readonly(settings_path)

    try:
        # 常駐とタスクバー上で同じアプリ扱いにされないよう、別の ID を名乗る。
        import ctypes
        shell32 = ctypes.WinDLL("shell32")
        set_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [ctypes.c_wchar_p]
        set_id.restype = ctypes.c_long
        set_id("traytools.diagnostics")
    except (AttributeError, OSError):
        pass
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    window = DiagnosticsWindow(app_settings, settings_path, runtime, process_start)
    window.show()
    window.raise_()
    window.activateWindow()
    window.refresh()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
