# editor/macro_py_dialog.py
# マクロの Python を別窓で見て・直して・実行する画面。
#
# できること:
#   - ソースの表示と編集(編集すると実行の途中状態は破棄される)
#   - ステップ実行(1文ずつ。本文の窓で実際に編集が進む)
#   - 通し実行(残りの文を全部)
#   - 最初から(途中状態を捨てて、頭に戻す)
#   - クリップボードへコピー(職場 Copilot など AI へ貼るため)
#   - 登録として保存(on_save があるときだけ。名前とソースを渡す)
#
# 実行そのものは editor/macro_py.py の PythonStepper が持ち、
# セッションは呼び側(窓)が session_factory で作って渡す(この
# 画面は Qt の外の形を知らない)。実行区間の undo の単位は、
# 呼び側の begin_run / end_run が面倒を見る(ステップをまたい
# でも1回の実行は1つの単位)。
#
# 次に実行する文は背景色で示す。エラーは行番号つきで下の欄に
# 出す(print の出力も同じ欄)。
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from editor import editor_icon
from editor import macro_py

_NEXT_LINE_COLOR = QColor(255, 243, 176)


class MacroPythonDialog(QDialog):
    """マクロ (Python) の画面。

    session_factory はソースを受けて PythonStepper を返す関数
    (作れなければ None)。begin_run / end_run は実行区間の開始と
    終了(呼び側が undo の単位と「再生中」の印を持つ)。on_save は
    (名前, ソース) を受ける登録の口で、無ければ保存ボタンは
    出ない。"""

    def __init__(self, parent=None, *, source="", name="",
                 session_factory=None, begin_run=None, end_run=None,
                 on_save=None):
        super().__init__(parent)
        self.setWindowIcon(editor_icon.qicon())
        # 製品名は editor/window.py の APP_NAME = "EDDy"。循環 import を
        # 避けるため文字列で持つ(他の画面と同じ)。
        self.setWindowTitle("マクロ (Python) - EDDy")
        self.resize(680, 520)
        self._session_factory = session_factory
        self._begin_run = begin_run
        self._end_run = end_run
        self._on_save = on_save
        self._session = None
        self._run_begun = False
        self._output_shown = 0

        layout = QVBoxLayout(self)
        if on_save is not None:
            name_row = QHBoxLayout()
            name_row.addWidget(QLabel("名前"))
            self._name_edit = QLineEdit(name)
            name_row.addWidget(self._name_edit, 1)
            layout.addLayout(name_row)
        else:
            self._name_edit = None
        self._edit = QPlainTextEdit()
        self._edit.setPlainText(source)
        self._edit.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))
        self._edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self._edit, 1)

        buttons = QHBoxLayout()
        self._run_button = QPushButton("通し実行")
        self._run_button.clicked.connect(self._on_run_all)
        buttons.addWidget(self._run_button)
        self._step_button = QPushButton("ステップ実行")
        self._step_button.clicked.connect(self._on_step)
        buttons.addWidget(self._step_button)
        self._reset_button = QPushButton("最初から")
        self._reset_button.clicked.connect(self._on_reset)
        buttons.addWidget(self._reset_button)
        copy_button = QPushButton("コピー")
        copy_button.clicked.connect(self._on_copy)
        buttons.addWidget(copy_button)
        if on_save is not None:
            save_button = QPushButton("登録として保存")
            save_button.clicked.connect(self._on_save_clicked)
            buttons.addWidget(save_button)
        buttons.addStretch(1)
        close_button = QPushButton("閉じる")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._refresh_state("")

    # ---------------------------------------------------------------
    # 状態(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def source(self):
        """いまのソース(編集欄の内容)。"""
        return self._edit.toPlainText()

    def session(self):
        """実行セッション(まだ作っていなければ None)。"""
        return self._session

    # ---------------------------------------------------------------
    # 実行
    # ---------------------------------------------------------------
    def _ensure_session(self):
        """セッションを用意する。用意できなければ理由を表示して
        None(呼び側はそのまま戻る)。"""
        if self._session is not None:
            return self._session
        source = self.source()
        invalid = macro_py.validate_python(source)
        if invalid is not None:
            self._refresh_state(invalid)
            return None
        if self._session_factory is None:
            self._refresh_state("実行できません（実行の口がありません）")
            return None
        self._session = self._session_factory(source)
        self._output_shown = 0
        if self._session is None:
            self._refresh_state("セッションを作れませんでした")
        return self._session

    def _begin(self):
        if not self._run_begun and self._begin_run is not None:
            self._begin_run()
            self._run_begun = True

    def _finish(self):
        if self._run_begun and self._end_run is not None:
            self._end_run()
            self._run_begun = False

    def step_once(self) -> bool:
        """次の1文を実行する。実行したら True。"""
        session = self._ensure_session()
        if session is None or session.done:
            return False
        self._begin()
        try:
            session.step()
        except macro_py.PythonMacroError as e:
            self._finish()
            self._refresh_state(str(e))
            return False
        if session.done:
            self._finish()
        self._refresh_state("")
        return True

    def run_all(self) -> bool:
        """残りの文を通しで実行する。最後まで行ったら True。"""
        session = self._ensure_session()
        if session is None or session.done:
            return False
        self._begin()
        try:
            session.run_all()
        except macro_py.PythonMacroError as e:
            self._finish()
            self._refresh_state(str(e))
            return False
        self._finish()
        self._refresh_state("")
        return True

    def reset_session(self):
        """実行の途中状態を捨てる(本文の変更は戻らない)。"""
        self._finish()
        self._session = None
        self._refresh_state("")

    # ---------------------------------------------------------------
    # ボタンの受け口
    # ---------------------------------------------------------------
    def _on_step(self):
        try:
            self.step_once()
        except Exception as e:  # noqa: BLE001  スロットで投げ切らない
            self._finish()
            self._refresh_state(f"エラー: {e}")

    def _on_run_all(self):
        try:
            self.run_all()
        except Exception as e:  # noqa: BLE001
            self._finish()
            self._refresh_state(f"エラー: {e}")

    def _on_reset(self):
        self.reset_session()

    def _on_copy(self):
        QGuiApplication.clipboard().setText(self.source())
        self._refresh_state("クリップボードへコピーしました")

    def _on_save_clicked(self):
        if self._on_save is None:
            return
        name = (self._name_edit.text() if self._name_edit else "").strip()
        if not name:
            self._refresh_state("名前を入れてください")
            return
        invalid = macro_py.validate_python(self.source())
        if invalid is not None:
            self._refresh_state(invalid)
            return
        try:
            self._on_save(name, self.source())
        except Exception as e:  # noqa: BLE001
            self._refresh_state(f"登録できませんでした: {e}")
            return
        self._refresh_state(f"『{name}』として登録しました")

    def _on_text_changed(self):
        # 編集したら、実行の途中状態は意味を失うので捨てる。
        if self._session is not None or self._run_begun:
            self.reset_session()
            self._refresh_state("編集したので、実行は最初からになります")

    # ---------------------------------------------------------------
    # 表示
    # ---------------------------------------------------------------
    def _refresh_state(self, message):
        session = self._session
        parts = []
        if session is not None:
            fresh = session.output[self._output_shown:]
            self._output_shown = len(session.output)
            for line in fresh:
                parts.append(f"print: {line}")
            if session.error is not None:
                parts.append(str(session.error))
            elif session.done:
                parts.append("終わりました")
            else:
                lineno = session.next_lineno()
                parts.append(
                    f"{session.position + 1} / {session.total} 文"
                    f"（次は {lineno} 行目）")
        if message:
            parts.append(message)
        self._status_label.setText("\n".join(parts))
        self._highlight_next()
        can_run = session is None or not session.done
        self._run_button.setEnabled(can_run)
        self._step_button.setEnabled(can_run)

    def _highlight_next(self):
        selections = []
        session = self._session
        if session is not None and not session.done:
            lines = session.statement_lines()
            if session.position < len(lines):
                start, end = lines[session.position]
                doc = self._edit.document()
                first = doc.findBlockByNumber(start - 1)
                last = doc.findBlockByNumber(end - 1)
                if first.isValid() and last.isValid():
                    cursor = QTextCursor(doc)
                    cursor.setPosition(first.position())
                    cursor.setPosition(
                        last.position() + last.length() - 1,
                        QTextCursor.MoveMode.KeepAnchor)
                    selection = QTextEdit.ExtraSelection()
                    selection.cursor = cursor
                    selection.format.setBackground(_NEXT_LINE_COLOR)
                    selections.append(selection)
                    # 次の文が見えるよう、編集欄のカーソルも寄せる。
                    caret = self._edit.textCursor()
                    caret.setPosition(first.position())
                    self._edit.setTextCursor(caret)
        self._edit.setExtraSelections(selections)

    def done(self, result):
        # 閉じるときに実行区間が開いたままなら、呼び側へ返す。
        self._finish()
        super().done(result)
