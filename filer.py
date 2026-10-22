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
import threading
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
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPen
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

import filer_core as core
import filer_launch
import filer_shell
import settings as settings_module

SETTINGS_SECTION = "filer"
ERROR_LOG_PATH = Path(__file__).resolve().parent / "filer_error.log"

# 落ち着いた暗色系の既定。settings.json の filer.colors に同じ名前で書けば1色ずつ上書きできる。
DEFAULT_COLORS = {
    "background": "#1b1d21",
    "text": "#d0d4da",
    "directory": "#8cc4ff",
    "hidden": "#6b717a",          # 隠し/システム属性。表示はするが薄く
    "cursor": "#2f4a6b",          # アクティブ窓のカーソル行
    "cursor_inactive": "#5a6270",  # 非アクティブ窓のカーソル(下線だけ)
    "mark": "#d8b85a",            # マークした行(色反転で目立たせる)
    "mark_text": "#1b1d21",
    "mark_cursor": "#f0d890",     # マーク行にカーソルが乗っているとき
    "header_active": "#2563eb",
    "header_active_text": "#ffffff",
    "header_inactive": "#2b2f36",
    "header_inactive_text": "#9aa0a8",
    "footer_text": "#9aa0a8",
    "isearch": "#ffd400",
    "log_background": "#141518",
    "log_text": "#b8bec6",
}

DEFAULT_FONT_FAMILIES = ["MS Gothic", "ＭＳ ゴシック", "Consolas"]
DEFAULT_FONT_SIZE = 10
DEFAULT_EDITOR = "notepad.exe"
DEFAULT_LIST_TIMEOUT = 5.0
RECENT_LIMIT = 50
LOG_LINES = 5

