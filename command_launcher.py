# command_launcher.py
# 登録コマンド専用の小さなランチャ(旧 CraftLaunch の使い勝手に寄せたもの)。
#
# 何をするか
#   ホットキー等で1行入力窓を出し、登録した名前を打つと前方一致で補完され、
#   Enter で実行する。検索主体のコマンドパレット(command_palette.py)とは別系統で、
#   こちらは「名前を覚えて打つ」ことを優先し、fuzzy 検索はしない。
#
# 登録の置き場
#   フォルダブックマーク(launcher.py の settings.json launcher.bookmarks)と一本化する。
#   各行は {"name", "path"} に任意の "params"(パラメタ)・"dir"(作業フォルダ)を足した
#   形で、種別は path の形から自動判定する:
#     folder … フォルダのパス。開き方は launcher.jump に委ねる(あふｗ/エクスプローラ)
#     url    … http(s) URL。既定ブラウザで開く
#     uri    … onenote: などの URI スキーム。OS に渡して開く(OneNote のページ等)
#     file   … 実行ファイルやデータファイル。パラメタを付けて起こす
#   フォルダジャンプのピッカーには従来どおり folder / url が出る(params 付きの
#   file はピッカーには出ない。あちらは「移動・開く」、こちらは「実行」の場所)。
#
# 入力の作法(旧 CraftLaunch に合わせた部分)
#   - 補完は前方一致。Space で次の候補、Shift+Space で前の候補、Tab で共通部分まで確定
#   - 引数は「名前;引数」。; の後ろは補完しない
#   - Tab は、フォルダの名前が確定しているときはそのフォルダ配下への掘り下げに使う
#   - パラメタ・対象・作業フォルダでは %param%(引数)・%環境変数%・旧版式の
#     %env[名前]% を展開する。%% は % そのもの
#   - Esc は、掘り下げ中なら戻る、文字があれば消す、無ければ窓を閉じる
#   - Ctrl+K で、入力中の文字列を履歴から消す
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import launcher

# ---------------------------------------------------------------
# 登録項目
# ---------------------------------------------------------------

KIND_FOLDER = "folder"
KIND_FILE = "file"
KIND_URL = "url"
KIND_URI = "uri"
KIND_BUILTIN = "builtin"

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):(.*)$", re.DOTALL)


@dataclass
class Entry:
    """登録1件。target が開く対象(path / URL / URI)、params と workdir は file 用。"""

    name: str
    target: str
    params: str = ""
    workdir: str = ""
    kind: str = KIND_FILE


def classify(target: str, is_dir=None) -> str:
    """対象の文字列から種別を決める。is_dir を渡せばファイルシステムを見ない。"""
    text = (target or "").strip()
    if launcher.is_url(text):
        return KIND_URL
    match = _SCHEME_RE.match(text)
    if match and not (len(match.group(1)) == 1 and match.group(2)[:1] in ("\\", "/")):
        # ドライブレター(C:\…)だけはスキーム扱いしない。それ以外は URI。
        return KIND_URI
    if is_dir is None:
        try:
            is_dir = os.path.isdir(text)
        except OSError:
            is_dir = False
    return KIND_FOLDER if is_dir else KIND_FILE


def build_entries(app_settings: dict) -> list:
    """ブックマーク一覧を Entry の列にする(登録順を保つ)。"""
    entries = []
    for bookmark in launcher.load_bookmarks(app_settings):
        target = bookmark.get("path") or ""
        entries.append(Entry(
            name=bookmark.get("name") or "",
            target=target,
            params=bookmark.get("params") or "",
            workdir=bookmark.get("dir") or "",
            kind=classify(target),
        ))
    return entries


# 内蔵コマンド。実行は持ち主(feature_screen)が target の id で振り分ける。
_BUILTINS = [
    ("Command", "command", "登録の一覧と編集を開く"),
    ("Config", "config", "settings.json を開く"),
    ("Reload", "reload", "登録を読み直す"),
    ("History", "history", "実行履歴から選ぶ"),
]


def builtin_entries() -> list:
    return [Entry(name, target, kind=KIND_BUILTIN) for name, target, _ in _BUILTINS]


