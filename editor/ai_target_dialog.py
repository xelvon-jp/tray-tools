# editor/ai_target_dialog.py
# 「AI の投げ先」の設定画面と、窓を選ぶ小窓。
#
# 並びの整えやプロファイルの組み立ては editor/ai_target.py
# (Qt 非依存)。ここは Qt の窓だけを持つ。検出 (プローブ) と
# テスト入力は、呼び側 (editor/window.py) から関数で受け取る
# (実体は copilot_io の裏方経由)。どちらも数秒かかることが
# あるので、裏のスレッドで回して橋で受け取る (window.py の
# 送信と同じ作法)。
#
# 窓の一覧 (プローブで指定) は UIA ではなく素の Win32 の列挙。
# UIA (comtypes) は常駐の中で動かさない決まりだが、ctypes の
# user32 呼び出しは window_tools などと同じで、その限りでは
# ない。Windows でなければ一覧は空になる (import は通る)。
import ctypes
import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from editor import ai_target as aitmod
from editor import editor_icon

try:
    # 専用の WinDLL を持つ (ctypes.windll の共有オブジェクトは
    # 他モジュールと署名の取り合いになるため。copilot_loop と同じ)。
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
except (AttributeError, OSError):
    # Windows 以外では WinDLL 自体が無い。一覧は空で倒す。
    _user32 = None

if _user32 is not None:
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.GetWindowTextW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong


