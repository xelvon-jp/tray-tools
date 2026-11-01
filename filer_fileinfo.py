# filer_fileinfo.py
# あっふの R「ファイル情報の編集」ダイアログ(あふｗの同名の画面に寄せたもの)。
#
#   - 1件: ファイル名・大小の変換・更新日時・属性(Read Only / System / Hidden / Archive)
#   - マークあり: 「ファイル情報の一括変更」。名前は直接は打たせず、変換(小文字 /
#     入れ替え / 大文字)を全件に当てる。日時は「変更する」を付けたときだけ、属性は
#     3つの状態(変えない / ON / OFF)で、ON/OFF にしたものだけ全件に当てる
#   - 対象に画像があり★を書ける場所なら、下部に「写真の★」の欄(★とキーワード)。
#     画像が無ければ欄は出さない(見た目は従来どおり)。あっふで★を付ける入口はここだけ。
#     ★は★の列のバッジと同じ色のボタンを横に並べ、押すか Alt+5/4/3/2/1/0/- で選ぶ
#     (選べるのは1つ。同じものをもう一度で「変えない」)。ボタンはフォーカスを取らない
#     (Tab の移動を増やさず、Enter はどこからでも OK のまま)
#
# ここは入力を集めて「何をどう変えるか」(filer_core.plan_file_info の形)を組み立てる
# だけ。実際のファイルに当てるのは呼び出し側(on_apply)で、失敗したらその理由を
# このダイアログに出して閉じない(打った内容を失わない)。写真の★も同じで、ここは
# 「何をどう変えるか」(photo_changes)を返すだけ。書くのは呼び出し側(閉じた後)。
import os
import time