def list_subfolders(base: str, prefix: str = "") -> list:
    """掘り下げ用のサブフォルダ列挙。原則はピッカーと同じ launcher.list_subfolders。

    そちらは Windows のファイル属性(st_file_attributes)で隠し・システムを除くため、
    属性の無い環境では AttributeError になる。その環境向けに、ドット始まりを
    除く素朴な列挙へ落とす(実機=Windows では常に本体の経路を通る)。"""
    try:
        return launcher.list_subfolders(base, prefix)
    except AttributeError:
        keyword = (prefix or "").casefold()
        found = []
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if keyword and not entry.name.casefold().startswith(keyword):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            found.append((entry.name, entry.path))
                    except OSError:
                        continue
        except OSError:
            return []
        found.sort(key=lambda pair: pair[0].casefold())
        return found[:200]


# ---------------------------------------------------------------
# 補完・入力の解釈(純粋な部分)
# ---------------------------------------------------------------

def name_part(text: str) -> str:
    """入力のうちコマンド名の部分(最初の ; まで)。"""
    return (text or "").split(";", 1)[0]


def parse_input(text: str):
    """入力を (名前, 引数) に分ける。; が無ければ引数は空。"""
    name, sep, param = (text or "").partition(";")
    return name.strip(), (param if sep else "")


def prefix_candidates(entries, typed: str) -> list:
    """typed で始まる名前の項目を、登録順のまま返す。大文字小文字は無視する。"""
    keyword = (typed or "").casefold()
    if not keyword:
        return list(entries)
    return [e for e in entries if e.name.casefold().startswith(keyword)]


def common_prefix(names) -> str:
    """名前の列の共通する先頭部分(大文字小文字を無視して比べ、最初の名前の形で返す)。"""
    names = list(names)
    if not names:
        return ""
    first = names[0]
    lowered = [n.casefold() for n in names]
    end = len(first)
    for other in lowered[1:]:
        i = 0
        while i < min(end, len(other)) and lowered[0][i] == other[i]:
            i += 1
        end = min(end, i)
    return first[:end]


# ---------------------------------------------------------------
# 置換と実行コマンドの組み立て
# ---------------------------------------------------------------

_ESCAPED_PERCENT = "\ue000"


def expand(template: str, param: str = "", env=None) -> str:
    """%param% / %env[名前]% / %名前% を展開する。%% は % そのもの。

    見つからない環境変数は、壊さないよう元の表記のまま残す。"""
    env = os.environ if env is None else env
    text = (template or "").replace("%%", _ESCAPED_PERCENT)
    # 旧版式の %env[名前]。末尾の % を付けた書き方も受ける。
    text = re.sub(r"%env\[([^\]]+)\]%?",
                  lambda m: env.get(m.group(1), m.group(0)), text)
    text = text.replace("%param%", param or "")
    text = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%",
                  lambda m: env.get(m.group(1), m.group(0)), text)
    return text.replace(_ESCAPED_PERCENT, "%")


def build_command(entry: Entry, param_text: str = ""):
    """実行の振り分けを決める。(種別, 中身) を返す。

    folder → ("jump", パス)      持ち主が launcher.jump で開く
    url/uri → ("open", 対象)     持ち主が os.startfile で開く
    file   → ("exec", (コマンドライン文字列, 作業フォルダ or None))
    builtin → ("builtin", id)
    """
    target = expand(entry.target, param_text)
    if entry.kind == KIND_BUILTIN:
        return ("builtin", entry.target)
    if entry.kind in (KIND_URL, KIND_URI):
        return ("open", target)
    if entry.kind == KIND_FOLDER:
        return ("jump", target)
    params = expand(entry.params, param_text)
    if param_text and "%param%" not in (entry.params or ""):
        # 置き場が無いのに引数が来たら、後ろに足す(名前;対象ファイル の素朴な
        # 使い方を拾うため)。
        params = f"{params} {param_text}".strip()
    command = f'"{target}"' if (" " in target and not target.startswith('"')) else target
    if params:
        command = f"{command} {params}"
    workdir = expand(entry.workdir, param_text) or None
    if workdir and not os.path.isdir(workdir):
        workdir = None
    return ("exec", (command, workdir))


