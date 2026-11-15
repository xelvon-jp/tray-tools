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
PHAL_VERSION = "1.62"

import argparse
import ctypes
import dataclasses
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
    同じ顔 (ok / message / candidate / marker / reset_marker /
    candidates)。検出役が例外を投げたときの受け皿にも使う。
    marker は診断リクエストの目印 {"id": 同定子} (1-43)、
    reset_marker はリセットの目印 (1-52) で、形は marker と同じ。
    candidates はスクリプトらしいカード全件の辞書 (木の順、
    1-57) で、持たない経路では None。"""

    def __init__(self, ok, message, candidate=None, marker=None,
                 reset_marker=None, candidates=None):
        self.ok = ok
        self.message = message
        self.candidate = candidate
        self.marker = marker
        self.reset_marker = reset_marker
        self.candidates = candidates


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

    def __init__(self, ok, message, needs_confirm=False,
                 attach_done=None, attach_failed=None):
        self.ok = ok
        self.message = message
        self.needs_confirm = needs_confirm
        # 添付の結果 (1.58、copilot_io.SendResult と同じ顔)。
        self.attach_done = list(attach_done or [])
        self.attach_failed = list(attach_failed or [])


def _default_ai_sender(text, profile, attachments=None):
    """AI の入力欄へ入れるだけの送信役 (送信はしない)。

    copilot_io.fill_text を使う (EDDy のテスト入力と同じ経路)。
    profile は PHAL 自前の投げ先 (script_runner_ai.build_profile が
    作る辞書) をそのまま渡す。import は遅らせる: copilot_io が壊れても
    PHAL の窓は開くように (投げ先の解決も同じ理由で遅延 import)。
    attachments は、outbox に実在することが確かめられた成果
    ファイルのフルパス (1.58)。copilot 側が、入力欄へ入れる前に
    添付する。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile,
                                attachments=attachments)


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


