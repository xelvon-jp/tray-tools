# editor/greplace_dialog.py
# フォルダ内の横断置換(grep して置換)の窓。実体は
# editor/greplace.py の純粋関数で、ここは入力・プレビュー・
# 実行の確認と結果の表示だけ。
#
# 流れ(安全側の決め):
#   1. 「対象を確認」で、対象ファイルと置換件数の一覧(プレビュー)
#      を出す。まだ何も書かない
#   2. 「置換を実行」はプレビューがあるときだけ押せる。条件(フォルダ・
#      検索・置換・チェック)を変えたらプレビューは古くなるので、
#      実行は押せなくなる(確かめた内容だけを実行するため)
#   3. 実行の前に確認を出す。実行が終わったら、結果のまとめを
#      残したままプレビューを自動で確認し直し、一覧を新しい
#      状態にそろえる
# プレビューも実行も裏のスレッドで走らせ、進捗と結果は Signal
# 経由で窓に戻す(grep の窓と同じ作法)。
#
# EDDy で開いているファイルは対象から外す。開いているパスの
# 取得は open_paths(呼び側が渡す関数)に任せ、プレビューの開始時と
# 実行の開始時にそれぞれ取り直す(途中で開かれたファイルも
# 実行では外れる)。
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from editor import editor_icon
from editor import grep as grepmod
from editor import greplace as replacemod
from editor import rxfind


class _ReplaceBridge(QObject):
    """ワーカースレッド → 窓への受け渡し。"""

    progress = Signal(int, str)
    preview_finished = Signal(object)
    execute_finished = Signal(object)