def run_exec(command: str, workdir=None) -> bool:
    """file 種別のコマンドラインを起こす。成否を返す(例外は外へ出さない)。"""
    try:
        subprocess.Popen(command, cwd=workdir)
        return True
    except OSError as e:
        print(f"[tray-tools] コマンドを実行できません ({command}): {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------
# 実行履歴(新しいものが先頭の文字列リスト)
# ---------------------------------------------------------------

def history_path() -> Path:
    import filer_core
    return Path(filer_core.data_dir()) / "cmdlauncher_history.json"


def load_history(path=None) -> list:
    try:
        import json
        data = json.loads(Path(path or history_path()).read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x) for x in data if isinstance(x, str) and x]
    except (OSError, ValueError):
        pass
    return []


def save_history(history, path=None) -> None:
    import json
    path = Path(path or history_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(history), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def record_history(history, text: str, limit: int = 100) -> list:
    """実行した入力を先頭に足した履歴を返す(重複は大文字小文字を無視して除く)。"""
    text = (text or "").strip()
    if not text:
        return list(history or [])
    rest = [h for h in (history or []) if h.casefold() != text.casefold()]
    return ([text] + rest)[:limit]


def forget_history(history, text: str) -> list:
    """入力中の文字列と一致する履歴を消した列を返す(Ctrl+K 用)。"""
    key = (text or "").strip().casefold()
    return [h for h in (history or []) if h.casefold() != key]


# ---------------------------------------------------------------
# 入力窓
# ---------------------------------------------------------------
from PySide6.QtCore import QEvent, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QCursor, QGuiApplication  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QLineEdit, QListWidget, QVBoxLayout, QWidget,
)


class _LauncherInput(QLineEdit):
    """Enter / Esc を窓へ渡す入力欄。Space や Tab など「消費するか分からない」キーは
    key_hook(窓が差す関数)に先に聞き、True が返ればそちらで処理済みにする。"""

    accept = Signal()
    cancel = Signal()
    key_hook = None

    def keyPressEvent(self, event):
        if self.key_hook is not None and self.key_hook(event):
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept.emit()
            return
        if event.key() == Qt.Key_Escape:
            self.cancel.emit()
            return
        super().keyPressEvent(event)


