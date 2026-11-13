# editor/window.py
# テキストエディタの窓。土台は QPlainTextEdit(editor/text_edit.py の
# TextEdit)、独立した QMainWindow。
#
# 第1段の範囲(仕様 docs/work-tools/spec.md 案B):
#   開く / 新規 / 上書き保存 / 名前を付けて保存(文字コードと改行は
#   開いたときのものを守り、変換は利用者が選んだときだけ)、
#   検索(次へ・前へ)、選択範囲を Copilot に投げる(copilot_io 経由)。
#
# 第2段の土台(同じく案B):
#   矩形選択は rect_edit の共用部品(text_edit 経由。こちらの矩形は
#   行を足せる)。マクロは「命令の列」の記録・再生(editor/macros.py)。
#   編集の操作は命令層(execute_command)を通し、キー割り当てと
#   マクロの再生が同じ命令を呼ぶ。F4 は直前の命令の繰り返し。
#   行の並べ替え・重複行の削除・選んだ範囲の変換(全角・半角と
#   大文字・小文字。editor/convert.py)も命令として載っている。
#
# 実機フィードバック(2026-11-13)で足したもの:
#   行番号、リボン風ツールバー、左ペイン(アウトラインのツリー＋下部の
#   ファイル情報)、フォントの既定(MS Gothic)と Ctrl+ホイールでの
#   大きさ変更、星印の行の強調表示、アウトライン・強調の正規表現の
#   設定ダイアログ、行末まで切り取り(Ctrl+K、命令 cut_to_end)。
# 表示の状態とフォントと正規表現は settings の editor 節に保存され、
# 呼び側が on_settings_change で受けて update_settings_section する。
# 正規表現まわりには、遅い式で固まらないための仕掛けが3つある:
#   各行は先頭200字だけ match する(outline.MATCH_HEAD_CHARS)、
#   設定ダイアログは OK のときに別プロセスで試験して遅い式を断る
#   (trial_patterns)、適用の前に「適用中」の印を書いて起動時に
#   残っていたら既定に戻す(pattern_applying、起動時の保険)。
#
# 落ちないための作法(このリポジトリの決まり):
#   - 外と触る入口(ファイル読み書き・送信)は例外を利用者への文にする。
#     スロットで投げ切るとプロセスごと終了する
#   - Copilot への送信は別スレッドで走らせ、結果は Signal 経由で窓に戻す
#     (ワーカースレッドから Qt を直接触らない。_AgentLoopBridge と同じ)。
#     UIA の実体は copilot_io の裏方プロセスが持つ(常駐の中で UIA を
#     動かすと pycaw と同居して即死するため。copilot_io 側の作法)
import json
import os
import re
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QFont,
    QGuiApplication,
    QKeySequence,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import copilot_io
import text_encoding
from editor import convert as convmod
from editor import document as docmod
from editor import macros as macmod
from editor import outline
from editor.highlighter import LineHighlighter
from editor.keymap import merge_keymap
from editor.outline_pane import OutlinePane
from editor.text_edit import TextEdit

APP_NAME = "テキストエディタ"
ENCODING_ORDER = ["utf-8", "cp932", "utf-16-le", "utf-16-be", "euc_jp", "iso2022_jp"]
# フォントの既定。settings の editor 節(font_family / font_size)で
# 上書きできる。大きさは第1段までの既定(10pt)に合わせた。
DEFAULT_FONT_FAMILY = "MS Gothic"
DEFAULT_FONT_SIZE = 10
# アウトラインの再構築を編集に追随させるときのまとめ待ち(ms)。
OUTLINE_REBUILD_DELAY_MS = 300

# フォントの大きさの保存のまとめ待ち(ms)。Ctrl+ホイールの1ノッチ
# ごとに設定ファイルを書くと書き込みが嵐になるので、止まってから
# まとめて1回だけ書く。
FONT_SAVE_DELAY_MS = 600

# 正規表現の「適用中」の印に使う、settings の editor 節のキー。
# 式を適用する前に True を書き、正常に適用できたら False に戻す。
# 起動したときに True のまま残っていたら、前回は適用の途中で固まった
# 疑いがあるので、保存済みの式は使わず既定に戻して開く(起動時の保険)。
PATTERN_APPLYING_KEY = "pattern_applying"

# 設定ダイアログの試験を別プロセスで待つ上限(秒)。試験自体の制限
# (outline.TRIAL_TIME_LIMIT_SEC)に、子プロセスの起動のぶんを足したもの。
# 上限を超えたら子プロセスごと止める(遅い式は呼び出し側では止められない)。
TRIAL_SUBPROCESS_TIMEOUT_SEC = outline.TRIAL_TIME_LIMIT_SEC + 2.0

# 試験の子プロセスで走らせるコード。標準入力の JSON
# ({"patterns": [...], "samples": [...]})を読み、各式を各試験文字列に
# match して、かかった秒数を標準出力へ1行で出す。Qt は読み込まない。
_TRIAL_CHILD_CODE = (
    "import json, re, sys, time\n"
    "data = json.load(sys.stdin)\n"
    "start = time.perf_counter()\n"
    "for pattern in data['patterns']:\n"
    "    rx = re.compile(pattern)\n"
    "    for sample in data['samples']:\n"
    "        rx.match(sample)\n"
    "sys.stdout.write(repr(time.perf_counter() - start))\n"
)


def trial_patterns(patterns, timeout=TRIAL_SUBPROCESS_TIMEOUT_SEC):
    """正規表現の並びが遅すぎないかを、別プロセスで試験する。

    戻り値は match にかかった秒数。時間切れ・起動失敗・応答が読めない
    ときは None(= 遅すぎるか、確かめられなかった。どちらも保存しない
    側に倒す)。遅い式は同じプロセスでは止められないので、子プロセス
    ごと打ち切れる形にしてある。"""
    payload = json.dumps({
        "patterns": [str(p) for p in patterns],
        "samples": outline.adversarial_samples(),
    })
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _TRIAL_CHILD_CODE],
            input=payload, capture_output=True, text=True,
            timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


class _SendBridge(QObject):
    """送信スレッド → 窓への結果の受け渡し(オブジェクトは使い回す)。"""

    finished = Signal(object)


def dirty_window_state(windows):
    """エディタの窓の並びから、未保存の (件数, 状態を読めなかった件数) を返す。

    再起動・終了の巻き添え判定(feature_screen.restart_blockers と
    filer.confirm_losses)が共用する。読めない窓は「未保存があるかも」
    側に倒すため、落とさず件数として返す。"""
    dirty = 0
    unreadable = 0
    for window in windows or []:
        if window is None:
            continue
        try:
            if window.is_dirty():
                dirty += 1
        except Exception:  # noqa: BLE001  読めない窓は「あるかも」側に倒す
            unreadable += 1
    return dirty, unreadable


def _compile_or_default(text, default_rx, default_text):
    """正規表現の文字列をコンパイルする。不正なら (既定, 既定の文字列)。

    設定ファイルが手で書き換えられていても、窓が開かなくならないため。"""
    try:
        return re.compile(text), text
    except re.error:
        return default_rx, default_text


