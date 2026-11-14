# script_runner_ai.py
# PHAL (script_runner.py) 独自の「AI の窓」設定と調査画面。
#
# 1-14 までは EDDy の投げ先設定 (editor 節) を借りていたが、今後は
# ダウンロードの自動化など PHAL 側の自動化が増えるので、投げ先は
# PHAL が自分の設定 (settings.json の script_runner 節) に持つ。
# EDDy の設定とは混ぜない。投げ先は相手ごとに窓の目印が違うので、
# 名前つきのプロファイル (ai_profiles) として複数持ち、選ばれて
# いる1件 (ai_profile_active) を使う。ai_target には有効な
# プロファイルの写しを保存のたびに同期しておく (旧キーだけを
# 読む側がそのまま動くように)。
#
# プロファイルの形は copilot_loop のプロファイル辞書にそろえる
# (editor/ai_target.build_custom_profile と同じ並び + 入力欄の
# クラス・名前)。入力欄の合わせ方は、まず AutomationId、無ければ
# クラスと名前 (copilot_loop._input_box が両方を見る)。
#
# 網頁の中の入力欄 (contenteditable) は AutomationId を持たない
# ことが多く、EDDy の検出ではページ全体の Document (RootWebArea)
# を掴んで失敗した (2026-11-14 実測)。なので調査は copilot_io の
# 深い走査 (scan) で候補を全部出し、人が選ぶ形にする。
#
# UIA は裏方プロセス (copilot_io_worker) の中だけ、という線引きは
# 従来どおり。窓の一覧だけは素の Win32 列挙 (editor の投げ先画面と
# 同じ作法で、UIA は使わない)。
import ctypes
import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

# PHAL のプロファイルの安定した名前 (設定の中で1つだけ持つ)。
PROFILE_NAME = "phal-ai"

# 欄のキーと表示名 (この並びで画面に出す)。
FIELD_LABELS = (
    ("label", "表示名"),
    ("process_name", "プロセス名"),
    ("window_class", "ウィンドウクラス"),
    ("window_title_contains", "タイトルに含む文字列"),
    ("input_automation_id", "入力欄の AutomationId"),
    ("input_class", "入力欄のクラス"),
    ("input_name", "入力欄の名前"),
    ("send_automation_id", "送信ボタンの AutomationId"),
    ("send_name", "送信ボタンの名前"),
    ("send_class", "送信ボタンのクラス"),
    ("app_id_prefix", "アプリIDの頭（任意）"),
)
FIELD_KEYS = tuple(key for key, _label in FIELD_LABELS)

# 自動化の切替 (settings.json の script_runner 節の直下に置く
# auto_* キー)。既定は全部 False (手動) で、実機で安全が確かめ
# られた段から1つずつ入れる (持ち主の方針、2026-11-14)。実行の
# 自動化 (auto_run、2026-11-15 追加) は、パネルを起動したあとに
# 新しく現れたファイルだけが対象で、起動時に既にあった分は
# 決して自動では走らせない (窓側が到着の前後を見分けている)。
AUTO_LABELS = (
    ("auto_dl", "DLを自動で押す"),
    ("auto_run", "実行を自動で始める"),
    ("auto_answer", "回答を自動で入れる"),
    ("auto_send", "送信を自動で押す"),
    # 診断の自動応答 (1-43): AI の発言の中の目印の行を見つけたら、
    # 診断の束 (1-41) を入力欄へ入れて送信まで自動でやる。呼ぶのは
    # AI 側で、人は切替を入れておくだけ。
    ("auto_diag", "診断の自動応答"),
)
AUTO_KEYS = tuple(key for key, _label in AUTO_LABELS)

# テスト入力の文 (3行)。入力欄に入れるだけで、送信はしない。
TEST_TEXT = (
    "PHAL からのテストです。\n"
    "これは送信されません。入力欄に入るかだけを確かめています。\n"
    "改行がそのまま入っていれば成功です。"
)

# カスタムの欄に無い項目の初期値 (copilot_loop の組み込みと桁を
# そろえた無難な値。editor/ai_target と同じ判断)。
_DEFAULT_RENDER_CLASS = "Chrome_RenderWidgetHostHWND"
_DEFAULT_SEND_BUTTONS = ["送信", "Send", "メッセージの送信"]
_DEFAULT_BUSY_BUTTONS = ["停止", "Stop", "生成を停止する"]
_DEFAULT_INPUT_BAND_PX = 170


