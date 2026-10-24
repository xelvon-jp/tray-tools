# filer_fileinfo.py
# 簡易ファイラの R「ファイル情報の編集」ダイアログ(あふｗの同名の画面に寄せたもの)。
#
#   - 1件: ファイル名・大小の変換・更新日時・属性(Read Only / System / Hidden / Archive)
#   - マークあり: 「ファイル情報の一括変更」。名前は直接は打たせず、変換(小文字 /
#     入れ替え / 大文字)を全件に当てる。日時は「変更する」を付けたときだけ、属性は
#     3つの状態(変えない / ON / OFF)で、ON/OFF にしたものだけ全件に当てる
#
# ここは入力を集めて「何をどう変えるか」(filer_core.plan_file_info の形)を組み立てる
# だけ。実際のファイルに当てるのは呼び出し側(on_apply)で、失敗したらその理由を
# このダイアログに出して閉じない(打った内容を失わない)。
import os
import time

from PySide6.QtCore import QEvent, QFileInfo, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QFileIconProvider,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import filer_core as core

# チェックボックスの四角は、濃紺の地では既定の描き方だと枠が地に溶けて見えない
# (実機で未チェックの Read Only / System / Hidden の四角が見えなかった)。明るい枠を
# 必ず描き、ON は明るい水色で塗りつぶし、一括変更の「変えない」(中間)は灰色の
# 横帯にして、3つの状態を形と色の両方で見分けられるようにする。
CHECKBOX_STYLE = (
    " SCOPE QCheckBox::indicator { width: 12px; height: 12px;"
    " border: 2px solid #c8c8f0; background-color: #00001c; }"
    " SCOPE QCheckBox::indicator:checked { background-color: #40d0ff;"
    " border: 2px solid #ffffff; }"
    " SCOPE QCheckBox::indicator:indeterminate { background-color: qlineargradient("
    "x1:0, y1:0, x2:0, y2:1, stop:0 #00001c, stop:0.3 #00001c, stop:0.31 #a0a0a0,"
    " stop:0.69 #a0a0a0, stop:0.7 #00001c, stop:1 #00001c); }"
    " SCOPE QCheckBox:focus { color: #ffd400; }")

ATTR_ROWS = (("readonly", "Read Only", "R"), ("system", "System", "S"),
             ("hidden", "Hidden", "H"), ("archive", "Archive", "A"))
CASE_BUTTONS = (("lower", "小文字"), ("swap", "⇔"), ("upper", "大文字"))


class NameEdit(QLineEdit):
    """ファイル名の入力欄。フォーカスが来ても全選択しない。

    QLineEdit は Tab やダイアログの初期フォーカスでフォーカスを得ると中身を全選択する。
    実機ではそれで「キャレットを拡張子の手前に」が上書きされていた(offscreen の
    テストではフォーカスの出来事が起きず見逃した)。フォーカスを得たら選択を外し、
    キャレットを caret に戻す。"""

    def __init__(self, text: str, caret: int):
        super().__init__(text)
        self.caret = caret

    def place_caret(self) -> None:
        self.deselect()
        self.setCursorPosition(min(self.caret, len(self.text())))

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.place_caret()

    def focusOutEvent(self, event):
        # Tab で離れて戻ってきたときは、離れたときの位置に戻す(全選択はしない)。
        self.caret = self.cursorPosition()
        super().focusOutEvent(event)


