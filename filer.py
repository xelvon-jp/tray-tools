# filer.py
# 簡易2画面ファイラ。あふｗ(AfxW)を入れられない PC でも、同じ指の動きでファイルを
# 扱えるようにするためのもの。キーの割り当てはユーザー自身の AFXW.key に寄せてある
# (filer_core.DEFAULT_KEYMAP。settings.json の filer.keys で上書きできる)。
#
# 常駐(tray-tools)とは別のプロセスで動く。起こすのは filer_launch.py で、
# `pythonw filer.py` 単体でも起動できる。別プロセスにしたのは、
#   - ファイル操作(SHFileOperationW)は終わるまで呼んだスレッドを止める。常駐の
#     メインスレッドで呼ぶと、その間ホットキーも付箋も止まる
#   - ネットワークのフォルダは読むだけで固まることがある。巻き添えを常駐に出さない
# ため。COM は使わない(CLAUDE.md の COM の罠を持ち込まない。必要な Win32 は
# filer_shell.py に ctypes で閉じ込めてある)。
#
# 一覧の読み取りは毎回別スレッドで行い、一定時間で見切る(UNC や切断された
# ネットワークドライブで窓ごと固まらないように)。見切ったら元のフォルダに留まる。
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QEvent,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QObject,
    QRect,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QLinearGradient,
)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

import affx_icon
import filer_archive
import filer_core as core
import filer_everything
import filer_extmenu
import filer_launch
import filer_phone
import filer_shell
import filer_update
import settings as settings_module
from traytools_send import pythonw_executable

SETTINGS_SECTION = "filer"
ERROR_LOG_PATH = Path(__file__).resolve().parent / "filer_error.log"
# F7 のコンテキストメニューを出す子プロセス(COM を使うのはそちらだけ)。
CONTEXT_MENU_SCRIPT = Path(__file__).resolve().parent / "filer_contextmenu.py"

# 既定の配色は、ユーザーのあふｗの「表示色」設定から拾った値(濃紺の地に白、カーソルは
# シアンの下線、マークは灰色のグラデーション)。settings.json の filer.colors に同じ名前で
# 書けば1色ずつ上書きできる。キーの名前は変えないこと(既に書いている人の設定が効かなくなる)。
DEFAULT_COLORS = {
    "background": "#00001c",
    "text": "#ffffff",            # 通常のファイル
    "directory": "#80ffff",       # フォルダ(属性に関係なくこの色)
    "system": "#808080",          # システム属性
    "hidden": "#6c6c6c",          # 隠し属性(システムより少し暗く)
    "readonly": "#ff0080",        # 読み込み専用
    "cursor": "#00ffff",          # アクティブ窓のカーソルの下線(行の背景は塗らない)。
                                  # 実画面から採った淡い鋼色(#bfcddb)はグレーに見えて
                                  # 分かりにくかったので、本人の希望でシアン 2026-10-31
    "cursor_inactive": "#2a4a66",  # 非アクティブ窓のカーソルの下線(細く暗く)
    "mark": "#7f7f7f",            # マークした行の背景(左端の色)
    "mark_end": "#0c0c0c",        # マークした行の背景(右端の色。左→右のグラデーション)
    "mark_text": "",              # マーク行の字の色。空ならファイルの種類の色のまま
    "mark_cursor": "",            # 前の版の名残(今はマーク背景＋下線で示すので使わない)
    "dialog_cursor": "#1a3a8a",   # ソート・比較マークのダイアログで選んでいる行
    "header_active": "#00001c",   # アクティブ窓のパス表示の地と枠(あふｗと同じく
                                  # 帯は付けず地のまま。区別は字の色とカーソルで)
    "header_active_text": "#ffffff",
    "header_inactive": "#00001c",
    "header_inactive_text": "#c0c0c0",
    "footer_text": "#a0a0b0",
    "isearch": "#ffd400",
    "log_background": "#00001c",
    "log_text": "#f9f9f9",
    # 窓の一番下のステータスバーと、その上のファンクションキーの段。ファンクションキー段は
    # あふｗと同じ明るい灰色の地に黒い字(2026-10-29、本人の実画面に合わせて寄せた)。
    "stash_background": "#14532d",  # 一時置き場を表示している窓の地(通常の #00001c と見分けがつく緑)
    "status_background": "#16163c",
    "status_text": "#e0e0e8",
    "fkey_background": "#f0f0f0",   # あふｗのファンクションキー段(明るい灰色に黒字)
    "fkey_text": "#000000",
    "fkey_border": "#808080",
}

# カーソルの下線の太さ(px)。アクティブ窓ははっきり、非アクティブ窓は控えめに。
CURSOR_LINE_ACTIVE = 3
CURSOR_LINE_INACTIVE = 1

DEFAULT_FONT_FAMILIES = ["MS Gothic", "ＭＳ ゴシック", "Consolas"]
DEFAULT_FONT_SIZE = core.DEFAULT_FONT_SIZE
DEFAULT_EDITOR = "notepad.exe"
# F9 で開くシェル。pwsh.exe(PowerShell 7)にしたければ settings の filer.shell で。
DEFAULT_SHELL = "powershell.exe"
DEFAULT_LIST_TIMEOUT = 5.0
RECENT_LIMIT = 50
# フォルダの自動監視の間隔。短いほど外部の変更に早く追いつくが、指紋の走査
# (フォルダ1つ分の scandir)がその分増える。2秒なら体感の遅れも無く、走査も軽い。
WATCH_INTERVAL_MS = 2000
# 左右の境界を端まで寄せて、ファイル窓の幅がこれ以下になったら「畳まれた」とみなす。
# 畳まれた窓はアクティブにしない(見えない窓でキーが効くと、見えない所のファイルを
# 消したり移したりしてしまう。2026-10-29 本人と相談して、あふｗより安全側に寄せた)。
COLLAPSED_PANE_PX = 4
# ライブ絞り込み: この件数より少なければ打鍵ごとにすぐ絞り、多ければ打ち終わりを待つ。
LIVE_FILTER_IMMEDIATE = 3000
LIVE_FILTER_DELAY_MS = 150
# プレビュー: カーソルが止まってからこれだけ待って読む(連打で読み込みを積まない)。
PREVIEW_DELAY_MS = 120
# 画像ビュアの回転・反転のキー(仮想キーコード → core.ImageTurn の操作)。
IMAGE_TURN_KEYS = {82: "R", 76: "L", 72: "H", 86: "V"}
# 画像の読み込み中にオートリピートを捨てる、カーソルを動かすキー。
PREVIEW_REPEAT_KEYS = frozenset((Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown,
                                 Qt.Key_Left, Qt.Key_Right, Qt.Key_Home, Qt.Key_End))
# 画像の読み込みを待つ上限(秒)。これを過ぎたらリピートを捨てるのをやめる(固まらない保険)。
IMAGE_WAIT_LIMIT = 2.0

# 書庫の中では書き込み系を断る。あふｗも書庫の中(仮想フォルダ)は読むだけ。
ARCHIVE_READONLY = "書庫内では使えません"
ARCHIVE_NO_WRITE = "書庫内へは書き込めません"
OVERWRITE, SKIP, CANCEL = "overwrite", "skip", "cancel"

# offscreen(テスト)では QKeyEvent.nativeVirtualKey() が 0 で来る。そのときだけ
# Qt のキーから仮想キーコードを引く。実機の Windows では常に native の値を使う
# (日本語キーボードの「:」は Qt では Key_Colon/Key_Asterisk と変わるが、VK は 0xBA で一定)。
_QT_TO_VK = {int(k): v for k, v in {
    Qt.Key_Backspace: 8, Qt.Key_Tab: 9, Qt.Key_Backtab: 9, Qt.Key_Return: 13, Qt.Key_Enter: 13,
    Qt.Key_Escape: 27, Qt.Key_Space: 32, Qt.Key_PageUp: 33, Qt.Key_PageDown: 34,
    Qt.Key_End: 35, Qt.Key_Home: 36, Qt.Key_Left: 37, Qt.Key_Up: 38, Qt.Key_Right: 39,
    Qt.Key_Down: 40, Qt.Key_Insert: 45, Qt.Key_Delete: 46,
    Qt.Key_Colon: 186, Qt.Key_Asterisk: 186, Qt.Key_Comma: 188, Qt.Key_Less: 188,
    Qt.Key_Period: 190, Qt.Key_Greater: 190, Qt.Key_Slash: 191, Qt.Key_Question: 191,
}.items()}


def _vk_of(event) -> int:
    vk = event.nativeVirtualKey()
    if vk:
        return vk
    key = int(event.key())
    if int(Qt.Key_A) <= key <= int(Qt.Key_Z) or int(Qt.Key_0) <= key <= int(Qt.Key_9):
        return key  # 英数字は Qt のキー値と VK が同じ
    if int(Qt.Key_F1) <= key <= int(Qt.Key_F24):
        return 112 + key - int(Qt.Key_F1)
    return _QT_TO_VK.get(key, 0)


def _log_exception(where: str) -> str:
    """例外を filer_error.log に残し、メッセージ窓用の1行を返す(pythonw では標準エラーが
    どこにも出ないので、ファイルに残さないと理由が追えない)。"""
    text = traceback.format_exc()
    try:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {where} =====\n{text}")
    except OSError:
        pass
    try:
        print(text, file=sys.stderr)
    except Exception:  # noqa: BLE001
        # コンソールが cp1252/cp932 の環境では日本語を含む traceback の
        # print が UnicodeEncodeError になる(CI のジョブ失敗の原因)。
        # 書けない文字は落としてでも、落とさずに出す。
        try:
            enc = getattr(sys.stderr, "encoding", None) or "utf-8"
            sys.stderr.write(text.encode(enc, "replace").decode(enc))
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
    exc_type, exc_value = sys.exc_info()[:2]
    return f"{exc_type.__name__}: {exc_value}" if exc_type else "unknown error"


def _thread_com_init() -> bool:
    """このスレッドで COM(STA) を初期化する。初期化できた(または
    初期化済みだった)なら True を返し、呼び出し側は対で
    _thread_com_uninit() を呼ぶ。モード違いで初期化できなかった
    (RPC_E_CHANGED_MODE)ときは False(戻す対が無い)。"""
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.c_long  # HRESULT
        # COINIT_APARTMENTTHREADED(0x2) | COINIT_DISABLE_OLE1DDE(0x4)
        hr = ole32.CoInitializeEx(None, 0x2 | 0x4)
        return hr in (0, 1)  # S_OK / S_FALSE
    except Exception:  # noqa: BLE001  初期化できなくても転送自体は試す
        return False


def _thread_com_uninit() -> None:
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass


def _section(app_settings) -> dict:
    section = (app_settings or {}).get(SETTINGS_SECTION)
    return section if isinstance(section, dict) else {}


def save_values(app_settings, settings_path, updates: dict) -> None:
    """filer セクションの該当キーだけを書き換える(bg_remove._save_values と同じ作法)。

    メモリ上の設定を丸ごと書くと既定値まで焼き込まれ、settings.json の姿が変わる。
    常駐が同時に別のキーを書いていても、読み直してから書けば消し合わない。"""
    if isinstance(app_settings, dict):
        section = app_settings.get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            section = app_settings[SETTINGS_SECTION] = {}
        section.update(updates)
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
        section.update(updates)
        settings_module.save_settings(stored, settings_path)
    except Exception as e:  # noqa: BLE001  覚えられないだけ。閉じる操作を止めない
        print(f"[filer] 設定を保存できません: {e}", file=sys.stderr)


# ---------------------------------------------------------------
# 一覧のモデルと描画
# ---------------------------------------------------------------
ENTRY_ROLE = Qt.UserRole + 1
MARK_ROLE = Qt.UserRole + 2

COL_NAME, COL_EXT, COL_SIZE, COL_DATE = range(4)