def sanitize_fields(value) -> dict:
    """保存された投げ先の欄を整える。FIELD_KEYS の文字列だけ残す。"""
    value = value if isinstance(value, dict) else {}
    fields = {}
    for key in FIELD_KEYS:
        item = value.get(key)
        fields[key] = item.strip() if isinstance(item, str) else ""
    return fields


def is_configured(fields) -> bool:
    """窓を特定できる情報が1つでもあるか。

    全部空のプロファイルは「最初の窓なら何でも」になってしまう
    ので、設定済みとはみなさない (EDDy の保存検証と同じ)。"""
    fields = sanitize_fields(fields)
    return bool(fields["process_name"] or fields["window_class"]
                or fields["app_id_prefix"])


def send_configured(fields) -> bool:
    """送信ボタンの目印が1つでもあるか。

    行のボタンが「送信」の役割まで進んでよいかの判定 (script_runner
    が使う)。目印が無ければ、回答を入れても「AIへ回答」のままに
    する (押しようが無いボタンを出さない)。"""
    fields = sanitize_fields(fields)
    return bool(fields["send_automation_id"] or fields["send_name"]
                or fields["send_class"])


def sanitize_automation(value) -> dict:
    """自動化の切替を整える。{auto_dl, auto_run, auto_answer,
    auto_send, auto_diag} の真偽だけの辞書を返す。

    value は settings.json の script_runner 節そのもの (キーは節の
    直下) か、切替だけの辞書。True のものだけ True で、それ以外
    (欠け・文字列など) は全部 False (手動) に倒す。自動化は
    既定で切るのが安全側。"""
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) is True for key in AUTO_KEYS}


# ---------------------------------------------------------------
# 設定のプロファイル (名前つきで複数持ち、切り替える)
# ---------------------------------------------------------------
# script_runner 節の形:
#   ai_profiles       … [{"name": 名前, "target": 欄の辞書}, ...]
#   ai_profile_active … 有効なプロファイルの位置 (読み取りでは
#                       名前でも受け付ける)
#   ai_target         … 有効なプロファイルの欄の写し。保存のたびに
#                       同期するので、旧キーだけを読む側もそのまま
#                       動く (読み取りの正本は ai_profiles 側)。
DEFAULT_PROFILE_NAME = "既定"


def _clean_profile_name(value) -> str:
    name = value.strip() if isinstance(value, str) else ""
    return name or DEFAULT_PROFILE_NAME


def _profile_entry(name, raw_target, fallback_source=None) -> dict:
    target = raw_target if isinstance(raw_target, dict) else fallback_source
    return {"name": _clean_profile_name(name),
            "target": sanitize_fields(target)}


def sanitize_profiles(section):
    """script_runner 節から (プロファイルの並び, 有効な位置) を出す。

    プロファイルは {"name": 名前, "target": 欄の辞書} の形に整える。
    ai_profiles が無ければ、旧 ai_target を「既定」1件に包む (移行)。
    どちらも無ければ空の「既定」1件。有効な位置は
    ai_profile_active (位置か名前) から決め、当てはまらなければ
    先頭にする。"""
    section = section if isinstance(section, dict) else {}
    profiles = []
    raw_profiles = section.get("ai_profiles")
    if isinstance(raw_profiles, list):
        for entry in raw_profiles:
            if not isinstance(entry, dict):
                continue
            profiles.append(_profile_entry(
                entry.get("name"), entry.get("target"), entry))
    if not profiles:
        profiles = [_profile_entry(
            DEFAULT_PROFILE_NAME, section.get("ai_target"))]
    active = 0
    raw_active = section.get("ai_profile_active")
    if isinstance(raw_active, bool):
        raw_active = None
    if isinstance(raw_active, int):
        active = min(max(raw_active, 0), len(profiles) - 1)
    elif isinstance(raw_active, str):
        wanted = raw_active.strip()
        for index, profile in enumerate(profiles):
            if profile["name"] == wanted:
                active = index
                break
    return profiles, active


def active_profile_fields(section) -> dict:
    """有効なプロファイルの欄 (読み取りの入口)。

    回答・送信・チャットの見張りは、ここを通して「今選ばれている
    投げ先」の欄だけを見る。"""
    profiles, active = sanitize_profiles(section)
    return profiles[active]["target"]


