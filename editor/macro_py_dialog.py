# editor/macro_py_dialog.py
# マクロの Python を別窓で見て・直して・実行する画面。
#
# できること:
#   - ソースの表示と編集(編集すると実行の途中状態は破棄される)
#   - ステップ実行(1文ずつ。本文の窓で実際に編集が進む)
#   - 通し実行(残りの文を全部)
#   - 停止(実行側の子プロセスを終了する。無限ループでも効く)
#   - 最初から(途中状態を捨てて、頭に戻す)
#   - クリップボードへコピー(職場 Copilot など AI へ貼るため)
#   - 登録として保存(on_save があるときだけ。名前とソースを渡す)
#
# 開いただけでは何も実行しない。実行は「通し実行」「ステップ
# 実行」を押したときだけ。実行そのものは editor/macro_py.py の
# PythonStepper(子プロセスで実行)が持ち、セッションは呼び側
# (窓)が session_factory で作って渡す(この画面は Qt の外の形を
# 知らない)。実行区間の undo の単位は、呼び側の begin_run /
# end_run が面倒を見る(ステップをまたいでも1回の実行は1つの
# 単位)。
#
# 実行中は、イベントを回しながら待つ(子が純粋な計算で回って
# いても画面が固まらないように)。その間は停止以外の操作を
# 止め、閉じることも止める(実行中のセッションを抱えたまま
# 画面が壊れるのを防ぐため)。
#
# セッションの後始末は、閉じる経路のどれでも行う(done だけで
# なく closeEvent と destroyed でも)。EDDy の窓ごと閉じられた
# ときなど、done を通らない経路で子プロセスが残るのを防ぐ
# (req-20261114-09 の指摘)。
#
# 次に実行する文は背景色で示す。エラーは行番号つきで下の欄に
# 出す(print の出力も同じ欄)。
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFontDatabase, QGuiApplication, QTextCursor

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
                 on_save=None, live_source=None):
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
        self._running = False
        # 記録のライブ表示 (live_source が渡されたとき)。記録中の
        # ソースを呼び側から受け取り、利用者が編集していなければ
        # そのまま差し替える。窓はモーダレスで開く前提 (EDDy の
        # 画面を操作しながら、記録が増えていくのを見られる)。
        self._live_source = live_source
        self._live_synced = source
        self._live_dirty = False
        self._syncing_live = False
        self._live_timer = None
        if live_source is not None:
            self._live_timer = QTimer(self)
            self._live_timer.setInterval(300)
            self._live_timer.timeout.connect(self._sync_live)
            self._live_timer.start()
        # 壊れるとき(EDDy の窓ごと閉じられたとき等)も、抱えて
        # いるセッションの子を残さないための受け口。自分の
        # メソッドをそのままつなぐと、親ごと壊れる経路では
        # PySide が呼び出しを届けないことがある(実測)ため、
        # クロージャ経由でつなぐ。
        self.destroyed.connect(lambda *args: self._on_destroyed())

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
        self._stop_button = QPushButton("停止")
        self._stop_button.clicked.connect(self._on_stop)
        self._stop_button.setEnabled(False)
        buttons.addWidget(self._stop_button)
        self._reset_button = QPushButton("最初から")
        self._reset_button.clicked.connect(self._on_reset)
        buttons.addWidget(self._reset_button)
        self._copy_button = QPushButton("コピー")
        self._copy_button.clicked.connect(self._on_copy)
        buttons.addWidget(self._copy_button)
        if on_save is not None:
            self._save_button = QPushButton("登録として保存")
            self._save_button.clicked.connect(self._on_save_clicked)
            buttons.addWidget(self._save_button)
        else:
            self._save_button = None
        buttons.addStretch(1)
        self._close_button = QPushButton("閉じる")
        self._close_button.clicked.connect(self.accept)
        buttons.addWidget(self._close_button)
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

    def running(self):
        """実行中(ステップ・通しの最中)か。"""
        return self._running

    # ---------------------------------------------------------------
    # 実行
    # ---------------------------------------------------------------
    def _ensure_session(self):
        """セッションを用意する。用意できなければ理由を表示して
        None(呼び側はそのまま戻る)。作るだけでは実行しない。"""
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
        else:
            # 実行中も画面が固まらないよう、待ちの間にイベントを
            # 回す口を渡す(停止ボタンを受け付けるため)。
            self._session.pump = self._pump_events
        return self._session

    @staticmethod
    def _pump_events():
        QApplication.processEvents()

    def _begin(self):
        if not self._run_begun and self._begin_run is not None:
            self._begin_run()
            self._run_begun = True

    def _finish(self):
        if self._run_begun and self._end_run is not None:
            self._end_run()
            self._run_begun = False

    def _discard_session(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def _cleanup_session(self):
        """閉じるときの後始末(実行区間を返し、セッションを捨てる)。

        done・closeEvent のどちらから呼ばれてもよい(2回目以降は
        何もしない)。"""
        self._finish()
        self._discard_session()

    def _on_destroyed(self, *args):
        # 画面が壊れる最中の受け口。窓側の後始末(_finish の先)は
        # 親の窓ごと壊れている途中かもしれず触らない。ここでは
        # セッションの子を終わらせることだけを確実に行う。
        try:
            self._discard_session()
        except Exception:  # noqa: BLE001  壊れる最中で投げ切らない
            pass

    def _set_running(self, running):
        self._running = running
        self._stop_button.setEnabled(running)
        for widget in (self._run_button, self._step_button,
                       self._reset_button, self._copy_button,
                       self._save_button, self._close_button,
                       self._name_edit, self._edit):
            if widget is not None:
                widget.setEnabled(not running)

    def step_once(self) -> bool:
        """次の1文を実行する。実行したら True。"""
        session = self._ensure_session()
        if session is None or session.done:
            return False
        self._begin()
        self._set_running(True)
        try:
            session.step()
        except macro_py.PythonMacroError as e:
            self._finish()
            self._refresh_state(str(e))
            return False
        finally:
            self._set_running(False)
        if session.done:
            self._finish()
        self._refresh_state("")
        return not session.stopped

    def run_all(self) -> bool:
        """残りの文を通しで実行する。最後まで行ったら True。"""
        session = self._ensure_session()
        if session is None or session.done:
            return False
        self._begin()
        self._set_running(True)
        try:
            session.run_all()
        except macro_py.PythonMacroError as e:
            self._finish()
            self._refresh_state(str(e))
            return False
        finally:
            self._set_running(False)
        self._finish()
        self._refresh_state("")
        return not session.stopped

    def stop_run(self):
        """実行を止める(実行側の子プロセスを終了する)。"""
        if self._session is not None:
            self._session.stop()

    def reset_session(self):
        """実行の途中状態を捨てる(本文の変更は戻らない)。"""
        self._finish()
        self._discard_session()
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

    def _on_stop(self):
        self.stop_run()

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
        if self._syncing_live:
            return  # ライブ表示の差し替えは「編集」として扱わない
        self._live_dirty = True
        # 編集したら、実行の途中状態は意味を失うので捨てる。
        if self._session is not None or self._run_begun:
            self.reset_session()
            self._refresh_state("編集したので、実行は最初からになります")

    def _sync_live(self):
        """記録のライブ表示を進める。記録中のソースが増えていたら、
        利用者が編集していないときに限り差し替える (編集した人の
        内容を勝手に消さない)。実行の途中 (セッションがある間) は
        差し替えない (アオイの指摘 req-20261114-22: ステップ実行の
        途中でライブ表示が本文を書き換えた)。"""
        if self._live_source is None or self._running or self._live_dirty:
            return
        if self._session is not None or self._run_begun:
            return
        try:
            source = self._live_source()
        except Exception:  # noqa: BLE001  呼び側の都合で画面を落とさない
            return
        if source is None or source == self._live_synced:
            return
        self._live_synced = source
        self._syncing_live = True
        try:
            self._edit.setPlainText(source)
            cursor = self._edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._edit.setTextCursor(cursor)
        finally:
            self._syncing_live = False

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
            elif session.stopped:
                parts.append("止めました")
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
        self._run_button.setEnabled(can_run and not self._running)
        self._step_button.setEnabled(can_run and not self._running)

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

    def closeEvent(self, event):  # noqa: N802  Qt の上書き
        # 実行中は閉じない(実行中のセッションを抱えたまま画面が
        # 壊れるのを防ぐ)。先に停止してもらう。
        if self._running:
            event.ignore()
            self._status_label.setText(
                "実行中です。停止してから閉じてください")
            return
        # done を通らない閉じ方(窓の×・親の窓からの close 等)
        # でも、セッションの子を残さない。
        self._cleanup_session()
        super().closeEvent(event)

    def done(self, result):
        # 閉じるときに実行区間が開いたままなら、呼び側へ返す。
        self._cleanup_session()
        super().done(result)
