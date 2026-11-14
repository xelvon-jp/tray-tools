# script_runner.py
# 「スクリプト実行パネル」。ダウンロードフォルダに届いた .py / .ps1 を
# 自動でチェック(読むだけ)し、一覧から選んで F5 で実行して、結果を
# クリップボードへ入れる小さな窓。Copilot の擬似エージェントループの
# 手数を減らす道具(仕様は collab-workspace の
# docs/work-tools/script-runner-spec.md)。
#
#   pythonw script_runner.py [--settings <settings.json のパス>]
#
# 常駐とは別のプロセスで動く(起こすのは script_runner_launch.py)。
# チェック・実行・文の組み立ては script_runner_core.py(窓と切り離した
# 純粋部分)にあり、ここは窓と配線だけ。
#
# 大事な線引き:
#   - 実行するのは、ボタンか F5 が押されたときだけ。届いただけでは
#     絶対に実行しない。
#   - 要確認のものは F5 では実行しない。「中身を見てから実行」で
#     該当行を見せ、もう一度押されて初めて実行する。
#   - 実行の直前に sha256 を取り直し、チェックしたときと違えば
#     実行せず、チェックからやり直す。
import argparse
import ctypes
import os
import queue
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import script_runner_core as core
import script_runner_launch
import settings as settings_module

POLL_INTERVAL_MS = 1200

STATUS_COLORS = {
    core.STATUS_OK: "#1f7a45",
    core.STATUS_ERROR: "#b3342d",
    core.STATUS_WARN: "#8a5a00",
    core.STATUS_UNKNOWN: "#5b665f",
    "checking": "#5b665f",
}

NOTE_STYLES = {
    "": "",
    "ok": "color: #1f7a45;",
    "error": "color: #b3342d; background: #fbeceb;",
    "warn": "color: #8a5a00; background: #fbf2dd;",
}


# ---------------------------------------------------------------
# 単一起動(slideshow.py と同じ作法。判定はミューテックス)
# ---------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None


def acquire_single_instance() -> bool:
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateMutexW
        create.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        create.restype = wintypes.HANDLE
        handle = create(None, False, script_runner_launch.MUTEX_NAME)
        already = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except OSError:
        return True
    if not handle:
        return True
    if already:
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        close(handle)
        return False
    _mutex_handle = handle
    return True


class CommandServer(QObject):
    """script_runner_launch から届く "show" を受ける口。"""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.server = QLocalServer(self)
        QLocalServer.removeServer(script_runner_launch.SERVER_NAME)
        if self.server.listen(script_runner_launch.SERVER_NAME):
            self.server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        try:
            while self.server.hasPendingConnections():
                sock = self.server.nextPendingConnection()
                sock.readyRead.connect(lambda s=sock: self._read(s))
        except Exception:  # noqa: BLE001  受け口で落とさない
            pass

    def _read(self, sock) -> None:
        try:
            data = bytes(sock.readAll()).decode("utf-8", errors="replace")
            if "show" in data:
                self.window.show()
                self.window.raise_()
                self.window.activateWindow()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------
# 裏で回す仕事(チェックと実行)
# ---------------------------------------------------------------
class _CheckWorker(QObject):
    done = Signal(object)  # (key, (mtime, size), CheckResult)

    def __init__(self, config):
        super().__init__()
        self._config = config
        self._queue = queue.Queue()
        self._stopped = False

    def request(self, key, path, stat_key):
        self._queue.put((key, path, stat_key))

    def run(self):
        while not self._stopped:
            try:
                key, path, stat_key = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = core.check_script(path, self._config)
            except Exception:  # noqa: BLE001  チェックの失敗は「不能」で出す
                result = core.CheckResult(
                    status=core.STATUS_UNKNOWN, kind="failed",
                    headline="チェックできませんでした",
                    sha256=core.file_sha256(path))
            self.done.emit((key, stat_key, result))

    def stop(self):
        self._stopped = True