def profiles_settings(profiles, active_index) -> dict:
    """保存用の辞書 {ai_profiles, ai_profile_active, ai_target} を作る。

    ai_target には有効なプロファイルの欄を写す (旧キーだけを読む側
    がそのまま動くように、保存のたび同期する)。"""
    cleaned = []
    for entry in profiles if isinstance(profiles, list) else []:
        if not isinstance(entry, dict):
            continue
        cleaned.append(_profile_entry(
            entry.get("name"), entry.get("target"), entry))
    if not cleaned:
        cleaned = [_profile_entry(DEFAULT_PROFILE_NAME, None)]
    if isinstance(active_index, bool) \
            or not isinstance(active_index, int):
        active_index = 0
    active = min(max(active_index, 0), len(cleaned) - 1)
    return {
        "ai_profiles": cleaned,
        "ai_profile_active": active,
        "ai_target": cleaned[active]["target"],
    }


def build_profile(fields) -> dict:
    """欄の値から、copilot_io へ渡すプロファイル辞書を組み立てる。

    形は copilot_loop のプロファイルにそろえる。欄に無い項目は
    初期値を入れ、応答の切り出し用マーカーは空にしておく (PHAL は
    回答を送る側で、切り出しはしない。当てずっぽうで埋めると
    誤動作の元になるため)。"""
    fields = sanitize_fields(fields)
    return {
        "name": PROFILE_NAME,
        "label": fields["label"],
        "process_name": fields["process_name"],
        "window_class": fields["window_class"],
        "window_title_contains": fields["window_title_contains"],
        "app_id_prefix": fields["app_id_prefix"],
        "render_class": _DEFAULT_RENDER_CLASS,
        "input_automation_id": fields["input_automation_id"],
        "input_class": fields["input_class"],
        "input_name": fields["input_name"],
        # 送信ボタンの目印 (PHAL が「送信」まで回すための3点)。
        # copilot_loop の組み込みが使う send_button (名前の並び) とは
        # 別物で、press_send_button がこちらだけを見る。
        "send_automation_id": fields["send_automation_id"],
        "send_name": fields["send_name"],
        "send_class": fields["send_class"],
        "send_button": list(_DEFAULT_SEND_BUTTONS),
        "busy_button": list(_DEFAULT_BUSY_BUTTONS),
        "assistant_marker": "",
        "user_marker": "",
        "idle_marker": [],
        "code_copy_button": [],
        "input_band_px": _DEFAULT_INPUT_BAND_PX,
    }


def fields_from_window_row(row) -> dict:
    """窓の一覧の1行 {title, process_name, window_class} から、
    欄へ写すぶんを作る (editor の投げ先画面と同じ作法)。

    タイトルは、そのまま入れると会話の題まで含んでしまうことが
    あるので、「 - 」で区切った最後の部分 (アプリ名のことが多い)
    を使う。区切りが無ければタイトル全体。どちらも60字で切る。"""
    row = row if isinstance(row, dict) else {}
    title = row.get("title") or ""
    title_contains = title.rsplit(" - ", 1)[-1].strip()[:60]
    return {
        "label": title_contains,
        "process_name": row.get("process_name") or "",
        "window_class": row.get("window_class") or "",
        "window_title_contains": title_contains,
    }


def candidate_text(candidate) -> str:
    """調査の候補1件を、人が選べる形の1行にする。"""
    candidate = candidate if isinstance(candidate, dict) else {}

    def show(value):
        return value if isinstance(value, str) and value else "(なし)"

    return (
        f"{candidate.get('control_type') or '?'}"
        f" / 名前: {show(candidate.get('name'))}"
        f" / AutomationId: {show(candidate.get('automation_id'))}"
        f" / クラス: {show(candidate.get('class_name'))}"
        f" / 値の書き込み: "
        f"{'可' if candidate.get('value_pattern') else '不可'}"
        f" / テキスト: "
        f"{'あり' if candidate.get('text_pattern') else 'なし'}")


def button_candidate_text(candidate) -> str:
    """送信ボタンの候補1件を、人が選べる形の1行にする。

    入力欄の候補と並びはそろえ、最後の段だけ「押せるか
    (InvokePattern)」に替える (ボタンに値の書き込みは関係ない)。"""
    candidate = candidate if isinstance(candidate, dict) else {}

    def show(value):
        return value if isinstance(value, str) and value else "(なし)"

    return (
        f"{candidate.get('control_type') or '?'}"
        f" / 名前: {show(candidate.get('name'))}"
        f" / AutomationId: {show(candidate.get('automation_id'))}"
        f" / クラス: {show(candidate.get('class_name'))}"
        f" / 押下: "
        f"{'可' if candidate.get('invoke_pattern') else '不可'}")


