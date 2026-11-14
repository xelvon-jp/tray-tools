# editor/macro_py_dialog.py
# マクロの Python を EDDy の右ペインで見て・直して・実行する画面。
#
# できること:
#   - ソースの表示と編集(編集すると実行の途中状態は破棄される)
#   - ステップ実行(1文ずつ。本文の窓で実際に編集が進む)
#   - 通し実行(実行の途中なら、その位置から次のブレークポイント
#     か末尾まで続ける。そうでなければ、カーソルのある行の文
#     から新しく始める)
#   - ブレークポイント(行番号の横=ガターをクリックで付け外し。
#     印は文の開始行にだけ付き、複数行の文の途中をクリック
#     したら開始行へ寄せる。通し実行は、印のある文の手前で
#     止まる。ソースを編集したら、印は行の増減に追随し、
#     変更範囲の中にあった印は消える)
#   - 実行位置の移動(ステップ実行の途中にソースの行をクリック
#     すると、次に実行する文がその行の文になる。すでに実行した
#     編集は取り消さない)
#   - 停止(実行側の子プロセスを終了する。無限ループでも効く)
#   - 最初から(途中状態を捨てて頭に戻り、先頭の文を次として示す)
#   - クリップボードへコピー(職場 Copilot など AI へ貼るため)
#   - 登録として保存(on_save があるときだけ。名前とソースを渡す)
#
# 開いただけでは何も実行しない。実行は「通し実行」「ステップ
# 実行」を押したときだけ。実行そのものは editor/macro_py.py の
# PythonStepper(子プロセスで実行)が持ち、セッションは呼び側
# (窓)が session_factory で作って渡す(この画面は Qt の外の形を
# 知らない)。実行区間の undo の単位は、呼び側の begin_run /
# end_run が面倒を見る(ステップをまたいでも1回の実行は1つの
# 単位)。ただし記録中は、ステップの合間に flush_run で編集
# ブロックだけを閉じる(Qt がブロック内の変化を伏せてしまう
# ため、そのままだと合間の手作業が記録へ届かない)。その場合
# の undo はステップごとの単位になる。
#
# 実行中は、イベントを回しながら待つ(子が純粋な計算で回って
# いても画面が固まらないように)。通し実行・ステップ実行・停止
# のボタンは実行中も押せるままにする(実行中の押し直しは二重
# 実行にならず、何もしない。停止はいつでも効く)。ソースの編集
# と「最初から」だけは、実行の途中状態と競合するので実行中は
# 止める。
#
# セッションの後始末は、閉じる経路のどれでも行う(closeEvent と
# destroyed)。EDDy の窓ごと閉じられたときなど、子プロセスが
# 残るのを防ぐ(req-20261114-09 の指摘)。
#
# 次に実行する文は黄色の背景色で示す (色は1色だけにする)。
# エラーは行番号つきで下の欄に出す
# (print の出力も同じ欄)。
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QRect, QSize, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontDatabase,
    QGuiApplication,
    QPainter,
    QTextCursor,
)

from editor import macro_py

_NEXT_LINE_COLOR = QColor(255, 243, 176)
# 本文の配色(既定)の現在行の背景と同じ色。本文と同じく薄く出す。
_BREAKPOINT_COLOR = QColor(198, 40, 40)
_GUTTER_BACK_COLOR = QColor(245, 245, 245)
_GUTTER_TEXT_COLOR = QColor(120, 120, 120)

# 窓の右ペインとしての望ましい幅(窓側がスプリッタの初期配分に使う)。
PANE_WIDTH = 380


class _Gutter(QWidget):
    """ソース表示の左端の帯(行番号エリア)。

    行番号とブレークポイントの赤い印を描く。クリックは「その行の
    ブレークポイントの付け外し」として編集欄へ知らせる。描画と
    位置の計算は編集欄(_SourceEdit)側が持つ。"""

    def __init__(self, edit):
        super().__init__(edit)
        self._edit = edit

    def sizeHint(self):  # noqa: N802  Qt の上書き
        return QSize(self._edit.gutter_width(), 0)

    def paintEvent(self, event):  # noqa: N802  Qt の上書き
        self._edit.paint_gutter(event)

    def mousePressEvent(self, event):  # noqa: N802  Qt の上書き
        if event.button() == Qt.MouseButton.LeftButton:
            lineno = self._edit.gutter_line_at(event.position().y())
            if lineno is not None:
                self._edit.breakpoint_toggled.emit(lineno)
        super().mousePressEvent(event)