class _RunSignals(QObject):
    done = Signal(object)  # RunResult


class ScriptItem:
    """一覧の1行分の状態。"""

    def __init__(self, path: Path, mtime: float, size: int):
        self.path = path
        self.key = str(path)
        self.name = path.name
        self.mtime = mtime
        self.size = size
        self.status = "checking"
        self.result = None          # CheckResult
        self.ran = False
        self.confirm_armed = False  # 「中身を見てから実行」の2段目

    def stat_key(self):
        return (self.mtime, self.size)


# ---------------------------------------------------------------
# 窓
# ---------------------------------------------------------------
class ScriptRunnerWindow(QWidget):
    def __init__(self, settings_path=None):
        super().__init__()
        self.settings_path = settings_path
        all_settings = settings_module.load_settings(settings_path)
        self.app_settings = all_settings
        self.config = core.normalize_config(
            all_settings.get("script_runner"))
        self.watch_folder = core.resolve_watch_folder(self.config)

        self.setWindowTitle("スクリプト実行")
        geometry = self.config.get("window_geometry") or [None, None, 340, 560]
        self.resize(geometry[2] or 340, geometry[3] or 560)
        if geometry[0] is not None and geometry[1] is not None:
            self.move(geometry[0], geometry[1])

        self._items = []            # [ScriptItem](表示順 = 新しい順)
        self._items_by_key = {}
        self._stable = {}           # key -> (size, 連続で同じだった回数)
        self._selected_key = None
        self._running_item = None
        self._run_handle = None
        self._run_thread = None
        self._run_signals = _RunSignals()
        self._run_signals.done.connect(self._on_run_done)
        self._last_clipboard_text = ""
        self._last_run = None       # (key, RunResult)
        self._editor_windows = []

        self._build_ui()
        self._apply_on_top(bool(self.config.get("always_on_top")),
                           save=False)

        # チェックは裏のスレッドで1件ずつ(サブプロセス待ちで窓を止めない)。
        import threading

        self._check_worker = _CheckWorker(self.config)
        self._check_worker.done.connect(self._on_check_done)
        self._check_thread = threading.Thread(
            target=self._check_worker.run, daemon=True,
            name="script-runner-check")
        self._check_thread.start()

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_INTERVAL_MS)
        self._poll()

    # ---------------- 画面 ----------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("スクリプト実行")
        title.setFont(QFont(title.font().family(), 11, QFont.Weight.Bold))
        header.addWidget(title)
        header.addStretch(1)
        version = QLabel(self._version_label())
        version.setStyleSheet("color: #5b665f; font-size: 11px;")
        header.addWidget(version)
        self.on_top_box = QCheckBox("前面に固定")
        self.on_top_box.setChecked(bool(self.config.get("always_on_top")))
        self.on_top_box.toggled.connect(
            lambda on: self._apply_on_top(on, save=True))
        header.addWidget(self.on_top_box)
        layout.addLayout(header)

        caption = QLabel(f"{self.watch_folder} に届いたもの(新しい順)")
        caption.setStyleSheet("color: #5b665f; font-size: 11px;")
        caption.setWordWrap(True)
        layout.addWidget(caption)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list, stretch=3)

        self.result_caption = QLabel("直前の結果")
        self.result_caption.setStyleSheet("color: #5b665f; font-size: 11px;")
        layout.addWidget(self.result_caption)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 9))
        self.output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.output, stretch=2)

        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self._stop_run)
        self.stop_button.setVisible(False)
        layout.addWidget(self.stop_button)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        QShortcut(QKeySequence("F5"), self, self._f5_pressed)
        QShortcut(QKeySequence("Ctrl+C"), self, self._copy_last)

    def _version_label(self) -> str:
        try:
            import filer_update

            return filer_update.version_label()
        except Exception:  # noqa: BLE001  版が出ないだけ
            return ""

    def _set_note(self, text: str, kind: str = ""):
        self.note.setText(text)
        self.note.setStyleSheet(NOTE_STYLES.get(kind, ""))

    def _set_clipboard(self, text: str):
        QGuiApplication.clipboard().setText(text)
        self._last_clipboard_text = text

    # ---------------- 一覧の見回り ----------------
    def _poll(self):
        try:
            self._poll_inner()
        except Exception:  # noqa: BLE001  見回りで窓を落とさない
            pass

    def _poll_inner(self):
        cutoff = time.time() - self.config["recent_hours"] * 3600
        candidates = core.list_candidates(
            self.watch_folder, cutoff, self.config["max_items"] * 3)
        present = {}
        changed = False
        for path, mtime, size in candidates:
            key = str(path)
            present[key] = (path, mtime, size)
            prev = self._stable.get(key)
            count = prev[1] + 1 if prev and prev[0] == size else 1
            self._stable[key] = (size, count)
            if count < 2:
                continue  # 大きさが落ち着くまでチェックしない
            item = self._items_by_key.get(key)
            if item is None:
                item = ScriptItem(path, mtime, size)
                self._items.append(item)
                self._items_by_key[key] = item
                self._queue_check(item)
                changed = True
            elif (mtime, size) != item.stat_key():
                # 同じ名前で上書きされた: チェックをやり直し、実行済みの
                # 印を外す。
                item.mtime, item.size = mtime, size
                item.ran = False
                item.confirm_armed = False
                item.result = None
                self._queue_check(item)
                changed = True
        for key in list(self._items_by_key):
            if key not in present:
                item = self._items_by_key.pop(key)
                if self._running_item is not item:
                    self._items.remove(item)
                    changed = True
        self._stable = {k: v for k, v in self._stable.items() if k in present}
        # 新しい順に並べ、上限を超えたら古い方から落とす。
        self._items.sort(key=lambda item: item.mtime, reverse=True)
        while len(self._items) > self.config["max_items"]:
            tail = self._items[-1]
            if tail is self._running_item:
                break
            self._items.pop()
            self._items_by_key.pop(tail.key, None)
            changed = True
        if changed:
            self._refresh_list()

    def _queue_check(self, item: ScriptItem):
        item.status = "checking"
        self._check_worker.request(item.key, item.path, item.stat_key())

    def _on_check_done(self, payload):
        key, stat_key, result = payload
        item = self._items_by_key.get(key)
        if item is None or item.stat_key() != stat_key:
            return  # チェックのあいだに中身が変わった。新しい方に任せる
        item.result = result
        item.status = result.status
        self._refresh_list()
        if self._selected_key == key:
            self._show_lower_for(item)

    # ---------------- 一覧の描画 ----------------
    def _refresh_list(self):
        # 作り直しの間は currentRowChanged を止める。clear() で行が -1 に
        # 戻るたびに _on_selection_changed が呼ばれ、そこからまた
        # _refresh_list に入る無限再帰になるため(実測で踏んだ)。
        selected = self._selected_key
        with QSignalBlocker(self.list):
            self.list.clear()
            for index, item in enumerate(self._items):
                entry = QListWidgetItem()
                entry.setData(Qt.ItemDataRole.UserRole, item.key)
                self.list.addItem(entry)
                widget = self._row_widget(item, item.key == selected)
                entry.setSizeHint(widget.sizeHint())
                self.list.setItemWidget(entry, widget)
                if item.key == selected:
                    self.list.setCurrentRow(index)
            if self.list.currentRow() < 0 and self._items:
                # 選択が無いときは先頭を選んだことにする。シグナルを止めて
                # いるので、選択側の状態もここで合わせておく。
                self.list.setCurrentRow(0)
                self._selected_key = self._items[0].key
                for item in self._items[1:]:
                    item.confirm_armed = False

    def _row_widget(self, item: ScriptItem, selected: bool) -> QWidget:
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(6, 4, 6, 4)
        box.setSpacing(2)
        top = QHBoxLayout()
        name = QLabel(item.name)
        name.setFont(QFont(name.font().family(), 10, QFont.Weight.Bold))
        top.addWidget(name)
        top.addStretch(1)
        when = datetime.fromtimestamp(item.mtime)
        today = datetime.now().date() == when.date()
        stamp = when.strftime("%H:%M" if today else "%m/%d %H:%M")
        time_label = QLabel(stamp)
        time_label.setStyleSheet("color: #5b665f; font-size: 11px;")
        top.addWidget(time_label)
        box.addLayout(top)

        status = QLabel(self._status_text(item))
        color = STATUS_COLORS.get(item.status, "#5b665f")
        status.setStyleSheet(f"color: {color}; font-size: 12px;")
        status.setWordWrap(True)
        box.addWidget(status)

        if selected:
            buttons = self._row_buttons(item)
            if buttons is not None:
                box.addLayout(buttons)
        return widget

    def _status_text(self, item: ScriptItem) -> str:
        if item.status == "checking":
            return "チェック中…"
        text = item.result.headline if item.result else ""
        if item.status == core.STATUS_OK:
            text += "・実行済み" if item.ran else "・読むだけ"
        elif item.ran:
            text += "・実行済み"
        return text

    def _row_buttons(self, item: ScriptItem):
        if item.status == "checking" or self._running_item is not None:
            return None
        box = QHBoxLayout()
        box.setSpacing(6)
        if item.status == core.STATUS_ERROR:
            copy_button = QPushButton("エラーをコピー")
            copy_button.setStyleSheet(
                "background: #1f6f5c; color: #ffffff;")
            copy_button.clicked.connect(
                lambda _c=False, it=item: self._copy_check_error(it))
            box.addWidget(copy_button)
        elif item.status in (core.STATUS_WARN, core.STATUS_UNKNOWN):
            if item.confirm_armed:
                run_button = QPushButton("確認しました。実行する")
                run_button.setStyleSheet(
                    "background: #1f6f5c; color: #ffffff;")
            else:
                run_button = QPushButton("中身を見てから実行")
            run_button.clicked.connect(
                lambda _c=False, it=item: self._view_and_run(it))
            box.addWidget(run_button)
        else:  # STATUS_OK
            run_button = QPushButton(
                "もう一度実行" if item.ran else "実行 F5")
            if not item.ran:
                run_button.setStyleSheet(
                    "background: #1f6f5c; color: #ffffff;")
            run_button.clicked.connect(
                lambda _c=False, it=item: self._start_run(it))
            box.addWidget(run_button)
        eddy_button = QPushButton("EDDy で開く")
        eddy_button.clicked.connect(
            lambda _c=False, it=item: self._open_in_eddy(it))
        box.addWidget(eddy_button)
        box.addStretch(1)
        return box

    def _on_selection_changed(self, row: int):
        entry = self.list.item(row) if row >= 0 else None
        self._selected_key = (
            entry.data(Qt.ItemDataRole.UserRole) if entry is not None else None)
        for item in self._items:
            if item.key != self._selected_key:
                item.confirm_armed = False
        self._refresh_list()
        item = self._items_by_key.get(self._selected_key or "")
        if item is not None:
            self._show_lower_for(item)

    def _selected_item(self):
        return self._items_by_key.get(self._selected_key or "")

    # ---------------- 下半分 ----------------
    def _show_lower_for(self, item: ScriptItem):
        if self._running_item is not None:
            return
        if item.result is not None and item.status in (
                core.STATUS_ERROR, core.STATUS_WARN, core.STATUS_UNKNOWN):
            self.result_caption.setText(f"チェックの結果・{item.name}")
            preview = core.build_check_preview(item.result)
            self.output.setPlainText(preview or item.result.headline)
            if item.status == core.STATUS_ERROR:
                self._set_note(
                    "実行はできません。エラーをコピーして Copilot に貼ると、"
                    "直してもらえます", "error")
            elif item.status == core.STATUS_WARN:
                self._set_note(
                    "F5 だけでは実行しません。中身を見てから実行してください",
                    "warn")
            else:
                self._set_note(item.result.headline, "warn")
            return
        if self._last_run is not None:
            key, run = self._last_run
            self.result_caption.setText(f"直前の結果・{run.name}")
            self.output.setPlainText(core.build_run_preview(run))
            return
        if item.result is not None:
            self.result_caption.setText(f"チェックの結果・{item.name}")
            self.output.setPlainText(item.result.headline)

    # ---------------- 実行 ----------------
    def _f5_pressed(self):
        item = self._selected_item()
        if item is None or self._running_item is not None:
            return
        if item.status == core.STATUS_OK:
            self._start_run(item)
        elif item.status in (core.STATUS_WARN, core.STATUS_UNKNOWN):
            # F5 では実行しない(仕様)。中身を見る段へ誘導する。
            self._show_lower_for(item)
            self._set_note(
                "要確認のため、F5 では実行しません。"
                "「中身を見てから実行」を押してください", "warn")

    def _view_and_run(self, item: ScriptItem):
        if self._running_item is not None:
            return
        if not item.confirm_armed:
            item.confirm_armed = True
            self._show_lower_for(item)
            self._set_note(
                "該当の行を表示しました。確かめてから、もう一度押すと"
                "実行します", "warn")
            self._refresh_list()
            return
        item.confirm_armed = False
        self._start_run(item)

    def _start_run(self, item: ScriptItem):
        if self._running_item is not None or item.result is None:
            return
        # 実行の直前に sha256 を取り直す。チェックしたときと違えば
        # 実行せず、チェックからやり直す(仕様のアオイ追記2)。
        current = core.file_sha256(item.path)
        if current != item.result.sha256:
            item.result = None
            item.ran = False
            self._queue_check(item)
            self._refresh_list()
            self._set_note(
                "中身が変わっていたので、チェックからやり直しました", "warn")
            return
        self._running_item = item
        self._run_handle = core.RunHandle()
        self.result_caption.setText(f"実行中・{item.name}")
        self.output.setPlainText("実行中…")
        self._set_note("")
        self.stop_button.setVisible(True)
        self._refresh_list()

        import threading

        def work():
            run = core.execute_script(
                item.path, self.config, self._run_handle,
                expected_sha256=item.result.sha256)
            self._run_signals.done.emit(run)

        self._run_thread = threading.Thread(
            target=work, daemon=True, name="script-runner-run")
        self._run_thread.start()

    def _stop_run(self):
        if self._run_handle is not None:
            self._run_handle.stop()

    def _on_run_done(self, run: core.RunResult):
        item = self._running_item
        self._running_item = None
        self._run_handle = None
        self.stop_button.setVisible(False)
        if item is not None:
            item.ran = True
            check = item.result
        else:
            check = None
        text = core.build_result_text(run.name, run, self.config)
        try:
            self._set_clipboard(text)
            copied = True
        except Exception:  # noqa: BLE001
            copied = False
        core.append_run_log(run, check)
        self._last_run = (item.key if item is not None else "", run)
        self.result_caption.setText(f"直前の結果・{run.name}")
        self.output.setPlainText(core.build_run_preview(run))
        if run.policy_stop:
            self._set_note(core.POLICY_NOTE, "error")
        elif copied:
            self._set_note(
                "クリップボードに入れました。Copilot に貼ってください", "ok")
        else:
            self._set_note("クリップボードへ入れませんでした", "error")
        self._refresh_list()

    # ---------------- コピー ----------------
    def _copy_check_error(self, item: ScriptItem):
        if item.result is None:
            return
        self._set_clipboard(core.build_check_text(item.name, item.result))
        self._set_note(
            "エラーをクリップボードに入れました。Copilot に貼ってください",
            "ok")

    def _copy_last(self):
        if self._last_clipboard_text:
            self._set_clipboard(self._last_clipboard_text)
            self._set_note("もう一度クリップボードに入れました", "ok")
            return
        item = self._selected_item()
        if item is not None and item.status == core.STATUS_ERROR:
            self._copy_check_error(item)

    # ---------------- EDDy で開く ----------------
    def _open_in_eddy(self, item: ScriptItem):
        try:
            import editor as editor_module
        except Exception as e:  # noqa: BLE001
            self._set_note(f"EDDy を開けませんでした ({e})", "error")
            return
        try:
            path = str(item.path)
            existing = editor_module.find_window_for_path(
                self._editor_windows, path)
            if existing is not None:
                window = existing
            else:
                section = self.app_settings.get("editor") or {}
                geometry = section.get("geometry")
                if not (isinstance(geometry, list) and len(geometry) == 4
                        and all(isinstance(v, int) for v in geometry)):
                    geometry = None
                window = editor_module.create_window(
                    path, geometry=geometry,
                    keymap_overrides=section.get("keymap"),
                    editor_settings=section,
                    on_settings_change=self._save_editor_settings,
                    session_path=editor_module.default_session_path())
                window.closed.connect(
                    lambda geo, w=window: self._on_editor_closed(w, geo))
                self._editor_windows.append(window)
                window.show()
            line = item.result.jump_line if item.result else 0
            if line:
                window.open_path_at_line(path, line)
            else:
                window.open_path(path)
            window.raise_()
            window.activateWindow()
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self._set_note(f"EDDy を開けませんでした ({e})", "error")

    def _save_editor_settings(self, updates) -> None:
        try:
            settings_module.update_settings_section(
                "editor", updates, self.settings_path)
            section = self.app_settings.get("editor")
            if isinstance(section, dict):
                section.update(updates)
        except Exception:  # noqa: BLE001  覚えられないだけ
            pass

    def _on_editor_closed(self, window, geometry) -> None:
        try:
            if window in self._editor_windows:
                self._editor_windows.remove(window)
            settings_module.update_settings_section(
                "editor", {"geometry": geometry}, self.settings_path)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 前面固定・終了 ----------------
    def _apply_on_top(self, on: bool, save: bool):
        self.config["always_on_top"] = bool(on)
        flag = Qt.WindowType.WindowStaysOnTopHint
        if bool(self.windowFlags() & flag) != bool(on):
            self.setWindowFlag(flag, bool(on))
            if self.isVisible():
                self.show()
        if save:
            try:
                settings_module.update_settings_section(
                    "script_runner", {"always_on_top": bool(on)},
                    self.settings_path)
            except Exception:  # noqa: BLE001  覚えられないだけ
                pass

    def closeEvent(self, event):
        try:
            self._poll_timer.stop()
            if self._run_handle is not None:
                self._run_handle.stop()
            self._check_worker.stop()
            rect = self.geometry()
            settings_module.update_settings_section(
                "script_runner",
                {"window_geometry": [rect.x(), rect.y(),
                                     rect.width(), rect.height()],
                 "always_on_top": bool(self.config.get("always_on_top"))},
                self.settings_path)
        except Exception:  # noqa: BLE001  終わりの保存で落とさない
            pass
        super().closeEvent(event)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="script_runner.py")
    parser.add_argument("--settings", default=None,
                        help="読む settings.json のパス(テスト用)")
    args = parser.parse_args(argv)
    app = QApplication([sys.argv[0]])
    if not acquire_single_instance():
        # 既に開いている。前へ出してもらって終わる。
        try:
            script_runner_launch.send(script_runner_launch.SHOW_COMMAND)
        except Exception:  # noqa: BLE001
            pass
        return 0
    window = ScriptRunnerWindow(settings_path=args.settings)
    CommandServer(window)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