class LauncherWindow(QWidget):
    """コマンドランチャの窓。候補の実行は持ち主(feature_screen)の役目。

    モードは3つ: name(登録名の補完) / path(フォルダ確定後の掘り下げ) /
    history(履歴から選ぶ)。path と history は一時的な寄り道で、Esc で name へ戻る。"""

    chosen = Signal(object, str, str)   # (Entry, 引数, 入力した文字列そのまま)
    dropped = Signal(list)              # ドロップされたパスや URL の文字列
    closed = Signal()
    on_forget = None                    # Ctrl+K 用: func(文字列) -> 新しい履歴 or None

    _MAX_ROWS = 8

    def __init__(self, entries, history=None):
        super().__init__()
        self.setWindowTitle("コマンドランチャ")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setFixedWidth(540)
        self.setAcceptDrops(True)
        self._entries = list(entries)
        self._history = list(history or [])
        self._mode = "name"
        self._path_base = None
        self._typed = ""
        self._cycle = -1
        self._hist_index = None
        self._shown = []
        self._deactivated = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self._path_label = QLabel()
        self._path_label.setVisible(False)
        layout.addWidget(self._path_label)
        self._input = _LauncherInput()
        self._input.setPlaceholderText("コマンド名を入力（; で引数）")
        self._input.key_hook = self._on_key
        self._input.textEdited.connect(self._on_text_edited)
        self._input.accept.connect(self._on_enter)
        self._input.cancel.connect(self._on_escape)
        layout.addWidget(self._input)
        self._list = QListWidget()
        self._list.setVisible(False)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list)

        self._deactivate_timer = QTimer(self)
        self._deactivate_timer.setSingleShot(True)
        self._deactivate_timer.setInterval(150)
        self._deactivate_timer.timeout.connect(self._on_deactivate_timeout)
        self._fit()

    # -- 持ち主からの操作 -------------------------------------------------
    def refresh(self, entries) -> None:
        """登録が変わったときに候補を作り直す(管理窓やドロップの後)。"""
        self._entries = list(entries)
        if self._mode == "name":
            self._update_popup()

    def set_history(self, history) -> None:
        self._history = list(history or [])
        if self._mode == "history":
            self._update_popup()

    def show_history(self) -> None:
        """History 内蔵コマンド用: 履歴を選ぶモードに入る。"""
        if not self._history:
            return
        self._mode = "history"
        self._typed = ""
        self._input.setText("")
        self._update_popup()

    # -- 候補 -------------------------------------------------------------
    def _current_shown(self) -> list:
        """いまのモードと入力に応じた候補を [(表示名, 中身), ...] で返す。"""
        if self._mode == "path":
            subs = list_subfolders(self._path_base or "", self._typed)
            return [(name, (name, path)) for name, path in subs]
        if self._mode == "history":
            keyword = self._typed.casefold()
            return [(h, h) for h in self._history
                    if h.casefold().startswith(keyword)]
        return [(e.name, e) for e in prefix_candidates(self._entries, self._typed)]

    def _update_popup(self) -> None:
        self._shown = self._current_shown()
        self._list.clear()
        for label, payload in self._shown:
            self._list.addItem(label)
            item = self._list.item(self._list.count() - 1)
            if isinstance(payload, Entry):
                item.setToolTip(payload.target)
            elif isinstance(payload, tuple):
                item.setToolTip(payload[1])
        visible = len(self._shown) >= 2 or (
            self._mode != "name" and len(self._shown) >= 1)
        self._list.setVisible(visible)
        if visible and self._list.count():
            self._list.setCurrentRow(0)
        self._fit()

    def _fit(self) -> None:
        height = self._input.sizeHint().height() + 16
        if self._path_label.isVisible():
            height += self._path_label.sizeHint().height() + 4
        if self._list.isVisible() and self._list.count():
            rows = min(self._list.count(), self._MAX_ROWS)
            row = self._list.sizeHintForRow(0) or 22
            height += row * rows + 6
        self.setFixedHeight(height)

    def _apply_completion(self) -> None:
        """先頭候補の名前を、打った部分の後ろを選択状態にして差し込む。"""
        if self._mode == "history" or not self._typed:
            return
        shown = self._current_shown()
        if not shown:
            return
        label = shown[0][0]
        if label != self._input.text():
            self._input.setText(label)
            self._input.setSelection(len(self._typed), len(label))
            # 補完が出ている状態が巡回の起点(次の Space で2件目へ進む)。
            self._cycle = 0

    # -- 入力イベント -------------------------------------------------------
    def _on_text_edited(self, text: str) -> None:
        self._cycle = -1
        self._hist_index = None
        if self._mode == "name" and ";" in text:
            # 引数を打っている間は補完しない(名前の部分だけ覚えておく)。
            self._typed = name_part(text)
            self._shown = []
            self._list.setVisible(False)
            self._fit()
            return
        self._typed = text
        self._apply_completion()
        self._update_popup()

    def _on_key(self, event) -> bool:
        key = event.key()
        mods = event.modifiers()
        if key == Qt.Key_Space:
            if not self._typed:
                return False
            return self._cycle_candidate(-1 if mods & Qt.ShiftModifier else 1)
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            return self._tab()
        if key in (Qt.Key_Down, Qt.Key_Up):
            return self._up_down(1 if key == Qt.Key_Down else -1)
        if key == Qt.Key_K and (mods & Qt.ControlModifier):
            raw = self._input.text()
            if raw and self.on_forget is not None:
                updated = self.on_forget(raw)
                if isinstance(updated, list):
                    self._history = updated
                    if self._mode == "history":
                        self._update_popup()
            return True
        return False

    def _cycle_candidate(self, step: int) -> bool:
        shown = self._current_shown()
        if not shown:
            return False
        if self._cycle < 0:
            self._cycle = 0 if step > 0 else len(shown) - 1
        else:
            self._cycle = (self._cycle + step) % len(shown)
        label = shown[self._cycle][0]
        self._input.setText(label)
        if self._mode != "history":
            self._input.setSelection(len(self._typed), len(label))
        if self._list.isVisible() and self._cycle < self._list.count():
            self._list.setCurrentRow(self._cycle)
        return True

    def _tab(self) -> bool:
        if self._mode == "history":
            return False
        shown = self._current_shown()
        if not shown:
            return False
        # 補完が出ているときは、入力欄に出ている名前そのものが「確定した名前」。
        exact = next((p for label, p in shown
                      if label.casefold() == self._input.text().casefold()), None)
        if exact is None:
            exact = next((p for label, p in shown
                          if label.casefold() == self._typed.casefold()), None)
        if self._mode == "name":
            if isinstance(exact, Entry) and exact.kind == KIND_FOLDER:
                self._mode = "path"
                self._path_base = exact.target
                self._typed = ""
                self._input.setText("")
                self._path_label.setText(exact.target)
                self._path_label.setVisible(True)
                self._update_popup()
                return True
            common = common_prefix([label for label, _ in shown])
            if len(common) > len(self._typed):
                self._typed = common
                self._apply_completion()
                self._update_popup()
                return True
            if isinstance(exact, Entry):
                self._input.setText(f"{exact.name};")
                self._input.setCursorPosition(len(exact.name) + 1)
                return True
            return False
        # path モード: 一致したフォルダがあれば、さらにその中へ降りる
        if isinstance(exact, tuple):
            self._path_base = exact[1]
            self._typed = ""
            self._input.setText("")
            self._path_label.setText(exact[1])
            self._update_popup()
            return True
        common = common_prefix([label for label, _ in shown])
        if len(common) > len(self._typed):
            self._typed = common
            self._apply_completion()
            self._update_popup()
            return True
        return False

    def _up_down(self, step: int) -> bool:
        if self._list.isVisible() and self._list.count():
            row = self._list.currentRow() + step
            row = max(0, min(row, self._list.count() - 1))
            self._list.setCurrentRow(row)
            return True
        if self._mode != "name" or not self._history:
            return False
        # 候補が出ていないときの ↑↓ は履歴の呼び出し。
        if step < 0:
            self._hist_index = (0 if self._hist_index is None
                                else min(self._hist_index + 1, len(self._history) - 1))
        else:
            if self._hist_index is None:
                return False
            self._hist_index -= 1
        text = "" if self._hist_index < 0 else self._history[self._hist_index]
        self._input.setText(text)
        self._typed = name_part(text)
        self._update_popup()
        return True

    # -- 決定・取消 ---------------------------------------------------------
    def _selected_payload(self):
        row = self._list.currentRow()
        if self._list.isVisible() and 0 <= row < len(self._shown):
            return self._shown[row][1]
        return None

    def _on_enter(self) -> None:
        if self._mode == "history":
            payload = self._selected_payload()
            if payload is None and self._shown:
                payload = self._shown[0][1]
            if payload is None:
                return
            self._mode = "name"
            self._input.setText(payload)
            self._typed = name_part(payload)
            self._apply_completion()
            self._update_popup()
            return
        if self._mode == "path":
            payload = self._selected_payload()
            if payload is None:
                exact = next((p for label, p in self._current_shown()
                              if label.casefold() == self._typed.casefold()), None)
                payload = exact
            if payload is None and len(self._shown) == 1:
                payload = self._shown[0][1]
            if payload is None:
                QApplication.beep()
                return
            name, path = payload
            self.chosen.emit(Entry(name, path, kind=KIND_FOLDER), "", path)
            self.close()
            return
        raw = self._input.text()
        name, param = parse_input(raw)
        if not name:
            return
        candidates = prefix_candidates(self._entries, name)
        entry = next((e for e in candidates
                      if e.name.casefold() == name.casefold()), None)
        if entry is None:
            selected = self._selected_payload()
            if isinstance(selected, Entry):
                entry = selected
        if entry is None and len(candidates) == 1:
            entry = candidates[0]
        if entry is None and ("://" in name or os.path.exists(name)):
            entry = Entry(name, name, kind=classify(name))
        if entry is None:
            QApplication.beep()
            return
        self.chosen.emit(entry, param, raw)
        # History だけは窓に残って履歴モードへ移るので、ここでは閉じない。
        if not (entry.kind == KIND_BUILTIN and entry.target == "history"):
            self.close()

    def _on_escape(self) -> None:
        if self._mode != "name":
            self._mode = "name"
            self._path_base = None
            self._path_label.setVisible(False)
            self._typed = ""
            self._input.setText("")
            self._update_popup()
            return
        if self._input.text():
            self._typed = ""
            self._input.setText("")
            self._update_popup()
            return
        self.close()

    def _on_item_clicked(self, item) -> None:
        row = self._list.row(item)
        if not (0 <= row < len(self._shown)):
            return
        label, payload = self._shown[row]
        if self._mode == "history":
            self._mode = "name"
            self._input.setText(payload)
            self._typed = name_part(payload)
        else:
            self._typed = label
            self._input.setText(label)
            self._input.setCursorPosition(len(label))
        self._apply_completion()
        self._update_popup()
        self._input.setFocus()

    # -- ドラッグ&ドロップ ----------------------------------------------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() or event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event):
        found = []
        mime = event.mimeData()
        for url in mime.urls():
            local = url.toLocalFile()
            found.append(local if local else url.toString())
        if not found and mime.hasText():
            text = mime.text().strip()
            if text:
                found.append(text)
        if found:
            self.dropped.emit(found)
        event.acceptProposedAction()

    # -- 表示・終了 ------------------------------------------------------------
    def show_at_cursor_screen(self) -> None:
        """カーソルのある画面の上寄り中央に出す(パレットと同じ場所感)。"""
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = avail.left() + (avail.width() - self.width()) // 2
            y = avail.top() + int(avail.height() * 0.18)
            self.move(max(x, avail.left()), max(y, avail.top()))
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus()

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self._deactivated = True
            self._deactivate_timer.start()
        elif e.type() == QEvent.WindowActivate:
            self._deactivated = False
        return super().event(e)

    def _on_deactivate_timeout(self) -> None:
        if not self._deactivated:
            return
        if QApplication.activePopupWidget() is not None:
            return
        self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


