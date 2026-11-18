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
PHAL_VERSION = "2.80"

# 入力・送信方式3 (2.61) で「大部分が一致」とみなす一致率の
# 下限。行単位で比べた比率がこれ以上なら、送信時の確認が
# 合わなくても送信する。
SEND_FORCE_RATIO = 0.9

# 受領応答は、実行がこの秒数を超えても続いているときだけ出す
# (2.66)。速く終わる実行では受領を出さず、結果だけが行く。
# 既定値は core.ACK_DELAY_DEFAULT_SEC と一致させること (2.80)。
ACK_DELAY_SECONDS = 5.0

# 段階が落ちたあと、表示だけ直前の段階を保つ秒数 (1.67)。
# DL 完了から実行開始までのような段階の合間の一瞬に、
# 待機 (監視) が割り込むちらつきを防ぐ。
STAGE_EXIT_GRACE_SEC = 2.0

import argparse
import ctypes
import dataclasses
import math
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QEvent, QObject, QSignalBlocker, Qt, QTimer, Signal)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProxyStyle,
    QPushButton,
    QSizePolicy,
    QStackedLayout,
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

# 変化の署名と追いかけモード (2.07)。通常時は、本走査の前に
# 署名だけを見て、変わっていなければ本走査を打ち切る。変化の
# 気配がある間は「追いかけモード」で本走査を短い間隔で回す。
# 署名がすり抜ける書き換えに備え、本走査は最大でもこの間隔で
# 必ず挟む。
SIGNATURE_INTERVAL_SEC = 5.0
CHASE_INTERVAL_SEC = 1.5
CHASE_WINDOW_SEC = 30.0
FULL_SCAN_MAX_GAP_SEC = 60.0

# DL 候補の名前 (中身の冒頭) から、ファイル名らしき部分を取る
# ための目印。「既に一覧にあるファイルか」の判定に使う。
_DL_FILENAME_RE = re.compile(
    r"[\w.\-]+\.(?:phal|py|ps1)", re.IGNORECASE)

# 概要の先頭にあるコマンドIDの接頭辞 (「1-29: 」「1-29 」の形)。
# 候補側の概要 (カードのコメントそのまま) には残り、ファイル側
# の概要 (core.extract_summary) では外されているので、照合の
# 前に両方から外してそろえる。コロンはあっても無くてもよい
# (2.25。コロン無しの形で照合が外れ続け、DL 済みの候補が
# 開き直しのたびに出直した実機報告への直し)。
_SUMMARY_ID_PREFIX_RE = re.compile(
    r"^[A-Za-z]?\d{1,3}-\d{1,3}\s*[:：]?\s*")


def _holder_command_id(holder) -> str:
    """DL 候補のコマンドID (「1-293」の形)。無ければ ""。

    概要の先頭から取るのが本命。取れなければ、名前 (中身の
    冒頭) の先頭数行のコメント行から取る。"""
    summary = " ".join((holder.summary or "").split())
    match = re.match(r"([A-Za-z]?\d{1,3}-\d{1,3})\b", summary)
    if match is not None:
        return match.group(1)
    for line in (holder.name or "").splitlines()[:4]:
        match = re.match(r"\s*#\s*([A-Za-z]?\d{1,3}-\d{1,3})\b", line)
        if match is not None:
            return match.group(1)
    return ""


def _summary_matches(candidate_summary, item_summary) -> bool:
    """DL 候補の概要と、ファイルの概要 (先頭コメント) が同じ物か。

    空白を畳み、先頭のコマンドID接頭辞を外して比べる。候補側の
    概要はカード名から取る都合で60文字で切れていることがある
    (copilot_loop._download_summary)。切り捨てられたらしい長さ
    (60文字) のときは、残りが一致する前方一致でも同じ物とみなす
    (接頭辞を外す前の長さで判断する)。どちらかが空なら不一致
    (概要の無い物同士を「同じ」と言わない)。

    逆に候補側が項目より長い形もある (2.50)。カード文が1行に
    潰れる窓では、候補の概要に続くコードの頭まで入って項目の
    概要より長くなる。その形は、項目側が文として特定できる
    長さ (16文字以上) で候補の接頭辞になっているときだけ
    同じ物とみなす。"""
    raw = " ".join((candidate_summary or "").split())
    cand = _SUMMARY_ID_PREFIX_RE.sub("", raw)
    item = _SUMMARY_ID_PREFIX_RE.sub(
        "", " ".join((item_summary or "").split()))
    if not cand or not item:
        return False
    if cand == item:
        return True
    if len(raw) >= 60 and item.startswith(cand):
        return True
    return len(item) >= 16 and cand.startswith(item)

# 一覧(表)の列。1スクリプト1行。時刻が一番左で、概要が幅を吸う。
COL_TIME = 0
COL_ID = 1
COL_NAME = 2
COL_AI = 3
COL_HASH = 4
COL_SUMMARY = 5
COL_STATUS = 6
COL_RUN = 7
SUMMARY_COLUMN_MIN_WIDTH = 80  # 概要列の下限 [px] (幅の配分用、2.75)

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
# dialog_error は「設定」の状態ラベルのエラー色 (従来は
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
        "detect_bg": "#d7f0e4",
        "selection_tint": (0, 120, 215, 70),
        "chrome": {
            "window": "#f2f3f5",
            "text": "#1d2126",
            "base": "#ffffff",
            "alternate_base": "#eceff2",
            "button": "#ffffff",
            "button_hover": "#e3ecf7",
            "button_pressed": "#d3dce6",
            "border": "#b9bfc8",
            "border_hover": "#7f8894",
            "header": "#e3e6ea",
            "disabled_text": "#9aa1a9",
        },
    },
    "dark": {
        "status": STATUS_COLORS_DARK,
        "duplicate": DUPLICATE_COLOR_DARK,
        "note_styles": NOTE_STYLES_DARK,
        "muted": "#9aa5a0",
        "link": "#58a6ff",
        "dialog_error": "#ff7b72",
        "accent_style": "background: #1f7a63; color: #ffffff;",
        "detect_bg": "#17402f",
        "selection_tint": (88, 166, 255, 90),
        "chrome": {
            "window": "#2a2d33",
            "text": "#e6e8ea",
            "base": "#1f2226",
            "alternate_base": "#262a30",
            "button": "#343943",
            "button_hover": "#454e5e",
            "button_pressed": "#2b3038",
            "border": "#4a5058",
            "border_hover": "#7d8590",
            "header": "#31353d",
            "disabled_text": "#7d8590",
        },
    },
}


def _chrome_palette(chrome: dict) -> QPalette:
    """テーマの地の色からパレットを作る。窓にだけ付ける。"""
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


def _chrome_stylesheet(chrome: dict) -> str:
    """テーマの地のスタイル。窓自身に付けるので、この窓と、
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
        " border: none;"
        f" border-right: 1px solid {chrome['border']};"
        " padding: 2px 4px; }"
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
        "QPushButton:hover {"
        f" background-color: {chrome['button_hover']};"
        f" border-color: {chrome['border_hover']}; }}"
        "QPushButton:pressed {"
        f" background-color: {chrome['button_pressed']}; }}"
        "QPushButton:disabled {"
        f" color: {chrome['disabled_text']}; }}"
        "QLineEdit, QComboBox {"
        f" background-color: {chrome['base']};"
        f" border: 1px solid {chrome['border']};"
        " padding: 1px 4px; }"
        # 設定ウィンドウのタブ (2.02)。素の描画のままだと、
        # ダークでタブの帯が白く残り、字も沈んで読めなかった
        # (実機のスクリーンショットで確認)。
        "QTabBar {"
        f" background-color: {chrome['window']}; }}"
        "QTabWidget::pane {"
        f" background-color: {chrome['window']};"
        f" border: 1px solid {chrome['border']}; }}"
        "QTabBar::tab {"
        f" background-color: {chrome['header']};"
        f" color: {chrome['text']};"
        f" border: 1px solid {chrome['border']};"
        " border-bottom: none; padding: 4px 10px; }"
        "QTabBar::tab:selected {"
        f" background-color: {chrome['window']}; }}"
        "QSpinBox {"
        f" background-color: {chrome['base']};"
        f" border: 1px solid {chrome['border']};"
        " padding: 1px 4px; }"
        "QSpinBox::up-button, QSpinBox::down-button {"
        f" background-color: {chrome['button']};"
        f" border-left: 1px solid {chrome['border']};"
        " width: 14px; }"
        "QSpinBox::up-button:hover, QSpinBox::down-button:hover {"
        f" background-color: {chrome['button_hover']}; }}"
        "QGroupBox {"
        f" border: 1px solid {chrome['border']};"
        " border-radius: 3px; margin-top: 8px;"
        " padding-top: 4px; }"
        "QGroupBox::title {"
        " subcontrol-origin: margin; left: 6px;"
        " padding: 0 2px; }"
    )


# ---------------------------------------------------------------
# おどうぐばことの連携 (2.37)。odougubako_core は tray-tools の
# 他の部分に依存しない独立モジュールで、ここでは実行時にだけ
# 読み込む (古い設置にファイルが無くても PHAL 本体は動く)。
# ---------------------------------------------------------------
def _odougubako_find(name):
    """おどうぐばこに道具があれば、そのフォルダを返す。"""
    try:
        import odougubako_core
        return odougubako_core.find_tool(name)
    except Exception:  # noqa: BLE001  道具箱が無い・読めない
        return None


def _odougubako_run_result(name, args, timeout_seconds):
    """おどうぐばこの道具を実行し、結果を RunResult にする。

    頻度 (回数・成功失敗・所要時間) の記録は odougubako_core
    側で済んでいる。こちらの RunResult は結果文の組み立て用。"""
    import odougubako_core
    result = odougubako_core.run_tool(
        name, list(args), timeout=float(timeout_seconds or 120))
    run = core.RunResult(name=name)
    run.exit_code = result.get("exit_code")
    run.seconds = float(result.get("seconds") or 0.0)
    run.stdout = result.get("stdout") or ""
    run.stderr = result.get("stderr") or ""
    reason = result.get("reason") or ""
    if reason and run.exit_code is None:
        if "タイムアウト" in reason:
            run.timed_out = True
        run.start_error = reason
    return run


_EVAL_SCORE_LABELS = ("使いやすさ", "選びやすさ", "期待どおりか")
_EVAL_SCORE_KEYS = ("usability", "findability", "expectation")


def _eval_reply_text(payload, request_id="") -> str:
    """道具評価の payload を解釈して記録し、受け付けの文を返す。

    形は「道具名 点 点 点 [メモ]」。点は _EVAL_SCORE_LABELS の
    順で 1〜5 (足りない分は記録しない)。道具が無い・点が1つも
    読めないときは、その旨の文を返す (記録はしない)。
    request_id は合図の依頼 ID (2.41、見出しに入れる)。"""
    mark = f"[PHAL:道具評価 {request_id}]" if request_id \
        else "[PHAL:道具評価]"
    tokens = str(payload or "").split()
    if not tokens:
        return f"{mark} 道具名がありません"
    name = tokens[0]
    if _odougubako_find(name) is None:
        return (f"{mark} 道具「{name}」は"
                "おどうぐばこにありません")
    scores = {}
    used = 0
    for token in tokens[1:]:
        if used >= len(_EVAL_SCORE_KEYS):
            break
        try:
            value = int(token)
        except ValueError:
            break
        if not 1 <= value <= 5:
            break
        scores[_EVAL_SCORE_KEYS[used]] = value
        used += 1
    if not scores:
        return (f"{mark} 点数が読み取れませんでした。"
                "道具名の後に、使いやすさ・選びやすさ・期待どおりか"
                "の順で 1〜5 の数字を並べてください")
    memo = " ".join(tokens[1 + used:])
    try:
        import odougubako_core
        recorded = odougubako_core.record_eval(name, scores, memo=memo)
    except Exception as e:  # noqa: BLE001
        return f"{mark} 記録できませんでした（{e}）"
    if not recorded.get("ok"):
        return (f"{mark} 記録できませんでした: "
                f"{recorded.get('reason') or '理由不明'}")
    parts = " ".join(
        f"{label}{scores[key]}"
        for key, label in zip(_EVAL_SCORE_KEYS, _EVAL_SCORE_LABELS)
        if key in scores)
    text = f"{mark} {name} の評価を記録しました（{parts}）"
    if memo:
        text += f"\nメモ: {memo}"
    return text


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
    candidates / rerun_marker)。検出役が例外を投げたときの
    受け皿にも使う。
    marker は診断リクエストの目印 {"id": 同定子} (1-43)、
    reset_marker はリセットの目印 (1-52) で、形は marker と同じ。
    rerun_marker は再実行の目印 (1.63) で、形は marker に
    "command_id" を足したもの。fulltext_marker は全文リクエスト
    の目印 (1.91) で、形は rerun_marker と同じ。history_marker
    は実行一覧の目印 (1.92) で、形は marker と同じ。stop_marker
    は停止の目印 (1.93) で、形は marker と同じ。log_marker は
    動作ログリクエストの目印 (1.96) で、形は marker に
    "params" (指定の文字列) を足したもの。capture_marker は
    キャプチャリクエストの目印 (2.54) で、形は marker に
    "target" (撮る対象の語) を足したもの。
    candidates はスクリプトらしいカード全件の辞書 (木の順、
    1-57) で、持たない経路では None。"""

    def __init__(self, ok, message, candidate=None, marker=None,
                 reset_marker=None, candidates=None,
                 rerun_marker=None, resend_marker=None,
                 settings_marker=None, manual_marker=None,
                 file_marker=None, fulltext_marker=None,
                 capture_marker=None,
                 history_marker=None, stop_marker=None,
                 log_marker=None, relay_marker=None,
                 input_marker=None, overwrite_marker=None,
                 force_marker=None,
                 kind="scan", signature=None,
                 watch=""):
        self.ok = ok
        self.message = message
        # watch は、この結果がどの窓 (プロファイル名) の見張りか
        # (2.08)。検出役の結果にも窓側で付ける。
        self.watch = watch
        # kind は "scan" (本走査) か "signature" (変化の署名だけ、
        # 2.07)。signature は署名の並び (取れなければ None)。
        self.kind = kind
        self.signature = signature
        self.candidate = candidate
        self.marker = marker
        self.reset_marker = reset_marker
        self.candidates = candidates
        self.rerun_marker = rerun_marker
        self.resend_marker = resend_marker
        self.settings_marker = settings_marker
        self.manual_marker = manual_marker
        self.file_marker = file_marker
        self.fulltext_marker = fulltext_marker
        self.capture_marker = capture_marker
        self.history_marker = history_marker
        self.stop_marker = stop_marker
        self.log_marker = log_marker
        self.relay_marker = relay_marker
        # 入力欄まわりの目印 (2.39)。input_marker は marker に
        # "request_id" と "target" を足したもの、overwrite_marker
        # と force_marker はさらに "body" を足したもの。
        self.input_marker = input_marker
        self.overwrite_marker = overwrite_marker
        self.force_marker = force_marker


# カード目撃の控えの寿命 (秒、2.38)。この間にフォルダへ届いた
# ファイルは、そのカードの窓の分として扱う。
_CARD_SEEN_TTL_SEC = 1800


def _card_file_name(candidate_name: str) -> str:
    """候補の名前からファイル名を取り出す (2.38)。

    カード型の候補の名前は「ファイル名＋概要」の形がありうる
    ので、先頭の1語がスクリプトの拡張子で終わるときだけ
    ファイル名として返す。リンク型 (Copilot アプリ) の名前は
    ファイル名そのもの。"""
    words = (candidate_name or "").split()
    if not words:
        return ""
    first = words[0]
    if first.lower().endswith((".phal", ".py", ".ps1")):
        return first
    return ""


def _file_head_fingerprint(path) -> str:
    """届いたファイルの先頭2行の指紋 (2.39)。

    カード型の候補 (Muse) は名前がファイル名ではなく中身の
    冒頭なので、ファイル名では押下と到着を結び付けられない。
    先頭2行 (shebang と概要コメント) はカード側にもファイル側
    にも同じ形で現れるので、その指紋で結ぶ。読めなければ ""。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(8192)
    except OSError:
        return ""
    return core.head_fingerprint(
        raw.decode("utf-8-sig", errors="replace"))


def _dl_name_matches(expected: str, arrived: str) -> bool:
    """DL 押下で期待した名前と、実際に届いたファイル名の照合。

    候補の名前は「ファイル名＋概要」の形がありうるので、文の
    各語をファイル名として比べる。ブラウザが同名の重複に
    付ける「 (1)」のような連番は、届いた側から外して比べる。

    カード型の候補 (Muse) は名前が「中身の冒頭＋ファイル名
    ＋種別」の形で、ファイル名が先頭に来ない (2.52)。先頭の
    1語だけでは結べず、概要の無い通常ファイル (先頭コメント
    の無いテキスト等) は指紋でも概要でも結べないため、文の
    中のどの語かでも一致すれば同じ物とみなす。"""
    words = (expected or "").split()
    if not words or not arrived:
        return False
    return any(
        _one_dl_name_matches(word, arrived) for word in words)


def _one_dl_name_matches(exp_name: str, arrived: str) -> bool:
    """期待した名前1語と、届いたファイル名の照合 (2.52)。"""
    if not exp_name or not arrived:
        return False
    if exp_name == arrived:
        return True
    stem, dot, ext = arrived.rpartition(".")
    if not dot:
        return False
    base = re.sub(r" \(\d+\)$", "", stem)
    return bool(base) and f"{base}.{ext}" == exp_name


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
        # watch は、この候補を見つけた窓のプロファイル名 (2.08)。
        # 特別行に出すときに窓側が付ける。
        self.watch = ""

    @property
    def key(self):
        return f"{self.name}\n{self.class_name}"


class _WatchState:
    """見張り1窓分の控え (2.08、二重待受)。

    変化の署名・追いかけ・候補 (特別行)・消費済みの控えなど、
    チャットの見張りの状態は窓ごとに持つ。name はプロファイル
    名 (ファイルの出どころの記録と同じ名前)。profile は
    copilot_io へ渡す辞書で、設定を読み直すたびに差し替える
    (未設定なら None で、その窓は見張らない)。"""

    def __init__(self, name):
        self.name = name
        self.profile = None
        self.holder = None          # _DlCandidate | None (特別行)
        self.consumed = set()       # 押下済みの候補の key の控え
        self.auto_tried = set()     # 自動で押してみた候補の key
        # 起動時に既にあった DL 候補の key の控え。最初の検出で
        # 取り、ここにある候補は自動ダウンロードの対象外にする
        # (開き直しの直後に古い添付を拾い直すのを防ぐ)。
        self.launch_keys = None
        self.last_candidate_key = None
        self.session = {}           # 開き直し引き継ぎ (core の控え)
        self.checking = False
        self.last_check = 0.0
        self.sig_last_check = 0.0
        self.last_full_scan = 0.0
        self.signature = None
        self.chase_until = 0.0
        # 直近の走査の1行 (診断の束に出す、1-57)。
        self.last_scan = "直近の走査: まだ無い"
        # この窓の見張りが「何も無い」状態に入った時刻
        # (monotonic、2.23)。階のカウントアップの起点で、
        # 候補が浮かぶと None へ戻る。
        self.idle_since = None


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


def _default_ai_sender(text, profile, attachments=None,
                       image_paste=False, lenient=False,
                       direct_check=False):
    """AI の入力欄へ入れるだけの送信役 (送信はしない)。

    copilot_io.fill_text を使う (EDDy のテスト入力と同じ経路)。
    profile は PHAL 自前の投げ先 (script_runner_ai.build_profile が
    作る辞書) をそのまま渡す。import は遅らせる: copilot_io が壊れても
    PHAL の窓は開くように (投げ先の解決も同じ理由で遅延 import)。
    attachments は、outbox に実在することが確かめられた成果
    ファイルのフルパス (1.58)。copilot 側が、入力欄へ入れる前に
    添付する。image_paste (2.55) は、クリップボードに置いてある
    画像を入れる前に貼る (キャプチャの配達)。lenient (2.59、
    入力・送信方式2) は、書き込みの成否を読み直しの一致で
    判定しない。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile,
                                attachments=attachments,
                                image_paste=image_paste,
                                lenient=lenient,
                                direct_check=direct_check)


def _default_ai_presser(profile, expected=None, force_threshold=None,
                        early_force=False):
    """AI の送信ボタンを押す役。本当に送信する。

    expected (2.59、入力・送信方式2) を渡したときは、入力欄の
    文がその文と合うときだけ押す (copilot_io.press_send_button
    の文の確認つき)。合わなければ送らずに失敗で返る。
    force_threshold (2.61、方式3) を渡すと、合わなくても
    一致率がその値以上なら押す。

    従来の形では、押す前に、入力欄を読み直す (送信の直前の確認):
      - 読めなければ、その段の理由を失敗として返す (押さない)
      - 空なら「入力欄が空です」で断る (入れたはずの回答が消えて
        いたら、空のまま送ってしまう事故になるため)
    読めて文があれば、プロファイルの目印の送信ボタンを Invoke で
    押す (copilot_io.press_send_button)。戻り値は copilot_io の
    SendResult と同じ顔 (ok / message)。"""
    import copilot_io

    if expected:
        return copilot_io.press_send_button(
            profile=profile, expected=expected,
            force_threshold=force_threshold,
            early_force=early_force)
    read = copilot_io.read_input_text(profile=profile)
    if not read.ok:
        return read
    if not (read.text or "").strip():
        return _AiAnswerResult(False, "入力欄が空です")
    return copilot_io.press_send_button(profile=profile)


def _default_ai_input_reader(profile):
    """対象の窓の入力欄を読む役 (2.39)。

    copilot_io.read_input_text を使う (裏方経由)。戻り値は
    copilot_io.ReadResult と同じ顔 (ok / message / text)。"""
    import copilot_io

    return copilot_io.read_input_text(profile=profile)


def _default_ai_force_sender(text, profile):
    """対象の窓の入力欄へ強制的に入れる役 (2.39、送信はしない)。

    copilot_io.fill_text の force 経路を使う: 書きかけがあって
    も上書きし、通常の書き込み (値の設定・貼り付け) が反映
    されなければ打鍵まで使う。戻り値は SendResult と同じ顔。"""
    import copilot_io

    return copilot_io.fill_text(text, profile=profile, force=True)


def _default_ai_downloader(profile):
    """チャットの最新発言から、ダウンロード候補を検出する役。

    copilot_io.latest_download を使う (裏方経由)。戻り値は
    DownloadResult と同じ顔 (ok / message / candidate)。"""
    import copilot_io

    return copilot_io.latest_download(profile=profile)


def _default_ai_signer(profile):
    """チャットの変化の署名を取る役 (2.07)。

    copilot_io.chat_signature を使う (裏方経由)。取れなければ
    None で、窓側は本走査に倒れる。"""
    import copilot_io

    return copilot_io.chat_signature(profile=profile)


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


# キャプチャの対象の語に混ざりうる、画面では見えない書式文字を
# 照合の前に取り除く (2.55、2.56 で区分 Cf 全体へ拡張)。


def _normalize_capture_word(text):
    """キャプチャの対象の語を照合用に整える (2.55、2.56 で拡張)。

    窓の走査で取れた語には、画面表示では見えない書式文字が
    混ざることがある (2.54・2.55 の実地で、対象が Muse の
    はずが合わずに撮れなかった)。前後の空白と、Unicode の
    書式文字 (区分 Cf。ゼロ幅の空白、左右方向の印 U+200E /
    U+200F、タグ文字など) と異体字セレクタを取り除いてから
    照合する。"""
    import unicodedata

    word = (text or "").strip()
    kept = []
    for ch in word:
        code = ord(ch)
        if unicodedata.category(ch) == "Cf":
            continue
        if 0xFE00 <= code <= 0xFE0F or code == 0x034F:
            continue
        kept.append(ch)
    return "".join(kept).strip()


def _image_looks_blank(image):
    """単色にしか見えない画像か (2.54)。

    PrintWindow で描画が取れていない窓は、真っ黒か真っ白の
    単色になる。格子状に画素を拾って、全部同じ色なら単色と
    みなす (本当に単色の画面を取り違えることはあるが、その
    ときは予備の撮り方でも同じ絵になるので実害は無い)。"""
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return True
    step_x = max(1, width // 32)
    step_y = max(1, height // 32)
    first = None
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            color = image.pixelColor(x, y).rgba()
            if first is None:
                first = color
            elif color != first:
                return False
    return True


class _BitmapInfoHeader(ctypes.Structure):
    """GetDIBits 用の BITMAPINFOHEADER (2.54)。"""

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _dib_to_qimage(gdi32, hdc, bitmap, width, height):
    """互換ビットマップの中身を QImage に移す (2.54)。

    上下が逆にならないよう、頭でっかち (biHeight を負) で
    取り出す。アルファは窓によって 0 埋めのことがあり、その
    まま PNG にすると透明になってしまうので、不透明な
    RGB32 に直してから返す。"""
    header = _BitmapInfoHeader()
    header.biSize = ctypes.sizeof(_BitmapInfoHeader)
    header.biWidth = width
    header.biHeight = -height
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0  # BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    lines = gdi32.GetDIBits(
        hdc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
    if not lines:
        return None
    image = QImage(
        bytes(buffer), width, height, width * 4,
        QImage.Format_ARGB32)
    return image.convertToFormat(QImage.Format_RGB32)


def _capture_window_image(hwnd):
    """窓の画像を取る (2.54、キャプチャリクエスト用)。

    裏にあっても撮れる PrintWindow (PW_RENDERFULLCONTENT) を
    本命にする。結果が単色にしかならない (描画が取れて
    いない) ときだけ、窓を前面に出して画面から切り取る
    (前面化は作業の邪魔になるので予備に限る)。戻り値は
    (QImage | None, 取れなかった理由の文)。"""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [
        wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        wintypes.UINT, ctypes.c_void_p,
        ctypes.POINTER(_BitmapInfoHeader), wintypes.UINT]
    gdi32.GetDIBits.restype = ctypes.c_int

    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(
            hwnd, ctypes.byref(rect)):
        return None, "窓の位置が分かりません"
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None, "窓の大きさが取れません"
    image = None
    hdc = user32.GetWindowDC(hwnd)
    if hdc:
        mem_dc = None
        bitmap = None
        try:
            mem_dc = gdi32.CreateCompatibleDC(hdc)
            if mem_dc:
                bitmap = gdi32.CreateCompatibleBitmap(
                    hdc, width, height)
            if mem_dc and bitmap:
                old = gdi32.SelectObject(mem_dc, bitmap)
                try:
                    if user32.PrintWindow(hwnd, mem_dc, 2):
                        image = _dib_to_qimage(
                            gdi32, mem_dc, bitmap, width, height)
                finally:
                    if old:
                        gdi32.SelectObject(mem_dc, old)
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(hwnd, hdc)
    if image is not None and not _image_looks_blank(image):
        return image, ""
    # 予備: 前面に出して画面から切り取る。
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        from PySide6.QtCore import QRect

        import capture_grab

        grabbed = capture_grab.grab_region(
            QRect(rect.left, rect.top, width, height))
        if grabbed is not None and not grabbed.isNull():
            return grabbed, ""
    except Exception:  # noqa: BLE001  予備が失敗しても本命の絵は返す
        pass
    if image is not None:
        return image, ""
    return None, "窓の描画を取得できませんでした"


class ScriptItem:
    """一覧の1行分の状態。"""

    def __init__(self, path: Path, mtime: float, size: int):
        self.path = path
        self.key = str(path)
        self.name = path.name
        self.mtime = mtime
        self.size = size
        self.status = "checking"
        self.ai = ""                # 出どころの AI 名 (2.04、無ければ空)
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
        # 持ち主が「飛ばす」で見送った印 (2.76)。立ったら、この
        # 到着は自動実行の入口・待ち列のどちらでも相手にしない。
        # 手動の実行はこの印に関係なく常にできる。控え (core の
        # skipped、名前と sha256 の組) と行き来して、開き直しを
        # またいで残る。
        self.skipped = False

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
        # 保存値は枠 (フレーム) の座標・大きさ (1.68)。ここでの
        # move/resize は表示前の仮置きで、枠が実在する showEvent
        # 以降に保存値どおりに置き直す (ずらさないための置き直し)。
        self._saved_frame_rect = geometry

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
        # 見張りの状態は窓ごと (_WatchState、2.08 の二重待受)。
        # _watch_list は処理・表示の順 (見張る窓の並び)、
        # _watches は名前引き。代表の状態は最初から1つ用意し、
        # 設定との同期 (_sync_watch_states) で名前と投げ先を
        # 差し替える。
        self._primary_state = _WatchState("")
        self._watches = {}
        self._watch_list = [self._primary_state]
        # 合図の処理中だけ、その出どころの窓を指す (投げ先の
        # 解決 _resolve_ai_target が見る、2.08)。
        self._dispatch_state = None
        # 合図の判定中だけ、その合図の依頼 ID を指す (動作ログ
        # の rid に使う、2.40。_dispatch_signal が立て下げする)。
        self._diag_ctx_rid = ""
        # 押下中の候補がどの窓の分か (到着の出どころ付けに使う)。
        self._dl_press_watch = None
        # 一覧の上限からあふれて外したファイルの控え (2.46)。
        # {key: (mtime, size)}。外した事実を覚えておき、同じ
        # 中身のまま候補に戻ってきても新規扱いしない (チェック
        # 結果の再掲が数秒ごとに繰り返されるのを止める)。
        self._overflow_seen = {}
        # 押下済み候補の控え (開き直しをまたいで残る)。会話内の
        # 控えは開き直しで消えるので、core の控えファイルから
        # 読み込んでおく (1-29)。
        self._dl_consumed_persisted = core.load_dl_consumed()
        # 開き直し引き継ぎ (1.70) の控えは代表の状態が持つ。
        # 閉じるときに直近の候補の key を残し (closeEvent)、
        # すぐ開き直した場合は最初の検出で「続き」判定に使う。
        self._primary_state.session = core.load_dl_session()
        # 到着した行の色付けの控え (2.07)。
        self._highlight_name = ""
        self._highlight_until = 0.0
        # AI の投げ先が設定済みか (_maybe_check_chat が解決の
        # たびに控える)。大きな進捗表示の明滅 (1.64) が、AI 窓の
        # 見張りが立っているかの判定に使う。
        self._ai_configured = False
        self._dl_pressing = False
        self._dl_press_auto = False
        self._dl_press_key = None
        # 押下に成功した DL の到着待ちの並び (2.38 で単一枠から
        # 並びへ)。1件は (候補の表示名, 押した時刻, 押した窓の
        # プロファイル名, 自動で押したか, 期待するファイル名)。
        # 到着はファイル名が一致した待ちだけが受け取る (名前を
        # 見ずに最初の到着が枠を奪うと、対象範囲 (a) の印と
        # 出どころが別のファイルに付くため、2.38)。落とした
        # ファイルが一覧に現れたら到着の確認として診断へ残し、
        # しばらく現れなければ「確認できなかった」と残す。
        self._dl_awaiting = []
        # スクリプト以外の到着確認で、大きさの落ち着きを見る
        # 控え (2.53)。パス → 前回見た大きさ。
        self._dl_arrival_stable = {}
        # この PHAL が自動で落としたファイルの key の控え (1.63、
        # 自動実行の対象範囲・案3 の (a))。到着の時点で、到着待ち
        # に控えた「自動で押したか」を見て積む。この会話の中だけ
        # の控えで、開き直すと消える
        # (ID の付いたファイルは (b) で引き続き対象になる)。
        self._auto_downloaded_keys = set()
        # カードとして見たファイル名 → (窓の名前, 目撃時刻) の
        # 控え (2.38)。フォルダに直接届いたファイルの出どころ
        # 判定に使う。この会話の中だけの控え。
        self._card_seen = {}
        # カード目撃の控え (先頭2行の指紋→窓、2.39)。カード型の
        # 候補はファイル名が取れないので、指紋側でも控える。
        self._card_seen_fp = {}
        # 出どころ不明で自動実行を見送った項目の控え (診断への
        # 記録を1項目1回にするため、2.38)。
        self._origin_unknown_noted = set()
        # 診断の控え (直近の動きの短い記録)。開き直しをまたいで
        # 残るよう core の控えファイルから読み込んでおく (1-41)。
        self._diag_events = core.load_diag_events()
        # 動作ログ (1.89)。開き直しをまたいで残す記録で、
        # _diag が診断の控えと並行して書く。「チェック（控え
        # から）」の列だけは個別に入れず、件数に畳む (下の
        # _diag と _flush_cached_check_log)。
        self._log_events = core.load_log_events()
        # コマンド履歴 (2.42)。AI からの合図と PHAL の応答を、
        # ファイルと混ぜて一覧に出すための記録。開き直しを
        # またいで残る。前回の会話で「保留」のまま終わった
        # 記録は結果が分からないので、見送りに確定させる。
        self._command_history = core.load_command_history()
        if core.command_history_settle_pending(
                self._command_history):
            core.save_command_history(self._command_history)
        # 送信中の合図の記録 (種別 → 記録の dict)。完了側が
        # 同じ dict を更新する。裏方スレッドで応答文が作られる
        # 合図 (ツール実行・入力欄まわり) は、文をここへ預けて
        # 完了側で記録へ移す。
        self._pending_commands = {}
        self._command_texts = {}
        self._command_seq = 0
        # 直近の _refresh_list が控えた行の並び
        # (("file", 項目) / ("command", 記録))。
        self._row_entry_list = []
        # ファイルの出どころ (2.04) と、今連携している AI の名前
        # (ラベル・ログ・出どころの記録で使う。_refresh_ai_label
        # が保つ)。
        self._item_ai = core.load_item_ai()
        self._ai_name = ""
        self._cached_check_count = 0
        self._cached_check_timer = QTimer(self)
        self._cached_check_timer.setSingleShot(True)
        self._cached_check_timer.timeout.connect(
            self._flush_cached_check_log)
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
        # 再実行の合図 (1.63)。消費済みの目印と直近に再実行した
        # 時刻の控え (開き直しをまたぐ。他の合図とは混ぜない)、
        # 見送り中の目印、見送り理由を1度だけ残すための控え。
        self._rerun_auto = core.load_rerun_auto()
        self._rerun_marker_pending = None
        self._rerun_noted = set()
        # 結果再送の合図 (1.75)。消費済みの目印と直近に再送した
        # 時刻の控え (開き直しをまたぐ。他の合図とは混ぜない)、
        # 見送り中の目印、見送り理由の控え、送信の最中の印と
        # 完了シグナル (診断の自動応答と同じ作り)。
        self._resend_auto = core.load_resend_auto()
        self._resend_marker_pending = None
        self._resend_noted = set()
        self._resend_sending = False
        self._resend_auto_mid = None
        self._resend_signals = _AiSignals()
        self._resend_signals.done.connect(self._on_resend_auto_done)
        # 設定確認の合図 (1.85)。控え・印・シグナルは他の合図と
        # 混ぜない (再送と同じ作り)。
        self._settings_auto = core.load_settings_auto()
        self._settings_marker_pending = None
        self._settings_noted = set()
        self._settings_sending = False
        self._settings_auto_mid = None
        self._settings_signals = _AiSignals()
        self._settings_signals.done.connect(
            self._on_settings_auto_done)
        # マニュアル再送の合図 (1.85)。同じく独立に持つ。
        self._manual_auto = core.load_manual_auto()
        self._manual_marker_pending = None
        self._manual_noted = set()
        self._manual_sending = False
        self._manual_auto_mid = None
        self._manual_signals = _AiSignals()
        self._manual_signals.done.connect(self._on_manual_auto_done)
        # 中継リクエストの合図 (2.10)。同じく独立に持つ。
        self._relay_auto = core.load_relay_auto()
        self._relay_marker_pending = None
        self._relay_noted = set()
        self._relay_sending = False
        self._relay_auto_mid = None
        # 中継の成功 ACK の保留 (2.27)。{src, dst, due, text,
        # state} の列。期限が来たら _flush_due_relay_acks が送る。
        self._relay_acks_pending = []
        self._relay_signals = _AiSignals()
        self._relay_signals.done.connect(self._on_relay_auto_done)
        self._relay_ack_signals = _AiSignals()
        self._relay_ack_signals.done.connect(
            self._on_relay_ack_done)
        self._relay_ack_sending = False
        # ファイルリクエストの合図 (1.88)。同じく独立に持つ。
        self._file_auto = core.load_file_auto()
        self._file_marker_pending = None
        self._file_noted = set()
        self._file_sending = False
        self._file_auto_mid = None
        self._file_signals = _AiSignals()
        self._file_signals.done.connect(self._on_file_auto_done)
        # キャプチャリクエストの合図 (2.54)。同じく独立に持つ。
        self._capture_auto = core.load_capture_auto()
        self._capture_marker_pending = None
        self._capture_noted = set()
        self._capture_sending = False
        self._capture_auto_mid = None
        # クリップボード経由の配達 (2.55): 送る直前に置いた画像と、
        # 元のクリップボードのテキストの控え (送った後に戻す)。
        self._capture_clip_image = None
        self._capture_clip_saved_text = None
        self._capture_signals = _AiSignals()
        self._capture_signals.done.connect(
            self._on_capture_auto_done)
        # 全文リクエストの合図 (1.91)。同じく独立に持つ。
        self._fulltext_auto = core.load_fulltext_auto()
        self._fulltext_marker_pending = None
        self._fulltext_noted = set()
        self._fulltext_sending = False
        self._fulltext_auto_mid = None
        self._fulltext_signals = _AiSignals()
        self._fulltext_signals.done.connect(
            self._on_fulltext_auto_done)
        # 実行一覧の合図 (1.92)。同じく独立に持つ。
        self._history_auto = core.load_history_auto()
        self._history_marker_pending = None
        self._history_noted = set()
        self._history_sending = False
        self._history_auto_mid = None
        self._history_signals = _AiSignals()
        self._history_signals.done.connect(
            self._on_history_auto_done)
        # 停止リクエストの合図 (1.93)。同じく独立に持つ。
        self._stop_auto = core.load_stop_auto()
        self._stop_marker_pending = None
        self._stop_noted = set()
        self._stop_sending = False
        self._stop_auto_mid = None
        self._stop_signals = _AiSignals()
        self._stop_signals.done.connect(self._on_stop_auto_done)
        # 動作ログリクエストの合図 (1.96)。同じく独立に持つ。
        self._log_auto = core.load_log_auto()
        self._log_marker_pending = None
        self._log_noted = set()
        self._log_sending = False
        self._log_auto_mid = None
        self._log_signals = _AiSignals()
        self._log_signals.done.connect(self._on_log_auto_done)
        # ツール実行リクエストの合図 (1.98)。同じく独立に持つ。
        # _tool_handle は実行中のツールの持ち手 (停止リクエスト
        # から止めるため)。実行していなければ None。
        self._tool_auto = core.load_tool_auto()
        self._tool_marker_pending = None
        self._tool_noted = set()
        self._tool_sending = False
        self._tool_auto_mid = None
        self._tool_handle = None
        self._tool_signals = _AiSignals()
        self._tool_signals.done.connect(self._on_tool_auto_done)
        # 道具説明リクエストの合図 (2.37)。同じく独立に持つ。
        self._describe_auto = core.load_describe_auto()
        self._describe_marker_pending = None
        self._describe_noted = set()
        self._describe_sending = False
        self._describe_auto_mid = None
        self._describe_signals = _AiSignals()
        self._describe_signals.done.connect(
            self._on_describe_auto_done)
        # 道具評価の合図 (2.37)。同じく独立に持つ。
        self._eval_auto = core.load_eval_auto()
        self._eval_marker_pending = None
        self._eval_noted = set()
        self._eval_sending = False
        self._eval_auto_mid = None
        self._eval_signals = _AiSignals()
        self._eval_signals.done.connect(self._on_eval_auto_done)
        # 入力欄まわりの合図 (2.39)。入力欄リクエスト・入力
        # 上書き・強制送信の3種で、控えと送信中を共有する。
        self._input_auto = core.load_input_auto()
        self._input_marker_pending = None
        self._input_noted = set()
        self._input_sending = False
        self._input_auto_mid = None
        self._input_signals = _AiSignals()
        self._input_signals.done.connect(self._on_input_auto_done)
        # おどうぐばこへの登録の受け渡し (2.37)。宣言つきで
        # 届いたファイルを、実行せず登録へ回すための待ち列と
        # 済みの控え (ファイルの key と中身の組)。
        self._register_queue = []
        self._register_tried = set()
        self._register_sending = False
        self._register_signals = _AiSignals()
        self._register_signals.done.connect(
            self._on_register_auto_done)
        # 応答済みの合図への知らせ (2.00)。再検出された合図を
        # 繰り返し実行せず、応答済みと1回だけ知らせるための
        # 送信中フラグとシグナル。
        self._refire_sending = False
        self._refire_signals = _AiSignals()
        self._refire_signals.done.connect(self._on_refire_notice_done)
        # 合図へのリアクション (2.33)。新しい目印には必ず
        # チャットで反応する (応答そのものか、見送りの理由の
        # 一行)。見送りの知らせの送信まわり。
        self._react_sending = False
        self._react_notes = []
        self._react_noted = {}
        self._log_calm = {}
        self._react_signals = _AiSignals()
        self._react_signals.done.connect(self._on_react_done)
        # 再実行の引数 (1.97)。_start_run が受けて控え、結果文の
        # 組み立て (_on_run_done) が見出しの後ろへ差し込む。
        self._running_args = []
        # 再実行の合図の依頼 ID (2.41)。_start_run が受けて
        # 控え、結果文の見出し (_on_run_done) に併記する。
        # 手動の実行では空。
        self._running_request_id = ""
        # 非常停止 (1.63)。入っている間は自動化 (DL・実行・回答・
        # 送信・合図への自動応答) が一切進まない。手動の操作は
        # そのままできる。開き直しをまたがない (窓を開いたら
        # 解除された状態から始まる)。
        self._emergency_stopped = False
        # 受領応答 (1-51)。送信の最中の印、結果を受けるシグナル、
        # 回答が受領の完了待ちになった項目の控え。回答・送信の
        # 連鎖の印 (_ai_filling 等) とは混ぜない。
        self._ack_sending = False
        self._ack_pending_answer = None
        # 回答の投入が他の送信と重なって見送られた項目の控え
        # (2.70、レビュー高6)。空いたら見回りが進める。
        self._answer_pending_item = None
        # 保留の再試行の間隔 (2.77、レビュー中2)。見回りのたびに
        # 送り役を起こさないよう、次に試してよい時刻と、伸びて
        # いく間隔 (2秒から10秒まで) を持つ。保留の診断は
        # episode ごとに1回だけ (_answer_pending_noted)。
        self._answer_pending_next_at = 0.0
        self._answer_pending_delay = 0.0
        self._answer_pending_noted = False
        # 受領の保留の再試行を抑える時刻 (2.77、レビュー中2)。
        self._ack_blocked_until = 0.0
        # 自動送信の保留 (deferred) の共通の間隔 (2.78、
        # レビュー重大1)。送信待ちの文がある間は、ほかの自動
        # 送信は撃ち直しを間引く (_chain_in_flight に混ぜる)。
        self._auto_blocked_until = 0.0
        self._auto_blocked = False
        # ループ内訳の計測 (2.79)。区間の境目の時刻 (monotonic)
        # と、直近の完了ループの区間表を控える。診断の束と
        # 動作ログの要約へ出すためのもので、動作には影響しない。
        self._loop_marks = {}
        self._loop_prev_send_done = None
        self._loop_stages = None
        self._loop_stages_name = ""
        # 受領応答を出す義務がある実行中の項目 (2.66、遅延送信)。
        self._ack_due_item = None
        # 送信中の受領応答の項目 (2.71)。保留されたら義務へ戻す。
        self._ack_item = None
        self._ack_signals = _AiSignals()
        self._ack_signals.done.connect(self._on_ack_done)
        # 開始プロンプトの送信 (1.77)。送信の最中の印、結果を
        # 受けるシグナル、送れなかったときにクリップボードへ
        # 入れる文の控え。受領の系統とは混ぜない。
        self._intro_sending = False
        self._intro_text = ""
        self._intro_attached = False
        # 開始プロンプトに入れる作業フォルダ (1.83)。ボタンを
        # 押したときにダイアログで選んだものが入る。
        self._intro_work_folder = ""
        self._intro_signals = _AiSignals()
        self._intro_signals.done.connect(self._on_intro_done)
        # 再開通知 (2.49)。起動したら投げ先の窓へ版数を1行送る。
        # 送信の最中の印、送り終えた印、混んでいて見送った回数、
        # 結果を受けるシグナル。受領の系統とは混ぜない。
        self._startup_notice_sending = False
        self._startup_notice_done = False
        self._startup_notice_tries = 0
        self._startup_signals = _AiSignals()
        self._startup_signals.done.connect(
            self._on_startup_notice_done)
        # チェックで止まった知らせ (1-53)。知らせ済みの中身
        # (sha256) の控え (開き直しをまたぐ)、送信の最中の印、
        # 結果を受けるシグナル。受領の系統とは混ぜない。
        self._blocked_notified = core.load_blocked_notified()
        self._notice_sending = False
        # 送信中の知らせの項目と、保留された知らせの再送の控え
        # (2.78、レビュー重大1)。知らせは合図の見張りからは
        # 撃ち直されないので、見回りで再送する。
        self._notice_item = None
        self._notice_pending_item = None
        self._notice_retry_next_at = 0.0
        self._notice_signals = _AiSignals()
        self._notice_signals.done.connect(self._on_notice_done)
        # チャットの検出役・DL 押下役 (テストで差し替える。None なら
        # 既定の _default_ai_downloader / _default_ai_dl_presser)。
        self.ai_downloader = None
        self.ai_signer = None
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
        # 入力欄を読む役・強制的に入れる役 (2.39、テストで差し
        # 替える。None なら既定の _default_ai_input_reader /
        # _default_ai_force_sender)。
        self.ai_input_reader = None
        self.ai_force_sender = None
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
        # 持ち主が見送った到着の控え (2.76、名前と sha256 の組)。
        self._skipped = core.load_skipped()
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
        # 直近の実行結果と直近のエラーの控え (2.73、phal_status
        # .json の last_run / last_error)。_note_phal_run が実行の
        # 終了ごとに差し替え、_publish_phal_status が毎回載せる。
        self._phal_last_run = None
        self._phal_last_error = None
        # 直近に発行した状態 (状態QR が読む、2.09)。
        self._phal_state = "idle"
        # 大きな進捗表示 (1.64) の対象行に出す項目 (実行中または
        # 直近)。_phal_item は概要だけなので、ファイル名も要る
        # こちらは項目そのものを控える。実行・回答・送信の開始で
        # 差し替える。
        self._display_item = None
        # 窓の上の表示 (1.48 の帯、1.64 で大きな進捗表示) の
        # ための、実行の時刻の控え。
        # _run_started_at は今の実行の開始 (monotonic)、
        # _last_run_seconds は直近の実行の所要秒 (run.seconds)。
        self._run_started_at = None
        self._last_run_seconds = None
        # 段階表示 (1.63、案D)。今の段階 (dl / run / answer /
        # send、待機中は None) と、その段階に入った時刻。段階の
        # 付け替えは _publish_phal_status が状態の変化に合わせて
        # 行う。大きな進捗表示のブロックの色は、段階と経過
        # (実行は無音の長さ)
        # から core.stage_color が決める。
        self._stage = None
        self._stage_since = None
        self._last_stage = None
        self._last_stage_since = None
        self._stage_cleared_at = None
        self._idle_since = None
        # 実行中の出力の逐次取り込み (1.63) の控え。実行スレッド
        # の _note_run_output が差し替え、表示のタイマが読む。
        # _run_lines は進捗申告を除いた標準出力の行数、
        # _run_progress は phal_lib.progress の直近の (done, total)。
        self._run_lines = 0
        self._run_last_line = ""
        self._run_last_output_at = None
        self._run_progress = None
        # 実行中の出力の末尾 (1.74)。診断の束へ入れて、長い
        # 実行の途中経過を AI が読めるようにする材料。
        self._run_tail = []
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
        # 窓の上の表示の 1秒ごとの付け直し (1.48)。経過時間だけが
        # 変わるので、軽い文言の付け直しだけ (追加のスレッド無し)。
        self._strip_timer = QTimer(self)
        self._strip_timer.timeout.connect(self._refresh_status_strip)
        self._strip_timer.start(1000)
        # 起動時は待機。モニタ側は、このファイルがあれば PHAL が
        # 動いていると分かる。
        self._publish_phal_status("idle", "待機中")
        # 再開通知 (2.49)。窓と送信の準備が落ち着く少し後に、
        # 投げ先の窓へ起動を1行知らせる。
        QTimer.singleShot(3000, self._try_startup_notice)

    # ---------------- 画面 ----------------
    @staticmethod
    def _emergency_button_style(stopped):
        """非常停止ボタンのスタイル (1.64、2.67 で縮小)。

        常時赤系で目立たせる。非常停止中はさらに明るい
        赤＋明るい枠にして、文言も「非常停止中（解除）」に
        変わる (_toggle_emergency_stop が付け替える)。
        2.67 からは、使う機会が少ないので他のボタンと同じ
        高さに縮めた (赤＋太字はそのまま)。"""
        if stopped:
            return ("background-color: #e03131; color: white;"
                    " font-size: 12px; font-weight: bold;"
                    " border: 2px solid #ff8787; border-radius: 6px;"
                    " padding: 3px 10px;")
        return ("background-color: #c0272d; color: white;"
                " font-size: 12px; font-weight: bold;"
                " border-radius: 6px; padding: 3px 10px;")

    @staticmethod
    def _big_panel_style(emergency):
        """大きな進捗表示のパネルのスタイル (1.64)。

        通常は灰色の枠。非常停止中は赤い枠と薄い赤の地にして、
        止まっていることが遠目にも分かるようにする。"""
        if emergency:
            return ("QFrame { background: rgba(224, 49, 49, 0.08);"
                    " border: 2px solid #e03131;"
                    " border-radius: 10px; }")
        return ("QFrame { background: rgba(128, 128, 128, 0.08);"
                " border: 2px solid rgba(128, 128, 128, 0.4);"
                " border-radius: 10px; }")

    def _stage_block_style(self, kind, color=None, ratio=0.0):
        """段階ブロックのスタイル (1.64、テーマ別は 1.77)。

        kind は idle (今後・待機) / done (済み) / now (現在)。
        done は緑、now の背景は core.stage_color の色をその
        まま使い、色が赤寄り (ratio >= 0.75) なら文字を白に
        する (1.63 のチップと同じ作法)。watch (監視ブロック
        の点灯、1.65) は作業中の緑と区別できる青、stopped
        (非常停止中の監視ブロック) は非常停止テーマの赤。
        idle と done は地の明るさで読みづらさが変わるので
        テーマごとに色を持つ (ライトで灰色の文字が沈む問題
        の対策、1.77)。watch / stopped / now は飽和した色+
        白文字で、どちらの地でも読めるので共通。"""
        base = ("font-size: 17px; font-weight: bold;"
                " border-radius: 10px; padding: 4px 6px;")
        if kind == "done":
            if self._dark_mode:
                return (base + " background: #14532d;"
                        " color: #7ee2a0;"
                        " border: 1px solid #1f7a41;")
            return (base + " background: #d7f2e0; color: #14532d;"
                    " border: 1px solid #4caf7d;")
        if kind == "watch":
            return (base + " background: #2f7fbf; color: #ffffff;"
                    " border: 1px solid #2f7fbf;")
        if kind == "stopped":
            return (base + " background: #e03131; color: #ffffff;"
                    " border: 1px solid #ff8787;")
        if kind == "now" and color is not None:
            red, green, blue = color
            foreground = "#ffffff" if ratio >= 0.75 else "#111111"
            return (base
                    + f" background: rgb({red}, {green}, {blue});"
                    + f" color: {foreground};"
                    + f" border: 1px solid"
                    + f" rgb({red}, {green}, {blue});")
        if self._dark_mode:
            return (base + " background: rgba(128, 128, 128, 0.25);"
                    " color: rgba(160, 160, 160, 0.9);"
                    " border: 1px solid rgba(128, 128, 128, 0.35);")
        return (base + " background: #e9ebee; color: #454b52;"
                " border: 1px solid #c6cbd2;")

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 最上段の1行 (2.67 で集約)。タイトルを左に、ボタンと
        # 表示のチェックは全部この行の右側へ右揃えで並べる。
        # 以前はタイトル行 (小窓・非常停止) と、進捗パネルの
        # 下のボタン行 (設定・状態QR・おどうぐばこ・開き直す・
        # 版・前面に固定) と、一覧の上の絞り込み行 (ダーク) に
        # 分かれていた。非常停止は使う機会が少ないので、他の
        # ボタンと同じ高さに縮めた (1.64〜2.66 は高さ44の大きな
        # ボタン)。赤い見た目と、停止中に進捗パネルの枠が赤く
        # なる表示はそのまま。タイトルは短く "PHAL" だけにする
        # (正式名と版は窓のタイトルバーに出ている。全文は
        # ツールチップ)。タイトルの横方向は Preferred (既定)。
        # Ignored にすると stretch とボタンに挟まれたこの行で
        # は幅0に潰れて文字が消える (1.64 の実機で発生し、
        # 1.65 で修正)。
        title_row = QHBoxLayout()
        title = QLabel("PHAL")
        title.setFont(QFont(title.font().family(), 11, QFont.Weight.Bold))
        title.setToolTip("PHAL：PHAL Helps Agent Loop")
        title.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        title_row.addWidget(title)
        title_row.addStretch(1)
        # 小窓モードの切替 (2.27)。上部 (この行と状態パネル)
        # だけを残して下部を隠す。状態は設定に覚える。
        self.mini_button = QPushButton("小窓")
        self.mini_button.setCheckable(True)
        self.mini_button.setToolTip(
            "小窓モード (上部の状態パネルだけを表示) の切り替え")
        self.mini_button.clicked.connect(self._toggle_mini_mode)
        title_row.addWidget(self.mini_button)
        self.ai_window_button = QPushButton("設定")
        self.ai_window_button.clicked.connect(self._open_ai_window)
        title_row.addWidget(self.ai_window_button)
        # 状態QR (2.09)。外から見えない場所でも、PHAL の状態
        # だけはスマホで運べるようにする。
        self.qr_button = QPushButton("状態QR")
        self.qr_button.setToolTip(
            "PHAL の状態だけを QR コードで表示します"
            "（スマホで読んでチャットへ貼る用）")
        self.qr_button.clicked.connect(self._show_status_qr)
        title_row.addWidget(self.qr_button)
        # おどうぐばこの GUI を開くボタン (2.37)。本体は独立
        # モジュールで、窓は PHAL 側で保持する (閉じても作り
        # 直さない)。
        self.odougubako_button = QPushButton("おどうぐばこ")
        self.odougubako_button.setToolTip(
            "おどうぐばこ（AI の道具箱）の一覧ウィンドウを開きます")
        self.odougubako_button.clicked.connect(self._open_odougubako)
        title_row.addWidget(self.odougubako_button)
        self._odougubako_window = None
        self.restart_button = QPushButton("開き直す")
        self.restart_button.setToolTip("PHAL を新しいコードで開き直します")
        self.restart_button.clicked.connect(self.restart)
        title_row.addWidget(self.restart_button)
        self.emergency_button = QPushButton("非常停止")
        self.emergency_button.setToolTip(
            "自動化（DL・実行・回答・送信）をすべて止めます。"
            "もう一度押すと解除します")
        self.emergency_button.setStyleSheet(
            self._emergency_button_style(False))
        self.emergency_button.clicked.connect(
            self._toggle_emergency_stop)
        title_row.addWidget(self.emergency_button)
        version = QLabel(self._version_label())
        version.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
        title_row.addWidget(version)
        self.version_label = version
        self.on_top_box = QCheckBox("前面に固定")
        self.on_top_box.setChecked(bool(self.config.get("always_on_top")))
        self.on_top_box.toggled.connect(
            lambda on: self._apply_on_top(on, save=True))
        title_row.addWidget(self.on_top_box)
        # ダークの切替も表示のチェックとしてこの行の右端へ
        # (2.67。以前は一覧の上の絞り込み行の右端にあった)。
        self.dark_box = QCheckBox("ダーク")
        self.dark_box.setToolTip("PHAL の窓を暗い配色にします")
        self.dark_box.setChecked(self._dark_mode)
        self.dark_box.toggled.connect(self._apply_dark_mode)
        title_row.addWidget(self.dark_box)
        layout.addLayout(title_row)

        # 進捗パネルのラベル (2.67)。「直前の結果」「動作ログ」
        # と同じ小さな見出しを、各段の頭にそろえる。
        self.progress_caption = QLabel("進捗")
        self.progress_caption.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
        layout.addWidget(self.progress_caption)

        # 大きな進捗表示 (1.64、2.15 で階形式)。タイトルの直下に
        # 横幅いっぱいのパネルとして置く。中身は AI ごとの「階」
        # (細いバーの並び) で、階ごとに [チェック＋名前] [監視 →
        # DL → 実行 → 回答 → 送信 の段階ブロック] [数字＋見出し]
        # を出す。階はプロファイルの数だけあり (1つなら1階だけ)、
        # チェックは2つ以上あるときだけ出す (2.14 の連携中チェッ
        # クの置き場がここへ移った)。下に対象行と小さい情報行
        # (手数・経過・ねらい。1.48 の帯の文言そのまま) を置く。
        # 色と文言の付け直しは _refresh_status_strip が1秒ごとに
        # やる。ループ実施中 (AI 窓の見張りが動作中) は、この
        # 監視している階だけがゆっくり明滅する (_update_pulse /
        # _tick_pulse)。
        panel = QFrame()
        panel.setStyleSheet(self._big_panel_style(False))
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(8, 6, 8, 6)
        panel_layout.setSpacing(4)
        self._floors_layout = QVBoxLayout()
        self._floors_layout.setContentsMargins(0, 0, 0, 0)
        self._floors_layout.setSpacing(3)
        panel_layout.addLayout(self._floors_layout)
        self._floors = {}
        # 対象行: 実行中または直近のファイル名＋概要。実行中は
        # 最後の出力行も添える。横幅はレイアウトに任せる。
        self._big_item = QLabel("")
        self._big_item.setStyleSheet("font-size: 12px;")
        self._big_item.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        panel_layout.addWidget(self._big_item)
        # 小さい情報行 (1.48)。手数・経過時間・今のねらいを
        # 1行で出す。ねらいが長いと窓の最小幅を押し広げるので、
        # 横幅はレイアウトに任せる (見出し・フォルダ名と同じ
        # 作法)。
        strip = QLabel()
        strip.setStyleSheet(
            f"font-size: 11px; color: {self._theme()['muted']};")
        strip.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        panel_layout.addWidget(strip)
        self.status_strip = strip
        layout.addWidget(panel)
        self._big_panel = panel
        # 小窓モードで隠す範囲は、レイアウトのこの位置から
        # 後ろ全部 (2.27)。
        self._lower_start = layout.count()
        self._mini_mode = False
        self._normal_height = None
        # 明滅は階ごとに付ける (2.16)。効果は階の枠へ付け
        # (_rebuild_floors)、タイマはここで1つだけ持ち、回すか
        # どうかは _update_pulse が決める。非常停止ボタンは
        # パネルの外 (タイトル行) なので明滅しない。
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._big_panel_emergency = False
        self._refresh_status_strip()

        # 連携先の表示と切替は、大きな進捗表示の階が担う
        # (2.15)。階の作り直しは _refresh_ai_label が受け持つ。
        # 窓全体のボタン行は 2.67 で最上段の1行へ集約した
        # (この下にあった行は廃止)。
        self._refresh_ai_label()

        # 検知の帯 (2.07)。ダウンロード候補を見つけたら、色付きで
        # しばらく知らせる。消えるのはタイマ (出す側が秒数を決める)。
        self._banner = QLabel("")
        self._banner.setStyleSheet(
            "font-size: 12px; padding: 4px 8px; "
            + self._theme()["accent_style"])
        self._banner.setVisible(False)
        layout.addWidget(self._banner)
        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(
            lambda: self._banner.setVisible(False))

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

        # 一覧のラベル (2.67)。「進捗」「直前の結果」「動作
        # ログ」と同じ小さな見出し。以前ここにあった絞り込み行
        # は廃止した: 「ID が無いファイルを表示しない」は表の
        # 下の「記録をコピー」の右へ、「ダーク」は最上段の
        # 1行の右端へ移した (どちらも部品は _build_ui の
        # それぞれの場所で作る)。
        self.list_caption = QLabel("一覧")
        self.list_caption.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
        layout.addWidget(self.list_caption)

        # 一覧は1スクリプト1行の表(時刻/ID/ファイル名/ハッシュ/
        # 概要/状態/実行)。
        # 詳しい状態や操作の残り(要確認の2段・エラーコピー・EDDy で
        # 開く)は、選択中の行に対する下半分と右クリックに置く。
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["時刻", "ID", "ファイル名", "AI", "ハッシュ", "概要",
             "状態", ""])
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
        # 列の境界が分かるよう格子線は出す (2.75。色はテーマの
        # gridline-color)。列幅をドラッグで変えるようになって、
        # 境目が見えないと掴めないという実機報告を受けた。
        self.table.setShowGrid(True)
        header = self.table.horizontalHeader()
        # 列幅は全部ドラッグで変えられる (2.72、環境2の所感から)。
        # 概要も含めて全部 Interactive (2.75)。Stretch だと概要の
        # 境界をドラッグしても幅が戻り、直感的でなかった。余った
        # 幅の配分は、表の大きさが変わったときに _distribute_
        # table_width が概要へ寄せる。保存済みの幅があればそれを
        # 初期値にする (保存は終了時)。保存が無い間は、ファイル
        # 名は _fit_name_column が、その他の列は初回だけ中身に
        # 合わせて決める。
        for col in (COL_TIME, COL_ID, COL_NAME, COL_AI, COL_HASH,
                    COL_SUMMARY, COL_STATUS, COL_RUN):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.Interactive)
        saved_widths = self.config.get("column_widths")
        if saved_widths:
            for col, width in enumerate(saved_widths):
                header.resizeSection(col, width)
        # 実行ボタンの列の下限 (2.74)。保存済みの幅が壊れて
        # いても、ボタンが潰れて見えなくならないようにする
        # (core.normalize_config でも締めているが、ここでも守る)。
        if header.sectionSize(COL_RUN) < core.RUN_COLUMN_MIN_WIDTH:
            header.resizeSection(COL_RUN, core.RUN_COLUMN_MIN_WIDTH)
        # 表の大きさの変化で、幅の余りを概要列へ配る (2.75)。
        self.table.installEventFilter(self)
        # 名前列を利用者がドラッグしたら、自動合わせを止める
        # (2.80、レビュー中3)。
        header.sectionResized.connect(self._on_section_resized)
        self.table.currentCellChanged.connect(self._on_selection_changed)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(
            self._on_context_menu)
        layout.addWidget(self.table, stretch=3)

        # 「PHALループ開始プロンプトを送信」は窓全体の入口。行の
        # 「AIへ回答」は実行が終わった行にだけ付くが、こちらは
        # 一覧の状態に関わらずいつでも押せる。表の直下に置く
        # (1.47、送信する形と改名は 1.77)。
        intro_row = QHBoxLayout()
        self.intro_button = QPushButton(
            "PHALループ開始プロンプトを入力")
        self.intro_button.setToolTip(
            "開始プロンプトを AI の窓へ送ります"
            "（送れないときはクリップボードへ入れて知らせます）")
        self.intro_button.clicked.connect(self._on_intro_clicked)
        intro_row.addWidget(self.intro_button)
        # 「記録をコピー」は開始プロンプトの隣 (1.51)。こちらは
        # クリップボードへ入れるだけのボタンで、表の直下に並べる。
        self.record_button = QPushButton("記録をコピー")
        self.record_button.setToolTip(
            "直近の実行履歴を Markdown にしてクリップボードへ入れます")
        self.record_button.clicked.connect(self._copy_loop_record)
        intro_row.addWidget(self.record_button)
        # 「ID が無いファイルを表示しない」は「記録をコピー」
        # の右へ (2.67、持ち主の指定)。以前は表の上の絞り込み
        # 行にあった。隠すのは見た目だけで、裏のチェックや
        # 実行の追跡は全ファイルのまま (_visible_items が表の
        # 行との対応)。
        self.hide_no_id_box = QCheckBox("ID が無いファイルを表示しない")
        self.hide_no_id_box.setChecked(bool(self.config.get("hide_no_id")))
        self.hide_no_id_box.toggled.connect(self._apply_hide_no_id)
        intro_row.addWidget(self.hide_no_id_box)
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

        # 動作ログの表示欄 (2.00)。これまでは設定画面の奥の
        # ビューアでしか見られなかったので、メイン画面の下部に
        # 7行分を常時出す。中身は _diag が書く動作ログそのもの
        # で、増えるたびに _refresh_log_view が末尾へ足す。
        self.log_caption = QLabel("動作ログ")
        self.log_caption.setStyleSheet(
            f"color: {self._theme()['muted']}; font-size: 11px;")
        layout.addWidget(self.log_caption)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setFixedHeight(
            self.log_view.fontMetrics().lineSpacing() * 7 + 14)
        layout.addWidget(self.log_view)
        self._log_view_shown = 0
        self._log_view_marker = None
        self._refresh_log_view()

        QShortcut(QKeySequence("F5"), self, self._f5_pressed)
        if bool(self.config.get("mini_mode")):
            self._set_mini_mode(True, save=False)

    def _refresh_ai_label(self):
        """トップ画面の連携先の選択肢を、今の設定に合わせ直す
        (2.04 で表示、2.05 で選択式に)。起動時と設定の保存のたび
        に呼び直す。読めなければ空にする (失敗しても窓は止め
        ない)。"""
        profiles, active, name = [], 0, ""
        watch = []
        try:
            import script_runner_ai as sr_ai

            all_settings = settings_module.load_settings(
                self.settings_path)
            section = all_settings.get("script_runner") or {}
            profiles, active = sr_ai.sanitize_profiles(section)
            name = profiles[active]["name"]
            watch = sr_ai.sanitize_watch_profiles(section)
        except Exception:  # noqa: BLE001  静かに空へ
            profiles, active, name = [], 0, ""
            watch = []
        self._ai_name = name
        self._rebuild_floors(profiles, active, watch)

    def _floor_block_style(self, kind, color=None, ratio=0.0):
        """階の段階ブロックのスタイル (2.15)。

        大きな進捗表示の段階ブロック (1.64) と同じ色使いで、
        文字と余白だけ細いバー用に小さくしたもの。"""
        return (self._stage_block_style(kind, color, ratio)
                + " font-size: 11px; border-radius: 6px;"
                + " padding: 1px 4px;")

    def _floor_frame_style(self) -> str:
        """階の枠のスタイル (2.16)。階ごとにグループを分ける。

        枠の色は段階ブロックの沈んだ色と同じ系 (テーマ別)。
        明滅は枠でなく背景の層が担う (2.25、_rebuild_floors)。"""
        if self._dark_mode:
            border = "rgba(128, 128, 128, 0.35)"
        else:
            border = "#c6cbd2"
        return (f"QFrame {{ border: 1px solid {border};"
                " border-radius: 6px; }")

    def _floor_bg_color(self) -> str:
        """階の背景の層の色 (2.25)。明滅で透け具合が変わる。

        Nagato の希望で、沈んだグレーからグリーンへ寄る色。"""
        return "#3f7d54" if self._dark_mode else "#a8d8b9"


    def _rebuild_floors(self, profiles, active, watch):
        """大きな進捗表示の階を作り直す (2.15)。

        プロファイルごとに1階。[チェック＋名前] [監視＋段階
        ブロック] [数字＋見出し] の細いバー。チェックは見張る
        窓の集合に入っている側だけ入り、どの階も操作できる
        (2.20 で主・副の区別をなくした)。プロファイルが1つ
        でもチェックは出す (2.62、見張りの一時停止にも使う
        ため。以前は2つ以上でだけ出していた)。付け外しは
        _on_ai_check_toggled が設定へ保存する。階ごとに枠で
        グループを分け、名前の欄の幅は階どうしで揃える。
        明滅の効果も階ごとに持つ (2.16)。"""
        layout = getattr(self, "_floors_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._floors = {}
        # 名前の欄は階どうしで幅を揃える (2.16、監視ブロックの
        # 左端が階ごとにずれないように)。部品を先に全部作り、
        # いちばん広いものに合わせる。
        name_widgets = []
        for index, entry in enumerate(profiles):
            name = entry["name"]
            box = QCheckBox(name)
            box.setStyleSheet("font-size: 11px;")
            box.setChecked(name in watch)
            box.setToolTip("チェックでこの窓を見張ります")
            box.toggled.connect(
                lambda checked, n=name:
                    self._on_ai_check_toggled(n, checked))
            name_widgets.append(box)
        name_width = max(
            (w.sizeHint().width() for w in name_widgets), default=0)
        for index, entry in enumerate(profiles):
            name = entry["name"]
            # 階ごとに枠でグループを分ける (2.16)。
            row = QFrame()
            row.setStyleSheet(self._floor_frame_style())
            # 背景の層と中身を重ねる (2.25)。明滅は背景の層
            # だけ (不透明度で滑らかに)。ブロックや文字は
            # 薄くならない。
            stack = QStackedLayout(row)
            stack.setStackingMode(
                QStackedLayout.StackingMode.StackAll)
            stack.setContentsMargins(0, 0, 0, 0)
            bg = QWidget()
            bg.setStyleSheet(
                f"background-color: {self._floor_bg_color()};"
                " border-radius: 6px;")
            bg.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            effect = QGraphicsOpacityEffect(bg)
            effect.setOpacity(0.0)
            bg.setGraphicsEffect(effect)
            stack.addWidget(bg)
            content = QWidget()
            stack.addWidget(content)
            row_layout = QHBoxLayout(content)
            row_layout.setContentsMargins(6, 3, 6, 3)
            row_layout.setSpacing(4)
            floor = {"row": row, "check": None, "name_label": None,
                     "bg": bg, "effect": effect}
            name_widget = name_widgets[index]
            name_widget.setFixedWidth(name_width)
            row_layout.addWidget(name_widget)
            floor["check"] = name_widget

            watch = QLabel("監視")
            watch.setAlignment(Qt.AlignmentFlag.AlignCenter)
            watch.setMinimumHeight(22)
            watch.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred)
            watch.setStyleSheet(self._floor_block_style("idle"))
            row_layout.addWidget(watch, stretch=1)
            floor["watch"] = watch
            chips = {}
            for stage_key in core.STAGE_ORDER:
                arrow = QLabel("›")
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                arrow.setStyleSheet(
                    "font-size: 12px; font-weight: bold;"
                    f" color: {self._theme()['muted']};")
                row_layout.addWidget(arrow)
                block = QLabel(core.STAGE_LABELS[stage_key])
                block.setAlignment(Qt.AlignmentFlag.AlignCenter)
                block.setMinimumHeight(22)
                block.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Preferred)
                block.setStyleSheet(self._floor_block_style("idle"))
                row_layout.addWidget(block, stretch=1)
                chips[stage_key] = block
            floor["chips"] = chips
            number_box = QVBoxLayout()
            number_box.setSpacing(0)
            number = QLabel("―")
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            number.setMinimumWidth(76)
            number.setStyleSheet(
                "font-size: 17px; font-weight: bold;")
            number_box.addWidget(number)
            caption = QLabel("待機中")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setStyleSheet(
                f"font-size: 10px; color: {self._theme()['muted']};")
            number_box.addWidget(caption)
            row_layout.addLayout(number_box)
            floor["number"] = number
            floor["caption"] = caption
            layout.addWidget(row)
            self._floors[name] = floor


    def _on_ai_check_toggled(self, name: str, checked: bool):
        """階のチェックで見張る窓の集合を入れ切りする (2.20、
        置き場は 2.15 で階へ、元は二重待受の切替 2.14)。

        どの階のチェックも操作でき、入れた窓が見張りに加わり、
        外した窓が外れる (見張りゼロも可)。設定へすぐ保存し、
        見張りは次の同期から新しい集合で動く。保存の形は他の
        トップ画面の切替と同じく、該当キーだけ (新しいキー
        ai_watch_profiles に集合全体を、プロファイル一覧の
        順で書く)。"""
        try:
            import script_runner_ai as sr_ai

            all_settings = settings_module.load_settings(
                self.settings_path)
            section = all_settings.get("script_runner") or {}
            watch = sr_ai.sanitize_watch_profiles(section)
            if checked and name not in watch:
                watch.append(name)
            if not checked and name in watch:
                watch.remove(name)
            profiles, _active = sr_ai.sanitize_profiles(section)
            order = [entry["name"] for entry in profiles]
            watch = [n for n in order if n in set(watch)]
            settings_module.update_settings_section(
                "script_runner", {"ai_watch_profiles": watch},
                self.settings_path)
        except Exception as e:  # noqa: BLE001  理由は注記で出す
            self._set_note(
                f"見張りを切り替えられませんでした: {e}", "error")
            self._refresh_ai_label()
            return
        if checked:
            self._diag(f"見張りに追加しました: {name}")
            self._set_note(f"{name} を見張りに追加しました", "ok")
        else:
            self._diag(f"見張りから外しました: {name}")
            self._set_note(f"{name} の見張りを外しました", "ok")
        self._refresh_ai_label()

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
        効くのはこの窓と、この窓を親に開くダイアログ (「設定」
        など) だけ。アプリケーション全体には付けない (同じ
        プロセスに居る他の窓を巻き込まないため。EDDy の窓は
        PHAL の親子に入らないので、こちらも付かない)。
        意味のある色はテーマ表から取り直し、一覧も作り直す。
        段階ブロックもテーマに合わせて付け直す (テーマと段階の
        色が食い違わないように)。ライトにも専用の明るい配色を
        当てる (1.78。OS 任せのままだと環境によって暗く描かれ、
        読みづらかったため)。"""
        theme = self._theme()
        chrome = theme["chrome"]
        self.setPalette(_chrome_palette(chrome))
        self.setStyleSheet(_chrome_stylesheet(chrome))
        self.table.setAlternatingRowColors(self._dark_mode)
        self._delegate.tint = QColor(*theme["selection_tint"])
        muted_style = f"color: {theme['muted']}; font-size: 11px;"
        self.version_label.setStyleSheet(muted_style)
        # 階の矢印や見出しはテーマ色で作るので、作り直して
        # 当て直す (2.15)。
        self._refresh_ai_label()
        self.result_caption.setStyleSheet(muted_style)
        # 段の見出し (2.67 でそろえた分) も同じ色へ追随させる。
        self.progress_caption.setStyleSheet(muted_style)
        self.list_caption.setStyleSheet(muted_style)
        self.log_caption.setStyleSheet(muted_style)
        self.folder_caption.setText(self._caption_html())
        self.note.setStyleSheet(
            theme["note_styles"].get(self._note_kind, ""))
        self._refresh_list()
        self._refresh_status_strip()

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
    def _diag(self, text: str, ai=None, rid=None):
        """直近の動きを診断の控えへ1件残す (1-41)。

        ループが止まったとき、画面の外からは理由が見えない。控えは
        「設定」の「診断をAIへ貼る」でまとめて AI へ渡せる。
        診断が理由で本体の連鎖が止まることがあってはならないので、
        ここでの失敗はすべて黙ってやり過ごす。
        あわせて動作ログ (1.89) にも書く。ただし「チェック
        （控えから）」の列は開き直しのたびに数十件出てログを
        押し流すので、個別には入れず件数だけ数えて後で1行に
        畳む。

        動作ログの [AI名] (ai、2.40 で出どころに合わせるよう
        修正): 明示の引数 > 走査の処理中 (_dispatch_state) は
        その窓のプロファイル名 > 主プロファイル名 (_ai_name)。
        全体の出来事 (非常停止など) は、呼び側が ai="" を明示
        すると名前を付けない。以前は全件に主プロファイル名を
        押していたため、二重待受で Copilot の窓の出来事まで
        [Muse] と出る不具合があった。
        rid (2.40) は合図の依頼 ID。明示の引数 > 走査の処理中
        に _dispatch_signal が立てた控え (_diag_ctx_rid)。
        非同期の完了側は、控えの組から取り出した ID を明示で
        渡す。"""
        try:
            core.diag_append(self._diag_events, text)
            core.save_diag_events(self._diag_events)
        except Exception:  # noqa: BLE001  診断は本体を止めない
            pass
        try:
            if str(text).startswith("チェック（控えから）"):
                self._cached_check_count += 1
                self._cached_check_timer.start(3000)
            else:
                if ai is None:
                    dispatch = getattr(self, "_dispatch_state",
                                       None)
                    if dispatch is not None:
                        ai = getattr(dispatch, "name", "") or ""
                    else:
                        ai = getattr(self, "_ai_name", "")
                if rid is None:
                    rid = getattr(self, "_diag_ctx_rid", "") \
                        or None
                core.log_append(self._log_events, text,
                                ai=ai, rid=rid)
                core.save_log_events(self._log_events)
                self._refresh_log_view()
        except Exception:  # noqa: BLE001  ログも本体を止めない
            pass

    def _refresh_log_view(self):
        """動作ログをメイン画面下部のログ欄へ映す (2.00)。

        _diag が1件足すたびに呼ばれ、増えた分だけ末尾へ足す。
        控えが上限で切り詰められるなどして並びが合わなくなっ
        たら全体を作り直す。ログ欄は _build_ui で作られるので、
        それより前に呼ばれたら何もしない。"""
        view = getattr(self, "log_view", None)
        if view is None:
            return
        events = self._log_events or []
        shown = getattr(self, "_log_view_shown", 0)
        marker = getattr(self, "_log_view_marker", None)
        if shown > len(events):
            shown = 0
        if shown and marker is not None:
            last = events[shown - 1]
            if (last.get("at"), last.get("text"),
                    last.get("ai"), last.get("rid")) != marker:
                shown = 0
        if shown == 0:
            view.clear()
        for line in core.format_log_lines(
                events[shown:], with_date=True):
            view.appendPlainText(line)
        self._log_view_shown = len(events)
        if events:
            last = events[-1]
            self._log_view_marker = (
                last.get("at"), last.get("text"), last.get("ai"),
                last.get("rid"))
        else:
            self._log_view_marker = None
        cursor = view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        view.setTextCursor(cursor)

    def _flush_cached_check_log(self):
        """数えた「チェック（控えから）」を1行に畳んで動作ログ
        へ入れる (1.89)。"""
        count, self._cached_check_count = self._cached_check_count, 0
        if count <= 0:
            return
        try:
            core.log_append(
                self._log_events,
                f"チェック（控えから）: {count}件（開き直し時の再掲）")
            core.save_log_events(self._log_events)
            self._refresh_log_view()
        except Exception:  # noqa: BLE001
            pass

    def _note_phal_run(self, run, command_id):
        """直近の実行結果を phal_status.json 用に控える (2.73)。

        実行の終了ごとに _phal_last_run を差し替え、成功でない
        ときは _phal_last_error (段は run) も差し替える。成功して
        も last_error は消さない (直近のエラーはいつ・何だったか
        を残す)。実際の書き出しは、この後の _publish_phal_status
        (停止・待機・回答のいずれか) が担う。"""
        if run.start_error:
            outcome = "start_error"
        elif run.timed_out:
            outcome = "timeout"
        elif run.stopped:
            outcome = "stopped"
        elif run.policy_stop:
            outcome = "policy_stop"
        elif run.exit_code == 0:
            outcome = "ok"
        else:
            outcome = "failed"
        self._phal_last_run = {
            "name": run.name,
            "command_id": command_id or "",
            "outcome": outcome,
            "exit_code": run.exit_code,
            "seconds": run.seconds,
            "stdout_head": (run.stdout or "")[:500],
            "stderr_head": (run.stderr or "")[:500],
            "at": int(time.time()),
        }
        if outcome != "ok":
            reason = run.start_error or ""
            if not reason and run.stderr and run.stderr.strip():
                reason = run.stderr.strip().splitlines()[0]
            if not reason:
                reason = {
                    "timeout": "時間切れで止めました",
                    "stopped": "停止されました",
                    "policy_stop": "実行ポリシーで止めました",
                }.get(outcome, f"終了コード {run.exit_code}")
            self._phal_last_error = {
                "stage": "run", "reason": reason,
                "at": int(time.time())}

    def _publish_phal_status(self, state, detail="",
                             stopped_reason="", command_id=None,
                             item=None, affect_stage=True):
        """ARiA モニタ表示用の状態を手元ファイルへ出す (req-20261115-02)。

        state は idle / dl / run / answer / send / stopped。command_id
        と item を渡せば、実行中または直近のスクリプトの素性を
        差し替える (渡さなければ今のまま)。書くのは状態が変わった
        ときだけで、書き込みに失敗してもループは止めない (どちらも
        core 側が受け持つ)。

        あわせて、段階表示 (1.63) の今の段階もここで付け替える
        (dl / run / answer / send がそのまま段階で、idle /
        stopped は段階なし)。状態の発行はここに一本化されている
        ので、段階の出入りもここで見るのが漏れない。ただし
        候補の表示 (新着) は affect_stage=False で呼び、段階には
        入れない。候補が見えているだけの間は「監視」であり、
        DL の段階は実際に落とし始めてから落とし終わるまで
        (1.66。候補の表示で DL が点灯しっぱなしになり、監視
        ブロックが隠れる実機報告を受けた直し)。"""
        self._phal_state = state
        try:
            stage = {"dl": "dl", "run": "run", "answer": "answer",
                     "send": "send"}.get(state)
            if affect_stage and stage != self._stage:
                if stage is None and self._stage is not None:
                    # 段階が落ちた時刻と直前の段階を覚える
                    # (表示の保持時間に使う。1.67 の
                    # _display_stage を参照)
                    self._last_stage = self._stage
                    self._last_stage_since = self._stage_since
                    self._stage_cleared_at = time.monotonic()
                if stage is not None:
                    self._last_stage = None
                    self._last_stage_since = None
                    self._stage_cleared_at = None
                self._stage = stage
                self._stage_since = time.monotonic() \
                    if stage is not None else None
            # 待機時間の起点 (1.68): 待機 (idle) に入った時刻を
            # 覚え、動き出したら消す。監視中の大きな数字で
            # 「待ってからどれだけ経ったか」を出すため。
            if state == "idle":
                if self._idle_since is None:
                    self._idle_since = time.monotonic()
            else:
                self._idle_since = None
            if command_id is not None:
                self._phal_command_id = command_id
            if item is not None:
                self._phal_item = item
            status = core.build_phal_status(
                PHAL_VERSION, state,
                command_id=self._phal_command_id,
                detail=detail, stopped_reason=stopped_reason,
                step=self._phal_step, item=self._phal_item,
                last_run=self._phal_last_run,
                last_error=self._phal_last_error)
            core.write_phal_status(status)
        except Exception:  # noqa: BLE001  状態の書き出しは本体を止めない
            pass

    def _publish_phal_idle(self, detail="待機中"):
        """連鎖が何も動いていなければ待機に戻す。

        実行・回答・送信の途中で呼ぶと、動いている段の表示を
        待機で上書きしてしまうので、連鎖が空いているときだけ
        書く。非常停止中は、モニタ側に stopped (非常停止) を
        残すため待機では上書きしない。"""
        if self._emergency_stopped:
            return
        if not self._chain_in_flight():
            self._publish_phal_status("idle", detail)

    def _status_strip_text(self) -> str:
        """小さい情報行の1行 (1.48 の帯の文言、1.64 で大きな
        進捗表示の中へ移した)。

        手数・経過時間・今のねらい。実行中は今の実行の経過秒、
        待機中は直近の実行の所要秒、まだ一度も実行していなければ
        「--:--」。ねらいは phal_status.json の item と同じ出所
        (_phal_item)。実行中は、逐次取り込み (1.63) の材料
        (出力行数・進捗・最後の行) も足す。非常停止中は、その
        旨を先頭に出す。"""
        if self._run_started_at is not None:
            label = "経過"
            seconds = time.monotonic() - self._run_started_at
        elif self._last_run_seconds is not None:
            label = "所要"
            seconds = self._last_run_seconds
        else:
            label = "経過"
            seconds = None
        text = (f"手数 {self._phal_step} ・ {label} "
                f"{core.format_elapsed(seconds)}"
                f" ・ ねらい：{self._phal_item}")
        if self._run_started_at is not None:
            text += f" ・ 出力 {self._run_lines} 行"
            if self._run_progress is not None:
                text += (f" ・ 進捗 {self._run_progress[0]}"
                         f"/{self._run_progress[1]}")
            if self._run_last_line:
                text += f" ・ 最後の行：{self._run_last_line[:60]}"
        if self._emergency_stopped:
            text = "非常停止中 ・ " + text
        return text

    def _display_stage(self):
        """表示に使う段階と、その開始時刻 (1.67)。

        段階が落ちた直後は、保持時間 (STAGE_EXIT_GRACE_SEC)
        の間だけ直前の段階を表示し続ける。DL の完了で候補が
        下がって待機が発行され、ダウンロードしたファイルの
        実行が始まるまでの一瞬に「監視」が割り込むちらつき
        を防ぐため (実機報告)。状態ファイル
        (phal_status.json) の中身には影響しない (表示だけ)。"""
        if self._stage is not None:
            return self._stage, self._stage_since
        if self._last_stage is not None \
                and self._stage_cleared_at is not None \
                and time.monotonic() - self._stage_cleared_at \
                < STAGE_EXIT_GRACE_SEC:
            return self._last_stage, self._last_stage_since
        return None, None

    def _refresh_status_strip(self):
        """小さい情報行と大きな進捗表示を付け直す。1秒ごとの
        タイマと、実行の開始・終了の直後から呼ぶ。

        段階ブロックの色: 済んだ段階は濃緑＋チェック、今の
        段階だけを core.stage_color の色で光らせる。経過は
        通常は段階の滞在時間だが、実行の段階だけは「最後の
        出力からの時間 (無音の長さ)」で測る (出力が続く限り
        緑寄り、無音が続くと黄 → 赤)。待機中 (段階なし) は
        段階ブロックが沈み、代わりに監視ブロックが青く光る
        (見張りが回っていれば。1.65)。非常停止中はパネルが
        赤系になり、大きな数字が「停止」になり、監視ブロッ
        クも赤く「停止」になる。"""
        self._flush_due_relay_acks()
        text = self._status_strip_text()
        self.status_strip.setText(text)
        self.status_strip.setToolTip(text)
        stage, stage_since = self._display_stage()
        ratio = 0.0
        color = None
        if stage is not None and stage_since is not None:
            if stage == "run" \
                    and self._run_last_output_at is not None:
                elapsed = time.monotonic() - self._run_last_output_at
            else:
                elapsed = time.monotonic() - stage_since
            ratio = core.stage_color_ratio(stage, elapsed)
            color = core.stage_color(stage, elapsed)
        floors = getattr(self, "_floors", None)
        if floors:
            # 段階が動いている間 (と非常停止中) だけ、既定の
            # 投げ先の階へ段階と数字を描く。待機中は全部の階を
            # 階ごとの描き方にする (2.23。見張っていない既定の
            # 階にカウントアップが出る実機報告への直し)。
            active_name = self._stage_floor_name() \
                if stage is not None or self._emergency_stopped \
                else None
            for name, floor in floors.items():
                if name == active_name:
                    self._render_floor_active(
                        floor, stage, color, ratio)
                else:
                    self._render_floor_idle(name, floor)
        if getattr(self, "_big_panel", None) is not None:
            item_text = self._big_item_text()
            self._big_item.setText(item_text)
            self._big_item.setToolTip(item_text)
            # パネルの赤系テーマは、非常停止の出入りだけで
            # 付け替える (毎秒当て直さない)。
            if self._big_panel_emergency != self._emergency_stopped:
                self._big_panel_emergency = self._emergency_stopped
                self._big_panel.setStyleSheet(self._big_panel_style(
                    self._emergency_stopped))
            self._update_pulse()

    def _stage_floor_name(self) -> str:
        """段階を描く階の名前 (2.15)。

        段階や連鎖が動いている間は、連鎖の出どころ (押した窓・
        ツールの出どころ・処理中の窓)、次に動いている項目の
        出どころ (item.ai) の階へ描く (2.46)。どちらも分から
        ない合図だけの連鎖や待機中は既定の投げ先の階 (2.21、
        元の「主の階」)。"""
        floors = getattr(self, "_floors", None) or {}
        if not floors:
            return ""
        if self._display_stage()[0] is not None \
                or getattr(self, "_running_item", None) is not None \
                or getattr(self, "_ai_filling", False) \
                or getattr(self, "_ai_sending", False):
            # 連鎖の出どころを先に見る (2.46)。DL のように項目が
            # まだ無い連鎖でも、押した窓・ツールの出どころ・
            # 処理中の窓が分かれば、その階へ描く (項目が無い
            # というだけで既定の階に描かれ、Copilot の DL が
            # Muse の階に出た実地の取り違えへの直し)。
            for ai in (getattr(self, "_dl_press_watch", None),
                       getattr(self, "_tool_auto_ai", None),
                       getattr(getattr(self, "_dispatch_state", None),
                               "name", None)):
                if ai and ai in floors:
                    return ai
            for item in (getattr(self, "_running_item", None),
                         getattr(self, "_display_item", None),
                         getattr(self, "_ack_pending_answer", None),
                         getattr(self, "_answer_pending_item", None)):
                ai = getattr(item, "ai", None)
                if ai and ai in floors:
                    return ai
        if self._ai_name in floors:
            return self._ai_name
        return next(iter(floors))

    def _render_floor_active(self, floor, stage, color, ratio):
        """動いている側の階へ段階と数字を描く (2.15)。

        段階ブロック・監視ブロック・数字の付け直しは、大きな
        進捗表示 (1.64) の作法そのままを、その階の部品へ
        当てる。"""
        blocks = floor["chips"]
        order = core.STAGE_ORDER
        current = order.index(stage) if stage in order else -1
        for index, key in enumerate(order):
            block = blocks[key]
            label = core.STAGE_LABELS[key]
            if self._emergency_stopped or current < 0 \
                    or index > current:
                block.setText(label)
                block.setStyleSheet(self._floor_block_style("idle"))
            elif index < current:
                block.setText(label + " ✓")
                block.setStyleSheet(self._floor_block_style("done"))
            else:
                block.setText(label)
                block.setStyleSheet(self._floor_block_style(
                    "now", color, ratio))
        watch = floor["watch"]
        watch_state = self._watch_block_state()
        if watch_state == "stopped":
            watch.setText("停止")
            watch.setStyleSheet(self._floor_block_style("stopped"))
        elif watch_state == "lit":
            watch.setText("監視")
            watch.setStyleSheet(self._floor_block_style("watch"))
        else:
            watch.setText("監視")
            watch.setStyleSheet(self._floor_block_style("idle"))
        self._refresh_big_number(floor["number"], floor["caption"])

    def _render_floor_idle(self, name, floor):
        """動いていない側の階を描く (2.15)。

        段階は全部沈める。見張っている階は監視ブロックを青く
        光らせ、見出しは監視中 (追いかけ中ならその旨)。見張っ
        ていない階は名前も薄くして、見出しに「待受なし」と
        出す。"""
        for key in core.STAGE_ORDER:
            block = floor["chips"][key]
            block.setText(core.STAGE_LABELS[key])
            block.setStyleSheet(self._floor_block_style("idle"))
        state = next(
            (s for s in self._watch_list if s.name == name), None)
        watching = state is not None and state.profile is not None
        watch = floor["watch"]
        watch.setText("監視")
        if self._emergency_stopped:
            watch.setStyleSheet(self._floor_block_style("idle"))
            floor["number"].setText("―")
            floor["caption"].setText("非常停止中")
        elif watching:
            watch.setStyleSheet(self._floor_block_style("watch"))
            # この窓の待機のカウントアップ (2.23)。起点は
            # 走査の適用で立てるのが本筋だが、起動直後は適用を
            # 通らない窓があり起点が立たない (実機報告)。
            # 候補が無く起点も無ければ、今を起点にする
            # (1.76 の捕捉と同じ作法、2.24)。
            if state.holder is not None:
                floor["number"].setText("―")
            else:
                if state.idle_since is None:
                    state.idle_since = time.monotonic()
                floor["number"].setText(core.format_elapsed(
                    time.monotonic() - state.idle_since))
            if time.monotonic() < state.chase_until:
                floor["caption"].setText("追いかけ中")
            else:
                floor["caption"].setText("監視中")
        else:
            watch.setStyleSheet(self._floor_block_style("idle"))
            floor["number"].setText("―")
            floor["caption"].setText("待受なし")
        check = floor.get("check")
        if check is not None:
            check.setStyleSheet(
                "font-size: 11px;" if watching else
                f"font-size: 11px; color: {self._theme()['muted']};")


    def _refresh_big_number(self, number_label, caption_label):
        """階の数字とその小さい見出しを付け直す (1.64、2.15 で階へ)。

        非常停止中は「停止」(赤、見出しは非常停止中)。実行中
        で進捗の申告があれば「2/3」(見出しは進捗)。実行中は
        経過、段階の途中はその段階の滞在時間、終わっていれば
        所要、まだ何も無ければ「―」(見出しは待機中、見張り
        が回っていれば監視中。1.65)。"""
        # 監視のカウントアップの起点 (1.76): 待機の起点
        # (_idle_since) は、待機の書き出しで記録され、待機
        # 以外の書き出し (候補の表示など) で消される。起動
        # 直後や候補が表示された後は、見張りが回っていても
        # 起点が無いことがあり、数字が「―」のままになる。
        # 捕捉できて見張りが回っているのに起点が無ければ、
        # 今を起点にして数え始める。
        if self._idle_since is None \
                and self._watch_block_state() == "lit":
            self._idle_since = time.monotonic()
        if self._emergency_stopped:
            value, caption = "停止", "非常停止中"
            style = ("font-size: 17px; font-weight: bold;"
                     " color: #e03131;")
        elif self._run_started_at is not None \
                and self._run_progress is not None:
            value = (f"{self._run_progress[0]}"
                     f"/{self._run_progress[1]}")
            caption = "進捗"
            style = "font-size: 17px; font-weight: bold;"
        elif self._run_started_at is not None:
            value = core.format_elapsed(
                time.monotonic() - self._run_started_at)
            caption = "経過"
            style = "font-size: 17px; font-weight: bold;"
        elif self._display_stage()[0] is not None:
            _ds, _dss = self._display_stage()
            value = core.format_elapsed(
                time.monotonic() - _dss)
            caption = core.STAGE_LABELS[_ds]
            style = "font-size: 17px; font-weight: bold;"
        elif self._watch_block_state() == "lit" \
                and self._idle_since is not None:
            value = core.format_elapsed(
                time.monotonic() - self._idle_since)
            caption = "監視中"
            style = "font-size: 17px; font-weight: bold;"
        elif self._last_run_seconds is not None:
            value = core.format_elapsed(self._last_run_seconds)
            caption = "所要"
            style = "font-size: 17px; font-weight: bold;"
        else:
            value = "―"
            caption = "監視中" \
                if self._watch_block_state() == "lit" else "待機中"
            style = "font-size: 17px; font-weight: bold;"
        number_label.setText(value)
        number_label.setStyleSheet(style)
        caption_label.setText(caption)


    def _big_item_text(self) -> str:
        """大きな進捗表示の対象行 (1.64)。

        実行中または直近の項目のファイル名＋概要。実行中は
        最後の出力行も添える。まだ何も実行していなければ、
        待っている旨だけ出す。"""
        item = self._display_item
        if item is not None:
            text = item.name
            if item.summary and item.summary != item.name:
                text += f" ― {item.summary}"
        elif self._phal_item:
            text = self._phal_item
        else:
            return "次のスクリプトを待っています"
        if self._run_started_at is not None and self._run_last_line:
            text += f" ― {self._run_last_line[:60]}"
        return text

    def _watch_block_state(self) -> str:
        """監視ブロックの状態 (1.65)。

        "stopped" は非常停止中 (赤く「停止」)。"lit" は、AI
        窓の見張りが回っていて (_loop_watching)、段階も自動
        化の連鎖も進んでいない待機状態 (青く「監視」)。それ
        以外 (連鎖の途中・見張りが回っていない) は "idle"
        (沈んだまま)。_loop_watching は連鎖の途中でも True
        になるので、連鎖のほうを先に見て落とす。"""
        if self._emergency_stopped:
            return "stopped"
        if self._display_stage()[0] is not None \
                or getattr(self, "_running_item", None) is not None \
                or getattr(self, "_ai_filling", False) \
                or getattr(self, "_ai_sending", False) \
                or getattr(self, "_diag_sending", False):
            return "idle"
        return "lit" if self._loop_watching() else "idle"

    def _loop_watching(self) -> bool:
        """AI 窓の見張り (ループ) が動作中か (1.64)。

        非常停止中は常に False。段階の途中・自動化の連鎖の
        途中はもちろん動作中。そうでなくても、投げ先が設定
        済みで、チャットの見張りに使う自動化 (DL・診断・
        リセット・再実行の合図) のどれかが入っていれば、
        見張りは回っている。全部切なら待機中で、明滅させ
        ない。"""
        if self._emergency_stopped:
            return False
        if self._stage is not None:
            return True
        if getattr(self, "_running_item", None) is not None \
                or getattr(self, "_ai_filling", False) \
                or getattr(self, "_ai_sending", False) \
                or getattr(self, "_diag_sending", False):
            return True
        if not getattr(self, "_ai_configured", False):
            return False
        flags = self._automation_flags()
        return bool(flags.get("auto_dl") or flags.get("auto_diag")
                    or flags.get("auto_reset")
                    or flags.get("auto_rerun"))

    def _floor_pulsing(self, name) -> bool:
        """その階が明滅するか (2.16、2.23 で全階同じ判定に)。

        非常停止中はどこも明滅しない。それ以外は、その階の
        窓が見張りに入っている (状態があり、プロファイルが
        割り当たっている) 間だけ明滅する。既定の投げ先の階
        だけ別判定だった作りは 2.23 でやめた (見張りを外した
        階が明滅し続ける実機報告への直し)。"""
        if self._emergency_stopped:
            return False
        floors = getattr(self, "_floors", None) or {}
        if not floors:
            return False
        state = next(
            (s for s in self._watch_list if s.name == name), None)
        return state is not None and state.profile is not None

    def _update_pulse(self):
        """階ごとの明滅の出入り (1.64、2.16 で階ごとへ)。

        明滅する階が1つでもあればタイマを回し、無ければ止めて
        全部の階の背景の層を透明へ戻す。_refresh_status_strip
        (1秒ごと) から呼ばれ、非常停止の付け替えもそちら経由
        でここへ届く。"""
        timer = getattr(self, "_pulse_timer", None)
        floors = getattr(self, "_floors", None)
        if timer is None or not floors:
            return
        if any(self._floor_pulsing(name) for name in floors):
            if not timer.isActive():
                timer.start(50)
        else:
            if timer.isActive():
                timer.stop()
            for floor in floors.values():
                floor["effect"].setOpacity(0.0)

    def _tick_pulse(self):
        """明滅の1コマ (1.64、2.16 で階ごとへ、2.25 で背景の
        層だけに)。周期 約3.4秒で、背景の層 (グリーン) の
        不透明度が滑らかに往復する。明滅しない階は透明な
        まま (背景なし)。"""
        floors = getattr(self, "_floors", None)
        if not floors:
            return
        phase = (time.monotonic() % 3.4) / 3.4
        ratio = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
        for name, floor in floors.items():
            floor["effect"].setOpacity(
                ratio * 0.55 if self._floor_pulsing(name) else 0.0)


    def _toggle_emergency_stop(self):
        """非常停止ボタンの仕事 (1.63)。

        押すと非常停止状態になる: 実行中なら既存の停止と同じく
        止め、自動実行の待ち列を空にし、以後は自動化 (DL・
        実行・回答・送信・合図への自動応答) が一切進まない。
        状態は大きな進捗表示 (赤系テーマ) と phal_status.json
        (state: stopped、stopped_reason: 非常停止) へ反映する。
        もう一度押すと解除し、止まっていた自動実行の連鎖を
        再開する。手動の操作は非常停止中もそのままできる。"""
        if self._emergency_stopped:
            self._emergency_stopped = False
            self.emergency_button.setText("非常停止")
            self.emergency_button.setStyleSheet(
                self._emergency_button_style(False))
            self._diag("非常停止を解除しました", ai="")
            self._set_note("非常停止を解除しました", "ok")
            self._publish_phal_status("idle", "非常停止を解除しました")
            self._refresh_status_strip()
            # 解除したら、止まっていた自動実行を進め直す。
            self._pump_auto_run()
            self._pump_register()
            return
        self._emergency_stopped = True
        self.emergency_button.setText("非常停止中（解除）")
        self.emergency_button.setStyleSheet(
            self._emergency_button_style(True))
        # 実行中なら、既存の「停止」と同じ持ち手で止める。
        self._stop_run()
        self._auto_run_queue.clear()
        self._diag("非常停止しました（自動化をすべて停止）", ai="")
        self._set_note(
            "非常停止中です。自動化はすべて止まっています", "error")
        self._publish_phal_status(
            "stopped", "非常停止中", stopped_reason="非常停止")
        self._refresh_status_strip()

    def _diag_status_label(self, status) -> str:
        """チェックの判定を、状態の列と同じ短い語にする。"""
        return {core.STATUS_OK: "OK", core.STATUS_WARN: "警告",
                core.STATUS_ERROR: "エラー"}.get(status, "不明")

    def _status_qr_text(self) -> str:
        """状態QR の中身を、今の機械の状態から組み立てる (2.09)。

        入れるのは版・状態・段階・手数・件数・エラーの種別・
        設定の入り切りだけ。ファイル名や中身は入れない (外へ
        運ぶ前提の文なので、core.build_status_qr_text の約束)。"""
        import script_runner_ai as sr_ai

        state_labels = {
            "idle": "待機中", "dl": "ダウンロード中",
            "run": "実行中", "answer": "回答中",
            "send": "送信中", "stopped": "停止",
        }
        stage_labels = {
            "dl": "DL", "run": "実行", "answer": "回答",
            "send": "送信",
        }
        link_label = ""
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            # 見張る窓の行は階のチェックと同じ並びにする
            # (2.14 の連携先の行を 2.23 でこの行へ一本化)。
            watch = sr_ai.sanitize_watch_profiles(section)
            profiles, active = sr_ai.sanitize_profiles(section)
            link_label = " ".join(
                ("☑" if entry["name"] in watch else "☐")
                + entry["name"]
                for index, entry in enumerate(profiles))
        except Exception:  # noqa: BLE001  読めなければなしとして出す
            pass
        last_failure = ""
        for item in self._items:
            run = item.last_run
            if run is None:
                continue
            if run.start_error or run.exit_code is None:
                last_failure = "起動失敗"
            elif run.timed_out:
                last_failure = "時間切れ"
            elif run.exit_code != 0:
                last_failure = f"終了コード{run.exit_code}"
            if last_failure:
                break
        return core.build_status_qr_text(
            PHAL_VERSION,
            state_labels.get(self._phal_state, self._phal_state),
            stage_labels.get(self._stage or "", "なし"),
            self._phal_step,
            len(self._items),
            sum(1 for item in self._items
                if item.last_run is not None),
            sum(1 for item in self._items
                if item.status == core.STATUS_ERROR),
            last_failure,
            self._automation_flags(),
            self._ai_name,
            self._emergency_stopped,
            link_label=link_label)

    def _show_status_qr(self):
        """状態QR のダイアログを出す (2.09)。

        QR の生成は diagnostics (segno) を遅れて import して
        借りる。作れなければ文だけのダイアログに倒す (文が
        読めれば、困りごとの大半は伝わる)。"""
        text = self._status_qr_text()
        dialog = QDialog(self)
        dialog.setWindowTitle("状態QR - PHAL")
        layout = QVBoxLayout(dialog)
        image_label = QLabel()
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        try:
            import diagnostics

            qr = diagnostics.make_qr(text)
            cells = len(qr.matrix)
            scale = max(2, 360 // (cells + 8))
            image = diagnostics.qr_to_qimage(qr, scale=scale)
            image_label.setPixmap(QPixmap.fromImage(image))
        except Exception:  # noqa: BLE001  QR が作れなければ文だけ
            image_label.setText("（QR コードを作れませんでした）")
        layout.addWidget(image_label)
        caption = QLabel(
            "スマホで読み取って、チャットへ貼ってください。"
            "中身は PHAL の状態だけで、仕事の内容は入っていません。")
        caption.setWordWrap(True)
        layout.addWidget(caption)
        text_view = QPlainTextEdit(text)
        text_view.setReadOnly(True)
        text_view.setMaximumHeight(180)
        layout.addWidget(text_view)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _diagnostics_text(self, request_id: str = "") -> str:
        """今の状態と直近の記録から、AI へ貼る診断の束を作る。

        「設定」の「診断をAIへ貼る」から呼ばれる (その窓の表示中
        プロファイルの入力欄へ入れるのは向こうの仕事)。
        request_id は自動応答のときだけ渡され、束の見出しに
        「[PHAL:診断 ID]」の形で入る (2.40)。"""
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
        holders = self._holders()
        if not holders:
            dl_line = "DL行: なし"
        else:
            dl_line = "DL行: 表示中（" + "、".join(
                holder.label for holder in holders) + "）"
            if self._dl_pressing:
                dl_line += "・押下中"
        chain = [
            dl_line,
            # 直近の走査 (1-57)。検出失敗や候補の抑制の切り分けに
            # 使う。組み立ては _note_dl_scan が持っている。窓ごと
            # に1行ずつ出す (2.08)。
            *[state.last_scan for state in self._watch_list],
            "実行中: " + (
                self._running_item.name
                if self._running_item is not None else "なし"),
            "回答・送信: " + (
                "回答を入れている最中" if self._ai_filling else
                "送信を押している最中" if self._ai_sending else "なし"),
            f"自動実行の待ち: {len(self._auto_run_queue)}件",
            self._loop_breakdown_line(),
            "開き直しの保留: " + (
                "あり" if self._restart_deferred else "なし"),
            # 窓位置と復元の照合の記録 (1.70)。
            "窓位置: " + self._window_position_line(),
        ]
        # 実行中の途中経過 (1.74): 出力の末尾を束へ入れる。
        # 長い実行の最中に診断を呼ばれたら、ここまで出ている
        # 分がそのまま読める。
        if self._running_item is not None and self._run_tail:
            tail = list(self._run_tail)[-12:]
            chain.append(
                f"実行中の出力（{self._run_lines}行まで到着、"
                f"末尾{len(tail)}行）:")
            for tail_line in tail:
                chain.append("　" + tail_line[:120])
        return core.build_diag_bundle(
            PHAL_VERSION, self._automation_flags(), profile_name,
            chain, self._diag_events, request_id=request_id)

    def _window_position_line(self) -> str:
        """診断用の窓位置1行 (1.70、1.72 で単純化)。現在と保存値。"""
        rect = self.frameGeometry()
        saved = getattr(self, "_saved_frame_rect", None)
        return (f"現在=({rect.x()},{rect.y()},"
                f"{rect.width()}x{rect.height()}) 保存={saved}")

    def _set_note(self, text: str, kind: str = ""):
        self._note_kind = kind
        self.note.setText(text)
        self.note.setStyleSheet(
            self._theme()["note_styles"].get(kind, ""))
        # 注記は次の動作で上書きされて消える。AI との議論や
        # バグの検証で要る情報がここだけに残って消えないよう、
        # 動作ログにも残す (2.00、ユーザーの指定)。空の消去と、
        # 直近に同じ文が記録済みの繰り返しは入れない。
        if text:
            try:
                recent = [e.get("text")
                          for e in self._log_events[-5:]]
                if text not in recent:
                    self._diag(text)
            except Exception:  # noqa: BLE001  ログは本体を止めない
                pass

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

    def _record_card_seen(self, name, profile_name) -> bool:
        """候補として見たファイル名と窓の対応を控える (2.38)。

        新規の目撃 (または窓が変わった目撃) なら True。同じ
        窓での再目撃は時刻だけ更新して False (再判定は不要)。"""
        return self._record_seen_in(
            self._card_seen, name, profile_name)

    def _record_card_seen_fp(self, fingerprint, profile_name) -> bool:
        """候補として見た先頭指紋と窓の対応を控える (2.39)。"""
        return self._record_seen_in(
            self._card_seen_fp, fingerprint, profile_name)

    @staticmethod
    def _record_seen_in(store, key, profile_name) -> bool:
        """目撃の控えへの記録 (名前版・指紋版の共通処理)。

        新規の目撃 (または窓が変わった目撃) なら True。同じ
        窓での再目撃は時刻だけ更新して False (再判定は不要)。
        控えが膨らんだら寿命切れを捨てる (store は書き換えず
        中身だけ入れ替える: 呼び側が辞書そのものを掴んでいる
        ため)。"""
        now = time.time()
        prev = store.get(key)
        if prev is not None and prev[0] == profile_name \
                and now - prev[1] < _CARD_SEEN_TTL_SEC:
            store[key] = (profile_name, now)
            return False
        store[key] = (profile_name, now)
        if len(store) > 200:
            cutoff = now - _CARD_SEEN_TTL_SEC
            for old in [k for k, v in store.items()
                        if v[1] < cutoff]:
                del store[old]
        return True

    def _card_seen_profile(self, name):
        """そのファイル名のカードを最後に見た窓の名前 (2.38)。

        控えが無い・古い (寿命超え) なら ""。"""
        return self._seen_profile_in(self._card_seen, name)

    def _card_seen_fp_profile(self, fingerprint):
        """その先頭指紋のカードを最後に見た窓の名前 (2.39)。"""
        return self._seen_profile_in(
            self._card_seen_fp, fingerprint)

    @staticmethod
    def _seen_profile_in(store, key):
        entry = store.get(key)
        if entry is None:
            return ""
        if time.time() - entry[1] > _CARD_SEEN_TTL_SEC:
            return ""
        return entry[0]

    def _take_dl_expectation(self, name, fingerprint="", summary=""):
        """到着したファイルに一致する DL の到着待ちを取り出す。

        一致しなければ None (待ちはそのまま残る)。到着順で
        結び付けると、別のダウンロード (他の AI の分を含む) が
        割り込んだときに印と出どころを取り違える (2.38)。
        照合はファイル名が基本で、カード型の候補 (名前が中身の
        冒頭でファイル名が無い) は先頭2行の指紋でも結ぶ (2.39)。
        指紋はカードの表示文から作るため、窓の描画で冒頭が潰れる
        とファイル側と一致しない。概要 (先頭コメントの一文) は
        表示でも残るので、概要でも結ぶ (2.47)。
        照合の内訳は診断に1行残す (2.48)。待ちが無いときは
        何も残さない (フォルダに直接届いたファイルで毎回出る
        と診断が流れるため)。"""
        entries = list(self._dl_awaiting)
        if not entries:
            return None
        matched = None
        matched_key = ""
        for index, entry in enumerate(self._dl_awaiting):
            expected = entry[4] if len(entry) > 4 else ""
            if _dl_name_matches(expected, name):
                matched_key = "名前"
                matched = self._dl_awaiting.pop(index)
                break
            exp_fp = entry[5] if len(entry) > 5 else ""
            if fingerprint and exp_fp and fingerprint == exp_fp:
                matched_key = "指紋"
                matched = self._dl_awaiting.pop(index)
                break
            exp_summary = entry[6] if len(entry) > 6 else ""
            if summary and exp_summary and _summary_matches(
                    exp_summary, summary):
                matched_key = "概要"
                matched = self._dl_awaiting.pop(index)
                break
        ai_name = entries[0][2] if len(entries[0]) > 2 else ""
        if matched is not None:
            self._diag(f"DL到着の照合: {name} は{matched_key}で一致",
                       ai=ai_name or None)
        else:
            first = entries[0]
            exp_name = first[4] if len(first) > 4 else ""
            exp_fp = first[5] if len(first) > 5 else ""
            exp_summary = first[6] if len(first) > 6 else ""
            self._diag(
                "DL到着の照合: 一致なし"
                f" ファイル名={name}"
                f" 指紋={' '.join(fingerprint.split())[:50] or '(空)'}"
                f" 概要={summary or '(空)'}"
                f" / 待ち{len(entries)}件"
                f" 名前={' '.join(exp_name.split())[:40] or '(空)'}"
                f" 指紋={' '.join(exp_fp.split())[:50] or '(空)'}"
                f" 概要={exp_summary or '(空)'}",
                ai=ai_name or None)
        return matched

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
            # 出どころ (2.04): 控えにあれば引き継ぎ、無くて起動
            # 後に現れたファイルなら、今連携している AI を付ける。
            # 起動前からあった分は分からないので付けない。
            item.ai = self._item_ai.get(item.name, "")
            if not item.ai and not self._is_launch_stat(
                    key, mtime, size):
                ai_name = getattr(self, "_ai_name", "")
                if ai_name:
                    item.ai = ai_name
                    self._item_ai[item.name] = ai_name
                    core.save_item_ai(self._item_ai)
            self._items.append(item)
            self._items_by_key[key] = item
            awaiting = self._take_dl_expectation(
                item.name, _file_head_fingerprint(path), item.summary)
            if awaiting is not None:
                # 押下に成功した DL のファイルが届いた (ファイル
                # 名が一致した分だけ、2.38)。自動で押した分なら、
                # 自動実行の対象範囲 (案3) の (a) として控える
                # (1.63)。
                if len(awaiting) > 3 and awaiting[3]:
                    self._auto_downloaded_keys.add(key)
                # 出どころは、押した窓にする (2.08。回答は出どころ
                # へ返るので、連携中の表示名で付けた分より優先)。
                watch_name = awaiting[2] if len(awaiting) > 2 else ""
                if watch_name and item.ai != watch_name:
                    item.ai = watch_name
                    self._item_ai[item.name] = watch_name
                    core.save_item_ai(self._item_ai)
                self._diag(f"DLの到着を確認: {item.name}",
                           ai=item.ai or None)
                # 到着した行をしばらく色付ける (2.07)。
                self._highlight_name = item.name
                self._highlight_until = time.monotonic() + 8.0
                # このダウンロードで開いたフライアウトを閉じる
                # (次の押下の邪魔になるため、1-67)。
                self._send_escape_to_ai(watch_name or None)
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
            # 上書きされた新しい中身は、自動で落とした当の中身では
            # ない (対象範囲 (a) の控えから外す、1.63)。
            self._auto_downloaded_keys.discard(key)
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

    def _poll_dl_arrivals(self):
        """スクリプト以外の DL 到着を確認する (2.53)。

        一覧の見回り (core.list_candidates) は .py / .ps1 /
        .phal しか拾わないので、通常ファイル (.txt 等) は項目に
        ならず、到着の照合 (_take_dl_expectation) に届かなかった
        (押せても「到着を確認できなかった」になっていた)。
        到着待ちがある間だけ、フォルダ直下を名前で照らして
        到着を確かめる。項目は作らない (実行の対象にはしない)。
        押す前からあった同名ファイルと取り違えないよう、更新
        時刻が押下の時刻より前のものは相手にしない。"""
        if not self._dl_awaiting:
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        try:
            entries = list(self.watch_folder.iterdir())
        except OSError:
            return
        present = set()
        for entry in entries:
            name = entry.name
            lowered = name.lower()
            if lowered.endswith(core.TARGET_EXTENSIONS):
                continue  # スクリプトは通常の見回りが扱う
            if lowered.endswith(core.PARTIAL_SUFFIXES):
                continue  # 書きかけ (.crdownload 等)
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            key = str(entry)
            present.add(key)
            hit = False
            for awaiting in self._dl_awaiting:
                expected = awaiting[4] if len(awaiting) > 4 else ""
                if not _dl_name_matches(expected, name):
                    continue
                pressed_wall = now_wall - (now_mono - awaiting[1])
                if stat.st_mtime < pressed_wall - 5.0:
                    continue  # 押す前からあった同名ファイル
                hit = True
                break
            if not hit:
                continue
            if self._dl_arrival_stable.get(key) != stat.st_size:
                self._dl_arrival_stable[key] = stat.st_size
                continue  # 大きさが落ち着くまで待つ
            self._dl_arrival_stable.pop(key, None)
            arrived = self._take_dl_expectation(
                name, _file_head_fingerprint(entry),
                core.extract_summary(entry))
            if arrived is None:
                continue
            watch_name = arrived[2] if len(arrived) > 2 else ""
            self._diag(f"DLの到着を確認: {name}",
                       ai=watch_name or None)
            # このダウンロードで開いたフライアウトを閉じる
            # (スクリプトの到着と同じ作法)。
            self._send_escape_to_ai(watch_name or None)
            self._publish_phal_idle(
                f"ダウンロードの到着を確認: {name}")
        for key in list(self._dl_arrival_stable):
            if key not in present:
                self._dl_arrival_stable.pop(key, None)

    def _poll_inner(self):
        self.restart_button.setEnabled(self._running_item is None)
        self._maybe_send_delayed_ack()
        self._pump_pending_answer()
        self._pump_pending_notice()
        # 追いかけモードの印と、到着した行の色付けの期限 (2.07)。
        self._refresh_chase_label()
        if self._highlight_name and \
                time.monotonic() > self._highlight_until:
            self._highlight_name = ""
            self._refresh_list()
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
            overflow = self._overflow_seen.get(key)
            if overflow is not None and key not in self._items_by_key:
                if overflow == (mtime, size) and \
                        len(self._items) >= self.config["max_items"]:
                    # 上限からあふれて外したファイルが、同じ中身の
                    # まま候補に戻ってきた (2.46)。新規扱いすると
                    # チェック結果の再掲と自動実行の判定が数秒
                    # ごとに繰り返されるので、空きが無いうちは
                    # 拾わない。変わっていれば控えを捨てて通常
                    # どおり扱う。
                    continue
                self._overflow_seen.pop(key, None)
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
                self._auto_downloaded_keys.discard(key)
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
            # あふれて外した事実を覚える (2.46)。次からの見回りで
            # 同じ中身なら新規扱いしない (上の候補ループ)。
            self._overflow_seen[tail.key] = (tail.mtime, tail.size)
            self._forget_pending_run(tail.key)
            changed = True
        if changed:
            self._refresh_list()
        # スクリプト以外のファイルの到着も確かめる (2.53)。
        self._poll_dl_arrivals()
        # DL の到着待ちが長引いていたら、「確認できなかった」
        # として診断に残す (押せたのにファイルが来ない、が
        # 画面の外からも分かるように)。
        if self._dl_awaiting:
            now_mono = time.monotonic()
            expired = [entry for entry in self._dl_awaiting
                       if now_mono - entry[1] > 180]
            if expired:
                self._dl_awaiting = [
                    entry for entry in self._dl_awaiting
                    if now_mono - entry[1] <= 180]
                for entry in expired:
                    self._diag(
                        f"DLの到着を確認できなかった: {entry[0]}")
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
        # ループ内訳のチェックの境目 (2.79)。
        if "detected" in self._loop_marks \
                and "checked" not in self._loop_marks:
            self._loop_marks["checked"] = time.monotonic()
            if elapsed_ms is not None:
                self._loop_marks["check_elapsed"] = elapsed_ms / 1000.0
        ms = f"（{elapsed_ms}ms）" if elapsed_ms is not None else ""
        self._diag(
            f"チェック: {item.name} → "
            f"{self._diag_status_label(result.status)}{ms}",
            ai=item.ai or None)
        self._restore_ran(item)
        # 見送りの控えとの行き来 (2.76)。チェック前に見送られて
        # いたら、ここで分かった sha256 で控えへ確定する。控えに
        # ある到着は項目に印を立てておく (表示と自動実行の門)。
        if item.skipped:
            if result.sha256:
                core.skipped_put(self._skipped, item.name,
                                 result.sha256)
                core.save_skipped(self._skipped)
        else:
            self._skip_if_user_skipped(item)
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
        if self._skip_if_user_skipped(item):
            return  # 持ち主が見送った到着は、自動では走らせない
        if item.auto_skip_executed:
            return  # 実行済みの中身と分かった分は、もう相手にしない
        if not item.auto_eligible:
            return
        if self._emergency_stopped:
            return  # 非常停止中は自動実行へ進めない (1.63)
        if not core.auto_run_in_scope(
                item.name, item.key in self._auto_downloaded_keys):
            # 対象範囲 (案3、1.63) の外: 自動で落としたファイル
            # でも、ファイル名にコマンド ID が付いたファイルでも
            # ない。表に載るだけで、知らせも出さない (止まった
            # 知らせも含む。門はこの先のチェック判定で不変)。
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
        if item.key not in self._auto_downloaded_keys:
            # 対象範囲 (b) (IDつきでフォルダに直接届いた分) は、
            # どこかの窓でカードとして見えていた分だけに限る
            # (2.38)。ダウンロードフォルダは他の AI と共有で、
            # 見張っていない相手のファイルまで ID だけで拾うと
            # 出どころを取り違えて実行し、結果も違う窓へ返す
            # (2026-11-17 実地: Copilot のファイルを Muse の
            # 分として実行)。カードを見た窓が出どころになる。
            seen_profile = self._card_seen_profile(item.name)
            if not seen_profile:
                # カード型の候補はファイル名で目撃を控えられない
                # ので、先頭指紋の目撃でも出どころを認める (2.39)。
                item_fp = _file_head_fingerprint(item.path)
                if item_fp:
                    seen_profile = self._card_seen_fp_profile(item_fp)
            if not seen_profile:
                if item.key not in self._origin_unknown_noted:
                    self._origin_unknown_noted.add(item.key)
                    if len(self._origin_unknown_noted) > 400:
                        self._origin_unknown_noted.clear()
                        self._origin_unknown_noted.add(item.key)
                    self._diag(
                        "自動実行を見送り（出どころ不明・カードが"
                        f"見えていない）: {item.name}")
                return
            if seen_profile != item.ai:
                item.ai = seen_profile
                self._item_ai[item.name] = seen_profile
                core.save_item_ai(self._item_ai)
        if self._admit_register(item):
            return  # 宣言つきはおどうぐばこへの登録に回す (2.37)
        if self._skip_if_executed(item):
            return  # 同じ中身を実行済み。自動では走らせない
        if item.ran:
            return  # 復元された実行済み。歯止めの控えが古くなって
            # いても、待ち列には入れない (二度走らせない)
        if item.key not in self._auto_run_queue:
            self._auto_run_queue.append(item.key)
            self._diag(f"自動実行の待ち列へ: {item.name}",
                       ai=item.ai or None)
        self._pump_auto_run()
        self._pump_register()

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
                f"自動実行を見送り（同じ内容を実行済み）: {item.name}",
                ai=item.ai or None)
        item.auto_skip_executed = True
        if item.key not in self._executed_skip_noted:
            self._executed_skip_noted.add(item.key)
            self._set_note(
                "同じ内容を実行済みのため、自動実行しませんでした"
                f"・{item.name}", "warn")
        return True

    def _skip_if_user_skipped(self, item: ScriptItem) -> bool:
        """持ち主が見送った到着なら、項目に印を付けて True (2.76)。

        判定は控え (名前と sha256 の組) で行う。手動の実行はここ
        では止めない (この判定を通るのは自動実行の入口と待ち列
        だけ)。"""
        sha = item.result.sha256 if item.result is not None else ""
        if not core.skipped_contains(self._skipped, item.name, sha):
            return False
        item.skipped = True
        return True

    def _skip_item(self, item: ScriptItem):
        """到着を見送る (2.76、「飛ばす」ボタンと右クリック)。

        自動実行の待ち列から外し、項目に印を付け、sha256 が
        分かっていれば控えにも残す (開き直しをまたぐ)。チェック
        前で sha256 がまだ無いときは、チェック完了時に
        _on_check_done が控えへ確定する。"""
        if item.ran or item is self._running_item:
            return
        item.skipped = True
        if item.key in self._auto_run_queue:
            self._auto_run_queue.remove(item.key)
        sha = item.result.sha256 if item.result is not None else ""
        if sha:
            core.skipped_put(self._skipped, item.name, sha)
            core.save_skipped(self._skipped)
        self._diag(f"自動実行を見送り（持ち主の指定）: {item.name}",
                   ai=item.ai or None)
        self._set_note(f"自動実行を見送りました・{item.name}", "ok")
        self._refresh_list()

    def _unskip_item(self, item: ScriptItem):
        """見送りを取り消す (2.76、右クリック)。

        印と控えを外し、自動実行の入口へ改めて通す (条件が合え
        ば待ち列に入り直す)。"""
        item.skipped = False
        sha = item.result.sha256 if item.result is not None else ""
        if sha:
            core.skipped_discard(self._skipped, item.name, sha)
            core.save_skipped(self._skipped)
        self._set_note(f"見送りを取り消しました・{item.name}", "ok")
        self._refresh_list()
        self._admit_auto_run(item)

    def _pump_auto_run(self):
        """自動実行の待ち列から、次の1件を始める。

        実行は直列に限る: 何か走っている間、または AI への回答・
        送信・受領応答が裏で動いている間は始めない (前の項目の
        連鎖が済んでから次へ進む)。待ち列の先頭から、条件が崩れた
        項目 (消えた・手で走らせた・中身が変わった等) は読み直して
        読み飛ばす。始められたら、その旨を note に残す。"""
        if self._emergency_stopped:
            return  # 非常停止中は自動実行を始めない (1.63)
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
            if item.skipped or self._skip_if_user_skipped(item):
                continue  # 持ち主が見送った。待ち列から外して進む
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
                self._diag(f"自動実行を開始: {item.name}",
                           ai=item.ai or None)
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

    def _entry_at_row(self, row: int):
        """表の行番号 → ("file", 項目) / ("command", 記録)。

        DL の特別行 (チャットで見つけた候補) が先頭にあるときは、
        その行はどちらの項目でもないので None を返す。並びは
        直近の _refresh_list が控えたものを使う。"""
        entries = getattr(self, "_row_entry_list", None)
        if entries is None:
            entries = [("file", item)
                       for item in self._visible_items()]
        index = row - len(self._holders())
        if 0 <= index < len(entries):
            return entries[index]
        return None

    def _item_at_row(self, row: int):
        """表の行番号 → ファイルの項目。コマンド行 (2.42) と
        DL の特別行は項目ではないので None を返す (ファイル側の
        操作は何もしない)。"""
        entry = self._entry_at_row(row)
        if entry is not None and entry[0] == "file":
            return entry[1]
        return None

    def _command_at_row(self, row: int):
        """表の行番号 → コマンドの記録 (2.42)。ファイル行と
        DL の特別行では None を返す。"""
        entry = self._entry_at_row(row)
        if entry is not None and entry[0] == "command":
            return entry[1]
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
        holders = self._holders()
        offset = len(holders)
        # ファイル行とコマンド行 (2.42) を時刻順に混ぜる。
        entries = [("file", item) for item in self._visible_items()]
        entries += [("command", record)
                    for record in self._command_history]
        entries.sort(
            key=lambda entry: (
                entry[1].mtime if entry[0] == "file"
                else float(entry[1].get("at") or 0)),
            reverse=True)
        self._row_entry_list = entries
        # 新しいアイテムが増えたら最上段へスクロールする
        # (2.67、持ち主の指定)。前回の描画で見えていたキーの
        # 集合と比べ、初めて出るキーがあれば「追加」とみなす。
        # 既存アイテムの状態変化だけの描き直しでは動かさない。
        # 選択は変えない (表示位置だけ動かす)。DL の特別行も
        # 対象に含める。
        current_keys = {("holder", holder.key) for holder in holders}
        for kind, entry in entries:
            current_keys.add(
                (kind, entry.key if kind == "file"
                 else str(entry.get("key") or "")))
        seen_keys = getattr(self, "_list_seen_keys", None)
        new_arrival = seen_keys is not None and bool(
            current_keys - seen_keys)
        self._list_seen_keys = current_keys
        with QSignalBlocker(self.table):
            self.table.setRowCount(len(entries) + offset)
            for holder_row, holder in enumerate(holders):
                self._fill_dl_row(holder_row, holder)
            selected_row = -1
            dup_shas = self._duplicate_shas()
            for index, entry in enumerate(entries):
                if entry[0] == "file":
                    self._fill_row(index + offset, entry[1],
                                   dup_shas)
                    key = entry[1].key
                else:
                    self._fill_command_row(index + offset, entry[1])
                    key = str(entry[1].get("key") or "")
                if key and key == selected:
                    selected_row = index + offset
            if selected_row >= 0:
                self.table.setCurrentCell(selected_row, 0)
            elif entries:
                # 選択が無い(または隠れた)ときは先頭の行を選んだことに
                # する。シグナルを止めているので、選択側の状態もここで
                # 合わせておく。
                self.table.setCurrentCell(offset, 0)
                first_kind, first = entries[0]
                self._selected_key = (
                    first.key if first_kind == "file"
                    else str(first.get("key") or ""))
                for item in self._items:
                    if item.key != self._selected_key:
                        item.confirm_armed = False
            elif holders:
                # ファイルの行は無いが、特別行だけはある。
                self.table.setCurrentCell(0, 0)
                self._selected_key = None
            else:
                # 出せる行が無い。隠れた項目が選択に残らないようにする。
                self._selected_key = None
        if new_arrival:
            # 選択セルの確定 (setCurrentCell) より後で戻す。
            # 先に戻すと、選択行へ寄せる動きに負ける。
            self.table.scrollToTop()
        self._fit_name_column()

    def eventFilter(self, obj, event):
        """表の大きさが変わったら、幅の余りを概要列へ配る (2.75)。

        eventFilter は表が Resize を処理する前に呼ばれるので、
        その場で viewport の幅を見ると1回前の幅になる (2.80、
        レビュー中1)。処理の後に配り直すよう、タイマで遅らせる。"""
        if obj is self.table and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self._distribute_table_width)
        return super().eventFilter(obj, event)

    def _on_section_resized(self, index, _old, _new):
        """名前列を利用者がドラッグしたら印を立てる (2.80、中3)。

        コード側の自動合わせ (_fit_name_column) の最中は立てない。"""
        if index == COL_NAME \
                and not getattr(self, "_fitting_columns", False):
            self._name_column_user_sized = True

    def _distribute_table_width(self):
        """列幅の合計を表の幅に合わせ、差分を概要列で受ける (2.75)。

        概要を Stretch にしていた頃は、概要の境界をドラッグして
        も幅が戻って直感的でなかった。全列 Interactive にした
        代わり、余りの配分は表 (≒窓) の大きさが変わったとき
        だけ、ここで行う。概要が下限に達したら、それ以上は
        縮めず横スクロールに任せる。"""
        header = self.table.horizontalHeader()
        viewport = self.table.viewport().width()
        if viewport <= 0:
            return
        total = sum(header.sectionSize(col)
                    for col in range(self.table.columnCount()))
        delta = viewport - total
        if delta == 0:
            return
        current = header.sectionSize(COL_SUMMARY)
        new_width = max(SUMMARY_COLUMN_MIN_WIDTH, current + delta)
        if new_width != current:
            header.resizeSection(COL_SUMMARY, new_width)

    def _fit_name_column(self):
        """ファイル名の列幅を、表示中の名前の一番長いものに合わせる
        (上限つき)。

        ResizeToContents だと長い名前が列を際限なく押し広げるので、
        この列だけ Interactive にして幅をここで決める。上限を超える
        名前は省略表示になり (全文はツールチップ)、余った幅は伸縮
        する概要の列が受け取る。窓幅を変えても伸びるのは概要だけ。

        保存済みの列幅があるときは、利用者の幅を優先して何も
        しない (2.72)。無いときは、その他の列も初回だけ中身に
        合わせる。"""
        if self.config.get("column_widths"):
            return
        header = self.table.horizontalHeader()
        # その他の列の初回の測定は、最初に行が入ったときにする
        # (2.80、レビュー中3)。一覧が空のうちに測ると、見出しの
        # 幅だけで決まってしまう。
        if not getattr(self, "_other_columns_fitted", False) \
                and self._visible_items():
            self._other_columns_fitted = True
            for col in (COL_TIME, COL_ID, COL_AI, COL_HASH,
                        COL_STATUS, COL_RUN):
                self.table.resizeColumnToContents(col)
            # ボタンがまだ無い行ばかりだと、実行ボタンの列が
            # 測れず潰れた幅になる (2.74、実機報告 2026-11-18)。
            if header.sectionSize(COL_RUN) < core.RUN_COLUMN_MIN_WIDTH:
                header.resizeSection(
                    COL_RUN, core.RUN_COLUMN_MIN_WIDTH)
        font = self.table.font()
        font.setBold(True)  # ファイル名のセルは太字で描かれる
        metrics = QFontMetrics(font)
        widest = metrics.horizontalAdvance("ファイル名")
        for item in self._visible_items():
            widest = max(widest, metrics.horizontalAdvance(item.name))
        for holder in self._holders():
            widest = max(widest,
                         metrics.horizontalAdvance(holder.label))
        for record in self._command_history:
            label = core.COMMAND_KIND_LABELS.get(
                str(record.get("kind") or ""), "")
            if label:
                widest = max(widest,
                             metrics.horizontalAdvance(label))
        width = min(widest + 24, NAME_COLUMN_MAX_WIDTH)
        # 利用者が名前列をドラッグしていたら、その幅を優先する
        # (2.80、レビュー中3)。
        if getattr(self, "_name_column_user_sized", False):
            return
        if header.sectionSize(COL_NAME) != width:
            self._fitting_columns = True
            try:
                header.resizeSection(COL_NAME, width)
            finally:
                self._fitting_columns = False

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
        ai_cell = QTableWidgetItem(item.ai or "")
        if item.ai:
            ai_cell.setToolTip(f"出どころ: {item.ai}")
        self.table.setItem(row, COL_AI, ai_cell)
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
        # 「飛ばす」は実行前の行にだけ出す (2.76)。見送り済み・
        # 実行中・実行済みの行には出さない。
        want_skip = (not item.ran and not item.skipped
                     and item is not self._running_item)
        if spec is None and not want_skip:
            if existing is not None:
                self.table.removeCellWidget(row, COL_RUN)
        elif not self._row_buttons_match(existing, item, spec,
                                         want_skip):
            self.table.setCellWidget(
                row, COL_RUN,
                self._make_row_buttons(item, spec, want_skip))
        # 到着したての行の色付け (2.07)。期限は _poll_inner が見て、
        # 切れたら描き直して消す。
        if self._highlight_name and \
                item.name == self._highlight_name and \
                time.monotonic() <= self._highlight_until:
            brush = QBrush(QColor(self._theme()["detect_bg"]))
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is not None:
                    cell.setBackground(brush)

    def _dl_button_enabled(self) -> bool:
        """DL の特別行のボタンを押せるか。実行・回答・送信・DL の
        どれかが動いているあいだは押せない (裏方が混ざらない)。"""
        return self._running_item is None \
            and not self._ai_filling and not self._ai_sending \
            and not self._dl_pressing

    def _fill_command_row(self, row: int, record: dict):
        """コマンド行 (2.42) を1行描く。

        列の使い方はファイル行と同じ: 時刻、依頼 ID、合図の種類
        (名前の列)、出どころ AI、(ハッシュは空)、概要、状態。
        実行ボタンは付けない (F5 の対象にもならない)。"""
        theme = self._theme()
        at = float(record.get("at") or 0)
        when = datetime.fromtimestamp(at) if at else None
        time_cell = QTableWidgetItem(
            when.strftime("%H:%M") if when else "")
        time_cell.setData(Qt.ItemDataRole.UserRole,
                          str(record.get("key") or ""))
        self.table.setItem(row, COL_TIME, time_cell)
        self.table.setItem(
            row, COL_ID,
            QTableWidgetItem(str(record.get("request_id") or "")))
        label = core.COMMAND_KIND_LABELS.get(
            str(record.get("kind") or ""), "コマンド")
        name_cell = QTableWidgetItem(label)
        font = name_cell.font()
        font.setBold(True)
        name_cell.setFont(font)
        self.table.setItem(row, COL_NAME, name_cell)
        self.table.setItem(
            row, COL_AI,
            QTableWidgetItem(str(record.get("ai") or "")))
        self.table.setItem(row, COL_HASH, QTableWidgetItem(""))
        self.table.setItem(
            row, COL_SUMMARY,
            QTableWidgetItem(str(record.get("summary") or "")))
        status = str(record.get("status") or "skipped")
        status_text = core.COMMAND_STATUS_LABELS.get(
            status, "見送り")
        reason = str(record.get("reason") or "")
        if reason:
            status_text = f"{status_text}（{reason}）"
        color_key = {"answered": core.STATUS_OK,
                     "skipped": core.STATUS_WARN,
                     "pending": "checking"}.get(
                         status, core.STATUS_WARN)
        status_cell = QTableWidgetItem(status_text)
        status_cell.setForeground(
            QColor(theme["status"].get(
                color_key,
                theme["status"][core.STATUS_UNKNOWN])))
        self.table.setItem(row, COL_STATUS, status_cell)
        # 実行ボタンは付けない。使い回しの行に残っていたら外す。
        if self.table.cellWidget(row, COL_RUN) is not None:
            self.table.removeCellWidget(row, COL_RUN)

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
        # AI の列には、候補が出た窓の名前を出す (2.08)。
        self.table.setItem(row, COL_AI,
                           QTableWidgetItem(holder.watch or ""))
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
                and existing.property("phal_dl_key")
                == self._dl_row_key(holder)
                and existing.isEnabled() == enabled):
            self.table.setCellWidget(row, COL_RUN,
                                     self._make_dl_button(holder))

    def _dl_row_key(self, holder: _DlCandidate):
        """特別行のボタンの同一判定 (窓の名前 + 候補の key、2.08)。

        2つの窓で同じ候補 (名前+クラス) が出うるので、窓も含める。"""
        return f"{holder.watch}\n{holder.key}"

    def _make_dl_button(self, holder: _DlCandidate):
        button = QPushButton("DL")
        button.setEnabled(self._dl_button_enabled())
        button.setProperty("phal_dl_key", self._dl_row_key(holder))
        state = self._watches.get(holder.watch or "")
        # ファイルの行と同じく pressed で始める (作り直し対策)。
        button.pressed.connect(
            lambda state=state: self._press_download(
                auto=False, state=state))
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
        if item.skipped and not item.ran:
            return "見送り"
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
            button.pressed.connect(
                lambda it=item: self._answer_to_ai(it, manual=True))
        elif mode == "send":
            button.pressed.connect(lambda it=item: self._send_to_ai(it))
        elif mode == "rerun":
            button.pressed.connect(lambda it=item: self._rerun_item(it))
        else:
            button.pressed.connect(lambda it=item: self._start_run(it))
        return button

    def _row_buttons_match(self, existing, item, spec, want_skip):
        """行のボタン類が今の項目・姿と一致するか (2.76)。

        _fill_row が作り直しを避けるための見比べ。実行ボタンだけ
        の頃の条件 (項目・押せるか・目立たせ・役割・テーマ) に、
        「飛ばす」の有無を足した。"""
        if existing is None:
            return False
        if existing.property("phal_item_key") != item.key:
            return False
        if bool(existing.property("phal_skip")) != want_skip:
            return False
        if existing.property("phal_theme") \
                != ("dark" if self._dark_mode else "light"):
            return False
        if spec is None:
            return not existing.property("phal_mode")
        return (existing.property("phal_mode") == spec[2]
                and bool(existing.property("phal_enabled")) == spec[0]
                and bool(existing.property("phal_accent")) == spec[1])

    def _make_row_buttons(self, item, spec, want_skip):
        """行のボタン類を作る (2.76)。

        「飛ばす」が要らない行は、これまでどおり実行ボタン単体。
        要る行は、実行ボタン (あれば) と「飛ばす」を横に並べた
        入れ物にする。押した瞬間に始める作法 (pressed) は実行
        ボタンと同じ。"""
        theme_name = "dark" if self._dark_mode else "light"
        run_button = self._make_run_button(item) \
            if spec is not None else None
        if not want_skip:
            run_button.setProperty("phal_enabled", spec[0])
            run_button.setProperty("phal_skip", False)
            return run_button
        skip_button = QPushButton("飛ばす")
        skip_button.setToolTip(
            "この到着を自動実行しない（手動では実行できます）")
        skip_button.pressed.connect(
            lambda it=item: self._skip_item(it))
        if run_button is None:
            skip_button.setProperty("phal_item_key", item.key)
            skip_button.setProperty("phal_mode", "")
            skip_button.setProperty("phal_enabled", True)
            skip_button.setProperty("phal_accent", False)
            skip_button.setProperty("phal_skip", True)
            skip_button.setProperty("phal_theme", theme_name)
            return skip_button
        run_button.setProperty("phal_enabled", spec[0])
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(run_button, 1)
        layout.addWidget(skip_button)
        box.setProperty("phal_item_key", item.key)
        box.setProperty("phal_mode", spec[2])
        box.setProperty("phal_enabled", spec[0])
        box.setProperty("phal_accent", spec[1])
        box.setProperty("phal_skip", True)
        box.setProperty("phal_theme", theme_name)
        return box

    def _on_selection_changed(self, row: int, *_rest):
        if row < len(self._holders()):
            return  # DL の特別行は選択の状態を持たない (ボタンだけが仕事)
        record = self._command_at_row(row)
        if record is not None:
            # コマンド行 (2.42): 実行の対象にはならず、下半分へ
            # 記録した応答文を出すだけ。
            self._selected_key = str(record.get("key") or "")
            for item in self._items:
                item.confirm_armed = False
            self._refresh_list()
            self._show_lower_for_command(record)
            return
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
        # 見送り (2.76)。実行前の行だけに出す。
        if not item.ran and item is not self._running_item:
            if item.skipped:
                unskip_action = menu.addAction("見送りを解除")
                unskip_action.triggered.connect(
                    lambda _c=False, it=item: self._unskip_item(it))
            else:
                skip_action = menu.addAction("自動実行を飛ばす")
                skip_action.triggered.connect(
                    lambda _c=False, it=item: self._skip_item(it))
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

    def _show_lower_for_command(self, record: dict):
        """コマンド行を選んだとき、下半分へ応答文を出す (2.42)。

        応答文が無い記録 (リセットなど) は、状態と理由で埋める。"""
        if self._running_item is not None:
            return
        label = core.COMMAND_KIND_LABELS.get(
            str(record.get("kind") or ""), "コマンド")
        self.result_caption.setText(f"応答・{label}")
        response = str(record.get("response") or "")
        if response:
            self.output.setPlainText(response)
            return
        status = core.COMMAND_STATUS_LABELS.get(
            str(record.get("status") or ""), "")
        reason = str(record.get("reason") or "")
        lines = [line for line in (status, reason) if line]
        self.output.setPlainText("\n".join(lines))

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

    def _rerun_item(self, item: ScriptItem, run_args=None,
                    request_id=""):
        """「再実行」(行のボタン・右クリックのメニューから)。前回
        の結果との差分だけを結果文にして、もう一度実行する (1.56)。

        対象はファイルがまだある行だけ。無くなっていれば、その
        旨をステータスに出して止める (チェックにも実行にも
        進まない)。実行そのものは通常の経路 (_start_run) を使う。
        明示の手動操作なので、実行済みハッシュの自動実行ガード
        には掛からず、同じ中身でも必ず走る。run_args (1.97) は
        合図から来た引数の並びで、スクリプトの sys.argv[1:] に
        入る。request_id (2.41) は合図から来た依頼 ID で、結果文
        の見出しに併記される (手動の再実行では空)。"""
        if self._running_item is not None:
            return
        if not item.path.exists():
            self._set_note(
                "ファイルが無くなっているため、再実行できません", "error")
            return
        self._start_run(item, rerun=True, run_args=run_args,
                        request_id=request_id)

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
                   rerun: bool = False, run_args=None,
                   request_id: str = ""):
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
                f"中身が変わっていたので実行せず再チェック: {item.name}",
                ai=item.ai or None)
            self._set_note(
                "中身が変わっていたので、チェックからやり直しました", "warn")
            return
        # 宣言つきファイルは、再実行・F5 などの明示の実行でも
        # 本体を実行せず、おどうぐばこへの登録に回す (2.44)。
        # 自動実行の経路では _admit_auto_run が先に振り分けるが、
        # 出どころゲートで見送られた分はそちらを通らないため、
        # 実行の入口でも判定する。合図からの再実行は、開始時に
        # 残したコマンド記録をここで確定させる (実行の完了側が
        # もう来ないため)。
        if self._admit_register(item):
            if request_id:
                self._finish_command(
                    "rerun", ok=True,
                    response="宣言つきファイルのため、実行せず"
                             "道具登録へ回しました（結果は道具登録"
                             "の応答で返ります）")
            self._set_note(
                "宣言つきファイルのため、実行せず道具登録へ"
                "回しました", "ok")
            return
        # 自動実行 (auto=True は _pump_auto_run からだけ) のときは、
        # 受領応答の義務だけを控える (1-51、2.66 で遅延化)。実際に
        # 出すのは、実行が ACK_DELAY_SECONDS を超えても続いている
        # ときだけで、見回りの _maybe_send_delayed_ack が担う。
        # 手動の実行では送らない。
        if auto and self._automation_flags()["auto_ack"]:
            self._ack_due_item = item
        self._running_item = item
        self._running_rerun = rerun
        self._running_args = [str(a) for a in (run_args or [])]
        self._running_request_id = str(request_id or "")
        self._run_started_at = time.monotonic()
        if "detected" in self._loop_marks:
            self._loop_marks["run_start"] = self._run_started_at
            self._loop_marks["name"] = item.name
        self._diag(f"実行開始: {item.name}", ai=item.ai or None)
        # 実行手数を数えて、モニタ表示用の状態を出す。
        self._phal_step += 1
        self._display_item = item
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
        # 逐次取り込み (1.63): 出力を行単位で受けて、大きな
        # 進捗表示の材料にする
        # 材料 (行数・最後の行・無音の長さ・進捗) を差し替える。
        self._run_handle.on_output = self._note_run_output
        self._run_lines = 0
        self._run_last_line = ""
        self._run_last_output_at = None
        self._run_progress = None
        self._run_tail = []
        self.result_caption.setText(f"実行中・{item.name}")
        self.output.setPlainText("実行中…")
        self._set_note("")
        self.stop_button.setVisible(True)
        self._refresh_list()
        self._refresh_status_strip()

        import threading

        def work():
            # try で包む (2.69、レビュー高5)。ここだけ包まれて
            # いないと、例外でスレッドが黙って死に、_running_item
            # が残ったまま自動化が全部止まる。失敗は起動でき
            # なかった形の結果として完了側へ渡す。
            try:
                run = core.execute_script(
                    item.path, self.config, self._run_handle,
                    expected_sha256=item.result.sha256,
                    args=self._running_args)
            except Exception as e:  # noqa: BLE001  完了側で報告する
                run = core.RunResult(
                    name=item.name, path=str(item.path),
                    start_error=f"実行の裏方で例外: {e}")
            self._run_signals.done.emit(run)

        self._run_thread = threading.Thread(
            target=work, daemon=True, name="script-runner-run")
        self._run_thread.start()

    def _note_run_output(self, kind, line):
        """実行中の出力の逐次取り込み (1.63、core の on_output)。

        実行スレッドから呼ばれる。表示の材料を差し替えるだけ
        で、Qt には触らない (付け直しは表示のタイマが1秒ごとに
        行う)。進捗申告の行 (phal_lib.progress の「進捗：N/M」)
        は進捗として拾い、通常の出力行数には数えない (結果文
        にはそのまま残る)。最後の出力時刻は標準出力・エラー
        出力のどちらでも差し替える (無音の長さは「音が無い」
        ことなので、両方を数える)。"""
        self._run_last_output_at = time.monotonic()
        # 末尾の控え (1.74)。診断の束が実行の途中経過として
        # 読む。標準・エラーとも時系列で混ぜて残す。
        self._run_tail.append(line[:200])
        if len(self._run_tail) > 30:
            del self._run_tail[:-30]
        if kind == "stdout":
            progress = core.parse_progress_line(line)
            if progress is not None:
                self._run_progress = progress
                return
            self._run_lines += 1
        self._run_last_line = line

    def _stop_run(self):
        if self._run_handle is not None:
            self._run_handle.stop()

    def _on_run_done(self, run: core.RunResult):
        self._loop_mark("run_done")  # ループ内訳の境目 (2.79)
        item = self._running_item
        self._running_item = None
        was_rerun = self._running_rerun
        self._running_rerun = False
        run_args = self._running_args
        self._running_args = []
        run_request_id = self._running_request_id
        self._running_request_id = ""
        self._run_handle = None
        self._run_started_at = None
        self._run_last_output_at = None
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
            f"（{run.seconds:.1f}秒）{flags}",
            ai=(item.ai or None) if item is not None else None)
        self._note_phal_run(run, core.parse_command_id(run.name))
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
            run, self.config, command_id,
            request_id=run_request_id)
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
                full_output_path=full_output_path,
                request_id=run_request_id)
        else:
            text = core.build_result_text(
                run.name, text_run, self.config,
                command_id=command_id,
                full_output_path=full_output_path,
                request_id=run_request_id)
        if was_rerun and run_args:
            # 引数つき再実行 (1.97) は、見出しの直後に引数を
            # 明示する (何を変えて回したかが結果だけから分かる
            # ように)。
            first, sep, rest = text.partition("\n")
            text = first + "\n引数：" + " ".join(run_args) \
                + (sep + rest if sep else "")
        if item is not None:
            item.last_result_text = text
            body = core.build_result_body(
                run.name, text_run, self.config,
                command_id=command_id,
                full_output_path=full_output_path,
                request_id=run_request_id)
            core.last_results_put(
                self._last_results, self._rerun_key(item),
                core.result_diff_source(body))
            core.save_last_results(self._last_results)
        if was_rerun and run_request_id:
            # 合図からの再実行は、コマンド履歴の記録もここで
            # 確定させる (2.42)。応答文は結果文そのもの。
            self._finish_command("rerun", ok=True, response=text)
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
        # 受領応答の送信や中継の送信がまだ裏で動いていれば、
        # 入力欄の取り合いを避けて、回答はその完了側
        # (_on_ack_done / _on_relay_auto_done) へ預ける。
        # 非常停止中は自動の回答へ進まない (1.63)。
        if item is not None and not self._emergency_stopped \
                and self._automation_flags()["auto_answer"]:
            if self._ack_sending or self._relay_sending:
                self._ack_pending_answer = item
            else:
                self._answer_to_ai(item)
        # 自動実行の待ち列があれば、次を始める (回答の投入が裏で
        # 始まっていたら _pump_auto_run 側で見合わせて、投入の
        # 終わりで改めて呼ばれる)。
        self._pump_auto_run()
        self._pump_register()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- AI へ回答 ----------------
    def _resolve_ai_target(self):
        """PHAL 自前の設定 (script_runner 節のプロファイル) から、
        AI の投げ先を解決する。

        戻り値は (設定済みか, copilot_io へ渡す profile)。profile は
        script_runner_ai.build_profile が作る辞書。窓を特定できる
        情報が無ければ「設定されていない」として (False, None) を
        返す。設定は毎回ファイルから取り直す (「設定」で変えたら、
        次に押したときから効く)。script_runner_ai は、壊れても PHAL が
        起動できなくならないよう、ここで遅れて import する。"""
        # 合図の処理中 (2.08) は、その合図が出た窓が投げ先になる
        # (見張りの結果の処理 _on_dl_checked が立てて、処理が
        # 終われば下げる。応答は出どころの窓へ返すため)。
        dispatch = getattr(self, "_dispatch_state", None)
        if dispatch is not None and dispatch.profile is not None:
            return (True, dispatch.profile)
        import script_runner_ai as sr_ai

        all_settings = settings_module.load_settings(self.settings_path)
        section = all_settings.get("script_runner") or {}
        fields = sr_ai.active_profile_fields(section)
        if not sr_ai.is_configured(fields):
            return (False, None)
        return (True, sr_ai.build_profile(fields))

    def _resolve_profile_by_name(self, name):
        """プロファイル名で投げ先を解決する (2.08)。

        _resolve_ai_target と同じ形 (設定済みか, profile) を返す。
        名前が無い・見つからない・未設定なら (False, None)。"""
        if not name:
            return (False, None)
        import script_runner_ai as sr_ai

        all_settings = settings_module.load_settings(self.settings_path)
        section = all_settings.get("script_runner") or {}
        profiles, _active = sr_ai.sanitize_profiles(section)
        for entry in profiles:
            if entry["name"] == name:
                fields = entry["target"]
                if not sr_ai.is_configured(fields):
                    return (False, None)
                return (True, sr_ai.build_profile(fields))
        return (False, None)

    def _resolve_item_target(self, item):
        """項目の出どころの AI へ返すための解決 (2.08)。

        出どころ (item.ai) が付いていて、そのプロファイルが
        設定済みならそちらを優先する。出どころが無い・見つから
        ないときは従来どおり有効なプロファイルにする。"""
        ai_name = getattr(item, "ai", "") or ""
        if ai_name:
            try:
                configured, profile = self._resolve_profile_by_name(
                    ai_name)
            except Exception:  # noqa: BLE001  解決できなければ有効側
                configured, profile = False, None
            if configured:
                return (True, profile)
        return self._resolve_ai_target()

    def _refresh_send_ready(self) -> bool:
        """送信ボタンの目印が設定済みかを解き直して控える。

        ボタンの出し分け (_run_button_spec) は行ごとに呼ばれるので、
        設定ファイルの読み直しはここに寄せ、タイミングは窓の起動・
        「設定」の保存・回答の開始の3か所だけにする。"""
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

    def _open_odougubako(self):
        """おどうぐばこの一覧ウィンドウを開く (2.37)。

        本体 (odougubako_gui) は実行時にだけ読み込む。ファイルが
        無い設置では、その旨を知らせるだけで本体は止めない。"""
        try:
            import odougubako_gui
        except Exception as e:  # noqa: BLE001
            self._set_note(
                f"おどうぐばこを開けませんでした（{e}）", "error")
            return
        if self._odougubako_window is None:
            self._odougubako_window = odougubako_gui.OdougubakoWindow()
        else:
            refresh = getattr(self._odougubako_window, "refresh", None)
            if callable(refresh):
                refresh()
        self._odougubako_window.show()
        self._odougubako_window.raise_()
        self._odougubako_window.activateWindow()

    def _open_ai_window(self):
        """「設定」画面を開く (2.01 でタブ化、2.02 で入口は
        このボタン1つに)。"""
        self._open_settings("ai")

    def _open_settings(self, initial_tab):
        """PHAL の設定ウィンドウを開く (2.01)。

        タブは 共通設定 / AI別設定 / 動作ログ / 自動化設定 /
        ツール設定。入口のボタンで初期タブが変わるだけで、
        OK で閉じたら全タブ分を script_runner 節へまとめて
        保存する (ai_profiles には ai_target の同期も含む。
        設定画面の実体と欄の整えは script_runner_ai)。"""
        import script_runner_ai as sr_ai

        all_settings = settings_module.load_settings(self.settings_path)
        section = all_settings.get("script_runner") or {}
        profiles, active = sr_ai.sanitize_profiles(section)
        folder = core.tools_folder()
        folder_tools = []
        if folder.is_dir():
            folder_tools = sorted(
                core.normalize_tool_name(p.name)
                for p in folder.iterdir()
                if p.is_file()
                and p.suffix.lower() in core.TOOL_FOLDER_EXTS)
        dialog = sr_ai.SettingsDialog(
            profiles[active]["target"], self,
            profiles=profiles, active_index=active,
            automation={
                **sr_ai.sanitize_automation(section),
                "ack_delay_seconds": sr_ai.sanitize_ack_delay(section),
            },
            common={
                "watch_text": str(
                    core.resolve_watch_folder(section)),
                "watch_folder": section.get("watch_folder") or "",
                "work_folder": section.get("work_folder") or "",
                "dark_mode": bool(section.get("dark_mode")),
                "summary_mode": bool(
                    section.get("summary_mode", True)),
                "timeout_seconds": section.get(
                    "timeout_seconds", 120),
                "intro_prompt": section.get("intro_prompt") or "",
            },
            tools_data={
                "names": core.sanitize_cli_tools(
                    section.get("cli_tools")),
                "paths": core.sanitize_cli_tool_paths(
                    section.get("cli_tool_paths")),
                "usages": core.sanitize_cli_tool_usages(
                    section.get("cli_tool_usages")),
                "folder_tools": folder_tools,
            },
            diag_text_provider=self._diagnostics_text,
            error_color=self._theme()["dialog_error"],
            intro_text_provider=self._intro_auto_text,
            tools_folder_opener=self._open_tools_folder,
            tool_searcher=core.search_cli_tool,
            initial_tab=initial_tab,
            watch_names=sr_ai.sanitize_watch_profiles(section))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updates = sr_ai.profiles_settings(
            dialog.profiles(), dialog.active_index())
        updates["ai_watch_profiles"] = dialog.watch_names()
        updates.update(dialog.automation())
        updates["intro_prompt"] = dialog.intro_prompt()
        updates.update(dialog.common_values())
        names, paths, usages = dialog.tools()
        updates.update({
            "cli_tools": core.sanitize_cli_tools(names),
            "cli_tool_paths": core.sanitize_cli_tool_paths(paths),
            "cli_tool_usages": core.sanitize_cli_tool_usages(
                usages),
        })
        settings_module.update_settings_section(
            "script_runner", updates, self.settings_path)
        # 共通設定のうち、窓が手元の控え (self.config) から読む
        # ものはその場で差し替える (ダークモードは見た目にも
        # 反映する)。
        self.config["timeout_seconds"] = updates["timeout_seconds"]
        self.config["summary_mode"] = updates["summary_mode"]
        # 監視フォルダが変わったら、その場で見張る先を切り替える
        # (2.02)。作業フォルダは使うたびに設定ファイルから
        # 読むので、保存だけで効く。
        self.config["watch_folder"] = updates.get("watch_folder") or ""
        self.watch_folder = core.resolve_watch_folder(self.config)
        self.folder_caption.setText(self._caption_html())
        self._refresh_ai_label()
        if bool(updates["dark_mode"]) != self._dark_mode:
            self.dark_box.setChecked(bool(updates["dark_mode"]))
        self._refresh_send_ready()
        self._set_note("設定を保存しました", "ok")
        self._refresh_list()
        # 「実行を自動で始める」を今入れた場合は、既にチェックが
        # 済んでいる対象を待ち列へ入れて回し始める (切替を入れる
        # 前にチェックが済んだ分は _admit_auto_run を通っていない
        # ため)。対象でなければ何も起きない。
        for item in list(self._items):
            self._admit_auto_run(item)

    def _open_tools_folder(self):
        """ツール置き場 (data_dir の phal_tools) を開く (2.00)。

        無ければ作る。ここに入れた実行ファイルは、ツール実行
        の合図で名前だけで実行できる。"""
        folder = core.tools_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 (Windows 専用)
        except Exception as e:  # noqa: BLE001
            self._set_note(
                f"ツール置き場を開けませんでした（{e}）", "error")

    def _schedule_answer_retry(self):
        """保留した回答の、次の再試行の時刻を決める (2.77)。

        間隔は2秒から始めて倍々に伸ばし、10秒で頭打ちにする
        (レビュー中2: 見回りのたびに送り役が走らないように)。
        保留が解けたときに _clear_answer_pending が間隔を戻す。"""
        self._answer_pending_delay = min(
            max(self._answer_pending_delay * 2, 2.0), 10.0)
        self._answer_pending_next_at = (
            time.monotonic() + self._answer_pending_delay)

    def _clear_answer_pending(self, item=None):
        """保留の控えを消す (2.77)。item を渡したら、その項目の
        控えのときだけ消す。"""
        if item is not None and self._answer_pending_item is not item:
            return
        self._answer_pending_item = None
        self._answer_pending_delay = 0.0
        self._answer_pending_noted = False

    def _pump_pending_answer(self):
        """保留中の回答を、空いたら進める (2.70、レビュー高6)。

        見回りから呼ぶ。非常停止中は自動の回答へ進まない
        (1.63 の線引きに合わせる)。回答済み・送信済みなら控えは
        捨てる (2.77、レビュー重大2: 成功や手動の回答で控えが
        残ると、同じ回答を入れ直した)。再試行の間隔が空くまで
        は送り役を起こさない (2.77、レビュー中2)。"""
        item = self._answer_pending_item
        if item is None or self._emergency_stopped:
            return
        if item.answered or item.sent or item.last_run is None:
            self._clear_answer_pending()
            return
        if time.monotonic() < self._answer_pending_next_at:
            return
        self._answer_pending_item = None
        self._answer_to_ai(item)

    def _answer_to_ai(self, item: ScriptItem, manual=False):
        """実行結果を、設定済みの AI の入力欄へ入れる (送信はしない)。

        入れる文は、結果をクリップボードへ入れるときと全く同じ文
        (実行終了時に項目に残した last_result_text。再実行なら
        差分だけの文)。送るかどうかは人が決める (この道具の
        線引き)。入力欄への投入は裏方 (copilot_io の worker) 仕事で
        数秒かかることがあるので、EDDy と同じく別スレッドで回し、
        終わりはシグナルで受ける。

        他の送信の最中であれば、黙って見送らず保留に控える
        (2.70、レビュー高6)。空いたら見回りの
        _pump_pending_answer が進める。手動の押下 (manual) では
        保留した旨をノートにも出す。"""
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending or self._notice_sending \
                or self._intro_sending or self._relay_sending:
            if item.last_run is not None:
                if self._answer_pending_item is not item:
                    self._answer_pending_item = item
                    self._answer_pending_noted = False
                if not self._answer_pending_noted:
                    self._diag(
                        f"回答を保留しました（他の送信の最中）: "
                        f"{item.name}")
                    self._answer_pending_noted = True
                self._schedule_answer_retry()
                if manual:
                    self._set_note(
                        "他の送信が終わるまで、回答を保留しました",
                        "ok")
            return
        # ここまで来たら実際に始める。保留の控えがこの項目なら
        # 消す (2.77、レビュー重大2: 手動で回答し直したときに
        # 控えが残ると、見回りが同じ回答を入れ直した)。
        self._clear_answer_pending(item)
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
            configured, profile = self._resolve_item_target(item)
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self._set_note(f"AI へ回答できませんでした（{e}）", "error")
            return
        if not configured:
            self._set_note(
                "AI の投げ先が設定されていません。PHAL の「設定」"
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
        # ZIP 添付の名前変え (1.81): プロファイルで有効なら、
        # .zip の添付を .zip.txt の写しに差し替え、その旨を
        # 回答文に書く (ユーザー → Copilot の向きは ZIP を
        # そのまま渡せないため)。
        if attach_paths and profile.get("zip_as_txt"):
            attach_paths, renamed = core.zip_as_txt_paths(
                attach_paths)
            for _orig, new_name in renamed:
                text += (f"\n添付の {new_name} は ZIP ファイル"
                         "です。.txt を外してから開いてください。")
                self._diag(f"ZIP 添付を名前変えして添付: {new_name}")
        # 入れた文を項目に残す (2.59)。入力・送信方式2 では、
        # 送信の段がこの文と入力欄を照合する。
        item.answered_text = text
        self._refresh_send_ready()
        sender = self.ai_sender or _default_ai_sender
        send_lenient = profile.get("send_method") in (2, 3, 4)
        # 方式4 (2.68) は、寛容な書き込みの確認を直接読みにする。
        send_direct = profile.get("send_method") == 4
        self._ai_filling = True
        self._loop_mark("fill_start")  # ループ内訳の境目 (2.79)
        self._ai_answer_item = item
        self._refresh_list()
        self._display_item = item
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
                if send_lenient:
                    # 方式2 (2.59): 書き込みでは止めない。
                    # lenient を知らない差し替え口では従来の
                    # 呼び方に倒す。direct_check (2.68、方式4) も
                    # 同じ作法で、知らない口では lenient だけに倒す。
                    try:
                        if send_direct:
                            result = sender(
                                text, profile, attach_paths,
                                lenient=True, direct_check=True)
                        else:
                            result = sender(text, profile, attach_paths,
                                            lenient=True)
                    except TypeError:
                        try:
                            result = sender(text, profile, attach_paths,
                                            lenient=True)
                        except TypeError:
                            if attach_paths:
                                result = sender(text, profile, attach_paths)
                            else:
                                result = sender(text, profile)
                elif attach_paths:
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
        self._loop_mark("fill_done")  # ループ内訳の境目 (2.79)
        self._ai_filling = False
        item, self._ai_answer_item = self._ai_answer_item, None
        if getattr(result, "ok", False):
            self._diag(
                f"AIへ回答を入れた: {item.name if item else ''}",
                ai=(item.ai or None) if item is not None else None)
            if item is not None:
                item.answered = True
                # 成功したら保留の控えも消す (2.77、レビュー重大2)。
                self._clear_answer_pending(item)
            if self._ai_send_ready:
                self._set_note("AI の入力欄に結果を入れました", "ok")
            else:
                # 送信ボタンの目印が無ければ、ボタンは「AIへ回答」の
                # ままになる。その理由と直し方を添える。
                self._set_note(
                    "AI の入力欄に結果を入れました。"
                    "送信ボタンは未設定です"
                    "（「設定」で設定できます）", "ok")
        elif getattr(result, "deferred", False) \
                and item is not None:
            # 送信待ちの文があり上書きを避けて保留された
            # (2.71、レビュー高3)。空いたら見回りが進める。
            self._answer_pending_item = item
            self._answer_pending_noted = True
            self._schedule_answer_retry()
            self._diag(
                f"AIへの回答を保留しました（送信待ちの文があるため）: "
                f"{item.name}",
                ai=(item.ai or None))
            self._set_note(
                "送信待ちの文があるため、回答を保留しました"
                "（空いたら自動で入れます）", "ok")
        else:
            message = getattr(result, "message", "") \
                or "AI へ回答できませんでした"
            self._diag(f"AIへ回答に失敗: {message}",
                       ai=(item.ai or None) if item is not None
                       else None)
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
        # 非常停止中は自動の送信へ進まない (1.63)。
        if getattr(result, "ok", False) and item is not None \
                and self._ai_send_ready \
                and not self._emergency_stopped \
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
        self._pump_register()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する
        # (送信が裏で始まっていたら、済むまで持ち越しになる)。
        self._fire_deferred_restart()

    def _intro_manual_path(self):
        """連携マニュアルのファイル (PHAL と同じフォルダに置く)。

        設置で script_runner.py と一緒に届く。無ければ None
        (添付なしで送る)。"""
        path = Path(__file__).resolve().parent / "phal-ai-manual.md"
        return path if path.is_file() else None

    def _intro_auto_text(self):
        """開始プロンプトの自動生成の文 (1.47)。

        環境は呼んだ時点で集める。集められない項目は core 側が
        「不明」にするので、ここでは止まらない。マニュアルの
        ファイルがあれば、添付している旨も文に入る (1.78)。"""
        try:
            env = core.collect_intro_environment(
                watch_folder=self.watch_folder,
                python_exe=self.config.get("python"))
        except Exception:  # noqa: BLE001  集められなくても文は作る
            env = {"watch_folder": str(self.watch_folder)}
        env["manual_attached"] = self._intro_manual_path() is not None
        # ZIP の行 (1.81) は、有効なプロファイルで zip_as_txt
        # が入っている相手にだけ出す。設定は毎回ファイルから
        # 取り直す。
        try:
            import script_runner_ai as sr_ai
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            profiles, active = sr_ai.sanitize_profiles(section)
            env["zip_as_txt"] = bool(
                profiles[active]["target"].get("zip_as_txt"))
        except Exception:  # noqa: BLE001  行を出さないだけ
            env["zip_as_txt"] = False
        # 作業フォルダ (1.83): 直前にダイアログで選んだもの、
        # 無ければ前回選んで設定に残っているもの。
        env["work_folder"] = (self._intro_work_folder
                              or self._saved_work_folder())
        return core.build_intro_prompt(env)

    def _saved_work_folder(self) -> str:
        """設定に残っている前回の作業フォルダ (無ければ空)。"""
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            value = section.get("work_folder")
        except Exception:  # noqa: BLE001  無いものとして扱う
            return ""
        return value if isinstance(value, str) else ""

    def _intro_prompt_text(self):
        """実際に送る開始プロンプトの文。

        「設定」で保存した文 (script_runner 節の intro_prompt) が
        あればそれを、無ければ自動生成の文を使う。保存した文は
        押すたびにファイルから取り直す (変えたら次から効く)。"""
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            saved = section.get("intro_prompt")
        except Exception:  # noqa: BLE001  読めなければ自動生成へ
            saved = None
        if isinstance(saved, str) and saved.strip():
            return saved
        return self._intro_auto_text()

    def _on_intro_clicked(self):
        """「PHALループ開始プロンプトを入力」: 開始プロンプトを
        AI の窓へ入れるまで (1.82、旧「…を送信」1.77)。

        送信前に人が文をチェックしたいので、送信ボタンは押さ
        ない。入れる役は回答と同じ ai_sender で、入力欄に書き
        かけがあれば入れる役が断る。入れないとき (投げ先が
        未設定・投入の失敗) は、文をクリップボードへ入れて
        ダイアログではっきり知らせる (ステータスバーの一瞬の
        表示だけにしない)。別の送信が途中のときも、ダイアログ
        で知らせて送らない。"""
        if self._intro_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending or self._notice_sending:
            QMessageBox.information(
                self, "PHAL",
                "いま別の送信中です。"
                "終わってからもう一度押してください。")
            return
        # 作業フォルダは仕事ごとに変わるので、押したときに
        # ダイアログで選んでもらう (1.83)。前回の選択が初期値。
        # キャンセルしたら、開始プロンプト自体を入れない。
        start = self._saved_work_folder() or str(self.watch_folder)
        chosen = QFileDialog.getExistingDirectory(
            self, "作業フォルダを選んでください", start)
        if not chosen:
            self._set_note(
                "作業フォルダが選ばれなかったので、"
                "開始プロンプトは入れませんでした", "warn")
            return
        self._intro_work_folder = chosen
        try:
            settings_module.update_settings_section(
                "script_runner", {"work_folder": chosen},
                self.settings_path)
        except Exception:  # noqa: BLE001  残せなくても続ける
            pass
        text = self._intro_prompt_text()
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  未設定として扱う
            configured, profile = False, None
        if not configured:
            self._intro_fallback(
                text, "AI の投げ先が設定されていません")
            return
        sender = self.ai_sender or _default_ai_sender
        self._intro_sending = True
        self._intro_text = text
        manual = self._intro_manual_path()
        attach = [str(manual)] if manual is not None else []
        self._intro_attached = bool(attach)

        def work():
            try:
                if attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                # 送信ボタンは押さない (1.82)。人が文を確認
                # してから自分で送信する。
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"開始プロンプトを入力できませんでした（{e}）")
            self._intro_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-intro").start()

    def _on_intro_done(self, result):
        self._intro_sending = False
        if getattr(result, "ok", False):
            suffix = ("（マニュアルを添付）"
                      if self._intro_attached else "")
            self._diag(f"開始プロンプトを入力しました{suffix}")
            self._set_note(
                "開始プロンプトを入力しました。"
                "確認して送信してください", "ok")
            return
        message = str(getattr(result, "message", "") or "")
        reason = (f"AI の窓へ入れませんでした（{message}）"
                  if message else "AI の窓へ入れませんでした")
        self._intro_fallback(self._intro_text, reason)

    def _intro_fallback(self, text, reason):
        """開始プロンプトを送れなかったときの受け止め。

        文をクリップボードへ入れ、ダイアログで理由と一緒に
        知らせる。人はクリップボードの文を AI のチャットへ
        貼れば始められる。"""
        copied = False
        try:
            self._set_clipboard(text)
            copied = True
        except Exception:  # noqa: BLE001  ダイアログで伝える
            copied = False
        self._diag(f"開始プロンプトの入力を見送り: {reason}")
        if copied:
            self._set_note(
                "開始プロンプトをクリップボードに入れました",
                "warn")
            manual = self._intro_manual_path()
            manual_line = (
                f"\nマニュアルのファイル（{manual}）も"
                "一緒に添付してください。"
                if manual is not None else "")
            QMessageBox.information(
                self, "PHAL",
                f"{reason}。\n"
                "代わりに開始プロンプトをクリップボードへ"
                "入れました。AI のチャットに貼ってください。"
                f"{manual_line}")
        else:
            QMessageBox.warning(
                self, "PHAL",
                f"{reason}。\n"
                "クリップボードへも入れませんでした。")

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
            configured, profile = self._resolve_item_target(item)
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self._set_note(f"送信できませんでした（{e}）", "error")
            return
        if not configured:
            self._set_note(
                "AI の投げ先が設定されていません。PHAL の「設定」"
                "で設定してください", "error")
            return
        presser = self.ai_presser or _default_ai_presser
        # 入力・送信方式2・3 (2.59、2.61): 回答で入れた文を
        # 渡し、送信の段で入力欄と照合する。方式3 は、合わなく
        # ても大部分が一致すれば送る (force_threshold つき)。
        # 入れた文が残っていない (この版より前に回答した等)
        # ときは従来の押下に倒す。
        expected = None
        force_threshold = None
        early_force = False
        if profile.get("send_method") in (2, 3, 4):
            expected = getattr(item, "answered_text", "") or None
            if expected and profile.get("send_method") in (3, 4):
                force_threshold = SEND_FORCE_RATIO
            if expected and profile.get("send_method") == 4:
                # 方式4 (2.68): 読み直しが安定して大部分が
                # 合えば、制限時間を待たずに押す。
                early_force = True
        self._ai_sending = True
        self._loop_mark("send_start")  # ループ内訳の境目 (2.79)
        self._ai_send_item = item
        self._refresh_list()
        self._display_item = item
        self._publish_phal_status(
            "send", f"送信中: {item.name}",
            command_id=core.parse_command_id(item.name) or "",
            item=item.summary or item.name)

        import threading

        def work():
            try:
                if expected:
                    try:
                        result = presser(
                            profile, expected, force_threshold,
                            early_force=early_force)
                    except TypeError:
                        try:
                            result = presser(
                                profile, expected, force_threshold)
                        except TypeError:
                            try:
                                result = presser(profile, expected)
                            except TypeError:
                                # 文の引数を知らない差し替え口
                                # (テストの fake 等) では従来の
                                # 呼び方に倒す。
                                result = presser(profile)
                else:
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"送信できませんでした（{e}）")
            self._send_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-ai-send").start()

    def _on_ai_send_done(self, result):
        self._loop_mark("send_done")  # ループ内訳の境目 (2.79)
        self._loop_finish_send()
        self._ai_sending = False
        item, self._ai_send_item = self._ai_send_item, None
        if getattr(result, "ok", False):
            self._diag(
                f"AIへ送信を押した: {item.name if item else ''}",
                ai=(item.ai or None) if item is not None else None)
            if item is not None:
                # サイクル完了。last_run (差し戻しの証拠) と回答済みの
                # 印は残し、ボタンだけ「実行」に戻る。
                item.sent = True
            message = getattr(result, "message", "") or ""
            if "大部分が一致" in message:
                # 方式3の強制送信 (2.61)。一致率つきで残す。
                self._diag(
                    f"AIへの送信: {message}",
                    ai=(item.ai or None) if item is not None
                    else None)
                self._set_note(message, "ok")
            else:
                self._set_note("送信しました", "ok")
        else:
            message = getattr(result, "message", "") \
                or "送信できませんでした"
            self._diag(f"AIへの送信に失敗: {message}",
                       ai=(item.ai or None) if item is not None
                       else None)
            self._set_note(str(message), "error")
            if "入力欄の文が想定と違う" in str(message):
                # 方式2の照合失敗 (2.60): 診断を QR で共有
                # できる小窓を出す。
                self._show_send_mismatch_qr(item, str(message))
        self._refresh_list()
        if getattr(result, "ok", False):
            self._publish_phal_idle("送信しました")
        else:
            self._publish_phal_idle("送信に失敗しました")
        # 送信まで済んだので、自動実行の次を進める。
        self._pump_auto_run()
        self._pump_register()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    def _show_send_mismatch_qr(self, item, message):
        """方式2の照合失敗を、QR で共有できる小窓で出す (2.60)。

        共有文は窓にも併記する (スマホで読む前に中身を
        確認できるように)。QR の生成は tray-tools の診断の
        仕組み (diagnostics、segno) を使う。ここでの失敗は
        送信失敗の表示そのものに影響させない。"""
        try:
            import diagnostics
            from diagnostics_window import QrView

            text = core.build_send_mismatch_qr_text(
                PHAL_VERSION,
                (item.ai or None) if item is not None else None,
                item.name if item is not None else None,
                message)
            image = diagnostics.qr_to_qimage(
                diagnostics.make_qr(text), scale=6)
        except Exception:  # noqa: BLE001
            return
        old = getattr(self, "_send_mismatch_window", None)
        if old is not None:
            try:
                old.close()
            except Exception:  # noqa: BLE001
                pass
        window = QDialog(self)
        window.setWindowTitle("送信診断の共有")
        layout = QVBoxLayout(window)
        intro = QLabel(
            "送信時の照合が合わなかった診断です。"
            "スマホで QR を読んで、文をそのまま共有してください。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        view = QrView(window)
        view.set_image(image)
        view.setMinimumSize(320, 320)
        layout.addWidget(view)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(body)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(window.close)
        layout.addWidget(close_button)
        window.resize(420, 560)
        self._send_mismatch_window = window
        window.show()
        window.raise_()
        window.activateWindow()

    # ---------------- AI へ再開通知 ----------------
    def _try_startup_notice(self):
        """起動を、投げ先の窓へ1行知らせる (2.49)。

        開き直しのたびに「いまの版で動き始めた」ことが
        チャット側から見えるようにする。送り方は受領応答と
        同じ (ai_sender で入れて ai_presser で送信まで)。
        別の送信が途中なら少し置いてやり直し、上限まで
        来たら見送る。投げ先が未設定のときも見送って、
        起動そのものは止めない。"""
        if self._startup_notice_done \
                or self._startup_notice_sending:
            return
        if self._ack_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._notice_sending or self._intro_sending:
            self._startup_notice_tries += 1
            if self._startup_notice_tries <= 10:
                QTimer.singleShot(3000, self._try_startup_notice)
            else:
                self._startup_notice_done = True
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  未設定として扱う
            configured, profile = False, None
        if not configured:
            self._startup_notice_done = True
            return
        text = f"[PHAL:再開] Ver{PHAL_VERSION} で起動しました"
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._startup_notice_sending = True

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"再開通知を送れませんでした（{e}）")
            self._startup_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-startup").start()

    def _on_startup_notice_done(self, result):
        self._startup_notice_sending = False
        self._startup_notice_done = True
        if getattr(result, "ok", False):
            self._diag("再開通知を送りました")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"再開通知に失敗: {message}")

    # ---------------- AI へ受領応答 ----------------
    def _maybe_send_delayed_ack(self):
        """受領応答の遅延送信 (2.66)。

        実行の開始では出さず、ACK_DELAY_SECONDS を超えても実行が
        続いているときだけ出す。終わっていれば出さない (結果が
        そのまま届く)。他の送信が途中なら、空くまで見送って
        次の見回りで出す。"""
        item = self._ack_due_item
        if item is None:
            return
        if self._running_item is not item:
            self._ack_due_item = None
            return
        # 秒数は設定から取る (2.72、設定画面の自動化タブで変更可)
        delay = self._automation_flags().get(
            "ack_delay", ACK_DELAY_SECONDS)
        if time.monotonic() - self._run_started_at < delay:
            return
        if self._ack_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._notice_sending or self._intro_sending:
            return
        if time.monotonic() < self._ack_blocked_until:
            return  # 保留された直後は間隔を空ける (2.77、中2)
        self._ack_due_item = None
        self._send_ack(item)

    def _send_ack(self, item: ScriptItem):
        """自動実行の開始を、受領応答 (Ack) として送る (1-51)。

        結果の回答とは別に「受け取って実行を始める」という1行を
        fill + 送信まで一気にやる (裏方仕事)。呼ぶのは遅延側
        (_maybe_send_delayed_ack、2.66) だけになった。
        送る役は回答・診断と同じ ai_sender / ai_presser。項目の
        answered / sent には触らない (受領は回答ではない)。診断の
        自動応答の消費控えにも触らない。

        送れなくても実行は止めない: 投げ先が未設定のときや、他の
        送信が途中のときは黙って見送る (知らせは補助的なもので、
        見送りまで診断に残すと記録が埋まってしまう)。実際に
        送ろうとして失敗したときだけ、完了側で診断に残す。"""
        if self._ack_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._notice_sending or self._intro_sending:
            return
        try:
            configured, profile = self._resolve_item_target(item)
        except Exception:  # noqa: BLE001  見送って実行は続ける
            return
        if not configured:
            return
        text = core.build_ack_text(
            core.parse_command_id(item.name) or "", item.name)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._ack_sending = True
        self._ack_item = item

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
        ack_item, self._ack_item = self._ack_item, None
        if getattr(result, "ok", False):
            self._diag("受領応答を送りました")
        elif getattr(result, "deferred", False) \
                and ack_item is not None \
                and self._running_item is ack_item:
            # 送信待ちの文があり上書きを避けて保留された
            # (2.71、レビュー高3)。実行が続いている間は義務へ
            # 戻し、見回りが空いたら出し直す。
            self._ack_due_item = ack_item
            self._ack_blocked_until = time.monotonic() + 5.0
            self._diag("受領応答を保留しました（送信待ちの文があるため）")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"受領応答に失敗: {message}")
        # 回答が受領の完了待ちだったら、ここで進める (実行が
        # 受領より先に終わっていた場合の取りこぼしを防ぐ)。
        # 非常停止中は自動の回答へ進まない (1.63)。中継の送信が
        # まだ動いていれば預け直す (2.13)。
        item, self._ack_pending_answer = self._ack_pending_answer, None
        if item is not None and self._relay_sending:
            self._ack_pending_answer = item
            item = None
        if item is not None and item.last_run is not None \
                and not self._emergency_stopped:
            self._answer_to_ai(item)
        # 自動実行の待ち列があれば次を進める (回答の投入が裏で
        # 始まっていたら _pump_auto_run 側で見合わせる)。
        self._pump_auto_run()
        self._pump_register()
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
        text = self._blocked_notice_text(item)
        core.blocked_notified_put(self._blocked_notified, sha)
        core.save_blocked_notified(self._blocked_notified)
        self._send_blocked_notice(item, text, profile)

    def _blocked_notice_text(self, item: ScriptItem) -> str:
        """止まった知らせの文を組み立てる (2.78 で分離。再送でも
        同じ文になるよう、項目のチェック結果から作る)。"""
        result = item.result
        reason = ""
        if result is not None and result.issues:
            reason = str(result.issues[0].message or "")
        if not reason and result is not None:
            reason = str(result.headline or "")
        return core.build_blocked_text(
            core.parse_command_id(item.name) or "", item.name, reason)

    def _send_blocked_notice(self, item: ScriptItem, text: str,
                             profile):
        """止まった知らせを送る (裏方仕事)。控え (知らせ済み) は
        呼び側が先に残してある。再送もここを通る。"""
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._notice_sending = True
        self._notice_item = item

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

    def _pump_pending_notice(self):
        """保留された止まった知らせを、空いたら送り直す (2.78、
        レビュー重大1)。

        知らせは合図の見張りからは撃ち直されない (チェックで
        止まった出来事は一度きり) ので、見回りが担う。控え
        (知らせ済み) は最初の送信前に残してあるので、ここでは
        送るだけ。"""
        item = self._notice_pending_item
        if item is None or self._emergency_stopped:
            return
        if time.monotonic() < self._notice_retry_next_at:
            return
        if self._notice_sending or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending:
            return
        if item.result is None \
                or item.result.status != core.STATUS_ERROR:
            self._notice_pending_item = None
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  次の見回りで試し直す
            return
        if not configured:
            return
        self._notice_pending_item = None
        self._send_blocked_notice(
            item, self._blocked_notice_text(item), profile)

    def _on_notice_done(self, result):
        self._notice_sending = False
        item, self._notice_item = self._notice_item, None
        if getattr(result, "deferred", False) \
                and item is not None:
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。文はまだ入力欄へ届いて
            # いないので、見回りで送り直す。
            self._notice_pending_item = item
            self._notice_retry_next_at = time.monotonic() + 5.0
            self._note_deferred("チェックで止まった知らせ")
        elif getattr(result, "ok", False):
            self._diag("チェックで止まった知らせを送りました")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"チェックで止まった知らせに失敗: {message}")
        # 自動実行の待ち列があれば次を進める (受領の完了側と同じ)。
        self._pump_auto_run()
        self._pump_register()
        # 保留中の開き直しがあれば、連鎖が済んだこの時点で実行する。
        self._fire_deferred_restart()

    # ---------------- チャットの見張り・DL ----------------
    def _automation_flags(self) -> dict:
        """自動化の切替 (script_runner 節の auto_*) を読み直す。

        「設定」で変えたら次から効くよう、使うたびにファイル
        から取る (投げ先の解決と同じ作法)。読めなければ全部手動
        (False) に倒す。"""
        try:
            import script_runner_ai as sr_ai

            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            result = sr_ai.sanitize_automation(section)
            result["ack_delay"] = sr_ai.sanitize_ack_delay(section)
            return result
        except Exception:  # noqa: BLE001  読めなければ手動のまま
            return {"auto_dl": False, "auto_run": False,
                    "auto_answer": False, "auto_send": False,
                    "auto_diag": False, "auto_ack": True,
                    "ack_delay": ACK_DELAY_SECONDS,
                    "auto_reset": False, "auto_rerun": False,
                    "auto_resend": False, "auto_tool": False}

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
        if holder is None:
            holder = self._primary_state.holder
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

    def _default_state(self):
        """既定の投げ先の見張り状態 (2.21)。

        出どころが決まらない操作 (出どころを付けられなかった
        走査結果や応答、手動の押下など) の受け先は、設定で
        選んだプロファイル (既定の投げ先) の見張り状態を
        優先し、それが見張りに入っていなければ見張りの
        先頭にする。見張りがゼロなら同期前の代表の状態。"""
        state = (getattr(self, "_watches", None) or {}).get(
            getattr(self, "_ai_name", "") or "")
        if state is not None:
            return state
        watch_list = getattr(self, "_watch_list", None) or []
        if watch_list:
            return watch_list[0]
        return self._primary_state

    def _sync_watch_states(self):
        """見張る窓の並びを、今の設定に合わせる (2.08、集合化は
        2.20)。

        見張る窓は名前の集合 (ai_watch_profiles、無ければ旧
        キーから導く) で決まり、並びはプロファイル一覧の順。
        個数の上限は無い (ゼロも可、その間は自動の検出・応答は
        止まる)。プロファイルの辞書は毎回作り直す (設定の読み
        直しと同じ作法)。状態は名前で引き継ぎ、並びから外れた
        窓の状態は捨てる (特別行も一緒に消える)。"""
        import script_runner_ai as sr_ai

        try:
            all_settings = settings_module.load_settings(
                self.settings_path)
            section = all_settings.get("script_runner") or {}
            profiles, active = sr_ai.sanitize_profiles(section)
            watch_names = sr_ai.sanitize_watch_profiles(section)
        except Exception:  # noqa: BLE001  読めなければ見張りなし
            profiles, active = [], 0
            watch_names = []
        desired = [entry for entry in profiles
                   if entry["name"] in watch_names]
        states = []
        for position, entry in enumerate(desired):
            name = entry["name"]
            state = self._watches.get(name)
            if state is None and position == 0 \
                    and self._primary_state.name in ("", name):
                # 最初の同期では、初期状態 (主) を引き継ぐ。
                state = self._primary_state
                state.name = name
            if state is None:
                state = _WatchState(name)
            fields = entry["target"]
            state.profile = sr_ai.build_profile(fields) \
                if sr_ai.is_configured(fields) else None
            states.append(state)
        self._watches = {state.name: state for state in states}
        self._watch_list = states
        if states:
            self._primary_state = states[0]
        # 設定済みかは、大きな進捗表示の明滅 (1.64) が「AI 窓の
        # 見張りが立っているか」の判定にも使う。見張る窓の
        # どれかに投げ先が設定されていればよい (2.20)。
        self._ai_configured = any(
            state.profile is not None for state in states)
        return states

    def _holders(self):
        """表示中の DL 候補 (特別行) の並び (見張る窓の並び順)。"""
        return [state.holder for state in self._watch_list
                if state.holder is not None]

    def _maybe_check_chat(self):
        """チャットの DL 候補の検出を、間引いて裏で回す。

        フォルダの見回り (_poll_inner) の終わりに呼ばれる。
        見張る窓ごとに _maybe_check_one を回す (2.20 で集合化、
        窓の数はいくつでもよい)。"""
        for state in self._sync_watch_states():
            # 候補と同じファイルが一覧に来たら、その窓の特別行
            # は役目を終える (人が手で落とした場合など。検出の
            # 結果を待たない)。
            if state.holder is not None \
                    and self._dl_holder_in_items(state.holder):
                self._diag(
                    "DL行を下げた（ファイルが一覧に現れた）: "
                    f"{state.holder.label}")
                state.holder = None
                self._refresh_list()
            self._maybe_check_one(state)

    def _maybe_check_one(self, state):
        """1つの窓の見張りを間引いて回す (2.08)。

        通常時は変化の署名だけを見て (SIGNATURE_INTERVAL_SEC)、
        変わっていなければ本走査を打ち切る。変化の気配がある
        間は追いかけモードで本走査を短い間隔で回す (2.07)。
        本走査は最大でも FULL_SCAN_MAX_GAP_SEC に1回は挟む。
        投げ先が未設定の窓は回さない。検出は裏方 (UIA) 仕事
        なので別スレッドで回し、結果はシグナルで受ける。失敗は
        黙る (特別行が出ないだけ) ので、note には出さない。"""
        if state.checking or state.profile is None:
            return
        now = time.monotonic()
        # 追いかけモード (2.07): 送信・実行・DL 待ちの途中は、
        # 返事が来るはずの時間帯なので追いかけに入る。
        if self._ai_busy_for_chase():
            self._enter_chase(state)
        chasing = now < state.chase_until
        if chasing:
            if now - state.last_check < CHASE_INTERVAL_SEC:
                return
            self._start_dl_check(state, signature=False)
        elif state.last_full_scan == 0.0 or \
                now - state.last_full_scan >= FULL_SCAN_MAX_GAP_SEC:
            # まだ本走査をしていない (初回) か、前回の本走査から
            # 空きすぎたときは本走査。間隔は従来どおり。
            if now - state.last_check < CHAT_POLL_INTERVAL_SEC:
                return
            self._start_dl_check(state, signature=False)
        else:
            if now - state.sig_last_check < SIGNATURE_INTERVAL_SEC:
                return
            self._start_dl_check(state, signature=True)

    def _start_dl_check(self, state, signature):
        """見張りの1回分を裏で回す (本走査か、署名だけか)。"""
        state.checking = True
        now = time.monotonic()
        if signature:
            state.sig_last_check = now
            fn = self.ai_signer or _default_ai_signer
        else:
            state.last_check = now
            fn = self.ai_downloader or _default_ai_downloader
        profile = state.profile

        import threading

        def work():
            try:
                if signature:
                    result = _DlCheckResult(
                        True, "", kind="signature",
                        signature=fn(profile))
                else:
                    result = fn(profile)
            except Exception as e:  # noqa: BLE001
                result = _DlCheckResult(
                    False, str(e),
                    kind="signature" if signature else "scan")
            # どの窓の結果かを付ける (付けられなければ、受ける
            # 側が主の窓として扱う)。
            try:
                result.watch = state.name
            except Exception:  # noqa: BLE001
                pass
            self._dl_signals.checked.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-dl-watch").start()

    def _enter_chase(self, state):
        """追いかけモードに入る (期限を延ばす、2.07)。"""
        state.chase_until = time.monotonic() + CHASE_WINDOW_SEC

    def _ai_busy_for_chase(self):
        """送信・実行・DL 待ちの途中か (追いかけの合図、2.07)。"""
        return bool(
            getattr(self, "_ai_filling", False)
            or getattr(self, "_ai_sending", False)
            or getattr(self, "_diag_sending", False)
            or getattr(self, "_running_item", None) is not None
            or bool(getattr(self, "_dl_awaiting", None)))

    def _on_signature_checked(self, result, state):
        """署名の結果を受ける (2.07)。

        取れなければ控えを空に戻し、次回は本走査に倒れる。初回は
        控えを作るだけ。変わっていたら追いかけモードに入り、
        本走査をすぐ回す (間隔の控えを戻す)。"""
        sig = getattr(result, "signature", None)
        if not getattr(result, "ok", False) or sig is None:
            state.signature = None
            return
        sig = list(sig)
        if state.signature is None:
            state.signature = sig
            return
        if sig != state.signature:
            state.signature = sig
            self._enter_chase(state)
            state.last_check = 0.0

    def _refresh_chase_label(self):
        """追いかけモードの印の付け直し (2.07、2.15 で階の見出しへ)。

        追いかけ中かどうかは階の見出しに出るので、進捗表示の
        付け直しを呼ぶだけにする。"""
        if getattr(self, "_floors", None):
            self._refresh_status_strip()


    def _show_banner(self, text, seconds=6):
        """検知の帯を出す (2.07)。"""
        banner = getattr(self, "_banner", None)
        if banner is None:
            return
        banner.setText(text)
        banner.setVisible(True)
        self._banner_timer.start(int(seconds * 1000))

    def _note_dl_scan(self, state, line: str):
        """直近の走査の1行を差し替え、変わったときだけ診断の
        控えにも残す (1-57)。

        走査は間隔を空けて何度も回るので、同じ内容を毎回残すと
        記録が流れてしまう。検出失敗・候補の抑制のような「黙ると
        切り分けられない」出来事は、状態が変わった時点で1回
        残れば足りる。控えは窓ごと (2.08)。"""
        full = f"直近の走査: {line}"
        if full == state.last_scan:
            return
        state.last_scan = full
        self._diag(full, ai=state.name or None)

    def _dl_suppress_reason(self, state, holder):
        """DL 候補を特別行に出さない理由。出すなら None。

        判定そのものは従来どおり (押下済みの控え・一覧にあるか)。
        ここでは、その判定に診断で読める名前を付ける (1-57)。
        消費済みの控えは、その候補を見つけた窓のもの (2.08)。"""
        if holder.key in state.consumed:
            return "消費済み"
        # DL 済みの物と、AI 名 (この窓) とコマンドIDが一致
        # したら出さない (2.25、Nagato の提案)。概要の照合
        # より確実に当たるので先に見る。
        holder_id = _holder_command_id(holder)
        if holder_id and any(
                core.parse_command_id(item.name) == holder_id
                and item.ai == state.name
                for item in self._items):
            return "一覧に既にある（ID・AI名一致）"
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

    def _dl_scan_line(self, state, chosen, suppressed, count):
        """走査の結果の1行 (診断用)。

        chosen は採用した候補 (無ければ None)、suppressed は
        [(候補, 理由)] (新しい順)、count は候補の総数。"""
        if chosen is not None:
            line = f"候補あり（{chosen.summary or chosen.label}）"
            if chosen.key in (state.launch_keys or set()):
                line += "・起動時の既存分"
            if suppressed:
                line += f"（新しい側の{len(suppressed)}件は抑制）"
            return line
        if count:
            holder, reason = suppressed[0]
            return (f"候補なし（抑制{len(suppressed)}件: "
                    f"{holder.summary or holder.label} は{reason}）")
        return "候補なし"

    def _dispatch_signal(self, marker, handler):
        """合図の判定を、その合図の依頼 ID の控えつきで呼ぶ
        (2.40)。

        判定の間だけ _diag_ctx_rid を立て、判定の中で書かれる
        動作ログに合図の ID (rid) が付くようにする。ID の在処
        は合図ごとに違う (request_id / command_id / path) ので、
        目印の辞書からその順に取る。2.41 で依頼 ID は全合図に
        必須になったので、通常は request_id が取れる (ID 無し
        の合図では空になり、rid は付かない)。
        非同期の完了側はこの控えに頼らず、ID を明示で渡す。"""
        rid = ""
        if isinstance(marker, dict):
            rid = str(marker.get("request_id")
                      or marker.get("command_id")
                      or marker.get("path") or "")
        self._diag_ctx_rid = rid
        try:
            handler(marker)
        finally:
            self._diag_ctx_rid = ""

    def _on_dl_checked(self, result):
        # どの窓の結果かで、その窓の状態へ処理する (2.08)。
        # 付けられなかった結果は既定の投げ先の窓として受ける
        # (2.21)。
        watch_name = getattr(result, "watch", "") or ""
        state = self._watches.get(watch_name) or self._default_state()
        state.checking = False
        if getattr(result, "kind", "scan") == "signature":
            self._on_signature_checked(result, state)
            return
        state.last_full_scan = time.monotonic()
        if not getattr(result, "ok", False):
            # 失敗も形跡を残す (1-57)。行は出ないし、今の行も
            # そのまま。理由が変わらない間は1回だけ残る
            # (_note_dl_scan が変化だけを見るため)。
            message = " ".join(
                str(getattr(result, "message", "") or "").split())
            if len(message) > 80:
                message = message[:79] + "…"
            self._note_dl_scan(
                state,
                f"検出失敗（{message}）" if message else "検出失敗")
            return
        # 候補か合図があれば、会話が動いている証拠なので追い
        # かけモードを延ばす (2.07)。
        _marker_names = (
            "marker", "reset_marker", "rerun_marker",
            "resend_marker", "settings_marker", "manual_marker",
            "file_marker", "fulltext_marker", "capture_marker",
            "history_marker",
            "stop_marker", "log_marker", "tool_marker",
            "describe_marker", "eval_marker", "relay_marker",
            "input_marker", "overwrite_marker", "force_marker")
        if getattr(result, "candidate", None) or \
                getattr(result, "candidates", None) or \
                any(getattr(result, name, None)
                    for name in _marker_names):
            self._enter_chase(state)
        # 合図への応答は、その合図が出た窓へ返す (2.08)。この
        # 結果の処理の間だけ、出どころの窓を投げ先の解決に
        # 割り当てる (合図の判定が終わればすぐ戻す)。
        self._dispatch_state = state
        # 診断リクエストの目印は、候補の有無と関係なく同じ走査の
        # 結果に乗ってくる (1-43)。候補の処理より先に見る (候補が
        # 無い結果では、この先で return してしまうため)。
        self._dispatch_signal(
            getattr(result, "marker", None), self._maybe_auto_diag)
        # リセットの目印も同じ走査に乗ってくる (1-52)。診断の
        # 判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "reset_marker", None),
            self._maybe_auto_reset)
        # 再実行の目印も同じ走査に乗ってくる (1.63)。診断・
        # リセットの判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "rerun_marker", None),
            self._maybe_auto_rerun)
        # 結果再送の目印も同じ走査に乗ってくる (1.75)。他の
        # 合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "resend_marker", None),
            self._maybe_auto_resend)
        # 設定確認・マニュアル再送の目印も同じ走査に乗ってくる
        # (1.85)。他の合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "settings_marker", None),
            self._maybe_auto_settings)
        self._dispatch_signal(
            getattr(result, "manual_marker", None),
            self._maybe_auto_manual)
        # ファイルリクエストの目印も同じ走査に乗ってくる
        # (1.88)。他の合図の判定とは別に見る。
        self._dispatch_signal(
            getattr(result, "file_marker", None),
            self._maybe_auto_file)
        # キャプチャの目印も同じ走査に乗ってくる (2.54)。他の
        # 合図の判定とは別に見る。
        self._dispatch_signal(
            getattr(result, "capture_marker", None),
            self._maybe_auto_capture)
        # 全文リクエストの目印も同じ走査に乗ってくる
        # (1.91)。他の合図の判定とは別に見る。
        self._dispatch_signal(
            getattr(result, "fulltext_marker", None),
            self._maybe_auto_fulltext)
        # 実行一覧の目印も同じ走査に乗ってくる (1.92)。他の
        # 合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "history_marker", None),
            self._maybe_auto_history)
        # 停止の目印も同じ走査に乗ってくる (1.93)。他の合図の
        # 判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "stop_marker", None),
            self._maybe_auto_stop)
        # 動作ログの目印も同じ走査に乗ってくる (1.96)。他の
        # 合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "log_marker", None),
            self._maybe_auto_log)
        # ツール実行の目印も同じ走査に乗ってくる (1.98)。他の
        # 合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "tool_marker", None),
            self._maybe_auto_tool)
        # 道具説明・道具評価の目印も同じ走査に乗ってくる
        # (2.37)。他の合図の判定とは別に見る (控えも別)。
        self._dispatch_signal(
            getattr(result, "describe_marker", None),
            self._maybe_auto_describe)
        self._dispatch_signal(
            getattr(result, "eval_marker", None),
            self._maybe_auto_eval)
        # 入力欄まわりの目印も同じ走査に乗ってくる (2.39)。
        # 他の合図の判定とは別に見る (控えは3種で共有)。
        self._dispatch_signal(
            getattr(result, "input_marker", None),
            self._maybe_auto_input)
        self._dispatch_signal(
            getattr(result, "overwrite_marker", None),
            self._maybe_auto_overwrite)
        self._dispatch_signal(
            getattr(result, "force_marker", None),
            self._maybe_auto_force)
        # 中継リクエストの目印も同じ走査に乗ってくる (2.10)。
        # 他の合図の判定とは別に見る (控えも間隔も別)。
        self._dispatch_signal(
            getattr(result, "relay_marker", None),
            self._maybe_auto_relay)
        self._flush_react_notes()
        self._dispatch_state = None
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
        # 見た候補のファイル名と窓を控える (2.38)。フォルダに
        # 直接届いたファイルの出どころ判定 (_admit_auto_run の
        # カード目撃ゲート) に使う。新しく目撃した名前は、
        # チェック済みの項目を自動実行の入口で再判定する
        # (ファイルの到着が目撃より先だった場合の取りこぼし
        # 防止)。
        newly_seen = []
        newly_seen_fp = []
        for holder in holders:
            fname = _card_file_name(holder.name)
            if fname and self._record_card_seen(fname, state.name):
                newly_seen.append(fname)
            # カード型の候補 (名前が中身の冒頭) はファイル名が
            # 取れないので、先頭指紋でも目撃を控える (2.39)。
            if "\n" in holder.name or \
                    holder.name.lstrip().startswith("#"):
                fp = core.head_fingerprint(holder.name)
                if fp and self._record_card_seen_fp(
                        fp, state.name):
                    newly_seen_fp.append(fp)
        for fname in newly_seen:
            for item in list(self._items):
                if item.name == fname and item.result is not None \
                        and not item.ran:
                    self._admit_auto_run(item)
        for fp in newly_seen_fp:
            for item in list(self._items):
                if item.result is not None and not item.ran \
                        and _file_head_fingerprint(item.path) == fp:
                    self._admit_auto_run(item)
        # 起動スナップショット (DL 候補版)。最初の検出で見えた
        # 候補は、起動した時点で既にあった分として控える。検出
        # に失敗した回 (ok=False) では取らない (上の return で
        # 戻っているため、ここへ来るのは成功した検出だけ)。候補
        # が無かった回は空の控えになり、その後に届いた候補は
        # 新しい分として扱う。
        if state.launch_keys is None:
            continuation = core.dl_session_continuation_key(
                state.session)
            state.session = {}
            if continuation is not None:
                # 開き直しの続き (1.70)。直前に閉じた時点で表示
                # していた候補だけを起動時分とし、合間に届いた
                # 新しい候補は自動 DL の対象に残す。
                state.launch_keys = {continuation}
                self._diag("DL: 開き直しの続きとして再開",
                           ai=state.name or None)
            else:
                state.launch_keys = {h.key for h in holders}
        # 新しい順 (木の末尾側) から見て、消費済みでも一覧に
        # 既にあるものでもない、最初のものを採用する (1-57)。
        # 従来は末尾の1件に固定で、それが消費済みだと本命の
        # 新しいカードが浮上しなかった (1-76)。
        chosen = None
        suppressed = []
        for holder in reversed(holders):
            reason = self._dl_suppress_reason(state, holder)
            if reason is None:
                chosen = holder
                break
            suppressed.append((holder, reason))
        self._note_dl_scan(
            state,
            self._dl_scan_line(state, chosen, suppressed,
                               len(holders)))
        if chosen is None:
            # 落とすものは無い (候補自体が無いか、全部が抑制)。
            # この窓の待機の起点を立てる (2.23、階の
            # カウントアップ用。既に立っていればそのまま)。
            if state.idle_since is None:
                state.idle_since = time.monotonic()
            # 特別行があれば下げる。
            if state.holder is not None:
                if holders:
                    self._diag(
                        "DL行を下げた（押下済み・一覧にある候補だった）: "
                        f"{state.holder.label}",
                        ai=state.name or None)
                else:
                    self._diag(
                        "DL行を下げた（候補が無くなった）: "
                        f"{state.holder.label}",
                        ai=state.name or None)
                state.holder = None
                self._refresh_list()
                self._publish_phal_idle("待機中")
            return
        holder = chosen
        if state.holder is not None \
                and state.holder.key == holder.key:
            return  # 同じ候補のまま。検出時刻もそのままにする
        holder.watch = state.name
        state.holder = holder
        state.idle_since = None
        state.last_candidate_key = holder.key
        self._diag(
            f"DL候補を表示: {holder.summary or holder.label}",
            ai=state.name or None)
        self._show_banner(
            f"ダウンロードを検知：{holder.summary or holder.label}")
        self._refresh_list()
        self._publish_phal_status(
            "dl", f"新着: {holder.summary or holder.label}",
            command_id="", item=holder.summary or holder.label,
            affect_stage=False)
        # 自動化: 「DLを自動で押す」が入っていれば、新しく見つけた
        # 候補を1回だけ自動で押す。起動時に既にあった候補 (起動
        # スナップショットにある key) は対象外にする (設置の開き
        # 直しの直後に古い添付を拾い直して落とし直すのを防ぐ。
        # 特別行は出したままなので、手動では押せる)。失敗しても
        # 繰り返さない (理由は _on_dl_pressed が note に出す。
        # 次の新しい候補でまた試す)。
        if self._automation_flags()["auto_dl"] \
                and not self._emergency_stopped \
                and holder.key not in state.auto_tried \
                and holder.key not in state.launch_keys:
            state.auto_tried.add(holder.key)
            self._press_download(auto=True, state=state)

    def _press_download(self, auto: bool, state=None):
        """DL の特別行のボタンの仕事。候補を押して落とす。

        押下役 (既定は _default_ai_dl_presser) の中で、候補は
        探し直してから Invoke で押す。投入と同じく裏方仕事なので
        別スレッドで回し、終わりはシグナルで受ける。押す窓は
        候補が出た窓 (state、2.08)。state が無ければ既定の
        投げ先の窓 (2.21)。"""
        if state is None:
            state = self._default_state()
        holder = state.holder
        if holder is None or self._dl_pressing:
            return
        profile = state.profile
        if profile is None:
            # 状態の控えが古い場合の保険に、有効なプロファイルで
            # 解決し直す (従来の経路)。
            try:
                configured, profile = self._resolve_ai_target()
            except Exception as e:  # noqa: BLE001  理由は利用者への文にする
                self._set_note(
                    f"ダウンロードできませんでした（{e}）", "error")
                return
            if not configured:
                self._set_note(
                    "AI の投げ先が設定されていません。PHAL の「設定」"
                    "で設定してください", "error")
                return
        presser = self.ai_dl_presser or _default_ai_dl_presser
        if auto:
            self._loop_begin()  # ループ内訳の検出の境目 (2.79)
        self._dl_pressing = True
        self._dl_press_auto = auto
        self._dl_press_key = holder.key
        self._dl_press_watch = state.name
        # 裏方へは、窓側が選んだ候補そのもの (名前+クラス名) を
        # 渡す (1-57)。裏方は取り直した木からその候補を探して
        # 押すので、検出と押下の間に新しい発言が積まれても、
        # 別の候補を押し間違えない。
        press_key = {"name": holder.name,
                     "class_name": holder.class_name}
        self._diag(
            f"DL押下を開始（{'自動' if auto else '手動'}）: "
            f"{holder.summary or holder.label}",
            ai=state.name or None)
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
            self._diag("DL押下に成功（到着待ち）",
                       ai=self._dl_press_watch or None)
            # 到着の確認は、落としたファイルが一覧に現れた時点
            # (_poll_candidate) で残す。候補の表示名を控えておく。
            press_state = self._watches.get(
                self._dl_press_watch or "") or self._default_state()
            holder = press_state.holder
            awaiting_label = holder.label \
                if holder is not None else ""
            self._dl_awaiting.append((
                awaiting_label,
                time.monotonic(), press_state.name,
                self._dl_press_auto,
                holder.name if holder is not None else "",
                core.head_fingerprint(
                    holder.name) if holder is not None else "",
                holder.summary if holder is not None else ""))
            # 押せた候補は、この会話ではもう出さない (落とした
            # ファイルはフォルダの見回りが通常の行として拾う)。
            if self._dl_press_key is not None:
                press_state.consumed.add(self._dl_press_key)
            if press_state.holder is not None \
                    and press_state.holder.key == self._dl_press_key:
                press_state.holder = None
            self._set_note(
                "自動でダウンロードしました" if self._dl_press_auto
                else "ダウンロードしました", "ok")
            _banner_head = "自動でダウンロードしました：" \
                if self._dl_press_auto else "ダウンロードしました："
            self._show_banner(_banner_head + awaiting_label)
            self._publish_phal_status(
                "dl", "ダウンロードしました（到着待ち）")
        else:
            message = getattr(result, "message", "") \
                or "ダウンロードできませんでした"
            self._diag(f"DL押下に失敗: {message}",
                       ai=self._dl_press_watch or None)
            if "ダウンロードパネルが開いたままでした" in str(message):
                # 押下役 (裏方) からの文に含まれる、フライアウトを
                # 閉じきれなかったという知らせを取り出しておく
                # (1-40。成功時は裏方から届かないので残せない)。
                self._diag(
                    "フライアウト: 閉じきれず開いたままだった",
                    ai=self._dl_press_watch or None)
            self._set_note(str(message), "error")
            self._publish_phal_idle("ダウンロードに失敗しました")
        self._dl_press_key = None
        self._dl_press_watch = None
        self._refresh_list()

    def _send_escape_to_ai(self, watch_name=None):
        """DL の到着が確かめられた直後、AI の窓へ ESC を送る
        (裏方仕事、1-67)。

        Edge のダウンロード・フライアウトは、この到着のときに
        開いたもの。閉じておかないと次の押下の邪魔になる
        (1-40 の UIA 解散が実物の形に当たらなかったための
        キー配送版)。結果は診断にだけ残し、失敗しても連鎖は
        止めない。投げ先が解決できなければ送らない。送る窓は、
        その DL を押した窓 (watch_name、2.08)。"""
        try:
            if watch_name:
                configured, profile = self._resolve_profile_by_name(
                    watch_name)
            else:
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

    # ---------------- 合図の再応答の抑止 (1.95、2.00 で作り替え) ----
    # 応答済みの合図が、開き直しをまたいで同定子 (id・key) が
    # 外れた状態で再検出されることがある (画面側の要素分割が
    # 変わると、同じ発言でも同定子が変わる)。種類ごとに、最後
    # に応答した中身 (payload) と応答した時刻を控えに残す。
    # payload は ID つき合図 (再実行・再送・全文) はコマンド
    # ID、ファイルはパス、それ以外は空文字 (種類そのものが
    # 単位)。診断の合図は対象外: 末尾4000字の範囲から流れ出る
    # ため再発火しにくく、間隔も5分で別管理のため。
    #
    # 1.95 では「同じ payload が15分以内に再び見えたら応答
    # しない」だったが、合図の行がチャットに残り続けるため、
    # 15分を過ぎて走査範囲に入り直すと再び実行・再送して
    # しまう事故が実地で続いた (ログ・再実行・ファイル、
    # 2026-11-15 夜)。2.00 からは、応答済みの payload は
    # 時間が過ぎても繰り返し実行しない。代わりに、再検出の
    # 最初の1回だけ「応答済み」と知らせる (黙って無視すると、
    # 頼んだ側が届いていないと誤解するため)。知らせを送った
    # ことも控えに残し、それ以降の再検出は黙って見送る。
    # 本当に同じ中身がもう一度必要なときは、内容を変えて
    # 出し直す (payload が変われば別の合図として実行される)。
    #
    # 2.46 で Nagato の決定により2点変えた。(1) 照合は依頼 ID
    # で統一する: ID が同じなら、内容が違っても同じ依頼として
    # 実行しない (種類ごとに payload の形が違い、ID だけの種類と
    # ID+内容の種類で扱いが割れていた)。もう一度必要なときは
    # 新しい ID で出し直す。(2) 再検出では知らせを出さない:
    # 開き直しのたびに過去分がまとめて再検出され、見送りの
    # 知らせがチャットと依頼履歴に連発したため。再検出は無言で
    # 見送り、知らせは「ID は同じで内容が違う = ID の使い回し」
    # のときにだけ出す (_suppressed_chat_text)。

    # 「種別:ID」の形の payload で、種別と取り違えないための
    # 種別名の集合 (道具評価の「ID:道具名」と見分ける)。
    _ID_FORM_KINDS = frozenset({
        "diag", "settings", "manual", "reset", "rerun", "resend",
        "file", "fulltext", "history", "stop", "log", "tool",
        "describe", "eval", "input", "overwrite", "force", "relay"})

    def _payload_id_part(self, payload):
        """payload から依頼 ID の部分を取り出す (2.46)。

        payload の形は合図ごとに違うが、ID は先頭にある
        (「ID」「ID\\n…」「ID:道具名」「種別:ID」)。本文そのもの
        が payload の中継など、ID の形をしていないものは空を
        返す (ID の判定をしない)。"""
        first = str(payload or "").split("\n", 1)[0].strip()
        if not first or " " in first or len(first) > 64:
            return ""
        if ":" in first:
            head, _, tail = first.partition(":")
            if head in self._ID_FORM_KINDS and tail:
                return tail
            return head
        return first

    def _answered_id_entry(self, state, payload):
        """依頼 ID が同じ応答済みの控えを返す (2.46)。

        戻り値は (応答時刻, 応答した payload) か None。payload
        そのものの一致は見ない (そちらは _answered_at_for)。
        控えは _record_answered が answered_ids に残したもの。
        移行用に、応答履歴 (answered_map) と last_payload の
        ID 部分の走査もする。中継は対象外 (payload は本文で、
        ID の判定は中継側の ID 履歴が別に持っている)。"""
        if state is getattr(self, "_relay_auto", None):
            return None
        rid = self._payload_id_part(payload)
        if not rid:
            return None
        ids = state.get("answered_ids")
        if isinstance(ids, dict):
            entry = ids.get(rid)
            if isinstance(entry, dict):
                return (float(entry.get("at") or 0.0),
                        str(entry.get("payload") or ""))
        amap = state.get("answered_map")
        if isinstance(amap, dict):
            for answered_payload, at in amap.items():
                if isinstance(at, (int, float)) and at > 0 and \
                        self._payload_id_part(answered_payload) == rid:
                    return (float(at), str(answered_payload))
        last = str(state.get("last_payload") or "")
        if last and self._payload_id_part(last) == rid:
            at = float(state.get("last_answered_at") or 0.0)
            if at > 0:
                return (at, last)
        return None

    def _answered_at_for(self, state, payload):
        """payload への応答時刻を返す (2.34)。履歴
        (answered_map) を優先し、無ければ従来の last_payload
        の組を見る。応答したことが無ければ None。"""
        key = str(payload or "")
        amap = state.get("answered_map")
        if isinstance(amap, dict):
            at = amap.get(key)
            if isinstance(at, (int, float)) and at > 0:
                return float(at)
        last = float(state.get("last_answered_at") or 0.0)
        if last <= 0:
            return None
        if str(state.get("last_payload") or "") != key:
            return None
        return last

    def _refire_suppressed(self, state, payload) -> bool:
        """同じ payload への応答が抑止時間内なら True (1.95)。

        抑止時間 (15分) の中は見送る。判定は応答の履歴
        (2.34) で行うので、応答済みの目印が複数残っていても
        交互の再検出で抑止をすり抜けない。2.46 からは、依頼 ID
        が応答済みなら内容が違っても見送る (ID が同じなら同じ
        依頼として扱う)。"""
        last = self._answered_at_for(state, payload)
        if last is not None and \
                time.time() - last < core.MARKER_REFIRE_SUPPRESS_SEC:
            return True
        return self._answered_id_entry(state, payload) is not None

    def _refire_answered_at(self, state, payload):
        """同じ payload に応答済みなら、その応答時刻を返す
        (2.00、2.34 で履歴対応)。応答したことが無ければ
        None。"""
        return self._answered_at_for(state, payload)

    def _record_answered(self, state, payload):
        """応答した中身と時刻を控えに残す (1.95)。保存は呼び
        出し側の既存の save (消費の保存) に相乗りする。別の
        payload に応答したら、知らせ済みの控えは古い payload
        のものなので消す (2.00)。応答の履歴 (2.34) にも残す
        (抑止時間を過ぎた分は落とす)。"""
        now = time.time()
        state["last_payload"] = str(payload or "")
        state["last_answered_at"] = now
        amap = state.get("answered_map")
        if not isinstance(amap, dict):
            amap = {}
        amap[str(payload or "")] = now
        cutoff = now - core.MARKER_REFIRE_SUPPRESS_SEC
        state["answered_map"] = {
            k: v for k, v in amap.items()
            if isinstance(v, (int, float)) and v > cutoff}
        if str(state.get("refire_notice_payload") or "") \
                != str(payload or ""):
            state.pop("refire_notice_payload", None)
            state.pop("refire_notice_at", None)
        # 依頼 ID の単位の控えも残す (2.46)。payload の抑止
        # (15分窓) とは別に、同じ ID の合図を内容が違っても
        # 再実行しないために使う。中継 (payload は本文) は
        # 対象外。控えは core 側の保存で answered_ids として
        # 残る (上限100件、古い順に落とす)。
        rid = self._payload_id_part(payload)
        if rid and state is not getattr(self, "_relay_auto", None):
            ids = state.get("answered_ids")
            if not isinstance(ids, dict):
                ids = {}
            ids[rid] = {"at": now, "payload": str(payload or "")}
            if len(ids) > 100:
                keep = sorted(
                    ids.items(),
                    key=lambda kv: (kv[1].get("at") or 0.0))[-100:]
                ids = dict(keep)
            state["answered_ids"] = ids

    def _maybe_refire_notice(self, state, payload, label, save_fn):
        """応答済みの合図の再検出を、実行せず知らせで返す
        (2.00)。

        知らせるのは payload ごとに1回だけ (控えに残す)。
        連鎖の途中や投げ先が未設定のときは何もしない (合図は
        消費されないので、見張りの次の回で改めて判定される。
        ただし知らせ済みの控えがある場合は二度と送らない)。"""
        if state.get("refire_notice_at") and str(
                state.get("refire_notice_payload") or "") \
                == str(payload or ""):
            return  # 知らせ済み。以後は黙って見送る
        # 2.31 から、知らせはチャットへ出さず診断にだけ残す
        # (Nagato 決定)。控えには残すので、以後は黙って見送る。
        state["refire_notice_payload"] = str(payload or "")
        state["refire_notice_at"] = time.time()
        try:
            save_fn(state)
        except Exception:  # noqa: BLE001  控えの保存失敗は黙る
            pass
        self._diag(
            f"応答済みの合図の再検出: {label}（知らせは送りません）")

    def _on_refire_notice_done(self, result):
        self._refire_sending = False
        if getattr(result, "ok", False):
            self._diag("応答済みの知らせを送りました")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。
            self._note_deferred("応答済みの知らせ")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"応答済みの知らせに失敗: {message}")
        self._fire_deferred_restart()

    def _note_calm(self, text: str):
        """同じ文面の繰り返しログを畳む (2.35)。

        再応答の抑止のように、同じ行が何度も出るものは
        15分に1行だけ書き、間の分は件数だけ添える。同定子
        (mid) で止めると、ずれのたびに初回扱いになって
        畳めなかった (2026-11-16 実地)。"""
        now = time.time()
        entry = self._log_calm.get(text)
        if entry is not None and now - entry[0] \
                < core.MARKER_REFIRE_SUPPRESS_SEC:
            entry[1] += 1
            return
        if len(self._log_calm) > 100:
            self._log_calm.clear()
            entry = None
        skipped = entry[1] if entry is not None else 0
        self._log_calm[text] = [now, 0]
        if skipped:
            text = f"{text}（間に{skipped}回省略）"
        self._diag(text)

    def _post_react_note_once(self, mid, reason, text):
        """見送りの理由をチャットへ1行出す (2.33、同じ目印・
        同じ理由では1度だけ)。

        新しい目印には必ず何か反応する (Nagato 決定: だんまり
        は NG)。応答そのものが反応になる場合は使わない。
        連鎖が埋まっていれば、空いてから送る (キューに残す)。
        text が None のときは何もしない (2.46: 同じ中身の
        再検出は無言で見送る)。"""
        if not text:
            return
        # 重複止めは発言の同定子 (mid) では行わない (2.34)。
        # mid は再描画でずれるため、ずれるたびに初回扱いに
        # なって知らせが連発した (2026-11-16 実地)。同じ
        # (理由, 文面) の知らせは、抑止時間 (15分) の中では
        # 1回だけにする。文面には応答時刻が入るので、正規の
        # 次の見送りは文面が変わって届く。
        stamp = (reason, text)
        now = time.time()
        if now - self._react_noted.get(stamp, 0.0) \
                < core.MARKER_REFIRE_SUPPRESS_SEC:
            return
        if len(self._react_noted) > 200:
            self._react_noted.clear()
        self._react_noted[stamp] = now
        if text not in self._react_notes:
            self._react_notes.append(text)
        # コマンド履歴にも見送りとして残す (2.42)。実際に
        # 知らせを出すときだけ (上の重複止めを通ったときだけ)。
        self._record_notice_command(reason, text)
        self._flush_react_notes()

    def _flush_react_notes(self):
        """リアクションのキューを、連鎖が空いていれば送る。"""
        if self._react_sending or not self._react_notes:
            return
        if self._chain_in_flight():
            return
        try:
            configured, profile = self._resolve_ai_target()
        except Exception:  # noqa: BLE001  送れなければ諦める
            self._react_notes.clear()
            return
        if not configured:
            self._react_notes.clear()
            return
        text = self._react_notes.pop(0)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._react_sending = True

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False,
                    f"リアクションを送れませんでした（{e}）")
            self._react_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-react-note").start()

    def _on_react_done(self, result):
        self._react_sending = False
        if not getattr(result, "ok", False):
            message = str(getattr(result, "message", "") or "")
            self._diag(f"リアクションの送信に失敗: {message}")
        self._flush_react_notes()
        self._fire_deferred_restart()

    def _suppressed_chat_text(self, label, state, payload, rid=""):
        """再応答の見送りをチャットへ出す文 (2.33)。

        rid (依頼 ID) が分かる合図では、見出しを「[PHAL:種別
        ID]」の形にする (2.40)。2.46 からは、同じ中身の再検出
        では知らせを出さない (None を返す。呼び側は None を
        受けたら黙る)。出すのは、依頼 ID は同じで内容が違う
        「ID の使い回し」のときだけ。"""
        head = f"[PHAL:{label} {rid}]" if rid else f"[PHAL:{label}]"
        at = self._refire_answered_at(state, payload)
        if at:
            return None  # 同じ中身の再検出。知らせは出さない
        entry = self._answered_id_entry(state, payload)
        if entry:
            when = time.strftime(
                "%H:%M", time.localtime(entry[0])) if entry[0] else ""
            if when:
                return (f"{head} {when} に応答済みの依頼IDのため"
                        "見送ります（内容が異なるため、IDの"
                        "使い回しとして扱いました。新しいIDで"
                        "出し直してください）")
            return (f"{head} 応答済みの依頼IDのため見送ります"
                    "（内容が異なるため、IDの使い回しとして"
                    "扱いました。新しいIDで出し直してください）")
        return f"{head} 応答済みのため見送ります"

    # ---------------- コマンド履歴 (2.42) ----------------
    # 知らせ ([PHAL:種別 ID] の文) から種別を引くための対応。
    # 見出しの種別名は合図ごとに決まっている (2.40 で統一)。
    _NOTICE_KIND_BY_LABEL = {
        "診断": "diag", "設定": "settings",
        "マニュアル": "manual", "マニュアル再送": "manual",
        "リセット": "reset", "再実行": "rerun",
        "再送": "resend", "結果再送": "resend",
        "ファイル": "file", "全文": "fulltext",
        "一覧": "history", "停止": "stop", "ログ": "log",
        "ツール": "tool", "道具説明": "describe",
        "道具評価": "eval", "入力欄": "input",
        "入力上書き": "overwrite", "強制送信": "force",
        "中継": "relay", "中継失敗": "relay",
        "中継見送り": "relay",
    }
    _NOTICE_REASON_TEXT = {
        "no_id": "依頼IDがないため見送り",
        "no_arg": "引数が読めないため見送り",
        "off": "自動応答が切のため見送り",
        "emergency": "非常停止中のため破棄",
        "refire": "応答済みのため見送り",
    }
    # 中継の却下 (_note_relay_once の理由名) → 履歴の理由。
    _RELAY_NACK_REASON_TEXT = {
        "no_id": "依頼IDがないため中継しません",
        "bad-arg": "引数が分からないため中継しません",
        "no-dst": "送り先を見張っていないため中継しません",
        "no-dual": "見張っている窓が1つだけのため中継しません",
        "need-arg": "送り先の指定が無いため中継しません",
        "empty-body": "本文が無いため中継しません",
        "loop": "ループ防止のため中継しません",
        "off": "自動中継が切のため中継しません",
    }

    def _find_command_record(self, kind, request_id):
        """同じ種別・同じ依頼 ID の履歴の記録を探す (2.46)。

        依頼履歴は1依頼1行が原則 (再検出のたびに行が増える
        のは誤解のもと)。新しい順に探して、あればその dict
        を返す。依頼 ID が空なら探さない (同定できない)。"""
        if not request_id:
            return None
        for record in reversed(self._command_history or []):
            if isinstance(record, dict) \
                    and record.get("kind") == kind \
                    and record.get("request_id") == request_id:
                return record
        return None

    def _new_command_record(self, kind, request_id="",
                            summary="", response="", ai=""):
        """コマンド履歴へ新しい記録を1件足す (2.42)。

        ai は合図が見つかった窓の名前 (走査の処理中は
        _dispatch_state から取る)。戻り値は履歴の並びに入った
        dict そのもの (後から状態を更新できる)。2.46 からは、
        同じ種別・同じ依頼 ID の記録が既にあれば新しい行を
        作らず、その記録を返す (1依頼1行)。"""
        existing = self._find_command_record(kind, request_id)
        if existing is not None:
            return existing
        if not ai:
            dispatch = getattr(self, "_dispatch_state", None)
            ai = dispatch.name if dispatch is not None else ""
        record = core.command_history_new(
            ai=ai, kind=kind, request_id=request_id,
            summary=summary, response=response)
        self._command_seq += 1
        record["key"] = (f"c{int(record['at'] * 1000000)}"
                         f"-{self._command_seq}")
        self._command_history = core.command_history_append(
            self._command_history, record)
        core.save_command_history(self._command_history)
        self._refresh_list()
        return record

    def _begin_command(self, kind, request_id="", summary="",
                       response="", ai=""):
        """合図への応答を送り始める時点で、コマンド履歴へ
        「保留」の記録を1件残す (2.42)。

        完了側は _finish_command で同じ記録を確定させる。
        同じ種別の送信が既に進行中なら、その記録を返す
        (種別ごとの送信は直列なので、記録も1件で足りる)。"""
        existing = self._pending_commands.get(kind)
        if existing is not None:
            return existing
        record = self._new_command_record(
            kind, request_id, summary, response, ai=ai)
        if record.get("status") == "answered":
            # 応答済みの記録が既にある (2.46 の1依頼1行で返って
            # きた)。確定済みの行を保留として触らない。
            return record
        self._pending_commands[kind] = record
        return record

    def _finish_command(self, kind, result=None, ok=False,
                        message="", response=None):
        """送信中だったコマンド記録を、結果で確定させる
        (2.42)。result は自動応答の完了オブジェクト
        (_AiAnswerResult)。ok/message を直接渡す形でもよい。
        response が無ければ、裏方で預けた応答文
        (_command_texts) を使う。"""
        record = self._pending_commands.pop(kind, None)
        if record is None:
            self._command_texts.pop(kind, None)
            return
        needs_confirm = False
        if result is not None:
            ok = bool(getattr(result, "ok", False))
            needs_confirm = bool(
                getattr(result, "needs_confirm", False))
            message = str(getattr(result, "message", "") or "")
        if response is None:
            response = self._command_texts.pop(kind, None)
        if response is not None:
            record["response"] = \
                core.truncate_command_response(response)
        if needs_confirm:
            record["status"] = "skipped"
            record["reason"] = "入力欄に書きかけがあるため見送り"
        elif ok:
            record["status"] = "answered"
            record["reason"] = ""
        else:
            record["status"] = "skipped"
            record["reason"] = (
                f"送信に失敗: {message}" if message
                else "送信に失敗")
        core.save_command_history(self._command_history)
        self._refresh_list()

    def _update_pending_command(self, kind, reason):
        """送信中のコマンド記録の理由だけを差し替える (2.42、
        中継の送り直し待ちなど、保留のまま様子が変わる分)。"""
        record = self._pending_commands.get(kind)
        if record is None:
            return
        record["reason"] = str(reason or "")
        core.save_command_history(self._command_history)
        self._refresh_list()

    def _record_command(self, kind, request_id="", summary="",
                        status="skipped", reason="", response="",
                        ai=""):
        """その場で確定するコマンド記録を1件残す (2.42)。

        知らせで終わる合図と、送信の完了コールバックを持た
        ない合図 (リセット) のための入口。送信中の記録
        (_pending_commands) には触らない。2.46 からは、同じ
        依頼の応答済みの記録が既にあれば、それを最終結果と
        して触らない (知らせの行で上書きしない)。"""
        existing = self._find_command_record(kind, request_id)
        if existing is not None and \
                existing.get("status") == "answered":
            return existing
        record = self._new_command_record(
            kind, request_id, summary, response, ai=ai)
        record["status"] = status
        record["reason"] = reason
        core.save_command_history(self._command_history)
        self._refresh_list()
        return record

    def _record_notice_command(self, reason, text):
        """知らせで終わる合図を、コマンド履歴へ見送りとして
        残す (2.42)。種別と依頼 ID は知らせの見出し
        ([PHAL:種別 ID]) から取る。種別が対応表に無い知らせ
        (道具登録など、コマンド行の対象外) は残さない。
        応答文は知らせの文そのもの。"""
        match = re.match(r"^\[PHAL:([^\s\]]+)(?:\s+([^\]]+))?\]",
                         text or "")
        if not match:
            return
        kind = self._NOTICE_KIND_BY_LABEL.get(match.group(1))
        if kind is None:
            return
        rid = (match.group(2) or "").split(" ", 1)[0]
        reason_text = self._NOTICE_REASON_TEXT.get(
            reason, "見送り")
        if reason == "refire" and "使い回し" in (text or ""):
            reason_text = "IDの使い回しのため見送り"
        self._record_command(
            kind, rid, status="skipped",
            reason=reason_text,
            response=text)

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
        消費済みでなく、回答・送信の連鎖が空いているときだけ
        (2.31 で送信間隔の見送りは廃止)。同じ発言かどうかは、
        全文の同定子 id か、目印を含む直近40行の第2の同定子
        key のどちらか一致すれば消費済みとする (1-49。会話の
        木のずれで id が漂っても、key が同じなら二度送らない)。
        連鎖が理由の間は見送りにして、見張りの次の回で改めて
        判定する (目印が最新の発言に残っている限り、また届く)。
        非常停止中の目印は消費して捨てる (遅れては送らない)。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._diag_marker_pending = None
            return
        consumed_id = str(self._diag_auto.get("consumed_id") or "")
        consumed_key = str(self._diag_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._diag_marker_pending = None
            self._note_diag_once(
                mid, "consumed",
                "診断の合図: 消費済みの発言のため見送り")
            return  # 消費済みの発言。二度は送らない
        if not req:
            self._diag_marker_pending = None
            self._consume_diag_marker(mid, key)
            self._note_diag_once(
                mid, "no_id",
                "診断の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:診断] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._diag_auto, req):
            # 同じ依頼IDへの応答は15分は繰り返さない (2.38)。
            # 診断だけ消費済みの単一枠で判定していたため、会話が
            # 進んで同定子 (id/key) がずれると別件扱いになり、
            # 診断の束を何度も送り直した (2026-11-17 実地)。
            # 他の合図と同じく、応答履歴 (payload=依頼ID) で止める。
            self._diag_marker_pending = None
            self._note_diag_once(
                mid, "refire",
                "診断の合図: 再応答を抑止（同定子が外れた再検出と"
                "みられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "診断", self._diag_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_diag"]:
            self._note_diag_once(
                mid, "off",
                "診断リクエストを検出（自動応答は切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:診断 {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._diag_marker_pending = None
            self._consume_diag_marker(mid, key)
            self._note_diag_once(
                mid, "emergency",
                "診断の自動応答: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:診断 {req}] 非常停止中のため破棄しました")
            return
        if self._diag_sending:
            self._diag_marker_pending = mid
            return
        if self._chain_in_flight():
            self._diag_marker_pending = mid
            self._note_diag_once(
                mid, "busy",
                "診断の自動応答: 回答・送信の連鎖の途中のため見送り"
                "（空いてから送ります）")
            return
        self._diag_marker_pending = None
        self._fire_auto_diag(mid, key, req)

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

    def _fire_auto_diag(self, mid: str, key: str = "",
                        req: str = ""):
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
        bundle = self._diagnostics_text(request_id=req)
        if not bundle:
            self._diag("診断の自動応答に失敗: 束が作れませんでした")
            self._consume_diag_marker(mid, key)
            return
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._diag_sending = True
        # 完了シグナル側へ渡す (mid, key) の組。完了時に両方を
        # 控えへ残す (1-49)。
        self._diag_auto_mid = (mid, key, req)
        self._begin_command("diag", req, response=bundle)
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
        self._finish_command("diag", result)
        pair, self._diag_auto_mid = self._diag_auto_mid, None
        # 旧い呼び出し (mid だけ) からの完了にも耐える。
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        answered_req = (pair[2] if isinstance(pair, tuple)
                        and len(pair) > 2 else "")
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._diag_auto["last_sent_at"] = time.time()
            self._record_answered(self._diag_auto, answered_req)
            self._consume_diag_marker(mid, key)
            self._diag("診断を自動で送りました", rid=answered_req or None)
            self._set_note("診断を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            # 入力欄に書きかけがあった。消さずに見送る (この道具の
            # 線引き)。理由は診断に残し、目印は消費する。
            self._consume_diag_marker(mid, key)
            self._diag(
                f"診断の自動応答を見送り（入力欄に書きかけ）: {message}", rid=answered_req or None)
            self._set_note(
                "診断の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("診断の自動応答")
        else:
            self._consume_diag_marker(mid, key)
            self._diag(f"診断の自動応答に失敗: {message}", rid=answered_req or None)
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
        同じ発言を消費済みでないときだけ (2.31 で間隔の見送り
        は廃止)。同じ発言かどうかは診断の目印と同じ同定
        (id か key のどちらか一致で消費済み) で、控えは
        リセット専用 (core の reset_auto) に持つ。非常停止中の
        目印は消費して捨てる (遅れては開き直さない)。

        開き直しそのものは request_restart に任せる。実行や連鎖
        の途中なら、そちらが連鎖の終了まで保留するので、ここでは
        割り込まない。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._reset_marker_pending = None
            return
        consumed_id = str(self._reset_auto.get("consumed_id") or "")
        consumed_key = str(self._reset_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._reset_marker_pending = None
            self._note_reset_once(
                mid, "consumed",
                "リセットの合図: 消費済みの発言のため見送り")
            return  # 消費済みの発言。二度は反応しない
        if not req:
            self._reset_marker_pending = None
            self._consume_reset_marker(mid, key)
            self._note_reset_once(
                mid, "no_id",
                "リセットの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:リセット] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._reset_auto, req):
            self._reset_marker_pending = None
            self._note_calm("リセットの合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "リセット", self._reset_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_reset"]:
            self._note_reset_once(
                mid, "off",
                "リセットの合図を検出（自動で開き直すは切のため"
                "見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:リセット {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._reset_marker_pending = None
            self._consume_reset_marker(mid, key)
            self._note_reset_once(
                mid, "emergency",
                "リセットの合図: 非常停止中のため見送り"
                "（遅れては開き直しません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:リセット {req}] 非常停止中のため破棄しました")
            return
        self._reset_marker_pending = None
        self._record_answered(self._reset_auto, req)
        self._consume_reset_marker(mid, key)
        self._diag("リセットの合図を受けて開き直します")
        # リセットは応答文を送らず開き直すだけなので、記録は
        # その場で応答済みに確定させる (2.42)。
        self._record_command("reset", req, status="answered")
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

    # ---------------- 再実行の合図 (1.63) ----------------
    def _note_rerun_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (診断・リセットと同じ作法。控えは混ぜない)。"""
        if len(self._rerun_noted) > 50:
            self._rerun_noted.clear()
        stamp = (mid, key)
        if stamp in self._rerun_noted:
            return
        self._rerun_noted.add(stamp)
        self._diag(text)

    def _find_item_by_command_id(self, command_id):
        """コマンド ID から表の項目を探す。無ければ None。

        同じ ID の項目が複数あれば、新しい順 (_items の並び) の
        最初のもの (再ダウンロードの写しより先に本命を見る)。"""
        if not command_id:
            return None
        for item in self._items:
            if core.parse_command_id(item.name) == command_id:
                return item
        return None

    def _maybe_auto_rerun(self, marker):
        """見張りの結果に乗ってきた再実行の目印を判定する。

        反応するのは、切替「合図で自動で再実行する」が入って
        いて、同じ発言を消費済みでなく、直近の自動再実行から
        5分以上空いていて、実行の連鎖が空いているときだけ。
        同じ発言かどうかは診断・リセットと同じ同定 (id か key
        のどちらか一致で消費済み) で、控えは再実行専用 (core
        の rerun_auto) に持つ。対象の ID が一覧に無ければ、
        診断に1行残して消費する (何度も探し直さない)。実行
        そのものは手動の再実行 (_rerun_item) の経路を使い、
        実行済みハッシュの自動実行ガードには掛からない。結果
        は差分の形で組み立てられ、回答・送信は通常の自動化の
        切替に従う。連鎖が途中の間は見送りにして、見張りの
        次の回で改めて判定する。間隔が空いていない合図は、
        消費だけして捨てる (1.73。遅れて実行はしない)。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、コマンド ID が読めないときも同様にする。"""
        mid = ""
        key = ""
        req = ""
        command_id = ""
        args_str = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
            command_id = str(marker.get("command_id") or "")
            args_str = str(marker.get("args") or "")
        if not mid:
            self._rerun_marker_pending = None
            return
        # 再応答の抑止の payload は依頼 ID とコマンド ID と
        # 引数の組 (2.41、1.97 の「ID＋引数」の組に依頼 ID を
        # 主として足した形)。どれかが違えば別の依頼として
        # 扱う。
        payload = req + "\n" + command_id + "\n" + args_str \
            if args_str else req + "\n" + command_id
        consumed_id = str(self._rerun_auto.get("consumed_id") or "")
        consumed_key = str(self._rerun_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._rerun_marker_pending = None
            self._note_rerun_once(
                mid, "consumed",
                "再実行の合図: 消費済みの発言のため見送り")
            return  # 消費済みの発言。二度は反応しない
        if not req:
            self._rerun_marker_pending = None
            self._consume_rerun_marker(mid, key)
            self._note_rerun_once(
                mid, "no_id",
                "再実行の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:再実行] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not command_id:
            self._rerun_marker_pending = None
            self._consume_rerun_marker(mid, key)
            self._note_rerun_once(
                mid, "no_arg",
                "再実行の合図: コマンドIDが読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:再実行 {req}] コマンドIDが読めないため"
                "応答しません。「目印 依頼ID コマンドID」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._rerun_auto, payload):
            self._rerun_marker_pending = None
            self._note_calm("再実行の合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "再実行", self._rerun_auto, payload,
                    rid=req))
            return
        # 知らせの見出し (2.41 からは依頼 ID を主にする)。
        mark = f"[PHAL:再実行 {req}]"
        if not self._automation_flags()["auto_rerun"]:
            self._note_rerun_once(
                mid, "off",
                "再実行の合図を検出（自動で再実行するは切のため"
                "見送り）")
            self._post_react_note_once(
                mid, "off",
                f"{mark} 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._rerun_marker_pending = None
            self._consume_rerun_marker(mid, key)
            self._note_rerun_once(
                mid, "emergency",
                "再実行の合図: 非常停止中のため見送り"
                "（遅れては再実行しません）")
            self._post_react_note_once(
                mid, "emergency",
                f"{mark} 非常停止中のため破棄しました")
            return
        item = self._find_item_by_command_id(command_id)
        if item is None:
            # 対象が一覧に無い。見張りのたびに探し直しても結果は
            # 同じなので、診断に残して消費する (最小の記録)。
            self._diag(
                "再実行の合図: 対象が見つかりません"
                f"（ID {command_id}）")
            self._consume_rerun_marker(mid, key)
            return
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or item.result is None:
            # 実行・回答の連鎖の途中か、対象のチェックがまだ
            # 済んでいない。見送りにして次の回で改めて判定する。
            self._rerun_marker_pending = mid
            self._note_rerun_once(
                mid, "busy",
                "再実行の合図: 実行の連鎖の途中のため見送り"
                "（空いてから再実行します）")
            return
        try:
            run_args = core.split_command_args(args_str)
        except ValueError:
            self._diag(
                "再実行の合図: 引数の引用符が閉じていません"
                f"（{args_str}）")
            self._consume_rerun_marker(mid, key)
            self._rerun_marker_pending = None
            return
        self._rerun_marker_pending = None
        self._record_answered(self._rerun_auto, payload)
        self._consume_rerun_marker(mid, key)
        self._diag(f"再実行の合図を受けて再実行します: {item.name}")
        self._begin_command(
            "rerun", req,
            summary=command_id
            + (f" {args_str}" if args_str else ""))
        self._rerun_item(item, run_args, request_id=req)

    def _discard_rerun_marker(self, mid, key=""):
        """目印を消費済みにするが、再実行の時刻は残す (1.73)。

        間隔内の合図を捨てるための消費。時刻まで新しいものに
        すると、合図が続く限り間隔が空かなくなってしまう。"""
        if not mid:
            return
        self._rerun_auto["consumed_id"] = mid
        if key:
            self._rerun_auto["consumed_key"] = key
        core.save_rerun_auto(self._rerun_auto)

    def _consume_rerun_marker(self, mid, key=""):
        """目印を消費済みにし、再実行した時刻と一緒に控えへ残す
        (開き直しをまたぐ)。消費と時刻を同時に残すのは、開き
        直した直後に同じ目印を再び拾っても、消費と間隔の両方で
        止まるようにするため (リセットと同じ作法)。"""
        if not mid:
            return
        self._rerun_auto["consumed_id"] = mid
        if key:
            self._rerun_auto["consumed_key"] = key
        self._rerun_auto["last_rerun_at"] = time.time()
        core.save_rerun_auto(self._rerun_auto)

    # ---------------- 結果再送の合図 (1.75) ----------------
    def _note_resend_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (他の合図と同じ作法。控えは混ぜない)。"""
        if len(self._resend_noted) > 50:
            self._resend_noted.clear()
        stamp = (mid, key)
        if stamp in self._resend_noted:
            return
        self._resend_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_resend(self, marker):
        """見張りの結果に乗ってきた結果再送の目印を判定する。

        反応するのは、切替「合図で結果を自動で再送する」が
        入っていて、同じ発言を消費済みでなく、直近の自動再送
        から60秒以上空いていて、回答・送信の連鎖が空いている
        ときだけ。同定は他の合図と同じ (id か key のどちらか
        一致で消費済み)、控えは再送専用 (core の resend_auto)。
        対象の ID の結果が控え (last_results) に無ければ、診断
        に1行残して消費する。送る文は控えの本文に再送の見出し
        を付け直したもの (core.build_resend_text)。再実行と
        違い、実行は伴わない。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、コマンド ID が読めないときも同様にする。再応答
        の抑止の payload は「依頼 ID＋コマンド ID」の組。"""
        mid = ""
        key = ""
        req = ""
        command_id = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
            command_id = str(marker.get("command_id") or "")
        if not mid:
            self._resend_marker_pending = None
            return
        payload = req + "\n" + command_id
        consumed_id = str(self._resend_auto.get("consumed_id") or "")
        consumed_key = str(self._resend_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._resend_marker_pending = None
            self._note_resend_once(
                mid, "consumed",
                "再送の合図: 消費済みの発言のため見送り")
            return  # 消費済みの発言。二度は反応しない
        if not req:
            self._resend_marker_pending = None
            self._consume_resend_marker(mid, key)
            self._note_resend_once(
                mid, "no_id",
                "再送の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:再送] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not command_id:
            self._resend_marker_pending = None
            self._consume_resend_marker(mid, key)
            self._note_resend_once(
                mid, "no_arg",
                "再送の合図: コマンドIDが読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:再送 {req}] コマンドIDが読めないため"
                "応答しません。「目印 依頼ID コマンドID」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._resend_auto, payload):
            self._resend_marker_pending = None
            self._note_calm("結果再送の合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "再送", self._resend_auto, payload,
                    rid=req))
            return
        # 知らせの見出し (2.41 からは依頼 ID を主にする)。
        mark = f"[PHAL:再送 {req}]"
        if not self._automation_flags()["auto_resend"]:
            self._note_resend_once(
                mid, "off",
                "結果再送の合図を検出（結果を自動で再送するは"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"{mark} 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._resend_marker_pending = None
            self._consume_resend_marker(mid, key)
            self._note_resend_once(
                mid, "emergency",
                "結果再送の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"{mark} 非常停止中のため破棄しました")
            return
        if self._resend_sending or self._chain_in_flight():
            self._resend_marker_pending = mid
            self._note_resend_once(
                mid, "busy",
                "結果再送の合図: 回答・送信の連鎖の途中のため"
                "見送り（空いてから送ります）")
            return
        entry = self._last_results.get(command_id)
        if not entry:
            self._diag(
                "結果再送の合図: 対象の結果がありません"
                f"（ID {command_id}）")
            self._consume_resend_marker(mid, key)
            self._resend_marker_pending = None
            return
        self._resend_marker_pending = None
        self._fire_auto_resend(mid, key, command_id, entry,
                               request_id=req)

    def _consume_resend_marker(self, mid, key=""):
        """目印を消費済みにして控えへ残す (開き直しをまたぐ)。
        再送した時刻は、送れたときに完了側で別に残す (診断の
        自動応答と同じ作法)。"""
        if not mid:
            return
        self._resend_auto["consumed_id"] = mid
        if key:
            self._resend_auto["consumed_key"] = key
        core.save_resend_auto(self._resend_auto)

    def _fire_auto_resend(self, mid, key, command_id, entry,
                          request_id=""):
        """直近の結果を、入力欄へ入れて送信まで一気にやる
        (裏方仕事)。入れる役・押す役は診断の自動応答と同じ
        (ai_sender / ai_presser)。入力欄に書きかけがあれば、
        そちらが上書きせずに断る。request_id は合図の依頼 ID
        (2.41、見出しと再応答の抑止とログの rid に使う)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"結果の自動再送に失敗: 投げ先を解決できません（{e}）")
            self._consume_resend_marker(mid, key)
            return
        if not configured:
            self._diag("結果の自動再送に失敗: AI の投げ先が未設定")
            self._consume_resend_marker(mid, key)
            self._set_note(
                "結果の自動再送: AI の投げ先が設定されていません",
                "error")
            return
        text = core.build_resend_text(
            command_id, entry.get("text") or "",
            entry.get("truncated", False),
            request_id=request_id)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._resend_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        # payload は「依頼 ID＋コマンド ID」の組 (2.41)。
        self._resend_auto_mid = (
            mid, key, request_id + "\n" + command_id, request_id)
        self._begin_command("resend", request_id,
                            summary=command_id, response=text)
        self._diag(f"結果を自動で再送します: ID {command_id}")
        self._publish_phal_status("answer", "結果を自動で再送しています")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"結果を再送できませんでした（{e}）")
            self._resend_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-resend-auto").start()

    def _on_resend_auto_done(self, result):
        self._resend_sending = False
        self._finish_command("resend", result)
        pair, self._resend_auto_mid = self._resend_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            # ログの rid は依頼 ID (2.41)。古い形の控えでは
            # payload (コマンド ID) に倒す。
            log_rid = pair[3] if len(pair) > 3 else payload
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._resend_auto["last_sent_at"] = time.time()
            self._record_answered(self._resend_auto, payload)
            self._consume_resend_marker(mid, key)
            self._diag("結果を自動で再送しました", rid=log_rid or None)
            self._set_note("結果を自動で再送しました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_resend_marker(mid, key)
            self._diag(
                f"結果の自動再送を見送り（入力欄に書きかけ）: {message}", rid=log_rid or None)
            self._set_note(
                "結果の自動再送: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("結果の自動再送")
        else:
            self._consume_resend_marker(mid, key)
            self._diag(f"結果の自動再送に失敗: {message}", rid=log_rid or None)
            self._set_note(
                f"結果を自動で再送できませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("結果を自動で再送しました")
        else:
            self._publish_phal_idle("結果の自動再送が終わりました")
        self._fire_deferred_restart()

    # ---------------- 設定確認の合図 (1.85) ----------------
    def _note_settings_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (他の合図と同じ作法)。"""
        if len(self._settings_noted) > 50:
            self._settings_noted.clear()
        stamp = (mid, key)
        if stamp in self._settings_noted:
            return
        self._settings_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_settings(self, marker):
        """設定確認の目印を判定する (1.85)。

        反応するのは、切替「合図で設定を自動で知らせる」が
        入っていて、同じ発言を消費済みでなく、直近の自動応答
        から60秒以上空いていて、回答・送信の連鎖が空いている
        ときだけ。送る文は core.build_settings_report が設定
        ファイルから組み立てる (値は毎回取り直す)。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._settings_marker_pending = None
            return
        consumed_id = str(
            self._settings_auto.get("consumed_id") or "")
        consumed_key = str(
            self._settings_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._settings_marker_pending = None
            self._note_settings_once(
                mid, "consumed",
                "設定の合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._settings_marker_pending = None
            self._consume_settings_marker(mid, key)
            self._note_settings_once(
                mid, "no_id",
                "設定の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:設定] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._settings_auto, req):
            self._settings_marker_pending = None
            self._note_calm("設定確認の合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "設定", self._settings_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_settings"]:
            self._note_settings_once(
                mid, "off",
                "設定確認の合図を検出（設定を自動で知らせるは"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:設定 {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._settings_marker_pending = None
            self._consume_settings_marker(mid, key)
            self._note_settings_once(
                mid, "emergency",
                "設定確認の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:設定 {req}] 非常停止中のため破棄しました")
            return
        if self._settings_sending or self._chain_in_flight():
            self._settings_marker_pending = mid
            self._note_settings_once(
                mid, "busy",
                "設定確認の合図: 回答・送信の連鎖の途中のため"
                "見送り（空いてから送ります）")
            return
        self._settings_marker_pending = None
        self._fire_auto_settings(mid, key, req)

    def _consume_settings_marker(self, mid, key=""):
        if not mid:
            return
        self._settings_auto["consumed_id"] = mid
        if key:
            self._settings_auto["consumed_key"] = key
        core.save_settings_auto(self._settings_auto)

    def _fire_auto_settings(self, mid, key, req=""):
        """設定の報告を、入力欄へ入れて送信まで一気にやる
        (裏方仕事)。入れる役・押す役は他の自動応答と同じ。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"設定の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_settings_marker(mid, key)
            return
        if not configured:
            self._diag("設定の自動応答に失敗: AI の投げ先が未設定")
            self._consume_settings_marker(mid, key)
            self._set_note(
                "設定の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
        except Exception:  # noqa: BLE001  空として報告する
            section = {}
        odg_tools = None
        try:
            import odougubako_core
            odg_tools = odougubako_core.list_tools()
        except Exception:  # noqa: BLE001  無くても報告は出す
            odg_tools = None
        text = core.build_settings_report(
            section, PHAL_VERSION, odg_tools, request_id=req)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._settings_sending = True
        self._settings_auto_mid = (mid, key, req)
        self._begin_command("settings", req, response=text)
        self._diag("設定を自動で知らせます")
        self._publish_phal_status("answer", "設定を自動で知らせています")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"設定を知らせられませんでした（{e}）")
            self._settings_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-settings-auto").start()

    def _on_settings_auto_done(self, result):
        self._settings_sending = False
        self._finish_command("settings", result)
        pair, self._settings_auto_mid = self._settings_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        answered_req = (pair[2] if isinstance(pair, tuple)
                        and len(pair) > 2 else "")
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._settings_auto["last_sent_at"] = time.time()
            self._record_answered(self._settings_auto, answered_req)
            self._consume_settings_marker(mid, key)
            self._diag("設定を自動で知らせました", rid=answered_req or None)
            self._set_note("設定を自動で知らせました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_settings_marker(mid, key)
            self._diag(
                f"設定の自動応答を見送り（入力欄に書きかけ）: {message}", rid=answered_req or None)
            self._set_note(
                "設定の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("設定の自動応答")
        else:
            self._consume_settings_marker(mid, key)
            self._diag(f"設定の自動応答に失敗: {message}", rid=answered_req or None)
            self._set_note(
                f"設定を自動で知らせられませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("設定を自動で知らせました")
        else:
            self._publish_phal_idle("設定の自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 中継リクエストの合図 (2.10) ----------------
    def _defer_relay_ack(self, source_name, dst_name, state,
                         request_id=""):
        """中継の成功 ACK を保留する (2.27)。

        成功の ACK は即送らず 30 秒 (core.RELAY_ACK_DELAY_SEC)
        保留し、その間に送り先からの戻り (逆向きの中継の成功)
        があれば送らない。戻りそのものが届いた証拠になるため。
        保留の前に、今回の中継が「戻り」にあたる保留中の ACK
        があれば取り消す。送り元が特定できないときは、従来ど
        おりその場で送る。失敗と却下の知らせは保留しない。
        request_id は中継の依頼 ID (2.41、ACK の見出しに入る)。"""
        head = f"[PHAL:中継受付 {request_id}]" if request_id \
            else "[PHAL:中継受付]"
        if state is not None:
            self._relay_acks_pending = [
                e for e in self._relay_acks_pending
                if not (e["dst"] == source_name
                        and e["src"] == dst_name)]
        if not source_name or state is None:
            self._post_relay_ack(
                f"{head} {dst_name}へ中継しました。"
                "返事を待っています", state=state)
            return
        self._relay_acks_pending.append({
            "src": source_name, "dst": dst_name,
            "due": time.time() + core.RELAY_ACK_DELAY_SEC,
            "text": (f"{head} {dst_name}へ"
                     "中継しました。返事を待っています"),
            "state": state})

    def _flush_due_relay_acks(self):
        """保留中の成功 ACK のうち、期限が来たものを送る
        (2.27、1秒ごとの表示付け直しから呼ばれる)。

        送る役が忙しい間は次回へ回す (割り込まない作法は
        _post_relay_ack と同じ)。期限から 120 秒を過ぎても
        送れなければ、古い知らせとして落とす。"""
        pending = getattr(self, "_relay_acks_pending", None)
        if not pending:
            return
        if self._emergency_stopped:
            # 非常停止中は知らせを出さず、保留も捨てる (2.29)。
            self._relay_acks_pending = []
            return
        now = time.time()
        keep = []
        for entry in pending:
            if now < entry["due"]:
                keep.append(entry)
                continue
            if self._relay_ack_sending or self._chain_in_flight():
                if now - entry["due"] < 120:
                    keep.append(entry)
                continue
            self._post_relay_ack(entry["text"],
                                 state=entry["state"])
        self._relay_acks_pending = keep

    def _post_relay_ack(self, text, state=None):
        """中継の経過を、送り元のチャットへ短い返事 (ACK) で
        返す (2.12、宛先は 2.17 で送り元の窓へ)。

        中継は一方通行で、成否が動作ログにしか残らず、送り元
        のチャット側からは発火したかどうか見えなかった。発火・
        見送り・成否を送り元の窓へ1件返して、見えるようにする。
        返事は回答の連鎖と同じ役 (入れる役・押す役) で送り元の
        プロファイルへ送る。連鎖の途中や送信中はログだけに
        残す (割り込まない)。state が無ければ既定の投げ先の
        窓へ返す (2.21)。"""
        if self._relay_ack_sending or self._chain_in_flight():
            return
        profile = None
        if state is not None:
            profile = getattr(state, "profile", None)
        if profile is None:
            profile = getattr(self._default_state(), "profile", None)
        if profile is None:
            try:
                configured, profile = self._resolve_ai_target()
            except Exception:  # noqa: BLE001
                return
            if not configured:
                return
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._relay_ack_sending = True

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"中継の返事を送れませんでした（{e}）")
            self._relay_ack_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-relay-ack").start()

    def _on_relay_ack_done(self, result):
        self._relay_ack_sending = False
        if getattr(result, "ok", False):
            self._diag("中継の返事（ACK）を送り元のチャットへ送りました")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"中継の返事（ACK）に失敗: {message}")

    def _note_relay_once(self, mid: str, key: str, text: str,
                         state=None, ack=True, elem: str = ""):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (2.31 から、見送りの知らせはチャットへ出さない。
        Nagato 決定: 中間の知らせは混乱の元)。
        抑止の照合は、同定子 (mid) だけでなく要素の同定子
        (elem) でも行う (2.29)。mid は木の先頭からの経路で
        作るため、チャットの表示範囲がずれると同じ要素でも
        変わってしまい、却下が連発した。非常停止中は、知らせ
        自体を出さない (診断にだけ残す)。"""
        if len(self._relay_noted) > 200:
            self._relay_noted.clear()
        stamps = {(mid, key)}
        if elem:
            stamps.add((elem, key))
        if stamps & self._relay_noted:
            return
        self._relay_noted |= stamps
        self._diag(text)
        # 中継の却下 ([PHAL:中継却下 ...] の文) は、コマンド
        # 履歴にも見送りとして残す (2.42)。却下はチャットへ
        # 出さない作法 (2.31) だが、履歴では理由が見える。
        if text.startswith("[PHAL:中継却下"):
            match = re.match(
                r"^\[PHAL:中継却下(?:\s+([^\]]+))?\]", text)
            rid = (match.group(1) or "") if match else ""
            self._record_command(
                "relay", rid, status="skipped",
                reason=self._RELAY_NACK_REASON_TEXT.get(
                    key, "中継しません"),
                response=text)

    def _stash_relay_pending(self, mid, key, body, retries=0,
                             delay=0.0, src="", dst="", rid=""):
        """中継の保留を本文ごとファイルの控えへ残す (2.13)。

        保留のまま目印が古くなって走査から消えても、控えから
        送れるようにする。同じ目印の控えが既にあれば、最初の
        検出時刻と送り直し回数は引き継ぐ (寿命は最初の検出
        から数える)。rid は中継の依頼 ID (2.41、控えから送る
        ときも配達文の見出しとループ判定に使う)。"""
        now = time.time()
        existing = core.sanitize_relay_pending(
            self._relay_auto.get("pending"))
        at = now
        if existing is not None and existing["id"] == mid:
            at = existing["at"] or now
            if not retries:
                retries = existing["retries"]
            if not src:
                src = existing.get("src") or ""
            if not dst:
                dst = existing.get("dst") or ""
            if not rid:
                rid = existing.get("rid") or ""
        self._relay_auto["pending"] = {
            "id": mid, "key": key, "body": body, "at": at,
            "retries": int(retries),
            "not_before": now + delay if delay else 0.0,
            "src": src, "dst": dst, "rid": rid,
        }
        core.save_relay_auto(self._relay_auto)

    def _clear_relay_pending(self, mid=None):
        pending = core.sanitize_relay_pending(
            self._relay_auto.get("pending"))
        if pending is None:
            if self._relay_auto.get("pending") is not None:
                self._relay_auto["pending"] = None
                core.save_relay_auto(self._relay_auto)
            return
        if mid is not None and pending["id"] != mid:
            return
        self._relay_auto["pending"] = None
        core.save_relay_auto(self._relay_auto)

    def _maybe_fire_relay_pending(self):
        """控えに残った中継の保留を、条件がそろえば送る
        (2.13)。

        目印が走査から消えた後でも届けるための経路。送り元の
        窓の走査結果からのみ呼ばれ、寿命切れ・消費済みは控えを
        片づける。条件がそろわない間は黙って控えを残す
        (見送りの理由は検出時に知らせ済み)。"""
        pending = core.sanitize_relay_pending(
            self._relay_auto.get("pending"))
        if pending is None:
            return
        now = time.time()
        if now - pending["at"] > core.RELAY_PENDING_TTL_SEC:
            self._clear_relay_pending(pending["id"])
            self._diag("中継の保留が期限切れのため破棄しました")
            return
        consumed_id = str(self._relay_auto.get("consumed_id") or "")
        consumed_key = str(self._relay_auto.get("consumed_key") or "")
        if pending["id"] == consumed_id or (
                pending["key"]
                and pending["key"] == consumed_key):
            self._clear_relay_pending(pending["id"])
            return
        watch_list = getattr(self, "_watch_list", None) or []
        dispatch = getattr(self, "_dispatch_state", None)
        src_name = pending.get("src") or ""
        dst_name = pending.get("dst") or ""
        if src_name and dst_name:
            # 経路の記録がある控え (2.17): 送り元の窓の走査から
            # だけ送り、送り先は記録のとおり。
            if dispatch is None or dispatch.name != src_name:
                return
            target = self._watches.get(dst_name)
            if target is None:
                return
        else:
            # 2.13 までの控え (経路の記録なし) の互換経路。
            # 見張りの先頭の窓の走査から、2番目へ送る (控えの
            # 寿命は10分なので、2.17 以降に作られた控えには
            # 必ず経路が入っており、ここへ来ることはほぼ無い)。
            if len(watch_list) < 2 or dispatch is not watch_list[0]:
                return
            target = watch_list[1]
            src_name = watch_list[0].name
        if not self._automation_flags()["auto_relay"]:
            return
        if self._emergency_stopped:
            return
        if now < pending["not_before"]:
            return
        if self._relay_sending or self._chain_in_flight():
            return
        # 控えから送る直前にも、連鎖のループ判定をかけ直す
        # (2.41)。保留の間に同じ連鎖の別の中継が成功して、
        # 履歴が変わっていることがあるため。連鎖の特定は
        # 検出時と同じ relay_find_chain (起点＋ID) で行い、
        # 特定した起点は完了側の履歴記録まで持ち回る。
        rid = pending.get("rid") or ""
        fire_dst = dst_name or (getattr(target, "name", "") or "")
        chain_origin = src_name
        chain_entry = None
        if rid:
            chain_origin, chain_entry = core.relay_find_chain(
                self._relay_auto.get("relay_ids"), src_name, rid)
        if rid and fire_dst:
            reason = core.relay_loop_reason(
                chain_entry, core.relay_body_hash(pending["body"]),
                fire_dst)
            if reason is not None:
                self._clear_relay_pending(pending["id"])
                self._note_relay_once(
                    pending["id"], "loop",
                    f"[PHAL:中継却下 {rid}] {reason}",
                    state=dispatch, elem=pending["key"])
                return
        retries = pending["retries"]
        self._clear_relay_pending(pending["id"])
        self._diag("中継の保留を控えから送ります")
        self._fire_auto_relay(pending["id"], pending["key"],
                              pending["body"], target,
                              retries=retries, source_name=src_name,
                              request_id=rid,
                              chain_origin=chain_origin)

    def _relay_route_for_arg(self, arg: str):
        """中継の引数「送り元2送り先」をプロファイル名の組へ
        ほどく (2.17)。

        名前は設定のプロファイル一覧と照合する (組の全体が
        一致するものを探すので、「2」を含む名前でも壊れない)。
        ほどけなければ None。"""
        try:
            import script_runner_ai as sr_ai

            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
            profiles, _active = sr_ai.sanitize_profiles(section)
        except Exception:  # noqa: BLE001  ほどけないものとして扱う
            return None
        names = [p["name"] for p in profiles]
        for src_name in names:
            for dst_name in names:
                if src_name != dst_name and \
                        arg == f"{src_name}2{dst_name}":
                    return src_name, dst_name
        return None

    def _maybe_auto_relay(self, marker):
        """中継リクエストの目印を判定する (2.10、経路は 2.17)。

        経路は目印の引数「送り元2送り先」で決まる (引数が
        無ければ、見張りが2つのときだけ「もう一方」へ)。
        引数つきは、送り元の窓からの合図のときだけ反応する。
        あわせて、切替「合図で別のAIへ自動で中継する」が
        入っていて、同じ発言を消費済みでなく、直近の中継から
        60秒以上空いていて、回答・送信の連鎖が空いているとき
        だけ送る。送る文は目印の次の行から発言の末尾まで
        (走査側が body に入れて寄こす)。受け付けられない
        目印 (引数がほどけない・本文が無い・自動中継がオフ
        など) には、理由を「[PHAL:中継却下] 」で送り元へ返す
        (2.19)。保留になった中継は
        本文と経路ごと控えに残し、目印が消えた後でも控えから
        送る (2.13)。
        2.41 で依頼 ID が必須になった: 無ければ NACK (ID が
        ないため中継しません) を返して消費する。あわせて、連鎖
        (起点＋依頼 ID) ごとの経路履歴 (控えの relay_ids) で
        ループ防止の3ルールを判定する (core.relay_loop_reason)。
        却下の知らせの出し方は従来どおり _note_relay_once
        (2.31 からチャットへ出さず診断だけ) にそろえ、見出しに
        ID を入れる。"""
        mid = ""
        key = ""
        body = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            body = str(marker.get("body") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._relay_marker_pending = None
            self._maybe_fire_relay_pending()
            return
        consumed_id = str(self._relay_auto.get("consumed_id") or "")
        consumed_key = str(self._relay_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._relay_marker_pending = None
            self._clear_relay_pending(mid)
            return
        watch_list = getattr(self, "_watch_list", None) or []
        dispatch = getattr(self, "_dispatch_state", None)
        # 却下の見出し (2.41 からは ID があれば入れる)。
        nack_head = f"[PHAL:中継却下 {req}]" if req \
            else "[PHAL:中継却下]"
        if not req:
            # 依頼 ID が無い中継は受け付けない (2.41)。形式の
            # 誤りは直らないので消費済みにし、再検出で却下を
            # 繰り返さない (他の形式誤りと同じ作法、2.29)。
            self._relay_marker_pending = None
            self._consume_relay_marker(mid, key)
            self._note_relay_once(
                mid, "no_id",
                "[PHAL:中継却下] 依頼IDがないため中継しません。"
                "「目印 依頼ID 経路引数 本文」の形で出し直して"
                "ください", state=dispatch, elem=key)
            return
        if self._refire_suppressed(self._relay_auto, body):
            self._relay_marker_pending = None
            self._note_relay_once(
                mid, "refire",
                "中継リクエストの再応答を抑止（同定子が外れた"
                "再検出とみられる）", state=dispatch, elem=key)
            return
        # 経路の解決 (2.17)。引数つきは送り元の窓からの合図
        # だけを受け、送り先は引数のとおり。引数なしは、見張り
        # が2つのときだけ「もう一方」へ送る。
        arg = ""
        if isinstance(marker, dict):
            arg = str(marker.get("arg") or "")
        dst_state = None
        if arg:
            pair = self._relay_route_for_arg(arg)
            if pair is None:
                # 引数の誤りは直らないので消費済みにし、再検出で
                # 却下を繰り返さない (2.29)。
                self._consume_relay_marker(mid, key)
                self._note_relay_once(
                    mid, "bad-arg",
                    f"{nack_head} 引数が分かりません"
                    f"（{arg}）", state=dispatch, elem=key)
                return
            src_name, dst_name = pair
            if dispatch is None or dispatch.name != src_name:
                self._note_relay_once(
                    mid, "wrong-src",
                    f"中継リクエスト: 送り元（{src_name}）の"
                    "窓でないため中継しません",
                    state=dispatch, ack=False, elem=key)
                return
            dst_state = self._watches.get(dst_name)
            if dst_state is None:
                self._note_relay_once(
                    mid, "no-dst",
                    f"{nack_head} 送り先（{dst_name}）を"
                    "見張っていないため送れません", state=dispatch, elem=key)
                return
        else:
            if len(watch_list) < 2:
                self._note_relay_once(
                    mid, "no-dual",
                    f"{nack_head} 見張っている窓が"
                    "1つだけのため送れません",
                    state=dispatch, elem=key)
                return
            if len(watch_list) > 2:
                self._note_relay_once(
                    mid, "need-arg",
                    f"{nack_head} 見張りが3つ以上あるため、"
                    "引数で送り先を指定してください",
                    state=dispatch, elem=key)
                return
            if dispatch is None:
                return
            dst_state = watch_list[1] \
                if dispatch is watch_list[0] else watch_list[0]
        src_name = dispatch.name if dispatch is not None else ""
        dst_name = dst_state.name
        if not self._automation_flags()["auto_relay"]:
            self._note_relay_once(
                mid, "off",
                f"{nack_head} 自動中継がオフのため"
                "送りませんでした", state=dispatch, elem=key)
            return
        if not body:
            # 本文が無い目印は受け付けず、理由を NACK で返す
            # (2.19)。黙って見送ると、送り元は「まだ見えて
            # いない」のか「形式ミス」なのか区別できない。
            # 形式の誤りは直らないので消費済みにし、再検出で
            # 却下を繰り返さない (2.29)。
            self._consume_relay_marker(mid, key)
            self._note_relay_once(
                mid, "empty-body",
                f"{nack_head} 本文がありません（目印の次の行か、"
                "目印の行の引数の後ろに本文を書いてください）",
                state=dispatch, elem=key)
            return
        # ループ防止の判定 (2.41)。連鎖 (起点＋依頼 ID) を
        # 特定して、その経路履歴に対して3ルールを見る
        # (core.relay_find_chain / relay_loop_reason)。履歴は
        # 中継が成功したときに _on_relay_auto_done で記録
        # される。断る場合は消費済みにして、再検出で繰り返
        # さない。特定した起点は完了側の履歴記録まで持ち回る。
        chain_origin, chain_entry = core.relay_find_chain(
            self._relay_auto.get("relay_ids"), src_name, req)
        loop_reason = core.relay_loop_reason(
            chain_entry, core.relay_body_hash(body), dst_name)
        if loop_reason is not None:
            self._relay_marker_pending = None
            self._consume_relay_marker(mid, key)
            self._note_relay_once(
                mid, "loop",
                f"{nack_head} {loop_reason}",
                state=dispatch, elem=key)
            return
        if self._refire_answered_at(self._relay_auto, body) \
                is not None:
            self._relay_marker_pending = None
            self._maybe_refire_notice(
                self._relay_auto, body, "中継",
                core.save_relay_auto)
            return
        if self._emergency_stopped:
            self._relay_marker_pending = mid
            self._stash_relay_pending(
                mid, key, body, src=src_name, dst=dst_name,
                rid=req)
            self._note_relay_once(
                mid, "emergency",
                "中継リクエスト: 非常停止中のため見送り",
                state=dispatch, elem=key)
            return
        if self._relay_sending or self._chain_in_flight():
            self._relay_marker_pending = mid
            self._stash_relay_pending(
                mid, key, body, src=src_name, dst=dst_name,
                rid=req)
            self._note_relay_once(
                mid, "busy",
                "中継リクエスト: 回答・送信の連鎖の途中のため"
                "見送り（空いてから送ります）",
                state=dispatch, elem=key)
            return
        self._relay_marker_pending = None
        pending = core.sanitize_relay_pending(
            self._relay_auto.get("pending"))
        retries = pending["retries"] if (
            pending is not None and pending["id"] == mid) else 0
        self._clear_relay_pending(mid)
        self._fire_auto_relay(mid, key, body, dst_state,
                              retries=retries, source_name=src_name,
                              request_id=req,
                              chain_origin=chain_origin)

    def _consume_relay_marker(self, mid, key=""):
        if not mid:
            return
        self._relay_auto["consumed_id"] = mid
        if key:
            self._relay_auto["consumed_key"] = key
        pending = core.sanitize_relay_pending(
            self._relay_auto.get("pending"))
        if pending is not None and pending["id"] == mid:
            self._relay_auto["pending"] = None
        core.save_relay_auto(self._relay_auto)

    def _fire_auto_relay(self, mid, key, body, target_state,
                         retries=0, source_name="", request_id="",
                         chain_origin=""):
        """中継の文を、送り先の窓の入力欄へ入れて送信まで
        一気にやる (裏方仕事)。入れる役・押す役は他の自動応答
        と同じで、投げ先だけ送り先のプロファイルに差し替える。
        retries は送り直しの回数 (2.13、完了側で上限を見る)。
        source_name は送り元のプロファイル名 (2.17、ACK の
        宛先に使う)。届ける文には、送り元の表示を先頭に
        付ける (2.30)。request_id は中継の依頼 ID (2.41、
        配達文と ACK・失敗の見出し、完了側の履歴記録に使う)。
        chain_origin は連鎖の起点 (2.41、検出時に
        relay_find_chain で特定したもの。完了側で履歴を記録
        する連鎖の帰属に使う)。"""
        fail_head = f"[PHAL:中継失敗 {request_id}]" if request_id \
            else "[PHAL:中継失敗]"
        profile = getattr(target_state, "profile", None)
        name = getattr(target_state, "name", "") or ""
        if profile is None:
            try:
                configured, profile = \
                    self._resolve_profile_by_name(name)
            except Exception as e:  # noqa: BLE001
                self._diag(
                    f"中継に失敗: 投げ先を解決できません（{e}）",
                    ai=source_name or None)
                self._consume_relay_marker(mid, key)
                self._record_command(
                    "relay", request_id, summary=body,
                    status="skipped",
                    reason="投げ先を解決できません",
                    response=f"{fail_head} 投げ先を解決できません",
                    ai=source_name)
                self._post_relay_ack(
                    f"{fail_head} 投げ先を解決できません",
                    state=self._watches.get(source_name))
                return
            if not configured:
                profile = None
        if profile is None:
            self._diag(f"中継に失敗: {name}の投げ先が未設定",
                       ai=source_name or None)
            self._consume_relay_marker(mid, key)
            self._set_note(
                f"中継: {name}の投げ先が設定されていません",
                "error")
            self._record_command(
                "relay", request_id, summary=body,
                status="skipped", reason="投げ先が未設定",
                response=(f"{fail_head} {name}の投げ先が"
                          "設定されていません"),
                ai=source_name)
            self._post_relay_ack(
                f"{fail_head} {name}の投げ先が"
                "設定されていません",
                state=self._watches.get(source_name))
            return
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        # 届ける文の先頭に、送り元を示す一行を付ける (2.30)。
        # 中継された文は、届け先では本人の発言と見分けが
        # つかない、という Nagato の指摘から。控えや再検出の
        # 照合は元の本文のままで、付けるのは送信のときだけ。
        # 2.41 で見出しに依頼 ID も入れる (受けた側が次へ
        # 中継するときに同じ ID を使うための目印)。
        if source_name:
            if request_id:
                send_text = (f"[PHAL:中継 {request_id}] "
                             f"{source_name} からのメッセージ"
                             f"\n{body}")
            else:
                send_text = (f"[PHAL:中継] {source_name}"
                             f" からのメッセージ\n{body}")
        else:
            send_text = body
        self._relay_sending = True
        self._relay_auto_mid = (mid, key, body, name, retries,
                                source_name, request_id,
                                chain_origin)
        if retries == 0:
            # 配達文そのものを応答文として記録する (2.42)。
            self._begin_command(
                "relay", request_id, summary=body,
                response=send_text, ai=source_name)
        self._diag(f"{name}へ中継します", ai=source_name or None)
        self._publish_phal_status(
            "send", f"{name}へ中継しています")

        import threading

        def work():
            try:
                result = sender(send_text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"中継できませんでした（{e}）")
            self._relay_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-relay-auto").start()

    def _on_relay_auto_done(self, result):
        self._relay_sending = False
        saved, self._relay_auto_mid = self._relay_auto_mid, None
        source_name = ""
        request_id = ""
        chain_origin = ""
        if isinstance(saved, tuple) and len(saved) == 8:
            mid, key, body, name, retries, source_name, \
                request_id, chain_origin = saved
        elif isinstance(saved, tuple) and len(saved) == 7:
            mid, key, body, name, retries, source_name, \
                request_id = saved
        elif isinstance(saved, tuple) and len(saved) == 6:
            mid, key, body, name, retries, source_name = saved
        elif isinstance(saved, tuple) and len(saved) == 5:
            mid, key, body, name, retries = saved
        elif isinstance(saved, tuple) and len(saved) == 4:
            mid, key, body, name = saved
            retries = 0
        else:
            mid, key, body, name, retries = "", "", "", "", 0
        fail_head = f"[PHAL:中継失敗 {request_id}]" if request_id \
            else "[PHAL:中継失敗]"
        ack_state = self._watches.get(source_name) \
            if source_name else None
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._relay_auto["last_sent_at"] = time.time()
            self._record_answered(self._relay_auto, body)
            # 連鎖 (起点＋ID) の経路履歴へ記録する (2.41、
            # ループ防止の材料。消費の保存で一緒にファイルへ
            # 残る)。起点は検出時に特定した chain_origin で、
            # 無ければ送り元の窓 (新規連鎖) とみなす。
            if request_id:
                self._relay_auto["relay_ids"] = \
                    core.relay_history_record(
                        self._relay_auto.get("relay_ids"),
                        chain_origin or source_name,
                        request_id, core.relay_body_hash(body),
                        source_name, name)
            self._consume_relay_marker(mid, key)
            self._clear_relay_pending(mid)
            self._finish_command("relay", result)
            self._diag(f"{name}へ中継しました",
                       ai=source_name or None,
                       rid=request_id or None)
            self._set_note(f"{name}へ中継しました", "ok")
            self._defer_relay_ack(source_name, name, ack_state,
                                  request_id=request_id)
        elif getattr(result, "needs_confirm", False):
            self._consume_relay_marker(mid, key)
            self._clear_relay_pending(mid)
            self._finish_command("relay", result)
            self._diag(
                f"中継を見送り（入力欄に書きかけ）: {message}",
                ai=source_name or None)
            self._set_note(
                f"中継: {name}の入力欄に書きかけがあるため"
                "送りませんでした", "warn")
            skip_head = f"[PHAL:中継見送り {request_id}]" \
                if request_id else "[PHAL:中継見送り]"
            self._post_relay_ack(
                f"{skip_head} {name}の入力欄に"
                "書きかけがあります", state=ack_state)
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。消費もクリアもせず、
            # 送り直しの回数を数えずに控え直す (失敗の
            # 送り直しと同じ経路)。失敗の ACK は出さない。
            self._stash_relay_pending(
                mid, key, body, retries=retries,
                delay=5.0,
                src=source_name, dst=name, rid=request_id)
            self._note_deferred("中継")
        elif retries < core.RELAY_RETRY_MAX:
            # 失敗は消費せず、少し置いて1回だけ送り直す
            # (2.13)。主側の送信と取り合ったような一時的な
            # 失敗を、人の手を借りずに越えるため。書きかけ
            # (needs_confirm) は送り直さない (上の分岐)。
            self._stash_relay_pending(
                mid, key, body, retries=retries + 1,
                delay=core.RELAY_RETRY_DELAY_SEC,
                src=source_name, dst=name, rid=request_id)
            self._update_pending_command(
                "relay", "送信に失敗したため送り直し待ち")
            self._diag(
                f"中継に失敗: {message}"
                "（少し置いて1回だけ送り直します）",
                ai=source_name or None)
            self._set_note(
                f"{name}へ中継できませんでした（{message}）"
                "。少し置いて送り直します", "warn")
            self._post_relay_ack(
                f"{fail_head} {message}"
                "。少し置いて1回だけ送り直します",
                state=ack_state)
        else:
            self._consume_relay_marker(mid, key)
            self._clear_relay_pending(mid)
            self._finish_command("relay", result)
            self._diag(f"中継に失敗: {message}",
                       ai=source_name or None)
            self._set_note(
                f"{name}へ中継できませんでした（{message}）",
                "error")
            self._post_relay_ack(
                f"{fail_head} {message}",
                state=ack_state)
        if getattr(result, "ok", False):
            self._publish_phal_idle(f"{name}へ中継しました")
        else:
            self._publish_phal_idle("中継が終わりました")
        # 中継の送信を待っていた回答があれば、ここで進める
        # (受領の完了側と同じ取りこぼし防止、2.13)。受領の
        # 送信がまだ動いていれば預け直す。
        item, self._ack_pending_answer = self._ack_pending_answer, None
        if item is not None and (self._ack_sending
                                 or self._emergency_stopped):
            self._ack_pending_answer = item
            item = None
        if item is not None and item.last_run is not None:
            self._answer_to_ai(item)
        self._fire_deferred_restart()

    # ---------------- マニュアル再送の合図 (1.85) ----------------
    def _note_manual_once(self, mid: str, key: str, text: str):
        if len(self._manual_noted) > 50:
            self._manual_noted.clear()
        stamp = (mid, key)
        if stamp in self._manual_noted:
            return
        self._manual_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_manual(self, marker):
        """マニュアル再送の目印を判定する (1.85)。

        反応の条件は設定確認と同じ (切替 auto_manual、消費済み
        でない、60秒の間隔、連鎖が空いている)。送るのは連携
        マニュアルのファイルの添付で、自動送信までやる (Nagato
        の決定)。ファイルが無ければ、その旨の文だけを送る。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._manual_marker_pending = None
            return
        consumed_id = str(self._manual_auto.get("consumed_id") or "")
        consumed_key = str(self._manual_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._manual_marker_pending = None
            self._note_manual_once(
                mid, "consumed",
                "マニュアルの合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._manual_marker_pending = None
            self._consume_manual_marker(mid, key)
            self._note_manual_once(
                mid, "no_id",
                "マニュアルの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:マニュアル] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._manual_auto, req):
            self._manual_marker_pending = None
            self._note_calm("マニュアル再送の合図の再応答を抑止（同定子が"
                "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "マニュアル", self._manual_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_manual"]:
            self._note_manual_once(
                mid, "off",
                "マニュアル再送の合図を検出（マニュアルを自動で"
                "再送するは切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:マニュアル {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._manual_marker_pending = None
            self._consume_manual_marker(mid, key)
            self._note_manual_once(
                mid, "emergency",
                "マニュアル再送の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:マニュアル {req}] 非常停止中のため破棄しました")
            return
        if self._manual_sending or self._chain_in_flight():
            self._manual_marker_pending = mid
            self._note_manual_once(
                mid, "busy",
                "マニュアル再送の合図: 回答・送信の連鎖の途中の"
                "ため見送り（空いてから送ります）")
            return
        self._manual_marker_pending = None
        self._fire_auto_manual(mid, key, req)

    def _consume_manual_marker(self, mid, key=""):
        if not mid:
            return
        self._manual_auto["consumed_id"] = mid
        if key:
            self._manual_auto["consumed_key"] = key
        core.save_manual_auto(self._manual_auto)

    def _fire_auto_manual(self, mid, key, req=""):
        """マニュアルを添付して、入力欄へ入れて送信まで一気に
        やる (裏方仕事)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"マニュアルの自動再送に失敗: 投げ先を解決できません（{e}）")
            self._consume_manual_marker(mid, key)
            return
        if not configured:
            self._diag("マニュアルの自動再送に失敗: AI の投げ先が未設定")
            self._consume_manual_marker(mid, key)
            self._set_note(
                "マニュアルの自動再送: AI の投げ先が設定されていません",
                "error")
            return
        manual = self._intro_manual_path()
        mark = f"[PHAL:マニュアル再送 {req}]" if req \
            else "[PHAL:マニュアル再送]"
        if manual is not None:
            text = (f"{mark} 連携マニュアル"
                    "（phal-ai-manual.md）を再送します。")
            attach = [str(manual)]
        else:
            text = (f"{mark} マニュアルのファイル"
                    "（phal-ai-manual.md）が見つかりませんでした。")
            attach = []
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._manual_sending = True
        self._manual_auto_mid = (mid, key, req)
        self._begin_command("manual", req, response=text)
        self._diag("マニュアルを自動で再送します")
        self._publish_phal_status(
            "answer", "マニュアルを自動で再送しています")

        import threading

        def work():
            try:
                if attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"マニュアルを再送できませんでした（{e}）")
            self._manual_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-manual-auto").start()

    def _on_manual_auto_done(self, result):
        self._manual_sending = False
        self._finish_command("manual", result)
        pair, self._manual_auto_mid = self._manual_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        answered_req = (pair[2] if isinstance(pair, tuple)
                        and len(pair) > 2 else "")
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._manual_auto["last_sent_at"] = time.time()
            self._record_answered(self._manual_auto, answered_req)
            self._consume_manual_marker(mid, key)
            self._diag("マニュアルを自動で再送しました", rid=answered_req or None)
            self._set_note("マニュアルを自動で再送しました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_manual_marker(mid, key)
            self._diag(
                f"マニュアルの自動再送を見送り（入力欄に書きかけ）: {message}", rid=answered_req or None)
            self._set_note(
                "マニュアルの自動再送: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("手順書の自動応答")
        else:
            self._consume_manual_marker(mid, key)
            self._diag(f"マニュアルの自動再送に失敗: {message}", rid=answered_req or None)
            self._set_note(
                f"マニュアルを自動で再送できませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("マニュアルを自動で再送しました")
        else:
            self._publish_phal_idle("マニュアルの自動再送が終わりました")
        self._fire_deferred_restart()

    # ---------------- ファイルリクエストの合図 (1.88) ----------------
    def _note_file_once(self, mid: str, key: str, text: str):
        if len(self._file_noted) > 50:
            self._file_noted.clear()
        stamp = (mid, key)
        if stamp in self._file_noted:
            return
        self._file_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_file(self, marker):
        """ファイルリクエストの目印を判定する (1.88)。

        反応の条件は他の合図と同じ (切替 auto_file、消費済みで
        ない、60秒の間隔、連鎖が空いている)。送れるのはダウン
        ロードフォルダと作業フォルダの配下のファイルだけ
        (core.resolve_file_request)。見つからない・範囲外の
        ときも、その旨の文を送って消費する (要求した側が黙って
        待つ形にしない)。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、パスが読めないときも同様にする。再応答の抑止
        の payload は「依頼 ID＋パス」の組。"""
        mid = ""
        key = ""
        rid = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            rid = str(marker.get("request_id") or "")
            req = str(marker.get("path") or "")
        if not mid:
            self._file_marker_pending = None
            return
        payload = rid + "\n" + req
        consumed_id = str(self._file_auto.get("consumed_id") or "")
        consumed_key = str(self._file_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._file_marker_pending = None
            self._note_file_once(
                mid, "consumed",
                "ファイルの合図: 消費済みの発言のため見送り")
            return
        if not rid:
            self._file_marker_pending = None
            self._consume_file_marker(mid, key)
            self._note_file_once(
                mid, "no_id",
                "ファイルの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:ファイル] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not req:
            self._file_marker_pending = None
            self._consume_file_marker(mid, key)
            self._note_file_once(
                mid, "no_arg",
                "ファイルの合図: パスが読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:ファイル {rid}] パスが読めないため"
                "応答しません。「目印 依頼ID パス」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._file_auto, payload):
            self._file_marker_pending = None
            self._note_calm("ファイルリクエストの合図の再応答を抑止"
                "（同定子が外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "ファイル", self._file_auto, payload,
                    rid=rid))
            return
        if not self._automation_flags()["auto_file"]:
            self._note_file_once(
                mid, "off",
                "ファイルリクエストの合図を検出（ファイルを自動で"
                "送るは切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:ファイル {rid}] "
                "自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._file_marker_pending = None
            self._consume_file_marker(mid, key)
            self._note_file_once(
                mid, "emergency",
                "ファイルリクエストの合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:ファイル {rid}] "
                "非常停止中のため破棄しました")
            return
        if self._file_sending or self._chain_in_flight():
            self._file_marker_pending = mid
            self._note_file_once(
                mid, "busy",
                "ファイルリクエストの合図: 回答・送信の連鎖の途中"
                "のため見送り（空いてから送ります）")
            return
        self._file_marker_pending = None
        self._fire_auto_file(mid, key, req, request_id=rid)

    def _consume_file_marker(self, mid, key=""):
        if not mid:
            return
        self._file_auto["consumed_id"] = mid
        if key:
            self._file_auto["consumed_key"] = key
        core.save_file_auto(self._file_auto)

    def _prepare_file_slice(self, path, spec, request_id=""):
        """範囲指定つきのファイルリクエストの送付物を準備する
        (1.99)。

        戻り値は (文, 添付パスの並び)。バイト範囲は、切り出した
        ファイルを outbox へ置いて添付する。行の範囲は、短けれ
        ば文そのものに含め、長ければ切り出したテキストファイル
        を添付する。失敗したときは理由の文だけを返す (要求した
        側を黙って待たせない)。request_id は合図の依頼 ID
        (2.41、見出しにファイル名と併記する)。"""
        mark = f"[PHAL:ファイル送信 {request_id} {path.name}]" \
            if request_id else f"[PHAL:ファイル送信 {path.name}]"
        try:
            size = path.stat().st_size
        except OSError as e:
            return f"{mark} ファイルを読めませんでした（{e}）", []
        if spec["kind"] == "bytes":
            try:
                data, truncated = core.read_byte_range(
                    path, spec["start"], spec["end"])
            except OSError as e:
                return \
                    f"{mark} ファイルを読めませんでした（{e}）", []
            out = core.outbox_dir() / core.slice_file_name(path, spec)
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
            except OSError as e:
                return (f"{mark} 切り出したファイルを"
                        f"作れませんでした（{e}）"), []
            text = (f"{mark} ファイルのバイト範囲 "
                    f"{spec['start']}〜{spec['start'] + len(data)}"
                    f"（全{size}バイト、{len(data)}バイト分）を送ります。")
            if truncated:
                text += "範囲が大きいため上限で切りました。"
            return text, [str(out)]
        if size > 32_000_000:
            return (f"{mark} ファイルが大きいため、行の範囲では"
                    "送れません。バイト範囲 (bytes:) で指定して"
                    f"ください（{size}バイト）"), []
        try:
            raw = path.read_bytes()
        except OSError as e:
            return f"{mark} ファイルを読めませんでした（{e}）", []
        if core.looks_binary(raw):
            return (f"{mark} そのファイルはバイナリのようで、"
                    "行の範囲では送れません。バイト範囲 (bytes:) "
                    "で指定してください"), []
        body, enc = core.decode_text_bytes(raw)
        sliced, total = core.slice_text_lines(body, spec)
        end = spec["end"] if spec["end"] is not None else total
        end = min(end, total)
        head = (f"{mark} ファイルの {spec['start']}〜{end}行目"
                f"（全{total}行、{enc}）です。")
        if not sliced:
            return head + "\n（範囲に行がありません）", []
        if len(sliced) <= 3800:
            return head + "\n" + sliced, []
        out = core.outbox_dir() / core.slice_file_name(path, spec)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(sliced, encoding="utf-8", newline="")
        except OSError as e:
            return (f"{mark} 切り出したファイルを"
                    f"作れませんでした（{e}）"), []
        return head + "ファイルで送ります。", [str(out)]

    def _fire_auto_file(self, mid, key, req, request_id=""):
        """要求されたファイルを添付して、送信まで一気にやる
        (裏方仕事)。範囲の判定は core.resolve_file_request。
        request_id は合図の依頼 ID (2.41、見出し・再応答の
        抑止・ログの rid に使う)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"ファイルの自動送信に失敗: 投げ先を解決できません（{e}）")
            self._consume_file_marker(mid, key)
            return
        if not configured:
            self._diag("ファイルの自動送信に失敗: AI の投げ先が未設定")
            self._consume_file_marker(mid, key)
            self._set_note(
                "ファイルの自動送信: AI の投げ先が設定されていません",
                "error")
            return
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
        except Exception:  # noqa: BLE001
            section = {}
        work = section.get("work_folder")
        # 範囲指定 (1.99): 末尾の指定 (lines:/bytes:) を分けて
        # から、パスを解決する。指定が無ければ従来どおり丸ごと。
        req_path, spec = core.parse_file_request(req)
        path, reason = core.resolve_file_request(
            req_path, str(core.resolve_watch_folder(self.config)),
            work if isinstance(work, str) and work else None)
        # 見出しは「[PHAL:ファイル送信 依頼ID ファイル名]」
        # (2.41、依頼 ID を主にし対象を併記する)。
        if path is not None:
            mark_name = f"{request_id} {path.name}" \
                if request_id else path.name
        else:
            mark_name = f"{request_id} {req_path}" \
                if request_id else req_path
        if path is not None and spec is not None:
            text, attach = self._prepare_file_slice(
                path, spec, request_id=request_id)
        elif path is not None:
            text = f"[PHAL:ファイル送信 {mark_name}] ファイルを送ります。"
            attach = [str(path)]
        elif reason == "outside":
            text = (f"[PHAL:ファイル送信 {mark_name}] そのファイルは"
                    "ダウンロードフォルダと作業フォルダの外なので"
                    f"送れません: {req}")
            attach = []
        else:
            text = (f"[PHAL:ファイル送信 {mark_name}] ファイルが"
                    "見つかりませんでした（ダウンロードフォルダと"
                    f"作業フォルダの配下にありません）: {req}")
            attach = []
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._file_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        # payload は「依頼 ID＋パス」の組 (2.41)。
        self._file_auto_mid = (
            mid, key, request_id + "\n" + req, request_id)
        self._begin_command("file", request_id, summary=req,
                            response=text)
        self._diag(f"ファイルを自動で送ります: {req}")
        self._publish_phal_status(
            "answer", "ファイルを自動で送っています")

        import threading

        def work_send():
            try:
                if attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"ファイルを送れませんでした（{e}）")
            self._file_signals.done.emit(result)

        threading.Thread(
            target=work_send, daemon=True,
            name="phal-file-auto").start()

    def _on_file_auto_done(self, result):
        self._file_sending = False
        self._finish_command("file", result)
        pair, self._file_auto_mid = self._file_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            # ログの rid は依頼 ID (2.41)。古い形の控えでは
            # payload (パス) に倒す。
            log_rid = pair[3] if len(pair) > 3 else payload
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._file_auto["last_sent_at"] = time.time()
            self._record_answered(self._file_auto, payload)
            self._consume_file_marker(mid, key)
            self._diag("ファイルを自動で送りました", rid=log_rid or None)
            self._set_note("ファイルを自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_file_marker(mid, key)
            self._diag(
                f"ファイルの自動送信を見送り（入力欄に書きかけ）: {message}", rid=log_rid or None)
            self._set_note(
                "ファイルの自動送信: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("ファイルの自動応答")
        else:
            self._consume_file_marker(mid, key)
            self._diag(f"ファイルの自動送信に失敗: {message}", rid=log_rid or None)
            self._set_note(
                f"ファイルを自動で送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("ファイルを自動で送りました")
        else:
            self._publish_phal_idle("ファイルの自動送信が終わりました")
        self._fire_deferred_restart()

    # ---------------- キャプチャリクエストの合図 (2.54) ----------------
    def _note_capture_once(self, mid: str, key: str, text: str):
        if len(self._capture_noted) > 50:
            self._capture_noted.clear()
        stamp = (mid, key)
        if stamp in self._capture_noted:
            return
        self._capture_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_capture(self, marker):
        """キャプチャリクエストの目印を判定する (2.54)。

        反応の条件は他の合図と同じ (切替 auto_capture、消費
        済みでない、再応答の抑止、連鎖が空いている)。対象は
        Muse / Copilot / PHAL / 全画面 で、無ければ合図が
        出た窓自身を撮る。撮れなかったときも、その旨の文を
        送って消費する (要求した側が黙って待つ形にしない)。
        依頼 ID が必須 (2.41): 無ければ知らせを返して消費
        する。再応答の抑止の payload は「依頼 ID＋対象」。"""
        mid = ""
        key = ""
        rid = ""
        target = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            rid = str(marker.get("request_id") or "")
            target = str(marker.get("target") or "")
        if not mid:
            self._capture_marker_pending = None
            return
        payload = rid + "\n" + target
        consumed_id = str(self._capture_auto.get("consumed_id") or "")
        consumed_key = str(
            self._capture_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._capture_marker_pending = None
            self._note_capture_once(
                mid, "consumed",
                "キャプチャの合図: 消費済みの発言のため見送り")
            return
        if not rid:
            self._capture_marker_pending = None
            self._consume_capture_marker(mid, key)
            self._note_capture_once(
                mid, "no_id",
                "キャプチャの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:キャプチャ] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._capture_auto, payload):
            self._capture_marker_pending = None
            self._note_calm("キャプチャの合図の再応答を抑止"
                "（同定子が外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "キャプチャ", self._capture_auto, payload,
                    rid=rid))
            return
        if not self._automation_flags()["auto_capture"]:
            self._note_capture_once(
                mid, "off",
                "キャプチャの合図を検出（画面を自動で撮って"
                "送るは切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:キャプチャ {rid}] "
                "自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._capture_marker_pending = None
            self._consume_capture_marker(mid, key)
            self._note_capture_once(
                mid, "emergency",
                "キャプチャの合図: 非常停止中のため見送り")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:キャプチャ {rid}] "
                "非常停止中のため破棄しました")
            return
        if self._capture_sending or self._chain_in_flight():
            self._capture_marker_pending = mid
            self._note_capture_once(
                mid, "busy",
                "キャプチャの合図: 回答・送信の連鎖の途中"
                "のため見送り（空いてから送ります）")
            return
        self._capture_marker_pending = None
        self._fire_auto_capture(mid, key, target, request_id=rid)

    def _consume_capture_marker(self, mid, key=""):
        if not mid:
            return
        self._capture_auto["consumed_id"] = mid
        if key:
            self._capture_auto["consumed_key"] = key
        core.save_capture_auto(self._capture_auto)

    def _capture_target_image(self, target, origin_profile):
        """キャプチャの対象を撮る (2.54)。

        戻り値は (QImage | None, 表示名, ファイル名の部品,
        取れなかった理由)。対象の解釈: Muse / Copilot は
        その名前の窓、PHAL はこの窓自身、全画面は仮想画面
        全体、空なら合図が出た窓 (origin_profile)。"""
        word = _normalize_capture_word(target)
        lowered = word.lower()
        if lowered == "phal":
            pixmap = self.grab()
            if pixmap is None or pixmap.isNull():
                return None, "PHAL", "phal", \
                    "PHAL の窓を撮れませんでした"
            return pixmap.toImage(), "PHAL", "phal", ""
        if word == "全画面" or lowered == "screen":
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return None, "全画面", "screen", \
                    "画面が取れません"
            import capture_grab

            image = capture_grab.grab_region(
                screen.virtualGeometry())
            if image is None or image.isNull():
                return None, "全画面", "screen", \
                    "画面を撮れませんでした"
            return image, "全画面", "screen", ""
        profile = origin_profile
        label = "合図が出た窓"
        slug = "origin"
        if lowered in ("Muse", "muse", "copilot"):
            # Muse の窓では、チャットの表示で "Claude" が
            # "Muse" に置き換わって見える (2.56 の実地で、失敗文
            # の文字コードから確定)。合図の語が Muse でも
            # Muse の窓として受ける (2.57)。
            name = "Copilot" if lowered == "copilot" else "Muse"
            configured, by_name = self._resolve_profile_by_name(
                name)
            if not configured or by_name is None:
                return None, name, name.lower(), \
                    "その窓の投げ先が設定されていません"
            profile = by_name
            label = name
            slug = name.lower()
        elif word:
            codes = " ".join(
                f"U+{ord(ch):04X}" for ch in word[:16])
            return None, word, "unknown", \
                f"対象『{word}』が分かりません" \
                f"（文字コード: {codes}）。" \
                "対象は Muse / Copilot / PHAL / 全画面 の" \
                "いずれかで指定してください"
        if profile is None:
            return None, label, slug, \
                "投げ先が設定されていません"
        import copilot_loop

        hwnd, _render = copilot_loop.find_window(profile)
        if not hwnd:
            return None, label, slug, "窓が見つかりません"
        image, reason = _capture_window_image(hwnd)
        return image, label, slug, reason

    def _set_clipboard_image(self, image):
        """撮れた画像をクリップボードへ置く (2.55)。

        元のテキストは控えておき、送信が終わったら
        _restore_clipboard_after_capture で戻す。置けたら
        True、置けなければ False (呼び側がファイル経由に
        切り替える)。"""
        try:
            clipboard = QGuiApplication.clipboard()
            if clipboard is None:
                return False
            try:
                self._capture_clip_saved_text = clipboard.text()
            except Exception:  # noqa: BLE001  読めなくても進む
                self._capture_clip_saved_text = None
            clipboard.setImage(image)
            self._capture_clip_image = image
            return True
        except Exception:  # noqa: BLE001
            self._capture_clip_image = None
            self._capture_clip_saved_text = None
            return False

    def _restore_clipboard_after_capture(self):
        """キャプチャの配達で使ったクリップボードを元へ戻す
        (2.55)。まだ画像が載っているときだけ、控えたテキスト
        へ戻す (その間に別の物がコピーされていたら触らない)。"""
        saved = self._capture_clip_saved_text
        self._capture_clip_image = None
        self._capture_clip_saved_text = None
        if saved is None:
            return
        try:
            clipboard = QGuiApplication.clipboard()
            mime = clipboard.mimeData() if clipboard else None
            if mime is not None and mime.hasImage():
                clipboard.setText(saved)
        except Exception:  # noqa: BLE001  復元は静かに試す
            pass

    def _fire_auto_capture(self, mid, key, target, request_id=""):
        """対象の画面を撮って、合図が出た窓へ送る (2.54)。

        撮る段は窓の描画を読むのでこの場で済ませ、送る段だけ
        裏方へ回す (ファイルの自動送信と同じ作り)。撮れた
        画像はファイルに保存せず、クリップボードへ置いて
        入力欄へ貼って渡す (2.55、Nagato の提案)。貼る段は
        copilot 側の attach_image_paste。クリップボードへ
        置けないときだけ、従来どおり Downloads へ保存して
        ファイル添付で送る。撮れなかったときも理由の文だけ
        は送る。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"キャプチャの自動送信に失敗: 投げ先を解決できません（{e}）")
            self._consume_capture_marker(mid, key)
            return
        if not configured:
            self._diag(
                "キャプチャの自動送信に失敗: AI の投げ先が未設定")
            self._consume_capture_marker(mid, key)
            self._set_note(
                "キャプチャの自動送信: AI の投げ先が設定されて"
                "いません", "error")
            return
        image, label, slug, reason = self._capture_target_image(
            target, profile)
        mark = f"[PHAL:キャプチャ {request_id} {label}]" \
            if request_id else f"[PHAL:キャプチャ {label}]"
        attach = []
        paste = False
        if image is None:
            text = f"{mark} 画面を撮れませんでした（{reason}）"
        elif self.ai_sender is None and \
                self._set_clipboard_image(image):
            paste = True
            text = (f"{mark} 画面を送ります"
                    f"（{image.width()}×{image.height()}）。")
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name = f"phal-capture-{slug}-{stamp}.png"
            folder = Path(
                str(core.resolve_watch_folder(self.config)))
            path = folder / name
            try:
                folder.mkdir(parents=True, exist_ok=True)
                saved = bool(image.save(str(path), "PNG"))
            except OSError:
                saved = False
            if saved:
                text = (f"{mark} 画面を送ります（{name}、"
                        f"{image.width()}×{image.height()}）。")
                attach = [str(path)]
            else:
                text = f"{mark} 撮れた画像を保存できませんでした"
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._capture_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        self._capture_auto_mid = (
            mid, key, request_id + "\n" + target, request_id)
        self._begin_command("capture", request_id,
                            summary=target or label, response=text)
        self._diag(f"画面を自動で送ります: {label}")
        self._publish_phal_status(
            "answer", "画面を自動で送っています")

        import threading

        def work_send():
            try:
                if paste:
                    result = _default_ai_sender(
                        text, profile, image_paste=True)
                elif attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"画面を送れませんでした（{e}）")
            self._capture_signals.done.emit(result)

        threading.Thread(
            target=work_send, daemon=True,
            name="phal-capture-auto").start()

    def _on_capture_auto_done(self, result):
        self._capture_sending = False
        self._restore_clipboard_after_capture()
        self._finish_command("capture", result)
        pair, self._capture_auto_mid = self._capture_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            log_rid = pair[3] if len(pair) > 3 else payload
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._capture_auto["last_sent_at"] = time.time()
            self._record_answered(self._capture_auto, payload)
            self._consume_capture_marker(mid, key)
            self._diag("画面を自動で送りました",
                       rid=log_rid or None)
            self._set_note("画面を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_capture_marker(mid, key)
            self._diag(
                f"キャプチャの自動送信を見送り（入力欄に書きかけ）: {message}",
                rid=log_rid or None)
            self._set_note(
                "キャプチャの自動送信: 入力欄に書きかけがある"
                "ため送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("キャプチャの自動応答")
        else:
            self._consume_capture_marker(mid, key)
            self._diag(f"キャプチャの自動送信に失敗: {message}",
                       rid=log_rid or None)
            self._set_note(
                f"画面を自動で送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("画面を自動で送りました")
        else:
            self._publish_phal_idle(
                "キャプチャの自動送信が終わりました")
        self._fire_deferred_restart()

    # ---------------- 全文リクエストの合図 (1.91) ----------------
    def _note_fulltext_once(self, mid: str, key: str, text: str):
        if len(self._fulltext_noted) > 50:
            self._fulltext_noted.clear()
        stamp = (mid, key)
        if stamp in self._fulltext_noted:
            return
        self._fulltext_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_fulltext(self, marker):
        """全文リクエストの目印を判定する (1.91)。

        反応の条件は他の合図と同じ (切替 auto_fulltext、消費
        済みでない、60秒の間隔、連鎖が空いている)。送れるのは
        要約モードで保存された全文ファイルだけ (core.
        find_full_output)。見つからないときも、その旨の文を
        送って消費する (要求した側が黙って待つ形にしない)。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、コマンド ID が読めないときも同様にする。再応答
        の抑止の payload は「依頼 ID＋コマンド ID」の組。"""
        mid = ""
        key = ""
        rid = ""
        cid = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            rid = str(marker.get("request_id") or "")
            cid = str(marker.get("command_id") or "")
        if not mid:
            self._fulltext_marker_pending = None
            return
        payload = rid + "\n" + cid
        consumed_id = str(
            self._fulltext_auto.get("consumed_id") or "")
        consumed_key = str(
            self._fulltext_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._fulltext_marker_pending = None
            self._note_fulltext_once(
                mid, "consumed",
                "全文の合図: 消費済みの発言のため見送り")
            return
        if not rid:
            self._fulltext_marker_pending = None
            self._consume_fulltext_marker(mid, key)
            self._note_fulltext_once(
                mid, "no_id",
                "全文の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:全文] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not cid:
            self._fulltext_marker_pending = None
            self._consume_fulltext_marker(mid, key)
            self._note_fulltext_once(
                mid, "no_arg",
                "全文の合図: コマンドIDが読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:全文 {rid}] コマンドIDが読めないため"
                "応答しません。「目印 依頼ID コマンドID」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._fulltext_auto, payload):
            self._fulltext_marker_pending = None
            self._note_calm("全文リクエストの合図の再応答を抑止（同定子が"
                "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "全文", self._fulltext_auto, payload, rid=rid))
            return
        if not self._automation_flags()["auto_fulltext"]:
            self._note_fulltext_once(
                mid, "off",
                "全文リクエストの合図を検出（全文の自動返信は"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:全文 {rid}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._fulltext_marker_pending = None
            self._consume_fulltext_marker(mid, key)
            self._note_fulltext_once(
                mid, "emergency",
                "全文リクエストの合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:全文 {rid}] 非常停止中のため破棄しました")
            return
        if self._fulltext_sending or self._chain_in_flight():
            self._fulltext_marker_pending = mid
            self._note_fulltext_once(
                mid, "busy",
                "全文リクエストの合図: 回答・送信の連鎖の途中"
                "のため見送り（空いてから送ります）")
            return
        self._fulltext_marker_pending = None
        self._fire_auto_fulltext(mid, key, cid, request_id=rid)

    def _consume_fulltext_marker(self, mid, key=""):
        if not mid:
            return
        self._fulltext_auto["consumed_id"] = mid
        if key:
            self._fulltext_auto["consumed_key"] = key
        core.save_fulltext_auto(self._fulltext_auto)

    def _fire_auto_fulltext(self, mid, key, command_id,
                            request_id=""):
        """要約の全文ファイルを添付して、送信まで一気にやる
        (裏方仕事)。ファイルの解決は core.find_full_output。
        request_id は合図の依頼 ID (2.41、見出し・再応答の
        抑止・ログの rid に使う)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"全文の自動送信に失敗: 投げ先を解決できません（{e}）")
            self._consume_fulltext_marker(mid, key)
            return
        if not configured:
            self._diag("全文の自動送信に失敗: AI の投げ先が未設定")
            self._consume_fulltext_marker(mid, key)
            self._set_note(
                "全文の自動送信: AI の投げ先が設定されていません",
                "error")
            return
        path = core.find_full_output(command_id)
        # 見出しは「[PHAL:全文送信 依頼ID コマンドID]」
        # (2.41、依頼 ID を主にしコマンド ID を併記する)。
        mark = f"[PHAL:全文送信 {request_id} {command_id}]" \
            if request_id else f"[PHAL:全文送信 {command_id}]"
        if path is not None:
            text = (f"{mark} 要約で送った"
                    "結果の全文を送ります。")
            attach = [str(path)]
        else:
            text = (f"{mark} 全文は見つかり"
                    "ませんでした。要約モードで全文が保存された"
                    "結果だけが対象です。")
            attach = []
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._fulltext_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        self._fulltext_auto_mid = (
            mid, key, request_id + "\n" + command_id, request_id)
        self._begin_command("fulltext", request_id,
                            summary=command_id, response=text)
        self._diag(f"全文を自動で送ります: {command_id}")
        self._publish_phal_status(
            "answer", "全文を自動で送っています")

        import threading

        def work_send():
            try:
                if attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"全文を送れませんでした（{e}）")
            self._fulltext_signals.done.emit(result)

        threading.Thread(
            target=work_send, daemon=True,
            name="phal-fulltext-auto").start()

    def _on_fulltext_auto_done(self, result):
        self._fulltext_sending = False
        self._finish_command("fulltext", result)
        pair, self._fulltext_auto_mid = self._fulltext_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            # ログの rid は依頼 ID (2.41)。古い形の控えでは
            # payload (コマンド ID) に倒す。
            log_rid = pair[3] if len(pair) > 3 else payload
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._fulltext_auto["last_sent_at"] = time.time()
            self._record_answered(self._fulltext_auto, payload)
            self._consume_fulltext_marker(mid, key)
            self._diag("全文を自動で送りました", rid=log_rid or None)
            self._set_note("全文を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_fulltext_marker(mid, key)
            self._diag(
                f"全文の自動送信を見送り（入力欄に書きかけ）: {message}", rid=log_rid or None)
            self._set_note(
                "全文の自動送信: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("全文の自動応答")
        else:
            self._consume_fulltext_marker(mid, key)
            self._diag(f"全文の自動送信に失敗: {message}", rid=log_rid or None)
            self._set_note(
                f"全文を自動で送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("全文を自動で送りました")
        else:
            self._publish_phal_idle("全文の自動送信が終わりました")
        self._fire_deferred_restart()

    # ---------------- 実行一覧の合図 (1.92) ----------------
    def _note_history_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (他の合図と同じ作法)。"""
        if len(self._history_noted) > 50:
            self._history_noted.clear()
        stamp = (mid, key)
        if stamp in self._history_noted:
            return
        self._history_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_history(self, marker):
        """実行一覧の目印を判定する (1.92)。

        反応するのは、切替「実行一覧の自動返信」が入っていて、
        同じ発言を消費済みでなく、直近の自動応答から60秒以上
        空いていて、回答・送信の連鎖が空いているときだけ。
        送る文は core.build_history_text が結果の控えから
        組み立てる (値は毎回取り直す)。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._history_marker_pending = None
            return
        consumed_id = str(
            self._history_auto.get("consumed_id") or "")
        consumed_key = str(
            self._history_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._history_marker_pending = None
            self._note_history_once(
                mid, "consumed",
                "一覧の合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._history_marker_pending = None
            self._consume_history_marker(mid, key)
            self._note_history_once(
                mid, "no_id",
                "一覧の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:一覧] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._history_auto, req):
            self._history_marker_pending = None
            self._note_calm("一覧リクエストの合図の再応答を抑止（同定子が"
                "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "一覧", self._history_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_history"]:
            self._note_history_once(
                mid, "off",
                "一覧リクエストの合図を検出（実行一覧の自動返信は"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:一覧 {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._history_marker_pending = None
            self._consume_history_marker(mid, key)
            self._note_history_once(
                mid, "emergency",
                "一覧リクエストの合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:一覧 {req}] 非常停止中のため破棄しました")
            return
        if self._history_sending or self._chain_in_flight():
            self._history_marker_pending = mid
            self._note_history_once(
                mid, "busy",
                "一覧リクエストの合図: 回答・送信の連鎖の途中の"
                "ため見送り（空いてから送ります）")
            return
        self._history_marker_pending = None
        self._fire_auto_history(mid, key, req)

    def _consume_history_marker(self, mid, key=""):
        if not mid:
            return
        self._history_auto["consumed_id"] = mid
        if key:
            self._history_auto["consumed_key"] = key
        core.save_history_auto(self._history_auto)

    def _fire_auto_history(self, mid, key, req=""):
        """実行一覧を、入力欄へ入れて送信まで一気にやる
        (裏方仕事)。入れる役・押す役は他の自動応答と同じ。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"一覧の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_history_marker(mid, key)
            return
        if not configured:
            self._diag("一覧の自動応答に失敗: AI の投げ先が未設定")
            self._consume_history_marker(mid, key)
            self._set_note(
                "一覧の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        text = core.build_history_text(request_id=req)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._history_sending = True
        self._history_auto_mid = (mid, key, req)
        self._begin_command("history", req, response=text)
        self._diag("実行一覧を自動で送ります")
        self._publish_phal_status(
            "answer", "実行一覧を自動で送っています")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"実行一覧を送れませんでした（{e}）")
            self._history_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-history-auto").start()

    def _on_history_auto_done(self, result):
        self._history_sending = False
        self._finish_command("history", result)
        pair, self._history_auto_mid = self._history_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        answered_req = (pair[2] if isinstance(pair, tuple)
                        and len(pair) > 2 else "")
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._history_auto["last_sent_at"] = time.time()
            self._record_answered(self._history_auto, answered_req)
            self._consume_history_marker(mid, key)
            self._diag("実行一覧を自動で送りました", rid=answered_req or None)
            self._set_note("実行一覧を自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_history_marker(mid, key)
            self._diag(
                f"一覧の自動応答を見送り（入力欄に書きかけ）: {message}", rid=answered_req or None)
            self._set_note(
                "一覧の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("履歴の自動応答")
        else:
            self._consume_history_marker(mid, key)
            self._diag(f"一覧の自動応答に失敗: {message}", rid=answered_req or None)
            self._set_note(
                f"実行一覧を自動で送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("実行一覧を自動で送りました")
        else:
            self._publish_phal_idle("一覧の自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 停止リクエストの合図 (1.93) ----------------
    def _note_stop_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (他の合図と同じ作法)。"""
        if len(self._stop_noted) > 50:
            self._stop_noted.clear()
        stamp = (mid, key)
        if stamp in self._stop_noted:
            return
        self._stop_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_stop(self, marker):
        """停止リクエストの目印を判定する (1.93)。

        反応するのは、切替「停止の自動応答」が入っていて、
        同じ発言を消費済みでなく、直近の自動応答から60秒以上
        空いているとき。実行中なら、回答・送信の連鎖の空きは
        見ずに即座に止める (止めるのが目的なので)。報告は
        止まった実行の結果文が担うので、別途の送信はしない。
        実行中でなければ、連鎖が空いているときだけ「実行中の
        スクリプトはありません。」と文で返す。"""
        mid = ""
        key = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
        if not mid:
            self._stop_marker_pending = None
            return
        consumed_id = str(self._stop_auto.get("consumed_id") or "")
        consumed_key = str(
            self._stop_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._stop_marker_pending = None
            self._note_stop_once(
                mid, "consumed",
                "停止の合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._stop_marker_pending = None
            self._consume_stop_marker(mid, key)
            self._note_stop_once(
                mid, "no_id",
                "停止の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:停止] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._stop_auto, req):
            self._stop_marker_pending = None
            self._note_calm("停止リクエストの合図の再応答を抑止（同定子が"
                "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "停止", self._stop_auto, req, rid=req))
            return
        if not self._automation_flags()["auto_stop"]:
            self._note_stop_once(
                mid, "off",
                "停止リクエストの合図を検出（停止の自動応答は"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:停止 {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._stop_marker_pending = None
            self._consume_stop_marker(mid, key)
            self._note_stop_once(
                mid, "emergency",
                "停止リクエストの合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:停止 {req}] 非常停止中のため破棄しました")
            return
        if self._run_handle is not None:
            # 実行中: 連鎖の空きは見ず、その場で止める。
            self._stop_marker_pending = None
            item = self._running_item
            name = item.name if item is not None else "(不明)"
            self._stop_auto["last_sent_at"] = time.time()
            self._record_answered(self._stop_auto, req)
            self._consume_stop_marker(mid, key)
            self._stop_run()
            self._diag(
                f"停止リクエストの合図で実行を止めました: {name}")
            self._record_command(
                "stop", req, status="answered",
                response=(f"実行を止めました: {name}"
                          "（結果は実行の結果文で送られます）"))
            self._set_note(
                "AIからの停止リクエストで実行を止めました", "warn")
            return
        if self._tool_handle is not None:
            # ツール実行中 (1.98): 同じくその場で止める。報告は
            # ツール実行の結果文 (停止しました) が担う。
            self._stop_marker_pending = None
            self._stop_auto["last_sent_at"] = time.time()
            self._record_answered(self._stop_auto, req)
            self._consume_stop_marker(mid, key)
            self._tool_handle.stop()
            self._diag(
                "停止リクエストの合図でツール実行を止めました")
            self._record_command(
                "stop", req, status="answered",
                response=("ツール実行を止めました"
                          "（結果はツール実行の結果文で"
                          "送られます）"))
            self._set_note(
                "AIからの停止リクエストでツール実行を止めました",
                "warn")
            return
        if self._stop_sending or self._chain_in_flight():
            self._stop_marker_pending = mid
            self._note_stop_once(
                mid, "busy",
                "停止リクエストの合図: 回答・送信の連鎖の途中の"
                "ため見送り（空いてから応答します）")
            return
        self._stop_marker_pending = None
        self._fire_auto_stop(mid, key, req)

    def _consume_stop_marker(self, mid, key=""):
        if not mid:
            return
        self._stop_auto["consumed_id"] = mid
        if key:
            self._stop_auto["consumed_key"] = key
        core.save_stop_auto(self._stop_auto)

    def _fire_auto_stop(self, mid, key, req=""):
        """「実行中はない」旨を、入力欄へ入れて送信まで一気に
        やる (裏方仕事)。入れる役・押す役は他の自動応答と
        同じ。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"停止の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_stop_marker(mid, key)
            return
        if not configured:
            self._diag("停止の自動応答に失敗: AI の投げ先が未設定")
            self._consume_stop_marker(mid, key)
            self._set_note(
                "停止の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        mark = f"[PHAL:停止 {req}]" if req else "[PHAL:停止]"
        text = f"{mark} 実行中のスクリプトはありません。"
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._stop_sending = True
        self._stop_auto_mid = (mid, key, req)
        self._begin_command("stop", req, response=text)
        self._diag("停止リクエストへ自動で応答します（実行中なし）")
        self._publish_phal_status(
            "answer", "停止リクエストへ自動で応答しています")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"応答を送れませんでした（{e}）")
            self._stop_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-stop-auto").start()

    def _on_stop_auto_done(self, result):
        self._stop_sending = False
        self._finish_command("stop", result)
        pair, self._stop_auto_mid = self._stop_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
        else:
            mid, key = pair or "", ""
        answered_req = (pair[2] if isinstance(pair, tuple)
                        and len(pair) > 2 else "")
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._stop_auto["last_sent_at"] = time.time()
            self._record_answered(self._stop_auto, answered_req)
            self._consume_stop_marker(mid, key)
            self._diag("停止リクエストへ自動で応答しました", rid=answered_req or None)
            self._set_note(
                "停止リクエストに応答しました（実行中の"
                "スクリプトはありません）", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_stop_marker(mid, key)
            self._diag(
                f"停止の自動応答を見送り（入力欄に書きかけ）: {message}", rid=answered_req or None)
            self._set_note(
                "停止の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("停止の自動応答")
        else:
            self._consume_stop_marker(mid, key)
            self._diag(f"停止の自動応答に失敗: {message}", rid=answered_req or None)
            self._set_note(
                f"停止リクエストへ応答できませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("停止リクエストへ応答しました")
        else:
            self._publish_phal_idle("停止の自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 動作ログリクエストの合図 (1.96) ----------------
    def _note_log_once(self, mid: str, key: str, text: str):
        """見送りの理由を、同じ目印・同じ理由では1度だけ診断に
        残す (他の合図と同じ作法)。"""
        if len(self._log_noted) > 50:
            self._log_noted.clear()
        stamp = (mid, key)
        if stamp in self._log_noted:
            return
        self._log_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_log(self, marker):
        """動作ログリクエストの目印を判定する (1.96)。

        反応するのは、切替「動作ログの自動返信」が入っていて、
        同じ発言を消費済みでなく、直近の自動応答から60秒以上
        空いていて、回答・送信の連鎖が空いているときだけ。
        送る文は core.build_log_reply が動作ログ (ビューアと
        同じ中身) から組み立てる (値は毎回取り直す)。再応答の
        抑止 (1.95) の payload は指定の文字列。"""
        mid = ""
        key = ""
        params = ""
        req = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            params = str(marker.get("params") or "").strip()
            req = str(marker.get("request_id") or "")
        payload = req if req else params
        if not mid:
            self._log_marker_pending = None
            return
        consumed_id = str(self._log_auto.get("consumed_id") or "")
        consumed_key = str(self._log_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._log_marker_pending = None
            self._note_log_once(
                mid, "consumed",
                "ログの合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._log_marker_pending = None
            self._consume_log_marker(mid, key)
            self._note_log_once(
                mid, "no_id",
                "ログの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:ログ] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if self._refire_suppressed(self._log_auto, payload):
            self._log_marker_pending = None
            self._note_calm("ログリクエストの合図の再応答を抑止（同定子が"
                "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "ログ", self._log_auto, payload, rid=req))
            return
        if not self._automation_flags()["auto_log"]:
            self._note_log_once(
                mid, "off",
                "ログリクエストの合図を検出（動作ログの自動返信は"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:ログ {req}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._log_marker_pending = None
            self._consume_log_marker(mid, key)
            self._note_log_once(
                mid, "emergency",
                "ログリクエストの合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:ログ {req}] 非常停止中のため破棄しました")
            return
        if self._log_sending or self._chain_in_flight():
            self._log_marker_pending = mid
            self._note_log_once(
                mid, "busy",
                "ログリクエストの合図: 回答・送信の連鎖の途中の"
                "ため見送り（空いてから送ります）")
            return
        self._log_marker_pending = None
        self._fire_auto_log(mid, key, params, payload, req)

    def _consume_log_marker(self, mid, key=""):
        if not mid:
            return
        self._log_auto["consumed_id"] = mid
        if key:
            self._log_auto["consumed_key"] = key
        core.save_log_auto(self._log_auto)

    def _fire_auto_log(self, mid, key, params, payload, req=""):
        """動作ログを、入力欄へ入れて送信まで一気にやる
        (裏方仕事)。全文が長いときはファイルに書いて添付し、
        文は見出しだけにする。入れる役・押す役は他の自動応答
        と同じ。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"ログの自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_log_marker(mid, key)
            return
        if not configured:
            self._diag("ログの自動応答に失敗: AI の投げ先が未設定")
            self._consume_log_marker(mid, key)
            self._set_note(
                "ログの自動応答: AI の投げ先が設定されていません",
                "error")
            return
        spec = core.parse_log_request(params)
        events = core.select_log_events(
            core.load_log_events(), spec)
        text = core.build_log_reply(
            events, spec, request_id=req,
            loop_line=self._loop_breakdown_line())
        attach = []
        if len(text) > 3800:
            path = core.data_dir() / "phal-log-latest.txt"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8",
                          newline="") as f:
                    f.write(text)
                attach = [str(path)]
                text = (text.splitlines()[0]
                        + "\n長いためファイルで送ります。")
            except OSError as e:
                self._diag(
                    f"ログのファイル書き出しに失敗: {e}")
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._log_sending = True
        self._log_auto_mid = (mid, key, payload, req)
        self._begin_command("log", req, summary=params,
                            response=text)
        self._diag(f"動作ログを自動で送ります: {params or '(指定なし)'}")
        self._publish_phal_status(
            "answer", "動作ログを自動で送っています")

        import threading

        def work():
            try:
                if attach:
                    result = sender(text, profile, attach)
                else:
                    result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"動作ログを送れませんでした（{e}）")
            self._log_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-log-auto").start()

    def _on_log_auto_done(self, result):
        self._log_sending = False
        self._finish_command("log", result)
        pair, self._log_auto_mid = self._log_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            req_rid = pair[3] if len(pair) > 3 else ""
        else:
            mid, key = pair or "", ""
            payload = ""
            req_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._log_auto["last_sent_at"] = time.time()
            self._record_answered(self._log_auto, payload)
            self._consume_log_marker(mid, key)
            self._diag("動作ログを自動で送りました", rid=req_rid or None)
            self._set_note("動作ログを自動で送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_log_marker(mid, key)
            self._diag(
                f"ログの自動応答を見送り（入力欄に書きかけ）: {message}",
                rid=req_rid or None)
            self._set_note(
                "ログの自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("ログの自動応答")
        else:
            self._consume_log_marker(mid, key)
            self._diag(f"ログの自動応答に失敗: {message}",
                       rid=req_rid or None)
            self._set_note(
                f"動作ログを自動で送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("動作ログを自動で送りました")
        else:
            self._publish_phal_idle("ログの自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- ツール実行の合図 (1.98) ----------------
    def _note_tool_once(self, mid: str, key: str, text: str):
        if len(self._tool_noted) > 50:
            self._tool_noted.clear()
        stamp = (mid, key)
        if stamp in self._tool_noted:
            return
        self._tool_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_tool(self, marker):
        """ツール実行の目印を判定する (1.98)。

        反応の条件は他の合図と同じ (切替 auto_tool、消費済みで
        ない、60秒の間隔、連鎖が空いている)。スクリプトの実行
        中も見送る (実行は1件ずつ直列の原則)。許可リストに無い
        ・見つからないときも、その旨の文を送って消費する。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、コマンド行が読めないときも同様にする。再応答
        の抑止の payload は「依頼 ID＋コマンド行」の組 (正当な
        繰り返しは新しい ID で出すのが仕様)。"""
        mid = ""
        key = ""
        rid = ""
        command = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            rid = str(marker.get("request_id") or "")
            command = str(marker.get("command") or "")
        if not mid:
            self._tool_marker_pending = None
            return
        payload = rid + "\n" + command
        consumed_id = str(self._tool_auto.get("consumed_id") or "")
        consumed_key = str(self._tool_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._tool_marker_pending = None
            self._note_tool_once(
                mid, "consumed",
                "ツールの合図: 消費済みの発言のため見送り")
            return
        if not rid:
            self._tool_marker_pending = None
            self._consume_tool_marker(mid, key)
            self._note_tool_once(
                mid, "no_id",
                "ツールの合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:ツール] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not command:
            self._tool_marker_pending = None
            self._consume_tool_marker(mid, key)
            self._note_tool_once(
                mid, "no_arg",
                "ツールの合図: コマンド行が読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:ツール {rid}] コマンド行が読めないため"
                "応答しません。「目印 依頼ID コマンド行」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._tool_auto, payload):
            self._tool_marker_pending = None
            self._note_calm("ツール実行の合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "ツール", self._tool_auto, payload, rid=rid))
            return
        if not self._automation_flags()["auto_tool"]:
            self._note_tool_once(
                mid, "off",
                "ツール実行の合図を検出（ツールを自動で実行する"
                "は切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:ツール {rid}] 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._tool_marker_pending = None
            self._consume_tool_marker(mid, key)
            self._note_tool_once(
                mid, "emergency",
                "ツール実行の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:ツール {rid}] 非常停止中のため破棄しました")
            return
        if self._tool_sending or self._running_item is not None \
                or self._chain_in_flight():
            self._tool_marker_pending = mid
            self._note_tool_once(
                mid, "busy",
                "ツール実行の合図: 実行・送信の連鎖の途中のため"
                "見送り（空いてから実行します）")
            return
        self._tool_marker_pending = None
        self._fire_auto_tool(mid, key, command, request_id=rid)

    def _consume_tool_marker(self, mid, key=""):
        if not mid:
            return
        self._tool_auto["consumed_id"] = mid
        if key:
            self._tool_auto["consumed_key"] = key
        core.save_tool_auto(self._tool_auto)

    def _fire_auto_tool(self, mid, key, command, request_id=""):
        """許可リストのツールを作業フォルダで実行し、結果を
        送信まで一気にやる (裏方仕事)。許可の判定と実在の解決
        は core.resolve_cli_tool。実行できない理由があるときも、
        その旨の文を送る (要求した側を黙って待たせない)。
        request_id は合図の依頼 ID (2.41、結果の見出し・再応答
        の抑止・ログの rid に使う)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"ツール実行の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_tool_marker(mid, key)
            return
        if not configured:
            self._diag("ツール実行の自動応答に失敗: AI の投げ先が未設定")
            self._consume_tool_marker(mid, key)
            self._set_note(
                "ツール実行の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        try:
            section = settings_module.load_settings(
                self.settings_path).get("script_runner") or {}
        except Exception:  # noqa: BLE001
            section = {}
        allowed = core.sanitize_cli_tools(section.get("cli_tools"))
        tool_paths = core.sanitize_cli_tool_paths(
            section.get("cli_tool_paths"))
        tool_dirs = [core.tools_folder()]
        work = section.get("work_folder")
        cwd = work if isinstance(work, str) and work else str(
            core.resolve_watch_folder(self.config))
        exe_path = None
        tool_args = []
        reply_text = ""
        odg_name = ""
        # 実行できない理由の返信にも、結果と同じ見出しを付ける
        # (2.41)。
        mark = f"[PHAL:ツール実行結果 {request_id}]" \
            if request_id else "[PHAL:ツール実行結果]"
        try:
            tokens = core.split_command_args(command)
        except ValueError:
            tokens = []
        if not tokens:
            reply_text = (
                f"{mark} ツール実行の引数を読み取れませんでした"
                "（引用符が閉じていないか、ツール名がありません）"
                f": {command}")
        else:
            tool_args = tokens[1:]
            # おどうぐばこの道具は、登録パスに次ぐ優先で名前で
            # 呼べる (2.37)。登録パスがある名前はそちらが正。
            if core.normalize_tool_name(tokens[0]) not in tool_paths \
                    and _odougubako_find(tokens[0]) is not None:
                odg_name = tokens[0]
            why = ""
            if not odg_name:
                exe_path, why = core.resolve_cli_tool(
                    tokens[0], allowed, tool_paths, tool_dirs)
            if why == "not_allowed":
                exe_path = None
                reply_text = (
                    f"{mark} そのツールは許可が無いため"
                    "実行できません: "
                    f"{tokens[0]}（許可: "
                    f"{', '.join(allowed) if allowed else 'なし'}"
                    "。ツール設定での登録か、ツール置き場への"
                    "配置で許可できます）")
            elif why == "not_found":
                exe_path = None
                reply_text = (
                    f"{mark} ツールが見つかりませんでした: "
                    f"{tokens[0]}"
                    + core.tool_not_found_note(
                        tokens[0], tool_paths, tool_dirs))
            elif why == "no_path":
                exe_path = None
                reply_text = (
                    f"{mark} ツール「{tokens[0]}」はパスが未設定の"
                    "ため実行できません。ツール設定で「探す」を"
                    "押すか、実行ファイルをドラッグ＆ドロップして、"
                    "パスを設定してください")
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._tool_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        # payload は「依頼 ID＋コマンド行」の組 (2.41)。
        self._tool_auto_mid = (
            mid, key, request_id + "\n" + command, request_id)
        self._begin_command("tool", request_id, summary=command,
                            response=reply_text)
        # 実行は受け付けた時点で1回だけ (2.45)。応答済みの控えと
        # 目印の消費を、送信の結果を待たずにここで残す (再実行の
        # 合図が実行前に記録するのと同じ形)。以前は送信が成功
        # したときだけ控えが残ったため、入力欄の書きかけで送信が
        # 見送られると、同じ発言の再検出でツールが再実行された。
        self._record_answered(
            self._tool_auto, request_id + "\n" + command)
        self._consume_tool_marker(mid, key)
        _dispatch = getattr(self, "_dispatch_state", None)
        self._tool_auto_ai = (_dispatch.name
                              if _dispatch is not None else "")
        handle = core.RunHandle()
        self._tool_handle = handle
        self._diag(f"ツールを自動で実行します: {command}")
        self._publish_phal_status(
            "run", f"ツール実行中: {command}")

        import threading

        def work_run():
            text = reply_text
            if odg_name:
                run = _odougubako_run_result(
                    odg_name, tool_args,
                    self.config.get("timeout_seconds"))
                text = core.build_tool_result_text(
                    command, run, self.config,
                    request_id=request_id)
            elif exe_path is not None:
                run = core.execute_command(
                    core.tool_command(exe_path) + tool_args,
                    cwd=cwd,
                    timeout_seconds=self.config.get(
                        "timeout_seconds"),
                    handle=handle)
                text = core.build_tool_result_text(
                    command, run, self.config,
                    request_id=request_id)
            # 応答文は裏方で確定するので、完了側 (コマンド履歴)
            # へ預ける (2.42)。
            self._command_texts["tool"] = text
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"結果を送れませんでした（{e}）")
            self._tool_signals.done.emit(result)

        threading.Thread(
            target=work_run, daemon=True,
            name="phal-tool-auto").start()

    def _on_tool_auto_done(self, result):
        self._tool_sending = False
        self._finish_command("tool", result)
        self._tool_handle = None
        pair, self._tool_auto_mid = self._tool_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            # ログの rid は依頼 ID (2.41)。古い形の控えでは
            # payload (コマンド行) は rid にしない。
            log_rid = pair[3] if len(pair) > 3 else ""
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._tool_auto["last_sent_at"] = time.time()
            self._record_answered(self._tool_auto, payload)
            self._consume_tool_marker(mid, key)
            self._diag("ツール実行の結果を自動で送りました",
                       ai=getattr(self, "_tool_auto_ai", "")
                       or None, rid=log_rid or None)
            self._set_note("ツール実行の結果を送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_tool_marker(mid, key)
            self._diag(
                f"ツール実行の応答を見送り（入力欄に書きかけ）: {message}",
                ai=getattr(self, "_tool_auto_ai", "") or None,
                rid=log_rid or None)
            self._set_note(
                "ツール実行の応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("道具の自動応答")
        else:
            self._consume_tool_marker(mid, key)
            self._diag(f"ツール実行の応答に失敗: {message}",
                       ai=getattr(self, "_tool_auto_ai", "")
                       or None, rid=log_rid or None)
            self._set_note(
                f"ツール実行の結果を送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("ツール実行の結果を送りました")
        else:
            self._publish_phal_idle("ツール実行の応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 道具説明の合図 (2.37) ----------------
    def _note_describe_once(self, mid: str, key: str, text: str):
        if len(self._describe_noted) > 50:
            self._describe_noted.clear()
        stamp = (mid, key)
        if stamp in self._describe_noted:
            return
        self._describe_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_describe(self, marker):
        """道具説明の目印を判定する (2.37)。

        反応の条件は設定の合図と同じ (切替 auto_describe、依頼
        ID 必須、消費済みでない、再応答の抑止、連鎖が空いて
        いる)。送る文はおどうぐばこの取説 (SKILL.md)。"""
        mid = ""
        key = ""
        req = ""
        tool_name = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
            tool_name = str(marker.get("tool_name") or "")
        if not mid:
            self._describe_marker_pending = None
            return
        consumed_id = str(
            self._describe_auto.get("consumed_id") or "")
        consumed_key = str(
            self._describe_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._describe_marker_pending = None
            self._note_describe_once(
                mid, "consumed",
                "道具説明の合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._describe_marker_pending = None
            self._consume_describe_marker(mid, key)
            self._note_describe_once(
                mid, "no_id",
                "道具説明の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:道具説明] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        payload = f"{req}:{tool_name}"
        if self._refire_suppressed(self._describe_auto, payload):
            self._describe_marker_pending = None
            self._note_calm("道具説明の合図の再応答を抑止（同定子が"
                            "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "道具説明", self._describe_auto, payload,
                    rid=req))
            return
        if not self._automation_flags()["auto_describe"]:
            self._note_describe_once(
                mid, "off",
                "道具説明の合図を検出（道具の取説を自動で送るは"
                "切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:道具説明 {req}] 自動応答が切のため"
                "応答しません")
            return
        if self._emergency_stopped:
            self._describe_marker_pending = None
            self._consume_describe_marker(mid, key)
            self._note_describe_once(
                mid, "emergency",
                "道具説明の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:道具説明 {req}] 非常停止中のため"
                "破棄しました")
            return
        if self._describe_sending or self._chain_in_flight():
            self._describe_marker_pending = mid
            self._note_describe_once(
                mid, "busy",
                "道具説明の合図: 回答・送信の連鎖の途中のため"
                "見送り（空いてから送ります）")
            return
        self._describe_marker_pending = None
        self._fire_auto_describe(mid, key, payload, tool_name, req)

    def _consume_describe_marker(self, mid, key=""):
        if not mid:
            return
        self._describe_auto["consumed_id"] = mid
        if key:
            self._describe_auto["consumed_key"] = key
        core.save_describe_auto(self._describe_auto)

    def _fire_auto_describe(self, mid, key, payload, tool_name,
                            req=""):
        """道具の取説を、入力欄へ入れて送信まで一気にやる
        (裏方仕事)。道具が無い・名前が無いときも、その旨の
        文を送る (要求した側を黙って待たせない)。応答の見出し
        には依頼 ID を入れる (2.40)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"道具説明の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_describe_marker(mid, key)
            return
        if not configured:
            self._diag("道具説明の自動応答に失敗: AI の投げ先が未設定")
            self._consume_describe_marker(mid, key)
            self._set_note(
                "道具説明の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        mark = f"[PHAL:道具説明 {req}]" if req \
            else "[PHAL:道具説明]"
        if not tool_name:
            text = (f"{mark} 道具名がありません。"
                    "「PHAL:道具説明リクエスト <依頼ID> <道具名>」"
                    "の形で出し直してください")
        else:
            try:
                import odougubako_core
                body = odougubako_core.describe(tool_name)
            except Exception:  # noqa: BLE001  道具箱が読めない
                body = ""
            if body:
                text = f"{mark} {tool_name} の取説\n{body}"
            else:
                text = (f"{mark} 道具「{tool_name}」は"
                        "おどうぐばこにありません")
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._describe_sending = True
        self._describe_auto_mid = (mid, key, payload)
        self._begin_command("describe", req, summary=tool_name,
                            response=text)
        self._diag(f"道具の取説を自動で送ります: {tool_name}")
        self._publish_phal_status(
            "answer", f"道具の取説を送っています: {tool_name}")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"取説を送れませんでした（{e}）")
            self._describe_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-describe-auto").start()

    def _on_describe_auto_done(self, result):
        self._describe_sending = False
        self._finish_command("describe", result)
        pair, self._describe_auto_mid = self._describe_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
        else:
            mid, key = pair or "", ""
            payload = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._describe_auto["last_sent_at"] = time.time()
            self._record_answered(self._describe_auto, payload)
            self._consume_describe_marker(mid, key)
            self._diag("道具の取説を自動で送りました",
                       rid=payload.split(":", 1)[0] or None)
            self._set_note("道具の取説を送りました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_describe_marker(mid, key)
            self._diag(
                f"道具説明の自動応答を見送り（入力欄に書きかけ）: {message}",
                rid=payload.split(":", 1)[0] or None)
            self._set_note(
                "道具説明の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("説明の自動応答")
        else:
            self._consume_describe_marker(mid, key)
            self._diag(f"道具説明の自動応答に失敗: {message}",
                       rid=payload.split(":", 1)[0] or None)
            self._set_note(
                f"道具の取説を送れませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("道具の取説を送りました")
        else:
            self._publish_phal_idle("道具説明の自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 道具評価の合図 (2.37) ----------------
    def _note_eval_once(self, mid: str, key: str, text: str):
        if len(self._eval_noted) > 50:
            self._eval_noted.clear()
        stamp = (mid, key)
        if stamp in self._eval_noted:
            return
        self._eval_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_eval(self, marker):
        """道具評価の目印を判定する (2.37)。

        反応の条件はツール実行と同じ (切替 auto_eval、消費済み
        でない、再応答の抑止、連鎖が空いている)。評価の記録
        そのものは裏方で即時に行い、受け付けの文を送る。
        2.41 で依頼 ID が必須になった: 無ければ知らせを返して
        消費し、内容が読めないときも同様にする。再応答の抑止
        の payload は「依頼 ID＋内容」の組。"""
        mid = ""
        key = ""
        rid = ""
        payload = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            rid = str(marker.get("request_id") or "")
            payload = str(marker.get("payload") or "")
        if not mid:
            self._eval_marker_pending = None
            return
        full_payload = rid + "\n" + payload
        consumed_id = str(self._eval_auto.get("consumed_id") or "")
        consumed_key = str(self._eval_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._eval_marker_pending = None
            self._note_eval_once(
                mid, "consumed",
                "道具評価の合図: 消費済みの発言のため見送り")
            return
        if not rid:
            self._eval_marker_pending = None
            self._consume_eval_marker(mid, key)
            self._note_eval_once(
                mid, "no_id",
                "道具評価の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                "[PHAL:道具評価] 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        if not payload:
            self._eval_marker_pending = None
            self._consume_eval_marker(mid, key)
            self._note_eval_once(
                mid, "no_arg",
                "道具評価の合図: 内容が読めないため見送り")
            self._post_react_note_once(
                mid, "no_arg",
                f"[PHAL:道具評価 {rid}] 内容が読めないため"
                "応答しません。「目印 依頼ID 道具名 点…」の形で"
                "出し直してください")
            return
        if self._refire_suppressed(self._eval_auto, full_payload):
            self._eval_marker_pending = None
            self._note_calm("道具評価の合図の再応答を抑止（同定子が"
                            "外れた再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    "道具評価", self._eval_auto, full_payload,
                    rid=rid))
            return
        if not self._automation_flags()["auto_eval"]:
            self._note_eval_once(
                mid, "off",
                "道具評価の合図を検出（道具の評価を自動で受け付ける"
                "は切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"[PHAL:道具評価 {rid}] 自動応答が切のため"
                "応答しません")
            return
        if self._emergency_stopped:
            self._eval_marker_pending = None
            self._consume_eval_marker(mid, key)
            self._note_eval_once(
                mid, "emergency",
                "道具評価の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"[PHAL:道具評価 {rid}] 非常停止中のため"
                "破棄しました")
            return
        if self._eval_sending or self._chain_in_flight():
            self._eval_marker_pending = mid
            self._note_eval_once(
                mid, "busy",
                "道具評価の合図: 回答・送信の連鎖の途中のため"
                "見送り（空いてから受け付けます）")
            return
        self._eval_marker_pending = None
        self._fire_auto_eval(mid, key, payload, request_id=rid)

    def _consume_eval_marker(self, mid, key=""):
        if not mid:
            return
        self._eval_auto["consumed_id"] = mid
        if key:
            self._eval_auto["consumed_key"] = key
        core.save_eval_auto(self._eval_auto)

    def _fire_auto_eval(self, mid, key, payload, request_id=""):
        """評価をおどうぐばこへ記録し、受け付けの文を送る
        (裏方仕事)。payload の形は「道具名 点 点 点 [メモ]」。
        点は使いやすさ・選びやすさ・期待どおりかの順で 1〜5。
        読み取れないときも、その旨の文を送る。request_id は
        合図の依頼 ID (2.41、見出し・再応答の抑止・ログの
        rid に使う)。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"道具評価の自動応答に失敗: 投げ先を解決できません（{e}）")
            self._consume_eval_marker(mid, key)
            return
        if not configured:
            self._diag("道具評価の自動応答に失敗: AI の投げ先が未設定")
            self._consume_eval_marker(mid, key)
            self._set_note(
                "道具評価の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        text = _eval_reply_text(payload, request_id=request_id)
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._eval_sending = True
        # 完了側の控え: (mid, key, 抑止の payload, 依頼 ID)。
        # payload は「依頼 ID＋内容」の組 (2.41)。
        self._eval_auto_mid = (
            mid, key, request_id + "\n" + payload, request_id)
        self._begin_command("eval", request_id, summary=payload,
                            response=text)
        self._diag(f"道具の評価を受け付けます: {payload}")
        self._publish_phal_status(
            "answer", "道具の評価を受け付けています")

        import threading

        def work():
            try:
                result = sender(text, profile)
                if getattr(result, "ok", False):
                    result = presser(profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"評価の受け付けを送れませんでした（{e}）")
            self._eval_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True,
            name="phal-eval-auto").start()

    def _on_eval_auto_done(self, result):
        self._eval_sending = False
        self._finish_command("eval", result)
        pair, self._eval_auto_mid = self._eval_auto_mid, None
        if isinstance(pair, tuple):
            mid, key = pair[0], (pair[1] if len(pair) > 1 else "")
            payload = pair[2] if len(pair) > 2 else ""
            # ログの rid は依頼 ID (2.41)。
            log_rid = pair[3] if len(pair) > 3 else ""
        else:
            mid, key = pair or "", ""
            payload = ""
            log_rid = ""
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._eval_auto["last_sent_at"] = time.time()
            self._record_answered(self._eval_auto, payload)
            self._consume_eval_marker(mid, key)
            self._diag("道具の評価を受け付けました",
                       rid=log_rid or None)
            self._set_note("道具の評価を受け付けました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_eval_marker(mid, key)
            self._diag(
                f"道具評価の自動応答を見送り（入力欄に書きかけ）: {message}",
                rid=log_rid or None)
            self._set_note(
                "道具評価の自動応答: 入力欄に書きかけがあるため"
                "送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("評価の自動応答")
        else:
            self._consume_eval_marker(mid, key)
            self._diag(f"道具評価の自動応答に失敗: {message}",
                       rid=log_rid or None)
            self._set_note(
                f"道具の評価を受け付けられませんでした（{message}）",
                "error")
        if getattr(result, "ok", False):
            self._publish_phal_idle("道具の評価を受け付けました")
        else:
            self._publish_phal_idle("道具評価の自動応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- 入力欄まわりの合図 (2.39) ----------------
    _INPUT_OP_LABELS = {
        "input": ("入力欄", "[PHAL:入力欄]"),
        "overwrite": ("入力上書き", "[PHAL:入力上書き]"),
        "force": ("強制送信", "[PHAL:強制送信]"),
    }

    def _note_input_once(self, mid: str, key: str, text: str):
        if len(self._input_noted) > 50:
            self._input_noted.clear()
        stamp = (mid, key)
        if stamp in self._input_noted:
            return
        self._input_noted.add(stamp)
        self._diag(text)

    def _maybe_auto_input(self, marker):
        self._maybe_auto_input_op(marker, "input")

    def _maybe_auto_overwrite(self, marker):
        self._maybe_auto_input_op(marker, "overwrite")

    def _maybe_auto_force(self, marker):
        self._maybe_auto_input_op(marker, "force")

    def _maybe_auto_input_op(self, marker, kind):
        """入力欄まわりの目印を判定する (2.39、Nagato の依頼)。

        入力欄リクエストは対象の窓の入力欄の今の文を返し、
        入力上書きは本文を対象の入力欄へ強制的に上書きし、
        強制送信は上書きに続けて送信まで行う。反応の条件は
        他の合図と同じ (切替、依頼 ID 必須、消費済みでない、
        再応答の抑止、連鎖が空いている)。結果は合図が出た
        窓へ文で返す。"""
        label, head = self._INPUT_OP_LABELS[kind]
        mid = key = req = target = body = ""
        if isinstance(marker, dict):
            mid = str(marker.get("id") or "")
            key = str(marker.get("key") or "")
            req = str(marker.get("request_id") or "")
            target = str(marker.get("target") or "")
            body = str(marker.get("body") or "")
        if not mid:
            self._input_marker_pending = None
            return
        consumed_id = str(self._input_auto.get("consumed_id") or "")
        consumed_key = str(self._input_auto.get("consumed_key") or "")
        if mid == consumed_id or (key and key == consumed_key):
            self._input_marker_pending = None
            self._note_input_once(
                mid, "consumed",
                f"{label}の合図: 消費済みの発言のため見送り")
            return
        if not req:
            self._input_marker_pending = None
            self._consume_input_marker(mid, key)
            self._note_input_once(
                mid, "no_id",
                f"{label}の合図: 依頼IDがないため見送り")
            self._post_react_note_once(
                mid, "no_id",
                f"{head} 依頼IDがないため応答しません。"
                "IDを付けて出し直してください")
            return
        # 以後の知らせと応答は、依頼 ID 入りの見出しで出す
        # (2.40。応答の対応づけは ID で行う)。
        head_req = f"[PHAL:{label} {req}]"
        payload = f"{kind}:{req}"
        if self._refire_suppressed(self._input_auto, payload):
            self._input_marker_pending = None
            self._note_calm(
                f"{label}の合図の再応答を抑止（同定子が外れた"
                "再検出とみられる）")
            self._post_react_note_once(
                mid, "refire",
                self._suppressed_chat_text(
                    label, self._input_auto, payload, rid=req))
            return
        flag = "auto_input" if kind == "input" else "auto_force"
        if not self._automation_flags()[flag]:
            self._note_input_once(
                mid, "off",
                f"{label}の合図を検出（自動応答が切のため見送り）")
            self._post_react_note_once(
                mid, "off",
                f"{head_req} 自動応答が切のため応答しません")
            return
        if self._emergency_stopped:
            self._input_marker_pending = None
            self._consume_input_marker(mid, key)
            self._note_input_once(
                mid, "emergency",
                f"{label}の合図: 非常停止中のため見送り"
                "（遅れては送りません）")
            self._post_react_note_once(
                mid, "emergency",
                f"{head_req} 非常停止中のため破棄しました")
            return
        if kind != "input" and not body:
            self._input_marker_pending = None
            self._consume_input_marker(mid, key)
            self._note_input_once(
                mid, "empty",
                f"{label}の合図: 本文がないため見送り")
            self._post_react_note_once(
                mid, "empty",
                f"{head_req} 本文がありません（目印の次の行から"
                "発言の末尾までが本文です）")
            return
        if self._input_sending or self._chain_in_flight():
            self._input_marker_pending = mid
            self._note_input_once(
                mid, "busy",
                f"{label}の合図: 回答・送信の連鎖の途中のため"
                "見送り（空いてから送ります）")
            return
        self._input_marker_pending = None
        self._fire_auto_input_op(
            kind, mid, key, payload, target, body)

    def _consume_input_marker(self, mid, key=""):
        if not mid:
            return
        self._input_auto["consumed_id"] = mid
        if key:
            self._input_auto["consumed_key"] = key
        core.save_input_auto(self._input_auto)

    def _resolve_input_target(self, kind, target):
        """入力欄まわりの合図の対象の窓を解決する (2.39)。

        窓名があれば、その見張りの窓かプロファイル名で解決
        する。入力上書き・強制送信で窓名が無ければ、見張りが
        2つのときの「もう一方」 (中継と同じ)。入力欄リク
        エストで窓名が無ければ、合図が出た窓自身。解決でき
        なければ (名前, None)。"""
        if not target and kind != "input":
            dispatch = getattr(self, "_dispatch_state", None)
            states = list(self._watches.values())
            if dispatch is not None and len(states) == 2:
                other = states[1] if states[0] is dispatch \
                    else states[0]
                target = other.name
        if target:
            state = self._watches.get(target)
            if state is not None and state.profile is not None:
                return target, state.profile
            try:
                configured, profile = \
                    self._resolve_profile_by_name(target)
            except Exception:  # noqa: BLE001
                configured, profile = False, None
            return target, (profile if configured else None)
        dispatch = getattr(self, "_dispatch_state", None)
        if dispatch is not None:
            return dispatch.name, dispatch.profile
        return "", None

    def _fire_auto_input_op(self, kind, mid, key, payload,
                            target, body):
        """入力欄まわりの合図の実仕事 (裏方仕事、2.39)。

        対象への読み・上書き・送信を行い、結果の文を合図が
        出た窓へ返す。対象が解決できないときも、その旨の文
        を返す (要求した側を黙って待たせない)。"""
        label, head = self._INPUT_OP_LABELS[kind]
        # 応答の見出しには依頼 ID を入れる (2.40)。
        req = payload.split(":", 1)[1] if ":" in payload else ""
        if req:
            head = f"[PHAL:{label} {req}]"
        try:
            configured, src_profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"{label}の自動応答に失敗: 投げ先を解決"
                f"できません（{e}）")
            self._consume_input_marker(mid, key)
            return
        if not configured:
            self._diag(
                f"{label}の自動応答に失敗: AI の投げ先が未設定")
            self._consume_input_marker(mid, key)
            self._set_note(
                f"{label}の自動応答: AI の投げ先が"
                "設定されていません", "error")
            return
        target_name, target_profile = self._resolve_input_target(
            kind, target)
        reader = self.ai_input_reader or _default_ai_input_reader
        force_sender = self.ai_force_sender or \
            _default_ai_force_sender
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._input_sending = True
        self._input_auto_mid = (mid, key, payload, kind)
        self._begin_command(kind, req, summary=target)
        self._diag(
            f"{label}の合図に応答します: 対象={target_name or '?'}")
        self._publish_phal_status(
            "answer", f"{label}の合図に応答しています")

        import threading

        def work():
            try:
                text = self._input_op_report(
                    kind, head, target_name, target_profile, body,
                    reader, force_sender, presser)
                # 応答文は裏方で確定するので、完了側 (コマンド
                # 履歴) へ預ける (2.42)。
                self._command_texts[kind] = text
                result = sender(text, src_profile)
                if getattr(result, "ok", False):
                    result = presser(src_profile)
            except Exception as e:  # noqa: BLE001
                result = _AiAnswerResult(
                    False, f"応答できませんでした（{e}）")
            self._input_signals.done.emit(result)

        threading.Thread(
            target=work, daemon=True, name="phal-input-auto").start()

    @staticmethod
    def _input_op_report(kind, head, target_name, target_profile,
                         body, reader, force_sender, presser):
        """対象への実仕事を行い、合図の窓へ返す文を作る (2.39)。

        裏方スレッドから呼ばれる (Qt には触らない)。"""
        if target_profile is None:
            return (
                f"{head} 対象の窓「{target_name}」が見つからない"
                "か、投げ先が未設定のため実行できませんでした")
        if kind == "input":
            read = reader(target_profile)
            if not getattr(read, "ok", False):
                return (
                    f"{head} {target_name} の入力欄が"
                    f"読めませんでした"
                    f"（{getattr(read, 'message', '')}）")
            text = str(getattr(read, "text", "") or "")
            if not text.strip():
                return f"{head} {target_name} の入力欄は空です"
            shown = text
            if len(text) > 2000:
                shown = text[:2000] + (
                    f"\n（長いので先頭2000文字まで。"
                    f"全文 {len(text)} 文字）")
            return (
                f"{head} {target_name} の入力欄"
                f"（{len(text)}文字）\n{shown}")
        filled = force_sender(body, target_profile)
        if not getattr(filled, "ok", False):
            return (
                f"{head} {target_name} の入力欄へ上書き"
                f"できませんでした"
                f"（{getattr(filled, 'message', '')}）")
        if kind == "overwrite":
            return (
                f"{head} {target_name} の入力欄に上書きしました"
                "（送信はしていません）")
        pressed = presser(target_profile)
        if not getattr(pressed, "ok", False):
            return (
                f"{head} {target_name} の入力欄には上書き"
                f"しましたが、送信できませんでした"
                f"（{getattr(pressed, 'message', '')}）")
        return f"{head} {target_name} へ送信しました"

    def _on_input_auto_done(self, result):
        self._input_sending = False
        saved, self._input_auto_mid = self._input_auto_mid, None
        if isinstance(saved, tuple) and len(saved) == 4:
            mid, key, payload, kind = saved
        else:
            mid, key, payload, kind = "", "", "", "input"
        self._finish_command(kind, result)
        label, _head = self._INPUT_OP_LABELS.get(
            kind, ("入力欄", ""))
        message = str(getattr(result, "message", "") or "")
        if getattr(result, "ok", False):
            self._input_auto["last_sent_at"] = time.time()
            self._record_answered(self._input_auto, payload)
            self._consume_input_marker(mid, key)
            self._diag(f"{label}の合図に応答しました",
                       rid=payload.split(":", 1)[1]
                       if ":" in payload else None)
            self._set_note(f"{label}の合図に応答しました", "ok")
        elif getattr(result, "needs_confirm", False):
            self._consume_input_marker(mid, key)
            self._diag(
                f"{label}の自動応答を見送り（入力欄に"
                f"書きかけ）: {message}",
                rid=payload.split(":", 1)[1]
                if ":" in payload else None)
            self._set_note(
                f"{label}の自動応答: 合図の窓の入力欄に"
                "書きかけがあるため送りませんでした", "warn")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。目印は消費しない。
            # 見張りが空いたときに撃ち直すので、依頼は
            # 消えない。
            self._note_deferred("入力の自動応答")
        else:
            self._consume_input_marker(mid, key)
            self._diag(f"{label}の自動応答に失敗: {message}",
                       rid=payload.split(":", 1)[1]
                       if ":" in payload else None)
            self._set_note(
                f"{label}の合図に応答できませんでした"
                f"（{message}）", "error")
        self._publish_phal_idle(f"{label}の合図への応答が終わりました")
        self._fire_deferred_restart()

    # ---------------- おどうぐばこへの登録の受け渡し (2.37) ----
    def _admit_register(self, item) -> bool:
        """宣言つきのファイルを、実行せず登録へ回す (2.37)。

        _admit_auto_run の途中で呼ばれ、登録の対象なら True を
        返す (呼び側は自動実行へ進まない)。判定は先頭の宣言行
        だけ (odougubako_core.parse_declarations)。登録済みの
        中身は二度回さない (tried の控え)。"""
        ident = (item.key, item.stat_key())
        if ident in self._register_tried:
            return True
        try:
            head = Path(item.path).read_text(
                encoding="utf-8", errors="replace")[:4000]
        except OSError:
            return False
        try:
            import odougubako_core
            decl = odougubako_core.parse_declarations(head)
        except Exception:  # noqa: BLE001  道具箱が無い環境では素通り
            return False
        if decl is None:
            return False
        self._register_tried.add(ident)
        if item.key not in self._register_queue:
            self._register_queue.append(item.key)
            self._diag(f"おどうぐばこへの登録の待ち列へ: {item.name}",
                       ai=item.ai or None)
        self._pump_register()
        return True

    def _pump_register(self):
        """登録の待ち列から、次の1件を始める (直列に限る)。"""
        if self._emergency_stopped or self._register_sending:
            return
        if self._running_item is not None or self._chain_in_flight():
            return
        while self._register_queue:
            key = self._register_queue.pop(0)
            item = self._items_by_key.get(key)
            if item is None:
                continue
            self._fire_auto_register(item)
            return

    def _fire_auto_register(self, item):
        """宣言つきファイルを odougubako_core で登録し、結果を
        送る (裏方仕事)。登録できないときも、その旨の文を送る。"""
        try:
            configured, profile = self._resolve_ai_target()
        except Exception as e:  # noqa: BLE001
            self._diag(
                f"道具登録の自動応答に失敗: 投げ先を解決できません（{e}）",
                ai=item.ai or None)
            return
        if not configured:
            self._diag("道具登録の自動応答に失敗: AI の投げ先が未設定",
                       ai=item.ai or None)
            self._set_note(
                "道具登録の自動応答: AI の投げ先が設定されていません",
                "error")
            return
        try:
            source = Path(item.path).read_text(encoding="utf-8")
        except OSError as e:
            self._diag(f"道具登録に失敗: ファイルが読めません（{e}）",
                       ai=item.ai or None)
            return
        try:
            import odougubako_core
            result = odougubako_core.register_source(
                source, created_from=item.name)
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "reason": f"登録で例外（{e}）",
                      "tool": None}
        if result.get("ok") and result.get("tool"):
            tool = result["tool"]
            text = (
                f"[PHAL:道具登録] {tool['name']} を登録しました"
                f"（分類: {tool['category']}、版: {tool['version']}）\n"
                f"説明: {tool['description']}\n"
                "ツール実行の合図で、名前で呼べます")
        else:
            text = (f"[PHAL:道具登録] 登録できませんでした: "
                    f"{result.get('reason') or '理由不明'}")
        sender = self.ai_sender or _default_ai_sender
        presser = self.ai_presser or _default_ai_presser
        self._register_sending = True
        self._register_auto_ai = item.ai or ""
        self._diag(f"道具登録の結果を送ります: {item.name}",
                   ai=item.ai or None)
        self._publish_phal_status(
            "answer", f"道具登録の結果を送っています: {item.name}")

        import threading

        def work():
            try:
                sent = sender(text, profile)
                if getattr(sent, "ok", False):
                    sent = presser(profile)
            except Exception as e:  # noqa: BLE001
                sent = _AiAnswerResult(
                    False, f"登録の結果を送れませんでした（{e}）")
            self._register_signals.done.emit(sent)

        threading.Thread(
            target=work, daemon=True,
            name="phal-register-auto").start()

    def _on_register_auto_done(self, result):
        self._register_sending = False
        if getattr(result, "ok", False):
            self._diag("道具登録の結果を送りました",
                       ai=getattr(self, "_register_auto_ai", "")
                       or None)
            self._set_note("道具を登録しました", "ok")
            self._publish_phal_idle("道具を登録しました")
        elif getattr(result, "deferred", False):
            # 送信待ちの文があり上書きを避けて保留された
            # (2.78、レビュー重大1)。
            self._note_deferred("道具登録の結果")
            self._set_note("道具登録の結果を保留しました（空いたら送ります）", "ok")
        else:
            message = str(getattr(result, "message", "") or "")
            self._diag(f"道具登録の結果の送信に失敗: {message}",
                       ai=getattr(self, "_register_auto_ai", "")
                       or None)
            self._set_note(
                f"道具登録の結果を送れませんでした（{message}）",
                "error")
            self._publish_phal_idle("道具登録の応答が終わりました")
        self._pump_register()
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

    def _note_deferred(self, what: str):
        """自動送信が保留 (deferred) されたときの共通の扱い
        (2.78、レビュー重大1)。

        送信待ちの文がある間は、ほかの自動送信は上書きを避けて
        保留される (2.71)。完了側で目印を消費しなければ、見張り
        が空いたときに撃ち直すので、依頼は消えない。撃ち直しの
        間隔は、ここで決める _auto_blocked_until を
        _chain_in_flight に混ぜて空ける (見回りのたびに裏方を
        起こさない)。診断は保留の期間ごとに1回だけ残す。"""
        now = time.monotonic()
        if not self._auto_blocked:
            self._auto_blocked = True
            self._diag(
                f"{what}を保留しました（送信待ちの文があるため）")
        self._auto_blocked_until = now + 5.0

    def _loop_mark(self, name: str):
        """ループ内訳の境目を打つ (2.79)。計測だけの控え。"""
        self._loop_marks[name] = time.monotonic()

    def _loop_begin(self):
        """新しいループの検出 (2.79)。

        前のループの「次の検出まで」を確定させてから、今回の
        境目を打ち始める。検出の区間は、前の送信完了から今回の
        検出まで (AI の生成と見張りの確認間隔を含む)。"""
        now = time.monotonic()
        if self._loop_stages is not None \
                and self._loop_prev_send_done is not None:
            for i, (name, sec) in enumerate(self._loop_stages):
                if name == "次の検出まで" and sec is None:
                    self._loop_stages[i] = (
                        name, now - self._loop_prev_send_done)
        self._loop_marks = {"detected": now,
                            "baseline": self._loop_prev_send_done}

    def _loop_finish_send(self):
        """送信が終わったら、今回のループの区間表を作る (2.79)。"""
        marks = self._loop_marks
        if "detected" not in marks or "send_done" not in marks:
            return

        def gap(a, b):
            if a in marks and b in marks:
                return max(0.0, marks[b] - marks[a])
            return None

        detect_sec = None
        if marks.get("baseline") is not None:
            detect_sec = max(0.0, marks["detected"] - marks["baseline"])
        dl_sec = None
        if "checked" in marks:
            dl_sec = max(
                0.0, marks["checked"]
                - (marks.get("check_elapsed") or 0.0)
                - marks["detected"])
        self._loop_stages = [
            ("検出", detect_sec),
            ("ダウンロード", dl_sec),
            ("チェック", marks.get("check_elapsed")),
            ("実行", gap("run_start", "run_done")),
            ("結果入力", gap("fill_start", "fill_done")),
            ("送信", gap("send_start", "send_done")),
            ("次の検出まで", None),
        ]
        self._loop_stages_name = marks.get("name", "")
        self._loop_prev_send_done = marks["send_done"]
        self._loop_marks = {}

    def _loop_breakdown_line(self) -> str:
        """診断の束・動作ログの要約へ出すループ内訳の1行
        (2.79)。"""
        if not self._loop_stages:
            return "ループ内訳: まだ完了したループがありません"
        return core.format_loop_breakdown(
            self._loop_stages, self._loop_stages_name)

    def _chain_in_flight(self) -> bool:
        """自動化の連鎖が途中かどうか。

        実行中・回答の投入中・送信中・受領応答の送信中・止まった
        知らせの送信中はもちろん途中。自動実行の待ち列に残りが
        ある場合も、切替が入っている間は途中とみなす (切替が
        切れた待ち列は進まないので、途中には数えない。詰まった
        まま開き直しが永遠に来ない事態を避ける)。送信待ちの文
        による保留の間隔 (_auto_blocked_until、2.78) の間も
        途中とみなし、自動応答の撃ち直しを間引く。"""
        if time.monotonic() < self._auto_blocked_until:
            return True
        self._auto_blocked = False
        if self._running_item is not None or self._ai_filling \
                or self._ai_sending or self._diag_sending \
                or self._ack_sending or self._notice_sending \
                or self._settings_sending or self._manual_sending \
                or self._file_sending or self._fulltext_sending \
                or self._history_sending or self._stop_sending \
                or self._log_sending or self._tool_sending \
                or self._refire_sending or self._relay_sending \
                or self._react_sending:
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
    def _toggle_mini_mode(self):
        self._set_mini_mode(bool(self.mini_button.isChecked()),
                            save=True)

    def _set_item_visible(self, item, visible):
        """レイアウトの要素 (部品か入れ子のレイアウト) を
        まとめて出し入れする (2.27、小窓モード用)。"""
        widget = item.widget()
        if widget is not None:
            widget.setVisible(visible)
            return
        sub = item.layout()
        if sub is not None:
            for i in range(sub.count()):
                child = sub.itemAt(i)
                if child is not None:
                    self._set_item_visible(child, visible)

    def _refit_mini_height(self):
        """小窓モードの窓の高さを、表示中の上部だけの寸法に
        合わせ直す (2.28)。

        隠した直後はレイアウトの寸法計算が古いままのことが
        あり、そのまま sizeHint を使うと大きな高さが残る
        (2.27 の実機で発生)。先にレイアウトを再計算させて
        から合わせる。"""
        if not getattr(self, "_mini_mode", False):
            return
        layout = self.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.resize(self.width(), self.sizeHint().height())

    def _set_mini_mode(self, on, save=False):
        """小窓モードの切り替え (2.27)。

        上部 (タイトル行と状態パネル) だけを残し、下部を
        隠して窓をその高さに縮める。戻すときは、入る前に
        覚えた高さへ戻す。状態は設定に覚える (任意)。"""
        on = bool(on)
        self._mini_mode = on
        if getattr(self, "mini_button", None) is not None:
            self.mini_button.setChecked(on)
        layout = self.layout()
        if layout is None:
            return
        if on and not self._normal_height:
            self._normal_height = self.height()
        for i in range(getattr(self, "_lower_start", 0),
                       layout.count()):
            item = layout.itemAt(i)
            if item is not None:
                self._set_item_visible(item, not on)
        if on:
            self._refit_mini_height()
            # 実機では枠やレイアウトの確定が遅れることがある
            # ため、次の巡回でもう一度合わせる (2.28)。
            QTimer.singleShot(0, self._refit_mini_height)
        else:
            height = self._normal_height or 0
            self._normal_height = None
            self.resize(self.width(),
                        max(height, self.sizeHint().height()))
        if save:
            self.config["mini_mode"] = on
            try:
                settings_module.update_settings_section(
                    "script_runner", {"mini_mode": on},
                    self.settings_path)
            except Exception:  # noqa: BLE001  覚えられないだけ
                pass

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

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, "_saved_frame_rect", None):
            self._apply_saved_frame_rect()
            # 枠の確定が遅れる場合に備え、少し置いてもう一度。
            QTimer.singleShot(600, self._apply_saved_frame_rect)

    def _apply_saved_frame_rect(self):
        """保存した枠の座標・大きさへ置き直す (1.68、1.72 で単純化)。

        保存値は枠 (フレーム) の座標・大きさで、閉じるときに
        frameGeometry() で取ったもの。大きさは枠が保存値に
        なるクライアント寸法へ resize し、位置は move() に
        枠の座標をそのまま渡して戻す。

        1.69〜1.71 の版は、ずれた分をクライアント座標
        (geometry()) で計算して move() に渡していた。
        move() が受けるのは枠の座標なので、直すたびに
        タイトルバー分 (実機で 31px) 下へ置き直していた
        (1.70 の照合記録で判明)。照合を繰り返す仕組みは
        廃止し、表示時と 600ms 後の2回だけ置き直す。"""
        rect = getattr(self, "_saved_frame_rect", None)
        if not rect or rect[0] is None or rect[1] is None:
            return
        try:
            frame = self.frameGeometry()
            client = self.geometry()
            self.resize(max(1, int(rect[2]) - (frame.width()
                                                - client.width())),
                        max(1, int(rect[3]) - (frame.height()
                                               - client.height())))
            self.move(int(rect[0]), int(rect[1]))
        except Exception:  # noqa: BLE001  位置の復元で落とさない
            pass
        if getattr(self, "_mini_mode", False):
            # 小窓で開いたときは、保存枠 (通常時の大きさ) から
            # 通常時の高さを覚え直し、窓は小窓の高さに合わせる
            # (2.27)。
            try:
                frame = self.frameGeometry()
                client = self.geometry()
                self._normal_height = max(
                    1, int(rect[3]) - (frame.height()
                                       - client.height()))
                self._refit_mini_height()
            except Exception:  # noqa: BLE001
                pass

    def closeEvent(self, event):
        try:
            self._poll_timer.stop()
            if self._run_handle is not None:
                self._run_handle.stop()
            self._check_worker.stop()
            rect = self.frameGeometry()
            save_rect = [rect.x(), rect.y(),
                         rect.width(), rect.height()]
            if getattr(self, "_mini_mode", False) \
                    and getattr(self, "_normal_height", None):
                save_rect[3] = (rect.height()
                                + int(self._normal_height)
                                - self.height())
            settings_module.update_settings_section(
                "script_runner",
                {"window_geometry": save_rect,
                 "always_on_top": bool(self.config.get("always_on_top")),
                 "hide_no_id": bool(self.config.get("hide_no_id")),
                 "dark_mode": bool(self.config.get("dark_mode")),
                 "column_widths": [
                     self.table.horizontalHeader().sectionSize(col)
                     for col in range(8)]},
                self.settings_path)
        except Exception:  # noqa: BLE001  終わりの保存で落とさない
            pass
        try:
            # 開き直し引き継ぎ (1.70)。直近の候補の key を残す。
            core.save_dl_session(
                self._primary_state.last_candidate_key)
        except Exception:  # noqa: BLE001  引き継ぎで落とさない
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