class FileInfoDialog(QDialog):
    """entries が1件ならその編集、複数なら一括変更。folder はその置き場所。

    on_apply(plans) は [plan, ...] を受け取り、失敗の理由(文字列)か None を返す。
    None なら閉じる、文字列ならダイアログに出して開いたまま。"""

    def __init__(self, parent, folder: str, entries, colors=None, font=None, on_apply=None):
        super().__init__(parent)
        self.setWindowTitle("ファイル情報の一括変更" if len(entries) > 1 else "ファイル情報の編集")
        self.folder = folder
        self.entries = [e for e in entries if not e.is_parent]
        self.bulk = len(self.entries) > 1
        self.on_apply = on_apply
        self.error_text = ""
        if font is not None:
            self.setFont(font)
        colors = colors or {}
        bg = colors.get("background", "#00001c")
        fg = colors.get("text", "#ffffff")
        self.setObjectName("fileInfo")
        self.setStyleSheet(
            f"#fileInfo {{ background-color: {bg}; }}"
            f" #fileInfo QLabel, #fileInfo QCheckBox, #fileInfo QGroupBox {{ color: {fg}; }}"
            f" #fileInfo QLineEdit, #fileInfo QSpinBox {{ background-color: #10103a;"
            f" color: {fg}; border: 1px solid #50508a; }}"
            f" #fileInfo QPushButton {{ background-color: #24244e; color: {fg};"
            f" border: 1px solid #50508a; padding: 1px 6px; }}"
            f" #fileInfo QPushButton:checked {{ background-color: #1a3a8a; }}"
            + CHECKBOX_STYLE.replace("SCOPE", "#fileInfo"))

        first = self.entries[0]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 上段: アイコンと元の名前(読み取り専用の表示)
        top = QHBoxLayout()
        self.icon_label = QLabel()
        icon = QFileIconProvider().icon(QFileInfo(os.path.join(folder, first.name)))
        self.icon_label.setPixmap(icon.pixmap(32, 32))
        top.addWidget(self.icon_label)
        self.original_label = QLabel(
            f"{len(self.entries)} 件を一括変更" if self.bulk else first.name)
        self.original_label.setTextFormat(Qt.PlainText)
        top.addWidget(self.original_label, 1)
        layout.addLayout(top)

        # ファイル名
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("ファイル名"))
        self.name_edit = NameEdit(first.name, core.caret_position(first.name, first.is_dir))
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)
        if self.bulk:
            self.name_edit.setText(f"{len(self.entries)} 件を一括変更")
            self.name_edit.setEnabled(False)
        else:
            self._place_caret()

        middle = QHBoxLayout()
        left = QVBoxLayout()
        middle.addLayout(left, 3)

        # 変換
        convert = QGroupBox("変換")
        grid = QGridLayout(convert)
        self.case_buttons = {}
        for row, (part, label) in enumerate((("all", "全体"), ("ext", "拡張子"))):
            grid.addWidget(QLabel(label), row, 0)
            for col, (mode, text) in enumerate(CASE_BUTTONS, start=1):
                button = QPushButton(text)
                button.setCheckable(self.bulk)   # 一括では「当てる/当てない」の切り替え
                button.clicked.connect(lambda _c=False, p=part, m=mode: self._on_case(p, m))
                grid.addWidget(button, row, col)
                self.case_buttons[(part, mode)] = button
        left.addWidget(convert)

        # タイムスタンプ
        stamp = QGroupBox("タイムスタンプ")
        sgrid = QGridLayout(stamp)
        now = time.localtime(first.mtime) if first.mtime else time.localtime()
        self.current_date = QLabel(time.strftime("%Y/%m/%d", now))
        self.current_time = QLabel(time.strftime("%H:%M:%S", now))
        sgrid.addWidget(self.current_date, 0, 0)
        sgrid.addWidget(self.current_time, 1, 0)
        self.stamp_edits = []
        for index, (value, low, high) in enumerate((
                (now.tm_year, 0, 9999), (now.tm_mon, 0, 12), (now.tm_mday, 0, 31),
                (now.tm_hour, 0, 23), (now.tm_min, 0, 59), (now.tm_sec, 0, 59))):
            box = QSpinBox()
            box.setRange(low, high)
            box.setValue(value)
            box.setButtonSymbols(QAbstractSpinBox.NoButtons)
            box.setAlignment(Qt.AlignRight)
            sgrid.addWidget(box, index // 3, 1 + index % 3)
            self.stamp_edits.append(box)
        self.stamp_check = None
        if self.bulk:
            # 一括では付けたときだけ日時を変える。年月日を 0 にすると時刻だけ変える(あふｗ)。
            self.stamp_check = QCheckBox("変更する（年月日 0 なら時刻だけ）")
            sgrid.addWidget(self.stamp_check, 2, 0, 1, 4)
        left.addWidget(stamp)

        # 属性
        attrs = QGroupBox("属性")
        agrid = QVBoxLayout(attrs)
        hint = QLabel("ALT+頭文字 ON/OFF")
        hint.setStyleSheet("font-size: 8pt;")
        agrid.addWidget(hint, 0, Qt.AlignRight)
        self.attr_checks = {}
        flags = core.attrs_to_flags(first.attrs)
        for key, label, _letter in ATTR_ROWS:
            check = QCheckBox(label)
            if self.bulk:
                # 3つの状態: 変えない(中間) / ON / OFF。最初は「変えない」。
                check.setTristate(True)
                check.setCheckState(Qt.PartiallyChecked)
            else:
                check.setChecked(flags[key])
            agrid.addWidget(check)
            self.attr_checks[key] = check
        middle.addWidget(attrs, 2)
        layout.addLayout(middle)

        # エラーの表示と OK / キャンセル
        self.error_label = QLabel()
        self.error_label.setTextFormat(Qt.PlainText)
        self.error_label.setStyleSheet("color: #ff6060;")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.ok_button = QPushButton("OK")
        self.ok_button.setDefault(True)          # Enter で OK
        self.ok_button.clicked.connect(self.try_accept)
        self.cancel_button = QPushButton("キャンセル")
        self.cancel_button.clicked.connect(self.reject)   # Esc もキャンセル(QDialog の既定)
        buttons.addWidget(self.ok_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

        # Tab の順: 名前 → 変換 → 日時 → 属性 → OK
        chain = ([self.name_edit] if not self.bulk else []) + \
            [self.case_buttons[k] for k in (("all", m) for m, _t in CASE_BUTTONS)] + \
            [self.case_buttons[k] for k in (("ext", m) for m, _t in CASE_BUTTONS)] + \
            ([self.stamp_check] if self.stamp_check else []) + self.stamp_edits + \
            [self.attr_checks[k] for k, _l, _a in ATTR_ROWS] + [self.ok_button, self.cancel_button]
        for a, b in zip(chain, chain[1:]):
            self.setTabOrder(a, b)
        (self.name_edit if not self.bulk else self.ok_button).setFocus()

    # --- 名前 ---
    def _place_caret(self) -> None:
        """何も選ばず、キャレットを拡張子の手前に置く(ユーザーの希望。全選択にしない)。"""
        entry = self.entries[0]
        self.name_edit.caret = core.caret_position(self.name_edit.text(), entry.is_dir)
        self.name_edit.place_caret()

    def showEvent(self, event):
        super().showEvent(event)
        if not self.bulk:
            self._place_caret()
            # 表示の直後に Qt が初期フォーカスを渡して全選択することがあるので、
            # 出来事が一巡した後でもう一度フォーカスとキャレットを置き直す。
            QTimer.singleShot(0, self._focus_name)

    def _focus_name(self) -> None:
        try:
            self.name_edit.setFocus()
            self._place_caret()
        except RuntimeError:
            pass  # 先に閉じられた

    def _on_case(self, part: str, mode: str) -> None:
        if self.bulk:
            # 一括: 行ごとにどれか1つだけ(押し直すと外れる)。
            pressed = self.case_buttons[(part, mode)].isChecked()
            for (p, m), button in self.case_buttons.items():
                if p == part and m != mode:
                    button.setChecked(False)
            self.case_buttons[(part, mode)].setChecked(pressed)
            return
        entry = self.entries[0]
        position = self.name_edit.cursorPosition()
        self.name_edit.setText(core.convert_case(self.name_edit.text(), part, mode, entry.is_dir))
        self.name_edit.setCursorPosition(position)

    def chosen_case(self, part: str):
        for (p, m), button in self.case_buttons.items():
            if p == part and button.isChecked():
                return m
        return None

    # --- 属性(Alt+頭文字で切り替え。フォーカスは動かさない) ---
    def _toggle_by_alt(self, event) -> bool:
        """Alt+R/S/H/A なら属性を切り替えて True。フォーカスは動かさない。"""
        if not event.modifiers() & Qt.AltModifier:
            return False
        letters = {letter: key for key, _label, letter in ATTR_ROWS}
        key = int(event.key())
        letter = chr(key) if int(Qt.Key_A) <= key <= int(Qt.Key_Z) else ""
        if letter not in letters:
            return False
        self.attr_checks[letters[letter]].nextCheckState()
        return True

    def event(self, event):
        try:
            if event.type() == QEvent.KeyPress and self._toggle_by_alt(event):
                return True
        except Exception:  # noqa: BLE001  スロットで投げ切ると落ちる
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        # 入力欄などにフォーカスがあると、Alt+頭文字はその部品から親へ回ってくる。
        try:
            if self._toggle_by_alt(event):
                return
        except Exception:  # noqa: BLE001
            return
        super().keyPressEvent(event)

    # --- 組み立て ---
    def stamp_fields(self):
        return tuple(box.value() for box in self.stamp_edits)

    def attr_changes(self) -> dict:
        changes = {}
        for key, check in self.attr_checks.items():
            state = check.checkState()
            if state == Qt.PartiallyChecked:
                continue
            changes[key] = state == Qt.Checked
        return changes

    def build_plans(self):
        """入力から変更の計画を作る。([計画], 誤りの説明 or "")。"""
        if self.bulk:
            fields = self.stamp_fields() if self.stamp_check.isChecked() else None
            plans, problems = core.plan_bulk_info(
                self.entries, self.chosen_case("all"), self.chosen_case("ext"),
                fields, self.attr_changes())
            if problems:
                name, reason = problems[0]
                return [], f"{name}: {reason}"
            return plans, ""
        entry = self.entries[0]
        name = self.name_edit.text().strip()
        problem = core.validate_name(name)
        if problem:
            return [], problem
        mtime, problem = core.make_timestamp(*self.stamp_fields())
        if problem:
            return [], problem
        plan = core.plan_file_info(entry, name, mtime, self.attr_changes())
        return ([plan] if plan else []), ""

    def show_error(self, text: str) -> None:
        self.error_text = text
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def try_accept(self) -> None:
        """OK。誤りがあれば出して閉じない。何も変わっていなければそのまま閉じる。"""
        try:
            plans, problem = self.build_plans()
            if problem:
                self.show_error(problem)
                return
            if plans and self.on_apply is not None:
                error = self.on_apply(plans)
                if error:
                    self.show_error(error)
                    return
            self.accept()
        except Exception as e:  # noqa: BLE001  スロットで投げ切ると落ちる
            self.show_error(f"{type(e).__name__}: {e}")
