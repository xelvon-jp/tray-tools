# command_palette.py
# 統合コマンドパレット。1つの入力窓から、機能・フォルダ・ファイル・アプリを
# 横断で探してその場で開く/実行する。入力が計算式なら、その場で答えも出す。
#
# 入口は縦ランチャの項目とホットキー(既定は未割り当て)で、開閉の管理は
# feature_screen.show_palette が持つ。候補の収集(何を対象にするか)と実行
# (選ばれたら何をするか)も feature_screen 側。ここは「探す・並べる・窓」の係。
#
# 検索は部分一致+あいまい(部分列)スコアに、frecency(使う頻度×近さ)の
# 押し上げを足したもの。履歴は %LOCALAPPDATA%\traytools\palette_history.json
# (PC ごとの状態なので settings.json には入れない。claude_progress の状態
# ファイルと同じ置き方)。
#
# ファイル候補は Everything(es.exe)任せで、呼び出しは feature_screen から
# 関数で渡される。ここは Qt のスレッドプールで受けて、古い結果は捨てる。
import ast
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QRunnable, QThreadPool, QTimer, Qt, Signal,
)
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

_KIND_LABELS = {
    "feature": "機能",
    "folder": "フォルダ",
    "file": "ファイル",
    "app": "アプリ",
    "calc": "計算",
}

# ファイル検索の入力待ち(ms)。打つたびに es.exe を叩かないための猶予。
_FILE_DEBOUNCE_MS = 300
# フォーカスを失ってから閉じるまでの猶予(ms)。カレンダーと同じ理由・同じ値。
_DEACTIVATE_CLOSE_MS = 150
_MAX_ROWS = 12


@dataclass
class Candidate:
    kind: str                 # feature / folder / file / app / calc
    title: str
    detail: str = ""          # パスや補足(2行目に出る)
    payload: dict = field(default_factory=dict)
    keywords: str = ""        # 検索の別名(項目キーや英字の別名など)


# ---------------------------------------------------------------
# あいまい検索
# ---------------------------------------------------------------
def score_match(query: str, text: str):
    """query が text にどれだけ合うか。合わなければ None、合えば大きいほど良い。

    前方一致 > 部分一致 > 部分列(あいまい)。部分列は連続と単語の頭に加点する。"""
    q = (query or "").strip().casefold()
    t = (text or "").casefold()
    if not q:
        return 0
    if t.startswith(q):
        return 1000 - len(t)
    idx = t.find(q)
    if idx >= 0:
        return 600 - idx * 2 - len(t) // 4
    score = 0
    pos = 0
    last = -2
    run = 0
    for ch in q:
        found = t.find(ch, pos)
        if found < 0:
            return None
        if found == last + 1:
            run += 1
            score += 5 + run
        else:
            run = 0
            score += 1
        if found == 0 or t[found - 1] in " _-/.・":
            score += 8
        last = found
        pos = found + 1
    return 100 + score - len(t) // 8


def filter_candidates(query: str, candidates, limit: int = _MAX_ROWS,
                      history=None, now=None):
    """候補を query で絞って並べる。

    history(候補キー → {"count","last"})を渡すと frecency の押し上げが効く:
    空の query では使う順(押し上げの大きい順)。query があるときは文字列の
    合い具合が主で、押し上げは近さをひっくり返す程度に抑えてある。"""
    q = (query or "").strip()
    if not q:
        if not history:
            return list(candidates)[:limit]
        now = time.time() if now is None else now
        ranked = sorted(
            candidates,
            key=lambda c: -frecency_boost(history.get(candidate_key(c) or ""), now))
        return ranked[:limit]
    scored = []
    for cand in candidates:
        best = score_match(q, cand.title)
        if cand.keywords:
            other = score_match(q, cand.keywords)
            if other is not None:
                best = other + 50 if best is None else max(best, other + 50)
        if best is not None:
            if history:
                now = time.time() if now is None else now
                best += frecency_boost(history.get(candidate_key(cand) or ""), now)
            scored.append((best, cand))
    scored.sort(key=lambda pair: -pair[0])
    return [cand for _score, cand in scored[:limit]]