# ---------------------------------------------------------------
# 窓の一覧 (素の Win32 列挙。UIA は使わない)
# ---------------------------------------------------------------
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


def _default_scanner(profile):
    """調査の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.scan_input(profile)


def _default_filler(text, profile):
    """テスト入力の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile)


def _default_button_scanner(profile):
    """送信ボタン調査の既定の実体 (copilot_io の裏方経由)。"""
    import copilot_io

    return copilot_io.scan_send_buttons(profile)


class _OpBridge(QObject):
    """裏スレッドの調査・テスト入力 → 画面への結果の受け渡し。"""

    finished = Signal(object)


class WindowPickerDialog(QDialog):
    """窓の一覧から1つ選ぶ小窓。rows は enumerate_visible_windows
    の形 ({title, window_class, process_name} の並び)。"""

    def __init__(self, rows, parent=None):
        super().__init__(parent)
        self.setWindowTitle("投げ先の窓を選ぶ - PHAL")
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        # 絞り込み。打つそばから、行の表示文 (タイトル / プロセス名 /
        # クラス) に当てはまる行だけ残す。大文字小文字は区別せず、
        # 空白で区切った語は全部含む行だけ (AND)。空なら全行。
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(
            "絞り込み (タイトル・プロセス名・クラス名の一部)")
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_edit)
        self._total = len(rows)
        self._all_text = (
            f"{len(rows)} 件の窓があります" if rows
            else "選べる窓がありません")
        self._status_label = QLabel(self._all_text)
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
        # 開いたらすぐ打てるよう、絞り込み欄にフォーカスを置く。
        self._filter_edit.setFocus()

    def _apply_filter(self, text):
        terms = [term.casefold() for term in text.split() if term]
        visible = 0
        for index in range(self._list.count()):
            item = self._list.item(index)
            haystack = item.text().casefold()
            matched = all(term in haystack for term in terms)
            item.setHidden(not matched)
            if matched:
                visible += 1
        if terms:
            self._status_label.setText(
                f"{visible} 件が当てはまります (全 {self._total} 件)")
        else:
            self._status_label.setText(self._all_text)
        current = self._list.currentItem()
        if current is not None and current.isHidden():
            # 選んでいる行が隠れたら、見えている先頭へ移す
            # (無ければ選択なし)。隠れた行が返ることはない。
            self._list.setCurrentItem(None)
            for index in range(self._list.count()):
                item = self._list.item(index)
                if not item.isHidden():
                    self._list.setCurrentItem(item)
                    break

    def selected_row(self):
        item = self._list.currentItem()
        if item is None:
            return None
        row = item.data(Qt.ItemDataRole.UserRole)
        return row if isinstance(row, dict) else None


def _copy_all_button(list_widget, status_label, heading):
    """候補の一覧を全部クリップボードへ入れるボタン。

    調査の結果は画面に収まらないことがある (Web の窓はボタンが
    大量にある)。結果をそのまま AI へ貼って見せられるようにする。
    """
    button = QPushButton("一覧をコピー")

    def _copy():
        lines = [heading]
        for row in range(list_widget.count()):
            lines.append(list_widget.item(row).text())
        QApplication.clipboard().setText("\n".join(lines))
        status_label.setText(f"{status_label.text()}  一覧をコピーしました。")

    button.clicked.connect(_copy)
    return button