from PySide6.QtCore import QEvent, QFileInfo, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
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
# 写真の★を選ぶキー(Alt+この文字 → ★の値)。5〜1 はその★、0 は未選別、- は削除候補。
# ダイアログのどこにフォーカスがあっても効く(QShortcut。名前の欄に数字を打つのとは
# ぶつからない: Alt なしの数字はそのまま欄に入る)。属性の Alt+R/S/H/A とも重ならない。
RATING_KEYS = {"5": 5, "4": 4, "3": 3, "2": 2, "1": 1, "0": 0, "-": -1}
# みてね済のボタンの3つの状態の順(押すたびに次へ)。None は「変えない」。
KEYWORD_CYCLE = (None, True, False)


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
    None なら閉じる、文字列ならダイアログに出して開いたまま。

    photo を渡すと下部に「写真の★」の欄を出す(対象に画像があり★を書ける場所のときだけ
    呼び出し側が渡す)。中身は次の dict。選んだ結果は閉じた後に photo_changes() で読む。
        count        ★を付ける画像の件数(見出しに「画像 n 件」)
        ratings      ★のボタン。並べる順の dict の列 {"value": ★の値, "label": ボタンの文字
                     (「⭐ ベスト」), "name": 正式名(「ベストショット」), "color": 字の色,
                     "background": 地の色}。「変えない」はどれも選んでいない状態
        rating_now   今の★の表示(「ベストショット」「いろいろ」など)
        keyword      キーワードの名前(「みてね済」。無ければ None で行を出さない)
        keyword_key  キーワードを Alt+この文字で切り替える(属性の頭文字と重なれば使わない)
        keyword_now  今のキーワードの表示(「付いている」「付いていない」「いろいろ」)

    colors は "background" / "text"(ダイアログの地と字)と "cursor"(写真の★のボタンを
    選んだときの枠)。

    photo_only を立てると、名前・変換・タイムスタンプ・属性を隠して「写真の★」の欄だけに
    する(★写真抽出の表示での R。名前が相対パスなので、名前などは変えない)。OK でも
    on_apply は呼ばない(変更の計画は空)。"""

    def __init__(self, parent, folder: str, entries, colors=None, font=None, on_apply=None,
                 photo=None, photo_only=False):
        super().__init__(parent)
        self.photo_only = bool(photo_only and photo)
        if self.photo_only:
            self.setWindowTitle("写真の★")
        else:
            self.setWindowTitle("ファイル情報の一括変更" if len(entries) > 1 else "ファイル情報の編集")
        self.folder = folder
        self.entries = [e for e in entries if not e.is_parent]
        self.bulk = len(self.entries) > 1
        self.on_apply = on_apply
        self.error_text = ""
        if font is not None:
            self.setFont(font)
        colors = colors or {}
        self.colors = colors
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
        self.name_label = QLabel("ファイル名")
        name_row.addWidget(self.name_label)
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
        if self.photo_only:
            # 写真の★の欄だけにする(名前・変換・タイムスタンプ・属性は出さない)
            for widget in (self.name_label, self.name_edit, convert, stamp, attrs):
                widget.hide()

        # 写真の★(対象に画像があり★を書ける場所のときだけ)
        self.photo_box = None
        self.rating_buttons = {}        # ★の値 → ボタン
        self.rating_names = {}          # ★の値 → 正式名(「ベストショット」)
        self.rating_choice = None       # 選んだ★(None は「変えない」)
        self.keyword = None
        self.keyword_button = None
        self.keyword_choice = None      # みてね済: None 変えない / True 付ける / False 外す
        self.keyword_key = ""
        if photo:
            self._build_photo_box(layout, photo)

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

        # Tab の順: 名前 → 変換 → 日時 → 属性 → OK(写真の★のボタンはフォーカスを取らない)
        chain = ([self.name_edit] if not self.bulk else []) + \
            [self.case_buttons[k] for k in (("all", m) for m, _t in CASE_BUTTONS)] + \
            [self.case_buttons[k] for k in (("ext", m) for m, _t in CASE_BUTTONS)] + \
            ([self.stamp_check] if self.stamp_check else []) + self.stamp_edits + \
            [self.attr_checks[k] for k, _l, _a in ATTR_ROWS] + \
            [self.ok_button, self.cancel_button]
        for a, b in zip(chain, chain[1:]):
            self.setTabOrder(a, b)
        (self.name_edit if not (self.bulk or self.photo_only) else self.ok_button).setFocus()

    # --- 写真の★ ---
    def _build_photo_box(self, layout, photo: dict) -> None:
        """下部の「写真の★」の欄。★は★の列のバッジと同じ色のボタンを横に並べ、各ボタンの
        下に小さくキーの案内(Alt+-・Alt+0・Alt+1〜Alt+5)。既定は「変えない」(どれも選んで
        いない)。みてね済は同じ形のボタン1つで、押すたびに 変えない → 付ける ✓ → 外す。"""
        box = QGroupBox(f"写真の★（画像 {photo.get('count', 0)} 件）")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(1)
        cursor = self.colors.get("cursor", "#00ffff")
        keys = {v: k for k, v in RATING_KEYS.items()}
        col = 0
        for item in photo.get("ratings") or ():
            value = int(item["value"])
            button = self._photo_button(item["label"], item.get("color", "#ffffff"),
                                        item.get("background", "#333333"), cursor)
            button.setToolTip(item.get("name") or item["label"])
            button.clicked.connect(lambda _c=False, v=value: self._on_rating_button(v))
            grid.addWidget(button, 0, col)
            if value in keys:
                grid.addWidget(self._key_hint(f"Alt+{keys[value]}"), 1, col, Qt.AlignHCenter)
            self.rating_buttons[value] = button
            self.rating_names[value] = item.get("name") or item["label"]
            col += 1
        keyword = photo.get("keyword")
        self.keyword_now_label = None
        if keyword:
            self.keyword = keyword
            key = (photo.get("keyword_key") or "").upper()
            # 属性の Alt+頭文字と重なるなら使わない(属性を優先)
            if len(key) == 1 and "A" <= key <= "Z" and key not in {a for _k, _l, a in ATTR_ROWS}:
                self.keyword_key = key
            grid.setColumnMinimumWidth(col, 10)      # ★のボタンとの間を少し空ける
            col += 1
            fg = self.colors.get("text", "#ffffff")
            self.keyword_button = self._photo_button("", fg, "#24244e", cursor)
            self.keyword_button.setToolTip(f"{keyword}を付ける／外す（押すたびに切り替え）")
            self.keyword_button.clicked.connect(self._on_keyword_button)
            grid.addWidget(self.keyword_button, 0, col)
            if self.keyword_key:
                grid.addWidget(self._key_hint(f"Alt+{self.keyword_key}"), 1, col, Qt.AlignHCenter)
        grid.setColumnStretch(col + 1, 1)
        # 今の★・キーワード(選んだ結果は下の1行)
        self.rating_now_label = QLabel(f"今: {photo.get('rating_now') or 'いろいろ'}")
        self.rating_now_label.setTextFormat(Qt.PlainText)
        grid.addWidget(self.rating_now_label, 2, 0, 1, max(len(self.rating_buttons), 1))
        if keyword:
            self.keyword_now_label = QLabel(f"今: {photo.get('keyword_now') or 'いろいろ'}")
            self.keyword_now_label.setTextFormat(Qt.PlainText)
            grid.addWidget(self.keyword_now_label, 2, col, 1, 2)
        self.rating_choice_label = QLabel()
        self.rating_choice_label.setTextFormat(Qt.PlainText)
        grid.addWidget(self.rating_choice_label, 3, 0, 1, col + 2)
        layout.addWidget(box)
        self.photo_box = box
        # Alt+5/4/3/2/1/0/- で★を、Alt+(キーワードの頭文字)でみてね済を切り替える。
        # QShortcut なのでダイアログのどこにフォーカスがあっても効き、入力欄に字は入らない。
        for text, value in RATING_KEYS.items():
            shortcut = QShortcut(QKeySequence(f"Alt+{text}"), self)
            shortcut.activated.connect(lambda v=value: self._on_rating_key(v))
        if self.keyword_key:
            shortcut = QShortcut(QKeySequence(f"Alt+{self.keyword_key}"), self)
            shortcut.activated.connect(self._on_keyword_button)
        self._show_photo_choice()

    def _photo_button(self, text: str, fg: str, bg: str, cursor: str) -> QPushButton:
        """写真の★の欄のボタン(★の列のバッジと同じ色の角丸。選んだらカーソルの色の太い枠)。
        フォーカスは取らない(Tab の移動を増やさず、Enter はどこからでも OK のまま)。"""
        button = QPushButton(text)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCursor(Qt.PointingHandCursor)
        # 自分のスタイルシートはダイアログの #fileInfo QPushButton より優先される
        button.setStyleSheet(
            f"QPushButton {{ background-color: {bg}; color: {fg}; border: 3px solid {bg};"
            f" border-radius: 5px; padding: 2px 6px; }}"
            f" QPushButton:checked {{ background-color: {bg}; border: 3px solid {cursor}; }}")
        return button

    @staticmethod
    def _key_hint(text: str) -> QLabel:
        hint = QLabel(text)
        hint.setStyleSheet("font-size: 8pt;")
        return hint

    def pick_rating(self, value) -> None:
        """★を選ぶ。選んでいるものをもう一度選ぶと「変えない」に戻る。"""
        if value not in self.rating_buttons:
            return
        self.rating_choice = None if value == self.rating_choice else value
        self._show_photo_choice()

    def cycle_keyword(self) -> None:
        """みてね済を 変えない → 付ける → 外す → 変えない の順に切り替える。"""
        if self.keyword_button is None:
            return
        index = KEYWORD_CYCLE.index(self.keyword_choice)
        self.keyword_choice = KEYWORD_CYCLE[(index + 1) % len(KEYWORD_CYCLE)]
        self._show_photo_choice()

    def _on_rating_button(self, value) -> None:
        try:
            self.pick_rating(value)
        except Exception:  # noqa: BLE001  スロットで投げ切ると落ちる
            pass

    def _on_rating_key(self, value) -> None:
        try:
            self.pick_rating(value)
        except Exception:  # noqa: BLE001
            pass

    def _on_keyword_button(self) -> None:
        try:
            self.cycle_keyword()
        except Exception:  # noqa: BLE001
            pass

    def _show_photo_choice(self) -> None:
        """ボタンの選んだ印・みてね済のボタンの文字・下の1行を、選んだ状態に合わせる。"""
        for value, button in self.rating_buttons.items():
            button.setChecked(value == self.rating_choice)
        if self.keyword_button is not None:
            state = {None: "変えない", True: "付ける ✓", False: "外す"}[self.keyword_choice]
            self.keyword_button.setText(f"{self.keyword}: {state}")
            self.keyword_button.setChecked(self.keyword_choice is not None)
        if self.rating_choice is None:
            self.rating_choice_label.setText("★: 変えない")
        else:
            self.rating_choice_label.setText(f"★: {self.rating_names[self.rating_choice]}にする")

    def photo_changes(self) -> dict:
        """写真の★の欄で変えるもの。{"rating": ★の値, "keyword": 付けるなら True / 外すなら
        False}。「変えない」のものは入れない(欄が無ければ空)。"""
        changes = {}
        if self.rating_choice is not None:
            changes["rating"] = int(self.rating_choice)
        if self.keyword_button is not None and self.keyword_choice is not None:
            changes["keyword"] = bool(self.keyword_choice)
        return changes

    # --- 名前 ---
    def _place_caret(self) -> None:
        """何も選ばず、キャレットを拡張子の手前に置く(ユーザーの希望。全選択にしない)。"""
        entry = self.entries[0]
        self.name_edit.caret = core.caret_position(self.name_edit.text(), entry.is_dir)
        self.name_edit.place_caret()

    def showEvent(self, event):
        super().showEvent(event)
        if not (self.bulk or self.photo_only):
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
        """Alt+R/S/H/A なら属性を切り替えて True。フォーカスは動かさない(写真の★と
        キーワードの Alt キーは QShortcut で受ける。_build_photo_box)。"""
        if self.photo_only or not event.modifiers() & Qt.AltModifier:
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
        if self.photo_only:
            return [], ""       # 写真の★の欄だけ(★は閉じた後に photo_changes で読む)
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