# ---------------------------------------------------------------
# frecency(使う頻度×近さ)の履歴
# ---------------------------------------------------------------
HISTORY_PATH = (Path(os.environ.get("LOCALAPPDATA") or Path.home())
                / "traytools" / "palette_history.json")
_HISTORY_MAX_KEYS = 200
# 押し上げの上限。文字列スコア(前方一致1000・部分一致600・部分列100〜)の
# 格を崩さない範囲に抑える。
_BOOST_CAP = 160.0


def candidate_key(cand):
    """履歴のキー。計算は一過性なので記録しない(None)。"""
    if cand.kind == "feature":
        item_key = cand.payload.get("item_key")
        return f"feature:{item_key}" if item_key else None
    if cand.kind in ("folder", "file", "app"):
        path = cand.payload.get("path")
        return f"{cand.kind}:{os.path.normcase(path)}" if path else None
    return None


def frecency_boost(entry, now: float) -> float:
    """履歴1件の押し上げ量。回数×近さ(1時間以内×4 / 1日×2 / 1週間×1 / それ以前×0.5)。"""
    if not isinstance(entry, dict):
        return 0.0
    try:
        count = float(entry.get("count") or 0)
        last = float(entry.get("last") or 0)
    except (TypeError, ValueError):
        return 0.0
    if count <= 0 or last <= 0:
        return 0.0
    age = max(0.0, now - last)
    weight = 4.0 if age < 3600 else 2.0 if age < 86400 else 1.0 if age < 604800 else 0.5
    return min(_BOOST_CAP, min(count, 20.0) * weight * 4.0)


