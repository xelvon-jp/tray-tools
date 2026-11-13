# editor/macro_dialog.py
# マクロの管理画面。登録したマクロ(editor/macros.py の登録マクロ。
# 設定の editor 節 macros に持つ)を一覧し、実行・名前の変更・削除・
# キー割り当ての変更・テキストでの編集をする窓。
#
# 変えるたびに on_change(登録マクロの対応の写し) が呼ばれ、呼び側
# (窓)がメニューへ反映して設定へ保存する(この窓自体は設定に触ら
# ない。色の設定画面やキー割り当ての画面と同じ作り)。
#
# キー割り当ては登録マクロごとに1つ持てる(登録の "key")。付け方
# の作法はキー割り当ての画面とそろえた:
#   - 別の登録マクロが使っているキーを付けたら、そのマクロの割り
#     当ては外れる(後勝ち。重なったままではどちらも効かないため)
#   - 組み込みの命令が使っているキー(reserved_keys)は付けられ
#     ない(組み込みを優先する。知らせに出す)
# 実際にショートカットとして効かせるのは窓側の仕事で、設定を手で
# 書き換えて重なった場合は、窓側が組み込み優先・先勝ちで付け直す。
# その場合も黙って外れたままにせず、この画面で「効いていない」こと
# と理由(組み込みと重なった／別のマクロと重なった)を選んだマクロに
# ついて表示する(macro_key_state)。キーを付け直せばその場で直る
# (レビュー req-20261114-07 の件B)。
#
# テキストで編集は、editor/macros.py のテキスト形式(1行1命令)を
# 小さな窓で開く。保存のときに解釈し、壊れた行があれば行番号つき
# の理由をその窓に残して閉じない。
#
# テストでは register_macro / rename_macro / delete_macro /
# set_macro_key / macro_text / apply_macro_text を直接呼べる。
from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QInputDialog,
    QKeySequenceEdit,
    QVBoxLayout,
)

from editor import editor_icon
from editor import macros as macmod
from editor.keymap_dialog import normalize_keys


class MacroTextDialog(QDialog):
    """マクロをテキスト形式で編集する小さな窓。

    validator はテキストを受けて、問題があれば理由の文を、無ければ
    None を返す関数。OK のときに確かめ、問題があれば理由を出して
    閉じない。"""

    def __init__(self, parent=None, *, name="", text="", validator=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        self.setWindowTitle(f"マクロのテキスト編集 - {name} - EDDy")
        self.resize(560, 420)
        self._validator = validator
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "1行1命令で書きます。# で始まる行はコメントです。"
            "引数は key=値（文字列は \"…\"）の形で、insert_text は"
            " insert_text \"文字列\" とも書けます。"))
        self._edit = QPlainTextEdit()
        self._edit.setPlainText(text)
        layout.addWidget(self._edit, 1)
        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = QPushButton("キャンセル")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        ok = QPushButton("保存")
        ok.clicked.connect(self._on_ok)
        bottom.addWidget(ok)
        layout.addLayout(bottom)

    def text(self):
        return self._edit.toPlainText()

    def set_error(self, message):
        self._error_label.setText(message or "")

    def _on_ok(self):
        if self._validator is not None:
            error = self._validator(self.text())
            if error:
                self.set_error(error)
                return
        self.accept()


