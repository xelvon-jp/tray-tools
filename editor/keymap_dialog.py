# editor/keymap_dialog.py
# キー割り当ての設定画面。命令(editor/keymap.py の DEFAULT_KEYMAP の
# すべて)と現在の割り当てを一覧し、その場でキーを押して変えられる窓。
#
# 変えるたびに on_change(上書き分) が呼ばれ、呼び側(窓)が開いている
# 文書へ反映して設定へ保存する(この窓自体は設定に触らない。色の
# 設定画面と同じ作り)。上書き分は「既定と違う項目だけ」で、割り当て
# を外した命令は空文字列で表す(editor/keymap.py の diff_overrides)。
# 項目ごとの「既定に戻す」と、まとめて戻す「すべて既定に戻す」が
# ある。キーの取り込みは Qt 標準の QKeySequenceEdit(EDDy の割り当て
# は1命令1キーなので、取り込む並びは最初の1つだけ)。
#
# 重なりの扱い: ある命令に、別の命令が使っているキーを付けたら、
# 別の命令の割り当ては外れる(後から付けた方が勝つ)。重なったまま
# 保存すると Qt のショートカットがどちらも効かなくなるため、その
# 場で解消する。外したことは画面下の知らせに残すので、その場で
# 分かる。開いた時点で既に重なっている表(設定を手で書き換えた
# 場合など)は、そのまま出して知らせにも出す。どちらかの割り当てを
# 変えれば、上記の作法で直る。
#
# テストでは set_command_key / clear_command_key / reset_command /
# reset_all を直接呼べる。
from PySide6.QtCore import QSignalBlocker
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QKeySequenceEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from editor import editor_icon
from editor import keymap as keymapmod


def normalize_keys(text):
    """キーの文字列を QKeySequence で読める形に揃える。

    戻り値は正規化した文字列。空は「割り当てなし」。読めない値は
    None(呼び側は変更しない側に倒す)。複数の並び(「Ctrl+X, Ctrl+Y」)
    は最初の1つだけを使う。"""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return ""
    sequence = QKeySequence(text)
    if sequence.count() == 0:
        return None
    return sequence.toString(
        QKeySequence.SequenceFormat.PortableText).split(", ")[0]