def _title_of(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _class_of(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _exe_of(hwnd):
    """窓を持っているプロセスの実行ファイル名。取れなければ ''。"""
    try:
        import psutil

        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return psutil.Process(pid.value).name()
    except Exception:  # noqa: BLE001
        return ""


def enumerate_visible_windows() -> list:
    """見えているトップレベルの窓を [{title, window_class,
    process_name}] で返す。Windows でなければ空。

    UIA は使わない (素の Win32 列挙)。タイトルの無い窓は、
    選んでも手掛かりにならないので除く。"""
    if _user32 is None:
        return []
    rows = []
    enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def on_window(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            title = _title_of(hwnd)
            if not title:
                return True
            rows.append({
                "title": title,
                "window_class": _class_of(hwnd),
                "process_name": _exe_of(hwnd),
            })
        except Exception:  # noqa: BLE001  1つの窓で全体を止めない
            pass
        return True

    callback = enum_proc(on_window)
    _user32.EnumWindows(callback, None)
    return rows


class _OpBridge(QObject):
    """裏スレッドの検出・テスト入力 → 画面への結果の受け渡し。"""

    finished = Signal(object)


class WindowPickerDialog(QDialog):
    """窓の一覧から1つ選ぶ小窓。rows は enumerate_visible_windows
    の形 ({title, window_class, process_name} の並び)。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        self.setWindowTitle("投げ先の窓を選ぶ - EDDy")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        self._status_label = QLabel(
            f"{len(rows)} 件の窓があります" if rows
            else "選べる窓がありません")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for row in rows:
            text = row.get("title") or ""
            detail = " / ".join(
                part for part in (row.get("process_name") or "",
                                  row.get("window_class") or "") if part)
            item = QListWidgetItem(f"{text}（{detail}）" if detail else text)
            item.setData(Qt.ItemDataRole.UserRole, row)
            self._list.addItem(item)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_row(self):
        item = self._list.currentItem()
        if item is None:
            return None
        row = item.data(Qt.ItemDataRole.UserRole)
        return row if isinstance(row, dict) else None


class AiTargetDialog(QDialog):
    """「AI の投げ先」の設定画面。

    builtin_profiles は組み込みプロファイルの並び (copilot_io の
    裏方から取ったもの)。custom_profile は保存済みのカスタム
    (無ければ None)。enumerate_windows / prober / filler は
    差し替え口で、prober(profile) は .ok / .message / .candidates、
    filler(text, profile) は .ok / .message / .needs_confirm を
    持つ結果を返す (copilot_io の ProbeResult / SendResult)。"""

    def __init__(self, builtin_profiles, selection, custom_profile,
                 parent=None, *, enumerate_windows=None, prober=None,
                 filler=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        self.setWindowTitle("AI の投げ先 - EDDy")
        self.resize(560, 520)
        self._builtins = [p for p in (builtin_profiles or [])
                          if isinstance(p, dict) and p.get("name")]
        self._enumerate_windows = enumerate_windows or (lambda: [])
        self._prober = prober
        self._filler = filler
        self._bridge = _OpBridge(self)
        self._bridge.finished.connect(self._on_op_finished)
        self._op_thread = None
        self._op_done = None
        self.custom_dirty = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._target_combo = QComboBox()
        self._target_combo.addItem(
            "自動（いままでどおり）", aitmod.AUTO_SELECTION)
        for profile in self._builtins:
            self._target_combo.addItem(
                aitmod.display_name(profile), profile["name"])
        self._target_combo.addItem("カスタム", aitmod.CUSTOM_PROFILE_NAME)
        index = self._target_combo.findData(selection or "")
        self._target_combo.setCurrentIndex(index if index >= 0 else 0)
        self._target_combo.currentIndexChanged.connect(
            self._on_target_changed)
        form.addRow("投げ先", self._target_combo)
        layout.addLayout(form)

        self._custom_group = QGroupBox("カスタムの内容")
        custom_form = QFormLayout(self._custom_group)
        self._label_edit = QLineEdit()
        self._process_edit = QLineEdit()
        self._class_edit = QLineEdit()
        self._title_edit = QLineEdit()
        self._aid_edit = QLineEdit()
        self._appid_edit = QLineEdit()
        custom_form.addRow("表示名", self._label_edit)
        custom_form.addRow("プロセス名", self._process_edit)
        custom_form.addRow("ウィンドウクラス", self._class_edit)
        custom_form.addRow("タイトルに含む文字列", self._title_edit)
        custom_form.addRow("入力欄の AutomationId", self._aid_edit)
        custom_form.addRow("アプリIDの頭（任意）", self._appid_edit)
        layout.addWidget(self._custom_group)

        row = QHBoxLayout()
        self._pick_button = QPushButton("プローブで指定…")
        self._pick_button.clicked.connect(self._pick_window)
        self._detect_button = QPushButton("入力欄を検出")
        self._detect_button.clicked.connect(self._detect_input)
        self._test_button = QPushButton("テスト入力")
        self._test_button.clicked.connect(self._test_fill)
        for button in (self._pick_button, self._detect_button,
                       self._test_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self._status_label = QLabel()
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 欄の初期値: 保存済みカスタム → 選択中の組み込み (下書き
        # として) → 空、の順。初期値の流し込みは「変更」に数えない。
        initial = None
        if isinstance(custom_profile, dict):
            initial = custom_profile
        elif selection:
            initial = aitmod.find_profile(self._builtins, selection)
        self._set_fields(aitmod.fields_from_profile(initial))
        for edit in self._field_edits():
            edit.textChanged.connect(self._mark_dirty)
        self._update_custom_enabled()

    # -- 欄まわり ------------------------------------------------------
    def _field_edits(self):
        return (self._label_edit, self._process_edit, self._class_edit,
                self._title_edit, self._aid_edit, self._appid_edit)

    def _set_fields(self, fields):
        for edit, key in zip(self._field_edits(), aitmod.FIELD_KEYS):
            edit.setText(fields.get(key) or "")

    def current_fields(self) -> dict:
        return {key: edit.text()
                for key, edit in zip(aitmod.FIELD_KEYS,
                                     self._field_edits())}

    def _mark_dirty(self):
        self.custom_dirty = True

    def selection_name(self) -> str:
        return self._target_combo.currentData() or ""

    def custom_profile(self) -> dict:
        return aitmod.build_custom_profile(self.current_fields())

    def _on_target_changed(self):
        self._update_custom_enabled()

    def _update_custom_enabled(self):
        self._custom_group.setEnabled(
            self.selection_name() == aitmod.CUSTOM_PROFILE_NAME)

    def _ensure_custom(self):
        """カスタムを編集する操作の前に、カスタム選択へ寄せる。"""
        if self.selection_name() != aitmod.CUSTOM_PROFILE_NAME:
            index = self._target_combo.findData(aitmod.CUSTOM_PROFILE_NAME)
            if index >= 0:
                self._target_combo.setCurrentIndex(index)

    def _show_status(self, message, error=False):
        self._status_label.setStyleSheet(
            "color: #c00000;" if error else "")
        self._status_label.setText(message)

    # -- プローブで指定 --------------------------------------------------
    def _pick_window(self):
        try:
            rows = self._enumerate_windows()
        except Exception:  # noqa: BLE001  一覧が取れないだけ
            rows = []
        picker = WindowPickerDialog(rows, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        row = picker.selected_row()
        if row is None:
            return
        self._ensure_custom()
        fields = self.current_fields()
        picked = aitmod.fields_from_window_row(row)
        if fields["label"]:  # 表示名は手で入れていればそちらを残す
            picked.pop("label", None)
        fields.update(picked)
        self._set_fields(fields)
        self.custom_dirty = True
        self._show_status(f"窓から取り込みました: {row.get('title') or ''}")

    # -- 検出とテスト入力 (裏のスレッド) --------------------------------
    def _start_op(self, fn, on_result):
        if self._op_thread is not None:
            return
        self._op_done = on_result
        for button in (self._pick_button, self._detect_button,
                       self._test_button):
            button.setEnabled(False)

        def run():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001  文にして画面へ戻す
                result = SimpleNamespace(
                    ok=False, message=f"失敗しました（{e}）",
                    candidates=[], needs_confirm=False)
            self._bridge.finished.emit(result)

        self._op_thread = threading.Thread(
            target=run, daemon=True, name="editor-ai-target-op")
        self._op_thread.start()

    def _on_op_finished(self, result):
        self._op_thread = None
        for button in (self._pick_button, self._detect_button,
                       self._test_button):
            button.setEnabled(True)
        on_result, self._op_done = self._op_done, None
        if on_result is not None:
            on_result(result)

    def _detect_input(self):
        if self._prober is None:
            self._show_status("この経路では入力欄を検出できません",
                              error=True)
            return
        self._ensure_custom()
        profile = self.custom_profile()
        self._show_status("入力欄を検出しています…")
        self._start_op(lambda: self._prober(profile),
                       self._on_probe_result)

    def _on_probe_result(self, result):
        if not getattr(result, "ok", False):
            self._show_status(
                getattr(result, "message", "検出できませんでした"),
                error=True)
            return
        candidates = getattr(result, "candidates", None) or []
        aid = ""
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get(
                    "automation_id"):
                aid = candidate["automation_id"]
                break
        if aid:
            self._aid_edit.setText(aid)
            self.custom_dirty = True
            self._show_status(
                f"{result.message}。"
                f"AutomationId「{aid}」を欄に入れました")
        else:
            self._show_status(
                f"{result.message}。ただし AutomationId が空のため、"
                "欄には入れられませんでした")

    def _test_fill(self):
        if self._filler is None:
            self._show_status("この経路ではテスト入力できません",
                              error=True)
            return
        selection = self.selection_name()
        if selection == aitmod.CUSTOM_PROFILE_NAME:
            profile = self.custom_profile()
        elif selection:
            profile = selection  # 組み込みは名前で渡す
        else:
            profile = None  # 自動
        self._show_status("テスト文を入力欄に入れています…")
        self._start_op(
            lambda: self._filler(aitmod.TEST_TEXT, profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    # -- 保存の検証 ------------------------------------------------------
    def accept(self):  # noqa: N802  Qt の上書き
        if self.selection_name() == aitmod.CUSTOM_PROFILE_NAME:
            fields = self.current_fields()
            if not (fields["process_name"].strip()
                    or fields["window_class"].strip()
                    or fields["app_id_prefix"].strip()):
                # 全部空のプロファイルは「最初の窓なら何でも」に
                # なってしまうので、保存させない。
                self._show_status(
                    "カスタムには、プロセス名・ウィンドウクラス・"
                    "アプリIDの頭のどれかが必要です", error=True)
                return
        super().accept()