class _SourceEdit(QPlainTextEdit):
    """マクロの Python ソースの編集欄。

    行番号エリア(ガター)を左に持ち、ブレークポイントの印を出す。
    本文をクリックしたら行番号を知らせる(実行位置を移すかどうか
    はペイン側が判断する)。"""

    line_clicked = Signal(int)
    breakpoint_toggled = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        # ブレークポイントの行番号(1始まり)の集合。ペインと共有
        # する(ペインが持ち、ここは描くために見る)。
        self.breakpoints = set()
        self._gutter = _Gutter(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self._update_gutter_width()

    # ---------------------------------------------------------------
    # ガターの幅と追随
    # ---------------------------------------------------------------
    def gutter_width(self):
        digits = max(2, len(str(max(1, self.blockCount()))))
        # 左にブレークポイントの印の場所、右に行番号。
        return 16 + self.fontMetrics().horizontalAdvance("9") * digits + 6

    def refresh_gutter(self):
        self._gutter.update()

    def _update_gutter_width(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(
                0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):  # noqa: N802  Qt の上書き
        super().resizeEvent(event)
        rect = self.contentsRect()
        self._gutter.setGeometry(
            QRect(rect.left(), rect.top(),
                  self.gutter_width(), rect.height()))

    # ---------------------------------------------------------------
    # ガターの描画と位置
    # ---------------------------------------------------------------
    def _visible_blocks(self):
        """見えているブロックと、その表示上の縦の範囲(上, 下)を
        順に返す。"""
        offset = self.contentOffset()
        block = self.firstVisibleBlock()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            yield block, geometry.top(), geometry.bottom()
            block = block.next()

    def paint_gutter(self, event):
        """行番号とブレークポイントの印を描く。"""
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), _GUTTER_BACK_COLOR)
        painter.setFont(self.font())
        width = self._gutter.width()
        for block, top, bottom in self._visible_blocks():
            if bottom < event.rect().top():
                continue
            if top > event.rect().bottom():
                break
            if not block.isVisible():
                continue
            lineno = block.blockNumber() + 1
            if lineno in self.breakpoints:
                size = max(4, min(10, int(bottom - top) - 4))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_BREAKPOINT_COLOR)
                painter.drawEllipse(
                    3, int(top + (bottom - top - size) / 2), size, size)
            painter.setPen(_GUTTER_TEXT_COLOR)
            painter.drawText(
                0, int(top), width - 4, int(bottom - top),
                Qt.AlignmentFlag.AlignRight
                | Qt.AlignmentFlag.AlignVCenter, str(lineno))

    def gutter_line_at(self, y):
        """ガターの縦位置 y に対応する行番号(1始まり)。どの行
        でもなければ None。"""
        for block, top, bottom in self._visible_blocks():
            if top > y:
                break
            if block.isVisible() and top <= y < bottom:
                return block.blockNumber() + 1
        return None

    # ---------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802  Qt の上書き
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.line_clicked.emit(self.textCursor().blockNumber() + 1)