class FileModel(QAbstractTableModel):
    """1つの窓の一覧。項目は filer_core.Entry、マークは名前の集合で持つ(Pane と共有)。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.marks = set()

    def set_entries(self, entries, marks) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.marks = marks
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 4

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.entries)):
            return None
        entry = self.entries[index.row()]
        if role == ENTRY_ROLE:
            return entry
        if role == MARK_ROLE:
            return entry.name in self.marks
        if role == Qt.DisplayRole:
            col = index.column()
            if entry.is_parent:
                return ".." if col == COL_NAME else ""
            if col == COL_NAME:
                return core.split_name(entry)[0]
            if col == COL_EXT:
                # あふｗと同じく点付き(.url)。名前の列とは別にして、長い名前は名前の側を
                # … で省いても拡張子は必ず見えるようにする。
                ext = core.split_name(entry)[1]
                return f".{ext}" if ext else ""
            if col == COL_SIZE:
                return core.format_size(entry)
            if col == COL_DATE:
                return core.format_mtime(entry.mtime)
        return None

    def row_changed(self, row: int) -> None:
        if 0 <= row < len(self.entries):
            self.dataChanged.emit(self.index(row, 0), self.index(row, 3))

    def all_changed(self) -> None:
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 3))


class RowDelegate(QStyledItemDelegate):
    """行の色を自前で塗る。Qt の選択色に任せないのは、「マーク」と「カーソル」と
    「アクティブかどうか」の組み合わせを、あふｗのように一目で見分けたいため。"""

    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane

    def paint(self, painter, option, index):
        colors = self.pane.colors
        entry = index.data(ENTRY_ROLE)
        marked = bool(index.data(MARK_ROLE))
        is_cursor = index.row() == self.pane.view.currentIndex().row()
        active = self.pane.is_active()
        rect = option.rect

        # 字の色はファイルの種類(フォルダ/システム/隠し/読み込み専用/通常)で決まる。
        # マーク行でも変えない(あふｗの「通常ファイルの色とマーク背景」と同じ見え方)。
        fg = self.pane.text_color(entry, index.column(), marked)
        painter.save()
        try:
            if marked:
                # マークは灰色のグラデーション。行全体で1本に見えるよう、列ごとではなく
                # ビューポートの左端〜右端を基準にする。
                width = max(self.pane.view.viewport().width(), 1)
                gradient = QLinearGradient(0, 0, width, 0)
                gradient.setColorAt(0.0, QColor(colors["mark"]))
                gradient.setColorAt(1.0, QColor(colors.get("mark_end") or colors["mark"]))
                painter.fillRect(rect, QBrush(gradient))
            if is_cursor:
                # カーソル行は背景を塗らず、下線だけ(あふｗのカーソル表示)。
                thick = CURSOR_LINE_ACTIVE if active else CURSOR_LINE_INACTIVE
                color = colors["cursor"] if active else colors["cursor_inactive"]
                painter.fillRect(QRect(rect.left(), rect.bottom() - thick + 1,
                                       rect.width(), thick), QColor(color))
            text = index.data(Qt.DisplayRole) or ""
            painter.setPen(QColor(fg))
            painter.setFont(option.font)
            text_rect = rect.adjusted(4, 0, -4, 0)
            align = Qt.AlignVCenter | (
                Qt.AlignRight if index.column() == COL_SIZE else Qt.AlignLeft)
            fm = QFontMetrics(option.font)
            text = fm.elidedText(text, Qt.ElideRight, max(text_rect.width(), 0))
            painter.drawText(text_rect, align, text)
        finally:
            painter.restore()


class FileView(QTableView):
    """キーはすべて窓(FilerWindow)へ渡す。Qt の既定のキー操作(先頭文字での移動・
    Tab でのフォーカス移動など)は使わない。未割り当てのキーで何かが起きないように。"""

    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane

    def event(self, event):
        try:
            kind = event.type()
            if kind in (QEvent.KeyPress, QEvent.KeyRelease) and event.key() in (
                    Qt.Key_Shift, Qt.Key_Control):
                # Shift / Ctrl を押している間は、Fキー一覧をその修飾付きの割り当てに
                # 切り替える(あふｗと同じ)。修飾キーそのものの押下・解放では
                # modifiers() にそのキーが入っていたりいなかったりするので、自分で足し引きする。
                flag = Qt.ShiftModifier if event.key() == Qt.Key_Shift else Qt.ControlModifier
                mods = event.modifiers()
                mods = (mods | flag) if kind == QEvent.KeyPress else (mods & ~flag)
                self.pane.window_.update_fkey_bar(mods)
            if kind == QEvent.KeyPress:
                self.pane.window_.handle_key(self.pane, event)
                return True
        except Exception:  # noqa: BLE001  スロットで投げ切るとプロセスごと落ちる
            self.pane.window_.log(f"エラー: {_log_exception('key')}")
            return True
        return super().event(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self.pane.fit_columns()
        except Exception:  # noqa: BLE001
            _log_exception("viewResize")

    # --- マウス ---
    # マウスでは行を選ばない・動かさない(あふｗと同じく、指はキーボードに置いたまま)。
    # クリックでカーソル行が飛ぶと、キーで決めた対象が知らないうちに変わる。できるのは
    # 「押した側の窓をアクティブにする」ことと、マークしたものを外のアプリへ
    # ドラッグすることだけ。窓の切り替えだけは、いちいち Tab に手を伸ばすより
    # クリックの方が早い場面があるので許可する(本人の指定)。
    def mousePressEvent(self, event):
        try:
            # 押した側の窓をアクティブにしてキーが効くようにする。カーソル行は
            # 動かさない(行の選択は super を呼ばないので起きない)。押した位置は
            # ドラッグの始まりとして覚えておく。
            self.pane.window_.activate(self.pane.side)
            self._press_pos = (event.position().toPoint()
                               if event.button() == Qt.LeftButton else None)
        except Exception:  # noqa: BLE001
            _log_exception("mousePress")
        event.accept()

    def mouseMoveEvent(self, event):
        try:
            start = getattr(self, "_press_pos", None)
            if start is None or not (event.buttons() & Qt.LeftButton):
                event.accept()
                return
            moved = (event.position().toPoint() - start).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                self.pane.window_.start_drag(self.pane)
        except Exception:  # noqa: BLE001
            self.pane.window_.log(f"エラー: {_log_exception('drag')}")
        event.accept()

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        """ホイールは一覧をスクロールするだけで、カーソル行は基本動かさない。ただし
        カーソルが画面の外へ出そうになったら、画面の端の行まで連れてくる(あふｗと同じ。
        見えない行が Enter や C の対象のまま残ると、何に効くのか分からなくなる)。"""
        super().wheelEvent(event)
        try:
            self.pane.keep_cursor_visible()
        except Exception:  # noqa: BLE001
            _log_exception("wheel")

    def contextMenuEvent(self, event):
        event.accept()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        try:
            self.pane.window_.activate(self.pane.side, focus=False)
        except Exception:  # noqa: BLE001
            _log_exception("focusIn")

    def currentChanged(self, current, previous):
        super().currentChanged(current, previous)
        # 行の色は delegate がカーソル位置を見て塗るので、前後の行を描き直させる。
        self.viewport().update()
        try:
            self.pane.update_footer()
            if self.pane.is_active():
                self.pane.window_.update_status_bar()
                self.pane.window_.schedule_preview()
        except Exception:  # noqa: BLE001
            _log_exception("currentChanged")


class ListBridge(QObject):
    """読み取りスレッド → メインスレッドへ結果を渡す口(Qt はスレッドを跨いで触れない)。"""

    finished = Signal(object)
    # 書庫の展開: 進み具合 (済んだ数, 全体, 名前) と、終わったとき (ExtractResult)。
    progress = Signal(object)
    extracted = Signal(object)
    # F7 のコンテキストメニュー(子プロセス)が終わったとき。値は終了コード。
    context_done = Signal(object)
    # ドライブのラベル・容量が読めたとき。(根, 種類 "label"|"space", 値)。
    drive_info = Signal(object)
    # プレビューの中身ができたとき。(要求番号, 結果)。
    preview_ready = Signal(object)
    # ハッシュの計算が終わったとき。[(パス, 名前, ハッシュ or None, エラー文字列 or "")]。
    hashed = Signal(object)
    # フォルダ構成(C→I)のツリーができたとき。(根, 行の一覧 or None, エラー文字列)。
    tree_built = Signal(object)
    # 転送キュー: 裏のスレッドで終わった転送の (op, result) を戻す。
    op_done = Signal(object)
    # フォルダの自動監視(外部で中身が変わったら再読込)の結果。
    folder_watch = Signal(object)
    # Shift+F5 の更新: fetch して調べた結果 / pull の結果。
    update_checked = Signal(object)
    update_pulled = Signal(object)
    # 版の連番(git log)が数え終わったとき。filer_update.git_version の結果 or None。
    version_ready = Signal(object)


# ---------------------------------------------------------------
# 片方の窓
# ---------------------------------------------------------------
class Pane(QWidget):
    def __init__(self, window, side: str):
        super().__init__(window)
        self.window_ = window
        self.side = side
        self.colors = window.colors
        self.path = None
        self.all_entries = []      # マスク前(".." を含まない)
        self.entries = []          # 表示中(".." を含む)
        self.marks = set()
        self.mask_text = core.DEFAULT_MASK
        self.patterns = []
        self.history = core.FolderHistory()
        # 並べ方は窓ごと(あふｗと同じ)。前回の設定を引き継ぐ。
        self.sort = core.normalize_sort(_section(window.app_settings).get(f"{side}_sort"))
        self.loading = False
        self.request_id = 0
        self.isearch_text = None   # None ならインクリメンタルサーチ中ではない
        # 書庫の中を見ているときは (書庫ファイル, 中のパス)。ふつうのフォルダなら None。
        # path の方は D:\x\foo.zip\sub の形で、履歴・O・H などはこちらを通す。
        self.archive = None
        # ライブ絞り込み(/)。語は空白区切りで AND。
        self.live_filter = ""
        self.live_words = []
        self._pending_filter = ""
        # 一時置き場(T)。パスの一覧を1つの一覧として出す。stash_from は
        # 置き場を開く前にいたフォルダ(T でもう一度押すと戻る)。
        self.stash = False
        self.stash_from = None
        # フラット表示(Alt+B)。いまのフォルダ以下のファイルを1つの一覧に。
        self.flat = False
        self.flat_loading = False
        self.flat_cancel = None
        self.flat_count = 0
        # 文字列検索(G)。結果はフラット表示の形で出す。grep_query が検索語、
        # grep_matches は {相対パス: (ヒット行数, 最初のヒット行)}。
        self.grep_query = None
        self.grep_case = False
        # 自動監視の基準にしたフォルダの指紋(core.folder_signature)。
        # None の間は「まだ基準が無い」。フォルダを移るたびに捨てる。
        self.watch_signature = None
        self.grep_matches = {}
        self.grep_loading = False
        self.grep_cancel = None
        self.grep_count = 0
        # Everything 検索(Shift+F)。結果はフラット表示の形で出す(フラット表示・文字列検索と
        # 同じ一覧の仕組みをそのまま使う)。es_query が検索語、es_all は「全体から探した」か、
        # es_status は "done" / "limit"(上限で打ち切った)。
        self.es_query = None
        self.es_all = False
        self.es_loading = False
        self.es_cancel = None
        self.es_status = "done"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.pane_font = QFont(window.list_font)
        self.pane_font.setPointSize(window.pane_font_sizes[side])
        # 上の2行(あふｗと同じ): 1行目はパスとマスク、2行目はドライブのラベル。
        self.header = QLabel()
        self.header.setTextFormat(Qt.PlainText)
        self.header.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.header)
        self.header2 = QLabel()
        self.header2.setTextFormat(Qt.PlainText)
        self.header2.setContentsMargins(4, 0, 4, 0)
        layout.addWidget(self.header2)

        self.model = FileModel(self)
        self.view = FileView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(RowDelegate(self))
        self.view.setSelectionMode(QAbstractItemView.NoSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.setTabKeyNavigation(False)
        # クリックではフォーカスを取らない(取ると focusIn で押した側の窓がアクティブに
        # なってしまう)。フォーカスはキー操作と mousePressEvent が明示的に渡す。
        self.view.setFocusPolicy(Qt.TabFocus)
        # 横には流さない。入りきらないときは fit_columns が列を減らす。
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 縦は、ファイルが多くて入りきらないときだけバーを出す(あふｗと同じ)。
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().hide()
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col in (COL_EXT, COL_SIZE, COL_DATE):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
        layout.addWidget(self.view, 1)

        # ライブ絞り込みの入力欄(/ で出る)。窓の下の行のすぐ上。
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setPlaceholderText("絞り込み（空白区切りで AND。Enter 確定 / Esc 解除）")
        self.filter_edit.setStyleSheet(
            f"background-color: {window.colors['background']}; color: {window.colors['isearch']};"
            f" border: 1px solid {window.colors['header_active']};")
        self.filter_edit.textChanged.connect(self._on_filter_text)
        self.filter_edit.installEventFilter(self)
        self.filter_edit.hide()
        layout.addWidget(self.filter_edit)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._apply_pending_filter)
        self._flat_timer = QTimer(self)
        self._flat_timer.timeout.connect(self._tick_flat)

        self.footer = QLabel()
        self.footer.setTextFormat(Qt.PlainText)
        self.footer.setContentsMargins(4, 0, 4, 0)
        self.footer.setAlignment(Qt.AlignCenter)   # あふｗは窓の下の行を中央に出す
        layout.addWidget(self.footer)
        # QLabel は既定だと文字列の幅を最小幅にする。深いパスを開くと窓がそれより狭く
        # できなくなり、左右の境界も動かせなくなるので、横幅は文字に合わせない。
        for label in (self.header, self.header2, self.footer):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.apply_font(self.pane_font)
        self.apply_style()

    def apply_font(self, font) -> None:
        """一覧の字を差し替える。行の高さと列の幅も字に合わせて測り直す
        (Shift/Ctrl+↑↓ の拡大縮小で、行が重なったり列が欠けたりしないように)。"""
        self.pane_font = QFont(font)
        self.view.setFont(self.pane_font)
        # 上のパス行・2行目(ラベル)・下の行は固定の字(10pt)。一覧の拡大縮小に
        # 連動させない(2026-10-29 要望。あふｗも小さい字で出す)。高さは字の高さちょうど。
        small = self.window_.log_font
        for widget in (self.header, self.header2, self.footer):
            widget.setFont(small)
            widget.setFixedHeight(QFontMetrics(small).height())
        fm = QFontMetrics(self.pane_font)
        # 行の高さは字の高さちょうど(あふｗと同じく行間を空けない)。Qt の行は既定で
        # 字より数px高く、一覧が間延びして見える。最小の高さも外しておかないと縮まない。
        header = self.view.verticalHeader()
        header.setMinimumSectionSize(1)
        header.setDefaultSectionSize(fm.height())
        self._col_widths = {
            col: fm.horizontalAdvance(sample) + 8
            for col, sample in ((COL_EXT, ".MMMMM"), (COL_SIZE, "9,999,999,999 "),
                                (COL_DATE, "26/10/23 12:34:56 "))}
        self._name_min = fm.horizontalAdvance("M" * 12)
        self.fit_columns()
        self._fit_ext_column()

    def _fit_ext_column(self) -> None:
        """拡張子の列は、そのフォルダで一番長い拡張子が欠けずに入る幅にする。

        あふｗと同じく、長いときは名前の側を削って拡張子は必ず見せる。
        固定の見本 ".MMMMM" の幅では、.safetensors のような長い拡張子が
        … で省かれてしまう。上限だけ付け、病的に長いものは従来どおり省く。"""
        widths = getattr(self, "_col_widths", None)
        entries = getattr(self, "entries", None)
        if not widths or not entries:
            return
        fm = QFontMetrics(self.pane_font)
        need = fm.horizontalAdvance(".MMMMM") + 8
        cap = fm.horizontalAdvance("M" * 24) + 8
        for entry in entries:
            if entry.is_parent:
                continue
            ext = core.split_name(entry)[1]
            if ext:
                need = max(need, fm.horizontalAdvance(f".{ext}") + 8)
                if need >= cap:
                    need = cap
                    break
        if widths[COL_EXT] != need:
            widths[COL_EXT] = need
            self.fit_columns()

    def fit_columns(self) -> None:
        """列の幅を窓に合わせる。名前の列が狭くなりすぎるなら、日時 → サイズ → 拡張子の
        順に列を隠す(境界を寄せたり字を大きくしたりしたときに、名前が読めなくなって
        横スクロールが出るのを防ぐ)。"""
        widths = getattr(self, "_col_widths", None)
        if not widths:
            return
        header = self.view.horizontalHeader()
        available = self.view.viewport().width()
        shown = [COL_EXT, COL_SIZE, COL_DATE]
        while shown and available - sum(widths[c] for c in shown) < self._name_min:
            shown.pop()
        for col in (COL_EXT, COL_SIZE, COL_DATE):
            if col in shown:
                header.showSection(col)
                header.resizeSection(col, widths[col])
            else:
                header.hideSection(col)
        self.view.viewport().update()

    # --- 見た目 ---
    def is_active(self) -> bool:
        return self.window_.active_side == self.side

    def apply_style(self) -> None:
        c = self.colors
        active = self.is_active()
        # 一時置き場の表示中は地の色を変えて、通常のフォルダと一目で見分ける。
        background = c["stash_background"] if self.stash else c["background"]
        header_style = (
            f"background-color: {c['header_active'] if active else c['header_inactive']};"
            f" color: {c['header_active_text'] if active else c['header_inactive_text']};")
        self.header.setStyleSheet(header_style)
        self.header2.setStyleSheet(header_style)
        border = c["header_active"] if active else c["header_inactive"]
        self.view.setStyleSheet(
            f"QTableView {{ background-color: {background}; color: {c['text']};"
            f" border: 2px solid {border}; }}"
            # 縦スクロールバーは濃紺の地でも見える色で(素のままだと地に埋もれる)。
            f" QScrollBar:vertical {{ background-color: {background}; width: 12px;"
            f" margin: 0px; }}"
            f" QScrollBar::handle:vertical {{ background-color: #54687f;"
            f" min-height: 24px; }}"
            f" QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            f" {{ height: 0px; }}"
            f" QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical"
            f" {{ background-color: transparent; }}")
        self.footer.setStyleSheet(
            f"background-color: {background}; color: {c['footer_text']};")
        self.update_header()
        self.view.viewport().update()

    def text_color(self, entry, column: int, marked: bool = False) -> str:
        """1つの欄の字の色。ファイルの種類(フォルダ/システム/隠し/読み込み専用/通常)で
        決まり、拡張子の列だけは拡張子別表示色(filer.ext_colors)を重ねる。名前の列は
        種類の色のまま。属性色の行(隠し等)やフォルダには拡張子の色を付けない。
        マーク行でも変えない(filer.colors の mark_text を書いたときだけそれにする)。"""
        colors = self.colors
        if marked and colors.get("mark_text"):
            return colors["mark_text"]
        kind = entry.color_kind if entry is not None else "text"
        color = colors.get(kind) or colors["text"]
        if column == COL_EXT:
            color = core.ext_color_for(entry, self.window_.ext_color_map) or color
        return color

    def drive_root(self):
        """この窓のドライブの根(書庫の中なら書庫のあるドライブ)。"""
        return core.drive_root(self.archive[0] if self.archive is not None else self.path)

    def update_header(self) -> None:
        if not hasattr(self, "header2"):
            return
        # 1行目: "C:\Program Files\ *.*"(あふｗと同じく、パスの末尾に \ と空白とマスク)。
        if self.stash:
            text = f"一時置き場 {self.mask_text}"
        elif self.path:
            text = self.path if self.path.endswith("\\") else self.path + "\\"
            text += f" {self.mask_text}"
        else:
            text = "(読み込み中)"
        if self.es_query is not None or self.es_loading:
            scope = "全体" if self.es_all else "以下"
            text += f"   [Everything({scope}): {self.es_query or ''}]"
        elif self.grep_query is not None or self.grep_loading:
            text += f"   [検索: {self.grep_query or ''}]"
        elif self.flat or self.flat_loading:
            text += "   [フラット]"
        if self.live_filter:
            text += f"   [絞込: {self.live_filter}]"
        if self.sort != core.DEFAULT_SORT:
            arrow = "↓" if self.sort["descending"] else "↑"
            text += f"   ({core.SORT_LABELS[self.sort['kind']]}{arrow})"
        # Shift+Z で隠しているものがあることを小さく出す(見えないものがあると気付けるように)。
        hidden_kinds = [label for label, shown in (("隠", self.window_.show_hidden),
                                                   ("シ", self.window_.show_system)) if not shown]
        if hidden_kinds:
            text += f"   〈{'/'.join(hidden_kinds)} 非表示〉"
        if self.loading:
            text += "   …"
        self.header.setText(text)
        # 2行目: ドライブのラベル(読み取りは別スレッド。まだなら空)。
        self.header2.setText(self.window_.drive_label_text(self.drive_root()))

    def update_footer(self) -> None:
        if not hasattr(self, "footer"):
            return  # 組み立ての途中(ビューを作った時点で currentChanged が飛ぶ)
        dirs, files = core.count_dirs_files(self.entries)
        count, total = core.marks_summary(self.entries, self.marks)
        if self.stash:
            size = sum(e.size for e in self.entries if not e.is_dir)
            text = f"一時置き場 {dirs + files}件   {size:,} bytes"
            if count:
                text += f"   Mark {count} ({total:,})"
        elif self.es_loading:
            text = "Everything で検索中（Esc で中止）"
        elif self.es_query is not None:
            size = sum(e.size for e in self.entries if not e.is_dir)
            text = f"Everything「{self.es_query}」 {dirs + files}件"
            if self.es_status == "limit":
                text += "（上限で打ち切り）"
            text += f"   {size:,} bytes"
            if count:
                text += f"   Mark {count} ({total:,})"
        elif self.grep_loading:
            text = f"検索中 {self.grep_count:,}ファイル走査（Esc で中止）"
        elif self.grep_query is not None:
            lines = sum(c for c, _ in self.grep_matches.values())
            size = sum(e.size for e in self.entries if not e.is_dir)
            text = f"検索「{self.grep_query}」 {files}件 / {lines:,}行ヒット   {size:,} bytes"
            if count:
                text += f"   Mark {count} ({total:,})"
        elif self.flat_loading:
            text = f"収集中 {self.flat_count:,}件（Esc で中止）"
        elif self.flat:
            size = sum(e.size for e in self.entries if not e.is_dir)
            text = core.flat_footer_text(files, size, count, total)
        else:
            space = None
            if self.archive is None:
                space = self.window_.drive_space(self.drive_root())
            text = core.footer_text(dirs, files, *(space or (None, None)),
                                    mark_count=count, mark_bytes=total)
        background = self.colors["stash_background"] if self.stash else self.colors["background"]
        if self.isearch_text is not None:
            text += f"   検索: {self.isearch_text}_"
            self.footer.setStyleSheet(
                f"background-color: {background}; color: {self.colors['isearch']};")
        else:
            self.footer.setStyleSheet(
                f"background-color: {background};"
                f" color: {self.colors['footer_text']};")
        self.footer.setText(text)

    # --- カーソル ---
    def cursor_row(self) -> int:
        return self.view.currentIndex().row()

    def cursor_entry(self):
        row = self.cursor_row()
        return self.entries[row] if 0 <= row < len(self.entries) else None

    def set_cursor(self, row: int) -> None:
        if not self.entries:
            return
        row = max(0, min(row, len(self.entries) - 1))
        index = self.model.index(row, 0)
        self.view.selectionModel().setCurrentIndex(index, QItemSelectionModel.NoUpdate)
        self.view.scrollTo(index)

    def keep_cursor_visible(self) -> None:
        """カーソル行が画面の外なら、画面に全部見えている行のうち近い端の行へ移す。
        画面の中にあるなら動かさない(ホイール用)。"""
        if not self.entries:
            return
        viewport = self.view.viewport().rect()
        first = self.view.rowAt(0)
        last = self.view.rowAt(viewport.height() - 1)
        if first < 0:
            return
        if last < 0:  # 一覧が画面より短い
            last = len(self.entries) - 1
        rows = [row for row in range(first, last + 1)
                if viewport.contains(self.view.visualRect(self.model.index(row, 0)))]
        if not rows:
            return
        row = self.cursor_row()
        if row < rows[0]:
            self.set_cursor(rows[0])
        elif row > rows[-1]:
            self.set_cursor(rows[-1])

    def page_rows(self) -> int:
        height = self.view.verticalHeader().defaultSectionSize() or 1
        return max(1, self.view.viewport().height() // height - 1)

    def targets(self) -> list:
        return core.pick_targets(self.entries, self.marks, self.cursor_row())

    def target_paths(self) -> list:
        return [os.path.join(self.path, e.name) for e in self.targets()]

    def real_dir(self):
        """実在するフォルダ。書庫の中なら書庫ファイルのあるフォルダ。"""
        if self.archive is not None:
            return os.path.dirname(self.archive[0])
        return self.path

    # --- 一覧の読み込み ---
    def load(self, path: str, focus=None, history="visit", keep_marks=False,
             walk_up=False, on_fail=None) -> None:
        """path を別スレッドで読み、終わったら一覧を差し替える。

        history … "visit"(履歴に積む) / None(積まない。再読込) / ("back"|"forward", 直前のパス)
        on_fail … 読めなかったときに呼ぶ(起動時の復元で、次の候補へ回すため)。"""
        # ふつうの読み込みはフラット表示・検索結果・一時置き場を終える
        # (フォルダを移る・BS・O など)。
        self._stop_flat()
        self._stop_grep()
        self._stop_es()
        was_stash = self.stash
        self.stash = False
        self.stash_from = None
        if was_stash:
            # 地の色を通常へ戻す。ここで戻しておかないと、緑のスタイルシートが
            # 残ったままになり、窓をクリック(アクティブ化)するまで直らない。
            self.apply_style()
        self.request_id += 1
        request = self.request_id
        self.loading = True
        self.update_header()
        # 書庫の一覧は大きいと時間がかかる(ネットワーク上の書庫もある)ので、見切りを長めに。
        # 書庫かどうかは字面だけで決める(ここは UI スレッドなので I/O をしない)。
        timeout = self.window_.list_timeout
        if filer_archive.looks_like_archive_path(path):
            timeout = max(timeout, self.window_.archive_timeout)
        timeout_ms = int(timeout * 1000)
        args = dict(path=path, focus=focus, history=history, keep_marks=keep_marks,
                    on_fail=on_fail)
        bridge = self.window_.bridge

        def worker():
            try:
                result = core.read_folder(path, focus, walk_up=walk_up)
                payload = (self.side, request, True, result, args)
            except Exception as e:  # noqa: BLE001  何が来ても結果として返す
                payload = (self.side, request, False, f"{type(e).__name__}: {e}", args)
            try:
                bridge.finished.emit(payload)
            except RuntimeError:
                pass  # 窓が先に閉じた

        threading.Thread(target=worker, daemon=True, name=f"filer-list-{self.side}").start()
        QTimer.singleShot(timeout_ms, lambda: self._on_timeout(
            request, path, on_fail, history, timeout))

    def _on_timeout(self, request, path, on_fail, history, timeout) -> None:
        try:
            if request != self.request_id or not self.loading:
                return
            # 以後この読み取りの結果が来ても捨てる(request_id を進めておく)。
            self.request_id += 1
            self.loading = False
            self.update_header()
            self.window_.log(f"応答がありません({timeout:g}秒): {path}")
            self._undo_history(history)
            if on_fail is not None:
                on_fail()
        except Exception:  # noqa: BLE001
            self.window_.log(f"エラー: {_log_exception('timeout')}")

    def _undo_history(self, history) -> None:
        if isinstance(history, tuple):
            direction, previous = history
            self.history.undo_step(direction, previous)

    def on_loaded(self, request, ok, result, args) -> None:
        if request != self.request_id:
            return  # 見切った後や、次の移動が始まった後に届いた古い結果
        self.loading = False
        if not ok:
            self.update_header()
            self.window_.log(f"開けません: {args['path']}  ({result})")
            self._undo_history(args["history"])
            if args["on_fail"] is not None:
                args["on_fail"]()
            return
        new_path, entries, focus, archive = result
        same = core.same_dir(self.path, new_path) if self.path else False
        if not same:
            # フォルダを移ったら、自動監視の指紋は旧フォルダのものなので捨てる
            # (次の監視で新しいフォルダの基準を取り直す)。
            self.watch_signature = None
        if archive is not None:
            archive_file, inner, skipped = archive
            entering = self.archive is None or not core.same_dir(self.archive[0], archive_file)
            if skipped and entering:
                # 書庫に入ったときに1回だけ知らせる(中を移動するたびには出さない)。
                self.window_.log(f"書庫: 危ないパスのエントリ {skipped}件を除外しました"
                                 "（.. や絶対パス。展開もしません）")
            self.archive = (archive_file, inner)
        else:
            self.archive = None
        previous_name = None
        previous_row = self.cursor_row()
        if same:
            entry = self.cursor_entry()
            previous_name = entry.name if entry is not None else None
        if not (same and args["keep_marks"]):
            self.marks = set()
        if not same:
            # フォルダを移ったらライブ絞り込みは解く(前のフォルダの語が残ると、何も
            # 無いように見えて混乱する)。
            self.clear_live_filter(rebuild=False)
        self.last_load_moved = not same     # よく行くフォルダの記録は「移った」ときだけ
        self.path = new_path
        self.all_entries = entries
        self._rebuild()
        # カーソルの行き先: 指定の名前 > (再読込なら)元の名前 > (再読込なら)元の行 > 先頭
        target = self._row_of(focus) if focus else None
        if target is None and same:
            target = self._row_of(previous_name) if previous_name else None
            if target is None:
                target = previous_row
        self.set_cursor(target if target is not None else 0)
        history = args["history"]
        if history == "visit":
            self.history.visit(new_path)
        elif isinstance(history, tuple):
            # 戻る/進むは履歴の側を先に動かしてある。読めた場所に揃えるだけ。
            self.history.current = new_path
        self.window_.on_pane_loaded(self)
        self.update_header()
        self.update_footer()

    def _rebuild(self) -> None:
        """マスク・ライブ絞り込み・Shift+Z を掛け直して表示の並びを作る。

        マークは「まだ一覧にあるもの」だけ持ち越す。絞り込みで見えなくなったマークは
        消さずに持っておく(絞り込みを解けば戻る)。ただし操作の対象になるのは見えている
        マークだけ(pick_targets は表示中の項目から選ぶ)。見えないものまで消したり
        コピーしたりするのは事故の元なので、安全な側に倒した。"""
        win = self.window_
        shown = core.filter_attrs(self.all_entries, win.show_hidden, win.show_system)
        masked = core.filter_entries(shown, self.patterns)
        visible = core.sort_entries(core.live_filter_entries(masked, self.live_words), self.sort)
        names = {e.name for e in self.all_entries}
        self.marks = {m for m in self.marks if m in names}
        parent = [] if (self.flat or self.stash or core.is_root(self.path)) else [core.PARENT_ENTRY]
        self.entries = parent + visible
        self.model.set_entries(self.entries, self.marks)
        self._fit_ext_column()

    def _row_of(self, name):
        if not name:
            return None
        folded = name.casefold()
        for row, e in enumerate(self.entries):
            if e.name.casefold() == folded:
                return row
        return None

    def reload(self, keep_marks=False, focus=None) -> None:
        if self.stash:
            self.show_stash()
            return
        if self.es_query is not None and self.path:
            self.load_everything(self.es_query, self.es_all, keep_marks=keep_marks)
            return
        if self.grep_query is not None and self.path:
            self.load_grep(self.grep_query, self.grep_case, keep_marks=keep_marks)
            return
        if self.flat and self.path:
            self.load_flat(keep_marks=keep_marks)   # フラット表示のまま集め直す
            return
        if self.path:
            self.load(self.path, focus=focus, history=None, keep_marks=keep_marks, walk_up=True)

    # --- 一時置き場(T) ---
    def show_stash(self) -> None:
        """一時置き場をこの窓に出す。項目の名前はフルパス(コア側の注記参照)。

        置き場に登録されたパスをその場で stat し直すので、移動・削除された
        実体はここで落ちる(登録からも外し、ログに残す)。"""
        win = self.window_
        if not self.stash:
            self.stash_from = self.real_dir() if self.archive else self.path
        self._stop_flat()
        self._stop_grep()
        self._stop_es()
        self.request_id += 1   # 進行中のフォルダ読み込みを無効化する
        self.loading = False
        entries, missing = core.stash_entries(win.stash_paths)
        if missing:
            win.remove_stash_paths(missing)
            win.log(f"一時置き場: 無くなっていた {len(missing)}件を登録から外しました")
        self.stash = True
        self.path = ""
        self.all_entries = entries
        self._rebuild()
        self.set_cursor(0)
        self.apply_style()
        self.update_header()
        self.update_footer()
        if self.is_active():
            win.update_status_bar()

    def close_stash(self) -> None:
        """置き場を閉じて、開く前にいたフォルダへ戻る。"""
        back = self.stash_from
        if back:
            # 後始末(フラグを落とす・地の色を戻す)は load 側に任せる。ここで
            # 先にフラグを落とすと、load の「置き場を終える」分岐(was_stash)が
            # 素通りして、地の色が緑のまま残る。N でフォルダへ移る経路は
            # 直に load を呼ぶので直っていたが、T で戻る経路はここを通る。
            self.load(back, history=None)
        else:
            self.stash = False
            self.stash_from = None
            self.apply_style()

    # --- フラット表示(Alt+B) ---
    def _stop_flat(self) -> None:
        if self.flat_cancel is not None:
            self.flat_cancel.set()
        self.flat_cancel = None
        self.flat = False
        self.flat_loading = False

    def load_flat(self, keep_marks: bool = False) -> None:
        """いまのフォルダより下のファイルを別スレッドで集めて1つの一覧にする。
        集めている間は窓の下の行に件数を出し、Esc で中止できる(cancel_flat)。"""
        self._stop_es()
        if self.flat_cancel is not None:
            self.flat_cancel.set()
        self.request_id += 1
        request = self.request_id
        cancel = threading.Event()
        self.flat_cancel = cancel
        self.flat_loading = True
        self.loading = True
        self.flat_count = 0
        root = self.path
        win = self.window_
        args = dict(path=root, flat=True, keep_marks=keep_marks)
        bridge = win.bridge
        show_hidden, show_system = win.show_hidden, win.show_system

        def progress(count):
            self.flat_count = count

        def worker():
            try:
                entries, status = core.collect_flat(root, show_hidden, show_system,
                                                    win.flat_limit, cancel, progress)
                payload = (self.side, request, True, (entries, status), args)
            except Exception as e:  # noqa: BLE001
                payload = (self.side, request, False, f"{type(e).__name__}: {e}", args)
            try:
                bridge.finished.emit(payload)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name=f"filer-flat-{self.side}").start()
        self._flat_timer.start(200)
        self.update_header()
        self.update_footer()

    def cancel_flat(self) -> None:
        if self.flat_loading and self.flat_cancel is not None:
            self.flat_cancel.set()

    def _tick_flat(self) -> None:
        try:
            if self.flat_loading or self.grep_loading or self.es_loading:
                self.update_footer()
            else:
                self._flat_timer.stop()
        except Exception:  # noqa: BLE001
            _log_exception("flatTick")

    def on_flat_loaded(self, request, ok, result, args) -> None:
        if request != self.request_id:
            return
        self.loading = False
        self.flat_loading = False
        self.flat_cancel = None
        self._flat_timer.stop()
        if not ok:
            self.window_.log(f"フラット表示: 集められません: {args['path']}  ({result})")
            self.flat = False
            self.update_header()
            self.update_footer()
            return
        entries, status = result
        if status == "cancel":
            self.window_.log("フラット表示: 中止しました")
            self.flat = False
            self.reload()
            return
        if status == "limit":
            self.window_.log(f"フラット表示: {len(entries):,}件で打ち切りました（多すぎるため）")
        if not args.get("keep_marks"):
            self.marks = set()
        self.flat = True
        self.all_entries = entries
        self._rebuild()
        self.set_cursor(0)
        self.update_header()
        self.update_footer()
        if self.is_active():
            self.window_.update_status_bar()

    # --- 文字列検索(G) ---
    def _stop_grep(self) -> None:
        if self.grep_cancel is not None:
            self.grep_cancel.set()
        self.grep_cancel = None
        self.grep_query = None
        self.grep_matches = {}
        self.grep_loading = False

    def load_grep(self, query: str, case_sensitive: bool = False,
                  keep_marks: bool = False) -> None:
        """いまのフォルダより下のファイルの中身から query を別スレッドで探し、
        ヒットしたファイルをフラット表示の形(相対パス名)で並べる。
        探している間は窓の下の行に走査数を出し、Esc で中止できる(cancel_grep)。"""
        self._stop_flat()
        self._stop_es()
        if self.grep_cancel is not None:
            self.grep_cancel.set()
        self.request_id += 1
        request = self.request_id
        cancel = threading.Event()
        self.grep_cancel = cancel
        self.grep_query = query
        self.grep_case = case_sensitive
        self.grep_loading = True
        self.loading = True
        self.grep_count = 0
        root = self.path
        win = self.window_
        args = dict(path=root, grep=query, keep_marks=keep_marks)
        bridge = win.bridge
        show_hidden, show_system = win.show_hidden, win.show_system

        def progress(count):
            self.grep_count = count

        def worker():
            try:
                entries, matches, status = core.grep_files(
                    root, query, case_sensitive, show_hidden, show_system,
                    core.GREP_LIMIT, cancel, progress)
                payload = (self.side, request, True, (entries, matches, status), args)
            except Exception as e:  # noqa: BLE001
                payload = (self.side, request, False, f"{type(e).__name__}: {e}", args)
            try:
                bridge.finished.emit(payload)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name=f"filer-grep-{self.side}").start()
        self._flat_timer.start(200)
        self.update_header()
        self.update_footer()

    def cancel_grep(self) -> None:
        if self.grep_loading and self.grep_cancel is not None:
            self.grep_cancel.set()

    def on_grep_loaded(self, request, ok, result, args) -> None:
        if request != self.request_id:
            return
        self.loading = False
        self.grep_loading = False
        self.grep_cancel = None
        self._flat_timer.stop()
        query = args["grep"]
        if not ok:
            self.window_.log(f"文字列検索: 探せません: {args['path']}  ({result})")
            self._stop_grep()
            self.update_header()
            self.update_footer()
            return
        entries, matches, status = result
        if status == "cancel":
            self.window_.log("文字列検索: 中止しました")
            self._stop_grep()
            self.reload()
            return
        if status == "limit":
            self.window_.log(f"文字列検索: {len(entries):,}件で打ち切りました（多すぎるため）")
        if not args.get("keep_marks"):
            self.marks = set()
        self.flat = True
        self.grep_matches = matches
        self.all_entries = entries
        self._rebuild()
        self.set_cursor(0)
        self.update_header()
        self.update_footer()
        if self.is_active():
            self.window_.update_status_bar()
        lines = sum(c for c, _ in matches.values())
        self.window_.log(f"文字列検索: 「{query}」 {len(entries)}件 / {lines:,}行ヒット")
        for e in entries[:50]:
            count, first = matches.get(e.name, (0, ""))
            if first:
                self.window_.log(f"  {e.name}（{count}行）: {first}")
        if len(entries) > 50:
            self.window_.log(f"  …ほか {len(entries) - 50}件")

    # --- Everything 検索(Shift+F) ---
    def _stop_es(self) -> None:
        if self.es_cancel is not None:
            self.es_cancel.set()
        self.es_cancel = None
        self.es_query = None
        self.es_loading = False
        self.es_status = "done"

    def load_everything(self, query: str, scope_all: bool = False,
                        keep_marks: bool = False) -> None:
        """es.exe で探し、結果をフラット表示の形で並べる(名前はこの窓のフォルダからの
        相対パス。全体から探したときに外のものはフルパス)。es.exe は別スレッドで待つ
        (UI を固めない)。Esc で中止できる(cancel_es)。"""
        win = self.window_
        es_argv = win.find_es()
        if es_argv is None:
            win.log(f"Everything検索: {filer_everything.NOT_FOUND_MESSAGE}")
            return
        self._stop_flat()
        self._stop_grep()
        if self.es_cancel is not None:
            self.es_cancel.set()
        self.request_id += 1
        request = self.request_id
        cancel = threading.Event()
        self.es_cancel = cancel
        self.es_query = query
        self.es_all = bool(scope_all)
        self.es_loading = True
        self.loading = True
        root = self.path
        args = dict(path=root, everything=query, scope_all=self.es_all, keep_marks=keep_marks)
        bridge = win.bridge
        limit, timeout = win.es_limit, win.es_timeout

        def worker():
            try:
                found = filer_everything.run_search(
                    es_argv, query, None if scope_all else root, limit, timeout, cancel)
                entries, missing = filer_everything.to_entries(found.paths, root)
                payload = (self.side, request, True, (found, entries, missing), args)
            except Exception as e:  # noqa: BLE001
                payload = (self.side, request, False, f"{type(e).__name__}: {e}", args)
            try:
                bridge.finished.emit(payload)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name=f"filer-es-{self.side}").start()
        self._flat_timer.start(200)
        self.update_header()
        self.update_footer()

    def cancel_es(self) -> None:
        if self.es_loading and self.es_cancel is not None:
            self.es_cancel.set()

    def on_es_loaded(self, request, ok, result, args) -> None:
        if request != self.request_id:
            return
        self.loading = False
        self.es_loading = False
        self.es_cancel = None
        self._flat_timer.stop()
        query = args["everything"]
        found = entries = None
        missing = 0
        if ok:
            found, entries, missing = result
        if not ok or found.status in ("error", "timeout", "cancel"):
            if not ok:
                self.window_.log(f"Everything検索: 探せません  ({result})")
            elif found.status == "cancel":
                self.window_.log("Everything検索: 中止しました")
            else:
                self.window_.log(f"Everything検索: {found.message}")
            # 通常の表示へ戻す(前の検索結果が出ていたら、それも片付ける)。
            self._stop_es()
            self.load(args["path"], history=None)
            return
        if not args.get("keep_marks"):
            self.marks = set()
        self.flat = True
        self.es_status = found.status
        self.all_entries = entries
        self._rebuild()
        self.set_cursor(0)
        self.update_header()
        self.update_footer()
        if self.is_active():
            self.window_.update_status_bar()
        scope = "全体" if args["scope_all"] else args["path"]
        text = f"Everything検索: 「{query}」 {len(entries)}件（範囲: {scope}）"
        if found.status == "limit":
            text += f"  ※{self.window_.es_limit:,}件で打ち切りました（語を足して絞ってください）"
        if missing:
            text += f"  ※索引にあって実在しない {missing}件は除きました"
        self.window_.log(text)

    def refilter(self) -> None:
        """表示の絞り込み(Shift+Z)が変わったときに並べ直す。カーソルは同じ項目に付いて
        いき、その項目が消えたら同じ行の辺りに留まる。マークは見えているものだけ残る。"""
        if not self.path:
            self.update_header()
            return
        entry = self.cursor_entry()
        name = entry.name if entry is not None else None
        row = self.cursor_row()
        self._rebuild()
        target = self._row_of(name)
        self.set_cursor(target if target is not None else row)
        self.update_header()
        self.update_footer()

    def set_sort(self, spec) -> None:
        """並べ方を変えて並べ直す。カーソルは同じ項目に付いていく。マークはそのまま。"""
        self.sort = core.normalize_sort(spec)
        entry = self.cursor_entry()
        name = entry.name if entry is not None else None
        if self.path:
            self._rebuild()
            row = self._row_of(name)
            self.set_cursor(row if row is not None else 0)
        self.update_header()
        self.update_footer()

    # --- ライブ絞り込み(/) ---
    def open_live_filter(self) -> None:
        """入力欄を出す。打つたびに一覧を絞る(Enter で確定、Esc で解除)。"""
        self.filter_edit.blockSignals(True)
        self.filter_edit.setText(self.live_filter)
        self.filter_edit.blockSignals(False)
        self.filter_edit.show()
        self.filter_edit.setFocus()
        self.filter_edit.end(False)

    def _on_filter_text(self, text: str) -> None:
        # 少ない一覧ならすぐ、多いときは打ち終わりを少し待ってから絞る(数万件で打鍵ごとに
        # 並べ直すと、打つのが追いつかなくなるため)。
        self._pending_filter = text
        if len(self.all_entries) < LIVE_FILTER_IMMEDIATE:
            self._apply_pending_filter()
        else:
            self._filter_timer.start(LIVE_FILTER_DELAY_MS)

    def _apply_pending_filter(self) -> None:
        try:
            self.set_live_filter(self._pending_filter)
        except Exception:  # noqa: BLE001
            _log_exception("liveFilter")

    def set_live_filter(self, text: str) -> None:
        self.live_filter = (text or "").strip()
        self.live_words = core.parse_live_filter(self.live_filter)
        if self.path:
            self.refilter()
        else:
            self.update_header()

    def close_live_filter(self, keep: bool) -> None:
        """入力欄を閉じる。keep なら絞ったまま(確定)、でなければ解除。"""
        self._filter_timer.stop()
        if keep:
            self.set_live_filter(self.filter_edit.text())
        else:
            self.set_live_filter("")
        self.filter_edit.hide()
        self.view.setFocus()

    def clear_live_filter(self, rebuild: bool = True) -> None:
        self._filter_timer.stop()
        self.filter_edit.hide()
        if not self.live_filter:
            return
        self.live_filter = ""
        self.live_words = []
        if rebuild and self.path:
            self.refilter()

    def _filter_key(self, event) -> bool:
        """入力欄の Enter / Esc。"""
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.close_live_filter(keep=True)
            return True
        if key == Qt.Key_Escape:
            self.close_live_filter(keep=False)
            return True
        return False

    def eventFilter(self, obj, event):
        try:
            if obj is self.filter_edit and event.type() == QEvent.KeyPress:
                if self._filter_key(event):
                    return True
        except Exception:  # noqa: BLE001  スロットで投げ切ると落ちる
            _log_exception("filterKey")
            return True
        return super().eventFilter(obj, event)

    def set_mask(self, text: str) -> None:
        self.mask_text = text if not core.is_default_mask(text) else core.DEFAULT_MASK
        self.patterns = core.parse_mask(self.mask_text)
        entry = self.cursor_entry()
        name = entry.name if entry is not None else None
        self.marks = set()
        if self.path:
            self._rebuild()
            row = self._row_of(name)
            self.set_cursor(row if row is not None else 0)
        self.update_header()
        self.update_footer()


# ---------------------------------------------------------------
# 窓
# ---------------------------------------------------------------
class _ChooserMenu(QMenu):
    """文字キーは項目を選ぶだけで、実行は Enter(とクリック)に限るメニュー。

    QMenu の既定では & の付いた文字キーを押した時点で項目が実行される(あふｗの
    &MENU と同じ即決)。それだと C → P のつもりが選択のつもりでもパスコピーが
    走ってしまうので、文字キーは「その項目を選ぶ」までに止める(2026-10-26
    ユーザー要望)。Enter・クリックは従来どおり実行、矢印・Esc も従来どおり。

    deletable を立てたメニューでは Del で「選んでいる項目の削除」を頼める(Alt+J の
    ワークスペース)。その場合は何も実行せずに閉じ、delete_action に項目を残す。"""

    deletable = False
    delete_action = None

    def keyPressEvent(self, event):  # noqa: N802 (Qt の上書き)
        if self.deletable and event.key() == Qt.Key_Delete:
            current = self.activeAction()
            if current is not None and current.isEnabled() and not current.isSeparator():
                self.delete_action = current
                self.close()
            event.accept()
            return
        text = event.text()
        modifiers = event.modifiers()
        plain = not (modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier))
        if plain and len(text) == 1 and text.isalnum():
            char = text.upper()
            matches = [a for a in self.actions()
                       if a.isEnabled() and not a.isSeparator() and _menu_key_of(a) == char]
            if matches:
                # 同じ文字の項目が複数あれば、押すたびに次へ(Ctrl+Enter のメニューは
                # 共有分とこの PC の分で文字が重なることがある)。
                # 最初の1回は先頭の項目(開いた直後に先頭が選ばれていても、同じ文字の
                # 1つ目へ)。同じ文字を続けて押したときだけ次へ進む。
                current = self.activeAction()
                repeat = getattr(self, "_last_char", None) == char and current in matches
                index = matches.index(current) + 1 if repeat else 0
                self.setActiveAction(matches[index % len(matches)])
            self._last_char = char
            event.accept()  # 対応する項目が無い文字も、実行には使わない
            return
        super().keyPressEvent(event)


def _menu_key_of(action) -> str:
    """メニュー項目の選択キー(大文字1字)。build_menu が付けた property を優先し、
    無ければ表示テキストの & の次の字から取る。"""
    stored = action.property("filer_menu_key")
    if isinstance(stored, str) and stored:
        return stored.upper()
    text = action.text()
    if "&" in text:
        rest = text.split("&", 1)[1]
        return rest[:1].upper() if rest else ""
    return text[:1].upper() if text else ""


class FilerWindow(QWidget):
    def __init__(self, app_settings=None, settings_path=None):
        super().__init__()
        self.app_settings = app_settings if app_settings is not None else {}
        self.settings_path = settings_path
        section = _section(self.app_settings)
        self.colors = dict(DEFAULT_COLORS)
        user_colors = section.get("colors")
        if isinstance(user_colors, dict):
            self.colors.update({k: v for k, v in user_colors.items()
                                if k in DEFAULT_COLORS and isinstance(v, str)})
        self.keymap = core.build_keymap(section.get("keys"))
        self.disabled_keys = core.disabled_codes(section.get("keys"))
        self.editor = section.get("editor") or DEFAULT_EDITOR
        self.shell = section.get("shell") or DEFAULT_SHELL
        # Enter でビュアに回す拡張子(あふｗの拡張子判別実行)。開いているビュアの窓。
        self.viewer_extensions = core.normalize_viewer_extensions(
            section.get("viewer_extensions"))
        self._viewers = []
        # Alt+W の WinMerge。空なら App Paths と既定の場所を探す。
        self.winmerge_setting = section.get("winmerge") or ""
        # フラット表示で集める上限(テストでは小さくする)。
        self.flat_limit = core.FLAT_LIMIT
        # Everything 検索(Shift+F)。件数の上限と es.exe を待つ秒数、前回の範囲(全体か)。
        try:
            self.es_limit = max(1, int(section.get("everything_limit") or filer_everything.ES_LIMIT))
        except (TypeError, ValueError):
            self.es_limit = filer_everything.ES_LIMIT
        try:
            self.es_timeout = max(1.0, float(section.get("everything_timeout_seconds")
                                             or filer_everything.ES_TIMEOUT))
        except (TypeError, ValueError):
            self.es_timeout = filer_everything.ES_TIMEOUT
        self.es_scope_all = False
        self.es_environ = None      # None なら os.environ(テストで差し替える)
        # フォルダ構成をコピー(C→I)の深さと行数の上限。
        try:
            self.tree_depth = max(1, int(section.get("tree_depth") or core.TREE_DEPTH))
        except (TypeError, ValueError):
            self.tree_depth = core.TREE_DEPTH
        try:
            self.tree_lines = max(2, int(section.get("tree_lines") or core.TREE_LINES))
        except (TypeError, ValueError):
            self.tree_lines = core.TREE_LINES
        # 拡張子別表示色(Z の設定ダイアログで変えられる)。
        self.ext_rules = core.normalize_ext_colors(section.get("ext_colors"))
        self.ext_color_map = core.ext_color_map(self.ext_rules)
        # よく行くフォルダの記録(リポジトリの外。形の違う行は捨てる)。
        stored = core.load_store(core.FRECENCY_STORE, {})
        self.frecency = {p: v for p, v in stored.items()
                         if isinstance(p, str) and isinstance(v, list) and len(v) == 2}
        # ドライブのラベルと容量(窓の2行目と下の行)。読み取りは別スレッドで、結果を
        # ドライブの根ごとに覚える。ラベルはドライブが変わったときだけ、容量は読み直しの
        # たびに読む。読めないうちは空のまま(固まらない方を取る)。
        self._drive_labels = {}
        self._drive_spaces = {}
        self._drive_pending = set()
        # Fキー一覧とステータスバー。
        self.fkey_overrides = section.get("fkey_labels") if isinstance(
            section.get("fkey_labels"), dict) else {}
        self.show_fkey_bar = section.get("show_fkey_bar") is not False
        self._fkey_mods = 0
        self.status_state = "Ready."
        try:
            self.list_timeout = max(0.5, float(section.get("list_timeout_seconds")
                                               or DEFAULT_LIST_TIMEOUT))
        except (TypeError, ValueError):
            self.list_timeout = DEFAULT_LIST_TIMEOUT
        try:
            self.archive_timeout = max(1.0, float(section.get("archive_timeout_seconds")
                                                  or filer_archive.ARCHIVE_LIST_TIMEOUT))
        except (TypeError, ValueError):
            self.archive_timeout = filer_archive.ARCHIVE_LIST_TIMEOUT
        recent = section.get("recent")
        self.recent = [p for p in recent if isinstance(p, str)] if isinstance(recent, list) else []
        # スマホへ送る(QR)は「環境1」の PC だけ(setup.py の環境選択が
        # settings.json の environment に書く)。既定は無効で、メニューにも出ない。
        self.phone_enabled = self.app_settings.get("environment") == 1
        # 一時置き場(T)に登録されたパスの一覧。filer.stash に保存する。
        stash = section.get("stash")
        self.stash_paths = ([os.path.normpath(p) for p in stash if isinstance(p, str)]
                            if isinstance(stash, list) else [])
        # 窓の配分(Alt+←→ / Alt+↑↓ / 境界のドラッグ)。閉じるときに保存する。
        self.split_ratio = core.clamp_split(section.get("split_ratio", core.DEFAULT_SPLIT_RATIO))
        self.log_lines = core.clamp_log_lines(section.get("log_lines", core.DEFAULT_LOG_LINES), 99)
        # ネットワークのフォルダの自動監視は間隔を空ける(2秒ごとの走査が
        # 通信を出し続けるため)。filer.watch_network_seconds で変更可。
        try:
            self.watch_network_seconds = min(
                120.0, max(2.0, float(section.get("watch_network_seconds") or 10.0)))
        except (TypeError, ValueError):
            self.watch_network_seconds = 10.0
        self._watch_network_at = {}   # side -> 最後にネットワーク側を見た時刻
        self._network_drive_cache = {}  # ドライブの根 -> ネットワークかどうか
        # 書庫の展開中(進捗の窓が出ている間)。重ねて始めない。
        self._extracting = None
        # Shift+Z の「隠しファイル / システムファイルを表示する」。既定はどちらも表示。
        self.show_hidden = section.get("show_hidden") is not False
        self.show_system = section.get("show_system") is not False
        # テストのときだけ SHFileOperation に足すフラグ(FOF_NOCONFIRMATION 等)。本番は 0。
        self.extra_op_flags = 0
        self._picker = None
        self._server = None
        # Ctrl+Enter の拡張子ごとのメニュー。ツールの場所は PC ごとの filer.tools。
        # 共有のメニューの置き場・この PC の分の置き場・exe の探し方はテストで差し替える。
        tools = section.get("tools")
        self.tools = dict(tools) if isinstance(tools, dict) else {}
        self.menu_shared_dir = filer_extmenu.MENU_DIR
        self.menu_local_dir = None          # None なら LOCALAPPDATA の traytools/menus
        self.menu_app_path = filer_extmenu.registry_app_path
        self.menu_which = filer_extmenu.shutil.which
        self.menu_exists = os.path.isfile
        # 版(タイトルに出す)。起動時に1回だけ読む。Shift+F5 の更新は別スレッドで。
        # まず .git を読むだけの速い方(日付だけ。ZIP 展開なら「ZIP 日付」)で出し、
        # 連番は git コマンドで別スレッドで数えて差し替える(_start_version_lookup)。
        # git が遅い・固まる PC でも起動を待たせないため。
        self.repo_dir = filer_update.REPO_DIR
        self.version_info = filer_update.quick_version(self.repo_dir)
        self.version = self.version_info["label"]
        self._update_busy = False
        self._restarting = False

        families = section.get("font_families")
        if not (isinstance(families, list) and families):
            families = DEFAULT_FONT_FAMILIES
        self.list_font = QFont()
        self.list_font.setFamilies([str(f) for f in families])
        self.list_font.setStyleHint(QFont.Monospace)
        self.list_font.setPointSize(core.clamp_font_size(section.get("font_size")
                                                         or DEFAULT_FONT_SIZE))
        # 一覧の字の大きさは左右の窓ごとに持つ(あふｗと同じく Shift/Ctrl+↑↓ は
        # アクティブ窓だけに効く)。窓ごとの値が無ければ、以前の共通の font_size を使う。
        # list_font は大きさの基準と、ダイアログ・メッセージ窓の字として残す。
        self.pane_font_sizes = {
            side: core.clamp_font_size(section.get(f"{side}_font_size")
                                       or self.list_font.pointSize())
            for side in ("left", "right")}
        # 一覧以外の字(メッセージ窓・パス表示・ダイアログ等)は 10pt 固定。一覧の
        # 拡大縮小(Shift/Ctrl+↑↓)にも、設定の font_size にも付いていかない
        # (2026-10-29 要望)。
        self.log_font = QFont(self.list_font)
        self.log_font.setPointSize(10)

        self.setWindowTitle(filer_update.window_title("", self.version))
        # アプリ既定(main で設定)に加え窓自体にも付ける。タスクバーが
        # 実行ファイル側の汎用アイコンを拾う環境でも窓のアイコンが勝つ。
        self.setWindowIcon(affx_icon.qicon())
        self.setObjectName("filerWindow")
        self.setStyleSheet(f"#filerWindow {{ background-color: {self.colors['background']}; }}")
        self.active_side = section.get("active") if section.get("active") in ("left", "right") else "left"

        self.bridge = ListBridge(self)
        self.bridge.finished.connect(self._on_list_finished)
        self.bridge.progress.connect(self._on_extract_progress)
        self.bridge.extracted.connect(self._on_extracted)
        self.bridge.context_done.connect(self._on_context_done)
        self.bridge.drive_info.connect(self._on_drive_info)
        self.bridge.preview_ready.connect(self._on_preview_ready)
        self.bridge.hashed.connect(self._on_hashed)
        self._hash_busy = False
        self.bridge.tree_built.connect(self._on_tree_built)
        self._tree_busy = False
        # ファイル転送のキュー(_transfer 参照)。実行中の op と待ちの列。
        self.bridge.op_done.connect(self._on_op_done)
        self._op_queue = []
        self._op_running = None
        # フォルダの自動監視。表示中のフォルダの中身が外部で変わったら再読込する。
        # 指紋の走査は別スレッド、判定と再読込はここ(UIスレッド)。
        self.bridge.folder_watch.connect(self._on_folder_watch)
        self.bridge.update_checked.connect(self._on_update_checked)
        self.bridge.update_pulled.connect(self._on_update_pulled)
        self.bridge.version_ready.connect(self._on_version_ready)
        self._watch_checking = False
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(WATCH_INTERVAL_MS)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_timer.start()
        # プレビュー(F3)。ON の間は反対窓の場所にプレビューを出す。
        self.preview_on = False
        self.preview_side = None
        self.preview_widget = None
        self._preview_fullscreen = False    # 右窓から入ったプレビューはフルスクリーン
        self._preview_was_maximized = False
        # プレビューの読み込み。要求はここへ積んで1つのスレッドが読む。新しい要求が
        # 来たら、まだ読み始めていない古い要求は捨てる(最新だけ出す)。世代は
        # プレビューを閉じたときに進め、古い結果を捨てる印にする。
        self._preview_queue = []
        self._preview_lock = threading.Lock()
        self._preview_worker_on = False
        self._preview_generation = 0
        # 画像の読み込み中(まだ表示に回っていない)か。読み込み中はキーのオートリピートに
        # よるカーソル移動を捨て、長押しの速さを画像を出せる速さに合わせる
        # (_drops_repeat)。要求ごとの通し番号で、最後に積んだ画像が出たら解く。
        self._preview_seq = 0
        self._image_wait_seq = None         # 待っている画像の要求の番号(None なら待ちなし)
        self._image_wait_since = 0.0
        # 画像ビュアの手動の回転・反転と回転固定(R / L / H / V、Shift+R / Shift+L)
        self.image_turn = core.ImageTurn()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._start_preview)

        # 左右の窓とメッセージ窓は QSplitter で区切る。境界はマウスでも動かせ、
        # Alt+矢印(あふｗと同じ)でも動かせる。どちらで動かしても同じ値を保存する。
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(0)
        self.vsplit = QSplitter(Qt.Vertical, self)
        self.hsplit = QSplitter(Qt.Horizontal, self.vsplit)
        # 境界は窓の端まで寄せられる(片方を畳み切るのも可。2026-10-29 要望)。
        # False だと各窓の最小サイズで止まってしまい、端まで寄せられない。
        self.hsplit.setChildrenCollapsible(True)
        self.vsplit.setChildrenCollapsible(False)
        self.hsplit.setHandleWidth(4)
        self.vsplit.setHandleWidth(4)
        self.left = Pane(self, "left")
        self.right = Pane(self, "right")
        self.hsplit.addWidget(self.left)
        self.hsplit.addWidget(self.right)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFocusPolicy(Qt.NoFocus)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFont(self.log_font)
        self.log_view.setStyleSheet(
            f"background-color: {self.colors['log_background']}; color: {self.colors['log_text']};"
            " border: none;")
        self.log_view.setMinimumHeight(self._log_height(core.LOG_LINES_MIN))
        self.vsplit.addWidget(self.hsplit)
        self.vsplit.addWidget(self.log_view)
        # 窓の大きさを変えたときは、増えた分をファイル窓に回す(メッセージ窓は行数を保つ)。
        self.vsplit.setStretchFactor(0, 1)
        self.vsplit.setStretchFactor(1, 0)
        self.hsplit.splitterMoved.connect(self._on_hsplit_moved)
        self.vsplit.splitterMoved.connect(self._on_vsplit_moved)
        # 左右の仕切りのダブルクリックで中央へ戻す(eventFilter 参照)。
        self.hsplit.handle(1).installEventFilter(self)
        root.addWidget(self.vsplit)

        # ファンクションキーの段(F1〜F12。押したのと同じコマンドを走らせるボタン)と、
        # 一番下のステータスバー。字はメッセージ窓と同じ基準の大きさ。
        self.fkey_bar = QWidget(self)
        fkey_layout = QHBoxLayout(self.fkey_bar)
        fkey_layout.setContentsMargins(0, 0, 0, 0)
        fkey_layout.setSpacing(1)
        self.fkey_buttons = []
        c = self.colors
        for number in range(1, core.FKEY_COUNT + 1):
            button = QPushButton(self.fkey_bar)
            button.setFocusPolicy(Qt.NoFocus)   # ファイル窓からフォーカスを奪わない
            button.setFont(self.log_font)
            button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            button.setFixedHeight(QFontMetrics(self.log_font).height() + 2)
            button.setStyleSheet(
                f"QPushButton {{ background-color: {c['fkey_background']}; color: {c['fkey_text']};"
                f" border: 1px solid {c['fkey_border']}; padding: 0px; }}")
            button.clicked.connect(lambda _checked=False, n=number: self.press_fkey(n))
            fkey_layout.addWidget(button, 1)
            self.fkey_buttons.append(button)
        root.addWidget(self.fkey_bar)
        self.fkey_bar.setVisible(self.show_fkey_bar)

        self.status_bar = QWidget(self)
        self.status_bar.setObjectName("filerStatus")
        self.status_bar.setStyleSheet(
            f"#filerStatus {{ background-color: {c['status_background']}; }}"
            f" #filerStatus QLabel {{ color: {c['status_text']}; }}")
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(4, 0, 4, 0)
        status_layout.setSpacing(12)
        self.status_labels = []
        # 状態 / 名前 / サイズ / 日時 / 属性。名前だけが伸び、ほかは字の幅ぶん。
        for stretch, align in ((2, Qt.AlignLeft), (5, Qt.AlignLeft), (2, Qt.AlignRight),
                               (2, Qt.AlignLeft), (1, Qt.AlignLeft)):
            label = QLabel(self.status_bar)
            label.setTextFormat(Qt.PlainText)
            label.setFont(self.log_font)
            label.setAlignment(align | Qt.AlignVCenter)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            label.setFixedHeight(QFontMetrics(self.log_font).height())
            status_layout.addWidget(label, stretch)
            self.status_labels.append(label)
        root.addWidget(self.status_bar)
        self.update_fkey_bar(Qt.NoModifier)

        self._restore_geometry(section)
        for pane in (self.left, self.right):
            pane.apply_style()
        self.update_status_bar()
        self._start_version_lookup()    # メッセージ窓ができてから(ログに版を出す)

    # --- 窓の位置と前回のパス ---
    def _restore_geometry(self, section) -> None:
        geometry = section.get("geometry")
        restored = False
        if isinstance(geometry, list) and len(geometry) == 4:
            try:
                x, y, w, h = (int(v) for v in geometry)
                rect = QRect(x, y, max(w, 400), max(h, 300))
                # 前回と画面構成が違う(モニタを外した等)なら、見えない所に出さない。
                if any(s.availableGeometry().intersects(rect) for s in QGuiApplication.screens()):
                    self.setGeometry(rect)
                    restored = True
            except (TypeError, ValueError):
                pass
        if not restored:
            screen = QGuiApplication.primaryScreen()
            area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
            w, h = min(1100, area.width() - 40), min(700, area.height() - 40)
            self.setGeometry(area.x() + (area.width() - w) // 2,
                             area.y() + (area.height() - h) // 2, w, h)
        self._start_maximized = bool(section.get("maximized"))

    # --- 窓の配分(左右の境界とメッセージ窓の高さ) ---
    def _log_height(self, lines: int) -> int:
        return QFontMetrics(self.log_font).lineSpacing() * lines + 10

    def _max_log_lines(self) -> int:
        """メッセージ窓の上限。窓全体の半分ほどまで。"""
        total = sum(self.vsplit.sizes()) or self.vsplit.height() or self.height()
        spacing = max(1, QFontMetrics(self.log_font).lineSpacing())
        return max(core.LOG_LINES_MIN, (total // 2 - 10) // spacing)

    def apply_layout(self) -> None:
        """split_ratio と log_lines を splitter に反映する(setSizes は splitterMoved を出さない)。"""
        width = sum(self.hsplit.sizes()) or self.hsplit.width()
        if width > 0:
            left = int(round(width * self.split_ratio))
            self.hsplit.setSizes([left, width - left])
        total = sum(self.vsplit.sizes()) or self.vsplit.height()
        if total > 0:
            self.log_lines = core.clamp_log_lines(self.log_lines, self._max_log_lines())
            log_height = min(self._log_height(self.log_lines), total - 50)
            self.vsplit.setSizes([total - log_height, log_height])
        self._ensure_active_visible()

    # --- 畳まれた窓の扱い ---
    def pane_collapsed(self, side: str) -> bool:
        """その窓が境界を端まで寄せて畳まれているか。

        まだ並べ終わっていない(左右とも幅 0)ときや、プレビューで窓が仕切りから
        外れているときは「畳まれていない」と答える(起動直後に中央へ戻したり、
        アクティブを動かしたりしないように)。"""
        sizes = self.hsplit.sizes()
        if len(sizes) < 2 or sum(sizes) <= 0:
            return False
        index = self.hsplit.indexOf(self.pane(side))
        if index < 0:
            return False
        return sizes[index] <= COLLAPSED_PANE_PX

    def _ensure_active_visible(self) -> None:
        """アクティブの窓が畳まれたら、見えている方の窓へアクティブを移す
        (あふｗの &VSPMV L/R が端へ寄せるときにカーソルを反対側へ移すのと同じ考え方)。"""
        if getattr(self, "preview_on", False) or not hasattr(self, "left"):
            return
        side = self.active_side
        other = "right" if side == "left" else "left"
        if self.pane_collapsed(side) and not self.pane_collapsed(other):
            self.activate(other)

    def center_split(self) -> None:
        """左右の境界を中央へ戻す(仕切りのダブルクリック、畳まれた窓へ移るとき)。"""
        self.split_ratio = core.DEFAULT_SPLIT_RATIO
        self.apply_layout()

    def eventFilter(self, obj, event):
        try:
            if (event.type() == QEvent.MouseButtonDblClick
                    and obj is self.hsplit.handle(1)):
                self.center_split()
                return True
        except Exception:  # noqa: BLE001  スロット内で投げ切るとプロセスごと落ちる
            _log_exception("splitHandle")
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        try:
            self.apply_layout()
        except Exception:  # noqa: BLE001
            _log_exception("resize")

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_layout_safely)

    def _apply_layout_safely(self) -> None:
        try:
            self.apply_layout()
        except Exception:  # noqa: BLE001
            _log_exception("layout")

    def _on_hsplit_moved(self, _pos, _index) -> None:
        try:
            sizes = self.hsplit.sizes()
            if sum(sizes) > 0:
                self.split_ratio = core.clamp_split(sizes[0] / sum(sizes))
            self._ensure_active_visible()   # マウスで畳んだときも見える側へ
        except Exception:  # noqa: BLE001
            _log_exception("hsplit")

    def _on_vsplit_moved(self, _pos, _index) -> None:
        try:
            log_height = self.vsplit.sizes()[1]
            spacing = max(1, QFontMetrics(self.log_font).lineSpacing())
            self.log_lines = core.clamp_log_lines(round((log_height - 10) / spacing),
                                                  self._max_log_lines())
        except Exception:  # noqa: BLE001
            _log_exception("vsplit")

    def start(self) -> None:
        """前回のパスで両窓を開く。無くなっていれば残っている親まで遡り、全部だめなら
        ユーザープロファイル(それも読めなければ C:\\)。"""
        section = _section(self.app_settings)
        home = os.path.expanduser("~")
        # 前回の終わりに消せなかった一時フォルダ(開いたままだったファイル)を片付ける。
        # ファイラは1つしか起きないので、残っているものはすべて前回以前のもの。
        threading.Thread(target=filer_archive.cleanup_temp, daemon=True).start()
        for pane, key in ((self.left, "left_path"), (self.right, "right_path")):
            saved = section.get(key)
            candidates = [p for p in (saved, home, "C:\\") if isinstance(p, str) and p]
            self._load_first(pane, candidates)
        self.activate(self.active_side)
        # J(ブックマーク)の初回が重くならないよう、重いモジュールを空き時間に先読みする。
        QTimer.singleShot(1000, self._preload_modules)

    def _preload_modules(self) -> None:
        """launcher(→ explorer_nav → comtypes)と picker を先に読み込んでおく。

        ファイラ本体の起動を軽く保つため、これらは使うときに読む作りだが、
        そのままだと J を初めて押したときに読み込みの時間(特に comtypes)が
        丸ごと乗り、ピッカーの立ち上がりが引っかかる。起動して少し経った
        空き時間に読み込んでしまえば、初回の J も2回目以降と同じ速さになる。
        読み込むだけで、COM のオブジェクトは作らない。"""
        if getattr(self, "_preloaded", False):
            return
        self._preloaded = True
        try:
            import launcher  # noqa: F401
            import picker  # noqa: F401
        except Exception:  # noqa: BLE001  先読みできなくても、J のときに読み直すだけ
            _log_exception("preload")

    # --- フォルダの自動監視 ---
    def _is_network_cached(self, path) -> bool:
        """そのパスがネットワーク側かどうか(ドライブごとに覚える)。

        GetDriveTypeW を2秒ごとに呼ぶのを避けるためのキャッシュ。
        UNC はドライブを経ないので、そのまま判定する。"""
        if str(path).startswith("\\\\"):
            return True
        root = os.path.splitdrive(os.path.abspath(str(path)))[0]
        if root not in self._network_drive_cache:
            self._network_drive_cache[root] = filer_shell.is_network_path(path)
        return self._network_drive_cache[root]

    def _watch_tick(self) -> None:
        """タイマーから。監視できる状態の窓があれば、指紋を取りに別スレッドを出す。

        ここ(UIスレッド)では I/O をしない。ネットワークのフォルダが固まっても
        画面が止まらないようにするため(読み込み本体と同じ作法)。
        次のどれかなら今回は見送る: 窓が見えていない / 最小化 / モーダル表示中 /
        入力欄(名前変更・絞り込み等)にフォーカス / マウスボタンを押している
        (ドラッグ中) / 前回の走査がまだ終わっていない。"""
        try:
            if self._watch_checking or not self.isVisible() or self.isMinimized():
                return
            if QApplication.activeModalWidget() is not None:
                return
            if isinstance(QApplication.focusWidget(), QLineEdit):
                return
            if QApplication.mouseButtons() != Qt.NoButton:
                return
            targets = []
            now = time.monotonic()
            for pane in (self.left, self.right):
                if (pane.path and pane.archive is None and not pane.flat
                        and pane.grep_query is None and not pane.loading
                        and not pane.flat_loading and not pane.grep_loading):
                    if self._is_network_cached(pane.path):
                        last = self._watch_network_at.get(pane.side, 0.0)
                        if now - last < self.watch_network_seconds:
                            continue        # ネットワーク側は間隔を空ける
                        self._watch_network_at[pane.side] = now
                    targets.append((pane.side, pane.path))
            if not targets:
                return
            self._watch_checking = True
            bridge = self.bridge

            def worker():
                results = {}
                for side, path in targets:
                    if not os.path.isdir(path):
                        results[side] = (path, "gone", None)
                        continue
                    try:
                        results[side] = (path, "ok", core.folder_signature(path))
                    except OSError:
                        results[side] = (path, "error", None)
                try:
                    bridge.folder_watch.emit(results)
                except RuntimeError:
                    pass  # 窓が先に閉じた

            threading.Thread(target=worker, daemon=True, name="filer-watch").start()
        except Exception:  # noqa: BLE001
            self._watch_checking = False
            _log_exception("watch_tick")

    def _on_folder_watch(self, results) -> None:
        """監視の結果を受ける。指紋が基準と違えば、その窓だけ再読込する。

        再読込は load の「同じフォルダ」経路を通るので、カーソルは名前で戻り、
        マークは keep_marks で持ち越す。書庫・フラット・検索結果は対象外
        (フラットは配下全部が対象で重いし、頻繁に変わる。検索結果は問い合わせの
        写しなので、要れば F5 で探し直す)。"""
        self._watch_checking = False
        try:
            for pane in (self.left, self.right):
                found = results.get(pane.side)
                if found is None:
                    continue
                path, status, signature = found
                if not pane.path or not core.same_dir(pane.path, path):
                    continue  # 走査の間にフォルダを移った。結果は捨てる
                if (pane.archive is not None or pane.flat or pane.grep_query is not None
                        or pane.loading or pane.flat_loading or pane.grep_loading):
                    pane.watch_signature = None
                    continue
                if status == "gone":
                    # フォルダ自体が消えた。再読込の walk_up で親へ遡らせる。
                    pane.watch_signature = None
                    pane.reload(keep_marks=True)
                elif status == "ok":
                    if pane.watch_signature is None:
                        pane.watch_signature = signature   # 基準を取るだけ
                    elif signature != pane.watch_signature:
                        pane.watch_signature = signature
                        pane.reload(keep_marks=True)
                # "error"(読めなかった)は一時的なものとして何もしない
        except Exception:  # noqa: BLE001
            _log_exception("folder_watch")

    def _load_first(self, pane, candidates) -> None:
        if not candidates:
            return
        first, rest = candidates[0], candidates[1:]
        pane.load(first, walk_up=True, on_fail=lambda: self._load_first(pane, rest))

    def save_state(self) -> None:
        geometry = (self.normalGeometry()
                    if self.isMaximized() or self.isFullScreen() else self.geometry())
        updates = {
            "geometry": [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
            "maximized": self.isMaximized(),
            "active": self.active_side,
            "recent": self.recent[:RECENT_LIMIT],
            "left_sort": self.left.sort,
            "right_sort": self.right.sort,
            "split_ratio": self.split_ratio,
            "log_lines": self.log_lines,
            "left_font_size": self.left.pane_font.pointSize(),
            "right_font_size": self.right.pane_font.pointSize(),
        }
        if self.left.path:
            updates["left_path"] = self.left.path
        if self.right.path:
            updates["right_path"] = self.right.path
        save_values(self.app_settings, self.settings_path, updates)

    def confirm_losses(self, verb: str = "閉じますか") -> bool:
        """閉じると失われるもの(転送の実行中・待ち)があれば確かめる。閉じてよければ True。
        裏のスレッドごとプロセスが終わるので、黙って閉じると転送が途中で切れる。"""
        if getattr(self, "_op_running", None) is not None or getattr(self, "_op_queue", None):
            running = 1 if self._op_running is not None else 0
            if not self.confirm(
                    "あっふ",
                    f"ファイル転送が残っています（実行中 {running}件・"
                    f"待ち {len(self._op_queue)}件）。\n{verb}？",
                    default_yes=False):
                return False
        return True

    def closeEvent(self, event):
        # 再起動(Shift+F5)では確認も保存も済ませてある(保存を後継の起動と書き合わない)。
        restarting = getattr(self, "_restarting", False)
        if not restarting and not self.confirm_losses():
            event.ignore()
            return
        try:
            if not restarting:
                self.save_state()
            if self._picker is not None:
                self._picker.close()
            if self._extracting is not None:
                self._extracting["cancel"].set()
            for viewer in list(self._viewers):
                viewer.close()
            # 書庫の中から開いたファイルの一時フォルダ。開いたままのものは消えずに残り、
            # 次の起動で片付く。
            filer_archive.cleanup_temp()
        except Exception:  # noqa: BLE001  閉じられなくなるのが最悪
            _log_exception("close")
        super().closeEvent(event)

    # --- 窓の切り替え ---
    def pane(self, side: str) -> Pane:
        return self.left if side == "left" else self.right

    @property
    def active(self) -> Pane:
        return self.pane(self.active_side)

    @property
    def other(self) -> Pane:
        return self.pane("right" if self.active_side == "left" else "left")

    def activate(self, side: str, focus: bool = True) -> None:
        changed = side != self.active_side
        if changed and getattr(self, "preview_on", False) and side == self.preview_side:
            # プレビューを出している側へ移ろうとしたら、プレビューを終えて元の窓に戻す
            # (隠れたままの窓へキーが行くと、見えない所で操作してしまうため)。
            self.preview_off()
        if changed and self.active.isearch_text is not None:
            self.end_isearch()
        if changed and not getattr(self, "preview_on", False) and self.pane_collapsed(side):
            # 畳まれた窓へ移ろうとしたら(←→/Tab/クリック)、境界を中央へ戻してから移る。
            # 見えない窓をアクティブにしない。
            self.center_split()
        self.active_side = side
        if focus:
            self.active.view.setFocus()
        if changed or focus:
            for pane in (self.left, self.right):
                pane.apply_style()
        self._update_title()
        self.update_status_bar()

    def _update_title(self) -> None:
        path = self.active.path or ""
        self.setWindowTitle(filer_update.window_title(path, getattr(self, "version", "")))

    def on_pane_loaded(self, pane) -> None:
        self.recent = core.push_recent(self.recent, pane.path, RECENT_LIMIT)
        self._update_title()
        # 空き容量は読み直しのたび(ファイル操作の後も読み直しが走る)、ラベルは初めての
        # ドライブのときだけ読む。書庫の中は容量を出さないが、ラベルは書庫のあるドライブ。
        root = pane.drive_root()
        if root:
            self.request_drive_info(root, space=pane.archive is None)
        if getattr(pane, "last_load_moved", False) and pane.archive is None:
            self.record_visit(pane.path)
        if pane.is_active():
            self.update_status_bar()
            self.schedule_preview()

    # --- よく行くフォルダ(Shift+H) ---
    def record_visit(self, path: str) -> None:
        """フォルダを訪れたことを記録する(書庫の中は記録しない。呼ぶ側で除いてある)。
        記録はリポジトリの外(%LOCALAPPDATA%\\traytools)。書けなくても操作は止めない。"""
        try:
            core.frecency_add(self.frecency, path, time.time())
            core.save_store(core.FRECENCY_STORE, self.frecency)
        except (OSError, ValueError, TypeError) as e:
            print(f"[filer] よく行くフォルダを記録できません: {e}", file=sys.stderr)

    def _prune_missing_frecency(self) -> None:
        """ローカルの固定ドライブにあって、もう無いフォルダを記録から消す。ネットワーク
        ドライブ・UNC は確かめない(止まりうるので。移動に失敗したときに消す)。"""
        changed = False
        for path in list(self.frecency):
            root = core.drive_root(path)
            if not root or root.startswith("\\\\"):
                continue
            try:
                if filer_shell.drive_type(root) == filer_shell.DRIVE_FIXED and \
                        not os.path.isdir(path):
                    del self.frecency[path]
                    changed = True
            except OSError:
                continue
        if changed:
            try:
                core.save_store(core.FRECENCY_STORE, self.frecency)
            except OSError:
                pass

    def cmd_frecency_jump(self):
        """Shift+H: よく行くフォルダへ(zoxide 風)。打った語をすべて含むパスを、
        回数と最近さのスコア順に出す。最後の語がフォルダ名に入るものが先。"""
        self._prune_missing_frecency()
        if not self.frecency:
            self.log("よく行くフォルダの記録がまだありません")
            return

        def candidates(text):
            return [(path, path) for path in core.frecency_query(self.frecency, text, time.time())]

        def go(path):
            def failed(path=path):
                core.frecency_remove(self.frecency, path)
                try:
                    core.save_store(core.FRECENCY_STORE, self.frecency)
                except OSError:
                    pass
                self.log(f"よく行くフォルダ: 開けないので記録から消しました: {path}")
            self.active.load(path, on_fail=failed)

        self._open_picker("よく行くフォルダ", [], go,
                          placeholder="語を空白区切りで（最後の語はフォルダ名に）",
                          dynamic_items=candidates)

    # --- ドライブのラベルと容量(別スレッドで読んで覚える) ---
    def drive_label_text(self, root) -> str:
        if not root:
            return ""
        return self._drive_labels.get(os.path.normcase(root), "")

    def drive_space(self, root):
        if not root:
            return None
        return self._drive_spaces.get(os.path.normcase(root))

    def request_drive_info(self, root: str, space: bool = True) -> None:
        """ラベル(まだ知らなければ)と容量を読みに行く。読むのは別スレッドで、終わったら
        drive_info のシグナルで受け取る。同じドライブを同時に二重には読まない。
        相手が切断されたネットワークドライブで止まっても、スレッドが残るだけで窓は
        止まらない(結果が来なければ空のまま)。"""
        key = os.path.normcase(root)
        want_label = key not in self._drive_labels
        if not (want_label or space) or key in self._drive_pending:
            return
        self._drive_pending.add(key)
        bridge = self.bridge
        reader_label, reader_space = self._read_drive_label, self._read_drive_space

        def worker():
            try:
                if want_label:
                    try:
                        label = reader_label(root)
                    except Exception:  # noqa: BLE001
                        label = ""
                    bridge.drive_info.emit((key, "label", label or ""))
                if space:
                    try:
                        value = reader_space(root)
                    except Exception:  # noqa: BLE001
                        value = None
                    bridge.drive_info.emit((key, "space", value))
                bridge.drive_info.emit((key, "done", None))
            except RuntimeError:
                pass  # 窓が先に閉じた

        threading.Thread(target=worker, daemon=True, name="filer-drive").start()

    # テストではここを差し替える(実際のドライブを読まない)。
    _read_drive_label = staticmethod(filer_shell.drive_label)
    _read_drive_space = staticmethod(filer_shell.disk_space)

    def _on_drive_info(self, payload) -> None:
        try:
            key, kind, value = payload
            if kind == "label":
                self._drive_labels[key] = value
            elif kind == "space":
                if value is None:
                    self._drive_spaces.pop(key, None)
                else:
                    self._drive_spaces[key] = value
            else:
                self._drive_pending.discard(key)
                return
            for pane in (self.left, self.right):
                root = pane.drive_root()
                if root and os.path.normcase(root) == key:
                    pane.update_header()
                    pane.update_footer()
        except Exception:  # noqa: BLE001
            _log_exception("driveInfo")

    # --- ステータスバー ---
    def set_status_state(self, text: str) -> None:
        """ステータスバーの左端(処理中の内容。待機中は "Ready.")。"""
        self.status_state = text or "Ready."
        self.update_status_bar()

    def update_status_bar(self) -> None:
        if not hasattr(self, "status_labels"):
            return  # 組み立ての途中
        fields = core.status_fields(self.active.cursor_entry())
        for label, text in zip(self.status_labels, (self.status_state,) + fields):
            label.setText(text)

    # --- ファンクションキーの段 ---
    def _mods_digit(self, mods) -> int:
        digit = 0
        if mods & Qt.ShiftModifier:
            digit |= core.SHIFT
        if mods & Qt.ControlModifier:
            digit |= core.CTRL
        return digit

    def update_fkey_bar(self, mods) -> None:
        """Fキーの段の表示を、いま押している修飾(Shift / Ctrl)の割り当てに合わせる。
        Alt は切り替えない(Alt 付きの割り当ては Alt+F4 の終了だけで、押している間に
        段を書き換えても見せるものがほぼ無い。Alt はメニューの起動にも使われる)。"""
        if not hasattr(self, "fkey_buttons"):
            return
        digit = self._mods_digit(mods)
        self._fkey_mods = digit
        labels = core.fkey_labels(self.keymap, digit, self.fkey_overrides)
        for button, label in zip(self.fkey_buttons, labels):
            button.setText(label)

    def press_fkey(self, number: int) -> None:
        """Fキーの区画を押した(クリック)。そのキーを押したのと同じコマンドを走らせる。
        空の区画では何もしない。フォーカスはファイル窓に残す。"""
        try:
            command = core.fkey_command(self.keymap, self._fkey_mods, number)
            if command:
                self.run_command(command)
            self.active.view.setFocus()
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('fkey')}")

    def changeEvent(self, event):
        super().changeEvent(event)
        try:
            # 窓から離れたら Fキーの段を素の割り当てに戻す(Shift を押したまま別の窓へ
            # 移ると、離したことが届かず切り替わったままになるため)。
            if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
                self.update_fkey_bar(Qt.NoModifier)
        except Exception:  # noqa: BLE001
            _log_exception("changeEvent")

    def _on_list_finished(self, payload) -> None:
        try:
            side, request, ok, result, args = payload
            if args.get("everything") is not None:
                self.pane(side).on_es_loaded(request, ok, result, args)
            elif args.get("grep"):
                self.pane(side).on_grep_loaded(request, ok, result, args)
            elif args.get("flat"):
                self.pane(side).on_flat_loaded(request, ok, result, args)
            else:
                self.pane(side).on_loaded(request, ok, result, args)
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('list')}")

    # --- メッセージ窓 ---
    def log(self, text: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {text}")

    # --- 前面化(filer_launch からの "show") ---
    def listen(self) -> None:
        server = QLocalServer(self)
        if server.listen(filer_launch.SERVER_NAME):
            server.newConnection.connect(self._on_new_connection)
            self._server = server

    def _on_new_connection(self) -> None:
        try:
            while self._server is not None and self._server.hasPendingConnections():
                sock = self._server.nextPendingConnection()
                sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
                sock.disconnected.connect(sock.deleteLater)
                # 繋いだ時点で届いていることもある(readyRead を繋ぐ前に来た分)。
                if sock.bytesAvailable():
                    self._on_ready_read(sock)
        except Exception:  # noqa: BLE001  外から叩かれる入口。必ず受ける
            _log_exception("connection")

    def _on_ready_read(self, sock) -> None:
        try:
            data = bytes(sock.readAll())
            if b"show" in data:
                # 受け取ったことを返す。送る側はこれを待ってから切るので、
                # 読む前に切られて依頼が消える、が起きない。
                sock.write(b"ok\n")
                sock.flush()
                self.bring_to_front()
        except Exception:  # noqa: BLE001
            _log_exception("readyRead")

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        self.active.view.setFocus()
        QTimer.singleShot(0, self._ensure_foreground)

    def _ensure_foreground(self) -> None:
        """前面化ロックで activateWindow が黙って失敗したときに、もう一度だけ取りに行く
        (picker._ensure_foreground と同じ。filer_launch が前面化の権利を譲ってある)。"""
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            hwnd = int(self.winId())
            if (user32.GetForegroundWindow() or 0) != hwnd:
                user32.SetForegroundWindow(wintypes.HWND(hwnd))
        except Exception:  # noqa: BLE001
            _log_exception("foreground")

    # --- キー ---
    def handle_key(self, pane, event) -> None:
        if pane.side != self.active_side:
            self.activate(pane.side, focus=False)
        mods = event.modifiers()
        if mods & Qt.MetaModifier:
            return  # Win キーとの組み合わせは使わない
        if event.key() == Qt.Key_Escape and self.active.flat_loading:
            self.active.cancel_flat()   # Esc は「処理の中止」: フラット表示の収集を止める
            return
        if event.key() == Qt.Key_Escape and self.active.grep_loading:
            self.active.cancel_grep()   # Esc は「処理の中止」: 文字列検索を止める
            return
        if event.key() == Qt.Key_Escape and self.active.es_loading:
            self.active.cancel_es()     # Esc は「処理の中止」: Everything 検索を止める
            return
        if self.active.isearch_text is not None and self._isearch_key(event):
            return
        if event.key() == Qt.Key_Escape and self.preview_on:
            self.preview_off()      # Esc はプレビュー(F3)を抜ける
            return
        if event.key() == Qt.Key_Backspace and self.preview_on:
            self.preview_off()      # ビューア(プレビュー)中の BS もプレビューを抜ける
            return                  # (親フォルダへは行かない。2026-10-29 要望)
        if (event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.preview_on
                and not (mods & (Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier))
                and core.preview_kind(pane.cursor_entry(),
                                      self.viewer_extensions) == "image"):
            self.preview_off()      # 画像ビューア中の Enter もビューアを抜ける
            return                  # (あふｗのビューアと同じ。2026-10-29 要望)
        vk = _vk_of(event)
        shift, ctrl, alt = (bool(mods & Qt.ShiftModifier), bool(mods & Qt.ControlModifier),
                            bool(mods & Qt.AltModifier))
        # 画像ビュア(プレビュー中でカーソルが画像)のときだけ、R / L / H / V を
        # 回転・反転に使う(あふｗの画像ビュアのキー)。ファイル一覧やテキストの
        # プレビューでは従来どおり(R=改名 など)。Shift+H / Shift+V は一覧側のまま。
        if (self.preview_on and not (ctrl or alt) and vk in IMAGE_TURN_KEYS
                and (not shift or IMAGE_TURN_KEYS[vk] in ("R", "L"))
                and core.preview_kind(pane.cursor_entry(), self.viewer_extensions) == "image"):
            self.turn_image(IMAGE_TURN_KEYS[vk], shift)
            return
        # 画像の読み込み中は、オートリピート(長押し)によるカーソル移動を捨てる。
        # 長押しの速さが画像を出せる速さになり、カーソルと表示がずれない。
        if self._drops_repeat(event):
            return
        # ログ(メッセージ窓)に選択範囲があれば、Ctrl+C はその文字列のコピーにする。
        # ログはフォーカスを持たない(NoFocus)のでキーはここへ来てしまい、通常は
        # Ctrl+英字の頭文字ジャンプに取られて、選択しても写せなかった。
        if ctrl and not shift and not alt and vk == 67:  # 67 = C
            cursor = self.log_view.textCursor()
            if cursor.hasSelection():
                QGuiApplication.clipboard().setText(
                    core.log_selection_text(cursor.selectedText()))
                return
        # プレビュー中は、本家ビューアと同じく ←→ で前・次のファイルへ移る。
        # ←→ の通常の割当(窓の切り替え)より優先。もとは「画像を表示中」だけ
        # だったが、画像とそれ以外が混ざるフォルダでは、画像でないファイルに
        # 差しかかった途端に → が窓の切り替えになってプレビューが意図せず
        # 終わってしまった(2026-10-29 指摘)。種類によらず効かせる。
        if (self.preview_on and not (shift or ctrl or alt)
                and event.key() in (Qt.Key_Left, Qt.Key_Right)):
            pane.set_cursor(pane.cursor_row()
                            + (1 if event.key() == Qt.Key_Right else -1))
            return
        code = core.scav(vk, shift, ctrl, alt)
        command = self.keymap.get(code)
        if command:
            self.run_command(command)
            return
        # Ctrl+英字 / Ctrl+数字 は頭文字ジャンプ。36 個をキー表に並べず、ここで一括で拾う
        # (filer.keys で個別に割り当てたキーは上で先に処理され、空文字で外したキーは
        # ここでも動かさない)。
        char = core.initial_jump_char(vk, shift, ctrl, alt)
        if char is not None and code not in self.disabled_keys:
            self.jump_initial(char)

    def jump_initial(self, char: str) -> None:
        pane = self.active
        row = core.initial_jump_index(pane.entries, char, pane.cursor_row())
        if row is not None:
            pane.set_cursor(row)

    def run_command(self, command: str) -> None:
        method = getattr(self, f"cmd_{command}", None)
        if method is None:
            return
        try:
            method()
        except Exception:  # noqa: BLE001  1つのコマンドの失敗で窓を落とさない
            self.log(f"エラー({command}): {_log_exception(command)}")

    # --- インクリメンタルサーチ ---
    def cmd_isearch(self) -> None:
        self.active.isearch_text = ""
        self.active.update_footer()

    def end_isearch(self) -> None:
        pane = self.active
        pane.isearch_text = None
        pane.update_footer()

    def _isearch_key(self, event) -> bool:
        """サーチ中のキー。処理したら True。矢印などは検索を終えてから通常の処理へ回す。"""
        pane = self.active
        key = event.key()
        if key in (Qt.Key_Escape, Qt.Key_Return, Qt.Key_Enter):
            self.end_isearch()
            return True
        if key == Qt.Key_Backspace:
            pane.isearch_text = pane.isearch_text[:-1]
            self._isearch_move()
            return True
        text = event.text()
        mods = event.modifiers()
        if text and text.isprintable() and not (mods & (Qt.ControlModifier | Qt.AltModifier)):
            pane.isearch_text += text
            self._isearch_move()
            return True
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt):
            return True
        if core.initial_jump_char(_vk_of(event), bool(mods & Qt.ShiftModifier),
                                  bool(mods & Qt.ControlModifier),
                                  bool(mods & Qt.AltModifier)) is not None:
            # Ctrl+英数字(頭文字ジャンプ)はサーチ中は効かせない。サーチを続ける。
            return True
        self.end_isearch()
        return False

    def _isearch_move(self) -> None:
        pane = self.active
        row = core.isearch_index(pane.entries, pane.isearch_text)
        if row is not None:
            pane.set_cursor(row)
        pane.update_footer()

    # --- カーソル ---
    def cmd_cursor_up(self):
        self.active.set_cursor(self.active.cursor_row() - 1)

    def cmd_cursor_down(self):
        self.active.set_cursor(self.active.cursor_row() + 1)

    def cmd_page_up(self):
        self.active.set_cursor(self.active.cursor_row() - self.active.page_rows())

    def cmd_page_down(self):
        self.active.set_cursor(self.active.cursor_row() + self.active.page_rows())

    def cmd_cursor_top(self):
        self.active.set_cursor(0)

    def cmd_cursor_bottom(self):
        self.active.set_cursor(len(self.active.entries) - 1)

    # --- 窓・フォルダ移動 ---
    def cmd_focus_left(self):
        target = core.arrow_target(self.active_side, "left")
        if target:
            self.activate(target)

    def cmd_focus_right(self):
        target = core.arrow_target(self.active_side, "right")
        if target:
            self.activate(target)

    def cmd_switch(self):
        self.activate("right" if self.active_side == "left" else "left")

    def cmd_parent(self):
        pane = self.active
        if pane.stash:
            # 置き場での BS は「開く前にいたフォルダへ戻る」(フラット表示と同じ作法)。
            pane.close_stash()
            return
        if not pane.path:
            return
        if pane.flat or pane.flat_loading or pane.es_loading:
            # フラット表示中の BS は「通常の表示へ戻る」(親へは行かない)。
            # 検索結果(G / Shift+F)もフラット表示の形なので同じ。
            pane.load(pane.path, history=None)
            return
        parent = core.parent_of(pane.path)
        if parent is None:
            # ドライブのルートでは、BS でドライブの選択へ(2026-10-29 要望)。
            self.cmd_drive()
            return
        # 親に移ったら、いま居たフォルダにカーソルを合わせる(あふｗと同じ)。
        pane.load(parent, focus=os.path.basename(core.normalize_dir(pane.path)))

    def cmd_enter(self):
        pane = self.active
        entry = pane.cursor_entry()
        if entry is None:
            return
        if entry.is_parent:
            self.cmd_parent()
        elif entry.is_dir:
            pane.load(os.path.join(pane.path, entry.name))
        elif pane.archive is None and filer_archive.is_archive_name(entry.name):
            # 書庫ファイルは中へ入る(あふｗの仮想フォルダ)。関連付けで開きたいときは Ctrl+Enter。
            pane.load(os.path.join(pane.path, entry.name))
        elif core.preview_kind(entry, self.viewer_extensions) == "image":
            # 画像は内部ビューア(プレビュー)で開く。関連付けで開きたいときは Ctrl+Enter。
            if self.preview_on:
                self.schedule_preview(immediate=True)
            else:
                self.preview_show()
        elif core.opens_in_viewer(entry.name, self.viewer_extensions):
            # テキストはビュアで(あふｗの拡張子判別実行)。関連付けは Ctrl+Enter。
            self._view_entry(entry)
        else:
            self._open_file(entry)

    # --- 簡易テキストビュア ---
    def _view_entry(self, entry) -> None:
        pane = self.active
        display = os.path.join(pane.path, entry.name)
        if pane.archive is not None:
            # 書庫の中のファイルは一時フォルダへ取り出してから開く。表示名は書庫の中のパス。
            self._extract_to_temp([entry], lambda paths: self.open_viewer(paths[0], display))
            return
        self.open_viewer(display)

    def open_viewer(self, path: str, title: str = None):
        """ビュアの窓を開く(モードレス。いくつ開いてもよい)。開いた窓を返す。"""
        import filer_viewer  # 使うときに読む(ファイラの起動を軽く保つ)

        section = _section(self.app_settings)
        geometry = section.get("viewer_geometry")
        if not (isinstance(geometry, list) and len(geometry) == 4
                and all(isinstance(v, int) for v in geometry)
                and any(s.availableGeometry().intersects(QRect(*geometry))
                        for s in QGuiApplication.screens())):
            geometry = None
        colors = {"background": self.colors["background"], "text": self.colors["text"]}
        try:
            viewer = filer_viewer.TextViewer(
                path, title_name=title or path, font=self.active.pane_font, colors=colors,
                editor_callback=lambda p: self._open_in_editor([p]), geometry=geometry)
        except OSError as e:
            self.log(f"ビュアで開けません: {path}  ({e})")
            return None
        viewer.closed.connect(lambda geo, v=viewer: self._on_viewer_closed(v, geo))
        self._viewers.append(viewer)
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()
        viewer.area.setFocus()
        self.log(f"ビュア: {title or path}")
        return viewer

    def _on_viewer_closed(self, viewer, geometry) -> None:
        try:
            if viewer in self._viewers:
                self._viewers.remove(viewer)
            save_values(self.app_settings, self.settings_path, {"viewer_geometry": geometry})
            if self.isVisible():
                # 閉じたらファイラへ戻る(キーがすぐ効くように)。
                self.activateWindow()
                self.active.view.setFocus()
        except Exception:  # noqa: BLE001
            _log_exception("viewerClosed")

    def cmd_open_assoc(self):
        entry = self.active.cursor_entry()
        if entry is None or entry.is_parent:
            return
        self._open_file(entry)

    def _open_file(self, entry) -> None:
        pane = self.active
        if pane.archive is not None:
            # 書庫の中のファイルは一時フォルダへ取り出してから開く。
            self._extract_to_temp([entry], lambda paths: self._startfile(paths[0]))
            return
        self._startfile(os.path.join(pane.path, entry.name))

    def _startfile(self, path: str) -> None:
        try:
            os.startfile(path)
            self.log(f"開く: {path}")
        except OSError as e:
            self.log(f"開けません: {path}  ({e})")

    def cmd_open_in_other(self):
        """Shift+Enter: カーソルのフォルダを反対窓で開く。ファイルならエディタで開く。

        あふｗの Shift+Enter はエディタだが、フォルダをエディタへ渡しても開けないので、
        フォルダのときだけ反対窓で開く動きに倒した(2026-10-26 ユーザー要望)。対象は
        マークではなくカーソル行。`..` のときは親フォルダを反対窓で開く。書庫の中の
        フォルダも、cmd_enter と同じパスの組み立てで反対窓へ渡せる。"""
        pane = self.active
        entry = pane.cursor_entry()
        if entry is not None and entry.is_dir:
            if entry.is_parent:
                parent = core.parent_of(pane.path) if pane.path else None
                if parent is not None:
                    self.other.load(parent)
            else:
                self.other.load(os.path.join(pane.path, entry.name))
            return
        self.cmd_edit()

    def cmd_edit(self):
        files = [e for e in self.active.targets() if not e.is_dir]
        if not files:
            return
        if self.active.archive is not None:
            self._extract_to_temp(files, self._open_in_editor)
            return
        self._open_in_editor([os.path.join(self.active.path, e.name) for e in files])

    def _open_in_editor(self, paths) -> None:
        # 1ファイルずつ起こす。メモ帳は引数を1つしか受け取らないので、まとめて渡すと
        # 2つ目以降が「ファイル名の続き」と解釈されて開けない。
        opened = 0
        for path in paths:
            try:
                subprocess.Popen([self.editor, path], close_fds=True)
                opened += 1
            except OSError as e:
                self.log(f"エディタを起動できません: {self.editor}  ({e})")
                break
        if opened:
            self.log(f"エディタ: {opened}ファイル ({self.editor})")

    # --- 外のアプリで開く(F9 / F10) ---
    def _real_folder(self):
        """いまの窓の実フォルダ。書庫の中なら書庫ファイルのあるフォルダ(書庫の中の
        パスは PowerShell もエクスプローラも開けない)。"""
        pane = self.active
        if pane.archive is not None:
            return os.path.dirname(pane.archive[0])
        return pane.path

    # --- エクスプローラの右クリックメニュー(F7) ---
    def cmd_context_menu(self):
        """F7: マーク(無ければカーソル行。".." ならいまのフォルダ自身)のコンテキストメニュー。

        メニューは COM(IContextMenu)でしか出せないので、子プロセス filer_contextmenu.py に
        出させる。シェル拡張がまるごと読み込まれる場所なので、ファイラ本体には持ち込まない
        (落ちても子プロセスだけで済む。CLAUDE.md の COM の項)。終わりは待たずに戻り、
        終わったら両窓を読み直す(削除・改名・貼り付けなどをメニューから行った場合)。"""
        pane = self.active
        if pane.archive is not None:
            self.log("書庫内ではコンテキストメニューは使えません")
            return
        if not pane.path:
            return
        paths = core.context_targets(pane.path, pane.entries, pane.marks, pane.cursor_row())
        if not paths:
            return
        if len({os.path.normcase(os.path.dirname(p)) for p in paths}) > 1:
            # フラット表示で別々のフォルダのものをマークしたとき。エクスプローラの
            # メニューは同じフォルダの中のものしか1つにまとめられない。
            self.log("右クリックメニュー: 別々のフォルダのものは1つのメニューにできません")
            return
        x, y = self._context_point(pane)
        self.spawn_context_menu(paths, x, y)

    def _context_point(self, pane):
        """メニューを出す位置(物理ピクセルの画面座標)。カーソル行の左下。"""
        view = pane.view
        rect = view.visualRect(view.currentIndex())
        local = rect.bottomLeft() if rect.isValid() else view.viewport().rect().center()
        point = view.viewport().mapToGlobal(local)
        screen = QGuiApplication.screenAt(point) or self.screen()
        if screen is None:
            return point.x(), point.y()
        origin = screen.geometry().topLeft()
        return core.to_native_point(point.x(), point.y(), origin.x(), origin.y(),
                                    screen.devicePixelRatio())

    def spawn_context_menu(self, paths, x: int, y: int) -> None:
        args = core.context_menu_args(paths, x, y, self._hwnd())
        if args is None:
            # マークが多くて引数に載らない。一時 JSON で渡す(子が読んだら消す)。
            fd, json_path = tempfile.mkstemp(prefix="filer_ctx_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(core.context_menu_payload(paths, x, y, self._hwnd()), f,
                          ensure_ascii=False)
            args = ["--json", json_path]
        argv = [pythonw_executable(), str(CONTEXT_MENU_SCRIPT)] + args
        # メニューの持ち主(子の非表示窓)が前面を取れるように、権利を譲ってから起こす。
        filer_launch._allow_foreground()
        try:
            proc = subprocess.Popen(argv, close_fds=True)
        except OSError as e:
            self.log(f"コンテキストメニューを起動できません  ({e})")
            return
        bridge = self.bridge

        def wait():
            try:
                code = proc.wait()
            except Exception:  # noqa: BLE001
                code = -1
            try:
                bridge.context_done.emit(code)
            except RuntimeError:
                pass  # 窓が先に閉じた

        threading.Thread(target=wait, daemon=True, name="filer-contextmenu").start()

    def _on_context_done(self, code) -> None:
        try:
            if code not in (0, None):
                self.log(f"コンテキストメニューを出せませんでした（終了コード {code}）")
            self.left.reload(keep_marks=True)
            self.right.reload(keep_marks=True)
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('contextDone')}")

    def cmd_open_shell(self):
        """F9: いまのフォルダを作業フォルダにして PowerShell を新しい窓で開く。
        パスはコマンド文字列に埋めず cwd で渡す(' や $ を含むフォルダ名で壊れない)。"""
        self._open_shell_at(self._real_folder())

    def _open_shell_at(self, folder) -> None:
        if not folder:
            return
        try:
            subprocess.Popen([self.shell, "-NoExit"], cwd=folder,
                             creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)
            self.log(f"PowerShell: {folder}")
        except OSError as e:
            self.log(f"PowerShell を起動できません: {self.shell}  ({e})")

    def cmd_open_explorer(self):
        """F10: いまのフォルダをエクスプローラで開く。"""
        folder = self._real_folder()
        if not folder:
            return
        try:
            subprocess.Popen(["explorer.exe", os.path.normpath(folder)], close_fds=True)
            self.log(f"エクスプローラ: {folder}")
        except OSError as e:
            self.log(f"エクスプローラを起動できません  ({e})")

    # --- 拡張子ごとのメニュー(Ctrl+Enter。あふｗの AfxExt) ---
    def menu_resolver(self):
        return filer_extmenu.Resolver(self.tools, app_path=self.menu_app_path,
                                      which=self.menu_which, exists=self.menu_exists)

    def menu_editor(self):
        return filer_extmenu.MenuEditor(self.tools, self.menu_shared_dir, self.menu_local_dir)

    def _menu_context(self, pane, entries) -> dict:
        """マクロの値。entries が空(".." の上)なら、いまのフォルダ自身が対象。"""
        targets = [os.path.join(pane.path, e.name) for e in entries if not e.is_parent]
        if not targets:
            targets = [pane.path]
        cursor = pane.cursor_entry()
        if cursor is not None and not cursor.is_parent:
            name = os.path.basename(cursor.name)
        else:
            name = os.path.basename(core.normalize_dir(pane.path).rstrip("\\")) or pane.path
        return {"targets": targets, "P": pane.path, "F": name, "O": self.other.path or "",
                "E": filer_extmenu.kind_of_name(name)
                if cursor is not None and not cursor.is_dir else "",
                "cwd": pane.real_dir()}

    def cmd_ext_menu(self):
        """Ctrl+Enter: 対象の拡張子(フォルダなら folder)のメニューを出す。

        対象はマーク、無ければカーソル行。".." の上ならいまのフォルダ自身(あふｗと同じ)。
        マークの拡張子が混ざっていたら、カーソル行がマークの中にあればその種類、
        無ければマークの先頭の種類で決める。定義が無い拡張子は今までどおり関連付けで開く。"""
        pane = self.active
        entry = pane.cursor_entry()
        if entry is None or not pane.path:
            return
        targets = pane.targets()
        if targets and entry in targets:
            kind_entry = entry
        elif targets:
            kind_entry = targets[0]
        else:
            kind_entry = entry
        kind = filer_extmenu.menu_kind(kind_entry)
        shared, local = filer_extmenu.find_menus(kind, self.menu_shared_dir, self.menu_local_dir)
        if shared is None and local is None:
            self.cmd_open_assoc()
            return
        resolver = self.menu_resolver()
        lines = filer_extmenu.visible_items(shared, local, resolver,
                                            in_archive=pane.archive is not None)
        items = []
        for line in lines:
            items.append(None if line.separator else (line.key or "?", line.label, line))
        if items:
            items.append(None)
        items.append(("T", "ツールを追加…", ("add",)))
        items.append(("M", "メニューを編集", ("edit",)))
        choice = self.choose_menu(kind, items)
        if choice is None:
            return
        ctx = self._menu_context(pane, targets)
        self.run_menu_choice(choice, kind, ctx, resolver)

    def run_menu_choice(self, choice, kind, ctx, resolver) -> None:
        pane = self.active
        if choice == ("add",):
            self.add_tool(kind)
            return
        if choice == ("edit",):
            path = filer_extmenu.ensure_local_file(kind, self.menu_local_dir)
            self._open_in_editor([path])
            return
        line = choice
        name = filer_extmenu.internal_name(line.command)
        if name is None:
            built = filer_extmenu.build_external(line, ctx, resolver)
            if built is None:
                self.log(f"メニュー: 実行するものが見つかりません: {line.command}")
                return
            argv, cwd = built
            try:
                subprocess.Popen(argv, cwd=cwd if cwd and os.path.isdir(cwd) else None,
                                 close_fds=True)
                self.log(f"{line.label}: {' '.join(argv[1:]) or argv[0]}")
            except OSError as e:
                self.log(f"{line.label} を起動できません: {argv[0]}  ({e})")
            return
        arg = filer_extmenu.internal_arg(line.command)
        if name == "NOP":
            return
        if name == "EXCD":
            parsed = filer_extmenu.parse_excd(arg, ctx)
            if parsed is None:
                self.log(f"メニュー: &EXCD の書き方が読めません: {arg}")
                return
            side, path, inside = parsed
            target = pane if side == "P" else self.other
            if inside:
                target.load(path)
            else:
                parent = core.parent_of(core.normalize_dir(path))
                if parent is None:
                    target.load(path)
                else:
                    target.load(parent, focus=os.path.basename(core.normalize_dir(path)))
            return
        paths = filer_extmenu.internal_targets(arg, ctx)
        uses_targets = "$MF" in arg
        if name == "EDIT":
            if pane.archive is not None and uses_targets:
                self.cmd_edit()
                return
            files = [p for p in paths if not os.path.isdir(p)]
            if files:
                self._open_in_editor(files)
            return
        if name == "SHELL":
            folder = paths[0] if paths else pane.real_dir()
            if not os.path.isdir(folder):
                folder = os.path.dirname(folder)
            self._open_shell_at(folder)
            return
        if name == "PROPERTY":
            if paths:
                self.spawn_properties(paths)
            return
        if name == "VIEW":
            entry = pane.cursor_entry()
            if uses_targets and entry is not None and not entry.is_dir:
                if core.preview_kind(entry, self.viewer_extensions) == "image":
                    if self.preview_on:
                        self.schedule_preview(immediate=True)
                    else:
                        self.preview_show()
                else:
                    self._view_entry(entry)
            elif paths and not os.path.isdir(paths[0]):
                self.open_viewer(paths[0])
            return
        if name == "SLIDESHOW":
            self.start_slideshow(paths)
            return
        if name == "OPEN":
            if pane.archive is not None and uses_targets:
                for entry in pane.targets() or [pane.cursor_entry()]:
                    if entry is not None and not entry.is_parent:
                        self._open_file(entry)
                return
            for path in paths:
                self._startfile(path)
            return

    def start_slideshow(self, paths) -> None:
        """&SLIDESHOW: 写真のスライドショー(slideshow.py)を、そのフォルダ(サブフォルダ込み)で
        一時的に再生する。登録済みのフォルダの設定は変えない。ファイルが渡されたらその
        フォルダ。マークが複数なら全部を対象にする。窓は別プロセス(slideshow_launch)。"""
        import slideshow_launch  # 使うときに読む
        folders = []
        for path in paths:
            folder = path if os.path.isdir(path) else os.path.dirname(path)
            if folder and os.path.isdir(folder) and folder not in folders:
                folders.append(folder)
        if not folders:
            self.log("スライドショー: フォルダがありません")
            return
        try:
            slideshow_launch.start(folders, self.settings_path)
            self.log(f"スライドショー: {folders[0]}"
                     + (f" ほか{len(folders) - 1}件" if len(folders) > 1 else ""))
        except Exception as e:  # noqa: BLE001
            self.log(f"スライドショーを起動できません  ({e})")

    def spawn_properties(self, paths) -> None:
        """エクスプローラのプロパティ窓。COM が要るので F7 と同じ子プロセスに出させる
        (filer_contextmenu.py --verb properties)。ファイラ本体は COM を読み込まない。"""
        folders = {os.path.normcase(os.path.dirname(os.path.normpath(p))) for p in paths}
        if len(folders) > 1:
            self.log("プロパティ: 別々のフォルダのものは1つにまとめられません")
            return
        args = core.context_menu_args(paths, 0, 0, self._hwnd(), verb="properties")
        if args is None:
            fd, json_path = tempfile.mkstemp(prefix="filer_ctx_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(core.context_menu_payload(paths, 0, 0, self._hwnd(),
                                                    verb="properties"), f, ensure_ascii=False)
            args = ["--json", json_path]
        filer_launch._allow_foreground()
        try:
            subprocess.Popen([pythonw_executable(), str(CONTEXT_MENU_SCRIPT)] + args,
                             close_fds=True)
            self.log(f"プロパティ: {paths[0]}" + (f" ほか{len(paths) - 1}件" if len(paths) > 1 else ""))
        except OSError as e:
            self.log(f"プロパティを開けません  ({e})")

    # --- ツールの追加(Ctrl+Enter のメニュー末尾 / Z の設定「拡張子とツール」) ---
    def open_with_candidates(self, kind: str) -> list:
        """この拡張子を開ける exe(テストでは差し替える)。"""
        return filer_extmenu.open_with_candidates(kind)

    def describe_exe(self, path: str) -> str:
        return filer_extmenu.file_description(path)

    def ask_add_tool(self, kind: str, editor, initial=None):
        """「ツールを追加」の小ダイアログ。結果の dict(scope 付き)か None。
        テストではここを差し替える。"""
        import filer_settings  # 使うときに読む

        dialog = filer_settings.AddToolDialog(
            self, kind, editor, candidates=self.open_with_candidates,
            describe=self.describe_exe, folder=self.active.real_dir() or "",
            initial=initial, font=self.log_font)
        accepted = self.exec_dialog(dialog) == QDialog.Accepted
        result = dialog.result_value() if accepted else None
        dialog.deleteLater()
        return result

    def add_tool(self, kind: str) -> None:
        editor = self.menu_editor()
        result = self.ask_add_tool(kind, editor)
        if not result:
            return
        try:
            line = editor.add_item(kind, result, result.get("scope") or "local")
        except ValueError as e:
            self.log(f"ツールを追加できません: {e}")
            return
        self.commit_menu_editor(editor)
        self.log(f"メニュー({kind}): 追加しました  {line}")

    def commit_menu_editor(self, editor) -> dict:
        """メニューの写しをファイルへ書き、新しいツールを settings.json の filer.tools に
        (ツールのキーだけ)書き足す。"""
        written = editor.commit()
        if written["tools"]:
            self.save_tools(written["tools"])
        if written["shared"]:
            self.log("リポジトリのファイルを変更しました（他の PC へ配るには commit・push が"
                     "必要です）: " + ", ".join(os.path.basename(p) for p in written["shared"]))
        return written

    def save_tools(self, updates: dict) -> None:
        """filer.tools の該当キーだけを差し替える(ほかのツールや filer の他のキーは触らない)。"""
        self.tools.update(updates)
        section = self.app_settings.setdefault(SETTINGS_SECTION, {}) if isinstance(
            self.app_settings, dict) else {}
        if isinstance(section, dict):
            current = section.get("tools") if isinstance(section.get("tools"), dict) else {}
            section["tools"] = {**current, **updates}
        if not self.settings_path:
            return
        try:
            stored = {}
            if os.path.exists(self.settings_path):
                with open(self.settings_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
            filer_section = stored.get(SETTINGS_SECTION)
            if not isinstance(filer_section, dict):
                filer_section = stored[SETTINGS_SECTION] = {}
            tools = filer_section.get("tools")
            if not isinstance(tools, dict):
                tools = filer_section["tools"] = {}
            tools.update(updates)
            settings_module.save_settings(stored, self.settings_path)
        except Exception as e:  # noqa: BLE001
            self.log(f"settings.json に書けません: {e}")

    # --- 更新を適用して再起動(Shift+F5) ---
    def check_updates(self, repo_dir):
        """fetch して調べる(別スレッドから呼ぶ。テストでは差し替える)。"""
        return filer_update.check_updates(repo_dir)

    def pull_updates(self, repo_dir):
        return filer_update.pull(repo_dir)

    def disk_version(self, repo_dir):
        """ディスク上の版({"label", "sha"})。起動時の self.version_info と比べる。"""
        return filer_update.version_info(repo_dir)

    def lookup_version(self, repo_dir):
        """git コマンドで版の連番を数える(別スレッドから呼ぶ)。"""
        return filer_update.git_version(repo_dir)

    def _start_version_lookup(self) -> None:
        self._version_done = False
        if not filer_update.has_git(self.repo_dir):
            self._version_done = True
            if self.version_info.get("label"):
                self.log(f"版: {filer_update.describe_version(self.version_info)}")
            return
        bridge, repo = self.bridge, self.repo_dir

        def worker():
            try:
                found = self.lookup_version(repo)
            except Exception:  # noqa: BLE001  版が日付だけになるだけ
                found = None
            try:
                bridge.version_ready.emit(found)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name="filer-version").start()

    def _on_version_ready(self, found) -> None:
        try:
            if found and found.get("label"):
                self.version_info = dict(found)
                self.version = found["label"]
                self._update_title()
            # ハッシュは調べ物用にログへ(タイトルは版だけ)
            if self.version_info.get("label"):
                self.log(f"版: {filer_update.describe_version(self.version_info)}")
        except Exception:  # noqa: BLE001  スロットの中の例外で落とさない
            _log_exception("versionReady")
        finally:
            self._version_done = True

    def cmd_update_restart(self):
        """Shift+F5: git で更新を取り込み、あっふだけを再起動する(常駐はそのまま)。"""
        if self._update_busy:
            self.log("更新: 確認の途中です")
            return
        if not filer_update.has_git(self.repo_dir):
            if self.confirm("あっふの更新", "この PC は ZIP 展開なので自動更新できません。\n"
                            "再起動だけしますか？", default_yes=False):
                self.restart_filer()
            return
        self._update_busy = True
        self.set_status_state("更新を確認中")
        self.log("更新: 新しいコミットを確かめています（git fetch）…")
        bridge, repo = self.bridge, self.repo_dir

        def worker():
            try:
                info = self.check_updates(repo)
                # 取り込むものが無くても、起動した後にこの PC で commit / pull していれば
                # ファイルの方が新しい。それを見分けるためにディスク上の版も読んでおく
                if isinstance(info, dict) and info.get("state") == "uptodate":
                    info["disk_version"] = self.disk_version(repo)
            except Exception as e:  # noqa: BLE001
                info = {"state": "error", "message": f"更新を確かめられません: {e}"}
            try:
                bridge.update_checked.emit(info)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name="filer-update-check").start()

    def _on_update_checked(self, info) -> None:
        try:
            self._update_busy = False
            self.set_status_state("Ready.")
            state = info.get("state")
            if state == "nogit":
                if self.confirm("あっふの更新", "この PC は ZIP 展開なので自動更新できません。\n"
                                "再起動だけしますか？", default_yes=False):
                    self.restart_filer()
                return
            if state == "error":
                self.log("更新: " + (info.get("message") or "失敗しました"))
                return
            if state in ("diverged", "dirty"):
                self.log(filer_update.describe_block(info))
                return
            if state == "uptodate":
                disk = info.get("disk_version") or {}
                if isinstance(disk, str):
                    disk = {"label": disk, "sha": ""}
                running_info = dict(getattr(self, "version_info", None) or {})
                running_info["label"] = self.version or ""
                if (disk.get("label") and running_info["label"]
                        and not filer_update.same_version(disk, running_info)):
                    # 動いているあっふが古いだけ。再起動すれば反映されるので「はい」を既定に
                    disk = filer_update.describe_version(disk)
                    running = filer_update.describe_version(running_info)
                    self.log(f"更新: ファイルは {disk}、動いているのは {running}")
                    if self.confirm("あっふの更新",
                                    f"ファイルは新しい版（{disk}）です。\n"
                                    f"動いているあっふは起動時の版（{running}）のままです。\n\n"
                                    "再起動して反映しますか？", default_yes=True):
                        self.restart_filer()
                    return
                self.log("更新: 最新です")
                if self.confirm("あっふの更新", "最新です。再起動だけしますか？",
                                default_yes=False):
                    self.restart_filer()
                return
            commits = info.get("commits") or []
            text = f"新しいコミットが {info.get('behind')}件あります。\n\n" + "\n".join(
                commits[:filer_update.LOG_LIMIT]) + "\n\n取り込んで（git pull --ff-only）再起動しますか？"
            if not self.confirm("あっふの更新", text, default_yes=True):
                self.log("更新: 取りやめました")
                return
            self._update_busy = True
            self.set_status_state("更新中")
            bridge, repo = self.bridge, self.repo_dir

            def worker():
                try:
                    result = self.pull_updates(repo)
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "message": f"git pull に失敗: {e}"}
                try:
                    bridge.update_pulled.emit((info, result))
                except RuntimeError:
                    pass

            threading.Thread(target=worker, daemon=True, name="filer-update-pull").start()
        except Exception:  # noqa: BLE001  スロットの中の例外で落とさない
            self.log(f"エラー: {_log_exception('updateChecked')}")

    def _on_update_pulled(self, payload) -> None:
        try:
            self._update_busy = False
            self.set_status_state("Ready.")
            info, result = payload
            if not result.get("ok"):
                self.log("更新: " + (result.get("message") or "失敗しました"))
                return
            self.log(f"更新: {result.get('message')}（{info.get('behind')}件）")
            if filer_update.resident_files(info.get("files")):
                self.log("常駐側も更新されました。トレイの 🔄 で再起動してください")
            if not self.restart_filer():
                self.log("更新は取り込みました。あとで Shift+F5 か閉じて開き直すと反映されます")
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('updatePulled')}")

    def restart_filer(self) -> bool:
        """あっふだけを再起動する。状態を保存 → 単一起動の権利を手放す → 後継を起こす →
        自分は終わる。手放してから起こすのは main.py の _restart と同じ理由(後継が
        「既に開いている」と判断して引き返すため)。起こせなければ取り戻して残る。"""
        if not self.confirm_losses("再起動しますか"):
            self.log("再起動を取りやめました")
            return False
        try:
            self.save_state()
        except Exception:  # noqa: BLE001  保存できなくても再起動はする
            _log_exception("restartSave")
        self.release_instance()
        try:
            self.spawn_successor()
        except OSError as e:
            self.log(f"再起動できません: {e}")
            self.reacquire_instance()
            return False
        self._restarting = True
        self.exit_app()
        return True

    def release_instance(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        QLocalServer.removeServer(filer_launch.SERVER_NAME)
        release_single_instance()

    def reacquire_instance(self) -> None:
        acquire_single_instance()
        self.listen()

    def spawn_successor(self) -> None:
        filer_launch.spawn()

    def exit_app(self) -> None:
        self.close()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    # --- マーク ---
    def _toggle_mark(self, step: int) -> None:
        pane = self.active
        entry = pane.cursor_entry()
        if entry is not None and not entry.is_parent:
            if entry.name in pane.marks:
                pane.marks.discard(entry.name)
            else:
                pane.marks.add(entry.name)
            pane.model.row_changed(pane.cursor_row())
        pane.set_cursor(pane.cursor_row() + step)
        pane.update_footer()

    def cmd_mark_down(self):
        self._toggle_mark(1)

    def cmd_mark_up(self):
        self._toggle_mark(-1)

    def _mark_range(self, direction: int) -> None:
        pane = self.active
        names = core.range_mark_names(pane.entries, pane.marks, pane.cursor_row(), direction)
        if not names:
            return
        pane.marks.update(names)
        pane.model.all_changed()
        pane.update_footer()

    def cmd_mark_range_up(self):
        self._mark_range(-1)

    def cmd_mark_range_down(self):
        self._mark_range(1)

    def _invert(self, include_dirs: bool) -> None:
        pane = self.active
        new_marks = core.invert_marks(pane.entries, pane.marks, include_dirs)
        # model と同じ集合を指し続けるよう、中身だけ入れ替える。
        pane.marks.clear()
        pane.marks.update(new_marks)
        pane.model.all_changed()
        pane.update_footer()

    def cmd_invert_files(self):
        self._invert(False)

    def cmd_invert_all(self):
        self._invert(True)

    # --- 再読込 ---
    def cmd_reload(self):
        self.active.reload(keep_marks=False)

    def cmd_reload_keep(self):
        self.active.reload(keep_marks=True)

    def cmd_reload_other(self):
        self.other.reload(keep_marks=False)

    # --- 左右のパス ---
    def cmd_sync_to_other(self):
        if self.other.path:
            self.active.load(self.other.path)

    def cmd_sync_other_to_me(self):
        if self.active.path:
            self.other.load(self.active.path)

    def cmd_swap_paths(self):
        left, right = self.left.path, self.right.path
        if left and right:
            self.left.load(right)
            self.right.load(left)

    # --- 履歴・ジャンプ ---
    def cmd_back(self):
        pane = self.active
        previous = pane.history.current
        target = pane.history.back()
        if target is not None:
            pane.load(target, history=("back", previous))

    def cmd_forward(self):
        pane = self.active
        previous = pane.history.current
        target = pane.history.forward()
        if target is not None:
            pane.load(target, history=("forward", previous))

    def _open_picker(self, title, items, on_path, on_accept=None, **kwargs):
        """J/H/L の選択窓(picker.PickerWindow を流用)。決めたらアクティブ窓をそこへ。
        on_accept を渡すと (名前, data) をそのまま受け取る(J の「ここを登録」用)。"""
        from picker import PickerWindow

        if self._picker is not None:
            self._picker.close()

        def accept(_name, data):
            if on_accept is not None:
                return on_accept(_name, data)   # True なら選択窓を閉じない
            path = data[1] if isinstance(data, tuple) else data
            on_path(path)

        picker = PickerWindow(title, items, accept, **kwargs)
        picker.closed.connect(self._on_picker_closed)
        self._picker = picker
        picker.show()
        return picker

    def _on_picker_closed(self) -> None:
        try:
            self._picker = None
            self.activateWindow()
            self.active.view.setFocus()
        except Exception:  # noqa: BLE001
            _log_exception("pickerClosed")

    def jump_to(self, path: str) -> None:
        """入力やブックマークから来たパスへ。ファイルならその置き場所を開いて合わせる
        (判定は読み取りスレッドの read_folder がやる。ネットワーク越しでも固まらない)。"""
        self.active.load(path)

    # --- 一時置き場(T / Shift+T) ---
    def _save_stash(self) -> None:
        save_values(self.app_settings, self.settings_path,
                    {"stash": list(self.stash_paths)})

    def add_stash_paths(self, paths) -> int:
        """パスを置き場へ登録する(重複は除く)。登録できた件数を返す。"""
        known = {os.path.normcase(p) for p in self.stash_paths}
        added = 0
        for path in paths:
            path = os.path.normpath(path)
            if os.path.normcase(path) in known:
                continue
            known.add(os.path.normcase(path))
            self.stash_paths.append(path)
            added += 1
        if added:
            self._save_stash()
        return added

    def remove_stash_paths(self, paths) -> None:
        drop = {os.path.normcase(os.path.normpath(p)) for p in paths}
        kept = [p for p in self.stash_paths if os.path.normcase(p) not in drop]
        if len(kept) != len(self.stash_paths):
            self.stash_paths = kept
            self._save_stash()

    def cmd_stash_add(self):
        pane = self.active
        if pane.stash:
            self.log("一時置き場: 置き場の中からは登録できません")
            return
        if self._refuse_archive("一時置き場への登録"):
            return
        targets = pane.targets()
        if not targets:
            return
        added = self.add_stash_paths(pane.target_paths())
        total = len(self.stash_paths)
        if added:
            self.log(f"一時置き場: {added}件を登録しました（全{total}件）")
            other = self.other
            if other.stash:
                other.show_stash()
        else:
            self.log(f"一時置き場: すでに登録されています（全{total}件）")

    def cmd_stash_toggle(self):
        pane = self.active
        if pane.stash:
            pane.close_stash()
        else:
            pane.show_stash()

    def _stash_remove_selected(self) -> None:
        """置き場での削除キー: 実ファイルは消さず、登録だけを外す。"""
        pane = self.active
        targets = pane.targets()
        if not targets:
            return
        names = [e.name for e in targets]
        if not self.confirm("一時置き場",
                            f"{len(names)}件を一時置き場から外しますか？\n"
                            "（ファイル自体は削除されません）",
                            default_yes=True):
            return
        # カーソルの行き先は通常窓の削除と同じ: 元の名前が残ればその行、
        # 無ければ元の行(次に来たもの)に留まる。先頭へは戻さない。
        entry = pane.cursor_entry()
        prev_name = entry.name if entry is not None else None
        prev_row = pane.cursor_row()
        self.remove_stash_paths(names)
        pane.show_stash()
        if pane.entries:
            target = pane._row_of(prev_name) if prev_name else None
            pane.set_cursor(target if target is not None
                            else min(prev_row, len(pane.entries) - 1))
        self.log(f"一時置き場: {len(names)}件を外しました（全{len(self.stash_paths)}件）")

    def cmd_history_jump(self):
        if not self.recent:
            self.log("履歴がありません")
            return
        items = [(p, p) for p in self.recent]
        self._open_picker("フォルダ履歴", items, self.jump_to)

    def cmd_bookmark_jump(self):
        # launcher は explorer_nav 経由で comtypes を読み込む。ファイラの起動を軽く保つ
        # ため、J を押したときに初めて読む(COM のオブジェクトは作らない。読み込むだけ)。
        import launcher

        # 押すたびに settings.json を読み直す。ファイラを開いている間に常駐側(Win+J や
        # あふｗの Alt+J)で足したブックマークも、開き直さずに出るように。登録・削除・
        # 並べ替えも読み直した dict に対して launcher の関数で行う(ファイルはキーだけ
        # 差し替えるので、ファイラ自身の filer セクションとは消し合わない)。
        current = (settings_module.load_settings(self.settings_path)
                   if self.settings_path else self.app_settings)
        # 書庫の中なら書庫のあるフォルダを登録する。ブックマークはあふｗ・エクスプローラ
        # からも使うので、書庫の中のパスを渡しても向こうが開けない。
        add_path = self.active.real_dir()
        picker = None

        def build_items() -> list:
            found = [(f"🧺 一時置き場（{len(self.stash_paths)}件）", ("stash", ""))]
            found += [(b["name"], (launcher.KIND_JUMP, b["path"]))
                      for b in launcher.load_bookmarks(current)]
            if add_path:
                found.append((f"{launcher.ADD_ITEM_PREFIX}   {add_path}",
                              (launcher.KIND_ADD, add_path)))
            return found

        def accept(_name, data):
            kind, path = data
            if kind == "stash":
                self.active.show_stash()
                return
            if kind != launcher.KIND_ADD:
                self.jump_to(path)
                return
            # 登録済みのフォルダなら増やさない。知らせて、その行を選んだまま窓を開けておく
            existing = launcher.find_bookmark_by_path(current, path)
            if existing is not None:
                picker.select_data((launcher.KIND_JUMP, existing["path"]),
                                   name=existing["name"])
                self.log(f"ブックマーク: 登録済みです（{existing['name']} → {existing['path']}）")
                return True
            # 名前の既定値はフォルダ名。ルート(C:\)は basename が空なのでパスそのもの。
            default_name = os.path.basename(path.rstrip("\\/")) or path
            name = launcher._ask_name(self, default_name)
            if name is None:
                return
            if launcher.save_bookmark(current, self.settings_path, name, path):
                self.log(f"ブックマーク: 登録しました（{name} → {path}）")
            else:
                self.log("ブックマーク: 設定ファイルに保存できませんでした")

        def delete(name, data):
            kind, path = data
            if kind != launcher.KIND_JUMP:
                return
            if not self.confirm("フォルダブックマーク", f"{name} を削除しますか？",
                                default_yes=False):
                return
            if launcher.delete_bookmark(current, self.settings_path, name, path):
                picker.set_items(build_items())
                self.log(f"ブックマーク: 削除しました（{name}。控え: {launcher.DELETED_LOG_NAME}）")
            else:
                self.log("ブックマーク: 設定ファイルに保存できませんでした")

        def move(name, data, step):
            kind, path = data
            if kind != launcher.KIND_JUMP:
                return
            if launcher.move_bookmark(current, self.settings_path, name, path, step):
                picker.set_items(build_items())

        picker = self._open_picker(
            "フォルダブックマーク", build_items(), self.jump_to, on_accept=accept,
            placeholder=launcher.PLACEHOLDER, hint=launcher.HINT,
            dynamic_items=launcher.typed_items, tab_text=launcher.tab_text,
            on_delete=delete, on_move=move)

    def cmd_path_jump(self):
        text = self.ask_text("パスを入力", "移動先（フォルダかファイルのパス）",
                             self.active.path or "")
        path = core.expand_typed_path(text) if text else None
        if path:
            self.jump_to(path)

    def cmd_desktop(self):
        self.active.load(filer_shell.desktop_path())

    def cmd_drive(self):
        drives = filer_shell.list_drives()
        self._open_picker("ドライブ", [(label, root) for root, label in drives], self.jump_to)

    # --- マスク ---
    def cmd_mask_clear(self):
        # : はマスクを *.* に戻す。ライブ絞り込みもいっしょに解く(「全部見せる」キー)。
        self.active.clear_live_filter(rebuild=False)
        self.active.set_mask(core.DEFAULT_MASK)

    # --- プレビュー(F3) ---
    def cmd_preview_toggle(self):
        if self.preview_on:
            self.preview_off()
        else:
            self.preview_show()

    def preview_show(self) -> None:
        """反対窓の場所をプレビューに差し替える(反対窓のパスやマークはそのまま持っておく)。"""
        import filer_preview  # 使うときに読む

        other = self.other
        self.preview_side = other.side
        if self.preview_widget is None:
            self.preview_widget = filer_preview.PreviewWidget(self, self.colors, self.log_font)
        index = self.hsplit.indexOf(other)
        self.hsplit.insertWidget(index, self.preview_widget)
        other.hide()
        self.preview_widget.show()
        self.preview_on = True
        self.image_turn.reset()
        self._image_wait_seq = None
        # 右窓から入ったときはフルスクリーンで出す(あふｗのビューア準拠。
        # 2026-10-29 要望)。左窓からのときは従来どおり反対窓の場所だけ。
        self._preview_fullscreen = self.active_side == "right"
        self._preview_was_maximized = self.isMaximized()
        # 戻す先は自分で控える。showNormal() 任せだと、Qt が控えた大きさを失って
        # 画面いっぱいのまま戻ることがある(offscreen のテストで実際に起きた)。
        self._preview_normal_geometry = (
            None if self._preview_was_maximized else self.geometry())
        if self._preview_fullscreen:
            self.showFullScreen()
        self.apply_layout()
        self.active.view.setFocus()
        if self._preview_fullscreen:
            # 大きさが変わってから読み始める(先に読むと小さい箱で縮められる)
            QTimer.singleShot(0, lambda: self.preview_on
                              and self.schedule_preview(immediate=True))
        else:
            self.schedule_preview(immediate=True)

    def preview_off(self) -> None:
        if not self.preview_on:
            return
        self.preview_on = False
        if self._preview_fullscreen:
            self._preview_fullscreen = False
            if self._preview_was_maximized:
                self.showMaximized()
            else:
                self.showNormal()
                saved = getattr(self, "_preview_normal_geometry", None)
                if saved is not None and saved.isValid():
                    self.setGeometry(saved)
        self._preview_timer.stop()
        with self._preview_lock:
            self._preview_generation += 1   # 待ち行列と読み込み中の結果は捨てる
            self._preview_queue.clear()
        self._image_wait_seq = None         # 読み込み待ちは必ず解く(キーを捨て続けない)
        self.image_turn.reset()             # 回転・回転固定はビュアを抜けたら解く
        pane = self.pane(self.preview_side)
        if self.preview_widget is not None:
            self.preview_widget.hide()
            self.preview_widget.setParent(None)
            self.preview_widget.deleteLater()
            self.preview_widget = None
        index = 0 if pane.side == "left" else 1
        self.hsplit.insertWidget(index, pane)
        pane.show()
        self.preview_side = None
        self.apply_layout()

    def schedule_preview(self, immediate: bool = False) -> None:
        """カーソルが動いたらプレビューを作り直す。連打で重くならないよう少し待つ。

        画像だけは待たない。下キーの長押しで次々と切り替えたいので、その場で
        読み始める(画像は表示の大きさに縮小して読むので1枚が速い。古い結果は
        要求番号で捨てられる)。"""
        if not getattr(self, "preview_on", False):
            return
        delay = 0 if immediate else PREVIEW_DELAY_MS
        if delay and core.preview_kind(self.active.cursor_entry(),
                                        self.viewer_extensions) == "image":
            delay = 0
        if delay:
            self._preview_timer.start(delay)
            return
        # 待ち時間が無いときはタイマーを経由せず、その場で待ち行列へ積む。
        # 0ミリ秒タイマーだと、イベントが回る前に続いたカーソル移動が1件に
        # 潰れてしまい、通り過ぎた画像が積まれないことがあるため。
        self._preview_timer.stop()
        self._start_preview()

    def _start_preview(self, force: bool = False) -> None:
        """いまのカーソルのファイルを読み込みへ回す(実際の読み取りは _drain_preview)。

        force は同じファイルでも読み直す(回転で縦横が入れ替わり、読み込みの縮小の
        大きさが合わなくなったとき)。"""
        try:
            if not self.preview_on or self.preview_widget is None:
                return
            pane = self.active
            entry = pane.cursor_entry()
            kind = core.preview_kind(entry, self.viewer_extensions)
            widget = self.preview_widget
            path = os.path.join(pane.path, entry.name) if kind != "none" else None
            # 別のファイルへ移ったら、回転固定していなければ向きを戻す
            self.image_turn.on_path(path)
            widget.set_turn(self.image_turn.turn, self.image_turn.suffix(), rerender=False)
            if kind == "none":
                with self._preview_lock:
                    self._preview_queue.clear()
                self._image_wait_seq = None
                widget.show_result({"title": "..", "lines": [], "image": None})
                return
            box = widget.image_box()
            odd = kind == "image" and core.turn_swaps_size(self.image_turn.turn)
            if odd:
                box = (box[1], box[0])   # 回して出すので、縦横を入れ替えた箱に収めて読む
            widget.show_loading(entry.name)
            with self._preview_lock:
                pending = self._preview_queue
                if not force and pending and pending[-1][2] == path:
                    return      # 同じファイルがまだ読み始められずに待っているなら1件でよい
                self._preview_seq += 1
                item = (self._preview_generation, self._preview_seq, path,
                        kind, entry, box, pane.archive, odd)
                # まだ読み始めていない古い要求は捨てて、最新だけを読む。長押しは
                # _drops_repeat で画像を出せる速さに抑えるので、通り過ぎる画像は
                # 出ない。単発の連打では途中を飛ばして、表示がカーソルに早く追いつく。
                self._preview_queue[:] = [item]
                if kind == "image":
                    self._image_wait_seq = self._preview_seq
                    self._image_wait_since = time.monotonic()
                else:
                    self._image_wait_seq = None
                if self._preview_worker_on:
                    return
                self._preview_worker_on = True
            threading.Thread(target=self._drain_preview, daemon=True,
                             name="filer-preview").start()
        except Exception:  # noqa: BLE001
            self._image_wait_seq = None
            self.log(f"エラー: {_log_exception('preview')}")

    def _drain_preview(self) -> None:
        """待ち行列を1件ずつ読み、表示へ回す(読み取りスレッド)。"""
        import filer_preview

        while True:
            with self._preview_lock:
                if not self._preview_queue:
                    self._preview_worker_on = False
                    return
                generation, seq, path, kind, entry, box, archive, odd = \
                    self._preview_queue.pop(0)
            try:
                result = filer_preview.load_preview(path, kind, entry, box, archive)
            except Exception as e:  # noqa: BLE001  1件の失敗で行列を止めない
                result = {"kind": "info", "title": entry.name, "image": None,
                          "lines": core.info_lines(entry.name, entry)
                          + ["", f"（読めません: {e}）"]}
            result["seq"] = seq
            result["path"] = path
            result["turn_odd"] = odd
            try:
                self.bridge.preview_ready.emit((generation, result))
            except RuntimeError:
                pass

    def _on_preview_ready(self, payload) -> None:
        try:
            generation, result = payload
            seq = result.get("seq")
            if self._image_wait_seq is not None and seq is not None \
                    and seq >= self._image_wait_seq:
                self._image_wait_seq = None     # 待っていた画像が出た(失敗でも解く)
            if generation != self._preview_generation or self.preview_widget is None:
                return     # プレビューを閉じた(開き直した)あとに届いた結果
            self.preview_widget.show_result(result)
            self._reload_if_turned(result)
        except Exception:  # noqa: BLE001
            self._image_wait_seq = None
            _log_exception("previewReady")

    def _drops_repeat(self, event) -> bool:
        """画像の読み込み中に来た、オートリピートのカーソル移動か(捨てるもの)。

        単発の押下は捨てない(取りこぼすとキーが効かないように感じる)。待ちが
        IMAGE_WAIT_LIMIT 秒を超えたら、固まらないよう捨てるのをやめる。"""
        if not self.preview_on or self._image_wait_seq is None:
            return False
        if not event.isAutoRepeat() or event.key() not in PREVIEW_REPEAT_KEYS:
            return False
        if time.monotonic() - self._image_wait_since > IMAGE_WAIT_LIMIT:
            self._image_wait_seq = None
            return False
        return True

    def turn_image(self, op: str, shift: bool = False) -> None:
        """画像ビュアの R / L / H / V(Shift+R / Shift+L は回転固定)。表示だけを回し、
        ファイルは書き換えない。向きは EXIF の自動回転を当てた後の絵にかかる。"""
        self.image_turn.press(op, shift)
        widget = self.preview_widget
        if widget is None:
            return
        widget.set_turn(self.image_turn.turn, self.image_turn.suffix())
        if widget.last is not None:
            self._reload_if_turned(widget.last)

    def _reload_if_turned(self, result) -> None:
        """縮めて読んだ絵の縦横が、いまの向きと入れ替わっていれば読み直す。

        読み込みは「回した後に窓に収まる」大きさに縮めるので、90°回すと箱の縦横が
        入れ替わり、そのままでは小さく(粗く)出る。いま見ているファイルのときだけ。"""
        if not (result.get("image") is not None and result.get("scaled")):
            return
        if bool(result.get("turn_odd")) == core.turn_swaps_size(self.image_turn.turn):
            return
        entry = self.active.cursor_entry()
        if entry is None or result.get("path") != os.path.join(self.active.path, entry.name):
            return
        self._start_preview(force=True)

    def cmd_live_filter(self):
        self.active.open_live_filter()

    def cmd_flat_view(self):
        """Alt+B: フラット表示の入り切り。入ると、いまのフォルダより下のファイルを全部
        1つの一覧に並べる(名前は相対パス)。もう一度 Alt+B・BS・フォルダの移動で戻る。"""
        pane = self.active
        if not pane.path:
            return
        if pane.archive is not None:
            self.log("書庫内ではフラット表示できません")
            return
        if pane.flat or pane.flat_loading:
            pane.load(pane.path, history=None)
            return
        pane.load_flat()

    def cmd_grep(self):
        """G: 文字列検索。いまのフォルダより下のファイルの中身から探し、ヒットした
        ファイルをフラット表示の形(相対パス)で並べる。書庫の中は探さない。"""
        pane = self.active
        if not pane.path:
            return
        if pane.archive is not None:
            self.log("文字列検索: 書庫内では使えません")
            return
        word = self.ask_text("文字列検索", "探す文字列（サブフォルダも含む・大文字小文字は区別しない）",
                             pane.grep_query or "")
        if word is None or not word.strip():
            return
        pane.load_grep(word.strip(), case_sensitive=False)

    def find_es(self):
        """es.exe を起こす引数の頭([exe, …])。見つからなければ None。
        場所は filer.tools の "es" → App Paths・PATH → 既定のインストール先の順。"""
        return filer_everything.find_es(self.menu_resolver(), self.es_environ,
                                        exists=self.menu_exists)

    def cmd_everything(self):
        """Shift+F: Everything 検索。es.exe で名前を探し、結果をフラット表示の形で並べる
        (範囲は既定でアクティブ窓のフォルダ以下。入力窓で全体に切り替えられる)。"""
        pane = self.active
        if pane.stash:
            self.log("Everything検索: 一時置き場では使えません（T で通常の表示へ）")
            return
        if not pane.path:
            return
        if pane.archive is not None:
            self.log("Everything検索: 書庫内では使えません")
            return
        if self.find_es() is None:
            self.log(f"Everything検索: {filer_everything.NOT_FOUND_MESSAGE}")
            return
        answer = self.ask_everything(pane.es_query or "", self.es_scope_all)
        if answer is None:
            return
        query, scope_all = answer
        if not query.strip():
            return
        self.es_scope_all = bool(scope_all)
        pane.load_everything(query.strip(), self.es_scope_all)

    def cmd_mask_input(self):
        current = self.active.mask_text
        text = self.ask_text("マスク", "表示するファイル（*.txt;*.md / 語だけなら「含む」）",
                             "" if core.is_default_mask(current) else current)
        if text is not None:
            self.active.set_mask(text.strip() or core.DEFAULT_MASK)

    # --- 複写・移動・削除 ---
    def _hwnd(self) -> int:
        try:
            return int(self.winId())
        except Exception:  # noqa: BLE001
            return 0

    def _after_operation(self, label, result, sources, dest=None,
                         dest_pane=None, dest_focus=None) -> None:
        code, aborted = result
        names = f"{len(sources)}件"
        where = f" → {dest}" if dest else ""
        if aborted:
            self.log(f"{label}: 中断しました（{names}{where}）")
        elif code != 0:
            self.log(f"{label}: 失敗しました（コード 0x{code:X}、{names}{where}）")
        else:
            self.log(f"{label}: {names}{where}")
        for pane in (self.left, self.right):
            if (pane is dest_pane and dest_focus and code == 0 and not aborted):
                # あふｗと同じく、送り先の窓では送ったものの行にカーソルを置く
                # (後からフォーカスを移したとき、その行に居るように)。
                pane.reload(focus=dest_focus)
            else:
                pane.reload()

    def _refuse_flat(self, label: str) -> bool:
        """フラット表示中は、名前の付け替え・フォルダ作成を断る。名前が相対パス
        ("sub\\a.txt")なので、改名がフォルダをまたぐ移動になったり、どのフォルダに
        作るのかが曖昧になったりするため。"""
        if self.active.flat or self.active.flat_loading:
            self.log(f"{label}: フラット表示・検索結果では使えません（Alt+B で通常の表示へ）")
            return True
        return False

    def _refuse_stash(self, label: str) -> bool:
        """一時置き場の表示中は、名前の付け替え・フォルダ作成を断る(フラット
        表示と同じ理由: 名前がフルパスで、置き場所が1つに定まらないため)。"""
        if self.active.stash:
            self.log(f"{label}: 一時置き場では使えません（T で通常の表示へ）")
            return True
        return False

    def _refuse_archive(self, label: str, source: bool = True, dest_pane=None) -> bool:
        """書庫の中で書き込み系を頼まれたら断る(メッセージだけ出して True)。

        source … アクティブ窓が書庫の中なら断る(削除・改名・移動など、元を変える操作)
        dest_pane … 書き込み先の窓。そこが書庫の中なら断る(書庫へは書き込めない)"""
        if dest_pane is not None and dest_pane.archive is not None:
            self.log(f"{label}: {ARCHIVE_NO_WRITE}")
            return True
        if source and self.active.archive is not None:
            self.log(f"{label}: {ARCHIVE_READONLY}")
            return True
        return False

    def _transfer(self, move: bool) -> None:
        src = self.active
        dst = self.other
        label = "移動" if move else "複写"
        if dst.stash:
            # 反対窓が一時置き場なら、複写・移動のキーは「登録」として働く
            # (実体は動かさない。本人の要望: Shift+T より C/M の方が分かりやすい)
            self.cmd_stash_add()
            return
        if self._refuse_archive(label, source=move, dest_pane=dst):
            return
        targets = src.targets()
        if not targets:
            return
        if not dst.path or core.same_dir(src.path, dst.path):
            self.log(f"{label}: 反対窓が同じフォルダです")
            return
        if src.archive is not None:
            # 書庫の中から実フォルダへの複写は「展開」になる。
            self._extract_to_dir(targets, dst.path)
            return
        paths = src.target_paths()
        self._enqueue_transfer(move, paths, src, dst)

    # --- ファイル転送のキュー ---
    # キューの管理だけ自前で、実行そのものは今までどおりシェルの転送
    # (SHFileOperation。進捗の窓・ごみ箱の「元に戻す」は Windows 標準のまま)。
    # 違うのは呼び出すスレッド: 裏のスレッドから呼ぶので、転送の間も窓は
    # 操作でき、次の転送を積んでおける(Double Commander の操作キューの考え方)。

    def _enqueue_transfer(self, move: bool, paths, src, dst) -> None:
        op = {"move": move, "label": "移動" if move else "複写",
              "paths": list(paths), "dest": dst.path, "dst_pane": dst,
              "from_stash": bool(src.stash),
              "focus": os.path.basename(paths[0]) if paths else None}
        self._op_queue.append(op)
        self._pump_op_queue()
        waiting = f"（待ち {len(self._op_queue)}件）" if self._op_queue else ""
        self.log(f"キューに追加: {op['label']} {len(paths)}件 → {dst.path}{waiting}")

    def _pump_op_queue(self) -> None:
        if self._op_running is not None or not self._op_queue:
            self._refresh_op_status()
            return
        op = self._op_queue.pop(0)
        self._op_running = op
        self._refresh_op_status()
        func = filer_shell.move_files if op["move"] else filer_shell.copy_files
        hwnd, flags = self._hwnd(), self.extra_op_flags

        def worker():
            # シェルのファイル操作は、呼び出しスレッドで COM(STA)が
            # 初期化されている前提。UI スレッド以外から呼ぶので、ここで
            # 初期化して対で戻す(ごみ箱やコピーフック等のシェル拡張が
            # 絡むときに効く)。
            com_ready = _thread_com_init()
            try:
                result = func(op["paths"], op["dest"], hwnd, flags)
            except Exception:  # noqa: BLE001  結果として戻し、窓は落とさない
                _log_exception("transfer worker")
                result = (0x80004005, False)  # E_FAIL として失敗の形に寄せる
            finally:
                if com_ready:
                    _thread_com_uninit()
            self.bridge.op_done.emit((op, result))

        threading.Thread(target=worker, daemon=True, name="filer-transfer").start()

    def _on_op_done(self, payload) -> None:
        op, result = payload
        self._op_running = None
        try:
            if op["move"] and op["from_stash"] and result == (0, False):
                # 置き場から移動し終えたものは、登録からも外す(実体が移ったため)
                self.remove_stash_paths(op["paths"])
            self._after_operation(op["label"], result, op["paths"], op["dest"],
                                  dest_pane=op["dst_pane"], dest_focus=op["focus"])
        finally:
            self._pump_op_queue()

    def _refresh_op_status(self) -> None:
        """キューの状態をステータスバー左端に出す(終われば Ready. に戻す)。"""
        try:
            if self._op_running is not None:
                text = (f"{self._op_running['label']}中"
                        f" {len(self._op_running['paths'])}件")
                if self._op_queue:
                    text += (f"（キュー待ち {len(self._op_queue)}件"
                             "・Shift+Q で待ちを取り消し）")
                self.set_status_state(text)
            elif self._op_queue:
                self.set_status_state(f"キュー待ち {len(self._op_queue)}件")
            else:
                self.set_status_state("Ready.")
        except Exception:  # noqa: BLE001  ステータス表示で窓を落とさない
            pass

    def cmd_queue_clear(self):
        """Shift+Q: 待ち行列を取り消す。実行中の転送はシェルの窓で止める。"""
        count = len(self._op_queue)
        if not count:
            self.log("キュー: 待ちはありません")
            return
        self._op_queue.clear()
        self.log(f"キュー: 待ち {count}件を取り消しました")
        self._refresh_op_status()

    def cmd_copy_direct(self):
        self._transfer(move=False)

    def cmd_move_direct(self):
        self._transfer(move=True)

    def cmd_copy_menu(self):
        if not self.active.targets():
            return
        items = [
            ("O", "反対窓にコピー", "copy"),
            ("P", "フルパスをクリップボードへ（改行区切り）", "path"),
            ("N", "ファイル名のみクリップボードへ", "name"),
            ("H", "ハッシュ(SHA-256)をクリップボードへ", "hash"),
            ("I", "フォルダ構成をコピー", "tree"),
        ]
        if self.phone_enabled:
            # 環境1のときだけ出す(無効な PC では項目自体を見せない)
            items.append(("S", "スマホへ送る（QR）", "phone"))
        items.append(("Q", "キャンセル", None))
        choice = self.choose_menu("複写", items)
        if choice == "copy":
            self._transfer(move=False)
        elif choice == "phone":
            self.cmd_phone_send()
        elif choice in ("path", "name"):
            paths = self.active.target_paths()
            QGuiApplication.clipboard().setText(core.clip_text(paths, names_only=choice == "name"))
            self.log(f"クリップボード: {len(paths)}件の{'フルパス' if choice == 'path' else '名前'}")
        elif choice == "hash":
            self._copy_hash()
        elif choice == "tree":
            self._copy_tree()

    def cmd_phone_send(self):
        """C→S(環境1のときだけ): 対象ファイルをスマホへ QR で渡す。

        ダイアログを開いている間だけ filer_phone の小さなサーバが動き、
        閉じれば URL は無効になる。フォルダと書庫の中身は渡せない。"""
        pane = self.active
        if pane.archive is not None:
            self.log("スマホへ送る: 書庫の中では使えません")
            return
        targets = pane.targets()
        if not targets:
            return
        paths = [p for p in pane.target_paths()
                 if os.path.isfile(p)]
        if not paths:
            self.log("スマホへ送る: ファイルが選ばれていません（フォルダは送れません）")
            return
        try:
            downloads = filer_phone.show_share_dialog(self, paths)
        except ImportError:
            self.log("スマホへ送る: QR の生成に segno が必要です"
                     "（setup.py を実行すると入ります）")
            return
        self.log(f"スマホへ送る: 終わりました（{downloads}件ダウンロード）")

    def _copy_hash(self):
        """C→H: 対象ファイルの SHA-256 を計算してクリップボードへ入れる。

        大きいファイルでも画面が固まらないよう計算は裏のスレッドで行い、
        終わったら _on_hashed が受け取ってクリップボードへ入れる。"""
        pane = self.active
        if pane.archive is not None:
            self.log("ハッシュ: 書庫の中では使えません")
            return
        files = [e for e in pane.targets() if not e.is_dir]
        if not files:
            self.log("ハッシュ: ファイルが選ばれていません(フォルダは計算しません)")
            return
        if self._hash_busy:
            self.log("ハッシュ: 計算中です。終わってからもう一度どうぞ")
            return
        items = [(os.path.join(pane.path, e.name), e.name) for e in files]
        self._hash_busy = True
        self.log(f"ハッシュ計算中: {len(items)}件")

        def worker():
            results = []
            for path, name in items:
                try:
                    results.append((name, core.sha256_of_file(path), ""))
                except OSError as e:
                    results.append((name, None, str(e)))
            self.bridge.hashed.emit(results)

        threading.Thread(target=worker, daemon=True, name="filer-hash").start()

    def _on_hashed(self, results) -> None:
        self._hash_busy = False
        done = [(name, digest) for name, digest, _err in results if digest]
        for name, digest, err in results:
            if digest:
                self.log(f"SHA-256 {name}: {digest}")
            else:
                self.log(f"ハッシュ: 計算できません: {name}  ({err})")
        if done:
            QGuiApplication.clipboard().setText(core.hash_clip_text(done))
            self.log(f"クリップボード: {len(done)}件のハッシュ")

    def tree_root(self):
        """C→I で書き出すフォルダ。カーソルがフォルダならそのフォルダ、ファイル(や ..)なら
        アクティブ窓のフォルダ。書庫の中では None。"""
        pane = self.active
        if pane.archive is not None:
            return None
        entry = pane.cursor_entry()
        if entry is not None and entry.is_dir and not entry.is_parent:
            return os.path.join(pane.path, entry.name)
        if pane.path:
            return pane.path
        # 一時置き場(パスが無い)でファイルの上なら、そのファイルの置き場所
        return os.path.dirname(entry.name) if entry is not None else None

    def _copy_tree(self):
        """C→I: フォルダ構成を罫線のツリーにしてクリップボードへ。走査は裏のスレッドで
        (ネットワーク上の大きなフォルダでも窓を固めない)。"""
        if self.active.archive is not None:
            self.log("フォルダ構成: 書庫の中では使えません")
            return
        root = self.tree_root()
        if not root:
            return
        if self._tree_busy:
            self.log("フォルダ構成: 作成中です。終わってからもう一度どうぞ")
            return
        self._tree_busy = True
        show_hidden, show_system = self.show_hidden, self.show_system
        depth, max_lines = self.tree_depth, self.tree_lines
        bridge = self.bridge

        def worker():
            try:
                lines, _cut = core.folder_tree(root, show_hidden, show_system, depth, max_lines)
                payload = (root, lines, "")
            except Exception as e:  # noqa: BLE001  結果として返し、窓は落とさない
                payload = (root, None, f"{type(e).__name__}: {e}")
            try:
                bridge.tree_built.emit(payload)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name="filer-tree").start()

    def _on_tree_built(self, payload) -> None:
        try:
            self._tree_busy = False
            root, lines, error = payload
            if lines is None:
                self.log(f"フォルダ構成: 読めません: {root}  ({error})")
                return
            QGuiApplication.clipboard().setText("\r\n".join(lines))
            more = "（一部省略）" if lines and lines[-1].startswith(core.TREE_MORE) else ""
            self.log(f"クリップボード: フォルダ構成 {len(lines)}行{more}  {root}")
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('tree')}")

    # --- ワークスペース(Alt+J。左右のパスの組) ---
    def _workspace_side_path(self, pane):
        """保存に使う窓のパス。一時置き場なら開く前にいたフォルダ。"""
        if pane.stash:
            return pane.stash_from
        return pane.path

    def cmd_workspace(self):
        """Alt+J: 左右の窓のパスを名前付きで保存・呼び出し・削除する小さなメニュー。
        先頭は「今の左右を保存…」、続いて保存済みの一覧。Enter で左右を一度に移し、
        Del で削除(確認あり)。保存先は %LOCALAPPDATA%\\traytools(PC ごとの個人データ)。"""
        while True:
            saved = core.load_workspaces()
            keys = "123456789ABCDEFGHIJKLMNOPRTUVWXYZ"   # S(保存)と Q(キャンセル)は除く
            items = [("S", "今の左右を保存…", ("save", None)), None]
            for index, ws in enumerate(saved):
                key = keys[index] if index < len(keys) else "-"
                items.append((key, f"{ws['name']}    {ws['left']} → {ws['right']}",
                              ("open", ws)))
            if not saved:
                items.append(("-", "（保存したワークスペースはまだありません）", None))
            items.append(None)
            items.append(("Q", "キャンセル", None))
            kind, value = self.choose_menu_deletable("ワークスペース（Del で削除）", items)
            if kind == "delete" and (value is None or value[0] != "open"):
                continue   # 保存・キャンセルの行の Del は何もしない(メニューはそのまま)
            if value is None:
                return
            action, ws = value
            if kind == "delete":
                if self.confirm("ワークスペース", f"「{ws['name']}」を削除しますか？\n"
                                f"（{ws['left']} → {ws['right']}）", default_yes=False):
                    try:
                        core.save_workspaces(core.workspace_remove(saved, ws["name"]))
                        self.log(f"ワークスペース: 削除しました（{ws['name']}）")
                    except OSError as e:
                        self.log(f"ワークスペース: 保存できません  ({e})")
                        return
                continue   # 一覧を出し直す(続けて消せるように)
            if action == "save":
                self._save_workspace(saved)
            elif action == "open":
                self.open_workspace(ws)
            return

    def _save_workspace(self, saved) -> None:
        left = self._workspace_side_path(self.left)
        right = self._workspace_side_path(self.right)
        if not left or not right:
            self.log("ワークスペース: 左右のどちらかのパスが決まっていません")
            return
        default = " / ".join(os.path.basename(core.normalize_dir(p).rstrip("\\/")) or p
                             for p in (left, right))
        name = self.ask_text("ワークスペースを保存", f"名前（{left} → {right}）", default)
        if name is None or not name.strip():
            return
        name = name.strip()
        index = core.workspace_find(saved, name)
        if index is not None and not self.confirm(
                "ワークスペース", f"「{saved[index]['name']}」を上書きしますか？",
                default_yes=False):
            return
        try:
            core.save_workspaces(core.workspace_put(saved, name, left, right))
        except OSError as e:
            self.log(f"ワークスペース: 保存できません  ({e})")
            return
        self.log(f"ワークスペース: 保存しました（{name}: {left} → {right}）")

    def open_workspace(self, ws) -> None:
        """左右を一度に移す。開けない側(消えた・繋がらない)はその側だけ移さず知らせる。
        読み取りは窓ごとの別スレッド(load)なので、ネットワークのパスでも固まらない。"""
        self.log(f"ワークスペース: {ws['name']}")
        for pane, key, label in ((self.left, "left", "左"), (self.right, "right", "右")):
            path = ws.get(key)
            if not path:
                continue

            def failed(path=path, label=label):
                self.log(f"ワークスペース: {label}は移動しません（開けません: {path}）")

            pane.load(path, on_fail=failed)

    def cmd_move_menu(self):
        if self._refuse_archive("移動", dest_pane=self.other):
            return
        if not self.active.targets():
            return
        choice = self.choose_menu("移動", [
            ("O", "反対窓に移動", "move"),
            ("Q", "キャンセル", None),
        ])
        if choice == "move":
            self._transfer(move=True)

    def _delete(self, permanent: bool) -> None:
        if self._refuse_archive("削除"):
            return
        pane = self.active
        targets = pane.targets()
        if not targets:
            return
        shown = "\n".join(e.name + ("\\" if e.is_dir else "") for e in targets[:8])
        if len(targets) > 8:
            shown += f"\n…ほか {len(targets) - 8}件"
        if permanent:
            question = f"{len(targets)}件を完全に削除します（ごみ箱に入りません）。よろしいですか？"
        else:
            question = f"{len(targets)}件をごみ箱へ送ります。よろしいですか？"
        # 完全削除も既定のボタンは「はい」(2026-10-27 ユーザー要望。Shift+D は
        # 意図が明確な操作なので、Enter でそのまま実行できるようにする)。
        if not self.confirm("削除", f"{question}\n\n{shown}", default_yes=True):
            return
        paths = [os.path.join(pane.path, e.name) for e in targets]
        result = filer_shell.delete_files(paths, self._hwnd(), permanent=permanent,
                                          extra_flags=self.extra_op_flags)
        self._after_operation("完全削除" if permanent else "削除(ごみ箱)", result, paths)

    def cmd_delete(self):
        if self.active.stash:
            self._stash_remove_selected()
            return
        self._delete(permanent=False)

    def cmd_delete_permanent(self):
        if self.active.stash:
            # 置き場では完全削除も「登録を外す」だけにする(実ファイルは消さない)
            self._stash_remove_selected()
            return
        self._delete(permanent=True)

    # --- 作成・改名 ---
    def cmd_mkdir(self):
        pane = self.active
        if self._refuse_stash("フォルダ作成"):
            return
        if not pane.path or self._refuse_archive("フォルダ作成") or self._refuse_flat("フォルダ作成"):
            return
        name = self.ask_text("フォルダ作成", "新しいフォルダの名前", "")
        if name is None:
            return
        name = name.strip()
        problem = core.validate_name(name)
        if problem:
            self.log(f"フォルダ作成: {problem}")
            return
        if core.name_conflicts([e.name for e in pane.all_entries], name):
            self.log(f"フォルダ作成: 同じ名前があります: {name}")
            return
        path = os.path.join(pane.path, name)
        try:
            os.mkdir(path)
        except OSError as e:
            self.log(f"フォルダ作成: できません: {path}  ({e})")
            return
        self.log(f"フォルダ作成: {path}")
        pane.reload(focus=name)

    def cmd_rename(self):
        """R: ファイル情報の編集(名前・大小の変換・更新日時・属性)。マークがあれば一括変更
        (あふｗと同じ)。対象はマーク、無ければカーソル行。".." は対象外。"""
        pane = self.active
        if self._refuse_stash("ファイル情報の編集"):
            return
        if not pane.path or self._refuse_flat("ファイル情報の編集"):
            return
        if pane.archive is not None:
            self.log("書庫内では編集できません")
            return
        if pane.marks:
            entries = [e for e in pane.entries if e.name in pane.marks and not e.is_parent]
        else:
            entry = pane.cursor_entry()
            entries = [] if entry is None or entry.is_parent else [entry]
        if not entries:
            return
        import filer_fileinfo  # 使うときに読む

        colors = {"background": self.colors["background"], "text": self.colors["text"]}
        dialog = filer_fileinfo.FileInfoDialog(
            self, pane.path, entries, colors=colors, font=self.log_font,
            on_apply=lambda plans: self._apply_file_info(pane, plans))
        self.exec_dialog(dialog)
        dialog.deleteLater()

    # --- 設定(Z) ---
    def cmd_settings_dialog(self):
        """Z: 設定ダイアログ(あふｗの Z ＝設定プログラム起動)。いまは拡張子別表示色だけ。
        決定で settings.json の filer.ext_colors だけを差し替え、両窓をその場で描き直す。"""
        import filer_settings  # 使うときに読む

        editor = self.menu_editor()
        dialog = filer_settings.SettingsDialog(
            self, self.ext_rules, font=self.log_font, menu_editor=editor,
            ask_tool=lambda kind, initial=None: self.ask_add_tool(kind, editor, initial))
        accepted = self.exec_dialog(dialog) == QDialog.Accepted
        rules = dialog.rules
        dialog.deleteLater()
        if not accepted:
            return
        written = self.commit_menu_editor(editor)
        if written["local"] or written["shared"]:
            self.log("設定: 拡張子とツールを保存しました（"
                     + ", ".join(os.path.basename(p)
                                 for p in written["local"] + written["shared"]) + "）")
        self.set_ext_rules(rules)
        save_values(self.app_settings, self.settings_path,
                    {"ext_colors": core.ext_colors_to_settings(rules)})
        self.log(f"設定: 拡張子別表示色を保存しました（{len(rules)}行）")

    def set_ext_rules(self, rules) -> None:
        self.ext_rules = core.normalize_ext_colors(core.ext_colors_to_settings(rules))
        self.ext_color_map = core.ext_color_map(self.ext_rules)
        for pane in (self.left, self.right):
            pane.view.viewport().update()

    # --- 一括改名(Renemo 風。Shift+R / Alt+R → Enter) ---
    def cmd_rename_menu(self):
        """Alt+R: 改名のメニュー。先頭が一括改名(あふｗで Alt+R → Enter で Renemo を
        起こしていた指の動きのまま)。2番目は R と同じファイル情報の編集。"""
        choice = self.choose_menu("改名", [
            ("B", "一括改名（Renemo風）", "batch"),
            ("I", "ファイル情報の編集", "info"),
            ("Q", "キャンセル", None),
        ])
        if choice == "batch":
            self.cmd_batch_rename()
        elif choice == "info":
            self.cmd_rename()

    def cmd_batch_rename(self):
        """Shift+R: 一括改名の窓。対象は見えているマーク、無ければ一覧の全部(".." 以外)。"""
        pane = self.active
        if self._refuse_stash("一括改名"):
            return
        if not pane.path or self._refuse_flat("一括改名"):
            return
        if pane.archive is not None:
            self.log("一括改名: 書庫内では使えません")
            return
        entries = [e for e in pane.entries if e.name in pane.marks and not e.is_parent]
        if not entries:
            entries = [e for e in pane.entries if not e.is_parent]
        if not entries:
            return
        import filer_rename
        import filer_renemo  # 使うときに読む

        items = [{"folder": pane.path, "name": e.name, "is_dir": e.is_dir, "size": e.size,
                  "mtime": e.mtime} for e in entries]
        existing = {pane.path: [e.name for e in pane.all_entries]}
        dialog = filer_renemo.RenemoWindow(
            self, items, existing, history=filer_rename.load_history(),
            on_execute=lambda pairs: self._run_batch_rename(pane, pairs),
            on_undo=lambda entry: self._undo_batch_rename(pane, entry),
            font=self.log_font)
        self.exec_dialog(dialog)
        dialog.deleteLater()

    # 実際の改名に使う関数(テストで失敗を起こすために差し替える)。
    rename_func = staticmethod(os.rename)

    def _run_batch_rename(self, pane, pairs):
        """一括改名を行う。失敗の理由か None。途中で失敗したら、それまでの分は巻き戻す。"""
        import filer_rename

        steps = filer_rename.plan_renames(pairs)
        count, error = filer_rename.execute_plan(steps, rename=self.rename_func)
        pane.reload(keep_marks=False)
        if error:
            self.log(f"一括改名: 失敗 {error}")
            return error
        changed = [(s, d) for s, d in pairs if s != d]
        try:
            filer_rename.save_history(filer_rename.add_history(filer_rename.load_history(), changed))
        except OSError as e:
            self.log(f"一括改名: 履歴を残せませんでした（{e}）")
        self.log(f"一括改名: {len(changed)}件")
        return None

    def _undo_batch_rename(self, pane, entry):
        """履歴の1件を元に戻す。戻せない理由か None。戻せたら、その記録は履歴から消す。"""
        import filer_rename

        problems = filer_rename.undo_problems(entry)
        if problems:
            return f"元に戻せません: {problems[0]}"
        steps = filer_rename.plan_renames(filer_rename.undo_pairs(entry))
        count, error = filer_rename.execute_plan(steps, rename=self.rename_func)
        pane.reload(keep_marks=False)
        if error:
            self.log(f"一括改名: 元に戻せませんでした {error}")
            return error
        history = [e for e in filer_rename.load_history() if e != entry]
        try:
            filer_rename.save_history(history)
        except OSError:
            pass
        self.log(f"一括改名: 元に戻しました（{len(entry.get('pairs', []))}件）")
        return None

    def exec_dialog(self, dialog) -> int:
        """モーダルで開く(テストではここを差し替えて、窓を出さずに操作する)。"""
        return dialog.exec()

    def _apply_file_info(self, pane, plans):
        """ファイル情報の変更を当てる。失敗の理由(文字列)か None を返す。

        名前がほかと重なるもの(一括で小文字にしたら重なった、も含む)は、何も変えずに
        理由を返す。当てている途中で失敗したら、そこまでの分は当たったまま読み直して見せ、
        理由を返す(ダイアログは開いたまま)。"""
        folder = pane.path
        for plan in plans:
            if "new" in plan:
                problem = core.validate_name(plan["new"])
                if problem:
                    return f"{plan['old']}: {problem}"
        problem = core.rename_collision(
            [e.name for e in pane.all_entries],
            {p["old"]: p["new"] for p in plans if "new" in p})
        if problem:
            return f"同じ名前があります: {problem}"
        done = []
        error = None
        for plan in plans:
            try:
                filer_shell.apply_file_info(folder, plan)
                done.append(plan)
            except OSError as e:
                error = f"{plan['old']}: {e.strerror or e}"
                break
        for plan in done:
            self.log(f"ファイル情報: {core.describe_file_info(plan)}")
        if done:
            focus = done[0].get("new", done[0]["old"])
            pane.reload(keep_marks=False, focus=focus)
        if error:
            self.log(f"ファイル情報: 失敗 {error}")
        return error

    # --- クリップボード(エクスプローラと相互に貼れる形) ---
    DROP_EFFECT_FORMAT = 'application/x-qt-windows-mime;value="Preferred DropEffect"'

    def _clip_set(self, move: bool) -> None:
        # 書庫の中のものは実ファイルではないので、エクスプローラへ渡せない。
        if self._refuse_archive("切り取り" if move else "クリップボードへコピー"):
            return
        paths = self.active.target_paths()
        if not paths:
            return
        mime = QMimeData()
        # setUrls(ローカルファイル)は Windows のクリップボードでは CF_HDROP になる。
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        mime.setData(self.DROP_EFFECT_FORMAT, QByteArray(core.encode_drop_effect(move)))
        QGuiApplication.clipboard().setMimeData(mime)
        self.log(f"クリップボードへ{'切り取り' if move else 'コピー'}: {len(paths)}件")

    def cmd_clip_copy(self):
        self._clip_set(move=False)

    def cmd_clip_cut(self):
        self._clip_set(move=True)

    def cmd_clip_paste(self):
        pane = self.active
        if pane.stash:
            self.log("貼り付け: 一時置き場へは貼り付けられません")
            return
        if not pane.path or self._refuse_archive("貼り付け", source=False, dest_pane=pane):
            return
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        if mime is None or not mime.hasUrls():
            self.log("貼り付け: クリップボードにファイルがありません")
            return
        paths = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        paths = [os.path.normpath(p) for p in paths if p]
        if not paths:
            self.log("貼り付け: クリップボードにファイルがありません")
            return
        move = core.decode_drop_effect(bytes(mime.data(self.DROP_EFFECT_FORMAT)))
        same = all(core.same_dir(os.path.dirname(p), pane.path) for p in paths)
        if move:
            if same:
                self.log("貼り付け: 同じフォルダへは移動できません")
                return
            result = filer_shell.move_files(paths, pane.path, self._hwnd(), self.extra_op_flags)
            if result == (0, False):
                # 切り取りは1回きり(エクスプローラと同じ)。残しておくと、移動済みの
                # パスをもう一度貼ろうとしてエラーになる。
                clipboard.clear()
            self._after_operation("貼り付け(移動)", result, paths, pane.path)
        else:
            # 同じフォルダへのコピーは「- コピー」の名前で複製する(エクスプローラと同じ)。
            extra = filer_shell.FOF_RENAMEONCOLLISION if same else 0
            result = filer_shell.copy_files(paths, pane.path, self._hwnd(),
                                            self.extra_op_flags | extra)
            self._after_operation("貼り付け(複写)", result, paths, pane.path)

    # --- 書庫からの展開 ---
    def _archive_plan(self, entries, pane=None):
        """窓(既定はアクティブ窓。書庫の中)の entries を展開する計画。(書庫の索引, 計画)。"""
        archive, inner = (pane or self.active).archive
        # 一覧を読んだときに作った索引がそのまま使われる(書庫が変わっていれば読み直す)。
        index = filer_archive.get_index(archive)
        return index, filer_archive.plan_extract(index, inner, [e.name for e in entries])

    def _extract_to_dir(self, entries, dest: str) -> None:
        """書庫の中のマーク(無ければカーソル)を dest へ展開する。フォルダは中身ごと。
        上書きになるものは1件ずつ尋ねる(「残りも同じ」で以降を省ける)。"""
        if self._extracting is not None:
            self.log("展開: ほかの展開が終わるまで待ってください")
            return
        index, plan = self._archive_plan(entries)
        if not plan:
            self.log("展開: 対象がありません")
            return
        skip = set()
        apply_all = None
        conflicts = filer_archive.find_conflicts(plan, dest)
        for number, rel in enumerate(conflicts):
            if apply_all is not None:
                decision = apply_all
            else:
                decision, same_for_rest = self.ask_overwrite(rel, len(conflicts) - number - 1)
                if same_for_rest:
                    apply_all = decision
            if decision == CANCEL:
                self.log("展開: 取り消しました")
                return
            if decision == SKIP:
                skip.add(rel)
        password = None
        if filer_archive.needs_password(index, plan):
            password = self.ask_password(os.path.basename(index.path))
            if password is None:
                self.log("展開: 取り消しました（パスワード）")
                return

        def done(result):
            self._report_extract(result, dest)
            self.left.reload()
            self.right.reload()

        self._start_extract(index, plan, dest, skip, password, "展開", done)

    def _extract_to_temp(self, entries, on_paths, pane=None) -> None:
        """書庫の中のもの(ファイルもフォルダも)を一時フォルダへ取り出し、取り出せたパスで
        on_paths を呼ぶ(Enter / E / V / Alt+W)。一時フォルダはファイラを閉じるとき・
        次の起動で片付く。pane を省くとアクティブ窓。"""
        if self._extracting is not None:
            self.log("展開: ほかの展開が終わるまで待ってください")
            return
        index, plan = self._archive_plan(entries, pane)
        if not plan:
            return
        password = None
        if filer_archive.needs_password(index, plan):
            password = self.ask_password(os.path.basename(index.path))
            if password is None:
                return
        dest = filer_archive.make_temp_dir()

        def done(result):
            if result.errors or result.cancelled:
                self._report_extract(result, dest)
            paths = [os.path.join(dest, e.name) for e in entries
                     if os.path.exists(os.path.join(dest, e.name))]
            if paths:
                on_paths(paths)

        self._start_extract(index, plan, dest, (), password, "取り出し", done)

    def _start_extract(self, index, plan, dest, skip, password, title, on_done) -> None:
        """展開を別スレッドで走らせ、進み具合を QProgressDialog に出す(中止できる)。"""
        total = sum(1 for p in plan if not p.is_dir)

        def run(progress, cancel):
            try:
                return filer_archive.extract(index, plan, dest, skip=skip, password=password,
                                             progress=progress, cancel=cancel)
            except Exception as e:  # noqa: BLE001  何が来ても結果として返す
                return filer_archive.ExtractResult(errors=[f"{type(e).__name__}: {e}"])

        self._start_job(title, os.path.basename(index.path), total, run, on_done)

    def _start_job(self, title, label, total, run, on_done) -> None:
        """時間のかかる処理(展開・圧縮)を別スレッドで走らせ、進み具合を QProgressDialog に
        出す(中止できる)。run(progress, cancel) は読み取りスレッドで呼ばれ、結果を返す。
        progress(済んだ数, 全体, 名前) で進み具合を知らせる。終わったら on_done(結果) を
        UI スレッドで呼ぶ。同時に走らせるのは1つだけ(self._extracting)。"""
        cancel = threading.Event()
        dialog = QProgressDialog(f"{title}: {label}", "中止", 0, max(total, 1), self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModal)
        # 一瞬で終わる小さな展開でまで窓をちらつかせない。
        dialog.setMinimumDuration(400)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.canceled.connect(cancel.set)
        self._extracting = {"cancel": cancel, "dialog": dialog, "on_done": on_done,
                            "title": title}
        self.set_status_state(f"{title}中")
        bridge = self.bridge

        def progress(done, all_, name):
            try:
                bridge.progress.emit((done, all_, name))
            except RuntimeError:
                pass

        def worker():
            try:
                result = run(progress, cancel)
            except Exception as e:  # noqa: BLE001  何が来ても結果として返す
                result = e
            try:
                bridge.extracted.emit(result)
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True, name="filer-job").start()

    def _on_extract_progress(self, payload) -> None:
        try:
            job = self._extracting
            if job is None:
                return
            done, total, name = payload
            dialog = job["dialog"]
            dialog.setMaximum(max(total, 1))
            dialog.setValue(min(done, max(total, 1)))
            dialog.setLabelText(f"{job['title']}: {done}/{total}\n{name}")
            self.set_status_state(f"{job['title']}中 {done}/{total}")
        except Exception:  # noqa: BLE001
            _log_exception("extractProgress")

    def _on_extracted(self, result) -> None:
        job = self._extracting
        self._extracting = None
        try:
            self.set_status_state("Ready.")
            if job is None:
                return
            try:
                job["dialog"].close()
                job["dialog"].deleteLater()
            except Exception:  # noqa: BLE001
                pass
            job["on_done"](result)
        except Exception:  # noqa: BLE001
            self.log(f"エラー: {_log_exception('extracted')}")

    def _report_extract(self, result, dest) -> None:
        text = f"展開: {result.written}件 → {dest}"
        if result.skipped:
            text += f"（スキップ {result.skipped}件）"
        if result.cancelled:
            text += "（中止しました）"
        self.log(text)
        for error in result.errors[:5]:
            self.log(f"展開: 失敗 {error}")
        if len(result.errors) > 5:
            self.log(f"展開: ほか {len(result.errors) - 5}件失敗")

    # --- 窓の大きさ・字の大きさ ---
    def cmd_split_left(self):
        self.split_ratio = core.step_split(self.split_ratio, -1)
        self.apply_layout()

    def cmd_split_right(self):
        self.split_ratio = core.step_split(self.split_ratio, 1)
        self.apply_layout()

    def cmd_log_grow(self):
        self.log_lines = core.clamp_log_lines(self.log_lines + 1, self._max_log_lines())
        self.apply_layout()

    def cmd_log_shrink(self):
        self.log_lines = core.clamp_log_lines(self.log_lines - 1, self._max_log_lines())
        self.apply_layout()

    def _change_font(self, step: int) -> None:
        """アクティブ窓の字だけを大きく/小さくする(あふｗと同じく左右別々)。"""
        pane = self.active
        size = core.clamp_font_size(pane.pane_font.pointSize() + step)
        if size == pane.pane_font.pointSize():
            return
        font = QFont(pane.pane_font)
        font.setPointSize(size)
        pane.apply_font(font)
        pane.set_cursor(pane.cursor_row())  # カーソル行を見える所に保つ

    def cmd_font_smaller(self):
        self._change_font(-1)

    def cmd_font_larger(self):
        self._change_font(1)

    # --- ソート ---
    def _side_label(self) -> str:
        return "左" if self.active_side == "left" else "右"

    def cmd_sort_dialog(self):
        spec = self.ask_sort(f"ソート({self._side_label()})", self.active.sort)
        if spec is not None:
            self.active.set_sort(spec)

    def cmd_sort_reverse(self):
        spec = dict(self.active.sort)
        spec["descending"] = not spec["descending"]
        self.active.set_sort(spec)
        order = "降順" if spec["descending"] else "昇順"
        self.log(f"ソート: {core.SORT_LABELS[spec['kind']]} {order}")

    # --- 同名ファイル比較マーク ---
    def cmd_compare_mark(self):
        pane, other = self.active, self.other
        if not pane.path or not other.path:
            return
        if core.same_dir(pane.path, other.path):
            # あふｗはダイアログも出さずに何もしない。黙っていると効いたか分からないので一言だけ。
            self.log("比較マーク: 左右が同じフォルダです")
            return
        section = _section(self.app_settings)
        remembered = bool(section.get("compare_include_dirs"))
        choice = self.ask_compare(f"同名ファイル比較マーク({self._side_label()})", remembered)
        if choice is None:
            return
        if remembered != choice["include_dirs"]:
            save_values(self.app_settings, self.settings_path,
                        {"compare_include_dirs": choice["include_dirs"]})
        try:
            tolerance = float(section.get("time_tolerance_sec", core.DEFAULT_TIME_TOLERANCE))
        except (TypeError, ValueError):
            tolerance = core.DEFAULT_TIME_TOLERANCE
        names = core.compare_marks(
            pane.entries, other.entries, choice["size"], choice["time"],
            include_dirs=choice["include_dirs"], strip_ext=choice["strip_ext"],
            tolerance=tolerance)
        if choice["unmark"]:
            removed = pane.marks & names
            pane.marks.difference_update(names)
            self.log(f"比較マーク解除: {len(removed)}件")
        else:
            if pane.marks:
                how = self.ask_overlap()
                if how is None:
                    return
                if how == "clear":
                    pane.marks.clear()
            pane.marks.update(names)
            self.log(f"比較マーク: {len(names)}件")
        pane.model.all_changed()
        pane.update_footer()

    def cmd_sync_copy(self):
        """フォルダ同期コピー(Shift+W)。今いるフォルダ同士で、相手に無いものと
        差分のあるファイルを一方向にコピーする。削除はしない(コピーのみ)。
        両方にあるフォルダの中身までは見ない(W の比較と同じ範囲)。"""
        label = "同期コピー"
        if self.active.archive is not None or self.other.archive is not None:
            self.log(f"{label}: 書庫の中では使えません")
            return
        if (self.active.flat or self.other.flat
                or self.active.flat_loading or self.other.flat_loading):
            self.log(f"{label}: フラット表示・検索結果では使えません")
            return
        if self.active.stash or self.other.stash:
            self.log(f"{label}: 一時置き場では使えません")
            return
        left, right = self.left, self.right
        if not left.path or not right.path:
            return
        if core.same_dir(left.path, right.path):
            self.log(f"{label}: 左右が同じフォルダです")
            return
        section = _section(self.app_settings)
        try:
            tolerance = float(section.get("time_tolerance_sec", core.DEFAULT_TIME_TOLERANCE))
        except (TypeError, ValueError):
            tolerance = core.DEFAULT_TIME_TOLERANCE

        def plan(src, dst):
            entries = core.sync_sources(src.entries, dst.entries, tolerance=tolerance)
            total = sum(e.size for e in entries if not e.is_dir)
            dirs = sum(1 for e in entries if e.is_dir)
            return entries, total, dirs

        plans = [plan(left, right), plan(right, left)]
        choice = self.ask_sync(plans, initial=1 if self.active is right else 0)
        if choice is None:
            return
        src, dst = (left, right) if choice == 0 else (right, left)
        entries = plans[choice][0]
        if not entries:
            self.log(f"{label}: 送るものがありません")
            return
        paths = [os.path.join(src.path, e.name) for e in entries]
        self.set_status_state(f"{label}中 {len(paths)}件")
        QApplication.processEvents()
        try:
            result = filer_shell.copy_files(paths, dst.path, self._hwnd(),
                                            self.extra_op_flags)
        finally:
            try:
                self.set_status_state("Ready.")
            except Exception:  # noqa: BLE001  finally の中で投げない(CLAUDE.md)
                pass
        self._after_operation(label, result, paths, dst.path,
                              dest_pane=dst, dest_focus=entries[0].name)

    # --- 各種設定一時切替(Shift+Z。あふｗの S-Z) ---
    def cmd_settings_menu(self):
        menu, chosen = self.build_menu("各種設定一時切替", [
            ("H", "隠しファイルを表示する", "hidden", self.show_hidden),
            ("S", "システムファイルを表示する", "system", self.show_system),
        ])
        choice = self._exec_menu(menu, chosen)
        if choice is not None:
            self.toggle_attr_filter(choice)

    def toggle_attr_filter(self, which: str) -> None:
        """隠し/システムファイルの表示を切り替えて、両窓を並べ直す(カーソルは同じ項目に
        付いていく)。属性は一覧を読んだときに取ってあるので読み直しは要らない。"""
        if which == "hidden":
            self.show_hidden = not self.show_hidden
            label, on = "隠しファイル", self.show_hidden
        else:
            self.show_system = not self.show_system
            label, on = "システムファイル", self.show_system
        save_values(self.app_settings, self.settings_path,
                    {"show_hidden": self.show_hidden, "show_system": self.show_system})
        for pane in (self.left, self.right):
            pane.refilter()
        self.log(f"設定: {label}を{'表示する' if on else '表示しない'}")

    # --- ドラッグ(ファイラから外のアプリへ) ---
    def drag_mime(self, pane):
        """ドラッグで渡す QMimeData。運ぶものが無ければ None(ドラッグを始めない)。

        運ぶのはマークしたものだけ。マークが無ければ何もしない(マウスでは選ばない)。
        setUrls(ローカルファイル)は Windows では CF_HDROP になり、エクスプローラや
        メールにそのまま落とせる。"""
        paths = core.drag_paths(pane.path or "", pane.entries, pane.marks)
        if not paths:
            return None
        if pane.archive is not None:
            self.log("書庫内からはドラッグできません")
            return None
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        return mime

    def start_drag(self, pane) -> None:
        mime = self.drag_mime(pane)
        if mime is None:
            return
        drag = QDrag(pane.view)
        drag.setMimeData(mime)
        # 既定はコピー。落とし先が移動を選ぶこともあるので、終わったら両窓を読み直す。
        result = drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        count = len(mime.urls())
        if result == Qt.MoveAction:
            self.log(f"ドラッグ: {count}件を移動しました")
        elif result == Qt.CopyAction:
            self.log(f"ドラッグ: {count}件を渡しました")
        self.left.reload(keep_marks=True)
        self.right.reload(keep_marks=True)

    # --- ZIP 圧縮(P。反対窓に作る) ---
    def cmd_zip_create(self):
        src, dst = self.active, self.other
        if self._refuse_archive("圧縮", source=True, dest_pane=dst):
            return
        targets = src.targets()
        if not targets or not src.path or not dst.path:
            return
        if self._extracting is not None:
            self.log("圧縮: ほかの処理が終わるまで待ってください")
            return
        name = self.ask_text("圧縮", f"反対窓（{dst.path}）に作る ZIP の名前",
                             core.default_zip_name(src.path, targets))
        if name is None:
            return
        name = name.strip()
        problem = core.validate_name(name)
        if problem:
            self.log(f"圧縮: {problem}")
            return
        dest = os.path.join(dst.path, name)
        if os.path.exists(dest):
            if os.path.isdir(dest):
                self.log(f"圧縮: 同じ名前のフォルダがあります: {name}")
                return
            if not self.confirm("圧縮", f"{name} は既にあります。上書きしますか？",
                                default_yes=False):
                return
        folder = src.path
        names = [e.name for e in targets]

        def run(progress, cancel):
            return filer_archive.create_zip(folder, names, dest, progress, cancel)

        def done(result):
            self._report_zip(result, dest)
            dst.reload(focus=name)

        self._start_job("圧縮", name, len(names), run, done)

    def _report_zip(self, result, dest) -> None:
        if isinstance(result, Exception):
            self.log(f"圧縮: 失敗しました（{type(result).__name__}: {result}）")
            return
        if result.cancelled:
            self.log("圧縮: 中止しました（作りかけの ZIP は残していません）")
            return
        if result.error:
            self.log(f"圧縮: 失敗しました（{result.error}。作りかけの ZIP は残していません）")
            return
        self.log(f"圧縮: {result.files}ファイル {result.folders}フォルダ"
                 f"（{core.format_bytes(result.source_bytes)}）→ {os.path.basename(dest)}"
                 f"（{core.format_bytes(result.zip_bytes)}）")
        for error in result.skipped[:5]:
            self.log(f"圧縮: 入れられませんでした {error}")
        if len(result.skipped) > 5:
            self.log(f"圧縮: ほか {len(result.skipped) - 5}件入れられませんでした")

    # --- WinMerge で比較(Alt+W) ---
    def _marked_entries(self, pane) -> list:
        return [e for e in pane.entries if e.name in pane.marks and not e.is_parent]

    def cmd_winmerge(self):
        pair = core.winmerge_pair(self._marked_entries(self.left),
                                  self._marked_entries(self.right))
        if pair is None:
            self.log(f"比較: {core.WINMERGE_HINT}")
            return
        exe = core.find_winmerge(self.winmerge_setting, registry=core.registry_winmerge_paths)
        if exe is None:
            self.log("比較: WinMerge が見つかりません（filer.winmerge で指定できます）")
            return
        items = [(self.pane(side), entry) for side, entry in pair]
        displays = [os.path.join(pane.path, entry.name) for pane, entry in items]
        resolved = [None, None]

        def step(i):
            # 書庫の中のものは一時フォルダへ取り出してから渡す(1つずつ順に)。
            if i == len(items):
                self.launch_winmerge(exe, displays, resolved)
                return
            pane, entry = items[i]
            if pane.archive is None:
                resolved[i] = displays[i]
                step(i + 1)
                return

            def got(paths, i=i):
                resolved[i] = paths[0]
                step(i + 1)

            self._extract_to_temp([entry], got, pane)

        step(0)

    def launch_winmerge(self, exe, displays, paths) -> None:
        """WinMerge を起こす。引数はリストで渡す(空白を含むパスで壊れない)。左右の表示名
        (/dl /dr)はファイラ上のパス(書庫の中なら書庫の中のパス)。"""
        argv = [exe, "/dl", displays[0], "/dr", displays[1], paths[0], paths[1]]
        try:
            subprocess.Popen(argv, close_fds=True)
            self.log(f"比較: {displays[0]} ⇔ {displays[1]}")
        except OSError as e:
            self.log(f"比較: WinMerge を起動できません（{e}）")

    # --- 閉じる ---
    def cmd_quit(self):
        self.close()

    # --- 入力の口(テストではここを差し替える) ---
    def ask_text(self, title: str, label: str, text: str = "", select: int = None):
        """1行入力。キャンセルなら None。select を渡すと先頭からその文字数だけを選んだ
        状態で出す(改名で拡張子の手前まで選ぶため。QInputDialog.getText では選べない)。"""
        dialog = QInputDialog(self)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setTextValue(text)
        edit = dialog.findChild(QLineEdit)
        if edit is not None and select is not None:
            # 表示時に全選択されるので、その後で選び直す。
            QTimer.singleShot(0, lambda: edit.setSelection(0, select))
        if dialog.exec() != QInputDialog.Accepted:
            return None
        return dialog.textValue()

    def ask_overwrite(self, rel: str, remaining: int):
        """展開先に同じ名前があるとき。(OVERWRITE|SKIP|CANCEL, 残りも同じにするか)。"""
        box = QMessageBox(self)
        box.setWindowTitle("展開")
        box.setText(f"同じ名前があります。上書きしますか？\n\n{rel}")
        overwrite = box.addButton("上書き(&O)", QMessageBox.AcceptRole)
        skip = box.addButton("スキップ(&S)", QMessageBox.RejectRole)
        cancel = box.addButton("取消", QMessageBox.DestructiveRole)
        box.setDefaultButton(skip)
        box.setEscapeButton(cancel)
        check = None
        if remaining > 0:
            check = QCheckBox(f"残り {remaining}件にも同じ扱いをする")
            box.setCheckBox(check)
        box.exec()
        clicked = box.clickedButton()
        decision = OVERWRITE if clicked is overwrite else SKIP if clicked is skip else CANCEL
        return decision, bool(check is not None and check.isChecked())

    def ask_password(self, archive_name: str):
        """書庫のパスワード。取消なら None。"""
        text, ok = QInputDialog.getText(self, "パスワード", f"{archive_name} のパスワード",
                                        QLineEdit.Password)
        return text if ok else None

    def confirm(self, title: str, text: str, default_yes: bool = True) -> bool:
        default = QMessageBox.Yes if default_yes else QMessageBox.No
        answer = QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No, default)
        return answer == QMessageBox.Yes

    def ask_sort(self, title: str, spec):
        """ソートのダイアログ。決めた並べ方か、取消なら None。"""
        dialog = SortDialog(self, title, spec, self.colors, self.list_font)
        return dialog.result_value if dialog.exec() == QDialog.Accepted else None

    def ask_compare(self, title: str, include_dirs: bool):
        """同名ファイル比較マークの条件。取消なら None。"""
        dialog = CompareDialog(self, title, include_dirs, self.colors, self.list_font)
        return dialog.result_value if dialog.exec() == QDialog.Accepted else None

    def ask_sync(self, plans, initial: int):
        """フォルダ同期コピーの確認。方向(0=左→右 / 1=右→左)か、取消なら None。"""
        dialog = SyncDialog(self, "フォルダ同期コピー", plans, initial,
                            self.colors, self.list_font)
        return dialog.result_value if dialog.exec() == QDialog.Accepted else None

    def ask_overlap(self):
        """既にマークがあるときの扱い。"clear" / "add" / None(取消)。"""
        box = QMessageBox(self)
        box.setWindowTitle("同名ファイル比較マーク")
        box.setText("既にマークがあります。")
        clear = box.addButton("クリアしてからマーク(&C)", QMessageBox.AcceptRole)
        add = box.addButton("重ねてマーク(&A)", QMessageBox.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(clear)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is clear:
            return "clear"
        if clicked is add:
            return "add"
        return None

    def build_menu(self, title: str, items):
        """文字キーは選択のみ・Enter で実行する小さなメニューを組む。(_ChooserMenu, {QAction: 値})。

        items は (文字, 表示, 値) か (文字, 表示, 値, チェック状態)。チェック状態を渡した
        項目はチェック付きで出す(Shift+Z の ON/OFF)。None は区切り線。先頭の項目を選んだ
        状態にしておく(開いてすぐ Enter で先頭が実行できるように)。"""
        menu = _ChooserMenu(self)
        menu.setTitle(title)
        chosen = {}
        for item in items:
            if item is None:
                menu.addSeparator()
                continue
            key, label, value = item[:3]
            action = menu.addAction(f"&{key}:{label}")
            action.setProperty("filer_menu_key", key)
            if len(item) > 3:
                action.setCheckable(True)
                action.setChecked(bool(item[3]))
            chosen[action] = value
        first = next((a for a in menu.actions() if a.isEnabled() and not a.isSeparator()), None)
        if first is not None:
            menu.setActiveAction(first)
        return menu, chosen

    def _menu_pos(self):
        view = self.active.view
        rect = view.visualRect(view.currentIndex())
        return view.viewport().mapToGlobal(
            rect.bottomLeft() if rect.isValid() else view.rect().center())

    def _exec_menu(self, menu, chosen):
        first = menu.activeAction()
        # 出した直後にも選び直す。popup の中で選択が外れる版の Qt があっても先頭に戻るように。
        if first is not None:
            QTimer.singleShot(0, lambda: menu.setActiveAction(first) if menu.isVisible() else None)
        return chosen.get(menu.exec(self._menu_pos()))

    def choose_menu(self, title: str, items):
        """文字キーは選択のみ・Enter で実行する小さなメニュー。選んだ値か None。"""
        menu, chosen = self.build_menu(title, items)
        return self._exec_menu(menu, chosen)

    def choose_menu_deletable(self, title: str, items):
        """choose_menu の Del 付き。("choose", 値) か、Del を押した項目の ("delete", 値)。
        何も選ばなければ ("choose", None)。"""
        menu, chosen = self.build_menu(title, items)
        menu.deletable = True
        value = self._exec_menu(menu, chosen)
        if menu.delete_action is not None:
            return "delete", chosen.get(menu.delete_action)
        return "choose", value

    def ask_everything(self, text: str, scope_all: bool):
        """Everything 検索の入力窓。(検索語, 全体から探すか) か、取消なら None。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("Everything 検索")
        layout = QVBoxLayout(dialog)
        label = QLabel("探す名前（空白区切りで AND・\"...\" で語句・ext:pdf など Everything の書き方）")
        layout.addWidget(label)
        edit = QLineEdit(text)
        edit.selectAll()
        layout.addWidget(edit)
        check = QCheckBox("全体から探す(&A)（オフならアクティブ窓のフォルダより下だけ）")
        check.setChecked(bool(scope_all))
        layout.addWidget(check)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        edit.setFocus()
        if dialog.exec() != QDialog.Accepted:
            return None
        return edit.text(), check.isChecked()


# ---------------------------------------------------------------
# 小さなダイアログ(ソート / 同名ファイル比較マーク)
# ---------------------------------------------------------------
class _KeyDialog(QDialog):
    """キーだけで操る小さなダイアログの土台。行は QLabel で並べ、カーソル行を色で示す。

    Qt の既定のキー操作(Tab のフォーカス移動・Enter の既定ボタン)は使わないので、
    event() で KeyPress を先に取る。スロット内の例外で落ちないよう、ここで必ず受ける。"""

    def __init__(self, parent, title, colors, font):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.colors = colors
        self.result_value = None
        self.setObjectName("filerDialog")
        self.setStyleSheet(f"#filerDialog {{ background-color: {colors['background']}; }}")
        self._font = font
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(2)
        self.row = 0

    def add_label(self, text="", hint=False) -> QLabel:
        label = QLabel(text)
        label.setFont(self._font)
        label.setTextFormat(Qt.PlainText)
        color = self.colors["footer_text"] if hint else self.colors["text"]
        label.setStyleSheet(f"color: {color}; padding: 2px 8px;")
        self._layout.addWidget(label)
        return label

    def paint_rows(self, rows, framed=None) -> None:
        """rows は [(QLabel, 文字列)]。カーソル行は背景色、framed の行は枠で示す
        (あふｗのソートで「いまの種類」に枠が付くのと同じ)。"""
        c = self.colors
        for index, (label, text) in enumerate(rows):
            label.setText(text)
            style = f"color: {c['text']}; padding: 2px 8px;"
            if index == self.row:
                style += f" background-color: {c['dialog_cursor']};"
            border = c["header_active"] if index == framed else "transparent"
            style += f" border: 1px solid {border};"
            label.setStyleSheet(style)

    def event(self, event):
        if event.type() == QEvent.KeyPress:
            try:
                self.on_key(event)
            except Exception:  # noqa: BLE001
                _log_exception("dialog key")
            return True
        return super().event(event)

    def on_key(self, event) -> None:  # 派生側で書く
        pass

    @staticmethod
    def letter_of(event) -> str:
        key = int(event.key())
        return chr(key) if int(Qt.Key_A) <= key <= int(Qt.Key_Z) else ""


class SortDialog(_KeyDialog):
    """あふｗの「ソート」。↑↓で種類、←→/Tab で昇順・降順、G/C でチェック、
    先頭文字(N/F/E/S/T/A/X)を押すと即決定(あふｗと同じ)、Enter でも決定、Esc で取消。"""

    def __init__(self, parent, title, spec, colors, font):
        super().__init__(parent, title, colors, font)
        self.spec = core.normalize_sort(spec)
        self.kind_labels = [self.add_label() for _ in core.SORT_KINDS]
        self.order_label = self.add_label()
        self.check_labels = [self.add_label(), self.add_label()]
        self.add_label("↑↓:選択  ←→/Tab:昇降  Enter:決定  Esc:取消", hint=True)
        self.row = core.SORT_KINDS.index(self.spec["kind"])
        self.refresh()

    def refresh(self) -> None:
        letters = {v: k for k, v in core.SORT_SHORTCUTS.items()}
        rows = [(label, f"{letters[kind]}: {core.SORT_LABELS[kind]}")
                for label, kind in zip(self.kind_labels, core.SORT_KINDS)]
        self.paint_rows(rows, framed=core.SORT_KINDS.index(self.spec["kind"]))
        if self.spec["descending"]:
            order = " 昇順 / 〔降順〕"
        else:
            order = "〔昇順〕/  降順"
        self.order_label.setText(f"順序:  {order}")
        mark = lambda on: "[x]" if on else "[ ]"  # noqa: E731
        self.check_labels[0].setText(f"{mark(self.spec['dirs_first'])} G: フォルダを上に集める")
        self.check_labels[1].setText(f"{mark(self.spec['ignore_case'])} C: 大文字小文字区別無し")

    def _decide(self, kind) -> None:
        self.spec["kind"] = kind
        self.result_value = dict(self.spec)
        self.accept()

    def on_key(self, event) -> None:
        key = event.key()
        letter = self.letter_of(event)
        if key == Qt.Key_Escape:
            self.reject()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._decide(core.SORT_KINDS[self.row])
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            step = -1 if key == Qt.Key_Up else 1
            self.row = (self.row + step) % len(core.SORT_KINDS)
        elif key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Tab, Qt.Key_Backtab):
            self.spec["descending"] = not self.spec["descending"]
        elif letter == "G":
            self.spec["dirs_first"] = not self.spec["dirs_first"]
        elif letter == "C":
            self.spec["ignore_case"] = not self.spec["ignore_case"]
        elif letter in core.SORT_SHORTCUTS:
            # 文字キーは押した時点で決定(あふｗと同じ。本人の指定で即時反映)。
            self._decide(core.SORT_SHORTCUTS[letter])
            return
        self.refresh()


class CompareDialog(_KeyDialog):
    """あふｗの「同名ファイル比較マーク」の条件。↑↓で行、←→で選択肢、Space でチェック、
    A/N/S/T/E は条件プリセットの選択だけ、Enter で決定、Esc で取消。
    Shift を押しながら決定するとマークを外す。"""

    ROW_SIZE, ROW_TIME, ROW_DIRS, ROW_EXT = range(4)

    def __init__(self, parent, title, include_dirs, colors, font):
        super().__init__(parent, title, colors, font)
        self.size_index = core.SIZE_CHOICES.index("any")
        self.time_index = core.TIME_CHOICES.index("any")
        self.include_dirs = bool(include_dirs)
        self.add_label("E:除拡  A:全同  N:全異  S:大同  T:時同", hint=True)
        self.rows = [self.add_label() for _ in range(4)]
        self.add_label("Shift を押しながら決定するとマークを外す", hint=True)
        self.refresh()

    @staticmethod
    def _options(labels, chosen) -> str:
        return " ".join(f"〔{t}〕" if i == chosen else f" {t} " for i, t in enumerate(labels))

    def refresh(self) -> None:
        texts = [
            "サイズ         " + self._options(core.SIZE_LABELS, self.size_index),
            "タイムスタンプ " + self._options(core.TIME_LABELS, self.time_index),
            f"{'[x]' if self.include_dirs else '[ ]'} フォルダ同士も同名があればマーク",
            "除：拡張子（[大時]比較無効）",
        ]
        self.paint_rows(list(zip(self.rows, texts)))

    def _decide(self, size, time_, strip_ext, unmark) -> None:
        self.result_value = {"size": size, "time": time_, "include_dirs": self.include_dirs,
                             "strip_ext": strip_ext, "unmark": unmark}
        self.accept()

    def on_key(self, event) -> None:
        key = event.key()
        shift = bool(event.modifiers() & Qt.ShiftModifier)
        letter = self.letter_of(event)
        if key == Qt.Key_Escape:
            self.reject()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._decide(core.SIZE_CHOICES[self.size_index], core.TIME_CHOICES[self.time_index],
                         self.row == self.ROW_EXT, shift)
            return
        if letter in core.COMPARE_SHORTCUTS:
            # 文字キーは条件プリセットの選択だけ。決定は Enter(メニューと同じ作法)。
            # 除拡張子は「最下行を選んで決定したか」で表すので、プリセットに
            # 合わせてカーソル行も動かす(外すプリセットなら最下行から離す)。
            size, time_, strip_ext = core.COMPARE_SHORTCUTS[letter]
            self.size_index = core.SIZE_CHOICES.index(size)
            self.time_index = core.TIME_CHOICES.index(time_)
            if strip_ext:
                self.row = self.ROW_EXT
            elif self.row == self.ROW_EXT:
                self.row = self.ROW_SIZE
            self.refresh()
            return
        if key in (Qt.Key_Up, Qt.Key_Down):
            self.row = (self.row + (-1 if key == Qt.Key_Up else 1)) % len(self.rows)
        elif key in (Qt.Key_Left, Qt.Key_Right):
            step = -1 if key == Qt.Key_Left else 1
            if self.row == self.ROW_SIZE:
                self.size_index = (self.size_index + step) % len(core.SIZE_CHOICES)
            elif self.row == self.ROW_TIME:
                self.time_index = (self.time_index + step) % len(core.TIME_CHOICES)
        elif key == Qt.Key_Space:
            self.include_dirs = not self.include_dirs
        self.refresh()


class SyncDialog(_KeyDialog):
    """フォルダ同期コピー(Shift+W)の確認。方向を ←→ で選ぶだけ(選択は選択、
    実行は Enter。メニューと同じ作法)。削除はしないことと、両方にある
    フォルダの中身は対象外であることを、押す前にここで明示する。"""

    LABELS = ("左 → 右", "右 → 左")

    def __init__(self, parent, title, plans, initial, colors, font):
        super().__init__(parent, title, colors, font)
        # plans は [(送るエントリ, 合計バイト, フォルダ件数)] × 2(0=左→右、1=右→左)
        self.plans = plans
        self.index = 1 if initial else 0
        self.rows = [self.add_label(), self.add_label()]
        self.add_label("削除はしません。両方にあるフォルダの中身は対象外です", hint=True)
        self.add_label("Enter: コピー実行  Esc: 取消", hint=True)
        self.refresh()

    def refresh(self) -> None:
        entries, total, dirs = self.plans[self.index]
        options = " ".join(
            f"〔{label}〕" if i == self.index else f" {label} "
            for i, label in enumerate(self.LABELS))
        if entries:
            detail = (f"送るもの     {len(entries)}件"
                      f"（合計 {core.format_bytes(total)}、うちフォルダ {dirs}件）")
        else:
            detail = "送るもの     ありません（差分なし）"
        self.paint_rows([(self.rows[0], "方向         " + options),
                         (self.rows[1], detail)])

    def on_key(self, event) -> None:
        key = event.key()
        if key == Qt.Key_Escape:
            self.reject()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.result_value = self.index
            self.accept()
            return
        if key in (Qt.Key_Left, Qt.Key_Right):
            self.index = 1 - self.index
            self.refresh()


# ---------------------------------------------------------------
# 起動
# ---------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def acquire_single_instance() -> bool:
    """先着なら True。既に開いていれば False(CLAUDE.md「単一起動の判定」)。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, filer_launch.MUTEX_NAME)
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except OSError:
        return True  # 判定できないなら起動を通す
    if not handle:
        return True
    if already:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)
        return False
    _mutex_handle = handle
    return True


