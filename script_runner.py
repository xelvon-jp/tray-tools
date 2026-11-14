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
#   - 警告(要確認)のものも F5・ボタンで実行できる。警告は理由を
#     下半分に出すだけで、実行は止めない(2026-11-14、持ち主の判断で
#     緩めた。人が間に入って見る前提)。チェック不能のものだけは
#     「中身を見てから実行」の2段にする。文法エラーは実行できない。
#   - 実行の直前に sha256 を取り直し、チェックしたときと違えば
#     実行せず、チェックからやり直す。
# PHAL の版。配布 (設置スクリプト) のたびに、配布番号に合わせて
# 更新する。タイトルバーに出して、開いている窓の版が分かるように
# する (古い窓が残っている事故の切り分け用)。
PHAL_VERSION = "1.45"

import argparse
import ctypes
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import script_runner_core as core
import script_runner_launch
import settings as settings_module

POLL_INTERVAL_MS = 1200

# チャットの見張り (DL 候補の検出) の間隔 (秒)。フォルダの見回り
# よりずっと間引く: 裏方の UIA 走査は軽くないのと、チャットの
# 変化は人の操作の速度でしか起きないため。
CHAT_POLL_INTERVAL_SEC = 10.0

# DL 候補の名前 (中身の冒頭) から、ファイル名らしき部分を取る
# ための目印。「既に一覧にあるファイルか」の判定に使う。
_DL_FILENAME_RE = re.compile(
    r"[\w.\-]+\.(?:phal|py|ps1)", re.IGNORECASE)

# 概要の先頭にあるコマンドIDの接頭辞 (「1-29: 」の形)。候補側の
# 概要 (カードのコメントそのまま) には残り、ファイル側の概要
# (core.extract_summary) では外されているので、照合の前に両方
# から外してそろえる。
_SUMMARY_ID_PREFIX_RE = re.compile(r"^\d{1,3}-\d{1,3}\s*[:：]\s*")


def _summary_matches(candidate_summary, item_summary) -> bool:
    """DL 候補の概要と、ファイルの概要 (先頭コメント) が同じ物か。

    空白を畳み、先頭のコマンドID接頭辞を外して比べる。候補側の
    概要はカード名から取る都合で60文字で切れていることがある
    (copilot_loop._download_summary)。切り捨てられたらしい長さ
    (60文字) のときは、残りが一致する前方一致でも同じ物とみなす
    (接頭辞を外す前の長さで判断する)。どちらかが空なら不一致
    (概要の無い物同士を「同じ」と言わない)。"""
    raw = " ".join((candidate_summary or "").split())
    cand = _SUMMARY_ID_PREFIX_RE.sub("", raw)
    item = _SUMMARY_ID_PREFIX_RE.sub(
        "", " ".join((item_summary or "").split()))
    if not cand or not item:
        return False
    if cand == item:
        return True
    return len(raw) >= 60 and item.startswith(cand)

# 一覧(表)の列。1スクリプト1行。時刻が一番左で、概要が幅を吸う。
COL_TIME = 0
COL_ID = 1
COL_NAME = 2
COL_HASH = 3
COL_SUMMARY = 4
COL_STATUS = 5
COL_RUN = 6

STATUS_COLORS = {
    core.STATUS_OK: "#1f7a45",
    core.STATUS_ERROR: "#b3342d",
    core.STATUS_WARN: "#8a5a00",
    core.STATUS_UNKNOWN: "#5b665f",
    "checking": "#5b665f",
}

# 中身が同じファイルが複数ある行の色(ファイル名とハッシュの列に
# 付ける)。ブラウザの再ダウンロードで付く「(2)」付きの写しなど、
# 消す候補だと一目で分かるようにする。
DUPLICATE_COLOR = "#c2512f"

# ファイル名の列の幅の上限(px)。列は中身に合わせて広がるが、
# ここで止める。余った幅は伸縮する概要の列が受け取る。長い名前は
# 省略表示になり、全文はセルのツールチップで見られる。
NAME_COLUMN_MAX_WIDTH = 260

NOTE_STYLES = {
    "": "",
    "ok": "color: #1f7a45;",
    "error": "color: #b3342d; background: #fbeceb;",
    "warn": "color: #8a5a00; background: #fbf2dd;",
}


# 選択中の行に重ねる薄い色。「選択したこと」はこの塗りだけで出す。
_SELECTION_TINT = QColor(0, 120, 215, 70)


# ---------------------------------------------------------------
# テーマ (1-44)。ダークモードは PHAL の窓の中の「ダーク」の
# 切替だけで切り替え、Windows の設定には連動しない。
# ライトの値は上の従来定数そのまま (ライトの見た目は変えない)。
# ダーク側の意味のある色は、暗い背景でも互いに見分けられる
# 明るさに寄せてある。chrome は窓の地の色 (背景・表・ボタン・
# 入力欄) で、ライトは OS 標準のまま触らないので None。
# dialog_error は「AI の窓」の状態ラベルのエラー色 (従来は
# script_runner_ai 側の #c00000。ライトでは そのまま渡す)。
# ---------------------------------------------------------------
STATUS_COLORS_DARK = {
    core.STATUS_OK: "#4cc38a",
    core.STATUS_ERROR: "#ff7b72",
    core.STATUS_WARN: "#e3b341",
    core.STATUS_UNKNOWN: "#9aa5a0",
    "checking": "#9aa5a0",
}

DUPLICATE_COLOR_DARK = "#f0854c"

NOTE_STYLES_DARK = {
    "": "",
    "ok": "color: #4cc38a;",
    "error": "color: #ff7b72; background: #4c2623;",
    "warn": "color: #e3b341; background: #4a3a17;",
}

THEMES = {
    "light": {
        "status": STATUS_COLORS,
        "duplicate": DUPLICATE_COLOR,
        "note_styles": NOTE_STYLES,
        "muted": "#5b665f",
        "link": "#1a5fb4",
        "dialog_error": "#c00000",
        "accent_style": "background: #1f6f5c; color: #ffffff;",
        "selection_tint": (0, 120, 215, 70),
        "chrome": None,
    },
    "dark": {
        "status": STATUS_COLORS_DARK,
        "duplicate": DUPLICATE_COLOR_DARK,
        "note_styles": NOTE_STYLES_DARK,
        "muted": "#9aa5a0",
        "link": "#58a6ff",
        "dialog_error": "#ff7b72",
        "accent_style": "background: #1f7a63; color: #ffffff;",
        "selection_tint": (88, 166, 255, 90),
        "chrome": {
            "window": "#2a2d33",
            "text": "#e6e8ea",
            "base": "#1f2226",
            "alternate_base": "#262a30",
            "button": "#343943",
            "border": "#4a5058",
            "header": "#31353d",
            "disabled_text": "#7d8590",
        },
    },
}