def create_window(entries, history=None) -> LauncherWindow:
    return LauncherWindow(entries, history)


# ---------------------------------------------------------------
# 管理窓(Command 内蔵コマンド)
# ---------------------------------------------------------------
from PySide6.QtWidgets import (  # noqa: E402
    QDialog, QFileDialog, QFormLayout, QHBoxLayout, QListWidgetItem, QPushButton,
)


class _EntryDialog(QDialog):
    """登録1件の編集。名前・対象は必須、パラメタと作業フォルダは任意。"""

    def __init__(self, parent=None, entry=None):
        super().__init__(parent)
        self.setWindowTitle("コマンドの登録")
        self.setMinimumWidth(460)
        entry = entry or {}
        form = QFormLayout(self)
        self._name = QLineEdit(entry.get("name") or "")
        form.addRow("名前", self._name)
        target_row = QHBoxLayout()
        self._target = QLineEdit(entry.get("path") or "")
        target_row.addWidget(self._target)
        file_button = QPushButton("ファイル…")
        file_button.clicked.connect(self._browse_file)
        target_row.addWidget(file_button)
        folder_button = QPushButton("フォルダ…")
        folder_button.clicked.connect(lambda: self._browse_folder(self._target))
        target_row.addWidget(folder_button)
        form.addRow("対象", target_row)
        self._params = QLineEdit(entry.get("params") or "")
        self._params.setPlaceholderText("%param% で引数を受けられます")
        form.addRow("パラメタ", self._params)
        dir_row = QHBoxLayout()
        self._dir = QLineEdit(entry.get("dir") or "")
        dir_row.addWidget(self._dir)
        dir_button = QPushButton("フォルダ…")
        dir_button.clicked.connect(lambda: self._browse_folder(self._dir))
        dir_row.addWidget(dir_button)
        form.addRow("作業フォルダ", dir_row)
        buttons = QHBoxLayout()
        ok_button = QPushButton("保存")
        ok_button.clicked.connect(self._on_ok)
        buttons.addWidget(ok_button)
        cancel_button = QPushButton("キャンセル")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        form.addRow(buttons)

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "対象のファイル")
        if path:
            self._target.setText(path.replace("/", "\\"))

    def _browse_folder(self, field) -> None:
        path = QFileDialog.getExistingDirectory(self, "フォルダを選ぶ")
        if path:
            field.setText(path.replace("/", "\\"))

    def _on_ok(self) -> None:
        if not self._name.text().strip() or not self._target.text().strip():
            QApplication.beep()
            return
        self.accept()

    def entry_dict(self) -> dict:
        entry = {"name": self._name.text().strip(),
                 "path": self._target.text().strip()}
        params = self._params.text().strip()
        if params:
            entry["params"] = params
        workdir = self._dir.text().strip()
        if workdir:
            entry["dir"] = workdir
        return entry