class MacroLibraryDialog(QDialog):
    """マクロの管理画面。

    macros は登録マクロの対応(名前 → {"commands": ..., "key": ...}。
    整えは editor/macros.py の sanitize_registered が行う)。
    reserved_keys は {キー: 組み込み命令の表示名} で、付けられない
    キー。current_commands は「いまのマクロ」を返す関数(登録ボタン
    用)。on_change は変更のたびに登録マクロの対応の写しを受ける。
    実行ボタンを押すと run_requested に名前が入り、窓が閉じる
    (呼び側が閉じたあとに再生する)。「Python で開く」を押すと
    python_requested に名前が入って窓が閉じ、呼び側が Python の
    別窓(editor/macro_py_dialog.py)を開く。Python の登録
    ({"python": ...})はテキスト編集の対象外で、キー割り当て・
    実行・名前の変更・削除は命令の列の登録と同じに扱う。"""

    def __init__(self, parent=None, *, macros=None, reserved_keys=None,
                 current_commands=None, on_change=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。循環 import を
        # 避けるため文字列で持つ(color_dialog と同じ)。
        self.setWindowTitle("マクロの管理 - EDDy")
        self.resize(640, 440)
        self._on_change = on_change
        self._current_commands = current_commands
        self.run_requested = None
        self.python_requested = None
        self._reserved = {}
        for keys, label in (reserved_keys or {}).items():
            normalized = normalize_keys(keys)
            if normalized:
                self._reserved[normalized] = label
        self._macros = macmod.sanitize_registered(macros)
        # キーの表記は QKeySequence の正規形に揃える(設定を手で
        # 書き換えた場合の表記ゆれを、重なりの判定に持ち込まない)。
        for entry in self._macros.values():
            normalized = normalize_keys(entry["key"])
            entry["key"] = normalized if normalized is not None else ""

        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        self._list = QListWidget()
        self._list.currentTextChanged.connect(self._on_selection_changed)
        left.addWidget(self._list, 1)
        register_button = QPushButton("いまのマクロを登録…")
        register_button.clicked.connect(self._on_register_clicked)
        left.addWidget(register_button)
        delete_button = QPushButton("削除")
        delete_button.clicked.connect(self._on_delete_clicked)
        left.addWidget(delete_button)
        layout.addLayout(left)

        right = QVBoxLayout()
        self._name_label = QLabel("")
        right.addWidget(self._name_label)
        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("キー割り当て"))
        self._key_edit = QKeySequenceEdit()
        self._key_edit.setFixedWidth(180)
        if hasattr(self._key_edit, "setClearButtonEnabled"):
            self._key_edit.setClearButtonEnabled(True)
        if hasattr(self._key_edit, "setMaximumSequenceLength"):
            self._key_edit.setMaximumSequenceLength(1)
        self._key_edit.keySequenceChanged.connect(self._on_key_changed)
        key_row.addWidget(self._key_edit)
        key_row.addStretch(1)
        right.addLayout(key_row)
        self._key_state_label = QLabel("")
        self._key_state_label.setWordWrap(True)
        right.addWidget(self._key_state_label)
        run_button = QPushButton("実行")
        run_button.clicked.connect(self._on_run_clicked)
        right.addWidget(run_button)
        rename_button = QPushButton("名前を変更…")
        rename_button.clicked.connect(self._on_rename_clicked)
        right.addWidget(rename_button)
        self._edit_button = QPushButton("テキストで編集…")
        self._edit_button.clicked.connect(self._on_edit_text_clicked)
        right.addWidget(self._edit_button)
        self._python_button = QPushButton("Python で開く…")
        self._python_button.clicked.connect(self._on_python_clicked)
        right.addWidget(self._python_button)
        right.addStretch(1)
        self._note_label = QLabel("")
        self._note_label.setWordWrap(True)
        right.addWidget(self._note_label)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        right.addWidget(close_button)
        layout.addLayout(right)

        self._reload_list()

    # ---------------------------------------------------------------
    # 状態の読み書き(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def names(self):
        """登録したマクロの名前の並び(登録順)。"""
        return list(self._macros)

    def values(self):
        """登録マクロの対応の写し。"""
        return macmod.copy_registered(self._macros)

    def selected_name(self):
        item = self._list.currentItem()
        return item.text() if item is not None else None

    def macro_key(self, name):
        entry = self._macros.get(name)
        return entry["key"] if entry is not None else ""

    def macro_key_state(self, name):
        """登録マクロのキーが実際に効くかどうか。

        戻り値は (状態, 相手) の組。状態は:
          "none"      キーが付いていない(相手は "")
          "ok"        効く(相手は "")
          "reserved"  組み込みの命令と重なっていて効かない
                      (相手はその命令の表示名)
          "duplicate" 別の登録マクロと重なっていて効かない
                      (相手はそのマクロの名前。窓側の付け直しは
                      先の登録が優先なので、後ろの方が外れる)
        付けるとき(_assign_key)は重なりを作らないが、設定を手で
        書き換えた場合や、組み込みの既定キーが後から変わった
        場合は重なりが入り込む。その場合の判定がここ。"""
        entry = self._macros.get(name)
        if entry is None or not entry["key"]:
            return ("none", "")
        keys = entry["key"]
        if keys in self._reserved:
            return ("reserved", self._reserved[keys])
        for other, other_entry in self._macros.items():
            if other == name:
                break
            if other_entry["key"] == keys:
                return ("duplicate", other)
        return ("ok", "")

    def macro_text(self, name):
        """登録マクロをテキスト形式にしたもの。

        Python のマクロはテキスト形式にはならないので ValueError
        (Python の別窓で開く)。"""
        entry = self._macros[name]
        if "python" in entry:
            raise ValueError(
                "Python のマクロです（「Python で開く」から開いてください）")
        return macmod.commands_to_text(entry["commands"], name)

    def is_python_macro(self, name):
        """その登録が Python のマクロかどうか。"""
        entry = self._macros.get(name)
        return entry is not None and "python" in entry

    # ---------------------------------------------------------------
    # 変更(すべて on_change へ流れる。不正な操作は ValueError)
    # ---------------------------------------------------------------
    def register_macro(self, name, commands, key=""):
        """マクロを登録する。同じ名前があれば ValueError。"""
        name = str(name or "").strip()
        if not name:
            raise ValueError("マクロの名前が空です")
        if name in self._macros:
            raise ValueError(f"同じ名前のマクロが既にあります（{name}）")
        checked = macmod.sanitize_registered(
            {name: {"commands": commands, "key": ""}})
        if name not in checked:
            raise ValueError("命令の列が不正か、空です")
        self._macros[name] = checked[name]
        if key:
            self._assign_key(name, key)
        self._reload_list(name)
        self._notify()
        return name

    def rename_macro(self, old, new):
        """登録マクロの名前を変える。"""
        new = str(new or "").strip()
        if old not in self._macros:
            raise ValueError(f"マクロがありません（{old}）")
        if not new:
            raise ValueError("マクロの名前が空です")
        if new != old and new in self._macros:
            raise ValueError(f"同じ名前のマクロが既にあります（{new}）")
        if new != old:
            renamed = {}
            for key_name, entry in self._macros.items():
                renamed[new if key_name == old else key_name] = entry
            self._macros = renamed
            self._reload_list(new)
            self._notify()
        return new

    def delete_macro(self, name):
        """登録マクロを消す。"""
        if name not in self._macros:
            raise ValueError(f"マクロがありません（{name}）")
        del self._macros[name]
        self._reload_list()
        self._notify()

    def set_macro_key(self, name, text):
        """登録マクロのキー割り当てを変える。

        組み込みと重なるキーは付けられない(False を返して知らせに
        残す)。別の登録マクロと重なったら後勝ちで外す。"""
        if name not in self._macros:
            return False
        normalized = normalize_keys(text)
        if normalized is None:
            self._note_label.setText("読めないキーです。")
            return False
        if not self._assign_key(name, normalized):
            return False
        self._refresh_detail()
        self._notify()
        return True

    def apply_macro_text(self, name, text):
        """テキスト形式で書いた内容を、登録マクロの命令に反映する。

        壊れた行があれば、その行番号つきで ValueError(内容は
        変えない)。テキスト側の名前(# name:)は使わない(名前の
        変更は rename_macro の仕事)。"""
        if name not in self._macros:
            raise ValueError(f"マクロがありません（{name}）")
        if "python" in self._macros[name]:
            raise ValueError(
                "Python のマクロです（「Python で開く」から開いてください）")
        _parsed_name, commands = macmod.commands_from_text(text)
        if not commands:
            raise ValueError("マクロの内容が空です")
        self._macros[name]["commands"] = commands
        self._notify()

    # ---------------------------------------------------------------
    # 内部
    # ---------------------------------------------------------------
    def _assign_key(self, name, normalized):
        """キーを割り当てる。組み込みと重なれば False。"""
        if normalized and normalized in self._reserved:
            self._note_label.setText(
                f"『{normalized}』は組み込みの命令"
                f"（{self._reserved[normalized]}）が使っているので、"
                "マクロには付けられません。")
            return False
        displaced = []
        if normalized:
            for other, entry in self._macros.items():
                if other != name and entry["key"] == normalized:
                    entry["key"] = ""
                    displaced.append(other)
        self._macros[name]["key"] = normalized
        if displaced:
            names = "・".join(displaced)
            self._note_label.setText(
                f"『{normalized}』は『{names}』から外して"
                f"『{name}』へ付けました。")
        else:
            self._note_label.setText("")
        return True

    def _notify(self):
        if self._on_change is not None:
            self._on_change(self.values())

    def _reload_list(self, select=None):
        blocker = QSignalBlocker(self._list)
        self._list.clear()
        for name in self._macros:
            self._list.addItem(name)
        del blocker
        if select is not None and select in self._macros:
            self._list.setCurrentRow(self.names().index(select))
        elif self._list.count() and self._list.currentRow() < 0:
            self._list.setCurrentRow(0)
        self._refresh_detail()

    def _refresh_detail(self):
        name = self.selected_name()
        is_python = bool(name) and self.is_python_macro(name)
        label = name or "（登録したマクロはありません）"
        if is_python:
            label += "（Python）"
        self._name_label.setText(label)
        # Python のマクロはテキスト形式では編集できない(別窓で開く)。
        self._edit_button.setEnabled(bool(name) and not is_python)
        self._python_button.setEnabled(bool(name))
        keys = self.macro_key(name) if name else ""
        blocker = QSignalBlocker(self._key_edit)
        self._key_edit.setKeySequence(QKeySequence(keys))
        del blocker
        state, other = self.macro_key_state(name) if name else ("none", "")
        if state == "reserved":
            self._key_state_label.setText(
                f"『{keys}』は組み込みの『{other}』と重なっているため、"
                "このマクロのキーとしては効いていません。"
                "別のキーを付けてください。")
        elif state == "duplicate":
            self._key_state_label.setText(
                f"『{keys}』は『{other}』も使っているため、"
                "このマクロのキーとしては効いていません"
                "（先に登録した方が優先されます）。"
                "別のキーを付けてください。")
        else:
            self._key_state_label.setText("")

    def _on_selection_changed(self, _text):
        self._refresh_detail()

    def _on_key_changed(self, _sequence):
        name = self.selected_name()
        if name is None:
            return
        text = self._key_edit.keySequence().toString(
            QKeySequence.SequenceFormat.PortableText)
        if text != self.macro_key(name):
            self.set_macro_key(name, text)

    def _on_run_clicked(self):
        name = self.selected_name()
        if name is not None:
            self.run_requested = name
            self.accept()

    def _on_python_clicked(self):
        name = self.selected_name()
        if name is not None:
            self.python_requested = name
            self.accept()

    def _on_delete_clicked(self):
        name = self.selected_name()
        if name is None:
            return
        try:
            self.delete_macro(name)
        except ValueError as e:
            self._note_label.setText(str(e))

    def _on_rename_clicked(self):
        name = self.selected_name()
        if name is None:
            return
        new, ok = QInputDialog.getText(
            self, "マクロの名前を変更", "新しい名前", text=name)
        if not ok:
            return
        try:
            self.rename_macro(name, new)
        except ValueError as e:
            self._note_label.setText(str(e))

    def _on_register_clicked(self):
        if self._current_commands is None:
            return
        commands = self._current_commands()
        if not commands:
            self._note_label.setText("登録できるマクロがありません"
                                     "（記録するか、読み込んでください）。")
            return
        name, ok = QInputDialog.getText(
            self, "マクロを登録", "マクロの名前")
        if not ok:
            return
        try:
            self.register_macro(name, commands)
        except ValueError as e:
            self._note_label.setText(str(e))

    def _on_edit_text_clicked(self):
        name = self.selected_name()
        if name is None:
            return

        def validate(text):
            try:
                self.apply_macro_text(name, text)
            except ValueError as e:
                return str(e)
            return None

        dialog = MacroTextDialog(
            self, name=name, text=self.macro_text(name),
            validator=validate)
        dialog.exec()