class MacroPythonPane(QWidget):
    """マクロ (Python) の右ペイン。

    見出しにはマクロ名を出し、名前が無ければ「新規マクロ」と
    出す。session_factory はソースを受けて PythonStepper を返す
    関数(作れなければ None)。begin_run / end_run は実行区間の
    開始と終了(呼び側が undo の単位と、実行中のタブの固定を
    持つ)。flush_run は、ステップの合間に実行区間の編集
    ブロックだけを閉じる口(区間は続く。合間の手作業が、窓側
    の記録へ届くようにするため)。on_save は (名前, ソース)
    を受ける登録の口で、無ければ保存ボタンは出ない。"""

    def __init__(self, parent=None, *, source="", name="",
                 session_factory=None, begin_run=None, end_run=None,
                 flush_run=None, on_save=None, live_source=None):
        super().__init__(parent)
        self._session_factory = session_factory
        self._begin_run = begin_run
        self._end_run = end_run
        self._flush_run = flush_run
        self._on_save = on_save
        self._session = None
        self._run_begun = False
        self._output_shown = 0
        self._running = False
        self._name = name
        self._breakpoints = set()
        self._paused_at_breakpoint = False
        # 閉じるのを断った (実行中) 印。try_close が確かめる。
        self._close_refused = False
        # ブレークポイントの追随用に覚える、文書のブロック数。
        self._bp_doc_blocks = None
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
        header = QHBoxLayout()
        self._title_label = QLabel(self._title_text())
        title_font = self._title_label.font()
        title_font.setBold(True)
        self._title_label.setFont(title_font)
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._close_button = QPushButton("閉じる")
        self._close_button.clicked.connect(self.close)
        header.addWidget(self._close_button)
        layout.addLayout(header)
        if on_save is not None:
            name_row = QHBoxLayout()
            name_row.addWidget(QLabel("名前"))
            self._name_edit = QLineEdit(name)
            name_row.addWidget(self._name_edit, 1)
            layout.addLayout(name_row)
        else:
            self._name_edit = None
        self._edit = _SourceEdit()
        self._edit.breakpoints = self._breakpoints
        self._edit.setPlainText(source)
        self._edit.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))
        # ブレークポイントの印を編集に追随させる (行の増減で
        # ずらし、変更範囲の中の印は消す)。数えの基準として、
        # 直前のブロック数を覚えておく。
        self._bp_doc_blocks = self._edit.blockCount()
        self._edit.document().contentsChange.connect(
            self._on_contents_change)
        self._edit.textChanged.connect(self._on_text_changed)
        self._edit.cursorPositionChanged.connect(
            lambda: self._refresh_highlights())
        self._edit.line_clicked.connect(self._on_line_clicked)
        self._edit.breakpoint_toggled.connect(self.toggle_breakpoint)
        layout.addWidget(self._edit, 1)

        # ボタンは2段に分ける。1段に全部並べると、その最小幅
        # (550px ほど) がペイン全体の最小幅になり、望ましい幅
        # PANE_WIDTH (380) までスプリッタで狭められなくなる
        # (アオイの実測 req-20261114-26: 既定幅が効かなかった
        # 原因はこれ。スプリッタは最小幅より小さくできない)。
        buttons = QHBoxLayout()
        self._run_button = QPushButton("通し実行")
        self._run_button.clicked.connect(self._on_run_all)
        buttons.addWidget(self._run_button)
        self._step_button = QPushButton("ステップ実行")
        self._step_button.clicked.connect(self._on_step)
        buttons.addWidget(self._step_button)
        self._stop_button = QPushButton("停止")
        self._stop_button.clicked.connect(self._on_stop)
        buttons.addWidget(self._stop_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        buttons2 = QHBoxLayout()
        self._reset_button = QPushButton("最初から")
        self._reset_button.clicked.connect(self._on_reset)
        buttons2.addWidget(self._reset_button)
        self._copy_button = QPushButton("コピー")
        self._copy_button.clicked.connect(self._on_copy)
        buttons2.addWidget(self._copy_button)
        if on_save is not None:
            self._save_button = QPushButton("登録として保存")
            self._save_button.clicked.connect(self._on_save_clicked)
            buttons2.addWidget(self._save_button)
        else:
            self._save_button = None
        buttons2.addStretch(1)
        layout.addLayout(buttons2)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        self._refresh_state("")

    # ---------------------------------------------------------------
    # 状態(テストと呼び側から使う)
    # ---------------------------------------------------------------
    def _title_text(self):
        """見出しの文。マクロ名、無ければ「新規マクロ」。"""
        return self._name if self._name else "新規マクロ"

    def source(self):
        """いまのソース(編集欄の内容)。"""
        return self._edit.toPlainText()

    def session(self):
        """実行セッション(まだ作っていなければ None)。"""
        return self._session

    def running(self):
        """実行中(ステップ・通しの最中)か。"""
        return self._running

    def breakpoints(self):
        """ブレークポイントの行番号(1始まり)の並び。"""
        return sorted(self._breakpoints)

    def toggle_breakpoint(self, lineno):
        """行番号 lineno(1始まり)のブレークポイントを付け外し
        する。

        印は文の開始行にだけ付ける。クリックした行が文の範囲
        (開始行〜終了行) の中なら、その文の開始行に付ける
        (複数行の文の途中行でも止まるように。アオイの指摘
        req-20261114-26: 開始行しか見ない実行側では、途中行の
        印は止まらなかった)。どの文の範囲にも入らない行
        (空行・文の間のコメント行) には付けない。行の範囲外
        なら何もしない。ソースの構文が読めない間は文の切り
        分けができないので、クリックした行そのもので付け外し
        する(実行に渡すときは開始行へ寄せ直す)。"""
        if not 1 <= lineno <= self._edit.blockCount():
            return
        if lineno in self._breakpoints:
            self._breakpoints.discard(lineno)
            self._edit.refresh_gutter()
            return
        lines = macro_py.statement_lines_for_source(self.source())
        if lines is None:
            target = lineno
        else:
            target = None
            for start, end in lines:
                if start <= lineno <= end:
                    target = start
                    break
            if target is None:
                return
        if target in self._breakpoints:
            self._breakpoints.discard(target)
        else:
            self._breakpoints.add(target)
        self._edit.refresh_gutter()

    def _on_contents_change(self, position, chars_removed,
                            chars_added):
        """ブレークポイントの印を、ソースの編集に追随させる。

        変更のあった範囲の中にあった印は消し、後ろの印は
        増減した行数だけずらす(アオイの指摘 req-20261114-26:
        編集すると印の行がずれたままだった)。変更の開始行は、
        行の頭が残る編集なら印も残し、行頭への行の挿入なら
        内容ごと押し下げ、行ごと消えたら印も消す。ライブ
        表示の差し替えのときは、本文ごと入れ替わるので印は
        そのままに、数えの基準だけ合わせ直す。"""
        doc = self._edit.document()
        new_blocks = doc.blockCount()
        old_blocks = self._bp_doc_blocks
        self._bp_doc_blocks = new_blocks
        if self._syncing_live or not self._breakpoints \
                or old_blocks is None:
            return
        # 変更の開始行 (0始まり)。変更より前は旧文書と共通
        # なので、新文書での行番号がそのまま旧文書の行番号。
        start_block = doc.findBlock(position).blockNumber()
        if chars_added > 0:
            added_lines = (doc.findBlock(position + chars_added)
                           .blockNumber() - start_block)
        else:
            added_lines = 0
        removed_lines = added_lines - (new_blocks - old_blocks)
        at_line_head = (
            position
            == doc.findBlockByNumber(start_block).position())
        delta = added_lines - removed_lines
        kept = set()
        for mark in self._breakpoints:
            line = mark - 1     # 0始まりで考える
            if line < start_block:
                kept.add(mark)
            elif line == start_block:
                if at_line_head and removed_lines > 0:
                    continue    # 行ごと消えた
                if at_line_head and added_lines > 0:
                    kept.add(mark + delta)  # 内容ごと押し下げる
                else:
                    kept.add(mark)          # 行の頭は残った
            elif line <= start_block + removed_lines:
                continue        # 変更範囲の中にあった (消す)
            else:
                kept.add(mark + delta)
        if kept != self._breakpoints:
            # 集合は編集欄と共有しているので、作り直さず中身を
            # 入れ替える。
            self._breakpoints.clear()
            self._breakpoints.update(kept)
            self._edit.refresh_gutter()

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
        # 呼び側は冗長に呼ばれてもよい作り (タブの固定は最初の
        # 1回だけ、編集ブロックは閉じていれば開き直す)。ステッ
        # プのたびに呼んで、_flush で閉じたブロックを開き直す。
        if self._begin_run is not None:
            self._begin_run()
            self._run_begun = True

    def _flush(self):
        """ステップの合間に、実行区間の編集ブロックだけを閉じる
        (実行区間は続く)。閉じ方は呼び側に任せる (記録中でない
        ときは閉じず、実行全体が1回の undo の単位のままになる)。"""
        if self._run_begun and self._flush_run is not None:
            self._flush_run()

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

        何度呼ばれてもよい(2回目以降は何もしない)。"""
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
        # 通し実行・ステップ実行・停止・閉じるは実行中も押せる
        # ままにする(押し直しの無効化は step_once / run_all の
        # 頭で行う)。実行の途中状態と競合するもの(ソースの編集・
        # 最初から・コピー・保存・名前)だけを実行中は止める。
        for widget in (self._reset_button, self._copy_button,
                       self._save_button, self._name_edit, self._edit):
            if widget is not None:
                widget.setEnabled(not running)

    def step_once(self) -> bool:
        """次の1文を実行する。実行したら True。"""
        if self._running:
            return False    # 実行中の押し直しは二重実行にしない
        session = self._ensure_session()
        if session is None or session.done:
            return False
        self._paused_at_breakpoint = False
        self._begin()
        self._set_running(True)
        try:
            session.step()
        except macro_py.PythonMacroError:
            self._finish()
            # エラーの文は session.error に入っていて、状態表示
            # がそちらから出す。ここで例外の文も渡すと二重に
            # 出るので、渡さない。
            self._refresh_state("")
            return False
        finally:
            self._set_running(False)
        if session.done:
            self._finish()
        else:
            # 合間に利用者の手が入れるよう、編集ブロックだけ
            # 閉じておく (次のステップで開き直す)。
            self._flush()
        self._refresh_state("")
        return not session.stopped

    def run_all(self) -> bool:
        """通し実行をする。最後まで行ったら True。

        実行の途中(セッションが残っている)なら、その位置から
        次のブレークポイントか末尾まで続ける。そうでなければ、
        カーソルのある行の文から新しく始める(複数行にまたがる
        文の途中の行にカーソルがあれば、その文の先頭から)。
        ブレークポイントで止まったときは False(まだ続きがある)。
        """
        if self._running:
            return False    # 実行中の押し直しは二重実行にしない
        session = self._session
        seek_note = ""
        if session is not None and session.done:
            # 終わっているセッションは捨てて、新しく始める。
            self._discard_session()
            session = None
        if session is None:
            session = self._ensure_session()
            if session is None:
                return False
            lineno = self._edit.textCursor().blockNumber() + 1
            index = macro_py.statement_index_for_line(
                session.statement_lines(), lineno)
            if index is None:
                # カーソルが最後の文より後ろにある。実行する文が
                # 無いので、作ったセッションは捨てて伝える。
                self._discard_session()
                self._refresh_state(
                    "カーソルより後ろに、実行する文がありません")
                return False
            if index > 0 and not session.seek(index):
                self._refresh_state("")
                return False
            if session.seek_skipped:
                seek_note = (
                    f"{session.seek_skipped} 文を飛ばしました"
                    "（変数が無いことがあります）")
        self._paused_at_breakpoint = False
        self._begin()
        self._set_running(True)
        try:
            # 印は文の開始行へ寄せてから渡す(実行側は開始行しか
            # 見ない。編集でずれた印や、構文が読めない間に付けた
            # 印をここで直す)。
            finished = session.run_until(macro_py.snap_breakpoints(
                session.statement_lines(), self._breakpoints))
        except macro_py.PythonMacroError:
            self._finish()
            # エラーの文は session.error から出る (二重に出さない)。
            self._refresh_state("")
            return False
        finally:
            self._set_running(False)
        if session.done:
            self._finish()
        else:
            self._paused_at_breakpoint = True
            # ブレークポイントで止まっている間も、合間の手作業
            # が記録へ届くよう、編集ブロックだけ閉じておく。
            self._flush()
        self._refresh_state(seek_note)
        return finished and not session.stopped

    def stop_run(self):
        """実行を止める(実行側の子プロセスを終了する)。"""
        if self._session is not None:
            self._session.stop()

    def reset_session(self, prepare=True):
        """実行の途中状態を捨てる(本文の変更は戻らない)。

        prepare が真(「最初から」ボタン)なら、先頭の文が次に
        なるようセッションを作り直して備える(作るだけでは
        実行しない)。偽なら捨てるだけにする(本文の編集から
        呼ばれる経路。編集のたびに子プロセスを作り直さない
        ため)。"""
        self._finish()
        self._discard_session()
        self._paused_at_breakpoint = False
        if not prepare:
            self._refresh_state("")
            return
        # 次に実行する文が先頭だと、黄色の印と状態表示で分かる
        # ようにする。作れないときは、理由が表示されたままに
        # なる。
        if self._ensure_session() is not None:
            self._refresh_state("")

    def _on_line_clicked(self, lineno):
        """ソースの行がクリックされたときの受け口。

        実行の途中(セッションがある間)なら、実行位置をその行の
        文へ移す(文の途中ならその文が次になる)。すでに実行した
        編集は取り消さない。セッションが無い・終わっている・
        実行中のときは、通常のカーソル移動として扱い何もしない。
        """
        if self._running:
            return
        session = self._session
        if session is None or session.done:
            return
        index = macro_py.statement_index_for_line(
            session.statement_lines(), lineno)
        if index is None:
            index = session.total   # 最後の文より後ろ = 末尾へ
        if index == session.position:
            return
        if session.seek(index):
            self._paused_at_breakpoint = False
            if session.done:
                self._finish()
            note = ""
            if session.seek_skipped:
                # 前へ飛ばした文は実行されない。その文が作る
                # はずだった変数が無いことを知らせる。
                note = (f"{session.seek_skipped} 文を飛ばしました"
                        "（変数が無いことがあります）")
            self._refresh_state(note)

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
        self._name = name
        self._title_label.setText(self._title_text())
        self._refresh_state(f"『{name}』として登録しました")

    def _on_text_changed(self):
        if self._syncing_live:
            return  # ライブ表示の差し替えは「編集」として扱わない
        self._live_dirty = True
        # 編集したら、実行の途中状態は意味を失うので捨てる。
        if self._session is not None or self._run_begun:
            self.reset_session(prepare=False)
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
                if self._paused_at_breakpoint:
                    parts.append(
                        f"{session.position} / {session.total} 文"
                        f"（{lineno} 行目のブレークポイントの手前で停止）")
                else:
                    parts.append(
                        f"{session.position + 1} / {session.total} 文"
                        f"（次は {lineno} 行目）")
        if message:
            parts.append(message)
        self._status_label.setText("\n".join(parts))
        self._refresh_highlights(move_caret=True)

    def _refresh_highlights(self, move_caret=False):
        """次に実行する文のハイライトを出し直す。

        色は黄色だけにする (カーソル行の水色は、2色あると
        分かりづらいという Nagato の指摘で外した)。move_caret
        が真なら、次の文が見えるよう編集欄のカーソルもその
        先頭へ寄せる(状態の更新のときだけ。利用者のカーソル
        移動に追随するだけのときは寄せない)。"""
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
                    if move_caret:
                        # 次の文が見えるよう、編集欄のカーソルも
                        # 寄せる。
                        caret = self._edit.textCursor()
                        caret.setPosition(first.position())
                        self._edit.setTextCursor(caret)
        self._edit.setExtraSelections(selections)

    def try_close(self):
        """閉じてみて、閉じたら True を返す。

        実行中は閉じるのを断る (closeEvent) ので、その場合は
        False。呼び側が「閉じたつもり」で新しいペインを作ると、
        実行中のペインへの参照を失い、子プロセスが残るおそれ
        があるため、確かめる口を用意する (アオイの指摘
        req-20261114-26)。"""
        self._close_refused = False
        self.close()
        return not self._close_refused

    def closeEvent(self, event):  # noqa: N802  Qt の上書き
        # 実行中は閉じない(実行中のセッションを抱えたまま画面が
        # 壊れるのを防ぐ)。先に停止してもらう。
        if self._running:
            self._close_refused = True
            event.ignore()
            self._status_label.setText(
                "実行中です。停止してから閉じてください")
            return
        # どの経路で閉じても、セッションの子を残さない。
        self._cleanup_session()
        super().closeEvent(event)


# ペイン化する前の名前(別窓だった頃の呼び名)。既存の import と
# テストの互換のために残す。
MacroPythonDialog = MacroPythonPane