class SaveOptionsDialog(QDialog):
    """「名前を付けて保存」で、文字コード・改行・BOM を選ぶ小さな窓。

    既定は文書が持っている今の値。ここで変えたときだけ変換が起きる。"""

    def __init__(self, doc, parent=None):
        super().__init__(parent)
        self.setWindowTitle("保存の形式")
        self._encoding = QComboBox()
        for enc in ENCODING_ORDER:
            self._encoding.addItem(
                text_encoding.ENCODING_LABELS.get(enc, enc), enc)
        self._encoding.setCurrentIndex(
            max(0, ENCODING_ORDER.index(doc.encoding))
            if doc.encoding in ENCODING_ORDER else 0)
        self._newline = QComboBox()
        self._newline.addItem("CRLF", docmod.CRLF)
        self._newline.addItem("LF", docmod.LF)
        self._newline.setCurrentIndex(0 if doc.newline == docmod.CRLF else 1)
        self._bom = QCheckBox("BOM を付ける")
        self._bom.setChecked(doc.bom)
        self._encoding.currentIndexChanged.connect(self._sync_bom_enabled)
        self._sync_bom_enabled()

        form = QFormLayout(self)
        form.addRow("文字コード", self._encoding)
        form.addRow("改行", self._newline)
        form.addRow("", self._bom)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _sync_bom_enabled(self):
        supported = bool(text_encoding.bom_bytes(self._encoding.currentData()))
        self._bom.setEnabled(supported)
        if not supported:
            self._bom.setChecked(False)

    def values(self):
        """(文字コード, 改行, BOM を付けるか)。"""
        return (self._encoding.currentData(),
                self._newline.currentData(),
                self._bom.isChecked())


class PatternSettingsDialog(QDialog):
    """アウトラインとみなす行・強調表示する行の正規表現を編集する窓。

    不正な正規表現はエラーを出して受け付けない(保存しない)。文法が
    通っても、遅すぎる式(破滅的なバックトラックで窓が固まるもの)は
    試験してからでないと受け付けない。

    trial_runner は「式の並び → かかった秒数(確かめられなければ
    None)」の関数で、既定は別プロセスで試験する trial_patterns。
    テストでは差し替えられる。"""

    def __init__(self, outline_pattern, highlight_pattern, parent=None,
                 trial_runner=None):
        super().__init__(parent)
        self._trial_runner = (trial_runner if trial_runner is not None
                              else trial_patterns)
        self.setWindowTitle("アウトライン・強調の設定")
        self._outline_edit = QLineEdit(outline_pattern)
        self._highlight_edit = QLineEdit(highlight_pattern)
        self._error_label = QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color: #c00000;")

        form = QFormLayout(self)
        form.addRow("アウトラインとみなす行の正規表現", self._outline_edit)
        form.addRow("強調表示する行の正規表現", self._highlight_edit)
        form.addRow(self._error_label)
        row = QHBoxLayout()
        self._reset_button = QPushButton("既定に戻す")
        self._reset_button.clicked.connect(self._reset_defaults)
        row.addWidget(self._reset_button)
        row.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        form.addRow(row)

    def _reset_defaults(self):
        self._outline_edit.setText(outline.DEFAULT_OUTLINE_PATTERN)
        self._highlight_edit.setText(outline.DEFAULT_HIGHLIGHT_PATTERN)
        self._error_label.clear()

    def validation_error(self):
        """不正があれば利用者向けの文を、無ければ None を返す。"""
        for label, value in (
                ("アウトライン", self._outline_edit.text()),
                ("強調表示", self._highlight_edit.text())):
            if not value.strip():
                return f"{label}の正規表現が空です"
            try:
                re.compile(value)
            except re.error as e:
                return f"{label}の正規表現が不正です（{e}）"
        return None

    def trial_error(self):
        """遅すぎる式なら利用者向けの文を、問題なければ None を返す。

        試験文字列は outline.adversarial_samples(意地の悪い並び)。
        試験全体で outline.TRIAL_TIME_LIMIT_SEC を超える式と、試験
        自体が確かめられなかった場合は、保存しない側に倒す。"""
        outline_text, highlight_text = self.values()
        try:
            seconds = self._trial_runner([outline_text, highlight_text])
        except Exception:  # noqa: BLE001  試験できないものは通さない
            seconds = None
        if seconds is None or seconds > outline.TRIAL_TIME_LIMIT_SEC:
            return ("正規表現の判定に時間がかかりすぎます"
                    "（窓が固まるおそれがあるため、保存しません）")
        return None

    def accept(self):  # noqa: N802  Qt の上書きではないが名前は Qt 側に合わせる
        error = self.validation_error()
        if error is None:
            error = self.trial_error()
        if error is not None:
            self._error_label.setText(error)
            return
        super().accept()

    def values(self):
        """(アウトラインの正規表現, 強調表示の正規表現)。"""
        return (self._outline_edit.text(), self._highlight_edit.text())