class InputCandidateDialog(QDialog):
    """調査で拾った入力欄の候補から1つ選ぶ小窓。"""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("入力欄の候補 - PHAL")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        self._status_label = QLabel(
            f"候補が {len(candidates)} 件あります。"
            "入力欄だと思うものを選んでください")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(candidate_text(candidate))
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self._list.addItem(item)
        if candidates:
            self._list.setCurrentRow(0)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        layout.addWidget(_copy_all_button(
            self._list, self._status_label, "入力欄の候補 - PHAL"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate(self):
        item = self._list.currentItem()
        if item is None:
            return None
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, dict) else None


class ButtonCandidateDialog(QDialog):
    """調査で拾った送信ボタンの候補から1つ選ぶ小窓。

    入力欄の候補の小窓 (InputCandidateDialog) と同じ形で、行の文だけ
    ボタン用 (button_candidate_text) に替えたもの。"""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("送信ボタンの候補 - PHAL")
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        self._status_label = QLabel(
            f"候補が {len(candidates)} 件あります。"
            "送信ボタンだと思うものを選んでください")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._list = QListWidget()
        for candidate in candidates:
            item = QListWidgetItem(button_candidate_text(candidate))
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self._list.addItem(item)
        if candidates:
            self._list.setCurrentRow(0)
        self._list.itemActivated.connect(lambda _item: self.accept())
        layout.addWidget(self._list, 1)
        layout.addWidget(_copy_all_button(
            self._list, self._status_label, "送信ボタンの候補 - PHAL"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_candidate(self):
        item = self._list.currentItem()
        if item is None:
            return None
        candidate = item.data(Qt.ItemDataRole.UserRole)
        return candidate if isinstance(candidate, dict) else None


class AiWindowDialog(QDialog):
    """PHAL の「AI の窓」の設定・調査画面。

    enumerate_windows / scanner / filler / button_scanner は差し替え口で、
    scanner・button_scanner(profile) は .ok / .message / .candidates、
    filler(text, profile) は .ok / .message / .needs_confirm を持つ
    結果を返す (copilot_io の ScanResult / SendResult)。調査とテスト
    入力は数秒かかることがあるので、裏のスレッドで回して橋で受け取る
    (EDDy の投げ先画面と同じ作法)。"""

    def __init__(self, fields, parent=None, *, enumerate_windows=None,
                 scanner=None, filler=None, button_scanner=None,
                 automation=None, profiles=None, active_index=0,
                 diag_text_provider=None, error_color="#c00000"):
        super().__init__(parent)
        # 状態ラベルのエラー色。窓側がテーマに合わせて渡す (1-44)。
        # 渡されなければ従来の色のまま。
        self._error_color = error_color
        self.setWindowTitle("AI の窓 - PHAL")
        self.resize(560, 720)
        self._enumerate_windows = (
            enumerate_windows or enumerate_visible_windows)
        self._scanner = scanner or _default_scanner
        self._filler = filler or _default_filler
        self._button_scanner = button_scanner or _default_button_scanner
        # 診断の束を作る役 (窓側が渡す)。無ければ「診断をAIへ貼る」
        # ボタンは出さない (この画面を単独で開く使い方との互換)。
        self._diag_text_provider = diag_text_provider
        self._bridge = _OpBridge(self)
        self._bridge.finished.connect(self._on_op_finished)
        self._op_thread = None
        self._op_done = None

        # プロファイル (名前つきの投げ先)。渡されなければ fields を
        # 「既定」1件として扱う (従来の呼び方との互換)。
        self._profiles = []
        for entry in profiles if isinstance(profiles, list) else []:
            if isinstance(entry, dict):
                self._profiles.append(_profile_entry(
                    entry.get("name"), entry.get("target"), entry))
        if not self._profiles:
            self._profiles = [_profile_entry(
                DEFAULT_PROFILE_NAME, fields)]
        if isinstance(active_index, bool) \
                or not isinstance(active_index, int):
            active_index = 0
        self._current = min(
            max(active_index, 0), len(self._profiles) - 1)

        layout = QVBoxLayout(self)

        # プロファイルの切り替え行 (一番上)。切り替えると欄の表示が
        # そのプロファイルの内容に替わる。編集中の欄は、切り替える
        # 前に今のプロファイルへ控えておく (黙って消さない)。
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("設定"))
        self._profile_combo = QComboBox()
        self._profile_combo.currentIndexChanged.connect(
            self._on_profile_switched)
        profile_row.addWidget(self._profile_combo, 1)
        self._add_profile_button = QPushButton("追加")
        self._add_profile_button.clicked.connect(self._add_profile)
        self._dup_profile_button = QPushButton("複写")
        self._dup_profile_button.clicked.connect(self._duplicate_profile)
        self._rename_profile_button = QPushButton("名前変更")
        self._rename_profile_button.clicked.connect(self._rename_profile)
        self._delete_profile_button = QPushButton("削除")
        self._delete_profile_button.clicked.connect(self._delete_profile)
        for button in (self._add_profile_button, self._dup_profile_button,
                       self._rename_profile_button,
                       self._delete_profile_button):
            profile_row.addWidget(button)
        layout.addLayout(profile_row)
        self._rebuild_profile_combo()

        form = QFormLayout()
        self._edits = {}
        for key, label in FIELD_LABELS:
            edit = QLineEdit()
            self._edits[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._pick_button = QPushButton("窓の一覧…")
        self._pick_button.clicked.connect(self._pick_window)
        self._scan_button = QPushButton("入力欄を調査")
        self._scan_button.clicked.connect(self._scan_inputs)
        self._scan_send_button = QPushButton("送信ボタンを調査")
        self._scan_send_button.clicked.connect(self._scan_send_buttons)
        self._test_button = QPushButton("テスト入力")
        self._test_button.clicked.connect(self._test_fill)
        for button in (self._pick_button, self._scan_button,
                       self._scan_send_button, self._test_button):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        # 診断の行: PHAL の直近の動きの記録を、表示中のプロファイル
        # の入力欄へ入れる (入れるだけ。送信は人が押す。テスト入力
        # と同じ線引き)。ループが止まったとき、その理由を AI へ
        # そのまま渡すためのもの (1-41)。
        if self._diag_text_provider is not None:
            diag_row = QHBoxLayout()
            self._diag_button = QPushButton("診断をAIへ貼る")
            self._diag_button.clicked.connect(self._fill_diagnostics)
            diag_row.addWidget(self._diag_button)
            diag_row.addStretch(1)
            layout.addLayout(diag_row)
        else:
            self._diag_button = None

        # 自動化の切替。ai_target の欄とは別の設定 (script_runner 節
        # の auto_* キー) なので、枠を分けて出す。既定は全部手動。
        auto_group = QGroupBox("自動化")
        auto_layout = QVBoxLayout(auto_group)
        self._auto_boxes = {}
        auto_given = sanitize_automation(automation)
        for key, label in AUTO_LABELS:
            box = QCheckBox(label)
            box.setChecked(auto_given[key])
            self._auto_boxes[key] = box
            auto_layout.addWidget(box)
        layout.addWidget(auto_group)

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

        self._set_fields(self._profiles[self._current]["target"])

    # -- プロファイルまわり ---------------------------------------------
    def _rebuild_profile_combo(self):
        """プルダウンをプロファイルの並びから作り直す。

        作り直しの間の currentIndexChanged は止める (切り替え扱いに
        なって編集中の欄を上書きしないように)。"""
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for profile in self._profiles:
            self._profile_combo.addItem(profile["name"])
        self._profile_combo.setCurrentIndex(self._current)
        self._profile_combo.blockSignals(False)
        self._sync_profile_buttons()

    def _sync_profile_buttons(self):
        # 最後の1件は消せない (投げ先が必ず1つは要る)。
        self._delete_profile_button.setEnabled(len(self._profiles) > 1)

    def _stash_current(self):
        """編集中の欄を、今のプロファイルへ控える。"""
        self._profiles[self._current]["target"] = self.fields()

    def _on_profile_switched(self, index):
        if index < 0 or index == self._current \
                or index >= len(self._profiles):
            return
        self._stash_current()
        self._current = index
        self._set_fields(self._profiles[index]["target"])
        self._show_status(
            f"設定「{self._profiles[index]['name']}」に切り替えました")

    def profiles(self) -> list:
        """全プロファイル (編集中の欄は今のものへ控えたうえで)。"""
        self._stash_current()
        return [_profile_entry(p["name"], p["target"])
                for p in self._profiles]

    def active_index(self) -> int:
        """今選ばれているプロファイルの位置。"""
        return self._current

    def _ask_profile_name(self, title, initial):
        """名前を聞く。やめた・空のときは None (空は理由を出す)。"""
        text, ok = QInputDialog.getText(self, title, "名前:", text=initial)
        if not ok:
            return None
        name = text.strip()
        if not name:
            self._show_status("名前が空なので、やめました", error=True)
            return None
        return name

    def _add_profile(self):
        name = self._ask_profile_name("設定の追加", "")
        if name is None:
            return
        self._stash_current()
        self._profiles.append(_profile_entry(name, None))
        self._current = len(self._profiles) - 1
        self._rebuild_profile_combo()
        self._set_fields(self._profiles[self._current]["target"])
        self._show_status(f"設定「{name}」を追加しました")

    def _duplicate_profile(self):
        current_name = self._profiles[self._current]["name"]
        name = self._ask_profile_name(
            "設定の複写", f"{current_name} のコピー")
        if name is None:
            return
        self._stash_current()
        copied = _profile_entry(
            name, self._profiles[self._current]["target"])
        self._profiles.insert(self._current + 1, copied)
        self._current += 1
        self._rebuild_profile_combo()
        self._set_fields(copied["target"])
        self._show_status(f"設定「{name}」を複写しました")

    def _rename_profile(self):
        name = self._ask_profile_name(
            "名前変更", self._profiles[self._current]["name"])
        if name is None:
            return
        self._profiles[self._current]["name"] = name
        self._rebuild_profile_combo()
        self._show_status(f"名前を「{name}」に変えました")

    def _delete_profile(self):
        if len(self._profiles) <= 1:
            return  # 最後の1件は消さない (ボタンも無効にしてある)
        name = self._profiles[self._current]["name"]
        del self._profiles[self._current]
        self._current = min(self._current, len(self._profiles) - 1)
        self._rebuild_profile_combo()
        self._set_fields(self._profiles[self._current]["target"])
        self._show_status(f"設定「{name}」を削除しました")

    # -- 欄まわり ------------------------------------------------------
    def _set_fields(self, fields):
        for key in FIELD_KEYS:
            self._edits[key].setText(fields.get(key) or "")

    def fields(self) -> dict:
        return sanitize_fields(
            {key: self._edits[key].text() for key in FIELD_KEYS})

    def automation(self) -> dict:
        """自動化の切替の今の値 (script_runner 節へ保存する形)。"""
        return {key: bool(self._auto_boxes[key].isChecked())
                for key in AUTO_KEYS}

    def _show_status(self, message, error=False):
        self._status_label.setStyleSheet(
            f"color: {self._error_color};" if error else "")
        self._status_label.setText(message)

    # -- 窓の一覧 --------------------------------------------------------
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
        fields = self.fields()
        picked = fields_from_window_row(row)
        if fields["label"]:  # 表示名は手で入れていればそちらを残す
            picked.pop("label", None)
        fields.update(picked)
        self._set_fields(fields)
        self._show_status(f"窓から取り込みました: {row.get('title') or ''}")

    # -- 調査とテスト入力 (裏のスレッド) --------------------------------
    def _op_widgets(self):
        """調査・テスト入力の間は止める部品 (押下系とプロファイル行)。"""
        widgets = [self._pick_button, self._scan_button,
                   self._scan_send_button, self._test_button,
                   self._profile_combo, self._add_profile_button,
                   self._dup_profile_button, self._rename_profile_button,
                   self._delete_profile_button]
        if self._diag_button is not None:
            widgets.append(self._diag_button)
        return tuple(widgets)

    def _start_op(self, fn, on_result):
        if self._op_thread is not None:
            return
        self._op_done = on_result
        for widget in self._op_widgets():
            widget.setEnabled(False)

        def run():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001  文にして画面へ戻す
                result = SimpleNamespace(
                    ok=False, message=f"失敗しました（{e}）",
                    candidates=[], needs_confirm=False)
            self._bridge.finished.emit(result)

        self._op_thread = threading.Thread(
            target=run, daemon=True, name="phal-ai-window-op")
        self._op_thread.start()

    def _on_op_finished(self, result):
        self._op_thread = None
        for widget in self._op_widgets():
            widget.setEnabled(True)
        # 削除ボタンは「1件だけなら無効」に戻す。
        self._sync_profile_buttons()
        on_result, self._op_done = self._op_done, None
        if on_result is not None:
            on_result(result)

    def _profile_or_none(self):
        """今の欄からプロファイルを作る。窓を特定できなければ
        None (理由は画面に出す)。"""
        fields = self.fields()
        if not is_configured(fields):
            self._show_status(
                "先に窓の情報が必要です。「窓の一覧…」から選ぶか、"
                "プロセス名・ウィンドウクラス・アプリIDの頭のどれかを"
                "入れてください", error=True)
            return None
        return build_profile(fields)

    def _scan_inputs(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("入力欄を調査しています…")
        self._start_op(lambda: self._scanner(profile),
                       self._on_scan_result)

    def _on_scan_result(self, result):
        if not getattr(result, "ok", False):
            self._show_status(
                getattr(result, "message", "調査できませんでした"),
                error=True)
            return
        candidates = [c for c in (getattr(result, "candidates", None)
                                  or []) if isinstance(c, dict)]
        if not candidates:
            self._show_status("候補が見つかりませんでした", error=True)
            return
        picker = InputCandidateDialog(candidates, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            self._show_status(
                f"{result.message}（選びませんでした）")
            return
        candidate = picker.selected_candidate()
        if candidate is not None:
            self._apply_candidate(candidate)

    def _apply_candidate(self, candidate):
        aid = (candidate.get("automation_id") or "").strip()
        class_name = (candidate.get("class_name") or "").strip()
        name = (candidate.get("name") or "").strip()
        if aid:
            self._edits["input_automation_id"].setText(aid)
            self._edits["input_class"].setText("")
            self._edits["input_name"].setText("")
            message = f"入力欄は AutomationId「{aid}」で合わせます"
        elif class_name or name:
            self._edits["input_automation_id"].setText("")
            self._edits["input_class"].setText(class_name)
            self._edits["input_name"].setText(name)
            message = (
                "AutomationId が無いので、クラスと名前で合わせます"
                f"（クラス: {class_name or '(なし)'} / "
                f"名前: {name or '(なし)'}）")
        else:
            self._show_status(
                "その候補には、合わせる目印（AutomationId・クラス・"
                "名前）がありません", error=True)
            return
        if not candidate.get("value_pattern"):
            message += (
                "。ただし、この要素には値の書き込み（ValuePattern）"
                "が無いので、テスト入力が失敗する可能性があります")
        self._show_status(message)

    def _scan_send_buttons(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("送信ボタンを調査しています…")
        self._start_op(lambda: self._button_scanner(profile),
                       self._on_button_scan_result)

    def _on_button_scan_result(self, result):
        if not getattr(result, "ok", False):
            self._show_status(
                getattr(result, "message", "調査できませんでした"),
                error=True)
            return
        candidates = [c for c in (getattr(result, "candidates", None)
                                  or []) if isinstance(c, dict)]
        if not candidates:
            self._show_status("候補が見つかりませんでした", error=True)
            return
        picker = ButtonCandidateDialog(candidates, self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            self._show_status(
                f"{result.message}（選びませんでした）")
            return
        candidate = picker.selected_candidate()
        if candidate is not None:
            self._apply_button_candidate(candidate)

    def _apply_button_candidate(self, candidate):
        aid = (candidate.get("automation_id") or "").strip()
        class_name = (candidate.get("class_name") or "").strip()
        name = (candidate.get("name") or "").strip()
        if aid:
            self._edits["send_automation_id"].setText(aid)
            self._edits["send_class"].setText("")
            self._edits["send_name"].setText("")
            message = f"送信ボタンは AutomationId「{aid}」で合わせます"
        elif class_name or name:
            self._edits["send_automation_id"].setText("")
            self._edits["send_class"].setText(class_name)
            self._edits["send_name"].setText(name)
            message = (
                "AutomationId が無いので、クラスと名前で合わせます"
                f"（クラス: {class_name or '(なし)'} / "
                f"名前: {name or '(なし)'}）")
        else:
            self._show_status(
                "その候補には、合わせる目印（AutomationId・クラス・"
                "名前）がありません", error=True)
            return
        if not candidate.get("invoke_pattern"):
            message += (
                "。ただし、この要素には押す仕組み（InvokePattern）"
                "が無いので、押せない可能性があります")
        self._show_status(message)

    def _test_fill(self):
        profile = self._profile_or_none()
        if profile is None:
            return
        self._show_status("テスト文を入力欄に入れています…")
        self._start_op(
            lambda: self._filler(TEST_TEXT, profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    def _fill_diagnostics(self):
        """診断の束を、表示中のプロファイルの入力欄へ入れる。

        入れるだけ (送信は人が押す)。束は押した時点の状態で作る
        (窓側の diag_text_provider)。作れなければ理由を画面に出す。"""
        profile = self._profile_or_none()
        if profile is None:
            return
        try:
            text = self._diag_text_provider()
        except Exception as e:  # noqa: BLE001  文にして画面へ出す
            self._show_status(
                f"診断を組み立てられませんでした（{e}）", error=True)
            return
        if not text:
            self._show_status("診断がありません", error=True)
            return
        self._show_status("診断を入力欄に入れています…")
        self._start_op(
            lambda: self._filler(text, profile),
            lambda result: self._show_status(
                getattr(result, "message", ""),
                error=not getattr(result, "ok", False)))

    # -- 保存の検証 ------------------------------------------------------
    def accept(self):  # noqa: N802  Qt の上書き
        fields = self.fields()
        if any(fields.values()) and not is_configured(fields):
            # 窓を特定できないプロファイルは「最初の窓なら何でも」
            # になってしまうので、保存させない。全部空なら未設定に
            # 戻す操作として受け付ける。
            self._show_status(
                "窓を特定するために、プロセス名・ウィンドウクラス・"
                "アプリIDの頭のどれかが必要です", error=True)
            return
        super().accept()