class GrepReplaceDialog(QDialog):
    """フォルダ内のファイルを横断で置換する窓。

    open_paths は「いま EDDy で開いているファイルのパス」を返す
    関数(対象から外すため)。on_jump は (パス, 行番号) を受ける
    関数で、プレビューの項目を選んだときに呼ばれ、窓は閉じる。"""

    def __init__(self, parent=None, *, folder="", word="", repl="",
                 case_sensitive=False, regex=False, open_paths=None,
                 on_jump=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。ここは循環
        # import を避けるため文字列で持つ(grep の窓と同じ)。
        self.setWindowTitle("フォルダ内を検索して置換 - EDDy")
        self.resize(720, 520)
        self._open_paths = open_paths
        self._on_jump = on_jump
        self._bridge = _ReplaceBridge(self)
        self._bridge.progress.connect(self._on_progress)
        self._bridge.preview_finished.connect(self._on_preview_finished)
        self._bridge.execute_finished.connect(self._on_execute_finished)
        self._thread = None
        self._busy = ""            # "" / "preview" / "execute"
        self._preview = None       # 表示中の一覧が済んだプレビュー
        self._preview_key = None   # そのプレビューの条件
        self._pending_note = ""    # 確認し直しの状態文の頭に残す実行結果

        layout = QVBoxLayout(self)
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("フォルダ"))
        self._folder_edit = QLineEdit(folder)
        folder_row.addWidget(self._folder_edit, 1)
        browse = QPushButton("参照…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)

        word_row = QHBoxLayout()
        word_row.addWidget(QLabel("検索"))
        self._word_edit = QLineEdit(word)
        self._word_edit.returnPressed.connect(self.start_preview)
        word_row.addWidget(self._word_edit, 1)
        self._case_check = QCheckBox("大文字小文字を区別")
        self._case_check.setChecked(bool(case_sensitive))
        word_row.addWidget(self._case_check)
        self._regex_check = QCheckBox("正規表現")
        self._regex_check.setChecked(bool(regex))
        word_row.addWidget(self._regex_check)
        layout.addLayout(word_row)

        repl_row = QHBoxLayout()
        repl_row.addWidget(QLabel("置換"))
        self._repl_edit = QLineEdit(repl)
        self._repl_edit.returnPressed.connect(self.start_preview)
        repl_row.addWidget(self._repl_edit, 1)
        layout.addLayout(repl_row)

        button_row = QHBoxLayout()
        self._preview_button = QPushButton("対象を確認")
        self._preview_button.clicked.connect(self.start_preview)
        button_row.addWidget(self._preview_button)
        self._execute_button = QPushButton("置換を実行")
        self._execute_button.setEnabled(False)
        self._execute_button.clicked.connect(self.start_execute)
        button_row.addWidget(self._execute_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._status_label = QLabel(
            f"対象はテキストファイル（{grepmod.MAX_FILE_BYTES // (1024 * 1024)} MB まで）。"
            "まず「対象を確認」で、置換するファイルと件数を確かめてください")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._results = QListWidget()
        self._results.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self._results, 1)

        # 条件が変わったら、表示中の一覧は古くなる(実行を止める)。
        self._folder_edit.textChanged.connect(self._invalidate_preview)
        self._word_edit.textChanged.connect(self._invalidate_preview)
        self._repl_edit.textChanged.connect(self._invalidate_preview)
        self._case_check.toggled.connect(self._invalidate_preview)
        self._regex_check.toggled.connect(self._invalidate_preview)

    # ---------------------------------------------------------------
    def _browse_folder(self):
        start = self._folder_edit.text() or ""
        folder = QFileDialog.getExistingDirectory(self, "置換するフォルダ", start)
        if folder:
            self._folder_edit.setText(folder)

    def _conditions(self):
        return (self._folder_edit.text().strip(), self._word_edit.text(),
                self._repl_edit.text(), self._case_check.isChecked(),
                self._regex_check.isChecked())

    def _current_excludes(self):
        if self._open_paths is None:
            return ()
        try:
            return tuple(self._open_paths())
        except Exception:  # noqa: BLE001  取得の失敗で窓を落とさない
            return ()

    def _invalidate_preview(self):
        if self._preview is None or self._busy:
            return
        self._preview = None
        self._preview_key = None
        self._execute_button.setEnabled(False)
        self._status_label.setText(
            "条件が変わりました。「対象を確認」で確かめ直してください")

    def _set_busy(self, kind):
        self._busy = kind
        busy = bool(kind)
        self._preview_button.setEnabled(not busy)
        self._execute_button.setEnabled(
            not busy and self._preview is not None)

    # ---------------------------------------------------------------
    # プレビュー(対象の確認)
    # ---------------------------------------------------------------
    def start_preview(self):
        if self._thread is not None:
            return
        folder, word, repl, case, regex = self._conditions()
        if not folder or not os.path.isdir(folder):
            self._status_label.setText("フォルダを選んでください")
            return
        if not word:
            self._status_label.setText("検索する文字列を入れてください")
            return
        self._results.clear()
        self._preview = None
        self._preview_key = None
        self._set_busy("preview")
        self._status_label.setText("対象を確認しています…")
        excludes = self._current_excludes()

        def run():
            try:
                payload = replacemod.preview_replace(
                    folder, word, repl, case_sensitive=case, regex=regex,
                    exclude_paths=excludes,
                    progress=lambda n, path: self._bridge.progress.emit(n, path))
            except rxfind.RegexFault as e:
                payload = e
            self._bridge.preview_finished.emit(payload)

        self._thread = threading.Thread(
            target=run, daemon=True, name="editor-grep-replace-preview")
        self._thread.start()

    def _on_progress(self, count, path):
        verb = "置換しています" if self._busy == "execute" else "確認しています"
        self._status_label.setText(
            f"{verb}…（{count} ファイル目: {os.path.basename(path)}）")

    def _on_preview_finished(self, payload):
        self._thread = None
        self._set_busy("")
        if isinstance(payload, rxfind.RegexFault):
            self._status_label.setText(str(payload))
            return
        self._preview = payload
        self._preview_key = self._conditions()
        self._fill_preview_list(payload)
        note, self._pending_note = self._pending_note, ""
        text = self._preview_summary(payload)
        self._status_label.setText(f"{note}{text}" if note else text)
        self._set_busy("")

    def _fill_preview_list(self, preview):
        self._results.clear()
        for target in preview.targets:
            item = QListWidgetItem(
                f"{os.path.basename(target.path)}  — {target.count} 件"
                f"（{target.first_line} 行目から）")
            item.setData(Qt.ItemDataRole.UserRole,
                         (target.path, target.first_line))
            item.setToolTip(target.path)
            self._results.addItem(item)

    @staticmethod
    def _skipped_summary(preview):
        parts = []
        if preview.skipped_open:
            parts.append(f"開いている {len(preview.skipped_open)} 件")
        if preview.skipped_unreadable:
            parts.append(f"読めない {len(preview.skipped_unreadable)} 件")
        if preview.skipped_binary:
            parts.append(f"バイナリらしい {len(preview.skipped_binary)} 件")
        if preview.skipped_unencodable:
            parts.append(
                f"書けない文字がある {len(preview.skipped_unencodable)} 件")
        return parts

    def _preview_summary(self, preview):
        text = (f"対象 {len(preview.targets)} ファイル・"
                f"合計 {preview.total_count} 件"
                f"（{preview.files_scanned} ファイルを確認）")
        if preview.truncated:
            text += (f" — 対象が多いため先頭 {replacemod.MAX_PREVIEW_FILES}"
                     " ファイルで打ち切りました")
        skipped = self._skipped_summary(preview)
        if skipped:
            text += "。対象外: " + "、".join(skipped)
        if not preview.targets and not skipped:
            text = f"見つかりませんでした（{preview.files_scanned} ファイルを確認）"
        return text

    # ---------------------------------------------------------------
    # 実行
    # ---------------------------------------------------------------
    def _confirm_execute(self, preview) -> bool:
        """実行前の確認。テストからは差し替えられる。"""
        folder = self._folder_edit.text().strip()
        answer = QMessageBox.question(
            self, "置換の確認",
            f"「{folder}」の中の {len(preview.targets)} ファイル"
            f"（合計 {preview.total_count} 件）を置換します。\n"
            "置換したファイルには、これまでの内容の控え（.bak）が残ります。\n"
            "よろしいですか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def start_execute(self):
        if self._thread is not None or self._preview is None:
            return
        if self._preview_key != self._conditions():
            self._invalidate_preview()
            return
        preview = self._preview
        if not self._confirm_execute(preview):
            return
        folder, word, repl, case, regex = self._conditions()
        self._set_busy("execute")
        self._status_label.setText("置換しています…")
        excludes = self._current_excludes()

        def run():
            payload = replacemod.execute_replace(
                preview, word, repl, case_sensitive=case, regex=regex,
                exclude_paths=excludes,
                progress=lambda n, path: self._bridge.progress.emit(n, path))
            self._bridge.execute_finished.emit(payload)

        self._thread = threading.Thread(
            target=run, daemon=True, name="editor-grep-replace-run")
        self._thread.start()

    def _on_execute_finished(self, payload):
        self._thread = None
        result = payload
        self._fill_outcome_list(result)
        self._pending_note = self._execute_summary(result) + " "
        # 一覧を新しい状態にそろえる(対象の確認を自動でやり直す)。
        # _pending_note は次のプレビューの状態文の頭に残る。
        self._preview = None
        self._preview_key = None
        self._set_busy("")
        self.start_preview()

    def _fill_outcome_list(self, result):
        self._results.clear()
        labels = {
            replacemod.STATUS_DONE: "置換しました",
            replacemod.STATUS_UNCHANGED: "置換しました",
            replacemod.STATUS_CHANGED: "飛ばしました",
            replacemod.STATUS_OPEN: "飛ばしました",
            replacemod.STATUS_UNREADABLE: "飛ばしました",
            replacemod.STATUS_BINARY: "飛ばしました",
            replacemod.STATUS_UNENCODABLE: "飛ばしました",
            replacemod.STATUS_FAILED: "失敗しました",
        }
        for outcome in result.outcomes:
            label = labels.get(outcome.status, outcome.status)
            text = f"{os.path.basename(outcome.path)}  — {label}"
            if outcome.status in (replacemod.STATUS_DONE,
                                  replacemod.STATUS_UNCHANGED):
                text += f"（{outcome.count} 件）"
            if outcome.detail:
                text += f": {outcome.detail}"
            item = QListWidgetItem(text)
            item.setToolTip(outcome.path)
            self._results.addItem(item)

    @staticmethod
    def _execute_summary(result):
        text = (f"{result.files_written} ファイル・"
                f"合計 {result.total_replaced} 件を置換しました")
        reasons = []
        changed = result.by_status(replacemod.STATUS_CHANGED)
        if changed:
            reasons.append(f"変わっていた {len(changed)} 件")
        opened = result.by_status(replacemod.STATUS_OPEN)
        if opened:
            reasons.append(f"開いている {len(opened)} 件")
        unencodable = result.by_status(replacemod.STATUS_UNENCODABLE)
        if unencodable:
            reasons.append(f"書けない文字がある {len(unencodable)} 件")
        unreadable = result.by_status(replacemod.STATUS_UNREADABLE)
        if unreadable:
            reasons.append(f"読めない {len(unreadable)} 件")
        binary = result.by_status(replacemod.STATUS_BINARY)
        if binary:
            reasons.append(f"バイナリらしい {len(binary)} 件")
        failed = result.by_status(replacemod.STATUS_FAILED)
        if failed:
            reasons.append(f"失敗 {len(failed)} 件")
        if reasons:
            text += "。飛ばしたもの: " + "、".join(reasons)
        return text

    # ---------------------------------------------------------------
    def _on_item_activated(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if data and self._on_jump is not None:
            path, line = data
            self._on_jump(path, line)
            self.accept()