# offscreen(テスト)では QKeyEvent.nativeVirtualKey() が 0 で来る。そのときだけ
# Qt のキーから仮想キーコードを引く。実機の Windows では常に native の値を使う
# (日本語キーボードの「:」は Qt では Key_Colon/Key_Asterisk と変わるが、VK は 0xBA で一定)。
_QT_TO_VK = {int(k): v for k, v in {
    Qt.Key_Backspace: 8, Qt.Key_Tab: 9, Qt.Key_Backtab: 9, Qt.Key_Return: 13, Qt.Key_Enter: 13,
    Qt.Key_Escape: 27, Qt.Key_Space: 32, Qt.Key_PageUp: 33, Qt.Key_PageDown: 34,
    Qt.Key_End: 35, Qt.Key_Home: 36, Qt.Key_Left: 37, Qt.Key_Up: 38, Qt.Key_Right: 39,
    Qt.Key_Down: 40, Qt.Key_Insert: 45, Qt.Key_Delete: 46,
    Qt.Key_Colon: 186, Qt.Key_Asterisk: 186, Qt.Key_Comma: 188, Qt.Key_Less: 188,
    Qt.Key_Period: 190, Qt.Key_Greater: 190,
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
    print(text, file=sys.stderr)
    exc_type, exc_value = sys.exc_info()[:2]
    return f"{exc_type.__name__}: {exc_value}" if exc_type else "unknown error"


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
                return core.split_name(entry)[1]
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

        bg = None
        if marked:
            bg = colors["mark_cursor"] if (is_cursor and active) else colors["mark"]
            fg = colors["mark_text"]
        else:
            if is_cursor and active:
                bg = colors["cursor"]
            if entry is not None and entry.hidden:
                fg = colors["hidden"]
            elif entry is not None and entry.is_dir:
                fg = colors["directory"]
            else:
                fg = colors["text"]
        painter.save()
        try:
            if bg:
                painter.fillRect(rect, QColor(bg))
            if is_cursor and not active:
                painter.setPen(QPen(QColor(colors["cursor_inactive"]), 1))
                painter.drawLine(rect.bottomLeft(), rect.bottomRight())
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
            if event.type() == QEvent.KeyPress:
                self.pane.window_.handle_key(self.pane, event)
                return True
        except Exception:  # noqa: BLE001  スロットで投げ切るとプロセスごと落ちる
            self.pane.window_.log(f"エラー: {_log_exception('key')}")
            return True
        return super().event(event)

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
        except Exception:  # noqa: BLE001
            _log_exception("currentChanged")


class ListBridge(QObject):
    """読み取りスレッド → メインスレッドへ結果を渡す口(Qt はスレッドを跨いで触れない)。"""

    finished = Signal(object)


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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QLabel()
        self.header.setFont(window.list_font)
        self.header.setTextFormat(Qt.PlainText)
        self.header.setContentsMargins(6, 2, 6, 2)
        layout.addWidget(self.header)

        self.model = FileModel(self)
        self.view = FileView(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(RowDelegate(self))
        self.view.setFont(window.list_font)
        self.view.setSelectionMode(QAbstractItemView.NoSelection)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setShowGrid(False)
        self.view.setWordWrap(False)
        self.view.setTabKeyNavigation(False)
        self.view.setFocusPolicy(Qt.StrongFocus)
        self.view.verticalHeader().hide()
        self.view.horizontalHeader().hide()
        fm = QFontMetrics(window.list_font)
        self.view.verticalHeader().setDefaultSectionSize(fm.height() + 2)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        for col, sample in ((COL_EXT, "MMMMMM"), (COL_SIZE, "9,999,999,999 "),
                            (COL_DATE, "26-10-23 12:34 ")):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            header.resizeSection(col, fm.horizontalAdvance(sample) + 8)
        self.view.doubleClicked.connect(self._on_double_clicked)
        layout.addWidget(self.view, 1)

        self.footer = QLabel()
        self.footer.setFont(window.list_font)
        self.footer.setTextFormat(Qt.PlainText)
        self.footer.setContentsMargins(6, 1, 6, 1)
        layout.addWidget(self.footer)
        self.apply_style()

    # --- 見た目 ---
    def is_active(self) -> bool:
        return self.window_.active_side == self.side

    def apply_style(self) -> None:
        c = self.colors
        active = self.is_active()
        self.header.setStyleSheet(
            f"background-color: {c['header_active'] if active else c['header_inactive']};"
            f" color: {c['header_active_text'] if active else c['header_inactive_text']};")
        border = c["header_active"] if active else c["header_inactive"]
        self.view.setStyleSheet(
            f"QTableView {{ background-color: {c['background']}; color: {c['text']};"
            f" border: 2px solid {border}; }}")
        self.footer.setStyleSheet(
            f"background-color: {c['background']}; color: {c['footer_text']};")
        self.update_header()
        self.view.viewport().update()

    def update_header(self) -> None:
        text = self.path or "(読み込み中)"
        if not core.is_default_mask(self.mask_text):
            text += f"   [{self.mask_text}]"
        if self.sort != core.DEFAULT_SORT:
            arrow = "↓" if self.sort["descending"] else "↑"
            text += f"   ({core.SORT_LABELS[self.sort['kind']]}{arrow})"
        if self.loading:
            text += "   …"
        self.header.setText(text)

    def update_footer(self) -> None:
        if not hasattr(self, "footer"):
            return  # 組み立ての途中(ビューを作った時点で currentChanged が飛ぶ)
        dirs = sum(1 for e in self.entries if e.is_dir and not e.is_parent)
        files = sum(1 for e in self.entries if not e.is_dir)
        text = f"フォルダ {dirs}  ファイル {files}"
        count, total = core.marks_summary(self.entries, self.marks)
        if count:
            text += f"   マーク {count}  {core.format_bytes(total)}"
        if self.isearch_text is not None:
            text += f"   検索: {self.isearch_text}_"
            self.footer.setStyleSheet(
                f"background-color: {self.colors['background']}; color: {self.colors['isearch']};")
        else:
            self.footer.setStyleSheet(
                f"background-color: {self.colors['background']};"
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

    def page_rows(self) -> int:
        height = self.view.verticalHeader().defaultSectionSize() or 1
        return max(1, self.view.viewport().height() // height - 1)

    def targets(self) -> list:
        return core.pick_targets(self.entries, self.marks, self.cursor_row())

    def target_paths(self) -> list:
        return [os.path.join(self.path, e.name) for e in self.targets()]

    def _on_double_clicked(self, index):
        try:
            self.set_cursor(index.row())
            self.window_.run_command("enter")
        except Exception:  # noqa: BLE001
            self.window_.log(f"エラー: {_log_exception('doubleClick')}")

    # --- 一覧の読み込み ---
    def load(self, path: str, focus=None, history="visit", keep_marks=False,
             walk_up=False, on_fail=None) -> None:
        """path を別スレッドで読み、終わったら一覧を差し替える。

        history … "visit"(履歴に積む) / None(積まない。再読込) / ("back"|"forward", 直前のパス)
        on_fail … 読めなかったときに呼ぶ(起動時の復元で、次の候補へ回すため)。"""
        self.request_id += 1
        request = self.request_id
        self.loading = True
        self.update_header()
        timeout_ms = int(self.window_.list_timeout * 1000)
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
        QTimer.singleShot(timeout_ms, lambda: self._on_timeout(request, path, on_fail, history))

    def _on_timeout(self, request, path, on_fail, history) -> None:
        try:
            if request != self.request_id or not self.loading:
                return
            # 以後この読み取りの結果が来ても捨てる(request_id を進めておく)。
            self.request_id += 1
            self.loading = False
            self.update_header()
            self.window_.log(f"応答がありません({self.window_.list_timeout:g}秒): {path}")
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
        new_path, entries, focus = result
        same = core.same_dir(self.path, new_path) if self.path else False
        previous_name = None
        previous_row = self.cursor_row()
        if same:
            entry = self.cursor_entry()
            previous_name = entry.name if entry is not None else None
        if not (same and args["keep_marks"]):
            self.marks = set()
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
        """マスクを掛け直して表示の並びを作る。マークは表示に残ったものだけ持ち越す。"""
        visible = core.sort_entries(
            core.filter_entries(self.all_entries, self.patterns), self.sort)
        names = {e.name for e in visible}
        self.marks = {m for m in self.marks if m in names}
        self.entries = ([] if core.is_root(self.path) else [core.PARENT_ENTRY]) + visible
        self.model.set_entries(self.entries, self.marks)

    def _row_of(self, name):
        if not name:
            return None
        folded = name.casefold()
        for row, e in enumerate(self.entries):
            if e.name.casefold() == folded:
                return row
        return None

    def reload(self, keep_marks=False, focus=None) -> None:
        if self.path:
            self.load(self.path, focus=focus, history=None, keep_marks=keep_marks, walk_up=True)

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
        self.editor = section.get("editor") or DEFAULT_EDITOR
        try:
            self.list_timeout = max(0.5, float(section.get("list_timeout_seconds")
                                               or DEFAULT_LIST_TIMEOUT))
        except (TypeError, ValueError):
            self.list_timeout = DEFAULT_LIST_TIMEOUT
        recent = section.get("recent")
        self.recent = [p for p in recent if isinstance(p, str)] if isinstance(recent, list) else []
        # テストのときだけ SHFileOperation に足すフラグ(FOF_NOCONFIRMATION 等)。本番は 0。
        self.extra_op_flags = 0
        self._picker = None
        self._server = None

        families = section.get("font_families")
        if not (isinstance(families, list) and families):
            families = DEFAULT_FONT_FAMILIES
        self.list_font = QFont()
        self.list_font.setFamilies([str(f) for f in families])
        self.list_font.setStyleHint(QFont.Monospace)
        try:
            self.list_font.setPointSize(int(section.get("font_size") or DEFAULT_FONT_SIZE))
        except (TypeError, ValueError):
            self.list_font.setPointSize(DEFAULT_FONT_SIZE)

        self.setWindowTitle("簡易ファイラ")
        self.setObjectName("filerWindow")
        self.setStyleSheet(f"#filerWindow {{ background-color: {self.colors['background']}; }}")
        self.active_side = section.get("active") if section.get("active") in ("left", "right") else "left"

        self.bridge = ListBridge(self)
        self.bridge.finished.connect(self._on_list_finished)

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.left = Pane(self, "left")
        self.right = Pane(self, "right")
        row.addWidget(self.left, 1)
        row.addWidget(self.right, 1)
        root.addLayout(row, 1)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFocusPolicy(Qt.NoFocus)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFont(self.list_font)
        self.log_view.setStyleSheet(
            f"background-color: {self.colors['log_background']}; color: {self.colors['log_text']};"
            " border: none;")
        fm = QFontMetrics(self.list_font)
        self.log_view.setFixedHeight(fm.lineSpacing() * LOG_LINES + 10)
        root.addWidget(self.log_view)

        self._restore_geometry(section)
        for pane in (self.left, self.right):
            pane.apply_style()

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

    def start(self) -> None:
        """前回のパスで両窓を開く。無くなっていれば残っている親まで遡り、全部だめなら
        ユーザープロファイル(それも読めなければ C:\\)。"""
        section = _section(self.app_settings)
        home = os.path.expanduser("~")
        for pane, key in ((self.left, "left_path"), (self.right, "right_path")):
            saved = section.get(key)
            candidates = [p for p in (saved, home, "C:\\") if isinstance(p, str) and p]
            self._load_first(pane, candidates)
        self.activate(self.active_side)

    def _load_first(self, pane, candidates) -> None:
        if not candidates:
            return
        first, rest = candidates[0], candidates[1:]
        pane.load(first, walk_up=True, on_fail=lambda: self._load_first(pane, rest))

    def save_state(self) -> None:
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        updates = {
            "geometry": [geometry.x(), geometry.y(), geometry.width(), geometry.height()],
            "maximized": self.isMaximized(),
            "active": self.active_side,
            "recent": self.recent[:RECENT_LIMIT],
            "left_sort": self.left.sort,
            "right_sort": self.right.sort,
        }
        if self.left.path:
            updates["left_path"] = self.left.path
        if self.right.path:
            updates["right_path"] = self.right.path
        save_values(self.app_settings, self.settings_path, updates)

    def closeEvent(self, event):
        try:
            self.save_state()
            if self._picker is not None:
                self._picker.close()
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
        if changed and self.active.isearch_text is not None:
            self.end_isearch()
        self.active_side = side
        if focus:
            self.active.view.setFocus()
        if changed or focus:
            for pane in (self.left, self.right):
                pane.apply_style()
        self._update_title()

    def _update_title(self) -> None:
        path = self.active.path or ""
        self.setWindowTitle(f"{path} - 簡易ファイラ" if path else "簡易ファイラ")

    def on_pane_loaded(self, pane) -> None:
        self.recent = core.push_recent(self.recent, pane.path, RECENT_LIMIT)
        self._update_title()

    def _on_list_finished(self, payload) -> None:
        try:
            side, request, ok, result, args = payload
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
        if self.active.isearch_text is not None and self._isearch_key(event):
            return
        code = core.scav(_vk_of(event), bool(mods & Qt.ShiftModifier),
                         bool(mods & Qt.ControlModifier), bool(mods & Qt.AltModifier))
        command = self.keymap.get(code)
        if command:
            self.run_command(command)

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
        if not pane.path:
            return
        parent = core.parent_of(pane.path)
        if parent is None:
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
        else:
            self.cmd_open_assoc()

    def cmd_open_assoc(self):
        pane = self.active
        entry = pane.cursor_entry()
        if entry is None or entry.is_parent:
            return
        path = os.path.join(pane.path, entry.name)
        try:
            os.startfile(path)
            self.log(f"開く: {path}")
        except OSError as e:
            self.log(f"開けません: {path}  ({e})")

    def cmd_edit(self):
        paths = [os.path.join(self.active.path, e.name)
                 for e in self.active.targets() if not e.is_dir]
        if not paths:
            return
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

    def _open_picker(self, title, items, on_path, **kwargs) -> None:
        """J/H/L の選択窓(picker.PickerWindow を流用)。決めたらアクティブ窓をそこへ。"""
        from picker import PickerWindow

        if self._picker is not None:
            self._picker.close()

        def accept(_name, data):
            path = data[1] if isinstance(data, tuple) else data
            on_path(path)

        picker = PickerWindow(title, items, accept, **kwargs)
        picker.closed.connect(self._on_picker_closed)
        self._picker = picker
        picker.show()

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

        bookmarks = launcher.load_bookmarks(self.app_settings)
        items = [(b["name"], (launcher.KIND_JUMP, b["path"])) for b in bookmarks]
        if not items:
            self.log("ブックマークがありません（トレイの「📁 フォルダブックマーク」で登録）")
        self._open_picker(
            "フォルダブックマーク", items, self.jump_to,
            placeholder=launcher.PLACEHOLDER,
            dynamic_items=launcher.typed_items, tab_text=launcher.tab_text)

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
        self.active.set_mask(core.DEFAULT_MASK)

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

    def _after_operation(self, label, result, sources, dest=None) -> None:
        code, aborted = result
        names = f"{len(sources)}件"
        where = f" → {dest}" if dest else ""
        if aborted:
            self.log(f"{label}: 中断しました（{names}{where}）")
        elif code != 0:
            self.log(f"{label}: 失敗しました（コード 0x{code:X}、{names}{where}）")
        else:
            self.log(f"{label}: {names}{where}")
        self.left.reload()
        self.right.reload()

    def _transfer(self, move: bool) -> None:
        src = self.active
        dst = self.other
        paths = src.target_paths()
        if not paths:
            return
        label = "移動" if move else "複写"
        if not dst.path or core.same_dir(src.path, dst.path):
            self.log(f"{label}: 反対窓が同じフォルダです")
            return
        func = filer_shell.move_files if move else filer_shell.copy_files
        result = func(paths, dst.path, self._hwnd(), self.extra_op_flags)
        self._after_operation(label, result, paths, dst.path)

    def cmd_copy_direct(self):
        self._transfer(move=False)

    def cmd_move_direct(self):
        self._transfer(move=True)

    def cmd_copy_menu(self):
        if not self.active.targets():
            return
        choice = self.choose_menu("複写", [
            ("O", "反対窓にコピー", "copy"),
            ("P", "フルパスをクリップボードへ（改行区切り）", "path"),
            ("N", "ファイル名のみクリップボードへ", "name"),
            ("Q", "キャンセル", None),
        ])
        if choice == "copy":
            self._transfer(move=False)
        elif choice in ("path", "name"):
            paths = self.active.target_paths()
            QGuiApplication.clipboard().setText(core.clip_text(paths, names_only=choice == "name"))
            self.log(f"クリップボード: {len(paths)}件の{'フルパス' if choice == 'path' else '名前'}")

    def cmd_move_menu(self):
        if not self.active.targets():
            return
        choice = self.choose_menu("移動", [
            ("O", "反対窓に移動", "move"),
            ("Q", "キャンセル", None),
        ])
        if choice == "move":
            self._transfer(move=True)

    def _delete(self, permanent: bool) -> None:
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
        # 完全削除は既定のボタンを「いいえ」にする(Enter の押し間違いで消さない)。
        if not self.confirm("削除", f"{question}\n\n{shown}", default_yes=not permanent):
            return
        paths = [os.path.join(pane.path, e.name) for e in targets]
        result = filer_shell.delete_files(paths, self._hwnd(), permanent=permanent,
                                          extra_flags=self.extra_op_flags)
        self._after_operation("完全削除" if permanent else "削除(ごみ箱)", result, paths)

    def cmd_delete(self):
        self._delete(permanent=False)

    def cmd_delete_permanent(self):
        self._delete(permanent=True)

    # --- 作成・改名 ---
    def cmd_mkdir(self):
        pane = self.active
        if not pane.path:
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
        pane = self.active
        entry = pane.cursor_entry()
        if entry is None or entry.is_parent:
            return
        new = self.ask_text("改名", entry.name, entry.name,
                            select=core.rename_selection_length(entry.name, entry.is_dir))
        if new is None:
            return
        new = new.strip()
        if new == entry.name:
            return
        problem = core.validate_name(new)
        if problem:
            self.log(f"改名: {problem}")
            return
        if core.name_conflicts([e.name for e in pane.all_entries], new, entry.name):
            self.log(f"改名: 同じ名前があります: {new}")
            return
        old_path = os.path.join(pane.path, entry.name)
        try:
            os.rename(old_path, os.path.join(pane.path, new))
        except OSError as e:
            self.log(f"改名: できません: {entry.name} → {new}  ({e})")
            return
        self.log(f"改名: {entry.name} → {new}")
        pane.reload(keep_marks=False, focus=new)

    # --- クリップボード(エクスプローラと相互に貼れる形) ---
    DROP_EFFECT_FORMAT = 'application/x-qt-windows-mime;value="Preferred DropEffect"'

    def _clip_set(self, move: bool) -> None:
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
        if not pane.path:
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

    def choose_menu(self, title: str, items):
        """先頭の文字キーで即決する小さなメニュー(あふｗの &MENU)。選んだ値か None。"""
        menu = QMenu(self)
        menu.setTitle(title)
        chosen = {}
        for key, label, value in items:
            action = menu.addAction(f"&{key}:{label}")
            chosen[action] = value
        view = self.active.view
        rect = view.visualRect(view.currentIndex())
        pos = view.viewport().mapToGlobal(rect.bottomLeft() if rect.isValid() else view.rect().center())
        action = menu.exec(pos)
        return chosen.get(action)


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
                style += f" background-color: {c['cursor']};"
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
    先頭文字(N/F/E/S/T/A/X)で即決、Enter で決定、Esc で取消。"""

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
            self._decide(core.SORT_SHORTCUTS[letter])
            return
        self.refresh()


class CompareDialog(_KeyDialog):
    """あふｗの「同名ファイル比較マーク」の条件。↑↓で行、←→で選択肢、Space でチェック、
    Enter で決定、Esc で取消。A/N/S/T/E は即決。Shift を押しながら決めるとマークを外す。"""

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
            size, time_, strip_ext = core.COMPARE_SHORTCUTS[letter]
            self._decide(size, time_, strip_ext, shift)
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


def main() -> int:
    app = QApplication(sys.argv)
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
        window.bring_to_front()
    except Exception:  # noqa: BLE001
        _log_exception("open")
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
