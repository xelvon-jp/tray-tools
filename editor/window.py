# editor/window.py
# テキストエディタの窓。土台は QPlainTextEdit、独立した QMainWindow。
#
# 第1段の範囲(仕様 docs/work-tools/spec.md 案B):
#   開く / 新規 / 上書き保存 / 名前を付けて保存(文字コードと改行は
#   開いたときのものを守り、変換は利用者が選んだときだけ)、
#   検索(次へ・前へ)、選択範囲を Copilot に投げる(copilot_io 経由)。
# マクロは第2段なので、ここには無い。
#
# 落ちないための作法(このリポジトリの決まり):
#   - 外と触る入口(ファイル読み書き・送信)は例外を利用者への文にする。
#     スロットで投げ切るとプロセスごと終了する
#   - Copilot への送信は別スレッドで走らせ、結果は Signal 経由で窓に戻す
#     (ワーカースレッドから Qt を直接触らない。_AgentLoopBridge と同じ)。
#     UIA の実体は copilot_io の裏方プロセスが持つ(常駐の中で UIA を
#     動かすと pycaw と同居して即死するため。copilot_io 側の作法)
import os
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut, QTextCursor
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
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import copilot_io
import text_encoding
from editor import document as docmod
from editor.keymap import merge_keymap

APP_NAME = "テキストエディタ"
ENCODING_ORDER = ["utf-8", "cp932", "utf-16-le", "utf-16-be", "euc_jp", "iso2022_jp"]


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


class EditorWindow(QMainWindow):
    """テキストエディタの窓。閉じたら closed にジオメトリ([x, y, w, h])が流れる
    (ビュアと同じ形。呼び側が設定へ保存する)。"""

    closed = Signal(object)

    def __init__(self, path=None, *, geometry=None, keymap_overrides=None,
                 sender=None, font=None, parent=None):
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

        self._edit = QPlainTextEdit()
        self._edit.setFont(font if font is not None else QFont("Consolas", 10))
        self._edit.document().modificationChanged.connect(
            lambda _modified: self._refresh_title())
        self._edit.cursorPositionChanged.connect(self._refresh_position)

        self._find_bar = self._build_find_bar()
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._edit)
        layout.addWidget(self._find_bar)
        self.setCentralWidget(central)
        self._find_bar.hide()

        self._build_menus()
        self._enc_label = QLabel()
        self._pos_label = QLabel()
        self.statusBar().addPermanentWidget(self._enc_label)
        self.statusBar().addPermanentWidget(self._pos_label)
        if geometry and len(geometry) == 4:
            self.setGeometry(*geometry)
        else:
            self.resize(760, 560)
        self._refresh_all()
        if path:
            self.open_path(path)

    # ---------------------------------------------------------------
    # 組み立て
    # ---------------------------------------------------------------
    def _action(self, label, slot, keymap_name=None, menu=None):
        action = QAction(label, self)
        if keymap_name:
            keys = self._keymap.get(keymap_name)
            if keys:
                action.setShortcut(QKeySequence(keys))
        action.triggered.connect(slot)
        if menu is not None:
            menu.addAction(action)
        return action

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")
        self._action("新規(&N)", self.new_document, "new", file_menu)
        self._action("開く(&O)…", self.open_dialog, "open", file_menu)
        self._action("上書き保存(&S)", lambda: self.save(), "save", file_menu)
        self._action("名前を付けて保存(&A)…", self.save_as, "save_as", file_menu)
        file_menu.addSeparator()
        self._action("閉じる(&C)", self.close, "close_window", file_menu)

        edit_menu = self.menuBar().addMenu("編集(&E)")
        # 元に戻す等は QPlainTextEdit の標準キー(Ctrl+Z 等)が効く。
        # メニューからは同じ槽を呼ぶだけ。
        self._action("元に戻す(&U)", self._edit.undo, None, edit_menu)
        self._action("やり直し(&R)", self._edit.redo, None, edit_menu)
        edit_menu.addSeparator()
        self._action("切り取り(&T)", self._edit.cut, None, edit_menu)
        self._action("コピー(&C)", self._edit.copy, None, edit_menu)
        self._action("貼り付け(&P)", self._edit.paste, None, edit_menu)
        self._action("すべて選択(&A)", self._edit.selectAll, None, edit_menu)

        search_menu = self.menuBar().addMenu("検索(&S)")
        self._action("検索(&F)…", self.show_find_bar, "find", search_menu)
        self._action("次を検索(&N)", lambda: self._do_find(True),
                     "find_next", search_menu)
        self._action("前を検索(&P)", lambda: self._do_find(False),
                     "find_prev", search_menu)

        copilot_menu = self.menuBar().addMenu("Copilot(&C)")
        self._action("選択範囲を Copilot に投げる(&S)", self.send_selection,
                     "send_selection", copilot_menu)

    def _build_find_bar(self):
        bar = QFrame()
        row = QHBoxLayout(bar)
        row.setContentsMargins(6, 4, 6, 4)
        row.addWidget(QLabel("検索"))
        self._find_edit = QLineEdit()
        self._find_edit.returnPressed.connect(lambda: self._do_find(True))
        row.addWidget(self._find_edit, 1)
        self._find_case = QCheckBox("大文字小文字を区別")
        row.addWidget(self._find_case)
        next_button = QPushButton("次へ")
        next_button.clicked.connect(lambda: self._do_find(True))
        row.addWidget(next_button)
        prev_button = QPushButton("前へ")
        prev_button.clicked.connect(lambda: self._do_find(False))
        row.addWidget(prev_button)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self._hide_find_bar)
        row.addWidget(close_button)
        escape = QShortcut(QKeySequence("Esc"), bar)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._hide_find_bar)
        return bar

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

    def _refresh_all(self):
        self._refresh_title()
        self._refresh_status_labels()
        self._refresh_position()

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
        self._edit.setPlainText(doc.text)
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
        self._edit.setPlainText("")
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
        g = self.geometry()
        self.closed.emit([g.x(), g.y(), g.width(), g.height()])
        super().closeEvent(event)

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

    def _do_find(self, forward: bool) -> bool:
        word = self._find_edit.text()
        if not word:
            return False
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