def record_use(history: dict, key: str, now: float) -> dict:
    """使った記録を1件足した履歴を返す(元の辞書は書き換えない)。"""
    updated = dict(history or {})
    entry = dict(updated.get(key) or {})
    try:
        count = int(entry.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    entry["count"] = count + 1
    entry["last"] = now
    updated[key] = entry
    if len(updated) > _HISTORY_MAX_KEYS:
        newest = sorted(updated.items(),
                        key=lambda kv: -(kv[1].get("last") or 0))[:_HISTORY_MAX_KEYS]
        updated = dict(newest)
    return updated


def load_history(path=None) -> dict:
    """履歴ファイルを読む。無い・壊れているなら空(学習が無いだけ)。"""
    try:
        data = json.loads(Path(path or HISTORY_PATH).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_history(history: dict, path=None) -> None:
    """履歴を書く(読み直しはしない丸ごとの状態ファイル。書けなくても諦める)。"""
    path = Path(path or HISTORY_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------
# その場計算(式と単位変換)
# ---------------------------------------------------------------
def _fmt(value: float) -> str:
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.10g}"


_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)


def _safe_eval(expr: str):
    """四則演算だけの式を ast で安全に評価する。できなければ None。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError
            return float(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, _BINOPS):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                if b == 0:
                    raise ZeroDivisionError
                return a / b
            if isinstance(node.op, ast.Mod):
                if b == 0:
                    raise ZeroDivisionError
                return a % b
            if abs(b) > 1000:
                raise ValueError
            return a ** b
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = ev(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        raise ValueError

    try:
        value = ev(tree)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return value if math.isfinite(value) else None


# 単位: 名前 -> (次元, 基準単位への換算係数)。長さの基準は m、重さは g、量は ml。
_UNITS = {
    "mm": ("長さ", 0.001), "cm": ("長さ", 0.01), "m": ("長さ", 1.0),
    "km": ("長さ", 1000.0), "inch": ("長さ", 0.0254), "ft": ("長さ", 0.3048),
    "g": ("重さ", 1.0), "kg": ("重さ", 1000.0), "t": ("重さ", 1_000_000.0),
    "ml": ("量", 1.0), "l": ("量", 1000.0),
}
_TEMP_UNITS = {"c", "f", "k"}

_CONV_ARROW_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z°]+)\s*(?:→|->|to)\s*([A-Za-z°]+)$", re.IGNORECASE)
_CONV_SPACE_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z°]+)\s+([A-Za-z°]+)$", re.IGNORECASE)


def _temp_key(unit: str) -> str:
    return unit.casefold().replace("°", "")


def _convert_temp(value: float, src: str, dst: str) -> float:
    celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[src]
    return {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[dst]


def _try_unit_conversion(text: str):
    m = _CONV_ARROW_RE.match(text) or _CONV_SPACE_RE.match(text)
    if not m:
        return None
    value = float(m.group(1))
    src_raw, dst_raw = m.group(2), m.group(3)
    src, dst = src_raw.casefold(), dst_raw.casefold()
    if _temp_key(src_raw) in _TEMP_UNITS or _temp_key(dst_raw) in _TEMP_UNITS:
        a, b = _temp_key(src_raw), _temp_key(dst_raw)
        if a not in _TEMP_UNITS or b not in _TEMP_UNITS or a == b:
            return None
        result = _convert_temp(value, a, b)
        return Candidate("calc", f"= {_fmt(result)}{dst_raw}",
                         f"単位変換 {src_raw} → {dst_raw}。Enter で結果をコピー",
                         {"value": _fmt(result)})
    if src in _UNITS and dst in _UNITS and _UNITS[src][0] == _UNITS[dst][0] and src != dst:
        result = value * _UNITS[src][1] / _UNITS[dst][1]
        return Candidate("calc", f"= {_fmt(result)}{dst_raw}",
                         f"単位変換 {src_raw} → {dst_raw}。Enter で結果をコピー",
                         {"value": _fmt(result)})
    return None


_EXPR_CHARS_RE = re.compile(r"^[0-9.+\-*/%()\s]+$")


def evaluate_query(text: str):
    """入力が計算なら calc 候補を1つ返す。そうでなければ None。

    対応: 四則演算(× ÷ − の記号と ^ も可)と、単位変換(100cm → m / 32f to c)。"""
    t = (text or "").strip()
    if not t:
        return None
    converted = _try_unit_conversion(t)
    if converted is not None:
        return converted
    if not re.search(r"\d", t):
        return None
    expr = (t.replace("×", "*").replace("÷", "/").replace("−", "-")
             .replace("^", "**").replace(",", ""))
    if not re.search(r"[+\-*/%()]", expr) or not _EXPR_CHARS_RE.match(expr):
        return None
    value = _safe_eval(expr)
    if value is None:
        return None
    formatted = _fmt(value)
    return Candidate("calc", f"= {formatted}", "Enter で結果をコピー",
                     {"value": formatted})


# ---------------------------------------------------------------
# アプリ候補(スタートメニューのショートカット)
# ---------------------------------------------------------------
def scan_app_candidates(roots=None):
    """スタートメニューの .lnk を集めて app 候補にする。同名は先勝ち。"""
    if roots is None:
        roots = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(os.path.join(
                appdata, "Microsoft", "Windows", "Start Menu", "Programs"))
        program_data = os.environ.get("ProgramData")
        if program_data:
            roots.append(os.path.join(
                program_data, "Microsoft", "Windows", "Start Menu", "Programs"))
    found = {}
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.lower().endswith(".lnk"):
                    continue
                title = name[:-4].strip()
                if title and title.casefold() not in found:
                    path = os.path.join(dirpath, name)
                    found[title.casefold()] = Candidate(
                        "app", title, path, {"path": path}, keywords=title)
    return sorted(found.values(), key=lambda c: c.title.casefold())


# ---------------------------------------------------------------
# 窓
# ---------------------------------------------------------------
class _FileSearchBridge(QObject):
    done = Signal(int, object)   # (世代, [Candidate])


class _FileSearchJob(QRunnable):
    def __init__(self, bridge, generation, func, query):
        super().__init__()
        self._bridge = bridge
        self._generation = generation
        self._func = func
        self._query = query

    def run(self):
        try:
            result = self._func(self._query) or []
        except Exception:  # noqa: BLE001  探せないことは候補が無いことと同じ
            result = []
        self._bridge.done.emit(self._generation, result)


class _PaletteInput(QLineEdit):
    move = Signal(int)
    accept = Signal()
    cancel = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Down:
            self.move.emit(1)
            return
        if event.key() == Qt.Key_Up:
            self.move.emit(-1)
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept.emit()
            return
        if event.key() == Qt.Key_Escape:
            self.cancel.emit()
            return
        super().keyPressEvent(event)


class PaletteWindow(QWidget):
    """パレットの窓。候補の収集と実行は持ち主(feature_screen)の役目。"""

    chosen = Signal(object)   # Candidate
    closed = Signal()

    def __init__(self, static_candidates, file_search=None, history=None):
        super().__init__()
        self.setWindowTitle("コマンドパレット")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setFixedWidth(580)
        self._static = list(static_candidates)
        self._file_search = file_search
        self._history = history or {}
        self._generation = 0
        self._file_results = []
        self._shown = []

        self._input = _PaletteInput(self)
        self._input.setPlaceholderText("機能・フォルダ・ファイル・アプリを検索（計算もできます）")
        self._input.textChanged.connect(self._on_text_changed)
        self._input.move.connect(self._move_selection)
        self._input.accept.connect(self._choose_current)
        self._input.cancel.connect(self.close)

        self._list = QListWidget(self)
        self._list.setFocusPolicy(Qt.NoFocus)
        self._list.itemClicked.connect(lambda _item: self._choose_current())
        self._list.itemDoubleClicked.connect(lambda _item: self._choose_current())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._input)
        layout.addWidget(self._list)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_FILE_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._start_file_search)

        self._bridge = _FileSearchBridge()
        self._bridge.done.connect(self._on_file_results)

        self._deactivate_timer = QTimer(self)
        self._deactivate_timer.setSingleShot(True)
        self._deactivate_timer.setInterval(_DEACTIVATE_CLOSE_MS)
        self._deactivate_timer.timeout.connect(self._on_deactivate_timeout)
        self._deactivated = False

        self._refresh("")

    # ---------------------------------------------------------------
    def _on_text_changed(self, text: str) -> None:
        self._generation += 1
        self._file_results = []
        self._refresh(text)
        if self._file_search is not None and len(text.strip()) >= 2:
            self._debounce.start()
        else:
            self._debounce.stop()

    def _refresh(self, text: str) -> None:
        rows = []
        calc = evaluate_query(text)
        if calc is not None:
            rows.append(calc)
        rows.extend(filter_candidates(text, self._static, _MAX_ROWS, self._history))
        seen_paths = {
            c.payload.get("path") for c in rows if c.payload.get("path")
        }
        # ファイル候補は Everything の順を土台に、よく開くものを前へ寄せる
        now = time.time()
        file_rows = sorted(
            self._file_results,
            key=lambda c: -frecency_boost(self._history.get(candidate_key(c) or ""), now))
        for cand in file_rows:
            if cand.payload.get("path") in seen_paths:
                continue
            rows.append(cand)
        self._shown = rows[:_MAX_ROWS + 1]
        self._list.clear()
        for cand in self._shown:
            label = _KIND_LABELS.get(cand.kind, cand.kind)
            text = cand.title if not cand.detail else f"{cand.title}\n{label} · {cand.detail}"
            self._list.addItem(QListWidgetItem(text))
        if self._shown:
            self._list.setCurrentRow(0)
        self._list.setVisible(bool(self._shown))
        self.adjustSize()

    def _start_file_search(self) -> None:
        if self._file_search is None:
            return
        query = self._input.text().strip()
        if len(query) < 2:
            return
        job = _FileSearchJob(self._bridge, self._generation, self._file_search, query)
        QThreadPool.globalInstance().start(job)

    def _on_file_results(self, generation: int, results) -> None:
        if generation != self._generation:
            return  # 打ち直した後の古い結果は捨てる
        self._file_results = list(results)
        self._refresh(self._input.text())

    def _move_selection(self, step: int) -> None:
        if not self._shown:
            return
        row = (self._list.currentRow() + step) % len(self._shown)
        self._list.setCurrentRow(row)

    def _choose_current(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._shown):
            cand = self._shown[row]
            self.close()
            self.chosen.emit(cand)

    # ---------------------------------------------------------------
    # 表示と閉じ方
    # ---------------------------------------------------------------
    def show_at_cursor_screen(self) -> None:
        """カーソルのある画面の上寄り中央に出す(Spotlight 風の場所感)。"""
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
        from PySide6.QtWidgets import QApplication
        if QApplication.activePopupWidget() is not None:
            return
        self.close()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


def create_window(static_candidates, file_search=None, history=None) -> PaletteWindow:
    return PaletteWindow(static_candidates, file_search, history)