def release_single_instance() -> None:
    """握っているミューテックスを手放す(再起動で後継に譲るため)。"""
    global _mutex_handle
    if not _mutex_handle:
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(_mutex_handle)
    except OSError:
        pass
    _mutex_handle = None


def _apply_taskbar_identity(window) -> None:
    """ピン留めされても、あっふ のアイコンと起動コマンドで戻れるように
    窓へ再起動情報を書く。icons/affx.ico はピンが参照する実ファイルで、
    無ければ描画して置く(失敗しても起動は続ける)。"""
    try:
        icon_path = Path(__file__).resolve().parent / "icons" / "affx.ico"
        blob = affx_icon.ico_bytes()
        if not icon_path.exists() or icon_path.read_bytes() != blob:
            # 無い、または中身が古い(作り方が変わった)ときは作り直す
            icon_path.write_bytes(blob)
        command = (f'"{pythonw_executable()}" '
                   f'"{Path(__file__).resolve()}"')
        filer_shell.set_taskbar_identity(
            int(window.winId()), "traytools.affx", command,
            str(icon_path), "あっふ")
    except Exception:  # noqa: BLE001
        _log_exception("taskbar")


def main() -> int:
    # タスクバーで pythonw の汎用アプリとしてまとめられ、汎用アイコンの
    # ままになるのを防ぐ。専用のアプリ ID を名乗ると窓のアイコンが勝つ。
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "traytools.affx")
    except Exception:  # noqa: BLE001  名乗れなくても起動は続ける
        pass
    app = QApplication(sys.argv)
    # 窓とタスクバーのアイコン(本家 AfxW 風のドット明朝。affx_icon が持つ)。
    app.setWindowIcon(affx_icon.qicon())
    if not acquire_single_instance():
        # 先客が居る。前面に出してもらって引き返す。
        try:
            filer_launch.send_show()
        except Exception:  # noqa: BLE001
            _log_exception("send_show")
        return 0
    try:
        app_settings = settings_module.load_settings()
        window = FilerWindow(app_settings, str(settings_module.SETTINGS_PATH))
        window.listen()
        if window._start_maximized:
            window.showMaximized()
        else:
            window.show()
        window.start()
        _apply_taskbar_identity(window)
        window.bring_to_front()
    except Exception:  # noqa: BLE001
        _log_exception("open")
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