class KeymapSettingsDialog(QDialog):
    """キー割り当ての設定画面。

    keymap は現在の実効の割り当て(命令名 → キー)。on_change は
    上書き分(既定と違う項目だけの対応)を受ける関数で、変更のたびに
    呼ばれる(呼ばれた側が反映と保存をする)。"""

    def __init__(self, parent=None, *, keymap=None, on_change=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。循環 import を
        # 避けるため文字列で持つ(color_dialog と同じ)。
        self.setWindowTitle("キー割り当ての設定 - EDDy")
        self.resize(560, 600)
        self._on_change = on_change
        merged = keymapmod.merge_keymap(keymap)
        self._table = {}
        for name, _label in keymapmod.COMMAND_LABELS:
            normalized = normalize_keys(merged.get(name))
            if normalized is None:
                normalized = keymapmod.DEFAULT_KEYMAP[name]
            self._table[name] = normalized
        self._defaults = {
            name: normalize_keys(keys)
            for name, keys in keymapmod.DEFAULT_KEYMAP.items()}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "キーの欄を選んで、付けたいキーを押してください。"
            "別の命令が使っているキーを付けると、その命令の割り当ては"
            "外れます。"))

        self._edits = {}
        self._reset_buttons = {}
        rows_widget = QWidget()
        rows = QVBoxLayout(rows_widget)
        rows.setContentsMargins(0, 0, 0, 0)
        for name, label in keymapmod.COMMAND_LABELS:
            row = QHBoxLayout()
            name_label = QLabel(label)
            name_label.setMinimumWidth(220)
            row.addWidget(name_label)
            edit = QKeySequenceEdit()
            edit.setFixedWidth(180)
            if hasattr(edit, "setClearButtonEnabled"):
                # 欄の消去ボタンで割り当てを外せるようにする。
                edit.setClearButtonEnabled(True)
            if hasattr(edit, "setMaximumSequenceLength"):
                edit.setMaximumSequenceLength(1)
            edit.keySequenceChanged.connect(
                lambda _seq, n=name: self._on_edit_changed(n))
            row.addWidget(edit)
            reset = QPushButton("既定に戻す")
            reset.clicked.connect(
                lambda _checked=False, n=name: self.reset_command(n))
            row.addWidget(reset)
            row.addStretch(1)
            rows.addLayout(row)
            self._edits[name] = edit
            self._reset_buttons[name] = reset
        rows.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(rows_widget)
        layout.addWidget(scroll, 1)

        self._note_label = QLabel("")
        self._note_label.setWordWrap(True)
        layout.addWidget(self._note_label)

        bottom = QHBoxLayout()
        reset_all = QPushButton("すべて既定に戻す")
        reset_all.clicked.connect(self.reset_all)
        bottom.addWidget(reset_all)
        bottom.addStretch(1)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

        for name, _label in keymapmod.COMMAND_LABELS:
            self._refresh_row(name)
        conflicts = self.conflicts()
        if conflicts:
            parts = []
            for keys, names in conflicts.items():
                labels = "・".join(
                    keymapmod.command_label(n) for n in names)
                parts.append(f"『{keys}』（{labels}）")
            detail = "、".join(parts)
            self._note_label.setText(
                f"重なりがあります：{detail}。どちらかの割り当てを"
                "変えると直ります。")

    # ---------------------------------------------------------------
    # 状態の読み書き(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def values(self):
        """実効の割り当て(命令名 → キー)の写し。"""
        return dict(self._table)

    def overrides(self):
        """上書き分(既定と違う項目だけ)の写し。"""
        return keymapmod.diff_overrides(self._table)

    def command_key(self, name):
        return self._table.get(name, "")

    def is_default(self, name) -> bool:
        return self._table.get(name) == self._defaults.get(name)

    def conflicts(self):
        """重なっているキー → 命令名の並び、の対応。

        割り当てなし(空)は重なりに入れない。通常の操作では後勝ちで
        解消されるので空になるが、開いた時点の表が重なっている
        場合はここに出る。"""
        by_key = {}
        for name, keys in self._table.items():
            if keys:
                by_key.setdefault(keys, []).append(name)
        return {keys: names for keys, names in by_key.items()
                if len(names) > 1}

    # ---------------------------------------------------------------
    # 変更(すべて on_change へ流れる)
    # ---------------------------------------------------------------
    def set_command_key(self, name, text):
        """命令のキーを変える。

        別の命令が同じキーを使っていたら、その命令の割り当ては
        外れる(後勝ち。その場で知らせに残す)。読めないキー・
        変化なしのときは何もしない。"""
        if name not in self._table:
            return
        normalized = normalize_keys(text)
        if normalized is None:
            return
        if self._table[name] == normalized:
            return
        displaced = []
        if normalized:
            for other, other_keys in self._table.items():
                if other != name and other_keys == normalized:
                    self._table[other] = ""
                    displaced.append(other)
                    self._refresh_row(other)
        self._table[name] = normalized
        self._refresh_row(name)
        if displaced:
            names = "・".join(
                keymapmod.command_label(n) for n in displaced)
            self._note_label.setText(
                f"『{normalized}』は『{names}』から外して"
                f"『{keymapmod.command_label(name)}』へ付けました。")
        else:
            self._note_label.setText("")
        self._notify()

    def clear_command_key(self, name):
        """命令の割り当てを外す(「なし」にする)。"""
        self.set_command_key(name, "")

    def reset_command(self, name):
        """命令のキーを既定に戻す。"""
        if name in self._defaults:
            self.set_command_key(name, self._defaults[name])

    def reset_all(self):
        """すべての命令のキーを既定に戻す。"""
        if all(self._table[name] == self._defaults[name]
               for name in self._table):
            return
        for name in self._table:
            self._table[name] = self._defaults[name]
            self._refresh_row(name)
        self._note_label.setText("")
        self._notify()

    # ---------------------------------------------------------------
    # 内部
    # ---------------------------------------------------------------
    def _on_edit_changed(self, name):
        sequence = self._edits[name].keySequence()
        text = sequence.toString(
            QKeySequence.SequenceFormat.PortableText)
        self.set_command_key(name, text)

    def _notify(self):
        if self._on_change is not None:
            self._on_change(self.overrides())

    def _refresh_row(self, name):
        edit = self._edits[name]
        blocker = QSignalBlocker(edit)
        edit.setKeySequence(QKeySequence(self._table[name]))
        del blocker
        self._reset_buttons[name].setEnabled(not self.is_default(name))