class EditorWindow(QMainWindow):
    """テキストエディタの窓。閉じたら closed にジオメトリ([x, y, w, h])が流れる
    (ビュアと同じ形。呼び側が設定へ保存する)。

    editor_settings は settings の editor 節(辞書)。font_family /
    font_size / show_line_numbers / show_outline_pane / outline_pattern /
    highlight_pattern / pattern_applying(適用中の印。起動時の保険で
    読む)を読み(無いキーは既定)、表示やフォントや正規表現が
    変わったら、差分を on_settings_change(辞書を受ける関数)へ流すので、
    保存は呼び側の流儀(update_settings_section)に任せる。"""

    closed = Signal(object)

    def __init__(self, path=None, *, geometry=None, keymap_overrides=None,
                 sender=None, font=None, editor_settings=None,
                 on_settings_change=None, parent=None):
        super().__init__(parent)
        self._doc = docmod.TextDocument()
        self._keymap = merge_keymap(keymap_overrides)
        # 送信の実体は差し替えられる(テストは fake を入れる)。
        self._sender = sender if sender is not None else copilot_io.send_text
        self._send_thread = None
        self._bridge = _SendBridge(self)
        self._bridge.finished.connect(self._on_send_finished)
        # 未保存で閉じる/新規にするときの確認は差し替えられる
        # (テストは QMessageBox を出さずにここを差す)。
        # 戻り値は "save" / "discard" / "cancel"。
        self.ask_save_changes = None
        # Copilot の入力欄に書きかけの文があるとき、上書きして送って
        # よいかの確認も差し替えられる(戻り値は bool)。
        self.ask_overwrite_input = None
        self._pending_send_text = None
        self._on_settings_change = on_settings_change
        section = editor_settings if isinstance(editor_settings, dict) else {}

        # マクロ(命令の列の記録・再生)と命令層の状態。文書の変化の
        # 信号が流れ始める前に用意する。
        self._recorder = macmod.MacroRecorder()
        # 記録中に起きた「記録できない操作」の件数(項目3の知らせ)。
        self._macro_skips = 0
        self._current_macro = []
        self._current_macro_name = ""
        self._in_command = False      # 命令の実行中(変化を二重に数えない)
        self._replaying = False       # マクロの再生中
        self._suppress_change_record = False  # 元に戻す・やり直しの最中
        # 変化の直前の (選択の開始, 選択の終了, カーソル位置)。削除の
        # 向き(BS か Del か、選択の削除か)の判定に使う。
        self._change_cache = (0, 0, 0)
        self._command_impls = {
            "insert_text": self._cmd_insert_text,
            "delete_backward": self._cmd_delete_backward,
            "delete_forward": self._cmd_delete_forward,
            "delete_selection": self._cmd_delete_selection,
            "cut_to_end": self._cmd_cut_to_end,
            "delete_line": self._cmd_delete_line,
            "find_next": self._cmd_find_next,
            "find_prev": self._cmd_find_prev,
            "move_line_start": self._cmd_move_line_start,
            "move_line_end": self._cmd_move_line_end,
            "move_line_up": self._cmd_move_line_up,
            "move_line_down": self._cmd_move_line_down,
            "move_char_left": self._cmd_move_char_left,
            "move_char_right": self._cmd_move_char_right,
            "sort_lines": self._cmd_sort_lines,
            "remove_duplicate_lines": self._cmd_remove_duplicate_lines,
            "convert_halfwidth": self._cmd_convert_halfwidth,
            "convert_fullwidth": self._cmd_convert_fullwidth,
            "convert_lower": self._cmd_convert_lower,
            "convert_upper": self._cmd_convert_upper,
        }

        self._edit = TextEdit()
        if font is not None:
            edit_font = font
        else:
            family = section.get("font_family") or DEFAULT_FONT_FAMILY
            size = section.get("font_size")
            if not isinstance(size, int) or size <= 0:
                size = DEFAULT_FONT_SIZE
            edit_font = QFont(str(family), size)
        self._edit.setFont(edit_font)
        if not section.get("show_line_numbers", True):
            self._edit.set_line_numbers_visible(False)

        # アウトライン・強調の正規表現(実効値)。設定が空なら既定、
        # 不正なら既定に倒す(窓が開かなくならないため)。
        # 起動時の保険: 前回の適用で「適用中」の印(pattern_applying)が
        # 残っていたら、前回は適用の途中で固まった疑いがある。保存済み
        # の式は使わず既定に戻して開く(設定への書き戻しは、印を立てる
        # ときに一緒に流す)。
        self._pattern_reset_updates = None
        if section.get(PATTERN_APPLYING_KEY):
            outline_text = ""
            highlight_text = ""
            self._pattern_reset_updates = {
                "outline_pattern": "",
                "highlight_pattern": "",
            }
        else:
            outline_text = section.get("outline_pattern") or ""
            highlight_text = section.get("highlight_pattern") or ""
        outline_rx, self._outline_pattern_text = _compile_or_default(
            outline_text or outline.DEFAULT_OUTLINE_PATTERN,
            outline.DEFAULT_OUTLINE_RX, outline.DEFAULT_OUTLINE_PATTERN)
        self._outline_rx = outline_rx
        highlight_rx, self._highlight_pattern_text = _compile_or_default(
            highlight_text or outline.DEFAULT_HIGHLIGHT_PATTERN,
            outline.DEFAULT_HIGHLIGHT_RX, outline.DEFAULT_HIGHLIGHT_PATTERN)
        self._highlighter = LineHighlighter(
            self._edit.document(), highlight_rx)
        self._pattern_guard_armed = False

        self._outline_pane = OutlinePane()
        self._outline_pane.jump_requested.connect(self._jump_to_line)
        if not section.get("show_outline_pane", True):
            self._outline_pane.setVisible(False)
        self._outline_timer = QTimer(self)
        self._outline_timer.setSingleShot(True)
        self._outline_timer.setInterval(OUTLINE_REBUILD_DELAY_MS)
        self._outline_timer.timeout.connect(self._rebuild_outline)

        # フォントの大きさの遅延保存(Ctrl+ホイールの嵐対策)。
        self._pending_font_size = None
        self._font_save_timer = QTimer(self)
        self._font_save_timer.setSingleShot(True)
        self._font_save_timer.setInterval(FONT_SAVE_DELAY_MS)
        self._font_save_timer.timeout.connect(self._flush_pending_font_size)

        self._edit.document().modificationChanged.connect(
            lambda _modified: self._refresh_title())
        self._edit.cursorPositionChanged.connect(self._refresh_position)
        self._edit.cursorPositionChanged.connect(self._track_change_cache)
        self._edit.font_size_changed.connect(self._on_font_size_changed)
        self._edit.document().contentsChanged.connect(
            self._on_contents_changed)
        self._edit.document().contentsChange.connect(
            self._on_document_change_for_macro)
        self._edit.move_key.connect(self._on_move_key)
        self._edit.unrecorded_move.connect(self._note_macro_skip)
        self._edit.mouse_pressed.connect(self._note_macro_skip)
        self._edit.undo_redo_started.connect(
            lambda: setattr(self, "_suppress_change_record", True))
        self._edit.undo_redo_finished.connect(
            lambda: setattr(self, "_suppress_change_record", False))

        self._find_bar = self._build_find_bar()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_layout.addWidget(self._outline_pane)
        body_layout.addWidget(self._edit, 1)
        layout.addWidget(body)
        layout.addWidget(self._find_bar)
        self.setCentralWidget(central)
        self._find_bar.hide()

        self._build_menus()
        self._build_toolbar()
        self._enc_label = QLabel()
        self._pos_label = QLabel()
        self.statusBar().addPermanentWidget(self._enc_label)
        self.statusBar().addPermanentWidget(self._pos_label)
        if geometry and len(geometry) == 4:
            self.setGeometry(*geometry)
        else:
            self.resize(760, 560)
        # ここから先で式が実際に走る(アウトライン抽出と強調の適用)。
        # 適用の前に「適用中」の印を立て、無事に開けたら消す。途中で
        # 固まれば印が残り、次回の起動で既定に戻る(起動時の保険)。
        self._arm_pattern_guard()
        self._refresh_all()
        if path:
            self.open_path(path)
        self._disarm_pattern_guard()
        # 開いた直後は本文にフォーカスを置く(ショートカットは本文に
        # フォーカスがあるときだけ効く作りなので)。
        self._edit.setFocus()

    # ---------------------------------------------------------------
    # 組み立て
    # ---------------------------------------------------------------
    def _action(self, label, slot, keymap_name=None, menu=None):
        action = QAction(label, self)
        if keymap_name:
            keys = self._keymap.get(keymap_name)
            if keys:
                action.setShortcut(QKeySequence(keys))
            # ショートカットは本文にフォーカスがあるときだけ効かせる。
            # 窓全体に効かせると、検索欄で打った Ctrl+K が本文の
            # 行末まで切り取りとして動いてしまう(レビュー項目6)。
            action.setShortcutContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._edit.addAction(action)
        action.triggered.connect(slot)
        if menu is not None:
            menu.addAction(action)
        return action

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")
        self._action("新規(&N)", self.new_document, "new", file_menu)
        self._act_open = self._action(
            "開く(&O)…", self.open_dialog, "open", file_menu)
        self._act_save = self._action(
            "上書き保存(&S)", lambda: self.save(), "save", file_menu)
        self._act_save_as = self._action(
            "名前を付けて保存(&A)…", self.save_as, "save_as", file_menu)
        file_menu.addSeparator()
        self._action("閉じる(&C)", self.close, "close_window", file_menu)

        edit_menu = self.menuBar().addMenu("編集(&E)")
        # 元に戻す等は QPlainTextEdit の標準キー(Ctrl+Z 等)が効く。
        # メニューからは同じ槽を呼ぶだけ(最中の変化は記録しない)。
        self._action("元に戻す(&U)", self._undo_edit, None, edit_menu)
        self._action("やり直し(&R)", self._redo_edit, None, edit_menu)
        edit_menu.addSeparator()
        self._action("切り取り(&T)", self._edit.cut, None, edit_menu)
        self._action("コピー(&C)", self._edit.copy, None, edit_menu)
        self._action("貼り付け(&P)", self._edit.paste, None, edit_menu)
        self._action("すべて選択(&A)", self._edit.selectAll, None, edit_menu)
        edit_menu.addSeparator()
        self._act_cut_eol = self._action(
            "行末まで切り取り(&K)",
            lambda: self.execute_command("cut_to_end"),
            "cut_to_end", edit_menu)
        self._action("行を削除(&D)",
                     lambda: self.execute_command("delete_line"),
                     "delete_line", edit_menu)
        edit_menu.addSeparator()
        self._action("行を並べ替え（昇順）(&S)",
                     lambda: self.execute_command(
                         "sort_lines", {"order": "asc"}),
                     None, edit_menu)
        self._action("行を並べ替え（降順）(&R)",
                     lambda: self.execute_command(
                         "sort_lines", {"order": "desc"}),
                     None, edit_menu)
        self._action("重複行を削除(&U)",
                     lambda: self.execute_command("remove_duplicate_lines"),
                     None, edit_menu)
        convert_menu = edit_menu.addMenu("変換(&T)")
        self._action("全角 → 半角(&H)",
                     lambda: self.execute_command("convert_halfwidth"),
                     None, convert_menu)
        self._action("半角 → 全角(&F)",
                     lambda: self.execute_command("convert_fullwidth"),
                     None, convert_menu)
        self._action("大文字 → 小文字(&L)",
                     lambda: self.execute_command("convert_lower"),
                     None, convert_menu)
        self._action("小文字 → 大文字(&U)",
                     lambda: self.execute_command("convert_upper"),
                     None, convert_menu)
        edit_menu.addSeparator()
        self._action("直前の操作を繰り返す(&F)", self.repeat_last,
                     "repeat_last", edit_menu)

        search_menu = self.menuBar().addMenu("検索(&S)")
        self._act_find = self._action(
            "検索(&F)…", self.show_find_bar, "find", search_menu)
        self._action("次を検索(&N)", lambda: self.execute_command("find_next"),
                     "find_next", search_menu)
        self._action("前を検索(&P)", lambda: self.execute_command("find_prev"),
                     "find_prev", search_menu)

        view_menu = self.menuBar().addMenu("表示(&V)")
        self._act_outline = QAction("アウトライン(&O)", self)
        self._act_outline.setCheckable(True)
        self._act_outline.setChecked(self._outline_pane.isVisible())
        self._act_outline.toggled.connect(self.set_outline_pane_visible)
        view_menu.addAction(self._act_outline)
        self._act_line_numbers = QAction("行番号(&L)", self)
        self._act_line_numbers.setCheckable(True)
        self._act_line_numbers.setChecked(self._edit.line_numbers_visible())
        self._act_line_numbers.toggled.connect(self.set_line_numbers_visible)
        view_menu.addAction(self._act_line_numbers)
        view_menu.addSeparator()
        self._action("アウトライン・強調の設定(&P)…",
                     self.show_pattern_settings, None, view_menu)

        macro_menu = self.menuBar().addMenu("マクロ(&M)")
        self._act_macro_record = QAction("記録開始(&R)", self)
        self._act_macro_record.setCheckable(True)
        self._act_macro_record.toggled.connect(self.set_macro_recording)
        macro_menu.addAction(self._act_macro_record)
        self._action("再生(&P)", self.play_macro, None, macro_menu)
        macro_menu.addSeparator()
        self._action("マクロを保存(&S)…", self.save_macro_dialog,
                     None, macro_menu)
        self._action("マクロを開いて再生(&O)…", self.load_macro_dialog,
                     None, macro_menu)

        copilot_menu = self.menuBar().addMenu("Copilot(&C)")
        self._act_send = self._action(
            "選択範囲を Copilot に投げる(&S)", self.send_selection,
            "send_selection", copilot_menu)

    def _build_toolbar(self):
        """リボン風のツールバー。メニューと同じ QAction を並べるので、
        処理もチェック状態も二重にならない。"""
        toolbar = QToolBar("操作", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (self._act_open, self._act_save, self._act_save_as,
                       self._act_find, self._act_send):
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self._act_outline)
        toolbar.addAction(self._act_line_numbers)
        self.addToolBar(toolbar)
        self._toolbar = toolbar

    def _build_find_bar(self):
        bar = QFrame()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 4, 6, 4)
        row.addWidget(QLabel("検索"))
        self._find_edit = QLineEdit()
        self._find_edit.returnPressed.connect(
            lambda: self.execute_command("find_next"))
        row.addWidget(self._find_edit, 1)
        self._find_case = QCheckBox("大文字小文字を区別")
        row.addWidget(self._find_case)
        next_button = QPushButton("次へ")
        next_button.clicked.connect(lambda: self.execute_command("find_next"))
        row.addWidget(next_button)
        prev_button = QPushButton("前へ")
        prev_button.clicked.connect(lambda: self.execute_command("find_prev"))
        row.addWidget(prev_button)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self._hide_find_bar)
        row.addWidget(close_button)
        escape = QShortcut(QKeySequence("Esc"), bar)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._hide_find_bar)
        # 本文側のショートカットは本文にフォーカスがあるときだけ
        # 効くので、検索欄にフォーカスがあっても次・前へ進めるよう、
        # 検索バーにも同じキー(F3 等)を置く。
        next_keys = self._keymap.get("find_next")
        if next_keys:
            next_shortcut = QShortcut(QKeySequence(next_keys), bar)
            next_shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            next_shortcut.activated.connect(
                lambda: self.execute_command("find_next"))
        prev_keys = self._keymap.get("find_prev")
        if prev_keys:
            prev_shortcut = QShortcut(QKeySequence(prev_keys), bar)
            prev_shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut)
            prev_shortcut.activated.connect(
                lambda: self.execute_command("find_prev"))
        return bar

    # ---------------------------------------------------------------
    # 設定の保存(呼び側へ差分を流す)
    # ---------------------------------------------------------------
    def _persist_settings(self, updates):
        """表示・フォント・正規表現の変更を呼び側へ渡す。

        スロットから呼ばれるので、保存に失敗しても窓を落とさない。"""
        if self._on_settings_change is None:
            return
        try:
            self._on_settings_change(dict(updates))
        except Exception:  # noqa: BLE001  保存の失敗で編集を止めない
            self.statusBar().showMessage("設定を保存できませんでした", 4000)

    def _arm_pattern_guard(self):
        """正規表現の適用の前に「適用中」の印(PATTERN_APPLYING_KEY)を
        設定へ書く。

        保存の口が無い(呼び側が設定を持たない)ときは何もしない。
        起動時の保険で既定に戻す場合は、その書き戻し(式を空にする)
        も同じ書き込みで流す。"""
        if self._on_settings_change is None:
            return
        updates = {PATTERN_APPLYING_KEY: True}
        if self._pattern_reset_updates:
            updates.update(self._pattern_reset_updates)
            self._pattern_reset_updates = None
        self._persist_settings(updates)
        self._pattern_guard_armed = True

    def _disarm_pattern_guard(self):
        """適用が無事に済んだので「適用中」の印を消す。"""
        if not self._pattern_guard_armed:
            return
        self._pattern_guard_armed = False
        self._persist_settings({PATTERN_APPLYING_KEY: False})

    # ---------------------------------------------------------------
    # 表示の切り替え
    # ---------------------------------------------------------------
    def set_outline_pane_visible(self, visible):
        visible = bool(visible)
        self._outline_pane.setVisible(visible)
        if self._act_outline.isChecked() != visible:
            blocker = QSignalBlocker(self._act_outline)
            self._act_outline.setChecked(visible)
        self._persist_settings({"show_outline_pane": visible})

    def outline_pane_visible(self) -> bool:
        return self._outline_pane.isVisible()

    def set_line_numbers_visible(self, visible):
        visible = bool(visible)
        self._edit.set_line_numbers_visible(visible)
        if self._act_line_numbers.isChecked() != visible:
            blocker = QSignalBlocker(self._act_line_numbers)
            self._act_line_numbers.setChecked(visible)
        self._persist_settings({"show_line_numbers": visible})

    def line_numbers_visible(self) -> bool:
        return self._edit.line_numbers_visible()

    def font_size(self) -> int:
        return self._edit.font_size()

    def _on_font_size_changed(self, size):
        # Ctrl+ホイールは連続して流れてくるので、その場では書かず、
        # 止まってから最後の大きさを1回だけ書く(遅延保存)。
        self._pending_font_size = size
        self._font_save_timer.start()

    def _flush_pending_font_size(self):
        """たまっているフォントの大きさの保存を、いま書く。"""
        if self._pending_font_size is None:
            return
        size = self._pending_font_size
        self._pending_font_size = None
        self._font_save_timer.stop()
        self._persist_settings({"font_size": size})

    # ---------------------------------------------------------------
    # アウトラインと左ペインの情報
    # ---------------------------------------------------------------
    def _on_contents_changed(self):
        self._outline_timer.start()
        self._refresh_pane_counts()

    def _rebuild_outline(self):
        entries, truncated = outline.extract_outline_ex(
            self._edit.toPlainText(), self._outline_rx,
            limit=outline.OUTLINE_ENTRY_LIMIT)
        self._outline_pane.set_outline(
            outline.build_outline_tree(entries), truncated=truncated)

    def _jump_to_line(self, line_number):
        document = self._edit.document()
        if line_number < 0 or line_number >= document.blockCount():
            return
        # ブロック番号で引く。findBlockByLineNumber は折り返した
        # 「見た目の行」で数えるので、長い行があると別の行へ飛ぶ。
        block = document.findBlockByNumber(line_number)
        cursor = QTextCursor(block)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        self._edit.setFocus()

    def _file_size_text(self):
        path = self._doc.path
        if path:
            try:
                return f"{os.path.getsize(path):,} バイト"
            except OSError:
                pass
        return "—"

    def _refresh_pane_info(self):
        label = text_encoding.ENCODING_LABELS.get(self._doc.encoding,
                                                  self._doc.encoding)
        format_text = (
            f"{label} / BOM {'あり' if self._doc.bom else 'なし'} / "
            f"{docmod.NEWLINE_LABELS.get(self._doc.newline, '')}")
        if self._doc.lossy_decode:
            format_text += " / 一部読み替えあり"
        self._outline_pane.set_file_info(
            path=self._doc.path, format_text=format_text,
            size_text=self._file_size_text())
        self._refresh_pane_counts()

    def _refresh_pane_counts(self):
        document = self._edit.document()
        # 文字数は characterCount で数える(toPlainText の全文コピーは
        # 大きい文書で打鍵のたびに重い。characterCount は末尾の段落
        # 区切りを1つ余分に数えるので、その分を引く)。
        self._outline_pane.set_counts(
            document.blockCount(), max(0, document.characterCount() - 1))

    # ---------------------------------------------------------------
    # アウトライン・強調の正規表現の設定
    # ---------------------------------------------------------------
    def show_pattern_settings(self):
        try:
            dialog = PatternSettingsDialog(
                self._outline_pattern_text, self._highlight_pattern_text,
                self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                outline_text, highlight_text = dialog.values()
                self._set_patterns(outline_text, highlight_text)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"設定を開けませんでした ({e})")

    def _set_patterns(self, outline_text, highlight_text):
        """正規表現を差し替え、開いている窓へ即反映して保存する。

        適用の前に「適用中」の印を立てる。適用の途中で固まれば印が
        残り、次回の起動で既定に戻る(起動時の保険)。"""
        self._arm_pattern_guard()
        self._outline_pattern_text = outline_text
        self._highlight_pattern_text = highlight_text
        self._outline_rx = outline.compile_pattern(outline_text)
        self._highlighter.set_pattern(
            outline.compile_pattern(highlight_text))
        self._rebuild_outline()
        # 既定と同じ式は、文字列ではなく空で保存する(空 = 組み込み
        # 既定を使う、という決め)。こうしておくと、組み込み既定を
        # あとで良くしたときに、保存済みの窓も追随する。
        updates = {
            "outline_pattern": (
                "" if outline_text == outline.DEFAULT_OUTLINE_PATTERN
                else outline_text),
            "highlight_pattern": (
                "" if highlight_text == outline.DEFAULT_HIGHLIGHT_PATTERN
                else highlight_text),
        }
        if self._pattern_guard_armed:
            # 適用が済んだので、印も同じ書き込みで消す。
            updates[PATTERN_APPLYING_KEY] = False
            self._pattern_guard_armed = False
        self._persist_settings(updates)

    # ---------------------------------------------------------------
    # 表示の更新
    # ---------------------------------------------------------------
    def _refresh_title(self):
        dirty = self._edit.document().isModified()
        name = self._doc.display_name
        self.setWindowTitle(f"{name}{' *' if dirty else ''} - {APP_NAME}")

    def _refresh_status_labels(self):
        label = text_encoding.ENCODING_LABELS.get(self._doc.encoding,
                                                  self._doc.encoding)
        text = f"{label} / {docmod.NEWLINE_LABELS.get(self._doc.newline, '')}"
        if self._doc.lossy_decode:
            text += " / 一部読み替えあり"
        self._enc_label.setText(text)

    def _refresh_position(self):
        cursor = self._edit.textCursor()
        line = cursor.blockNumber() + 1
        column = cursor.positionInBlock() + 1
        self._pos_label.setText(f"{line} 行 {column} 桁")
        self._outline_pane.set_cursor_position(line, column)

    def _refresh_all(self):
        self._refresh_title()
        self._refresh_status_labels()
        self._refresh_position()
        self._refresh_pane_info()
        # ここで同期的に作り直すので、編集の追随で予約された分は
        # 要らない(二重に走らせない)。
        self._outline_timer.stop()
        self._rebuild_outline()

    # ---------------------------------------------------------------
    # 開く・新規
    # ---------------------------------------------------------------
    def open_path(self, path) -> bool:
        """ファイルを開く。失敗したら理由をステータスに出して False。

        未保存の編集があるときは、先に確認する(黙って捨てない)。
        __init__ からの初回は未編集なので、確認はそのまま通る。"""
        if not self._confirm_discard():
            return False
        try:
            doc = docmod.load_document(path)
        except OSError as e:
            self.statusBar().showMessage(f"開けませんでした: {path} ({e})")
            return False
        self._doc = doc
        self._suppress_change_record = True
        try:
            self._edit.setPlainText(doc.text)
        finally:
            self._suppress_change_record = False
        self._edit.document().setModified(False)
        self._refresh_all()
        if doc.lossy_decode:
            self.statusBar().showMessage(
                "一部の文字を読み替えました（保存すると置換文字で書かれます）")
        return True

    def open_dialog(self):
        try:
            start = os.path.dirname(self._doc.path) if self._doc.path else ""
            path, _ = QFileDialog.getOpenFileName(
                self, "開く", start, "すべてのファイル (*)")
            if path:
                self.open_path(path)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"開けませんでした ({e})")

    def new_document(self):
        if not self._confirm_discard():
            return
        self._doc = docmod.TextDocument()
        self._suppress_change_record = True
        try:
            self._edit.setPlainText("")
        finally:
            self._suppress_change_record = False
        self._edit.document().setModified(False)
        self._refresh_all()

    # ---------------------------------------------------------------
    # 保存
    # ---------------------------------------------------------------
    def _sync_text(self):
        self._doc.text = self._edit.toPlainText()

    def save(self) -> bool:
        """上書き保存。保存先が無ければ「名前を付けて保存」に回す。"""
        if self._doc.path is None:
            return self.save_as()
        return self._write()

    def save_to(self, path, encoding=None, newline=None, bom=None) -> bool:
        """ダイアログ無しで、指定の形式で書く(呼び側・テスト用)。"""
        self._sync_text()
        try:
            docmod.save_document(self._doc, path=path, encoding=encoding,
                                 newline=newline, bom=bom)
        except (OSError, UnicodeEncodeError, ValueError) as e:
            self.statusBar().showMessage(f"保存できませんでした: {e}")
            return False
        self._edit.document().setModified(False)
        self._refresh_all()
        self.statusBar().showMessage("保存しました", 4000)
        return True

    def _write(self) -> bool:
        return self.save_to(self._doc.path)

    def save_as(self) -> bool:
        try:
            start = self._doc.path or ""
            path, _ = QFileDialog.getSaveFileName(
                self, "名前を付けて保存", start, "すべてのファイル (*)")
            if not path:
                return False
            self._sync_text()
            dialog = SaveOptionsDialog(self._doc, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return False
            encoding, newline, bom = dialog.values()
            return self.save_to(path, encoding=encoding, newline=newline,
                                bom=bom)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"保存できませんでした ({e})")
            return False

    # ---------------------------------------------------------------
    # 閉じる前の確認
    # ---------------------------------------------------------------
    def _confirm_discard(self) -> bool:
        """未保存の変更を捨ててよいか。進めてよければ True。"""
        if not self._edit.document().isModified():
            return True
        if self.ask_save_changes is not None:
            answer = self.ask_save_changes()
        else:
            answer = self._ask_save_changes_dialog()
        if answer == "save":
            return self.save()
        return answer == "discard"

    def _ask_save_changes_dialog(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setText(f"「{self._doc.display_name}」への変更を保存しますか？")
        save_button = box.addButton(
            "保存", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("保存しない", QMessageBox.ButtonRole.DestructiveRole)
        cancel_button = box.addButton(
            "キャンセル", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_button:
            return "save"
        if clicked is cancel_button:
            return "cancel"
        return "discard"

    def closeEvent(self, event):  # noqa: N802  Qt の上書き
        if not self._confirm_discard():
            event.ignore()
            return
        # 遅延保存のたまりは、閉じる前に書き切る。
        self._flush_pending_font_size()
        g = self.geometry()
        self.closed.emit([g.x(), g.y(), g.width(), g.height()])
        super().closeEvent(event)

    # ---------------------------------------------------------------
    # 編集(標準に無いもの)
    # ---------------------------------------------------------------
    def cut_to_end_of_line(self):
        """行末まで切り取り(命令 cut_to_end、既定は Ctrl+K)。

        選択範囲があれば通常の切り取り。無ければ、カーソル位置から
        行末まで(改行そのものは含めない)を切り取ってクリップボードへ
        入れる。行末にカーソルがあるときは何もしない。削除は1回の
        undo で戻せる単位にする。"""
        cursor = self._edit.textCursor()
        if cursor.hasSelection():
            self._edit.cut()
            return
        if cursor.atBlockEnd():
            return
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                            QTextCursor.MoveMode.KeepAnchor)
        text = cursor.selectedText()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        cursor.beginEditBlock()
        cursor.removeSelectedText()
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)

    # ---------------------------------------------------------------
    # 命令層(キー割り当てとマクロの再生が同じ命令を呼ぶ)
    # ---------------------------------------------------------------
    def execute_command(self, name, args=None, _note=True) -> bool:
        """名前つき命令を1つ実行する。

        実行できたら True。実行した命令は記録係(MacroRecorder)へ
        渡る(記録中なら列に残り、F4 の「直前の命令」にもなる)。
        再生の最中は _note=False で呼ばれ、記録には混ざらない。
        命令の実体(_cmd_*)は「記録に残す引数」を返し、None なら
        その回は記録しない(空の検索など)。"""
        impl = self._command_impls.get(name)
        if impl is None:
            return False
        self._in_command = True
        try:
            effective = impl(args or {})
        finally:
            self._in_command = False
            self._track_change_cache()
        if _note and effective is not None:
            self._recorder.note_command(name, effective)
        return True

    def repeat_last(self):
        """F4: 直前の命令をもう一度実行する(命令が無ければ何もしない)。"""
        last = self._recorder.last_command
        if last is None:
            return
        name, args = last
        self.execute_command(name, dict(args))

    def _move_cursor(self, operation, args):
        count = args.get("count", 1)
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        extend = bool(args.get("extend"))
        mode = (QTextCursor.MoveMode.KeepAnchor if extend
                else QTextCursor.MoveMode.MoveAnchor)
        cursor = self._edit.textCursor()
        for _ in range(count):
            cursor.movePosition(operation, mode)
        self._edit.setTextCursor(cursor)
        return {"count": count, "extend": extend}

    def _cmd_move_line_start(self, args):
        return self._move_cursor(
            QTextCursor.MoveOperation.StartOfBlock, args)

    def _cmd_move_line_end(self, args):
        return self._move_cursor(
            QTextCursor.MoveOperation.EndOfBlock, args)

    def _cmd_move_line_up(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Up, args)

    def _cmd_move_line_down(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Down, args)

    def _cmd_move_char_left(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Left, args)

    def _cmd_move_char_right(self, args):
        return self._move_cursor(QTextCursor.MoveOperation.Right, args)

    def _cmd_insert_text(self, args):
        text = str(args.get("text") or "")
        if not text:
            return None
        cursor = self._edit.textCursor()
        cursor.insertText(text)
        self._edit.setTextCursor(cursor)
        return {"text": text}

    def _cmd_delete_backward(self, args):
        return self._delete_chars(args, forward=False)

    def _cmd_delete_forward(self, args):
        return self._delete_chars(args, forward=True)

    def _delete_chars(self, args, forward):
        count = args.get("count", 1)
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        cursor = self._edit.textCursor()
        for _ in range(count):
            if forward:
                cursor.deleteChar()
            else:
                cursor.deletePreviousChar()
        self._edit.setTextCursor(cursor)
        return {"count": count}

    def _cmd_delete_selection(self, args):
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return None
        cursor.removeSelectedText()
        self._edit.setTextCursor(cursor)
        return {}

    def _cmd_cut_to_end(self, args):
        self.cut_to_end_of_line()
        return {}

    def _cmd_delete_line(self, args):
        cursor = self._edit.textCursor()
        block = cursor.block()
        pos = block.position()
        text_len = len(block.text())
        if block.next().isValid():
            start, end = pos, pos + text_len + 1       # 改行ごと消す
        elif pos > 0:
            start, end = pos - 1, pos + text_len       # 前の改行ごと
        else:
            start, end = pos, pos + text_len           # 1行だけの文書
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self._edit.setTextCursor(cursor)
        return {}

    # ---------------------------------------------------------------
    # 行の並べ替え・重複行の削除・範囲の変換
    # ---------------------------------------------------------------
    def _target_line_range(self):
        """並べ替え・重複削除の対象の (開始ブロック, 終了ブロック)。

        複数行にまたがる選択があれば、その行の範囲(選択の終わりが
        行頭ちょうどのときは、その行は含めない)。無ければ文書全体。
        どちらの場合も、文書の最後のブロックが空(末尾の改行の後ろ)
        なら対象から外す。含めると並べ替えで末尾の改行が消え、
        空行が先頭へ来てしまう。"""
        document = self._edit.document()
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            first, last = 0, document.blockCount() - 1
        else:
            start_block = document.findBlock(cursor.selectionStart())
            end_block = document.findBlock(cursor.selectionEnd())
            first = start_block.blockNumber()
            last = end_block.blockNumber()
            if last > first and cursor.selectionEnd() == end_block.position():
                last -= 1
        if (last > first and last == document.blockCount() - 1
                and not document.findBlockByNumber(last).text()):
            last -= 1
        return first, last

    def _replace_line_range(self, first, last, new_lines):
        """first〜last ブロックの本文を new_lines で置き換える。
        1回の undo で戻せる単位にし、カーソルは範囲の頭へ置く。"""
        document = self._edit.document()
        start_block = document.findBlockByNumber(first)
        end_block = document.findBlockByNumber(last)
        start = start_block.position()
        end = end_block.position() + len(end_block.text())
        cursor = self._edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText("\n".join(new_lines))
        cursor.endEditBlock()
        cursor.setPosition(start)
        self._edit.setTextCursor(cursor)

    def _cmd_sort_lines(self, args):
        order = args.get("order")
        order = "desc" if order == "desc" else "asc"
        first, last = self._target_line_range()
        if last - first < 1:
            return None  # 1行以下では並べ替える意味が無い
        document = self._edit.document()
        lines = [document.findBlockByNumber(n).text()
                 for n in range(first, last + 1)]
        new_lines = sorted(lines, reverse=(order == "desc"))
        if new_lines == lines:
            return None  # 変わらないなら記録も undo も作らない
        self._replace_line_range(first, last, new_lines)
        return {"order": order}

    def _cmd_remove_duplicate_lines(self, args):
        first, last = self._target_line_range()
        document = self._edit.document()
        lines = [document.findBlockByNumber(n).text()
                 for n in range(first, last + 1)]
        seen = set()
        new_lines = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                new_lines.append(line)
        if len(new_lines) == len(lines):
            return None  # 重複が無ければ何もしない
        self._replace_line_range(first, last, new_lines)
        return {}

    def _convert_selection(self, func):
        """選択範囲の文字列を func で変換して置き換える。選択が無い、
        または変換で変わらなければ None(記録しない)。"""
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            return None
        # 段落の区切りは U+2029 で返るので、変換の前に \n に戻す。
        text = cursor.selectedText().replace("\u2029", "\n")
        new_text = func(text)
        if new_text == text:
            return None
        cursor.beginEditBlock()
        cursor.insertText(new_text)
        cursor.endEditBlock()
        self._edit.setTextCursor(cursor)
        return {}

    def _cmd_convert_halfwidth(self, args):
        return self._convert_selection(convmod.to_halfwidth)

    def _cmd_convert_fullwidth(self, args):
        return self._convert_selection(convmod.to_fullwidth)

    def _cmd_convert_lower(self, args):
        return self._convert_selection(convmod.to_lower)

    def _cmd_convert_upper(self, args):
        return self._convert_selection(convmod.to_upper)

    def _cmd_find_next(self, args):
        return self._find_command(args, forward=True)

    def _cmd_find_prev(self, args):
        return self._find_command(args, forward=False)

    def _find_command(self, args, forward):
        word = args.get("text")
        if word is None:
            word = self._find_edit.text()
        else:
            word = str(word)
        if not word:
            return None
        case = args.get("case")
        case = self._find_case.isChecked() if case is None else bool(case)
        self._do_find(forward, word, case)
        return {"text": word, "case": case}

    # ---------------------------------------------------------------
    # マクロの記録(文書の変化と移動キーの切り分け)
    # ---------------------------------------------------------------
    def _track_change_cache(self):
        cursor = self._edit.textCursor()
        self._change_cache = (cursor.selectionStart(),
                              cursor.selectionEnd(), cursor.position())

    def _on_move_key(self, name, extend):
        # 移動は本文が標準で済ませている。ここでは「起きたこと」と
        # して記録係へ渡すだけ(実行はしない)。
        self._recorder.note_command(name, {"count": 1, "extend": bool(extend)})

    def _note_macro_skip(self):
        """記録できない操作が記録中に起きたら、数えて知らせる。
        黙って記録から外すと、再生結果が実物と違うことに利用者が
        気づけないため。"""
        if not self._recorder.recording:
            return
        self._macro_skips += 1
        self.statusBar().showMessage(
            f"● マクロ記録中（記録できない操作 {self._macro_skips} 件）", 0)

    def _on_document_change_for_macro(self, pos, removed, added):
        try:
            if not (self._in_command or self._replaying
                    or self._suppress_change_record):
                doc = self._edit.document()
                sel_start, sel_end, cursor_before = self._change_cache
                if (pos == 0 and added > 0
                        and added >= doc.characterCount() - 1
                        and (removed > 0 or added > 1)):
                    # 文書の丸ごと差し替え(ファイルの読み込み等)は
                    # 編集の記録にしない。ただし記録中に起きたら
                    # (全選択して打ち直す等)、知らせる対象にする。
                    # 条件の最後の節は、空の文書への最初の打鍵
                    # (removed=0, added=1)を差し替えと間違えないため。
                    self._recorder.break_insert_chain()
                    self._note_macro_skip()
                elif (added == 0 and removed == 1 and pos == 0
                        and sel_start == sel_end and cursor_before == 0
                        and doc.characterCount() <= 1):
                    # 差し替えの前半で出る「空にする削除」も記録
                    # しない(実体は末尾の区切り文字だけ)。代償として、
                    # 1文字だけの文書での Del は記録に残らない。
                    self._recorder.break_insert_chain()
                    self._note_macro_skip()
                else:
                    inserted = ""
                    if added > 0:
                        # pos / added は UTF-16 の単位。Python の文字列
                        # の位置とは絵文字(サロゲートペア)でずれるので、
                        # 挿入された文字列はカーソルで取る。
                        cursor = QTextCursor(doc)
                        cursor.setPosition(pos)
                        cursor.setPosition(
                            pos + added, QTextCursor.MoveMode.KeepAnchor)
                        inserted = cursor.selectedText().replace(
                            "\u2029", "\n")
                    commands = self._recorder.commands
                    before_len = len(commands)
                    before_last = None
                    if commands:
                        last = commands[-1]
                        before_last = (last["cmd"],
                                       dict(last.get("args") or {}))
                    self._recorder.feed_change(
                        pos, removed, added, inserted,
                        sel_start, sel_end, cursor_before)
                    recorded = len(commands) > before_len
                    if not recorded and before_last is not None and commands:
                        last = commands[-1]
                        recorded = (
                            (last["cmd"], dict(last.get("args") or {}))
                            != before_last)
                    if not recorded:
                        # 選択範囲を打って上書きする入力、矩形選択での
                        # 編集など、命令に切り分けられなかった変化。
                        # 黙って外さず、記録中なら知らせる。
                        self._note_macro_skip()
        except Exception:  # noqa: BLE001  記録の失敗で編集を止めない
            pass
        finally:
            self._track_change_cache()

    def _undo_edit(self):
        self._suppress_change_record = True
        try:
            self._edit.undo()
        finally:
            self._suppress_change_record = False
            self._track_change_cache()

    def _redo_edit(self):
        self._suppress_change_record = True
        try:
            self._edit.redo()
        finally:
            self._suppress_change_record = False
            self._track_change_cache()

    # ---------------------------------------------------------------
    # マクロ(記録・再生・保存)
    # ---------------------------------------------------------------
    @property
    def macro_recording(self) -> bool:
        return self._recorder.recording

    def recorded_commands(self) -> list:
        """記録中の命令の列(写し)。"""
        return [dict(entry, args=dict(entry.get("args") or {}))
                for entry in self._recorder.commands]

    def set_macro_recording(self, on):
        on = bool(on)
        if on == self._recorder.recording:
            return
        if on:
            self._recorder.start()
            self._macro_skips = 0
            self._act_macro_record.setText("記録終了(&R)")
            self.statusBar().showMessage("● マクロ記録中", 0)
        else:
            self._recorder.stop()
            self._current_macro = self.recorded_commands()
            self._current_macro_name = ""
            self._act_macro_record.setText("記録開始(&R)")
            message = (f"マクロの記録を終了しました"
                       f"（{len(self._current_macro)} 手）")
            if self._macro_skips:
                message += (f"。記録できない操作が {self._macro_skips} 件"
                            "あり、再生結果が実物と違う場合があります")
            self.statusBar().showMessage(message, 8000)
        action = getattr(self, "_act_macro_record", None)
        if action is not None and action.isChecked() != on:
            blocker = QSignalBlocker(action)
            action.setChecked(on)

    def play_macro(self, commands=None) -> bool:
        """マクロを再生する。commands が無ければ、最後に記録・読み
        込みしたマクロ。再生全体は1回の undo で戻せる単位にする。"""
        if commands is None:
            commands = self._current_macro
        if not commands:
            self.statusBar().showMessage("再生するマクロがありません", 4000)
            return False
        self._replaying = True
        cursor = self._edit.textCursor()
        cursor.beginEditBlock()
        try:
            for entry in commands:
                self.execute_command(entry["cmd"],
                                     dict(entry.get("args") or {}),
                                     _note=False)
        finally:
            cursor.endEditBlock()
            self._replaying = False
            self._track_change_cache()
        return True

    def save_macro(self, path, name="") -> bool:
        """いまのマクロ(記録中なら記録中の列)を JSON で書く。"""
        commands = (self.recorded_commands() if self._recorder.recording
                    else self._current_macro)
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(macmod.macro_to_json(commands, name))
        except OSError as e:
            self.statusBar().showMessage(f"マクロを保存できませんでした: {e}")
            return False
        return True

    def load_macro(self, path) -> list:
        """マクロの JSON を読み、いまのマクロにする。命令の列を返す。
        読めなければ ValueError / OSError を上げる(呼び側が伝える)。"""
        with open(path, encoding="utf-8") as f:
            name, commands = macmod.macro_from_json(f.read())
        self._current_macro = commands
        self._current_macro_name = name
        return commands

    def save_macro_dialog(self):
        try:
            path, _ = QFileDialog.getSaveFileName(
                self, "マクロを保存", "", "マクロ (*.json);;すべてのファイル (*)")
            if not path:
                return
            name = os.path.splitext(os.path.basename(path))[0]
            if self.save_macro(path, name):
                self.statusBar().showMessage("マクロを保存しました", 4000)
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self.statusBar().showMessage(f"マクロを保存できませんでした ({e})")

    def load_macro_dialog(self):
        try:
            path, _ = QFileDialog.getOpenFileName(
                self, "マクロを開く", "", "マクロ (*.json);;すべてのファイル (*)")
            if not path:
                return
            commands = self.load_macro(path)
        except Exception as e:  # noqa: BLE001  理由は利用者への文にする
            self.statusBar().showMessage(f"マクロを読めませんでした: {e}")
            return
        self.play_macro(commands)

    # ---------------------------------------------------------------
    # 検索
    # ---------------------------------------------------------------
    def show_find_bar(self):
        self._find_bar.show()
        self._find_edit.setFocus()
        self._find_edit.selectAll()

    def _hide_find_bar(self):
        self._find_bar.hide()
        self._edit.setFocus()

    def _do_find(self, forward: bool, word=None, case_sensitive=None) -> bool:
        """検索の実体。word / case_sensitive が None なら検索欄の値を使う
        (命令層からは、記録・再生のために使った値を渡してくる)。"""
        if word is None:
            word = self._find_edit.text()
        if not word:
            return False
        if case_sensitive is None:
            case_sensitive = self._find_case.isChecked()
        text = self._edit.toPlainText()
        cursor = self._edit.textCursor()
        if forward:
            start = (cursor.selectionEnd() if cursor.hasSelection()
                     else cursor.position())
            index = docmod.find_next(text, word, start, case_sensitive)
            if index < 0:
                index = docmod.find_next(text, word, 0, case_sensitive)
        else:
            start = (cursor.selectionStart() if cursor.hasSelection()
                     else cursor.position())
            index = docmod.find_prev(text, word, start, case_sensitive)
            if index < 0:
                index = docmod.find_prev(text, word, len(text), case_sensitive)
        if index < 0:
            self.statusBar().showMessage(f"「{word}」は見つかりません", 4000)
            return False
        cursor.setPosition(index)
        cursor.setPosition(index + len(word), QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.centerCursor()
        return True

    # ---------------------------------------------------------------
    # Copilot へ送る
    # ---------------------------------------------------------------
    def send_selection(self):
        cursor = self._edit.textCursor()
        if not cursor.hasSelection():
            self.statusBar().showMessage("選択範囲がありません")
            return
        if self._send_thread is not None:
            self.statusBar().showMessage("Copilot へ送信中です")
            return
        # QPlainTextEdit の選択文字列は段落の区切りが U+2029 になる。
        text = cursor.selectedText().replace("\u2029", "\n")
        self._start_send(text, force=False)

    def _start_send(self, text, force):
        """送信を裏のスレッドで始める。sender の呼び方は
        sender(text) が通常、sender(text, True) が「入力欄の上書きを
        承知で送る」再送(copilot_io.send_text の force 引数)。"""
        self._pending_send_text = text
        self.statusBar().showMessage("Copilot へ送信しています…")

        def run():
            try:
                if force:
                    result = self._sender(text, True)
                else:
                    result = self._sender(text)
            except Exception as e:  # noqa: BLE001  理由は利用者への文にする
                result = copilot_io.SendResult(
                    False, f"Copilot に送れませんでした（{e}）")
            self._bridge.finished.emit(result)

        self._send_thread = threading.Thread(
            target=run, daemon=True, name="editor-copilot-send")
        self._send_thread.start()

    def _on_send_finished(self, result):
        self._send_thread = None
        if getattr(result, "needs_confirm", False):
            # Copilot の入力欄に書きかけの文がある。そのまま送ると
            # 上書きされて消えるので、利用者に確かめてから送り直す。
            if self._confirm_overwrite_input():
                text = self._pending_send_text
                if text:
                    self._start_send(text, force=True)
                    return
            self.statusBar().showMessage(
                "送信をやめました（入力欄の文はそのままです）", 8000)
            return
        self.statusBar().showMessage(result.message, 0 if not result.ok else 8000)

    def _confirm_overwrite_input(self) -> bool:
        """Copilot の入力欄の書きかけを上書きして送ってよいか。"""
        if self.ask_overwrite_input is not None:
            return bool(self.ask_overwrite_input())
        answer = QMessageBox.question(
            self, APP_NAME,
            "Copilot の入力欄に書きかけの文があります。\n"
            "そのまま送ると、選択している範囲で上書きされて、"
            "書きかけの文は消えます。送りますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    # ---------------------------------------------------------------
    # テスト・呼び側からの参照
    # ---------------------------------------------------------------
    def is_dirty(self) -> bool:
        """未保存の変更があるか。再起動・終了の巻き添え判定
        (feature_screen.restart_blockers、filer.confirm_losses)から呼ばれる。"""
        return self._edit.document().isModified()

    def current_document(self) -> docmod.TextDocument:
        self._sync_text()
        return self._doc

    def text(self) -> str:
        return self._edit.toPlainText()