def _default_ai_dl_presser(profile, key=None):
    """チャットのダウンロード候補を押す役 (本当に落とす)。

    copilot_io.press_download_button を使う (裏方経由で、候補を
    探し直して Invoke で押す)。key は窓側が選んだ候補
    ({"name", "class_name"}、1-57) で、裏方はその候補を探して
    押す。戻り値は SendResult と同じ顔。"""
    import copilot_io

    return copilot_io.press_download_button(profile=profile, key=key)


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
        # 直近の実行でクリップボードへ入れた結果文そのもの (再実行
        # なら差分だけの文)。「AIへ回答」はこれをそのまま入れる
        # ので、作り直して差分が落ちることがない (1.56)。
        self.last_result_text = None
        # 直近の実行の標準出力にあった「添付：ファイル名」の指定
        # (名前だけの並び、1.58)。結果文には含まれていない。
        # 「AIへ回答」の段で outbox と照合し、実在するものだけを
        # 添えて copilot 側へ渡す。
        self.attach_names = []
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
        self._running_rerun = False  # 実行中のが「再実行」かどうか (1.56)
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
        self._dl_launch_keys = None   # 起動時に既にあった DL 候補の
        # key の控え。最初の検出で取り、ここにある候補は自動
        # ダウンロードの対象外にする (自動実行の起動スナップ
        # ショットと同じ考え方。開き直しの直後に古い添付を拾い
        # 直して落とし直すのを防ぐ)。手動の押下は対象外のまま。
        # 押下済み候補の控え (開き直しをまたいで残る)。会話内の
        # 控えは開き直しで消えるので、core の控えファイルから
        # 読み込んでおく (1-29)。
        self._dl_consumed_persisted = core.load_dl_consumed()
        self._dl_checking = False
        self._dl_last_check = 0.0
        # 直近の走査の1行 (診断の束に出す。1-57)。検出失敗・候補
        # の抑制が無言で消えないよう、走査のたびに組み立て直し、
        # 変わったときだけ診断の控えにも残す (_note_dl_scan)。
        self._dl_last_scan = "直近の走査: まだ無い"
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
        # 最中の印、結果を受けるシグナル。_diag_auto_mid は送信の
        # 最中の (mid, key) の組 (1-49)。_diag_marker_pending は
        # 従来どおり mid だけを入れる。
        self._diag_auto = core.load_diag_auto()
        self._diag_marker_pending = None
        self._diag_sending = False
        self._diag_auto_mid = None
        self._diag_noted = set()
        self._diag_signals = _AiSignals()
        self._diag_signals.done.connect(self._on_diag_auto_done)
        # リセットの合図 (1-52)。消費済みの目印と直近に開き直した
        # 時刻の控え (開き直しをまたぐ。診断の控えとは混ぜない)、
        # 見送り中の目印、見送り理由を1度だけ残すための控え。
        self._reset_auto = core.load_reset_auto()
        self._reset_marker_pending = None
        self._reset_noted = set()
        # 受領応答 (1-51)。送信の最中の印、結果を受けるシグナル、
        # 回答が受領の完了待ちになった項目の控え。回答・送信の
        # 連鎖の印 (_ai_filling 等) とは混ぜない。
        self._ack_sending = False
        self._ack_pending_answer = None
        self._ack_signals = _AiSignals()
        self._ack_signals.done.connect(self._on_ack_done)
        # チェックで止まった知らせ (1-53)。知らせ済みの中身
        # (sha256) の控え (開き直しをまたぐ)、送信の最中の印、
        # 結果を受けるシグナル。受領の系統とは混ぜない。
        self._blocked_notified = core.load_blocked_notified()
        self._notice_sending = False
        self._notice_signals = _AiSignals()
        self._notice_signals.done.connect(self._on_notice_done)
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
        # ループ記録の控え (1.51)。_on_run_done が1手1件で足す
        # (古い順)。「記録をコピー」が直近20手までを Markdown に
        # してクリップボードへ入れる。起動後の実行だけで、開き直す
        # と消える (永続化はしない)。
        self._loop_records = []
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
        # 前回の実行結果の控え (1.56)。再実行の差分の相手。
        # コマンドID (無ければファイル名) ごとに直近1件の結果文
        # (差分の対象になる部分)。開き直しをまたぐ JSON から読む。
        self._last_results = core.load_last_results()
        # 外から届いた開き直しの頼みを、自動化の連鎖が済むまで
        # 保留している印。設置スクリプトの開き直しは実行の終了と
        # ほぼ同時に届くので、そのまま閉じると回答・送信が飛ぶ。
        self._restart_deferred = False
        # ARiA モニタ表示用の状態 (req-20261115-02)。_phal_step は
        # 起動後の実行手数。_phal_command_id / _phal_item は、実行中
        # または直近のスクリプトのコマンドIDと概要。_publish_phal_status
        # が、状態が変わったときだけ phal_status.json へ書く。
        self._phal_step = 0
        self._phal_command_id = ""
        self._phal_item = ""
        # 窓の上の帯 (1.48) のための、実行の時刻の控え。
        # _run_started_at は今の実行の開始 (monotonic)、
        # _last_run_seconds は直近の実行の所要秒 (run.seconds)。
        self._run_started_at = None
        self._last_run_seconds = None
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
        # 窓の上の帯の 1秒ごとの付け直し (1.48)。経過時間だけが
        # 変わるので、軽い文言の付け直しだけ (追加のスレッド無し)。
        self._strip_timer = QTimer(self)
        self._strip_timer.timeout.connect(self._refresh_status_strip)
        self._strip_timer.start(1000)
        # 起動時は待機。モニタ側は、このファイルがあれば PHAL が
        # 動いていると分かる。
        self._publish_phal_status("idle", "待機中")

    # ---------------- 画面 ----------------
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 窓の上の細い帯 (1.48)。手数・経過時間・今のねらいを
        # 1行で出す。ねらいが長いと窓の最小幅を押し広げるので、
        # 横幅はレイアウトに任せる (見出し・フォルダ名と同じ
        # 作法)。中身は _refresh_status_strip が 1秒ごとに
        # 付け直す。
        strip = QLabel()
        strip.setStyleSheet("font-size: 11px;")
        strip.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(strip)
        self.status_strip = strip
        self._refresh_status_strip()

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

        # 「ループを始める」は窓全体の入口。行の「AIへ回答」は実行
        # が終わった行にだけ付くが、こちらは一覧の状態に関わらず
        # いつでも押せる。表の直下に置く (1.47)。
        intro_row = QHBoxLayout()
        self.intro_button = QPushButton("ループを始める")
        self.intro_button.setToolTip(
            "導入プロンプトをクリップボードへ入れます")
        self.intro_button.clicked.connect(self._copy_intro_prompt)
        intro_row.addWidget(self.intro_button)
        # 「記録をコピー」は「ループを始める」の隣 (1.51)。どちらも
        # クリップボードへ入れるだけのボタンで、表の直下に並べる。
        self.record_button = QPushButton("記録をコピー")
        self.record_button.setToolTip(
            "直近の実行履歴を Markdown にしてクリップボードへ入れます")
        self.record_button.clicked.connect(self._copy_loop_record)
        intro_row.addWidget(self.record_button)
        intro_row.addStretch(1)
        layout.addLayout(intro_row)

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

    def _publish_phal_status(self, state, detail="",
                             stopped_reason="", command_id=None,
                             item=None):
        """ARiA モニタ表示用の状態を手元ファイルへ出す (req-20261115-02)。

        state は idle / dl / run / answer / send / stopped。command_id
        と item を渡せば、実行中または直近のスクリプトの素性を
        差し替える (渡さなければ今のまま)。書くのは状態が変わった
        ときだけで、書き込みに失敗してもループは止めない (どちらも
        core 側が受け持つ)。"""
        try:
            if command_id is not None:
                self._phal_command_id = command_id
            if item is not None:
                self._phal_item = item
            status = core.build_phal_status(
                PHAL_VERSION, state,
                command_id=self._phal_command_id,
                detail=detail, stopped_reason=stopped_reason,
                step=self._phal_step, item=self._phal_item)
            core.write_phal_status(status)
        except Exception:  # noqa: BLE001  状態の書き出しは本体を止めない
            pass

    def _publish_phal_idle(self, detail="待機中"):
        """連鎖が何も動いていなければ待機に戻す。

        実行・回答・送信の途中で呼ぶと、動いている段の表示を
        待機で上書きしてしまうので、連鎖が空いているときだけ
        書く。"""
        if not self._chain_in_flight():
            self._publish_phal_status("idle", detail)

    def _status_strip_text(self) -> str:
        """窓の上の帯の1行 (1.48)。

        手数・経過時間・今のねらい。実行中は今の実行の経過秒、
        待機中は直近の実行の所要秒、まだ一度も実行していなければ
        「--:--」。ねらいは phal_status.json の item と同じ出所
        (_phal_item)。"""
        if self._run_started_at is not None:
            label = "経過"
            seconds = time.monotonic() - self._run_started_at
        elif self._last_run_seconds is not None:
            label = "所要"
            seconds = self._last_run_seconds
        else:
            label = "経過"
            seconds = None
        return (f"手数 {self._phal_step} ・ {label} "
                f"{core.format_elapsed(seconds)}"
                f" ・ ねらい：{self._phal_item}")

    def _refresh_status_strip(self):
        """帯の文言を付け直す。1秒ごとのタイマと、実行の開始・
        終了の直後から呼ぶ。"""
        text = self._status_strip_text()
        self.status_strip.setText(text)
        self.status_strip.setToolTip(text)

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
            # 直近の走査 (1-57)。検出失敗や候補の抑制の切り分けに
            # 使う。組み立ては _note_dl_scan が持っている。
            self._dl_last_scan,
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
                # ダウンロードの段は終わり。自動実行が始まれば
                # そちらが run を出す。
                self._publish_phal_idle(
                    f"ダウンロードの到着を確認: {item.name}")
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
            item.last_result_text = None
            item.attach_names = []
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
            self._publish_phal_idle(
                "ダウンロードの到着を確認できませんでした")
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
        if item.status == core.STATUS_ERROR:
            # チェックで実行不可。自動では走らせないが、新規の
            # 対象ファイルなら無言で終わらせず知らせる (1-53)。
            self._notify_blocked(item)
            return
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
        送信・受領応答が裏で動いている間は始めない (前の項目の
        連鎖が済んでから次へ進む)。待ち列の先頭から、条件が崩れた
        項目 (消えた・手で走らせた・中身が変わった等) は読み直して
        読み飛ばす。始められたら、その旨を note に残す。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._ack_sending \
                or self._notice_sending:
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
            self._start_run(item, auto=True)
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
        "run"(実行) / "rerun"(再実行) / "answer"(AIへ回答) /
        "send"(送信):
          - 実行が終わった行 (ran かつ last_run あり) は「AIへ回答」
          - 回答を入れ終わり、送信ボタンの目印が設定済みなら「送信」
          - 送信まで終わったら、サイクルが戻って「実行」。前回の
            結果が控えに残っていれば「再実行」(差分だけを送る)
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
        if self._has_previous_result(item):
            # 実行済みで前回の結果が残っている行の「実行」は、
            # 差分だけを送る「再実行」になる (1.56)。
            return (enabled, accent, "rerun")
        return (enabled, accent, "run")

    def _make_run_button(self, item: ScriptItem):
        # 行のボタンは1つだけ。実行前は「実行」、実行が終わると
        # 「AIへ回答」、回答が入ると「送信」に替わる。前回の結果が
        # 控えに残っている行の「実行」は「再実行」になる (役割は
        # _run_button_spec が決める)。
        spec = self._run_button_spec(item)
        if spec is None:
            return None
        enabled, accent, mode = spec
        button = QPushButton(
            {"run": "実行", "rerun": "再実行", "answer": "AIへ回答",
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
        elif mode == "rerun":
            button.pressed.connect(lambda it=item: self._rerun_item(it))
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
            # 前回の実行結果が控えにあれば「再実行」も出す。結果文
            # は前回との差分だけになる (1.56)。
            if self._has_previous_result(item):
                rerun_action = menu.addAction("再実行")
                rerun_action.setEnabled(idle and item.result is not None)
                rerun_action.triggered.connect(
                    lambda _c=False, it=item: self._rerun_item(it))
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
    def _rerun_key(self, item: ScriptItem) -> str:
        """前回の結果の控えで使う、そのスクリプトの鍵。

        コマンドID (ファイル名の先頭) があればそれ、無ければ
        ファイル名。再ダウンロードで名前が変わっても、同じ ID
        なら同じ控えに当たる (1.56)。"""
        return core.parse_command_id(item.name) or item.name

    def _has_previous_result(self, item: ScriptItem) -> bool:
        """前回の実行結果が控えに残っているか (再実行の出し分け用)。

        控えは開き直しをまたぐ JSON (phal_last_results.json) で、
        実行が終わるたびに直近1件で上書きされる。"""
        return self._rerun_key(item) in self._last_results

    def _rerun_item(self, item: ScriptItem):
        """「再実行」(行のボタン・右クリックのメニューから)。前回
        の結果との差分だけを結果文にして、もう一度実行する (1.56)。

        対象はファイルがまだある行だけ。無くなっていれば、その
        旨をステータスに出して止める (チェックにも実行にも
        進まない)。実行そのものは通常の経路 (_start_run) を使う。
        明示の手動操作なので、実行済みハッシュの自動実行ガード
        には掛からず、同じ中身でも必ず走る。"""
        if self._running_item is not None:
            return
        if not item.path.exists():
            self._set_note(
                "ファイルが無くなっているため、再実行できません", "error")
            return
        self._start_run(item, rerun=True)

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

    def _start_run(self, item: ScriptItem, auto: bool = False,
                   rerun: bool = False):
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
            item.last_result_text = None
            item.attach_names = []
            item.answered = False
            item.sent = False
            self._queue_check(item)
            self._refresh_list()
            self._diag(
                f"中身が変わっていたので実行せず再チェック: {item.name}")
            self._set_note(
                "中身が変わっていたので、チェックからやり直しました", "warn")
            return
        # 自動実行 (auto=True は _pump_auto_run からだけ) のときは、
        # チェックを通過して実行が始まるこの直前に、受領応答を
        # 先行して送る (1-51)。手動の実行では送らない。
        if auto and self._automation_flags()["auto_ack"]:
            self._send_ack(item)
        self._running_item = item
        self._running_rerun = rerun
        self._run_started_at = time.monotonic()
        self._diag(f"実行開始: {item.name}")
        # 実行手数を数えて、モニタ表示用の状態を出す。
        self._phal_step += 1
        self._publish_phal_status(
            "run", f"実行中: {item.name}",
            command_id=core.parse_command_id(item.name) or "",
            item=item.summary or item.name)
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
        self._refresh_status_strip()

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
        was_rerun = self._running_rerun
        self._running_rerun = False
        self._run_handle = None
        self._run_started_at = None
        self._last_run_seconds = run.seconds
        self._refresh_status_strip()
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
        if run.stopped or run.timed_out or run.policy_stop:
            reason = ("停止されました" if run.stopped else
                      "時間切れで止めました" if run.timed_out else
                      "実行ポリシーで止めました")
            self._publish_phal_status(
                "stopped", f"実行が止まりました: {run.name}",
                stopped_reason=reason)
        elif not self._automation_flags()["auto_answer"]:
            # 回答へ自動で進まない場合は、ここで待機に戻る。進む
            # 場合は _answer_to_ai が answer を出す。
            self._publish_phal_idle("実行が終わりました")
        # 長い出力なら全文をファイルへ置き、そのパスを結果文と
        # ループ記録の控えで使い回す (二重に保存しない。1.51)。
        command_id = core.parse_command_id(run.name)
        full_output_path = core.save_full_output_if_long(
            run, self.config, command_id)
        # 添付の指定 (1.58): 元の標準出力から「添付：ファイル名」
        # の行を取り出す。添付行は結果文 (要約を含む) に残さない
        # ので、組み立てには取り出し後の出力を持つ写しを使う。
        # 元の run はプレビューや記録用にそのまま残す。取り出した
        # 添付名は項目に残し、回答の段で outbox と照合する。
        clean_stdout, attach_names = core.extract_attach_lines(
            run.stdout)
        text_run = run
        if clean_stdout != run.stdout:
            text_run = dataclasses.replace(run, stdout=clean_stdout)
        if item is not None:
            item.attach_names = attach_names
        # 再実行のときは、差分だけを結果文にする。相手は開き直し
        # をまたぐ控え (phal_last_results.json) から取る。今回の
        # 結果は、次回の前回として控えに残す (通常の実行でも。
        # 直近1件がいつも前回になる)。「AIへ回答」で作り直す
        # ときにも同じ文を送るため、入れた文を項目にも残す。
        if was_rerun and item is not None:
            prev = self._last_results.get(self._rerun_key(item))
            text = core.build_rerun_result_text(
                run.name, text_run, self.config,
                command_id=command_id,
                previous_text=prev["text"] if prev else None,
                previous_truncated=prev.get("truncated", False)
                if prev else False,
                full_output_path=full_output_path)
        else:
            text = core.build_result_text(
                run.name, text_run, self.config,
                command_id=command_id,
                full_output_path=full_output_path)
        if item is not None:
            item.last_result_text = text
            body = core.build_result_body(
                run.name, text_run, self.config,
                command_id=command_id,
                full_output_path=full_output_path)
            core.last_results_put(
                self._last_results, self._rerun_key(item),
                core.result_diff_source(body))
            core.save_last_results(self._last_results)
        try:
            self._set_clipboard(text)
            copied = True
        except Exception:  # noqa: BLE001
            copied = False
        core.append_run_log(run, check)
        self._last_run = (item.key if item is not None else "", run)
        # ループ記録の控えに1手足す (1.51)。ねらいは表の概要。
        self._loop_records.append(core.LoopRecord(
            at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            command_id=command_id or "",
            name=run.name,
            summary=item.summary if item is not None else "",
            run=run,
            full_output_path=full_output_path or ""))
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
        # 受領応答の送信がまだ裏で動いていれば、入力欄の取り合いを
        # 避けて、回答は受領の完了側 (_on_ack_done) へ預ける。
        if item is not None and self._automation_flags()["auto_answer"]:
            if self._ack_sending:
                self._ack_pending_answer = item
            else:
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

        入れる文は、結果をクリップボードへ入れるときと全く同じ文
        (実行終了時に項目に残した last_result_text。再実行なら
        差分だけの文)。送るかどうかは人が決める (この道具の
        線引き)。入力欄への投入は裏方 (copilot_io の worker) 仕事で
        数秒かかることがあるので、EDDy と同じく別スレッドで回し、
        終わりはシグナルで受ける。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending or self._notice_sending:
            return
        run = item.last_run
        if run is None:
            return
        # 残っている文をそのまま使う (再実行の差分を落とさない
        # ため)。無いときだけ、従来どおり組み立てる (その場合も
        # 添付行は結果文に入れないので、取り出し後の出力で組む)。
        text = item.last_result_text
        if not text:
            clean_stdout, _names = core.extract_attach_lines(
                run.stdout)
            build_run = run
            if clean_stdout != run.stdout:
                build_run = dataclasses.replace(
                    run, stdout=clean_stdout)
            text = core.build_result_text(
                run.name, build_run, self.config,
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
        # 添付 (1.58): 実行の段で項目に残した添付名を、置き場
        # (outbox) と照合する。実在するものだけを回答に添えて
        # copilot 側へ渡す。無いものは添付せず、文章の末尾に
        # 1行足して伝える。
        attach_paths, missing_names = core.attach_file_paths(
            getattr(item, "attach_names", None))
        for name in missing_names:
            self._diag(f"添付の指定があったがファイルが無い: {name}")
        if missing_names:
            text += "".join(
                f"\n添付できませんでした：{name}"
                for name in missing_names)
        self._refresh_send_ready()
        sender = self.ai_sender or _default_ai_sender
        self._ai_filling = True
        self._ai_answer_item = item
        self._refresh_list()
        self._publish_phal_status(
            "answer", f"AI へ回答を入れています: {item.name}",
            command_id=core.parse_command_id(item.name) or "",
            item=item.summary or item.name)

        import threading

        def work():
            try:
                # 送信役の差し替え口には従来の2引数のものがある
                # (テストの fake 等) ので、添付があるときだけ
                # 3引数で呼ぶ。
                if attach_paths:
                    result = sender(text, profile, attach_paths)
                else:
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
        # 添付の結果 (1.58) も診断に残す (copilot 側で添付できた
        # もの・できなかったもの、ファイル名だけ)。
        for name in getattr(result, "attach_done", None) or []:
            self._diag(f"添付した: {name}")
        for name in getattr(result, "attach_failed", None) or []:
            self._diag(f"添付に失敗: {name}")
        self._refresh_list()
        # 自動化: 「送信を自動で押す」が入っていれば、入れた回答を
        # そのまま送信まで進める (手で入れた場合も同じ)。送信ボタン
        # の目印が無ければ進めない (その理由は上の note が既に
        # 伝えているので、ここで余計な失敗を重ねない)。
        if getattr(result, "ok", False) and item is not None \
                and self._ai_send_ready \
                and self._automation_flags()["auto_send"]:
            self._send_to_ai(item)
        elif getattr(result, "ok", False):
            # 送信へ進まない場合は、ここで待機に戻る。
            self._publish_phal_idle("AI の入力欄に結果を入れました")
        else:
            self._publish_phal_idle("AI への回答に失敗しました")
        # この項目の連鎖が一段落したら、自動実行の次を進める
        # (送信まで自動の場合は _on_ai_send_done でもう一度呼ぶ)。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する
        # (送信が裏で始まっていたら、済むまで持ち越しになる)。
        self._fire_deferred_restart()

    def _copy_intro_prompt(self):
        """導入プロンプトをクリップボードへ入れる (1.47)。

        環境は押した時点で集める。集められない項目は core 側が
        「不明」にするので、ここでは止まらない。AI の投げ先の
        設定は要らない (クリップボードへ入れるだけ。送信はしない)。"""
        try:
            env = core.collect_intro_environment(
                watch_folder=self.watch_folder,
                python_exe=self.config.get("python"))
        except Exception:  # noqa: BLE001  集められなくても文は作る
            env = {"watch_folder": str(self.watch_folder)}
        text = core.build_intro_prompt(env)
        try:
            self._set_clipboard(text)
        except Exception:  # noqa: BLE001
            self._set_note("クリップボードへ入れませんでした", "error")
            return
        self._diag("ループを始める導入プロンプトをコピーしました")
        self._set_note(
            "クリップボードに入れました。Copilot に貼ってください", "ok")

    def _copy_loop_record(self):
        """直近の実行履歴を Markdown にしてクリップボードへ入れる (1.51)。

        控えは起動後の実行だけ (開き直すと消える)。空のときは
        何も入れず、その旨だけ知らせる。送信はしない。"""
        if not self._loop_records:
            self._set_note("まだ記録がありません", "warn")
            return
        text = core.build_loop_record_markdown(
            self._loop_records, self.config)
        try:
            self._set_clipboard(text)
        except Exception:  # noqa: BLE001
            self._set_note("クリップボードへ入れませんでした", "error")
            return
        self._diag("ループ記録をコピーしました")
        self._set_note(
            "クリップボードに入れました。Copilot に貼ってください", "ok")

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
        self._publish_phal_status(
            "send", f"送信中: {item.name}",
            command_id=core.parse_command_id(item.name) or "",
            item=item.summary or item.name)

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
        if getattr(result, "ok", False):
            self._publish_phal_idle("送信しました")
        else:
            self._publish_phal_idle("送信に失敗しました")
        # 送信まで済んだので、自動実行の次を進める。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- AI へ受領応答 ----------------
    def _send_ack(self, item: ScriptItem):
        """自動実行の開始を、受領応答 (Ack) として先に送る (1-51)。

        結果の回答とは別に「受け取って実行を始める」という1行を、
        実行が始まる直前に fill + 送信まで一気にやる (裏方仕事)。
        送る役は回答・診断と同じ ai_sender / ai_presser。項目の
        answered / sent には触らない (受領は回答ではない)。診断の
        自動応答の消費控えにも触らない。

        送れなくても実行は止めない: 投げ先が未設定のときや、他の
        送信が途中のときは黙って見送る (知らせは補助的なもので、
        見送りまで診断に残すと記録が埋まってしまう)。実際に
        送ろうとして失敗したときだけ、完了側で診断に残す。"""
        if self._ack_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._notice_sending:
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  見送って実行は続ける
            return
        if not configured:
            return
        text = core.build_ack_text(
            core.parse_command_id(item.name) or "", item.name)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._ack_sending = True

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"受領を送れませんでした（{e}）")
            self._ack_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-ai-ack").start()

    def _on_ack_done(self, result):
        self._ack_sending = False
        if getattr(result, "ok", False):
            self._diag("受領応答を送りました")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"受領応答に失敗: {message}")
        # 回答が受領の完了待ちだったら、ここで進める (実行が
        # 受領より先に終わっていた場合の取りこぼしを防ぐ)。
        item, self._ack_pending_answer = self._ack_pending_answer, None
        if item is not None and item.last_run is not None:
            self._answer_to_ai(item)
        # 自動実行の待ち列があれば次を進める (回答の投入が裏で
        # 始まっていたら _pump_auto_run 側で見合わせる)。
        self._pump_auto_run()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    def _notify_blocked(self, item: ScriptItem):
        """チェックで実行不可になった新規ファイルを無言にしない (1-53)。

        自動実行 (auto_run) が入っているのにチェックで止まった
        ファイルは、受領も結果も出ず、放っておくとループが無言
        で止まる。切替 auto_ack (受領の切替、既定 入) に相乗り
        して、理由つきの1行をチャットへ出す。出すのは自動実行
        の対象 (起動後に届いた・変わった分) だけ、同じ中身
        (sha256) では1度だけ (控えは開き直しをまたぐ)。

        送信は受領 (Ack) と同じ独立系統の流儀: 投げ先が未設定の
        ときや、他の送信が途中のときは黙って見送り (その場合は
        控えに残さない)、実際に送り始めたら先に控えてから送る
        (送信の最中にチェックが来直しても二度出さない)。失敗
        しても他の処理は止めない。実行の連鎖・診断の控えには
        触らない。"""
        result = item.result
        if result is None or result.status != core.STATUS_ERROR:
            return
        flags = self._automation_flags()
        if not flags["auto_run"] or not flags["auto_ack"]:
            return
        sha = result.sha256 or ""
        if not sha or core.blocked_notified_contains(
                self._blocked_notified, sha):
            return
        if self._notice_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending:
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  見送って処理は続ける
            return
        if not configured:
            return
        reason = ""
        if result.issues:
            reason = str(result.issues[0].message or "")
        if not reason:
            reason = str(result.headline or "")
        text = core.build_blocked_text(
            core.parse_command_id(item.name) or "", item.name, reason)
        core.blocked_notified_put(self._blocked_notified, sha)
        core.save_blocked_notified(self._blocked_notified)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._notice_sending = True

        import threading

        def work():
            try:
                sent = sender(text, profile)
                if getattr(sent, "ok", False):
                    sent = presser(profile)
            except Exception as e:  # noqa: BLE001
                sent = _AiAnswerResult(
                    False, f"止まった知らせを送れませんでした（{e}）")
            self._notice_signals.done.emit(sent)

        threading.Thread(
            target=work, daemon=True, name="phal-ai-blocked").start()

    def _on_notice_done(self, result):
        self._notice_sending = False
        if getattr(result, "ok", False):
            self._diag("チェックで止まった知らせを送りました")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"チェックで止まった知らせに失敗: {message}")
        # 自動実行の待ち列があれば次を進める (受領の完了側と同じ)。
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
                    "auto_diag": False, "auto_ack": True,
                    "auto_reset": False}

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

    def _note_dl_scan(self, line: str):
        """直近の走査の1行を差し替え、変わったときだけ診断の
        控えにも残す (1-57)。

        走査は間隔を空けて何度も回るので、同じ内容を毎回残すと
        記録が流れてしまう。検出失敗・候補の抑制のような「黙ると
        切り分けられない」出来事は、状態が変わった時点で1回
        残れば足りる。"""
        full = f"直近の走査: {line}"
        if full == self._dl_last_scan:
            return
        self._dl_last_scan = full
        self._diag(full)

    def _dl_suppress_reason(self, holder):
        """DL 候補を特別行に出さない理由。出すなら None。

        判定そのものは従来どおり (押下済みの控え・一覧にあるか)。
        ここでは、その判定に診断で読める名前を付ける (1-57)。"""
        if holder.key in self._dl_consumed:
            return "消費済み"
        # 同じファイルが既に一覧にあるか (名前・概要での判定)。
        if not self._dl_holder_in_items(holder):
            return None
        # 開き直しをまたぐ押下済みの控えにあり、かつそのファイル
        # が一覧にあれば、もう落とした物として出さない。ファイル
        # が無くなっていれば (人が消した場合)、一覧にも無いの
        # でここには来ず、落とし直せるよう再び出るのが正しい。
        persisted = core.dl_consumed_get(
            self._dl_consumed_persisted, holder.name,
            holder.class_name)
        if persisted is not None:
            return "消費済み（控え）・一覧に既にある"
        return "一覧に既にある"

    def _dl_scan_line(self, chosen, suppressed, count):
        """走査の結果の1行 (診断用)。

        chosen は採用した候補 (無ければ None)、suppressed は
        [(候補, 理由)] (新しい順)、count は候補の総数。"""
        if chosen is not None:
            line = f"候補あり（{chosen.summary or chosen.label}）"
            if chosen.key in (self._dl_launch_keys or set()):
                line += "・起動時の既存分"
            if suppressed:
                line += f"（新しい側の{len(suppressed)}件は抑制）"
            return line
        if count:
            holder, reason = suppressed[0]
            return (f"候補なし（抑制{len(suppressed)}件: "
                    f"{holder.summary or holder.label} は{reason}）")
        return "候補なし"

    def _on_dl_checked(self, result):
        self._dl_checking = False
        if not getattr(result, "ok", False):
            # 失敗も形跡を残す (1-57)。行は出ないし、今の行も
            # そのまま。理由が変わらない間は1回だけ残る
            # (_note_dl_scan が変化だけを見るため)。
            message = " ".join(
                str(getattr(result, "message", "") or "").split())
            if len(message) > 80:
                message = message[:79] + "…"
            self._note_dl_scan(
                f"検出失敗（{message}）" if message else "検出失敗")
            return
        # 診断リクエストの目印は、候補の有無と関係なく同じ走査の
        # 結果に乗ってくる (1-43)。候補の処理より先に見る (候補が
        # 無い結果では、この先で return してしまうため)。
        self._maybe_auto_diag(getattr(result, "marker", None))
        # リセットの目印も同じ走査に乗ってくる (1-52)。診断の
        # 判定とは別に見る (控えも間隔も別)。
        self._maybe_auto_reset(
            getattr(result, "reset_marker", None))
        # 候補の並び (木の順) を作る。candidates (1-57) を持た
        # ない経路では、従来の candidate 1件だけの並びにする。
        holders = []
        seen_keys = set()
        raw = []
        candidates = getattr(result, "candidates", None)
        if isinstance(candidates, list):
            raw.extend(candidates)
        candidate = getattr(result, "candidate", None)
        if isinstance(candidate, dict) and candidate.get("name"):
            # candidate は candidates の末尾と一致するはずだが、
            # 含まれていなければ末尾に足す (互換の保険)。
            cand_key = _DlCandidate(candidate).key
            if not any(
                    isinstance(c, dict) and c.get("name")
                    and _DlCandidate(c).key == cand_key
                    for c in raw):
                raw.append(candidate)
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            holder = _DlCandidate(entry)
            if holder.key in seen_keys:
                continue
            seen_keys.add(holder.key)
            holders.append(holder)
        # 起動スナップショット (DL 候補版)。最初の検出で見えた
        # 候補は、起動した時点で既にあった分として控える。検出
        # に失敗した回 (ok=False) では取らない (上の return で
        # 戻っているため、ここへ来るのは成功した検出だけ)。候補
        # が無かった回は空の控えになり、その後に届いた候補は
        # 新しい分として扱う。
        if self._dl_launch_keys is None:
            self._dl_launch_keys = {h.key for h in holders}
        # 新しい順 (木の末尾側) から見て、消費済みでも一覧に
        # 既にあるものでもない、最初のものを採用する (1-57)。
        # 従来は末尾の1件に固定で、それが消費済みだと本命の
        # 新しいカードが浮上しなかった (1-76)。
        chosen = None
        suppressed = []
        for holder in reversed(holders):
            reason = self._dl_suppress_reason(holder)
            if reason is None:
                chosen = holder
                break
            suppressed.append((holder, reason))
        self._note_dl_scan(
            self._dl_scan_line(chosen, suppressed, len(holders)))
        if chosen is None:
            # 落とすものは無い (候補自体が無いか、全部が抑制)。
            # 特別行があれば下げる。
            if self._dl_holder is not None:
                if holders:
                    self._diag(
                        "DL行を下げた（押下済み・一覧にある候補だった）: "
                        f"{self._dl_holder.label}")
                else:
                    self._diag(
                        "DL行を下げた（候補が無くなった）: "
                        f"{self._dl_holder.label}")
                self._dl_holder = None
                self._refresh_list()
                self._publish_phal_idle("待機中")
            return
        holder = chosen
        if self._dl_holder is not None \
                and self._dl_holder.key == holder.key:
            return  # 同じ候補のまま。検出時刻もそのままにする
        self._dl_holder = holder
        self._diag(
            f"DL候補を表示: {holder.summary or holder.label}")
        self._refresh_list()
        self._publish_phal_status(
            "dl", f"新着: {holder.summary or holder.label}",
            command_id="", item=holder.summary or holder.label)
        # 自動化: 「DLを自動で押す」が入っていれば、新しく見つけた
        # 候補を1回だけ自動で押す。起動時に既にあった候補 (起動
        # スナップショットにある key) は対象外にする (設置の開き
        # 直しの直後に古い添付を拾い直して落とし直すのを防ぐ。
        # 特別行は出したままなので、手動では押せる)。失敗しても
        # 繰り返さない (理由は _on_dl_pressed が note に出す。
        # 次の新しい候補でまた試す)。
        if self._automation_flags()["auto_dl"] \
                and holder.key not in self._dl_auto_tried \
                and holder.key not in self._dl_launch_keys:
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
        # 裏方へは、窓側が選んだ候補そのもの (名前+クラス名) を
        # 渡す (1-57)。裏方は取り直した木からその候補を探して
        # 押すので、検出と押下の間に新しい発言が積まれても、
        # 別の候補を押し間違えない。
        press_key = {"name": holder.name,
                     "class_name": holder.class_name}
        self._diag(
            f"DL押下を開始（{'自動' if auto else '手動'}）: "
            f"{holder.summary or holder.label}")
        self._publish_phal_status(
            "dl", f"ダウンロード中: {holder.summary or holder.label}",
            command_id="", item=holder.summary or holder.label)
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
                result = presser(profile, press_key)
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
            self._publish_phal_status(
                "dl", "ダウンロードしました（到着待ち）")
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
            self._publish_phal_idle("ダウンロードに失敗しました")
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
        送信の連鎖が空いているときだけ。同じ発言かどうかは、全文
        の同定子 id か、目印を含む直近40行の第2の同定子 key の
        どちらか一致すれば消費済みとする (1-49。会話の木のずれで
        id が漂っても、key が同じなら二度送らない)。間隔と連鎖が
        理由の間は見送りにして、見張りの次の回で改めて判定する
        (目印が最新の発言に残っている限り、また届く)。"""
        mid = ""
        key = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
        if not mid:
            self._diag_marker_pending = None
            return
        consumed_id = str(self._diag_auto.get("consumed_id") or "")
        consumed_key = str(self._diag_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
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
        self._fire_auto_diag(mid, key)

    def _consume_diag_marker(self, mid, key=""):
        """目印を消費済みにして控えへ残す (開き直しをまたぐ)。

        送れたときだけでなく、見送り (書きかけ)・失敗のときも
        消費する。同じ発言への試みは1回だけに決めておかないと、
        見張りのたびに裏方を起こし続けることになる。呼び直しは
        新しい発言 (別の目印) でできる。id と key の両方を残す
        (1-49)。key が無ければ id だけが残る (従来の呼び出し)。"""
        if not mid:
            return
        self._diag_auto["consumed_id"] = mid
        if key:
            self._diag_auto["consumed_key"] = key
        core.save_diag_auto(self._diag_auto)

    def _fire_auto_diag(self, mid: str, key: str = ""):
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
            self._consume_diag_marker(mid, key)
            return
        if not configured:
            self._diag("診断の自動応答に失敗: AI の投げ先が未設定")
            self._consume_diag_marker(mid, key)
            self._set_note(
                "診断の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        bundle = self._diagnostics_text()
        if not bundle:
            self._diag("診断の自動応答に失敗: 束が作れませんでした")
            self._consume_diag_marker(mid, key)
            return
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._diag_sending = True
        # 完了シグナル側へ渡す (mid, key) の組。完了時に両方を
        # 控えへ残す (1-49)。
        self._diag_auto_mid = (mid, key)
        self._diag("診断の自動応答を開始")
        # 診断の束の貼り付けも、モニタ上は回答の段に入れる。
        self._publish_phal_status("answer", "診断を自動で送っています")

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
        pair, self._diag_auto_mid = self._diag_auto_mid, None
        # 旧い呼び出し (mid だけ) からの完了にも耐える。
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._diag_auto["last_sent_at"] = time.time()
            self._consume_diag_marker(mid, key)
            self._diag("診断を自動で送りました")
            self._set_note("診断を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            # 入力欄に書きかけがあった。消さずに見送る (この道具の
            # 線引き)。理由は診断に残し、目印は消費する。
            self._consume_diag_marker(mid, key)
            self._diag(
                f"診断の自動応答を見送り（入力欄に書きかけ）: {message}")
            self._set_note(
                "診断の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        else:
            self._consume_diag_marker(mid, key)
            self._diag(f"診断の自動応答に失敗: {message}")
            self._set_note(
                f"診断を自動で送れませんでした（{message}）", "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("診断を自動で送りました")
        else:
            self._publish_phal_idle("診断の自動応答が終わりました")
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- リセットの合図 (1-52) ----------------
    def _note_reset_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に残す
        (診断の自動応答の _note_diag_once と同じ作法。控えは混ぜ
        ない)。"""
        if len(self._reset_noted) > 50:
            self._reset_noted.clear()
        stamp = (mid, key)
        if stamp in self._reset_noted:
            return
        self._reset_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_reset(self, marker):
        """見張りの結果に乗ってきたリセットの目印を判定する。

        反応するのは、切替「合図で自動で開き直す」が入っていて、
        同じ発言を消費済みでなく、直近の自動リセットから10分
        以上空いているときだけ。同じ発言かどうかは診断の目印と
        同じ同定 (id か key のどちらか一致で消費済み) で、控えは
        リセット専用 (core の reset_auto) に持つ。間隔が理由の間
        は見送りにして、見張りの次の回で改めて判定する (目印が
        最新の発言に残っている限り、また届く)。

        開き直しそのものは request_restart に任せる。実行や連鎖
        の途中なら、そちらが連鎖の終了まで保留するので、ここでは
        割り込まない。"""
        mid = ""
        key = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
        if not mid:
            self._reset_marker_pending = None
            return
        consumed_id = str(self._reset_auto.get("consumed_id") or "")
        consumed_key = str(self._reset_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._reset_marker_pending = None
            return  # 消費済みの発言。二度は反応しない
        if not self._automation_flags()["auto_reset"]:
            self._note_reset_once(
                mid, "off",
                "リセットの合図を検出（自動で開き直すは切のため"
                "見送り）")
            return
        last = float(self._reset_auto.get("last_reset_at") or 0.0)
        if time.time() - last < core.RESET_AUTO_COOLDOWN_SEC:
            self._reset_marker_pending = mid
            self._note_reset_once(
                mid, "cooldown",
                "リセットの合図: 直近に開き直したばかりのため"
                "見送り（間隔が空いてから開き直します）")
            return
        self._reset_marker_pending = None
        self._consume_reset_marker(mid, key)
        self._diag("リセットの合図を受けて開き直します")
        self.request_restart()

    def _consume_reset_marker(self, mid, key=""):
        """目印を消費済みにし、開き直した時刻と一緒に控えへ残す
        (開き直しをまたぐ)。消費と時刻を同時に残すのは、開き直し
        た直後に同じ目印を再び拾っても、消費と間隔の両方で止まる
        ようにするため。"""
        if not mid:
            return
        self._reset_auto["consumed_id"] = mid
        if key:
            self._reset_auto["consumed_key"] = key
        self._reset_auto["last_reset_at"] = time.time()
        core.save_reset_auto(self._reset_auto)

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

        実行中・回答の投入中・送信中・受領応答の送信中・止まった
        知らせの送信中はもちろん途中。自動実行の待ち列に残りが
        ある場合も、切替が入っている間は途中とみなす (切替が
        切れた待ち列は進まないので、途中には数えない。詰まった
        まま開き直しが永遠に来ない事態を避ける)。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending or self._notice_sending:
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