class ManagerDialog(QDialog):
    """登録の一覧窓。新規・編集・削除・並べ替えと、名前での絞り込み。"""

    def __init__(self, app_settings, settings_path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("登録コマンド一覧")
        self.setMinimumSize(520, 360)
        self._settings = app_settings
        self._settings_path = settings_path
        layout = QVBoxLayout(self)
        self._search = QLineEdit()
        self._search.setPlaceholderText("名前で絞り込み")
        self._search.textChanged.connect(lambda _t: self._rebuild())
        layout.addWidget(self._search)
        self._list = QListWidget()
        self._list.itemDoubleClicked.connect(lambda _i: self._edit())
        layout.addWidget(self._list)
        row = QHBoxLayout()
        for label, func in (("新規", self._new), ("編集", self._edit),
                            ("削除", self._delete), ("↑", lambda: self._move(-1)),
                            ("↓", lambda: self._move(1))):
            button = QPushButton(label)
            button.clicked.connect(func)
            row.addWidget(button)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        layout.addLayout(row)
        self._rebuild()

    def _rebuild(self, select=None) -> None:
        keyword = self._search.text().casefold()
        self._list.clear()
        for bookmark in launcher.load_bookmarks(self._settings):
            name = bookmark.get("name") or ""
            if keyword and keyword not in name.casefold():
                continue
            item = QListWidgetItem(f"{name}　—　{bookmark.get('path') or ''}")
            item.setData(Qt.UserRole, (name, bookmark.get("path") or ""))
            self._list.addItem(item)
            if select == (name, bookmark.get("path") or ""):
                self._list.setCurrentItem(item)

    def _selected(self):
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def _new(self) -> None:
        dialog = _EntryDialog(self)
        if dialog.exec():
            entry = dialog.entry_dict()
            launcher.upsert_bookmark(self._settings, self._settings_path, entry)
            self._rebuild(select=(entry["name"], entry["path"]))

    def _edit(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        name, path = selected
        current = next((b for b in launcher.load_bookmarks(self._settings)
                        if b.get("name") == name and b.get("path") == path), None)
        if current is None:
            return
        dialog = _EntryDialog(self, current)
        if dialog.exec():
            entry = dialog.entry_dict()
            launcher.update_bookmark(self._settings, self._settings_path,
                                     name, path, entry)
            self._rebuild(select=(entry["name"], entry["path"]))

    def _delete(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        launcher.delete_bookmark(self._settings, self._settings_path,
                                 selected[0], selected[1])
        self._rebuild()

    def _move(self, step: int) -> None:
        selected = self._selected()
        if selected is None:
            return
        if launcher.move_bookmark(self._settings, self._settings_path,
                                  selected[0], selected[1], step):
            self._rebuild(select=selected)

    def keyPressEvent(self, event):
        if (event.key() in (Qt.Key_Up, Qt.Key_Down)
                and event.modifiers() & Qt.ShiftModifier
                and self._list.hasFocus()):
            self._move(-1 if event.key() == Qt.Key_Up else 1)
            return
        super().keyPressEvent(event)


def open_manager(app_settings, settings_path, parent=None) -> None:
    """登録の管理窓を開く(閉じるまで戻らない)。"""
    dialog = ManagerDialog(app_settings, settings_path, parent)
    dialog.exec()