def _dark_palette(chrome: dict) -> QPalette:
    """ダークの地の色からパレットを作る。窓にだけ付ける。"""
    palette = QPalette()
    active = QPalette.ColorGroup.Active
    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(QPalette.ColorRole.Window, QColor(chrome["window"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(chrome["text"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(chrome["base"]))
    palette.setColor(
        QPalette.ColorRole.AlternateBase, QColor(chrome["alternate_base"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(chrome["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(chrome["button"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(chrome["text"]))
    palette.setColor(
        QPalette.ColorRole.ToolTipBase, QColor(chrome["button"]))
    palette.setColor(
        QPalette.ColorRole.ToolTipText, QColor(chrome["text"]))
    palette.setColor(
        QPalette.ColorRole.PlaceholderText,
        QColor(chrome["disabled_text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
    palette.setColor(
        QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText):
        palette.setColor(disabled, role, QColor(chrome["disabled_text"]))
    return palette


def _dark_stylesheet(chrome: dict) -> str:
    """ダークの地のスタイル。窓自身に付けるので、この窓と、
    この窓を親に開くダイアログだけに効く (アプリケーション
    全体には付けない)。

    QWidget の地の色を最初に置くのは、ダイアログへ効かせる
    ため。パレットは親の窓から子ダイアログへは受け継がれない
    (別窓としてアプリの標準で解釈される) が、スタイルシートは
    親子をたどって効く。個別の部品の規則は QWidget より優先
    される (派生側のセレクタが勝つ) ので、地の色だけが残る。"""
    return (
        "QWidget {"
        f" background-color: {chrome['window']};"
        f" color: {chrome['text']}; }}"
        "QHeaderView::section {"
        f" background-color: {chrome['header']};"
        f" color: {chrome['text']};"
        " border: none; padding: 2px 4px; }"
        "QTableWidget {"
        f" background-color: {chrome['base']};"
        f" alternate-background-color: {chrome['alternate_base']};"
        f" gridline-color: {chrome['border']}; }}"
        "QPlainTextEdit, QTextEdit {"
        f" background-color: {chrome['base']}; }}"
        "QPushButton {"
        f" background-color: {chrome['button']};"
        f" border: 1px solid {chrome['border']};"
        " border-radius: 3px; padding: 2px 8px; }"
        "QPushButton:disabled {"
        f" color: {chrome['disabled_text']}; }}"
        "QLineEdit, QComboBox {"
        f" background-color: {chrome['base']};"
        f" border: 1px solid {chrome['border']};"
        " padding: 1px 4px; }"
    )


class _NoFocusDelegate(QStyledItemDelegate):
    """表の「現在のセル」の枠と、選択行の枠を描かないための delegate。

    - State_HasFocus を外す: 現在のセルに付く、キャレットのような
      フォーカス枠が消える。
    - State_Selected も外す: 中身の描画(CE_ItemViewItem)でも、選択中
      のセルにテーマ背景を描かせない。行の背景そのものは、表が
      delegate の前に PE_PanelItemViewRow で描くので、そちらは
      _NoFocusRectStyle で止める(1.5 の HasFocus だけの対策では
      どちらの経路にも届かず、実機で縦棒が残った)。選択したことは、
      描いたあとに薄い色を重ねて分かるようにする。
    行の選択そのものとキー操作は今までどおり。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 選択行に重ねる色。窓がテーマに合わせて差し替える (1-44)。
        self.tint = _SELECTION_TINT

    def paint(self, painter, option, index):
        view_option = QStyleOptionViewItem(option)
        selected = bool(
            view_option.state & QStyle.StateFlag.State_Selected)
        view_option.state &= ~QStyle.StateFlag.State_HasFocus
        view_option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, view_option, index)
        if selected:
            painter.fillRect(view_option.rect, self.tint)


class _NoFocusRectStyle(QProxyStyle):
    """枠を描かないための proxy style(表にだけ付ける)。

    止めるのは2つ:
      - PE_FrameFocusRect: 現在セルのフォーカス枠(delegate 側でも
        落としているが、経路によらず止める)。
      - PE_PanelItemViewRow(選択中・フォーカス有りのときだけ):
        QTableView は delegate の前に、行の背景をセル単位でこの
        primitive で描く。Windows のスタイルはそこにテーマの枠つき
        背景を描くので、選択行の所々に縦棒が見えていた(実機報告
        2026-11-14)。delegate では止められない描画なので、ここで
        止める。選択したことは delegate の薄い塗りで出す。
    他の描画は元のスタイルのまま。"""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            return
        if element == QStyle.PrimitiveElement.PE_PanelItemViewRow:
            state = option.state
            if state & (QStyle.StateFlag.State_Selected
                        | QStyle.StateFlag.State_HasFocus):
                return
        super().drawPrimitive(element, option, painter, widget)



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
    """script_runner_launch から届く "show" と "restart" を受ける口。"""

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
            if "restart" in data:
                self.window.request_restart()
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
        # 並びは (優先度, 通番, key, path, stat_key)。優先度 0 が先。
        self._queue = queue.PriorityQueue()
        self._seq = 0
        # 予約済みの控え {(key, stat_key): 並んでいる側の集合 ("n"=通常
        # / "p"=優先)}。同じチェックを二重に並べないために request 側
        # (窓のスレッド) と run 側 (チェックのスレッド) の両方から
        # 触るので鍵をかける。
        self._pending = {}
        # 優先側が済んだのに通常側がまだ並んでいる (key, stat_key)。
        # 通常側が後から出てきたら素通りさせる (同じチェックが2回
        # 走らないように)。通常側を取り出した時点で消すので、後から
        # 来た新しい通常予約を巻き込むことはない。
        self._priority_done = set()
        self._lock = threading.Lock()
        self._stopped = False

    def request(self, key, path, stat_key):
        with self._lock:
            if (key, stat_key) in self._pending:
                return  # 同じチェックが既に並んでいる
            self._pending[(key, stat_key)] = {"n"}
            self._seq += 1
            self._queue.put((1, self._seq, key, path, stat_key))

    def request_priority(self, key, path, stat_key):
        """選ばれた行のチェックを先頭へ回す。

        まだ並んでいなければ優先で並べ、通常で並んでいれば優先の
        複製を並べて通常側を素通りさせる。"""
        ident = (key, stat_key)
        with self._lock:
            lanes = self._pending.get(ident)
            if lanes is not None and "p" in lanes:
                return  # 既に優先で並んでいる
            if lanes is None:
                self._pending[ident] = {"p"}
            else:
                lanes.add("p")
            self._seq += 1
            self._queue.put((0, self._seq, key, path, stat_key))

    def run(self):
        while not self._stopped:
            try:
                rank, _seq, key, path, stat_key = \
                    self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            ident = (key, stat_key)
            if rank == 1:
                with self._lock:
                    if ident in self._priority_done:
                        # 優先側で済ませた分。通常側は素通り。
                        self._priority_done.discard(ident)
                        self._pending.pop(ident, None)
                        continue
            started = time.monotonic()
            try:
                result = core.check_script(path, self._config)
            except Exception:  # noqa: BLE001  チェックの失敗は「不能」で出す
                result = core.CheckResult(
                    status=core.STATUS_UNKNOWN, kind="failed",
                    headline="チェックできませんでした",
                    sha256=core.file_sha256(path))
            elapsed_ms = int(
                (time.monotonic() - started) * 1000)
            with self._lock:
                lanes = self._pending.get(ident)
                if lanes is not None:
                    lanes.discard("p" if rank == 0 else "n")
                    if rank == 0 and "n" in lanes:
                        # 通常側が残っている。後で素通りさせる。
                        self._priority_done.add(ident)
                    if not lanes:
                        self._pending.pop(ident, None)
            self.done.emit((key, stat_key, result, elapsed_ms))

    def stop(self):
        self._stopped = True


class _RunSignals(QObject):
    done = Signal(object)  # RunResult


class _AiSignals(QObject):
    done = Signal(object)  # AI へ回答の結果 (SendResult と同じ顔)


class _DlSignals(QObject):
    checked = Signal(object)  # チャットの見張りの結果 (_DlCheckResult)
    pressed = Signal(object)  # DL 押下の結果 (SendResult と同じ顔)
    escaped = Signal(object)  # ESC 送信の結果 (SendResult と同じ顔)


class _DlCheckResult:
    """チャットの見張りの結果受け。copilot_io.DownloadResult と
    同じ顔 (ok / message / candidate / marker)。検出役が例外を
    投げたときの受け皿にも使う。marker は診断リクエストの目印
    {"id": 同定子} (1-43)。"""

    def __init__(self, ok, message, candidate=None, marker=None):
        self.ok = ok
        self.message = message
        self.candidate = candidate
        self.marker = marker


class _DlCandidate:
    """チャットで見つけたダウンロード候補 (表の特別行1行分)。

    ファイルの項目 (ScriptItem) とは別に持つ。行と項目の対応や
    実行の追跡に混ぜないため。key は候補の同一判定 (名前+クラス)
    で、押下済みの控えに使う。"""

    def __init__(self, candidate: dict):
        candidate = candidate if isinstance(candidate, dict) else {}
        self.name = candidate.get("name") or ""
        self.class_name = candidate.get("class_name") or ""
        self.label = candidate.get("label") or \
            " ".join(self.name.split())[:80]
        self.summary = candidate.get("summary") or ""
        self.detected_at = time.time()

    @property
    def key(self):
        return f"{self.name}\n{self.class_name}"


class _AiAnswerResult:
    """copilot_io.SendResult と同じ顔の、窓側の結果受け。
    送信役が例外を投げたときの受け皿に使う。"""

    def __init__(self, ok, message, needs_confirm=False):
        self.ok = ok
        self.message = message
        self.needs_confirm = needs_confirm


def _default_ai_sender(text, profile):
    """AI の入力欄へ入れるだけの送信役 (送信はしない)。

    copilot_io.fill_text を使う (EDDy のテスト入力と同じ経路)。
    profile は PHAL 自前の投げ先 (script_runner_ai.build_profile が
    作る辞書) をそのまま渡す。import は遅らせる: copilot_io が壊れても
    PHAL の窓は開くように (投げ先の解決も同じ理由で遅延 import)。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile)


def _default_ai_presser(profile):
    """AI の送信ボタンを押す役。本当に送信する。

    押す前に、入力欄を読み直す (送信の直前の確認):
      - 読めなければ、その段の理由を失敗として返す (押さない)
      - 空なら「入力欄が空です」で断る (入れたはずの回答が消えて
        いたら、空のまま送ってしまう事故になるため)
    読めて文があれば、プロファイルの目印の送信ボタンを Invoke で
    押す (copilot_io.press_send_button)。戻り値は copilot_io の
    SendResult と同じ顔 (ok / message)。"""
    import copilot_io

    read = copilot_io.read_input_text(profile=profile)
    if not read.ok:
        return read
    if not (read.text or "").strip():
        return _AiAnswerResult(False, "入力欄が空です")
    return copilot_io.press_send_button(profile=profile)


def _default_ai_downloader(profile):
    """チャットの最新発言から、ダウンロード候補を検出する役。

    copilot_io.latest_download を使う (裏方経由)。戻り値は
    DownloadResult と同じ顔 (ok / message / candidate)。"""
    import copilot_io

    return copilot_io.latest_download(profile=profile)


def _default_ai_dl_presser(profile):
    """チャットのダウンロード候補を押す役 (本当に落とす)。

    copilot_io.press_download_button を使う (裏方経由で、候補を
    探し直して Invoke で押す)。戻り値は SendResult と同じ顔。"""
    import copilot_io

    return copilot_io.press_download_button(profile=profile)


def _default_ai_esc_sender(profile):
    """DL の到着後に、AI の窓へ ESC を送る役 (1-67)。

    Edge のダウンロード・フライアウトはダウンロードのたびに
    開き、次の押下の邪魔になる。到着が確かめられた直後に閉じて
    おく。copilot_io.send_escape を使う (裏方経由で、対象の窓へ
    PostMessage)。戻り値は SendResult と同じ顔で、文に状態語
    (sent / no-hwnd / failed) が残る。"""
    import copilot_io

    return copilot_io.send_escape(profile=profile)


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
        self.summary = ""           # 先頭コメント(概要列)
        self.ran = False
        self.returned = False       # 「差し戻す」を済ませた印
        self.last_run = None        # この項目の直近の RunResult(差し戻しの証拠)
        self.confirm_armed = False  # 「中身を見てから実行」の2段目
        self.answered = False       # 結果を AI の入力欄へ入れ終わった印
        self.sent = False           # 送信まで済ませた印(サイクル完了)
        # 自動実行の対象か。パネル起動の時点で既にあったファイル
        # (とそのときの中身) は False のまま。起動後に新しく現れた
        # とき、または起動後に中身が変わったときだけ True になる
        # (付け替えは見回りの _poll_candidate が行う)。
        self.auto_eligible = False
        # 中身が実行済みだったため、自動実行を見送った印 (1-29)。
        # 立ったら、この中身の間は自動実行の入口・待ち列のどちら
        # でも二度と相手にしない (中身が変われば見回りが外す)。
        # 手動の実行はこの印に関係なく常にできる。
        self.auto_skip_executed = False

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
        # 前回、強制終了などで残った BOM 付け写しを掃除する(低2)。
        # 起動直後は実行中のものは無い。
        core.cleanup_bom_copies(self.watch_folder)

        self.setWindowTitle(
            f"擬似エージェントループ支援ツール PHAL Ver{PHAL_VERSION}")
        geometry = self.config.get("window_geometry") or [None, None, 340, 560]
        self.resize(geometry[2] or 340, geometry[3] or 560)
        if geometry[0] is not None and geometry[1] is not None:
            self.move(geometry[0], geometry[1])

        self._items = []            # [ScriptItem](表示順 = 新しい順)
        self._items_by_key = {}
        self._poll_problem_seen = set()  # 見回りの問題の署名(通知は1度だけ)
        self._stable = {}           # key -> (size, 連続で同じだった回数)
        self._selected_key = None
        self._running_item = None
        self._run_handle = None
        self._run_thread = None
        self._run_signals = _RunSignals()
        self._run_signals.done.connect(self._on_run_done)
        self._ai_signals = _AiSignals()
        self._ai_signals.done.connect(self._on_ai_answer_done)
        self._ai_filling = False
        self._ai_answer_item = None
        self._send_signals = _AiSignals()
        self._send_signals.done.connect(self._on_ai_send_done)
        self._ai_sending = False
        self._ai_send_item = None
        # チャットの見張り (DL 候補の検出と特別行)。
        self._dl_signals = _DlSignals()
        self._dl_signals.checked.connect(self._on_dl_checked)
        self._dl_signals.pressed.connect(self._on_dl_pressed)
        self._dl_signals.escaped.connect(self._on_escape_sent)
        self._dl_holder = None        # _DlCandidate | None (特別行)
        self._dl_consumed = set()     # 押下済みの候補の key の控え
        self._dl_auto_tried = set()   # 自動で押してみた候補の key
        # 押下済み候補の控え (開き直しをまたいで残る)。会話内の
        # 控えは開き直しで消えるので、core の控えファイルから
        # 読み込んでおく (1-29)。
        self._dl_consumed_persisted = core.load_dl_consumed()
        self._dl_checking = False
        self._dl_last_check = 0.0
        self._dl_pressing = False
        self._dl_press_auto = False
        self._dl_press_key = None
        # 押下に成功した DL の到着待ち (候補の表示名, 押した時刻)。
        # 落としたファイルが一覧に現れたら到着の確認として診断へ
        # 残し、しばらく現れなければ「確認できなかった」と残す。
        self._dl_awaiting = None
        # 診断の控え (直近の動きの短い記録)。開き直しをまたいで
        # 残るよう core の控えファイルから読み込んでおく (1-41)。
        self._diag_events = core.load_diag_events()
        # 診断の自動応答 (1-43)。消費済みの目印と直近に送った
        # 時刻の控え (開き直しをまたぐ)、見送り中の目印、送信の
        # 最中の印、結果を受けるシグナル。
        self._diag_auto = core.load_diag_auto()
        self._diag_marker_pending = None
        self._diag_sending = False
        self._diag_auto_mid = None
        self._diag_noted = set()
        self._diag_signals = _AiSignals()
        self._diag_signals.done.connect(self._on_diag_auto_done)
        # チャットの検出役・DL 押下役 (テストで差し替える。None なら
        # 既定の _default_ai_downloader / _default_ai_dl_presser)。
        self.ai_downloader = None
        self.ai_dl_presser = None
        # DL 到着後に AI の窓へ ESC を送る役 (テストで差し替える。
        # None なら既定の _default_ai_esc_sender、1-67)。
        self.ai_esc_sender = None
        # 送信ボタンの目印が設定済みか (ボタンの「送信」役割の出し分け)。
        # 設定の解決は _refresh_send_ready が行い、ここは控え。
        self._ai_send_ready = False
        # AI へ回答の送信役 (テストで差し替える。None なら既定の
        # _default_ai_sender)。
        self.ai_sender = None
        # AI の送信ボタンを押す役 (テストで差し替える。None なら
        # 既定の _default_ai_presser)。
        self.ai_presser = None
        self._last_clipboard_text = ""
        self._last_run = None       # (key, RunResult)
        self._editor_windows = []
        # 自動実行まわり。起動スナップショットは、最初の見回りで
        # 一覧にあったファイルの {key: (mtime, size)}。これと一致
        # する項目は「起動前からあった分」で、自動では走らせない。
        # _auto_run_queue は実行待ちの key の列 (先入れ先出し)。
        # _auto_run_tried は自動で走らせ始めた (key, stat_key) の
        # 控えで、同じ中身を2度は自動で走らせないために使う。
        self._launch_stats = None
        self._auto_run_queue = []
        self._auto_run_tried = set()
        # 自動実行の対象の控え (開き直しをまたいで残る)。開く前に
        # 対象だったファイルは、開き直した後も対象のままにする
        # ために、core の控えファイルから読み込んでおく。
        self._pending_runs = core.load_pending_runs()
        # 実行済みの中身 (sha256) の控え。自動実行は、ここにある
        # 中身を二度は走らせない (1-29)。手動の実行は関係しない。
        # _executed_skip_noted は、見送りの知らせを出した項目の
        # key (この会話で1項目1度だけ出すため)。
        self._executed_hashes = core.load_executed_hashes()
        self._executed_skip_noted = set()
        # 外から届いた開き直しの頼みを、自動化の連鎖が済むまで
        # 保留している印。設置スクリプトの開き直しは実行の終了と
        # ほぼ同時に届くので、そのまま閉じると回答・送信が飛ぶ。
        self._restart_deferred = False
        # テーマ (1-44)。_build_ui が色を引くので、先に決めておく。
        # _default_palette はライトへ戻すときの受け皿 (付ける前の
        # パレットの写し)。_note_kind はメモ欄の今の色付けの控えで、
        # テーマを切り替えたときに付け直すために持つ。
        self._dark_mode = bool(self.config.get("dark_mode"))
        self._note_kind = ""
        self._default_palette = QPalette(self.palette())

        self._build_ui()
        self._apply_on_top(bool(self.config.get("always_on_top")),
                           save=False)
        # 起動時は、窓が見える前にテーマを当てておく。
        self._apply_theme()

        # チェックは裏のスレッドで1件ずつ(サブプロセス待ちで窓を止めない)。
        # 前回までのチェック結果の控えを読み込んでおく (変わっていない
        # ファイルは開いた直後からチェック済みの表示になる)。
        self._check_cache = core.load_check_cache()

        self._check_worker = _CheckWorker(self.config)
        self._check_worker.done.connect(self._on_check_done)
        self._check_thread = threading.Thread(
            target=self._check_worker.run, daemon=True,
            name="script-runner-check")
        self._check_thread.start()

        self._refresh_send_ready()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(POLL_INTERVAL_MS)
        self._poll()

    # ---------------- 画面 ----------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 見出しは独立の行に置く。見出し・版・前面固定を1行に並べると
        # 見出しの文言の幅で窓の最小幅が設計(340)を超えてしまうため。
        # 幅はレイアウトに任せ、最小幅には数えない(低3と同じ作法)。
        title = QLabel("PHAL：PHAL Helps Agent Loop")
        title.setFont(QFont(title.font().family(), 11, QFont.Weight.Bold))
        title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(title)

        header = QHBoxLayout()
        header.addStretch(1)
        self.ai_window_button = QPushButton("AIの窓…")
        self.ai_window_button.clicked.connect(self._open_ai_window)
        header.addWidget(self.ai_window_button)
        self.restart_button = QPushButton("開き直す")
        self.restart_button.setToolTip("PHAL を新しいコードで開き直します")
        self.restart_button.clicked.connect(self.restart)
        header.addWidget(self.restart_button)
        version = QLabel(self._version_label())
        version.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
        header.addWidget(version)
        self.version_label = version
        self.on_top_box = QCheckBox("前面に固定")
        self.on_top_box.setChecked(bool(self.config.get("always_on_top")))
        self.on_top_box.toggled.connect(
            lambda on: self._apply_on_top(on, save=True))
        header.addWidget(self.on_top_box)
        layout.addLayout(header)

        # 対象フォルダのパスはクリックできて、あっふ のアクティブな
        # 窓がそのフォルダを開く(届いた物をそのままファイラで見たい
        # とき用)。パス部分だけリンクにした1つのラベルで、文の
        # 流れが途切れないようにする(2ラベルに分けると、後ろの文が
        # 窓の真ん中に浮いて見えた。2026-11-14 実機報告)。
        caption = QLabel(self._caption_html())
        caption.setStyleSheet("font-size: 11px;")
        caption.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse)
        caption.linkActivated.connect(
            lambda _link: self._open_watch_in_filer())
        # フォルダのパスが長いと、折り返せない部分の幅のぶん窓の
        # 最小幅が押し広がり、設計幅(340)に縮められなくなる(低3)。
        # 横幅はレイアウトに任せ、ラベル自身の希望幅で窓幅を決め
        # ない。全文はツールチップでも見られる。
        caption.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        caption.setToolTip(f"{self.watch_folder} に届いたもの(新しい順)")
        layout.addWidget(caption)
        self.folder_caption = caption

        # 絞り込み。隠すのは見た目だけで、裏のチェックや実行の
        # 追跡は全ファイルのまま(_visible_items が表の行との対応)。
        filter_row = QHBoxLayout()
        self.hide_no_id_box = QCheckBox("ID が無いファイルを表示しない")
        self.hide_no_id_box.setChecked(bool(self.config.get("hide_no_id")))
        self.hide_no_id_box.toggled.connect(self._apply_hide_no_id)
        filter_row.addWidget(self.hide_no_id_box)
        filter_row.addStretch(1)
        # ダークの切替は見出し行ではなくここに置く。見出し行は
        # 中身の幅が窓の最小幅を決めてしまう (設計幅 340 を超える
        # と窓が縮まなくなる) ため、設定の並びであるこの行の
        # 右端に置く (1-44)。
        self.dark_box = QCheckBox("ダーク")
        self.dark_box.setToolTip("PHAL の窓を暗い配色にします")
        self.dark_box.setChecked(self._dark_mode)
        self.dark_box.toggled.connect(self._apply_dark_mode)
        filter_row.addWidget(self.dark_box)
        layout.addLayout(filter_row)

        # 一覧は1スクリプト1行の表(時刻/ID/ファイル名/ハッシュ/
        # 概要/状態/実行)。
        # 詳しい状態や操作の残り(要確認の2段・エラーコピー・EDDy で
        # 開く)は、選択中の行に対する下半分と右クリックに置く。
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["時刻", "ID", "ファイル名", "ハッシュ", "概要", "状態", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._delegate = _NoFocusDelegate(self.table)
        self.table.setItemDelegate(self._delegate)
        # 行背景の枠(PE_PanelItemViewRow)とフォーカス枠を、表の
        # スタイル側でも止める(delegate と二重の対策)。
        self._table_style = _NoFocusRectStyle(self.table.style())
        self.table.setStyle(self._table_style)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_ID, QHeaderView.ResizeMode.ResizeToContents)
        # ファイル名だけは Interactive: 幅は _fit_name_column が
        # 中身に合わせて上限つきで決める (ResizeToContents だと
        # 長い名前が際限なく列を押し広げるため)。
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(COL_HASH, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_SUMMARY, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_RUN, QHeaderView.ResizeMode.ResizeToContents)
        self.table.currentCellChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            self._on_context_menu)
        layout.addWidget(self.table, stretch=3)

        self.result_caption = QLabel("直前の結果")
        self.result_caption.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
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

    # ---------------- テーマ (1-44) ----------------
    def _theme(self) -> dict:
        return THEMES["dark" if self._dark_mode else "light"]

    def _caption_html(self) -> str:
        theme = self._theme()
        return (
            f'<a href="open-in-filer" style="color: {theme["link"]};">'
            f"{self.watch_folder}</a>"
            f'<span style="color: {theme["muted"]};">'
            " に届いたもの(新しい順)</span>")

    def _apply_theme(self):
        """今のテーマを窓全体へ当てる。

        地の色はパレット+窓自身のスタイルシートで当てるので、
        効くのはこの窓と、この窓を親に開くダイアログ (「AIの窓」
        など) だけ。アプリケーション全体には付けない (同じ
        プロセスに居る他の窓を巻き込まないため。EDDy の窓は
        PHAL の親子に入らないので、こちらも付かない)。
        意味のある色はテーマ表から取り直し、一覧も作り直す。
        ライトでは、付ける前のパレットに戻し、スタイルシートを
        空にして、今までと見た目を変えない。"""
        theme = self._theme()
        chrome = theme["chrome"]
        if chrome is None:
            self.setPalette(QPalette(self._default_palette))
            self.setStyleSheet("")
            self.table.setAlternatingRowColors(False)
        else:
            self.setPalette(_dark_palette(chrome))
            self.setStyleSheet(_dark_stylesheet(chrome))
            self.table.setAlternatingRowColors(True)
        self._delegate.tint = QColor(*theme["selection_tint"])
        muted_style = f"color: {theme['muted']}; font-size: 11px;"
        self.version_label.setStyleSheet(muted_style)
        self.result_caption.setStyleSheet(muted_style)
        self.folder_caption.setText(self._caption_html())
        self.note.setStyleSheet(
            theme["note_styles"].get(self._note_kind, ""))
        self._refresh_list()

    def _apply_dark_mode(self, on: bool):
        """「ダーク」の切替。再起動なしでその場で切り替わり、
        設定にも残す (前面に固定・絞り込みと同じ作法)。"""
        self._dark_mode = bool(on)
        self.config["dark_mode"] = bool(on)
        try:
            settings_module.update_settings_section(
                "script_runner", {"dark_mode": bool(on)},
                self.settings_path)
        except Exception:  # noqa: BLE001  覚えられないだけ
            pass
        self._apply_theme()

    # ---------------- 診断 ----------------
    def _diag(self, text: str):
        """直近の動きを診断の控えへ1件残す (1-41)。

        ループが止まったとき、画面の外からは理由が見えない。控えは
        「AIの窓」の「診断をAIへ貼る」でまとめて AI へ渡せる。
        診断が理由で本体の連鎖が止まることがあってはならないので、
        ここでの失敗はすべて黙ってやり過ごす。"""
        try:
            core.diag_append(self._diag_events, text)
            core.save_diag_events(self._diag_events)
        except Exception:  # noqa: BLE001  診断は本体を止めない
            pass

    def _diag_status_label(self, status) -> str:
        """チェックの判定を、状態の列と同じ短い語にする。"""
        return {core.STATUS_OK: "OK", core.STATUS_WARN: "警告",
                core.STATUS_ERROR: "エラー"}.get(status, "不明")

    def _diagnostics_text(self) -> str:
        """今の状態と直近の記録から、AI へ貼る診断の束を作る。

        「AIの窓」の「診断をAIへ貼る」から呼ばれる (その窓の表示中
        プロファイルの入力欄へ入れるのは向こうの仕事)。"""
        profile_name = "(未設定)"
        try:
            import script_runner_ai as sr_ai

            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            profiles, active = sr_ai.sanitize_profiles(section)
            profile_name = profiles[active]["name"]
            if not sr_ai.is_configured(profiles[active]["target"]):
                profile_name += "（未設定）"
        except Exception:  # noqa: BLE001  名前が取れなくても束は作る
            pass
        if self._dl_holder is None:
            dl_line = "DL行: なし"
        else:
            dl_line = f"DL行: 表示中（{self._dl_holder.label}）"
            if self._dl_pressing:
                dl_line += "・押下中"
        chain = [
            dl_line,
            "実行中: " + (
                self._running_item.name
                if self._running_item is not None else "なし"),
            "回答・送信: " + (
                "回答を入れている最中" if self._ai_filling else
                "送信を押している最中" if self._ai_sending else "なし"),
            f"自動実行の待ち: {len(self._auto_run_queue)}件",
            "開き直しの保留: " + (
                "あり" if self._restart_deferred else "なし"),
        ]
        return core.build_diag_bundle(
            PHAL_VERSION, self._automation_flags(), profile_name,
            chain, self._diag_events)

    def _set_note(self, text: str, kind: str = ""):
        self._note_kind = kind
        self.note.setText(text)
        self.note.setStyleSheet(
            self._theme()["note_styles"].get(kind, ""))

    def _set_clipboard(self, text: str):
        QGuiApplication.clipboard().setText(text)
        self._last_clipboard_text = text

    # ---------------- 一覧の見回り ----------------
    def _poll(self):
        try:
            self._poll_inner()
        except Exception as exc:  # noqa: BLE001  見回りで窓を落とさない
            # 飲み込むだけだと、一覧が止まっている理由が画面から
            # 分からない。問題は note に1度だけ出す。
            self._report_poll_problem("見回り", "", exc)

    def _report_poll_problem(self, where: str, name: str, exc: Exception):
        """見回りの問題を note に出す。同じ問題は1度だけ。

        以前は例外を黙って飲んでいたため、1件のファイルで例外が出ると
        一覧への追加がその回ごと止まり、理由も残らなかった。"""
        signature = f"{where}:{type(exc).__name__}:{exc}"
        if signature in self._poll_problem_seen:
            return
        self._poll_problem_seen.add(signature)
        target = f"{name} の処理" if name else where
        self._set_note(
            f"{target}で問題がありました({type(exc).__name__}: {exc})。"
            "他のファイルの見回りは続けています", "error")

    def _poll_candidate(self, path, mtime, size) -> bool:
        """候補1件分の取り込み。状態が変わったら True。

        例外は受ける側(_poll_inner)に任せる。1件の問題で見回り全体を
        止めないため、ここでは受けない。"""
        key = str(path)
        prev = self._stable.get(key)
        count = prev[1] + 1 if prev and prev[0] == size else 1
        self._stable[key] = (size, count)
        if count < 2:
            return False  # 大きさが落ち着くまでチェックしない
        item = self._items_by_key.get(key)
        if item is None:
            item = ScriptItem(path, mtime, size)
            # 起動スナップショットに無い (または中身が違う) なら、
            # 起動後に現れた分として自動実行の対象にできる。開き
            # 直す前に対象だった分は、控えが一致すれば引き続き対象。
            item.auto_eligible = self._decide_auto_eligible(
                key, mtime, size)
            item.summary = core.extract_summary(path)
            self._items.append(item)
            self._items_by_key[key] = item
            if self._dl_awaiting is not None:
                # 押下に成功した DL のファイルが届いた。
                self._dl_awaiting = None
                self._diag(f"DLの到着を確認: {item.name}")
                # このダウンロードで開いたフライアウトを閉じる
                # (次の押下の邪魔になるため、1-67)。
                self._send_escape_to_ai()
            self._queue_check(item)
            return True
        if (mtime, size) != item.stat_key():
            # 同じ名前で上書きされた: チェックをやり直し、実行済みと
            # 差し戻し済みの印を外す。概要も新しい中身から取り直す。
            # 中身の変更が起動後に起きたなら、新しい中身は自動実行の
            # 対象にできる (スナップショットの中身そのままなら不可)。
            # 古い中身の控えは捨てて、新しい中身で改めて決める。
            self._forget_pending_run(key)
            item.auto_eligible = self._decide_auto_eligible(
                key, mtime, size)
            item.auto_skip_executed = False  # 中身が変われば判定し直す
            item.mtime, item.size = mtime, size
            item.ran = False
            item.returned = False
            item.last_run = None
            item.answered = False
            item.sent = False
            item.confirm_armed = False
            item.result = None
            item.summary = core.extract_summary(path)
            self._queue_check(item)
            return True
        return False

    def _is_launch_stat(self, key, mtime, size) -> bool:
        """その (更新時刻, 大きさ) が起動スナップショットのものか。

        スナップショットがまだ無い間は False (起動前からあったと
        は言えない)。スナップショットは最初の _poll_inner で取る
        ので、項目が生まれる時点では必ず取れている。"""
        stats = self._launch_stats
        return stats is not None and stats.get(key) == (mtime, size)

    def _decide_auto_eligible(self, key, mtime, size) -> bool:
        """自動実行の対象かどうかを決める。対象なら控えにも残す。

        原則は起動スナップショットとの照合 (一致 = 起動前から
        あった分で、対象にしない)。ただし開き直す前に「対象」と
        控えられていた分は、控えの素性 (更新時刻・大きさ) が今の
        中身と一致する間は、起動直後の姿でも対象のままにする
        (ダウンロードの直後に PHAL が開き直しても、自動実行が
        続くように)。控えの無い滞留分は決して対象にしない。"""
        eligible = not self._is_launch_stat(key, mtime, size)
        if not eligible:
            eligible = core.pending_run_get(
                self._pending_runs, key, mtime, size) is not None
        if eligible:
            self._remember_pending_run(key, mtime, size)
        return eligible

    def _remember_pending_run(self, key, mtime, size, sha256=""):
        """対象になったファイルの素性を、開き直しをまたぐ控えへ。

        sha256 はチェックが済んでから分かるので、済んだ時点で
        改めて呼んで埋める (空のまま呼ぶと、既に分かっている
        sha256 を消してしまうので、その場合は今の値を保つ)。"""
        if not sha256:
            prev = self._pending_runs.get(key)
            if isinstance(prev, dict):
                sha256 = prev.get("sha256") or ""
        core.pending_run_put(self._pending_runs, key, mtime, size,
                             sha256)
        core.save_pending_runs(self._pending_runs)

    def _forget_pending_run(self, key):
        """対象の控えを捨てる (走らせた・消えた・中身が変わった)。"""
        if core.pending_run_discard(self._pending_runs, key):
            core.save_pending_runs(self._pending_runs)

    def _poll_inner(self):
        self.restart_button.setEnabled(self._running_item is None)
        cutoff = time.time() - self.config["recent_hours"] * 3600
        candidates = core.list_candidates(
            self.watch_folder, cutoff, self.config["max_items"] * 3)
        if self._launch_stats is None:
            # 最初の見回りで居たファイルが「起動前からあった分」。
            # これらは自動では決して走らせない (古い設置ファイルの
            # 山を再実行してしまう事故を防ぐ)。
            self._launch_stats = {
                str(path): (mtime, size)
                for path, mtime, size in candidates}
        present = {}
        changed = False
        for path, mtime, size in candidates:
            key = str(path)
            present[key] = (path, mtime, size)
            try:
                if self._poll_candidate(path, mtime, size):
                    changed = True
            except Exception as exc:  # noqa: BLE001  1件で全体を止めない
                self._report_poll_problem("一覧の見回り", path.name, exc)
        for key in list(self._items_by_key):
            if key not in present:
                item = self._items_by_key.pop(key)
                # 一覧から消えたファイルの対象の控えも捨てる。
                self._forget_pending_run(key)
                if self._running_item is not item:
                    self._items.remove(item)
                    changed = True
        self._stable = {k: v for k, v in self._stable.items() if k in present}
        # 消えたファイルの自動実行まわりの控えも捨てる。
        self._auto_run_queue = [
            k for k in self._auto_run_queue if k in present]
        self._auto_run_tried = {
            ident for ident in self._auto_run_tried
            if ident[0] in present}
        # 新しい順に並べ、上限を超えたら古い方から落とす。
        self._items.sort(key=lambda item: item.mtime, reverse=True)
        while len(self._items) > self.config["max_items"]:
            tail = self._items[-1]
            if tail is self._running_item:
                break
            self._items.pop()
            self._items_by_key.pop(tail.key, None)
            self._forget_pending_run(tail.key)
            changed = True
        if changed:
            self._refresh_list()
        # DL の到着待ちが長引いていたら、「確認できなかった」
        # として診断に残す (押せたのにファイルが来ない、が
        # 画面の外からも分かるように)。
        if self._dl_awaiting is not None \
                and time.monotonic() - self._dl_awaiting[1] > 180:
            label = self._dl_awaiting[0]
            self._dl_awaiting = None
            self._diag(
                f"DLの到着を確認できなかった: {label}")
        # チャットの見張りは間引いて回す (中では間隔を見ている)。
        self._maybe_check_chat()
        # 保留中の開き直しの保険: 連鎖の節目で拾い損ねても、見回り
        # ごとに「もう済んだか」を見直す。
        self._fire_deferred_restart()

    def _restore_ran(self, item: ScriptItem):
        """チェック結果が付いた時点で、実行済みの印を復元する。

        この中身の sha256 が実行済みの控え (開き直しをまたいで
        残る) にあれば、実行済みとして扱う (1-33)。状態列の
        「・済」の表示と、自動実行の歯止め (_admit_auto_run が
        ran を見て止まる) のため。ただしこの会話で実行した
        結果を持っているわけではないので、last_run は None の
        ままにする: 回答・送信の段は last_run を見て止まり、
        行のボタンは「実行」の姿に戻る (姿の判定は
        _run_button_spec が last_run ありきで見ている)。"""
        if item.ran or item.result is None:
            return
        sha = item.result.sha256
        if sha and core.executed_hash_contains(
                self._executed_hashes, sha):
            item.ran = True

    def _queue_check(self, item: ScriptItem):
        cached = core.check_cache_get(
            self._check_cache, item.path, item.mtime, item.size)
        if cached is not None:
            # パス・更新時刻・大きさが前回のチェック時と一致する。
            # 中身は同じはずなので、控えの結果をそのまま使い、
            # チェッカーのサブプロセスは起こさない。
            item.result = cached
            item.status = cached.status
            self._diag(
                f"チェック（控えから）: {item.name} → "
                f"{self._diag_status_label(cached.status)}")
            self._restore_ran(item)
            if item.auto_eligible:
                # 対象の控えに、チェックで分かった sha256 も埋める。
                self._remember_pending_run(
                    item.key, item.mtime, item.size, cached.sha256)
            # チェック完了と同じ扱いで、自動実行の入口も通す
            # (起動後に落とした同じファイルの再来など)。
            self._admit_auto_run(item)
            return
        item.status = "checking"
        self._check_worker.request(item.key, item.path, item.stat_key())

    def _on_check_done(self, payload):
        key, stat_key, result = payload[:3]
        elapsed_ms = payload[3] if len(payload) > 3 else None
        item = self._items_by_key.get(key)
        if item is None or item.stat_key() != stat_key:
            return  # チェックのあいだに中身が変わった。新しい方に任せる
        item.result = result
        item.status = result.status
        ms = f"（{elapsed_ms}ms）" if elapsed_ms is not None else ""
        self._diag(
            f"チェック: {item.name} → "
            f"{self._diag_status_label(result.status)}{ms}")
        self._restore_ran(item)
        if item.auto_eligible:
            # 対象の控えに、チェックで分かった sha256 も埋める。
            self._remember_pending_run(
                item.key, item.mtime, item.size, result.sha256)
        core.check_cache_put(self._check_cache, item.path,
                             item.mtime, item.size, result)
        core.save_check_cache(self._check_cache)
        self._refresh_list()
        if self._selected_key == key:
            self._show_lower_for(item)
        # 自動化: 「実行を自動で始める」が入っていれば、対象の
        # ファイルはチェックが済んだ時点で実行待ちに入れる。
        self._admit_auto_run(item)

    # ---------------- 自動実行 ----------------
    def _admit_auto_run(self, item: ScriptItem):
        """チェックが済んだ項目を、条件が合えば自動実行の待ち列へ。

        条件: 切替が入っている / 起動後に現れた・変わった分である
        (auto_eligible) / まだ走らせていない (手動で走らせた分も
        含む) / 手動の実行と同じ条件で走らせられる (問題なし・
        警告は可、エラー・チェック不能は不可)。
        待ち列への追加だけで、実際の開始は _pump_auto_run が
        空きを見て行う。
        実行済みの印が復元された分 (ran だが last_run は無い) は
        待ち列には入れないが、実行済みハッシュの歯止め
        (_skip_if_executed) は通して、印と知らせを 1-29 の作法に
        そろえる。"""
        if item is None or item.result is None:
            return
        if item.auto_skip_executed:
            return  # 実行済みの中身と分かった分は、もう相手にしない
        if not item.auto_eligible:
            return
        if item.ran and item.last_run is not None:
            return  # この会話で走らせた分は、もう相手にしない
        if item.status not in (core.STATUS_OK, core.STATUS_WARN):
            return
        if (item.key, item.stat_key()) in self._auto_run_tried:
            return
        if not self._automation_flags()["auto_run"]:
            return
        if self._skip_if_executed(item):
            return  # 同じ中身を実行済み。自動では走らせない
        if item.ran:
            return  # 復元された実行済み。歯止めの控えが古くなって
            # いても、待ち列には入れない (二度走らせない)
        if item.key not in self._auto_run_queue:
            self._auto_run_queue.append(item.key)
            self._diag(f"自動実行の待ち列へ: {item.name}")
        self._pump_auto_run()

    def _skip_if_executed(self, item: ScriptItem) -> bool:
        """中身が実行済みなら、自動実行の見送りを確定して True。

        見送ったら項目に印を付け、知らせを出す (1項目につきこの
        会話で1度だけ)。手動の実行はここでは止めない (この判定を
        通るのは自動実行の入口と待ち列だけ)。"""
        sha = item.result.sha256 if item.result is not None else ""
        if not sha or not core.executed_hash_contains(
                self._executed_hashes, sha):
            return False
        if not item.auto_skip_executed:
            self._diag(
                f"自動実行を見送り（同じ内容を実行済み）: {item.name}")
        item.auto_skip_executed = True
        if item.key not in self._executed_skip_noted:
            self._executed_skip_noted.add(item.key)
            self._set_note(
                "同じ内容を実行済みのため、自動実行しませんでした"
                f"・{item.name}", "warn")
        return True

    def _pump_auto_run(self):
        """自動実行の待ち列から、次の1件を始める。

        実行は直列に限る: 何か走っている間、または AI への回答・
        送信が裏で動いている間は始めない (前の項目の連鎖が済んで
        から次へ進む)。待ち列の先頭から、条件が崩れた項目 (消えた・
        手で走らせた・中身が変わった等) は読み直して読み飛ばす。
        始められたら、その旨を note に残す。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending:
            return
        if not self._automation_flags()["auto_run"]:
            return
        while self._auto_run_queue:
            key = self._auto_run_queue.pop(0)
            item = self._items_by_key.get(key)
            if item is None or not item.auto_eligible or item.ran:
                continue
            if item.result is None or item.status not in (
                    core.STATUS_OK, core.STATUS_WARN):
                continue
            if self._skip_if_executed(item):
                continue  # 同じ中身を実行済み。待ち列から外して進む
            ident = (item.key, item.stat_key())
            if ident in self._auto_run_tried:
                continue
            self._start_run(item)
            if self._running_item is item:
                # 本当に始まったときだけ控える (_start_run が中身の
                # 変化でチェックに戻した場合は、済んだチェックの
                # 結果で改めて待ち列に入る)。
                self._auto_run_tried.add(ident)
                self._diag(f"自動実行を開始: {item.name}")
                self._set_note(
                    f"自動で実行を始めました・{item.name}", "ok")
            return

    # ---------------- 一覧の描画 ----------------
    def _visible_items(self):
        """表に出す項目(表示順)。「ID が無いファイルを表示しない」が
        入っているときは、コマンドID のある項目だけ。チェックや実行の
        追跡は _items 全体で続くので、隠れた項目の状態もここへ戻る。"""
        if not self.config.get("hide_no_id"):
            return list(self._items)
        return [item for item in self._items
                if core.parse_command_id(item.name) is not None]

    def _item_at_row(self, row: int):
        """表の行番号 → 項目。行の解決は必ずこの対応を通す。

        DL の特別行 (チャットで見つけた候補) が先頭にあるときは、
        その行は項目ではないので None を返す (ファイル側の操作は
        何もしない)。"""
        visible = self._visible_items()
        index = row - (1 if self._dl_holder is not None else 0)
        if 0 <= index < len(visible):
            return visible[index]
        return None

    def _duplicate_shas(self):
        """中身(sha256)が同じファイルが2件以上あるハッシュの集合。

        表示中の行だけでなく、追跡中の項目すべてで数える(隠れた
        行との重なりも警告に含めるため)。ハッシュがまだ分からない
        項目(チェック中など)は数に入れない。"""
        counts = {}
        for item in self._items:
            sha = item.result.sha256 if item.result is not None else ""
            if sha:
                counts[sha] = counts.get(sha, 0) + 1
        return {sha for sha, n in counts.items() if n >= 2}

    def _duplicate_others(self, item: ScriptItem):
        """item と中身が同じ、他の項目の名前の一覧。

        重なりが無ければ空。同じ内容の写しが複数あるとき、下半分に
        「どれが重なっているか」を出すために使う。"""
        sha = item.result.sha256 if item.result is not None else ""
        if not sha:
            return []
        return [other.name for other in self._items
                if other is not item and other.result is not None
                and other.result.sha256 == sha]

    def _refresh_list(self):
        # 作り直しの間は currentCellChanged を止める。行が -1 に戻る
        # たびに _on_selection_changed が呼ばれ、そこからまた
        # _refresh_list に入る無限再帰になるため(旧リストで実測)。
        # DL の特別行があるときは、それを常に先頭 (行0) に固定し、
        # ファイルの行は1つずらす。
        selected = self._selected_key
        visible = self._visible_items()
        offset = 1 if self._dl_holder is not None else 0
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(visible) + offset)
            if self._dl_holder is not None:
                self._fill_dl_row(0, self._dl_holder)
            selected_row = -1
            dup_shas = self._duplicate_shas()
            for index, item in enumerate(visible):
                self._fill_row(index + offset, item, dup_shas)
                if item.key == selected:
                    selected_row = index + offset
            if selected_row >= 0:
                self.table.setCurrentCell(selected_row, 0)
            elif visible:
                # 選択が無い(または隠れた)ときは先頭の行を選んだことに
                # する。シグナルを止めているので、選択側の状態もここで
                # 合わせておく。
                self.table.setCurrentCell(offset, 0)
                self._selected_key = visible[0].key
                for item in self._items:
                    if item.key != self._selected_key:
                        item.confirm_armed = False
            elif self._dl_holder is not None:
                # ファイルの行は無いが、特別行だけはある。
                self.table.setCurrentCell(0, 0)
                self._selected_key = None
            else:
                # 出せる行が無い。隠れた項目が選択に残らないようにする。
                self._selected_key = None
        self._fit_name_column()

    def _fit_name_column(self):
        """ファイル名の列幅を、表示中の名前の一番長いものに合わせる
        (上限つき)。

        ResizeToContents だと長い名前が列を際限なく押し広げるので、
        この列だけ Interactive にして幅をここで決める。上限を超える
        名前は省略表示になり (全文はツールチップ)、余った幅は伸縮
        する概要の列が受け取る。窓幅を変えても伸びるのは概要だけ。"""
        header = self.table.horizontalHeader()
        font = self.table.font()
        font.setBold(True)  # ファイル名のセルは太字で描かれる
        metrics = QFontMetrics(font)
        widest = metrics.horizontalAdvance("ファイル名")
        for item in self._visible_items():
            widest = max(widest, metrics.horizontalAdvance(item.name))
        if self._dl_holder is not None:
            widest = max(widest,
                         metrics.horizontalAdvance(self._dl_holder.label))
        width = min(widest + 24, NAME_COLUMN_MAX_WIDTH)
        if header.sectionSize(COL_NAME) != width:
            header.resizeSection(COL_NAME, width)

    def _fill_row(self, row: int, item: ScriptItem,
                  dup_shas=frozenset()):
        when = datetime.fromtimestamp(item.mtime)
        time_cell = QTableWidgetItem(when.strftime("%H:%M"))
        time_cell.setData(Qt.ItemDataRole.UserRole, item.key)
        self.table.setItem(row, COL_TIME, time_cell)
        command_id = core.parse_command_id(item.name) or ""
        id_cell = QTableWidgetItem(command_id)
        self.table.setItem(row, COL_ID, id_cell)
        name_cell = QTableWidgetItem(item.name)
        name_cell.setToolTip(item.name)
        font = name_cell.font()
        font.setBold(True)
        name_cell.setFont(font)
        self.table.setItem(row, COL_NAME, name_cell)
        # ハッシュはチェック結果の sha256 の先頭8文字。チェック前は
        # 空。中身が同じファイルが他にもある行は、ファイル名と
        # ハッシュを警告色にする(写しの消し忘れに気付けるように)。
        sha = item.result.sha256 if item.result is not None else ""
        hash_cell = QTableWidgetItem(sha[:8])
        if sha:
            hash_cell.setToolTip(sha)
        if sha and sha in dup_shas:
            color = QColor(self._theme()["duplicate"])
            name_cell.setForeground(color)
            hash_cell.setForeground(color)
        self.table.setItem(row, COL_HASH, hash_cell)
        status_cell = QTableWidgetItem(self._status_cell_text(item))
        theme = self._theme()
        color = theme["status"].get(item.status, theme["muted"])
        status_cell.setForeground(QColor(color))
        self.table.setItem(row, COL_STATUS, status_cell)
        summary_cell = QTableWidgetItem(item.summary)
        summary_cell.setToolTip(item.summary)
        self.table.setItem(row, COL_SUMMARY, summary_cell)
        # setRowCount では既存のセル部品が残るので、ボタンが無い行は
        # 明示的に外す(状態が変わった行に古いボタンが残らないように)。
        # 姿が変わらないボタンは作り直さず使い回す: 作り直しが押下と
        # 解放の間に入ると、そのクリックが消えて「押せない」感じに
        # なる(実機報告 2026-11-14)。
        existing = self.table.cellWidget(row, COL_RUN)
        spec = self._run_button_spec(item)
        if spec is None:
            if existing is not None:
                self.table.removeCellWidget(row, COL_RUN)
        elif not (isinstance(existing, QPushButton)
                  and existing.property("phal_item_key") == item.key
                  and existing.isEnabled() == spec[0]
                  and bool(existing.property("phal_accent")) == spec[1]
                  and existing.property("phal_mode") == spec[2]
                  and existing.property("phal_theme")
                  == ("dark" if self._dark_mode else "light")):
            self.table.setCellWidget(row, COL_RUN,
                                     self._make_run_button(item))

    def _dl_button_enabled(self) -> bool:
        """DL の特別行のボタンを押せるか。実行・回答・送信・DL の
        どれかが動いているあいだは押せない (裏方が混ざらない)。"""
        return self._running_item is None \
            and not self._ai_filling and not self._ai_sending \
            and not self._dl_pressing

    def _fill_dl_row(self, row: int, holder: _DlCandidate):
        """DL の特別行を描く。列の意味はファイルの行と同じで、
        状態は「DL待ち」、ボタンは「DL」。

        ボタンは、候補と押せる状態が同じなら作り直さない (ファイル
        の行と同じ理由: 作り直しが押下と解放の間に入るとクリックが
        消える)。"""
        when = datetime.fromtimestamp(holder.detected_at)
        self.table.setItem(row, COL_TIME,
                           QTableWidgetItem(when.strftime("%H:%M")))
        self.table.setItem(row, COL_ID, QTableWidgetItem(""))
        self.table.setItem(row, COL_HASH, QTableWidgetItem(""))
        name_cell = QTableWidgetItem(holder.label)
        name_cell.setToolTip(holder.label)
        font = name_cell.font()
        font.setBold(True)
        name_cell.setFont(font)
        self.table.setItem(row, COL_NAME, name_cell)
        summary_cell = QTableWidgetItem(holder.summary)
        summary_cell.setToolTip(holder.summary)
        self.table.setItem(row, COL_SUMMARY, summary_cell)
        status_cell = QTableWidgetItem("DL待ち")
        status_cell.setForeground(QColor(self._theme()["muted"]))
        self.table.setItem(row, COL_STATUS, status_cell)
        existing = self.table.cellWidget(row, COL_RUN)
        enabled = self._dl_button_enabled()
        if not (isinstance(existing, QPushButton)
                and existing.property("phal_dl_key") == holder.key
                and existing.isEnabled() == enabled):
            self.table.setCellWidget(row, COL_RUN,
                                     self._make_dl_button(holder))

    def _make_dl_button(self, holder: _DlCandidate):
        button = QPushButton("DL")
        button.setEnabled(self._dl_button_enabled())
        button.setProperty("phal_dl_key", holder.key)
        # ファイルの行と同じく pressed で始める (作り直し対策)。
        button.pressed.connect(lambda: self._press_download(auto=False))
        return button

    def _status_cell_text(self, item: ScriptItem) -> str:
        # 状態の列は短い語だけ。詳しい見出し(CheckResult.headline)は
        # 選択中の行について下半分に出る。
        # 基本はチェックの判定 (OK / 警告 / 不明) で、この中身を
        # 実行済みなら後ろに「・済」を足して、判定と進み具合の
        # 両方を見せる (1-33)。色は _fill_row が item.status で
        # 引くので、「・済」が付いても判定の色のまま変わらない。
        # エラーは実行できないので「・済」は付かない。
        if item is self._running_item:
            return "実行中"
        if item.status == "checking":
            return "チェック中"
        if item.returned:
            return "差し戻し済"
        if item.status == core.STATUS_ERROR:
            return "エラー"
        if item.status == core.STATUS_OK:
            label = "OK"
        elif item.status == core.STATUS_WARN:
            label = "警告"
        else:
            label = "不明"
        if item.ran:
            label += "・済"
        return label

    def _run_button_spec(self, item: ScriptItem):
        """行のボタンの要否と姿。付けないときは None。

        戻り値は (押せるか, 初回を目立たせるか, 役割)。役割は
        "run"(実行) / "answer"(AIへ回答) / "send"(送信):
          - 実行が終わった行 (ran かつ last_run あり) は「AIへ回答」
          - 回答を入れ終わり、送信ボタンの目印が設定済みなら「送信」
          - 送信まで終わったら、サイクルが戻って「実行」
        チェック中・エラー・チェック不能の行には付けない: チェック
        不能の2段とエラーコピーは右クリックのメニューに置く。警告の
        行は問題なしと同じく実行できる(2026-11-14 緩和)。_fill_row が
        既存のボタンと見比べて、同じなら作り直さないために使う。"""
        if item.status not in (core.STATUS_OK, core.STATUS_WARN) \
                or item.result is None:
            return None
        enabled = self._running_item is None \
            and not self._ai_filling and not self._ai_sending
        if item.ran and item.last_run is not None and not item.sent:
            if item.answered and self._ai_send_ready:
                return (enabled, False, "send")
            return (enabled, False, "answer")
        accent = not item.ran and self._running_item is None
        return (enabled, accent, "run")

    def _make_run_button(self, item: ScriptItem):
        # 行のボタンは1つだけ。実行前は「実行」、実行が終わると
        # 「AIへ回答」、回答が入ると「送信」に替わる (役割は
        # _run_button_spec が決める)。
        spec = self._run_button_spec(item)
        if spec is None:
            return None
        enabled, accent, mode = spec
        button = QPushButton(
            {"run": "実行", "answer": "AIへ回答",
             "send": "送信"}[mode])
        button.setEnabled(enabled)
        if accent:
            button.setStyleSheet(self._theme()["accent_style"])
        button.setProperty("phal_item_key", item.key)
        button.setProperty("phal_accent", accent)
        button.setProperty("phal_mode", mode)
        button.setProperty(
            "phal_theme", "dark" if self._dark_mode else "light")
        # clicked ではなく pressed: 一覧の作り直しが押下と解放の間に
        # 入ると、解放は作り直された側に届いて clicked が出ず、
        # 「押せない」感じになる(実機報告 2026-11-14)。押した瞬間に
        # 始める。
        if mode == "answer":
            button.pressed.connect(lambda it=item: self._answer_to_ai(it))
        elif mode == "send":
            button.pressed.connect(lambda it=item: self._send_to_ai(it))
        else:
            button.pressed.connect(lambda it=item: self._start_run(it))
        return button

    def _on_selection_changed(self, row: int, *_rest):
        if self._dl_holder is not None and row == 0:
            return  # DL の特別行は選択の状態を持たない (ボタンだけが仕事)
        item = self._item_at_row(row)
        self._selected_key = item.key if item is not None else None
        for item in self._items:
            if item.key != self._selected_key:
                item.confirm_armed = False
        self._refresh_list()
        item = self._items_by_key.get(self._selected_key or "")
        if item is not None:
            if item.status == "checking":
                # 選んだ行のチェックをキューの先頭へ回す。
                self._check_worker.request_priority(
                    item.key, item.path, item.stat_key())
            self._show_lower_for(item)

    def _selected_item(self):
        return self._items_by_key.get(self._selected_key or "")

    # ---------------- 行のダブルクリック・右クリック ----------------
    def _on_cell_double_clicked(self, row: int, _column: int):
        item = self._item_at_row(row)
        if item is not None:
            self._open_in_eddy(item)

    def _on_context_menu(self, pos):
        cell = self.table.itemAt(pos)
        if cell is None:
            return
        row = cell.row()
        item = self._item_at_row(row)
        if item is None:
            return
        # 右クリックした行を選んだことにもする(ファイルマネージャの作法)。
        self.table.setCurrentCell(row, 0)
        menu = self._row_menu(item)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _row_menu(self, item: ScriptItem) -> QMenu:
        # 行ごとの操作。出し分けと手順は旧ボタンの並びと同じ:
        # 問題なしは実行、要確認は「中身を見てから実行」の2段、
        # エラーは「エラーをコピー」。「EDDy で開く」は全行共通。
        menu = QMenu(self)
        idle = self._running_item is None
        if item.status in (core.STATUS_OK, core.STATUS_WARN):
            action = menu.addAction("実行")
            action.setEnabled(idle and item.result is not None)
            action.triggered.connect(
                lambda _c=False, it=item: self._start_run(it))
        elif item.status == core.STATUS_UNKNOWN:
            label = ("確認しました。実行する" if item.confirm_armed
                     else "中身を見てから実行")
            action = menu.addAction(label)
            action.setEnabled(idle)
            action.triggered.connect(
                lambda _c=False, it=item: self._view_and_run(it))
        elif item.status == core.STATUS_ERROR:
            action = menu.addAction("エラーをコピー")
            action.triggered.connect(
                lambda _c=False, it=item: self._copy_check_error(it))
        menu.addSeparator()
        ret = menu.addAction("差し戻す…")
        ret.setEnabled(item is not self._running_item)
        ret.triggered.connect(
            lambda _c=False, it=item: self._return_item(it))
        trash = menu.addAction("ゴミ箱に移す")
        trash.setEnabled(item is not self._running_item)
        trash.triggered.connect(
            lambda _c=False, it=item: self._trash_item(it))
        menu.addSeparator()
        eddy = menu.addAction("EDDy で開く")
        eddy.triggered.connect(
            lambda _c=False, it=item: self._open_in_eddy(it))
        return menu

    # ---------------- 下半分 ----------------
    def _show_lower_for(self, item: ScriptItem):
        if self._running_item is not None:
            return
        # 中身が同じファイルが他にもあれば、その名前を末尾に足す。
        # どちらを残すか判断して、写しをゴミ箱へ移せるように。
        others = self._duplicate_others(item)
        dup_note = ("同じ内容のファイルがあります：" + "、".join(others)
                    if others else "")

        def with_dup(text):
            if dup_note:
                return text + "\n" + dup_note
            return text

        if item.result is not None and item.status in (
                core.STATUS_ERROR, core.STATUS_WARN, core.STATUS_UNKNOWN):
            self.result_caption.setText(f"チェックの結果・{item.name}")
            preview = core.build_check_preview(item.result)
            self.output.setPlainText(
                with_dup(preview or item.result.headline))
            if item.status == core.STATUS_ERROR:
                self._set_note(
                    "実行はできません。エラーをコピーして Copilot に貼ると、"
                    "直してもらえます", "error")
            elif item.status == core.STATUS_WARN:
                self._set_note(
                    "警告がありますが、F5 で実行できます。"
                    "理由は上のチェック結果のとおりです", "warn")
            else:
                self._set_note(item.result.headline, "warn")
            return
        if self._last_run is not None:
            key, run = self._last_run
            self.result_caption.setText(f"直前の結果・{run.name}")
            self.output.setPlainText(
                with_dup(core.build_run_preview(run)))
            return
        if item.result is not None:
            self.result_caption.setText(f"チェックの結果・{item.name}")
            self.output.setPlainText(with_dup(item.result.headline))

    # ---------------- 実行 ----------------
    def _f5_pressed(self):
        item = self._selected_item()
        if item is None or self._running_item is not None:
            return
        if item.status in (core.STATUS_OK, core.STATUS_WARN):
            self._start_run(item)
        elif item.status == core.STATUS_UNKNOWN:
            # チェックできなかったものだけは F5 で実行しない。
            # 中身を見る段へ誘導する。
            self._show_lower_for(item)
            self._set_note(
                "チェックできなかったため、F5 では実行しません。"
                "行を右クリックして「中身を見てから実行」から"
                "進めてください", "warn")

    def _view_and_run(self, item: ScriptItem):
        if self._running_item is not None:
            return
        if not item.confirm_armed:
            item.confirm_armed = True
            self._show_lower_for(item)
            self._set_note(
                "該当の行を表示しました。確かめてから、もう一度右クリック"
                "して「確認しました。実行する」を選ぶと実行します", "warn")
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
            item.returned = False
            item.last_run = None
            item.answered = False
            item.sent = False
            self._queue_check(item)
            self._refresh_list()
            self._diag(
                f"中身が変わっていたので実行せず再チェック: {item.name}")
            self._set_note(
                "中身が変わっていたので、チェックからやり直しました", "warn")
            return
        self._running_item = item
        self._diag(f"実行開始: {item.name}")
        # 走らせることが決まったら、開き直しをまたぐ対象の控えは
        # 役目を終える (自動でも手動でも。以後はこの項目の「実行
        # 済み」の印が受け持ち、開き直した後も二度は走らせない)。
        self._forget_pending_run(item.key)
        # 実行を始めた中身の sha256 を「実行済み」として控える
        # (手動の実行も数える)。同じ中身の写しが後から届いても、
        # 自動では二度走らせないためのもの (1-29)。チェック済みの
        # sha256 は、直前の取り直しで今の中身と一致を確認済み。
        run_sha = item.result.sha256 if item.result is not None else ""
        if run_sha:
            core.executed_hash_put(self._executed_hashes, run_sha)
            core.save_executed_hashes(self._executed_hashes)
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
            item.last_run = run  # 差し戻しの証拠に、この項目にも残す
            # 新しい実行結果は、まだ回答も送信もしていない。
            item.answered = False
            item.sent = False
            check = item.result
        else:
            check = None
        flags = ""
        if run.timed_out:
            flags += "・時間切れ"
        if run.stopped:
            flags += "・停止された"
        if run.policy_stop:
            flags += "・方針で停止"
        if run.start_error:
            flags += f"・起動できず（{run.start_error}）"
        self._diag(
            f"実行終了: {run.name} 終了コード {run.exit_code}"
            f"（{run.seconds:.1f}秒）{flags}")
        text = core.build_result_text(
            run.name, run, self.config,
            command_id=core.parse_command_id(run.name))
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
        # 自動化: 「回答を自動で入れる」が入っていれば、人の押下を
        # 待たずに結果を入力欄へ入れる。失敗の知らせは通常の回答と
        # 同じく _on_ai_answer_done が note に出す (黙らない)。
        if item is not None and self._automation_flags()["auto_answer"]:
            self._answer_to_ai(item)
        # 自動実行の待ち列があれば、次を始める (回答の投入が裏で
        # 始まっていたら _pump_auto_run 側で見合わせて、投入の
        # 終わりで改めて呼ばれる)。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- AI へ回答 ----------------
    def _resolve_ai_target(self):
        """PHAL 自前の設定 (script_runner 節のプロファイル) から、
        AI の投げ先を解決する。

        戻り値は (設定済みか, copilot_io へ渡す profile)。profile は
        script_runner_ai.build_profile が作る辞書。窓を特定できる
        情報が無ければ「設定されていない」として (False, None) を
        返す。設定は毎回ファイルから取り直す (「AIの窓…」で変えたら、
        次に押したときから効く)。script_runner_ai は、壊れても PHAL が
        起動できなくならないよう、ここで遅れて import する。"""
        import script_runner_ai as sr_ai

        all_settings = settings_module.load_settings(self.settings_path)
        section = all_settings.get("script_runner") or {}
        fields = sr_ai.active_profile_fields(section)
        if not sr_ai.is_configured(fields):
            return (False, None)
        return (True, sr_ai.build_profile(fields))

    def _refresh_send_ready(self) -> bool:
        """送信ボタンの目印が設定済みかを解き直して控える。

        ボタンの出し分け (_run_button_spec) は行ごとに呼ばれるので、
        設定ファイルの読み直しはここに寄せ、タイミングは窓の起動・
        「AIの窓…」の保存・回答の開始の3か所だけにする。"""
        try:
            import script_runner_ai as sr_ai

            all_settings = settings_module.load_settings(
                self.settings_path)
            section = all_settings.get("script_runner") or {}
            fields = sr_ai.active_profile_fields(section)
            self._ai_send_ready = sr_ai.is_configured(fields) \
                and sr_ai.send_configured(fields)
        except Exception:  # noqa: BLE001  読めなければ未設定として扱う
            self._ai_send_ready = False
        return self._ai_send_ready

    def _open_ai_window(self):
        """PHAL 独自の「AI の窓」の設定・調査画面を開く。

        OK で閉じたら、script_runner 節の ai_profiles に保存する
        (ai_target にも有効なプロファイルの写しを同期する。設定
        画面の実体と欄の整えは script_runner_ai)。"""
        import script_runner_ai as sr_ai

        all_settings = settings_module.load_settings(self.settings_path)
        section = all_settings.get("script_runner") or {}
        profiles, active = sr_ai.sanitize_profiles(section)
        dialog = sr_ai.AiWindowDialog(
            profiles[active]["target"], self,
            automation=sr_ai.sanitize_automation(section),
            profiles=profiles, active_index=active,
            diag_text_provider=self._diagnostics_text,
            error_color=self._theme()["dialog_error"])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updates = sr_ai.profiles_settings(
            dialog.profiles(), dialog.active_index())
        updates.update(dialog.automation())
        settings_module.update_settings_section(
            "script_runner", updates, self.settings_path)
        self._refresh_send_ready()
        self._set_note("AI の窓の設定を保存しました", "ok")
        self._refresh_list()
        # 「実行を自動で始める」を今入れた場合は、既にチェックが
        # 済んでいる対象を待ち列へ入れて回し始める (切替を入れる
        # 前にチェックが済んだ分は _admit_auto_run を通っていない
        # ため)。対象でなければ何も起きない。
        for item in list(self._items):
            self._admit_auto_run(item)

    def _answer_to_ai(self, item: ScriptItem):
        """実行結果を、設定済みの AI の入力欄へ入れる (送信はしない)。

        入れる文は、結果をクリップボードへ入れるときと全く同じ
        build_result_text の文。送るかどうかは人が決める (この道具の
        線引き)。入力欄への投入は裏方 (copilot_io の worker) 仕事で
        数秒かかることがあるので、EDDy と同じく別スレッドで回し、
        終わりはシグナルで受ける。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending:
            return
        run = item.last_run
        if run is None:
            return
        text = core.build_result_text(
            run.name, run, self.config,
            command_id=core.parse_command_id(run.name))
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self._set_note(f"AI へ回答できませんでした（{e}）", "error")
            return
        if not configured:
            self._set_note(
                "AI の投げ先が設定されていません。PHAL の「AIの窓…」"
                "で設定してください", "error")
            return
        self._refresh_send_ready()
        sender = self.ai_sender or _default_ai_sender
        self._ai_filling = True
        self._ai_answer_item = item
        self._refresh_list()

        import threading

        def work():
            try:
                result = sender(text, profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"AI へ回答できませんでした（{e}）")
            self._ai_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-ai-answer").start()

    def _on_ai_answer_done(self, result):
        self._ai_filling = False
        item, self._ai_answer_item = self._ai_answer_item, None
        if getattr(result, "ok", False):
            self._diag(
                f"AIへ回答を入れた: {item.name if item else ''}")
            if item is not None:
                item.answered = True
            if self._ai_send_ready:
                self._set_note("AI の入力欄に結果を入れました", "ok")
            else:
                # 送信ボタンの目印が無ければ、ボタンは「AIへ回答」の
                # ままになる。その理由と直し方を添える。
                self._set_note(
                    "AI の入力欄に結果を入れました。"
                    "送信ボタンは未設定です"
                    "（「AIの窓…」で設定できます）", "ok")
        else:
            message = getattr(result, "message", "") \
                or "AI へ回答できませんでした"
            self._diag(f"AIへ回答に失敗: {message}")
            self._set_note(str(message), "error")
        self._refresh_list()
        # 自動化: 「送信を自動で押す」が入っていれば、入れた回答を
        # そのまま送信まで進める (手で入れた場合も同じ)。送信ボタン
        # の目印が無ければ進めない (その理由は上の note が既に
        # 伝えているので、ここで余計な失敗を重ねない)。
        if getattr(result, "ok", False) and item is not None \
                and self._ai_send_ready \
                and self._automation_flags()["auto_send"]:
            self._send_to_ai(item)
        # この項目の連鎖が一段落したら、自動実行の次を進める
        # (送信まで自動の場合は _on_ai_send_done でもう一度呼ぶ)。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する
        # (送信が裏で始まっていたら、済むまで持ち越しになる)。
        self._fire_deferred_restart()

    # ---------------- AI へ送信 ----------------
    def _send_to_ai(self, item: ScriptItem):
        """AI の送信ボタンを押す (本当に送信する)。

        押す役 (既定は _default_ai_presser) の中で、押す前に
        入力欄を読み直す: 読めなければその段の理由で止め、空なら
        「入力欄が空です」で断る。回答を入れた直後に人が欄を
        空にしていたら、空のまま送る事故になるため。投入と同じく
        裏方仕事なので、別スレッドで回して終わりはシグナルで受ける。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending:
            return
        if item.last_run is None or not item.answered:
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self._set_note(f"送信できませんでした（{e}）", "error")
            return
        if not configured:
            self._set_note(
                "AI の投げ先が設定されていません。PHAL の「AIの窓…」"
                "で設定してください", "error")
            return
        presser = self.ai_presser or _default_ai_presser
        self._ai_sending = True
        self._ai_send_item = item
        self._refresh_list()

        import threading

        def work():
            try:
                result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"送信できませんでした（{e}）")
            self._send_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-ai-send").start()

    def _on_ai_send_done(self, result):
        self._ai_sending = False
        item, self._ai_send_item = self._ai_send_item, None
        if getattr(result, "ok", False):
            self._diag(
                f"AIへ送信を押した: {item.name if item else ''}")
            if item is not None:
                # サイクル完了。last_run (差し戻しの証拠) と回答済みの
                # 印は残し、ボタンだけ「実行」に戻る。
                item.sent = True
            self._set_note("送信しました", "ok")
        else:
            message = getattr(result, "message", "") \
                or "送信できませんでした"
            self._diag(f"AIへの送信に失敗: {message}")
            self._set_note(str(message), "error")
        self._refresh_list()
        # 送信まで済んだので、自動実行の次を進める。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- チャットの見張り・DL ----------------
    def _automation_flags(self) -> dict:
        """自動化の切替 (script_runner 節の auto_*) を読み直す。

        「AIの窓…」で変えたら次から効くよう、使うたびにファイル
        から取る (投げ先の解決と同じ作法)。読めなければ全部手動
        (False) に倒す。"""
        try:
            import script_runner_ai as sr_ai

            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            return sr_ai.sanitize_automation(section)
        except Exception:  # noqa: BLE001  読めなければ手動のまま
            return {"auto_dl": False, "auto_run": False,
                    "auto_answer": False, "auto_send": False,
                    "auto_diag": False}

    def _dl_holder_in_items(self, holder=None) -> bool:
        """DL 候補と同じファイルが、既に一覧にあるか。

        判定は2通り。(1) 候補の名前 (中身の冒頭) から取った
        ファイル名らしき部分が、項目名と一致する。(2) 候補の
        概要が、項目の概要 (先頭コメント) と一致する。

        名前だけでは言い当てられない: カードの名前は中身の冒頭
        そのもので、ファイル名らしき部分はコードの中に出てくる
        別のファイル名 (script_runner.py など) を拾うことがあり、
        落としたファイル自身の名前は含まれないことが多い。1-27
        の実機では、この判定が外れ続けて、設置の開き直しのたびに
        同じ候補を落とし直す無限ループになった。概要は設置
        ファイルごとに違う一文なので、こちらを本命にする。"""
        holder = holder if holder is not None else self._dl_holder
        if holder is None:
            return False
        match = _DL_FILENAME_RE.search(holder.name)
        if match is not None and any(
                item.name == match.group(0) for item in self._items):
            return True
        if holder.summary:
            return any(
                _summary_matches(holder.summary, item.summary)
                for item in self._items)
        return False

    def _maybe_check_chat(self):
        """チャットの DL 候補の検出を、間引いて裏で回す。

        フォルダの見回り (_poll_inner) の終わりに呼ばれる。間隔
        (CHAT_POLL_INTERVAL_SEC) が空いていなければ何もしない。
        投げ先が未設定なら見張る相手が居ないので回さない。検出は
        裏方 (UIA) 仕事なので別スレッドで回し、結果はシグナルで
        受ける。失敗は黙る (特別行が出ないだけ) ので、ここでも
        note には出さない。"""
        # 候補と同じファイルが一覧に来たら、特別行は役目を終える
        # (人が手で落とした場合など。検出の結果を待たない)。
        if self._dl_holder is not None and self._dl_holder_in_items():
            self._diag(
                "DL行を下げた（ファイルが一覧に現れた）: "
                f"{self._dl_holder.label}")
            self._dl_holder = None
            self._refresh_list()
        if self._dl_checking:
            return
        now = time.monotonic()
        if now - self._dl_last_check < CHAT_POLL_INTERVAL_SEC:
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  見張れないだけ
            return
        if not configured:
            return
        self._dl_checking = True
        self._dl_last_check = now
        downloader = self.ai_downloader or _default_ai_downloader

        import threading

        def work():
            try:
                result = downloader(profile)
            except Exception as e:  # noqa: BLE001
                result = _DlCheckResult(False, str(e), None)
            self._dl_signals.checked.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-dl-watch").start()

    def _on_dl_checked(self, result):
        self._dl_checking = False
        if not getattr(result, "ok", False):
            return  # 失敗は黙る。行は出ないし、今の行もそのまま
        # 診断リクエストの目印は、候補の有無と関係なく同じ走査の
        # 結果に乗ってくる (1-43)。候補の処理より先に見る (候補が
        # 無い結果では、この先で return してしまうため)。
        self._maybe_auto_diag(getattr(result, "marker", None))
        candidate = getattr(result, "candidate", None)
        if not isinstance(candidate, dict) or not candidate.get("name"):
            # 落とすものは無い。特別行があれば下げる。
            if self._dl_holder is not None:
                self._diag(
                    "DL行を下げた（候補が無くなった）: "
                    f"{self._dl_holder.label}")
                self._dl_holder = None
                self._refresh_list()
            return
        holder = _DlCandidate(candidate)
        # 同じファイルが既に一覧にあるか (名前・概要での判定)。
        present = self._dl_holder_in_items(holder)
        # 開き直しをまたぐ押下済みの控えにあり、かつそのファイルが
        # 一覧にあれば、もう落とした物として出さない。ファイルが
        # 無くなっていれば (人が消した場合)、落とし直せるよう
        # 再び出すのが正しい。
        persisted = core.dl_consumed_get(
            self._dl_consumed_persisted, holder.name, holder.class_name)
        if holder.key in self._dl_consumed or present \
                or (persisted is not None and present):
            # 押下済み、または同じファイルが既に一覧にある。
            if self._dl_holder is not None:
                self._diag(
                    "DL行を下げた（押下済み・一覧にある候補だった）: "
                    f"{self._dl_holder.label}")
                self._dl_holder = None
                self._refresh_list()
            return
        if self._dl_holder is not None \
                and self._dl_holder.key == holder.key:
            return  # 同じ候補のまま。検出時刻もそのままにする
        self._dl_holder = holder
        self._diag(
            f"DL候補を表示: {holder.summary or holder.label}")
        self._refresh_list()
        # 自動化: 「DLを自動で押す」が入っていれば、新しく見つけた
        # 候補を1回だけ自動で押す。失敗しても繰り返さない (理由は
        # _on_dl_pressed が note に出す。次の新しい候補でまた試す)。
        if self._automation_flags()["auto_dl"] \
                and holder.key not in self._dl_auto_tried:
            self._dl_auto_tried.add(holder.key)
            self._press_download(auto=True)

    def _press_download(self, auto: bool):
        """DL の特別行のボタンの仕事。候補を押して落とす。

        押下役 (既定は _default_ai_dl_presser) の中で、候補は
        探し直してから Invoke で押す。投入と同じく裏方仕事なので
        別スレッドで回し、終わりはシグナルで受ける。"""
        holder = self._dl_holder
        if holder is None or self._dl_pressing:
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self._set_note(f"ダウンロードできませんでした（{e}）", "error")
            return
        if not configured:
            self._set_note(
                "AI の投げ先が設定されていません。PHAL の「AIの窓…」"
                "で設定してください", "error")
            return
        presser = self.ai_dl_presser or _default_ai_dl_presser
        self._dl_pressing = True
        self._dl_press_auto = auto
        self._dl_press_key = holder.key
        self._diag(
            f"DL押下を開始（{'自動' if auto else '手動'}）: "
            f"{holder.summary or holder.label}")
        # 押下を始めた候補は、開き直しをまたいで控える (手動でも
        # 自動でも)。開き直した先で同じ候補を見つけても、落とした
        # ファイルが一覧にある間は二度と出さない・押さない。
        core.dl_consumed_put(self._dl_consumed_persisted,
                             holder.name, holder.class_name,
                             holder.summary)
        core.save_dl_consumed(self._dl_consumed_persisted)
        self._refresh_list()

        import threading

        def work():
            try:
                result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"ダウンロードできませんでした（{e}）")
            self._dl_signals.pressed.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-dl-press").start()

    def _on_dl_pressed(self, result):
        self._dl_pressing = False
        if getattr(result, "ok", False):
            self._diag("DL押下に成功（到着待ち）")
            # 到着の確認は、落としたファイルが一覧に現れた時点
            # (_poll_candidate) で残す。候補の表示名を控えておく。
            holder = self._dl_holder
            self._dl_awaiting = (
                holder.label if holder is not None else "",
                time.monotonic())
            # 押せた候補は、この会話ではもう出さない (落とした
            # ファイルはフォルダの見回りが通常の行として拾う)。
            if self._dl_press_key is not None:
                self._dl_consumed.add(self._dl_press_key)
            if self._dl_holder is not None \
                    and self._dl_holder.key == self._dl_press_key:
                self._dl_holder = None
            self._set_note(
                "自動でダウンロードしました" if self._dl_press_auto
                else "ダウンロードしました", "ok")
        else:
            message = getattr(result, "message", "") \
                or "ダウンロードできませんでした"
            self._diag(f"DL押下に失敗: {message}")
            if "ダウンロードパネルが開いたままでした" in str(message):
                # 押下役 (裏方) からの文に含まれる、フライアウトを
                # 閉じきれなかったという知らせを取り出しておく
                # (1-40。成功時は裏方から届かないので残せない)。
                self._diag(
                    "フライアウト: 閉じきれず開いたままだった")
            self._set_note(str(message), "error")
        self._dl_press_key = None
        self._refresh_list()

    def _send_escape_to_ai(self):
        """DL の到着が確かめられた直後、AI の窓へ ESC を送る
        (裏方仕事、1-67)。

        Edge のダウンロード・フライアウトは、この到着のときに
        開いたもの。閉じておかないと次の押下の邪魔になる
        (1-40 の UIA 解散が実物の形に当たらなかったための
        キー配送版)。結果は診断にだけ残し、失敗しても連鎖は
        止めない。投げ先が解決できなければ送らない。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  解決できなければ送らない
            configured, profile = False, None
        if not configured:
            self._diag("ESC送信: 投げ先が未設定のため見送り")
            return
        sender = self.ai_esc_sender or _default_ai_esc_sender

        import threading

        def work():
            try:
                result = sender(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"ESC送信: failed（{e}）")
            self._dl_signals.escaped.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-esc").start()

    def _on_escape_sent(self, result):
        message = str(getattr(result, "message", "") or "")
        self._diag(message if message else "ESC送信: failed")

    # ---------------- 診断の自動応答 (1-43) ----------------
    def _note_diag_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に残す。

        見張りは間隔を置いて何度も同じ目印を見つける。理由を毎回
        残すと、控え (上限60件) が見送りの文で埋まってしまう。"""
        if len(self._diag_noted) > 50:
            self._diag_noted.clear()
        stamp = (mid, key)
        if stamp in self._diag_noted:
            return
        self._diag_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_diag(self, marker):
        """見張りの結果に乗ってきた診断リクエストの目印を判定する。

        送るのは、切替「診断の自動応答」が入っていて、同じ発言を
        消費済みでなく、直近の送信から間隔が空いていて、回答・
        送信の連鎖が空いているときだけ。間隔と連鎖が理由の間は
        見送りにして、見張りの次の回で改めて判定する (目印が
        最新の発言に残っている限り、また届く)。"""
        mid = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
        if not mid:
            self._diag_marker_pending = None
            return
        if mid == str(self._diag_auto.get("consumed_id") or ""):
            self._diag_marker_pending = None
            return  # 消費済みの発言。二度は送らない
        if not self._automation_flags()["auto_diag"]:
            self._note_diag_once(
                mid, "off",
                "診断リクエストを検出（自動応答は切のため見送り）")
            return
        if self._diag_sending:
            self._diag_marker_pending = mid
            return
        last = float(self._diag_auto.get("last_sent_at") or 0.0)
        if time.time() - last < core.DIAG_AUTO_COOLDOWN_SEC:
            self._diag_marker_pending = mid
            self._note_diag_once(
                mid, "cooldown",
                "診断の自動応答: 直近に送ったばかりのため見送り"
                "（間隔が空いてから送ります）")
            return
        if self._chain_in_flight():
            self._diag_marker_pending = mid
            self._note_diag_once(
                mid, "busy",
                "診断の自動応答: 回答・送信の連鎖の途中のため見送り"
                "（空いてから送ります）")
            return
        self._diag_marker_pending = None
        self._fire_auto_diag(mid)

    def _consume_diag_marker(self, mid):
        """目印を消費済みにして控えへ残す (開き直しをまたぐ)。

        送れたときだけでなく、見送り (書きかけ)・失敗のときも
        消費する。同じ発言への試みは1回だけに決めておかないと、
        見張りのたびに裏方を起こし続けることになる。呼び直しは
        新しい発言 (別の目印) でできる。"""
        if not mid:
            return
        self._diag_auto["consumed_id"] = mid
        core.save_diag_auto(self._diag_auto)

    def _fire_auto_diag(self, mid: str):
        """診断の束を、入力欄へ入れて送信まで一気にやる (裏方仕事)。

        入れる役は回答と同じ ai_sender (copilot_io.fill_text)。
        そちらは入力欄に書きかけがあれば上書きせずに断るので、
        書きかけを消す事故にはならない (断られたら見送りとして
        記録し、目印は消費する)。押す役も送信と同じ ai_presser で、
        押す前に入力欄を読み直して空なら断る。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001  理由は診断と note へ
            self._diag(f"診断の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_diag_marker(mid)
            return
        if not configured:
            self._diag("診断の自動応答に失敗: AI の投げ先が未設定")
            self._consume_diag_marker(mid)
            self._set_note(
                "診断の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        bundle = self._diagnostics_text()
        if not bundle:
            self._diag("診断の自動応答に失敗: 束が作れませんでした")
            self._consume_diag_marker(mid)
            return
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._diag_sending = True
        self._diag_auto_mid = mid
        self._diag("診断の自動応答を開始")

        import threading

        def work():
            try:
                result = sender(bundle, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"診断を送れませんでした（{e}）")
            self._diag_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-diag-auto").start()

    def _on_diag_auto_done(self, result):
        self._diag_sending = False
        mid, self._diag_auto_mid = self._diag_auto_mid, None
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._diag_auto["last_sent_at"] = time.time()
            self._consume_diag_marker(mid)
            self._diag("診断を自動で送りました")
            self._set_note("診断を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            # 入力欄に書きかけがあった。消さずに見送る (この道具の
            # 線引き)。理由は診断に残し、目印は消費する。
            self._consume_diag_marker(mid)
            self._diag(
                f"診断の自動応答を見送り（入力欄に書きかけ）: {message}")
            self._set_note(
                "診断の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        else:
            self._consume_diag_marker(mid)
            self._diag(f"診断の自動応答に失敗: {message}")
            self._set_note(
                f"診断を自動で送れませんでした（{message}）", "error")
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

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

    # ---------------- 差し戻し・ゴミ箱 ----------------
    def _return_item(self, item: ScriptItem):
        # 実行時にエラーになった物を、証拠(実行結果)と全文つきで
        # AI へ返す。理由は任意(実行結果が主な証拠)なので空でも進める。
        reason, ok = QInputDialog.getMultiLineText(
            self, "差し戻す", "理由・言いたいこと(空でも構いません)", "")
        if not ok:
            return
        try:
            script_text = item.path.read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            script_text = ""
        self._set_clipboard(core.build_return_text(
            item.name, reason, item.last_run, self.config, script_text,
            command_id=core.parse_command_id(item.name)))
        item.returned = True
        self._set_note("差し戻しの文をクリップボードに入れました", "ok")
        self._refresh_list()

    def _trash_item(self, item: ScriptItem):
        if item is self._running_item:
            return
        if core.move_to_recycle_bin(item.path):
            self._set_note(f"{item.name} をゴミ箱に移しました", "ok")
            # 一覧の掃除は見回りがやる(消えたファイルは項目ごと落ちる)。
            # 1回余分に回して、行がすぐ消えるようにする。
            self._poll()
        else:
            self._set_note(
                f"{item.name} をゴミ箱に移せませんでした", "error")

    # ---------------- あっふ で開く ----------------
    def _open_watch_in_filer(self) -> None:
        """対象フォルダを あっふ のアクティブな窓で開く。届かなければ
        下のメモ欄で知らせる(エクスプローラーでは開かない)。"""
        try:
            import filer_launch

            result = filer_launch.open_or_navigate(str(self.watch_folder))
        except Exception:  # noqa: BLE001  届かない理由は問わず知らせる
            result = ""
        if result not in ("shown", "spawned"):
            # "starting" はファイラの起動途中で、移動が届いていない。
            self._set_note("あっふ で開けませんでした", "error")

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

    # ---------------- 再開(更新したコードへの乗り換え) ----------------
    def request_restart(self) -> None:
        """外から届く開き直しの頼み (設置スクリプトの仕掛け) の入口。

        設置スクリプトの開き直しは、その実行の終了とほぼ同時に
        届く。そのまま閉じると、実行結果の回答・送信 (自動化の
        連鎖) が飛んでしまう。連鎖の途中なら頼みを覚えておいて、
        一段落してから開き直す。何も動いていなければ、従来どおり
        その場で開き直す。手で押す「開き直す」ボタンは restart に
        直結のままで、従来どおり即時に開き直す (実行中はボタン
        自体が無効になる)。"""
        if self._chain_in_flight():
            self._restart_deferred = True
            self._diag("開き直しの頼みを受けた（連鎖の終了まで保留）")
            self._set_note(
                "開き直しは、自動化の処理が終わってから行います",
                "warn")
            return
        self._diag("開き直しの頼みを受けた（即時）")
        self.restart()

    def _chain_in_flight(self) -> bool:
        """自動化の連鎖が途中かどうか。

        実行中・回答の投入中・送信中はもちろん途中。自動実行の
        待ち列に残りがある場合も、切替が入っている間は途中と
        みなす (切替が切れた待ち列は進まないので、途中には数え
        ない。詰まったまま開き直しが永遠に来ない事態を避ける)。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending:
            return True
        return bool(self._auto_run_queue) \
            and self._automation_flags()["auto_run"]

    def _fire_deferred_restart(self) -> None:
        """保留中の開き直しを、連鎖が済んでいれば実行する。

        連鎖の節目 (_on_run_done / _on_ai_answer_done /
        _on_ai_send_done の末尾) と見回りの終わりから呼ばれる。
        呼び出しの直前に _pump_auto_run が次の実行を始めていたら、
        連鎖はまだ途中なので持ち越す。"""
        if not self._restart_deferred or self._chain_in_flight():
            return
        self._restart_deferred = False
        self.restart()

    def restart(self) -> None:
        """新しいコードで開き直す。

        先に寝かせ役(script_runner_launch の respawn)を置いてから、通常の
        閉じ方で終わる。寝かせ役が、このプロセスが消えたのを見て後継を
        起こすので、後継はディスクの新しいコードで上がる。実行中の
        スクリプトがあれば、通常の終了と同じく closeEvent が止める。

        寝かせ役を置けなければ、閉じずに残って知らせる。古いコードの
        ままではあるが、落とすだけになるのを避けるため。
        """
        try:
            script_runner_launch.spawn_respawn_helper(self.settings_path)
        except Exception:  # noqa: BLE001
            self._diag("開き直しの準備に失敗した")
            self._set_note("再開の準備ができませんでした", "error")
            return
        self._diag("開き直しを実行する")
        self.close()

    # ---------------- 絞り込み・前面固定・終了 ----------------
    def _apply_hide_no_id(self, on: bool):
        self.config["hide_no_id"] = bool(on)
        try:
            settings_module.update_settings_section(
                "script_runner", {"hide_no_id": bool(on)},
                self.settings_path)
        except Exception:  # noqa: BLE001  覚えられないだけ
            pass
        self._refresh_list()
        if self._running_item is not None:
            return  # 実行中の下半分は実行側の表示に任せる
        # 下半分が、隠れた項目のまま残らないように合わせる。
        item = self._selected_item()
        if item is not None:
            self._show_lower_for(item)
        elif self._last_run is not None:
            _key, run = self._last_run
            self.result_caption.setText(f"直前の結果・{run.name}")
            self.output.setPlainText(core.build_run_preview(run))
        else:
            self.result_caption.setText("直前の結果")
            self.output.setPlainText("")

    def _apply_on_top(self, on: bool, save: bool):
        self.config["always_on_top"] = bool(on)
        flag = Qt.WindowType.WindowStaysOnTopHint
        if bool(self.windowFlags() & flag) != bool(on):
            # フラグを変えると窓はいったん隠れる。隠れた後では
            # isVisible() が False になるので、見えていたかは
            # 変える前に覚えておく (実機報告 2026-11-14: 前面に
            # 固定のチェックで窓が消えた)。
            was_visible = self.isVisible()
            self.setWindowFlag(flag, bool(on))
            if was_visible:
                self.show()
                self.raise_()
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
                 "always_on_top": bool(self.config.get("always_on_top")),
                 "hide_no_id": bool(self.config.get("hide_no_id")),
                 "dark_mode": bool(self.config.get("dark_mode"))},
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
