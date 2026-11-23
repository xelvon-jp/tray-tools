# -*- coding: utf-8 -*-
"""AI対話モードの対話パネル（PySide6）。1列の小さな画面（幅 約400px）。表示と操作だけ。
判定は ui_dialogue_logic、結線は dialogue_controller、下書き欄は ui_draft.DraftView。

上から: 1行ヘッダー（宛先・選び直す・送信までする・⚙）／下書き欄（主役。認識中の波形→白）／未配達の行（あるときだけ）／
下段（確定して届ける・全部消す・1行の通知・保留・セッション）。録音中の表示・経過時間・音量メーター・マイクの指摘は
メイン窓の上段にあるので、ここには書かない。確定履歴の一覧は⚙メニューから開く別ダイアログ。

スレッド: controller の on_event は作業・配達・ヘッドセットのスレッドから来るので、Qt シグナル（QueuedConnection）で
メインスレッドへ渡す。宛先を選ぶ・セッションの終了は別スレッド（画面を止めない）。全スロットは safe() で包み、
例外で落とさない（PySide6 は未処理例外でプロセスごと落ちる）。
モーダルなダイアログは _ask_text / _ask_end_choice / _confirm / _warn_box / _info_box に集めてあり、テストでは差し替える。
クリップボード: このパネルが文字をクリップボードへ入れることはしない（入れてよいのは、利用者が明示的に Ctrl+C／右クリック→コピーを
したときだけ）。未配達の文・確定履歴・診断は、選択してコピーできるテキストで見せる。
"""
from __future__ import annotations

import functools
import logging
import threading
import time

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget, QComboBox)

import ui_dialogue_logic as L
import ui_icons as I
import ui_style as S
from ui_draft import DraftView
from ui_widgets import CheckBox

log = logging.getLogger("kabeuchi")

TONE_COLOR = {"muted": S.MUTED, "rec": S.REC, "accent": S.ACCENT, "warn": S.ORANGE, "ok": S.OKGREEN}
FADED_COLOR = S.CTRL_DISABLED


def safe(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        try:
            return fn(self, *a, **kw)
        except Exception:
            log.exception("対話パネルのスロット %s で例外", fn.__name__)
            try:
                self.set_notice(f"エラーが起きました（error.log を見てください）: {fn.__name__}", "warn")
            except Exception:
                pass
    return wrapper


class _Bridge(QObject):
    event = Signal(str, object)
    done = Signal(str, object)      # 別スレッドの仕事の完了（end など）


def make_und_text() -> QPlainTextEdit:
    """未配達の文を見せる読み取り専用の欄。選択して Ctrl+C／右クリック→コピーできる（ボタンでの自動コピーはしない）。"""
    t = QPlainTextEdit()
    t.setReadOnly(True)
    t.setMaximumHeight(64)
    t.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
    t.setToolTip("届かなかった文です。選択して Ctrl+C（または右クリック→コピー）で、手で貼れます")
    return t


class DiagnosticDialog(QDialog):
    """接続の状態（診断）。読み取り専用の欄に文字を出す。選択して Ctrl+C／右クリック→コピーできる（AI にそのまま渡せるように）。
    クリップボードへ自動では入れない・コピーのボタンも付けない。"""

    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(440, 340)
        lay = QVBoxLayout(self)
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.txt.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.txt.setPlainText(text)
        lay.addWidget(self.txt, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_close = QPushButton("閉じる")
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_close)
        lay.addLayout(row)


class HistoryDialog(QDialog):
    """確定履歴（確定メッセージ・時刻・送信済み/未送信/未確認・記録のみ）。読み取り専用（選択して Ctrl+C でコピーできる）。
    開いている間は1秒ごとに更新する。"""

    def __init__(self, get_messages, parent=None):
        super().__init__(parent)
        self.setWindowTitle("確定したメッセージ")
        self.resize(420, 420)
        self._get = get_messages
        self._key = None
        lay = QVBoxLayout(self)
        self.lst = QListWidget()
        self.lst.setMaximumHeight(170)
        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setPlaceholderText("上の一覧から選ぶと、確定した全文がここに出ます（選択して Ctrl+C でコピーできます）")
        lay.addWidget(self.lst)
        lay.addWidget(self.txt, 1)
        self._msgs: list = []
        self.lst.currentRowChanged.connect(self._show_row)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.refresh()

    def refresh(self) -> None:
        msgs = list(self._get() or [])
        key = [(m.id, m.status, getattr(m, "sent", ""), getattr(m, "stage", "")) for m in msgs]
        if key == self._key:
            return
        self._key = key
        self._msgs = msgs
        cur = self.lst.currentRow()
        self.lst.blockSignals(True)
        self.lst.clear()
        for r in L.history_rows(msgs):
            it = QListWidgetItem(f"{r['time']}  [{r['status']}]  {r['snippet']}")
            it.setToolTip(r["text"])
            self.lst.addItem(it)
        self.lst.blockSignals(False)
        if self.lst.count():
            self.lst.setCurrentRow(cur if 0 <= cur < self.lst.count() else self.lst.count() - 1)
        self._show_row(self.lst.currentRow())

    def _show_row(self, i: int) -> None:
        if not (0 <= i < len(self._msgs)):
            self.txt.setPlainText("")
            return
        self.txt.setPlainText(self._msgs[i].text)

    def done(self, r) -> None:
        self.timer.stop()
        super().done(r)


class DialoguePanel(QWidget):
    def __init__(self, controller, parent=None, on_started=None, on_settings=None):
        super().__init__(parent)
        self.ctl = controller
        self.on_started = on_started        # セッション開始後に呼ぶ（メイン窓が画面を結び直す）
        self.on_settings = on_settings      # ⚙ → 設定…（メイン窓の設定ダイアログを開く）
        self.setMinimumWidth(300)
        self._pick_name = ""
        self._ending = False
        self._rev = -1                      # 下書き欄が追いついているバッファの版
        self._buf_text = ""                 # その版でのバッファの確定済み部分（差分＝追記分の計算元）
        self._dirty = False                 # 手修正をバッファへ渡せていない（版の食い違い）
        self._events: list[tuple[str, dict]] = []  # 直近のイベント（テスト・診断用）
        self._notice = L.NoticeLine()
        self._history_dlg: HistoryDialog | None = None
        self.bridge = _Bridge()
        self.bridge.event.connect(self._on_event, Qt.QueuedConnection)
        self.bridge.done.connect(self._on_done, Qt.QueuedConnection)
        controller.set_event_sink(lambda n, i: self.bridge.event.emit(n, i))
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.fill_presets()
        self.refresh()

    # ---- モーダルの出入口（テストでは差し替える）
    def _ask_text(self, title, label, default=""):
        t, ok = QInputDialog.getText(self, title, label, text=default)
        return t if ok else None

    def _confirm(self, title, text) -> bool:
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes

    def _warn_box(self, title, text) -> None:
        QMessageBox.warning(self, title, text)

    def _info_box(self, title, text) -> None:
        """情報の窓。文字は選択してコピーできる（診断などをそのまま AI に渡せるように）。"""
        box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, self)
        box.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        lab = box.findChild(QLabel, "qt_msgbox_label")
        if lab is not None:
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        box.exec()

    def _show_diag(self, title, text) -> None:
        """診断の窓（選択してコピーできる文字）。テストでは差し替える。"""
        DiagnosticDialog(title, text, self).exec()

    def _ask_end_choice(self, report: dict) -> str:
        """セッション終了時に未配達／下書きが残っているとき。"discard" / "keep" を返す。
        既定では確認の窓を出さず、残す（"keep"。捨てない・次のセッションへ持ち越す）。dialogue.ask_on_end を True にすると窓を出す。"""
        if not bool(self.ctl.cfg.get("ask_on_end", False)):
            return "keep"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("届いていない内容があります")
        parts = []
        if report.get("undelivered_count"):
            parts.append(f"未配達 {report['undelivered_count']} 件")
        if report.get("draft_text"):
            parts.append("確定していない下書き")
        box.setText("・".join(parts) + " が残っています。\n破棄するか、残す（次のセッションへ持ち越す）かを選んでください。")
        b_discard = box.addButton("破棄", QMessageBox.DestructiveRole)
        box.addButton("あとで（残す）", QMessageBox.RejectRole)
        box.exec()
        c = box.clickedButton()
        return "discard" if c is b_discard else "keep"

    # ---- 画面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        # 1行ヘッダー: 宛先（プリセット）・選び直す・送信までする・⚙
        head = QHBoxLayout()
        head.setSpacing(6)
        self.cmb_preset = QComboBox()
        self.cmb_preset.setMinimumWidth(90)
        self.cmb_preset.setToolTip("宛先（AI アプリなどの入力欄）のプリセット")
        self.btn_repick = QPushButton("選び直す")
        self.btn_repick.setToolTip("入力欄をクリックして、宛先を取り直します（名前を付けて保存）")
        self.chk_send = CheckBox("送信までする")
        self.chk_send.setToolTip("入力欄へ届けて読み戻しで確認できたあと、送信ボタン（無ければ Enter）まで押します。"
                                 "ヘッドセットのボタン・Ctrl+Enter・ホットキーも同じ動きになります")
        self.btn_menu = QToolButton()
        self.btn_menu.setIcon(I.make_icon("gear"))
        self.btn_menu.setIconSize(QSize(16, 16))
        self.btn_menu.setToolTip("設定・確定履歴・接続の状態（診断）")
        self.btn_menu.setPopupMode(QToolButton.InstantPopup)
        self.btn_menu.setCursor(Qt.PointingHandCursor)
        self.btn_menu.setStyleSheet(
            f"QToolButton {{ background: {S.PANEL2}; border: 1px solid {S.BORDER}; border-radius: 8px; padding: 6px 8px; }}"
            f" QToolButton:hover {{ border-color: {S.ACCENT}; }} QToolButton::menu-indicator {{ image: none; width: 0px; }}")
        self.menu = QMenu(self.btn_menu)
        self.act_settings = QAction("設定…", self.menu)
        self.act_history = QAction("確定履歴を開く…", self.menu)
        self.act_headset = QAction("接続の状態（診断）…", self.menu)
        self.act_del_preset = QAction("この宛先のプリセットを削除…", self.menu)
        for a in (self.act_settings, self.act_history, self.act_headset):
            self.menu.addAction(a)
        self.menu.addSeparator()
        self.menu.addAction(self.act_del_preset)
        self.btn_menu.setMenu(self.menu)
        head.addWidget(self.cmb_preset, 1)
        head.addWidget(self.btn_repick)
        head.addWidget(self.chk_send)
        head.addWidget(self.btn_menu)
        root.addLayout(head)
        # 警告（あるときだけ。宛先の窓が見つからない／ヘッドセットを受け付けられない）
        self.lbl_warn = QLabel("")
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setStyleSheet(f"color: {S.ORANGE}; font-weight: bold;")
        self.lbl_warn.setVisible(False)
        root.addWidget(self.lbl_warn)
        self.lbl_pick = QLabel("")           # 宛先を選んでいる間の案内（クリック待ち）
        self.lbl_pick.setWordWrap(True)
        self.lbl_pick.setStyleSheet(f"color: {S.ORANGE}; font-weight: bold;")
        self.lbl_pick.setVisible(False)
        root.addWidget(self.lbl_pick)

        # 下書き欄（主役）
        self.txt_draft = DraftView()
        self.txt_draft.setMinimumHeight(150)
        self.txt_draft.setPlaceholderText("セッションを始めると、話した内容がここに出ます。認識中（波形）→白（清書）。"
                                          "キーボードで直せます。確定するまで入力先へは書きません")
        self.act_history_ctx = QAction("確定履歴を開く…", self)
        self.txt_draft.extra_actions = [self.act_history_ctx]
        root.addWidget(self.txt_draft, 1)

        # 未配達（あるときだけ）
        self.box_und = QFrame()
        self.box_und.setObjectName("dlgFrame")
        self.box_und.setAttribute(Qt.WA_StyledBackground, True)
        self.box_und.setStyleSheet(f"#dlgFrame {{ background: {S.WARN_BG}; border: 1px solid {S.ORANGE}; border-radius: 8px; }}"
                                   f" #dlgFrame QLabel {{ background: transparent; }}")
        ul = QVBoxLayout(self.box_und)
        ul.setContentsMargins(8, 6, 8, 6)
        ul.setSpacing(4)
        self.lbl_und = QLabel("")
        self.lbl_und.setWordWrap(True)
        self.lbl_und.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_und.setStyleSheet(f"color: {S.ORANGE}; font-weight: bold;")
        self.txt_und = make_und_text()
        ur = QHBoxLayout()
        ur.setSpacing(6)
        self.btn_retry = QPushButton("再試行")
        self.btn_discard = QPushButton("破棄")
        self.btn_discard.setObjectName("danger")
        self.btn_und_repick = QPushButton("宛先を選び直す")
        for b in (self.btn_retry, self.btn_discard, self.btn_und_repick):
            ur.addWidget(b)
        ul.addWidget(self.lbl_und)
        ul.addWidget(self.txt_und)
        ul.addLayout(ur)
        self.box_und.setVisible(False)
        root.addWidget(self.box_und)

        # 下段: 確定して届ける・全部消す／1行の通知・保留・セッション
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.btn_commit = QPushButton("確定して届ける")
        self.btn_commit.setObjectName("primary")
        self.btn_draft_discard = QPushButton("全部消す")
        foot.addWidget(self.btn_commit, 1)
        foot.addWidget(self.btn_draft_discard)
        root.addLayout(foot)
        low = QHBoxLayout()
        low.setSpacing(6)
        self.lbl_notice = QLabel("")
        self.lbl_notice.setWordWrap(True)            # 長い警告（送信できなかった等）が切れないように折り返す
        self.btn_hold = QPushButton("保留")
        self.btn_hold.setCheckable(True)
        self.btn_hold.setToolTip("保留: 取り込みを止めます（確定済みのものには影響しません）")
        self.btn_session = QPushButton("セッション開始")
        self.btn_session.setObjectName("recSmall")
        low.addWidget(self.lbl_notice, 1)
        low.addWidget(self.btn_hold)
        low.addWidget(self.btn_session)
        root.addLayout(low)

        self.chk_send.toggled.connect(lambda on: self.set_send_after_deliver(on))
        self.txt_draft.textChanged.connect(self._on_draft_edited)
        self.txt_draft.commitRequested.connect(lambda: self.commit("window"))
        self.txt_draft.aboutToEdit.connect(self._before_edit)
        self.btn_session.clicked.connect(lambda: self.toggle_session())
        self.btn_hold.clicked.connect(lambda: self.toggle_hold())
        self.btn_commit.clicked.connect(lambda: self.commit("window"))
        self.btn_draft_discard.clicked.connect(lambda: self.discard_draft())
        self.btn_repick.clicked.connect(lambda: self.repick())
        self.btn_und_repick.clicked.connect(lambda: self.repick())
        self.cmb_preset.activated.connect(lambda i: self.preset_chosen())
        self.btn_retry.clicked.connect(lambda: self.retry())
        self.btn_discard.clicked.connect(lambda: self.discard_undelivered())
        self.act_settings.triggered.connect(lambda: self.open_settings())
        self.act_history.triggered.connect(lambda: self.open_history())
        self.act_history_ctx.triggered.connect(lambda: self.open_history())
        self.act_headset.triggered.connect(lambda: self.show_headset_status())
        self.act_del_preset.triggered.connect(lambda: self.delete_preset())

    # ---- 表示の更新
    def set_notice(self, text: str, tone: str = "muted") -> None:
        """下段の1行の通知。成功は時間で薄くなって消える。警告は残る（次の通知まで）。"""
        self._notice.post(text, tone, time.monotonic())
        self._render_notice()

    def _render_notice(self) -> None:
        snap = self.ctl.snapshot()
        text, tone, faded = self._notice.view(time.monotonic(), L.idle_hint(snap, methods=self.ctl.commit_method_names()))
        if self.lbl_notice.text() != text:
            self.lbl_notice.setText(text)
            self.lbl_notice.setToolTip(text)
        self.lbl_notice.setStyleSheet(f"color: {FADED_COLOR if faded else TONE_COLOR.get(tone, S.MUTED)};")

    def notice_text(self) -> str:
        """いま下段の通知に出ている文言（テスト・診断用）。"""
        return self._notice.view(time.monotonic(), L.idle_hint(self.ctl.snapshot(), methods=self.ctl.commit_method_names()))[0]

    def fill_presets(self) -> None:
        names = self.ctl.preset_names()
        cur = (self.ctl.profile or {}).get("name", "")
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        self.cmb_preset.addItem("（宛先を選ぶ）", "")
        for n in names:
            self.cmb_preset.addItem(n, n)
        i = self.cmb_preset.findData(cur)
        self.cmb_preset.setCurrentIndex(max(0, i))
        self.cmb_preset.blockSignals(False)

    def _warn_text(self) -> str:
        tv = self.ctl.target_view()
        parts = [L.target_header_warn(tv.state)]
        wanted = bool(self.ctl.cfg.get("headset_button"))
        parts.append(L.headset_header_warn(self.ctl.headset_state(), wanted, self.ctl.headset_reason))
        return "　".join(p for p in parts if p)

    def refresh(self) -> None:
        snap = self.ctl.snapshot()
        active = bool(snap.get("active"))
        picking = self.ctl.picking
        warn = self._warn_text()
        if self.lbl_warn.text() != warn:
            self.lbl_warn.setText(warn)
        self.lbl_warn.setVisible(bool(warn))
        self._refresh_session_buttons(snap, active)
        self._apply_commit_button(snap, L.press_action(snap))
        self.btn_repick.setText("選び直す" if self.ctl.profile else "宛先を選ぶ")
        self.btn_repick.setEnabled(not picking)
        act_del = getattr(self, "act_del_preset", None)       # 統合 GUI には無い（プリセットの削除は画面から外した）
        if act_del is not None:
            act_del.setEnabled(bool(self.ctl.profile))
        self.cmb_preset.setEnabled(not picking)
        want_send = bool(self.ctl.cfg.get("send_after_deliver"))
        if self.chk_send.isChecked() != want_send:      # 設定画面からの変更にも追従する
            self.chk_send.blockSignals(True)
            self.chk_send.setChecked(want_send)
            self.chk_send.blockSignals(False)
        self._sync_draft()
        self.btn_draft_discard.setEnabled(bool(self.txt_draft.final_text().strip() or self.txt_draft.tail_blocks()))
        self._fill_und(snap)
        self._render_notice()

    def _refresh_session_buttons(self, snap: dict, active: bool) -> None:
        """［セッション開始／終了］と［保留］の見た目（統合 GUI の UnifiedPanel は録音ボタン側にあるので差し替える）。"""
        self.btn_session.setText("セッション終了" if active else "セッション開始")
        self.btn_session.setObjectName("recStop" if active else "recSmall")
        self.btn_session.style().unpolish(self.btn_session)
        self.btn_session.style().polish(self.btn_session)
        self.btn_session.setEnabled(not self._ending)
        self.btn_hold.setEnabled(active)
        self.btn_hold.setChecked(bool(snap.get("held")))
        self.btn_hold.setText("保留を解除" if snap.get("held") else "保留")

    def _apply_commit_button(self, snap: dict, act: str) -> None:
        """［確定して届ける］の文言・有効/無効（統合 GUI では文言を差し替える）。"""
        self.btn_commit.setEnabled(act in (L.PRESS_COMMIT, L.PRESS_RETRY, L.PRESS_EMPTY))
        self.btn_commit.setText("未配達を再試行" if act == L.PRESS_RETRY else "確定して届ける")
        self.btn_commit.setToolTip(L.PRESS_MESSAGE[act] + "（下書き欄で Ctrl+Enter でも確定）")

    # ---- 下書き欄（1つの編集可能な欄）とバッファの同期
    def _buffer(self):
        ds = self.ctl.ds
        return ds.buffer if ds is not None else None

    def _sync_draft(self) -> None:
        """バッファの下書き（確定済み部分）と、清書待ちの発話の波形（認識中）が変わっていたら、欄へ追いつく。
        手修正が未反映なら、追いついたあと渡し直す。"""
        buf = self._buffer()
        ed = self.txt_draft
        ed.setReadOnly(buf is None)
        if buf is None:
            if ed.final_text():
                ed.set_final_text("")
            ed.apply_tail_ops(L.plan_tail(ed.tail_blocks(), []))
            self._rev, self._buf_text, self._dirty = -1, "", False
            return
        rev = buf.draft_rev
        if rev != self._rev:
            new = buf.draft_final_text
            op, cut, text = L.plan_draft_sync(self._buf_text, new)
            if op in ("append", "patch"):
                ed.append_final(text, cut)
            elif op == "replace":
                ed.set_final_text(text)
            self._rev, self._buf_text = rev, new
        if self._dirty:
            self._push_edit()
        self._sync_tail()

    def _sync_tail(self) -> None:
        ds = self.ctl.ds
        waves = ds.pending_waves() if ds is not None else []
        ops = L.plan_tail(self.txt_draft.tail_blocks(), L.build_tail(waves))
        if ops:
            self.txt_draft.apply_tail_ops(ops)

    @safe
    def _before_edit(self) -> None:
        """利用者が直す操作の直前: 欄をバッファへ追いつかせてから、構造（確定済み／認識中の波形）を判定させる。"""
        if self._buffer() is not None:
            self._sync_draft()

    @safe
    def _on_draft_edited(self) -> None:
        if self.txt_draft.programmatic:
            return
        self._push_edit()

    def _push_edit(self) -> bool:
        """下書き欄の確定済み部分をバッファへ渡す（手修正。以後の認識結果はこの内容の末尾へ足される）。
        見ていない認識結果が先に届いていたとき（版の食い違い）は渡さず、次の同期で取り込んでからやり直す。"""
        ds = self.ctl.ds
        buf = self._buffer()
        if ds is None or buf is None:
            return False
        ok = ds.set_draft_text(self.txt_draft.final_text(), base_rev=self._rev)
        self._dirty = not ok
        if ok:
            self._rev, self._buf_text = buf.draft_rev, buf.draft_final_text
        return ok

    def flush_edit(self) -> None:
        """確定の直前: 下書き欄の現在の内容を必ずバッファへ反映する（確定は欄の内容を送る）。"""
        for _ in range(3):
            self._sync_draft()
            if not self._dirty and self._push_edit():
                return

    def _fill_und(self, snap: dict) -> None:
        buf = self._buffer()
        und = L.undelivered_rows(buf.undelivered) if buf is not None else []
        self.box_und.setVisible(bool(und))
        if und:
            last = und[-1]
            self.lbl_und.setText(f"未配達 {len(und)}件（内容は消えません）: {last['reason']}")
            text = "\n".join(r["text"] for r in und)         # 選択して Ctrl+C／右クリック→コピーできる（自動ではコピーしない）
            if self.txt_und.toPlainText() != text:
                self.txt_und.setPlainText(text)

    @safe
    def _tick(self) -> None:
        if self.ctl.picking:
            self.lbl_pick.setVisible(True)
            self.lbl_pick.setText(L.pick_guide_text(self.ctl.pick_remaining()))
        elif self.lbl_pick.isVisible():
            self.lbl_pick.setVisible(False)
        self.refresh()

    # ---- コントローラのイベント（メインスレッド）
    @safe
    def _on_event(self, name: str, info: dict) -> None:
        self._events.append((name, info))
        del self._events[:-50]
        if name == "picked":
            self._after_picked(info)
        elif name == "pick_failed":
            self.set_notice(info.get("text") or "宛先を選べませんでした", "warn")
        elif name == "headset_failed":
            self.set_notice("ヘッドセットボタンを受け付けられません: " + str(info.get("reason", "")), "warn")
        elif name == "mouse_failed":
            self.set_notice("マウスのボタンを受け付けられません: " + str(info.get("reason", "")), "warn")
        elif name == "click_failed":
            self.set_notice("舌打ちの検出を始められません: " + str(info.get("reason", "")), "warn")
        elif name == "headset_press":
            # 押した瞬間に必ず反応を出す（このあと pressed で、確定／再試行／空 のどれになったかに置き換わる）
            self.set_notice("ヘッドセットのボタンを受け取りました", "accent")
        elif name == "pressed":
            text, tone = L.press_notice(str(info.get("source", "")), str(info.get("action", "")))
            if text:
                self.set_notice(text, tone)
        elif name == "undelivered":
            self.set_notice("届けられませんでした: " + L.reason_text(str(info.get("reason", ""))) + "（内容は保持しています）", "warn")
        elif name == "delivered":
            self.set_notice(*L.delivered_short(info, time.strftime("%H:%M")))
        elif name == "recognition_error":
            self.set_notice("認識に失敗した発話があります（音声は WAV に残っています）", "warn")
        elif name == "stopped":
            self._on_stopped(info.get("report") or {})
        elif name == "target":
            self.fill_presets()
        self.refresh()

    @safe
    def _on_done(self, name: str, info) -> None:
        if name == "end":
            self._ending = False
        self.refresh()

    # ---- 操作
    @safe
    def toggle_session(self) -> None:
        if self.ctl.active:
            self.end_session()
        else:
            self.start_session()

    def start_session(self) -> None:
        if self.ctl.picking:
            self.set_notice("宛先の選択中です。終えてから始めてください", "warn")
            return
        if not self.ctl.profile and not getattr(self.ctl, "record_only", False):
            self.set_notice("宛先が未設定です。届け先が決まるまで、確定した内容は未配達として保持されます", "warn")
        try:
            self.ctl.start()
        except Exception as e:
            log.exception("対話セッションを開始できません")
            self._warn_box("セッションを開始できません", f"{e}\n\n通常の録音中でないか、マイクの接続を確認してください。")
            return
        if self.on_started is not None:
            try:
                self.on_started()
            except Exception:
                log.exception("セッション開始後の画面の結び直し")
        self.set_notice("セッションを始めました（確定するまで入力先へは書きません）", "ok")
        self.refresh()

    def end_session(self) -> None:
        """終了は別スレッドで（残りの認識・配達を待つため）。終わったら "stopped" イベントで確認へ。"""
        if self._ending:
            return
        self._ending = True
        self.set_notice("セッションを終えています（清書が終わっていない発話があれば、その終わりを待ちます）…", "muted")

        def run():
            try:
                self.ctl.stop()
            except Exception:
                log.exception("対話セッションの終了")
            finally:
                self.bridge.done.emit("end", None)
        threading.Thread(target=run, name="kabeuchi-dialogue-end", daemon=True).start()

    def _on_stopped(self, report: dict) -> None:
        self._ending = False
        pend = int((report or {}).get("pending_recognitions") or 0)
        if pend:      # 清書が 60 秒では終わらなかった発話: 終わり次第、下書きへ追加される
            self.set_notice(f"清書が終わっていない発話が {pend} 件あります。終わり次第、下書きへ追加します", "warn")
        if not (report or {}).get("needs_attention"):
            if not pend:
                self.set_notice("セッションを終えました", "muted")
            return
        choice = self._ask_end_choice(report)
        out = self.ctl.ops().resolve_end(choice, report)
        if choice == "discard":
            self.set_notice(f"破棄しました（未配達 {out['discarded']} 件）", "muted")
        elif not pend:
            self.set_notice("残しました（次のセッションへ持ち越します。下書きは白い文字で出ます）", "warn")

    @safe
    def set_send_after_deliver(self, on: bool) -> None:
        """［送信までする］。設定 dialogue.send_after_deliver と連動（保存する）。次の配達から効く。"""
        self.ctl.set_option("send_after_deliver", bool(on))
        self.set_notice("確定したら、入力先へ届けて送信まで行います" if on else "確定したら、入力先へ届けるだけにします（送信はしません）",
                        "muted")

    @safe
    def toggle_hold(self) -> None:
        if self.ctl.active:
            self.ctl.hold()
        self.refresh()

    @safe
    def commit(self, source: str = "window") -> str:
        self.flush_edit()
        act = self.ctl.commit(source)
        self.refresh()
        return act

    def commit_from_hotkey(self) -> None:
        """グローバルホットキー（メインスレッド側のスロットから呼ばれる）。"""
        self.commit("hotkey")

    @safe
    def discard_draft(self) -> None:
        if self.ctl.ds is not None and (self.ctl.ds.buffer.has_draft or self.txt_draft.final_text().strip()
                                        or self.txt_draft.tail_blocks()):
            if self._confirm("下書きを全部消す", "確定前の下書きを全部消します。よろしいですか？"):
                self.ctl.ds.discard_draft()
                self._sync_draft()
        self.refresh()

    # 未配達
    def _ops(self):
        return self.ctl.ops()

    @safe
    def retry(self) -> None:
        if self.ctl.ds is None:
            return
        if not self._ops().retry():
            self.set_notice("再試行できません（セッション中で、配達中でないときだけ）", "warn")
        self.refresh()

    @safe
    def discard_undelivered(self) -> None:
        if self.ctl.ds is None:
            return
        if self._confirm("未配達を破棄", "未配達すべてを破棄します（確定履歴にはテキストが残ります）。よろしいですか？"):
            n = self._ops().discard(None)
            self.set_notice(f"破棄しました（{n} 件）", "muted")
        self.refresh()

    # ⚙メニュー
    @safe
    def open_settings(self) -> None:
        if self.on_settings is not None:
            self.on_settings()

    @safe
    def open_history(self) -> None:
        """確定履歴を別ダイアログで開く（開いたままでも使える）。"""
        d = self._history_dlg
        if d is None or not d.isVisible():
            d = self._history_dlg = HistoryDialog(
                lambda: (self.ctl.ds.buffer.history if self.ctl.ds is not None else []), self)
        self._show_history(d)

    def _show_history(self, d: HistoryDialog) -> None:
        d.show()
        d.raise_()

    @safe
    def show_headset_status(self) -> None:
        """接続の状態（診断）: ヘッドセットの独占・マウスの戻るボタン・舌打ちの状態と、診断ログの場所を見せる。"""
        from dialogue_log import log_path
        wanted = bool(self.ctl.cfg.get("headset_button"))
        text = L.headset_status_text(self.ctl.headset_diagnostics(), wanted)
        text += "\n" + L.mouse_status_line(self.ctl.mouse_diagnostics(), str(self.ctl.cfg.get("mouse_button") or ""))
        text += "\n" + L.click_status_line(self.ctl.click_diagnostics(), bool(self.ctl.cfg.get("click_commit")))
        self._show_diag("接続の状態（診断）", text + f"\n\n診断ログ: {log_path()}")

    # 宛先
    @safe
    def preset_chosen(self) -> None:
        name = self.cmb_preset.currentData()
        if name:
            self.ctl.select_preset(str(name))
        self.refresh()

    @safe
    def delete_preset(self) -> None:
        name = (self.ctl.profile or {}).get("name", "")
        if name and self._confirm("プリセットを削除", f"プリセット「{name}」を削除します。よろしいですか？"):
            self.ctl.delete_preset(name)
            self.fill_presets()
        self.refresh()

    @safe
    def pick_target(self, name: str = "") -> None:
        self._pick_name = name
        if not self.ctl.pick_async(name, exclude_roots=self._own_roots()):
            self.set_notice("宛先を選べません（選択中、または終了処理中です）", "warn")
        else:
            self.lbl_pick.setVisible(True)
            self.lbl_pick.setText(L.pick_guide_text(self.ctl.pick_remaining()))
        self.refresh()

    @safe
    def repick(self) -> None:
        self.pick_target((self.ctl.profile or {}).get("name", ""))

    def _own_roots(self) -> list[int]:
        try:
            return [int(self.window().winId())]
        except Exception:
            return []

    def _after_picked(self, info: dict) -> None:
        """クリックで取れた宛先を、名前を付けて保存する。不正な名前・同名は確認して聞き直す。"""
        prof = info.get("profile") or {}
        default = self._pick_name or prof.get("name") or ""
        for _ in range(5):
            name = self._ask_text("宛先に名前を付けて保存",
                                  f"{prof.get('process_name', '')} の入力欄を取得しました。\n保存する名前:", default)
            if name is None:
                self.set_notice("保存しませんでした（今回の宛先としても使いません）", "muted")
                return
            chk = L.validate_preset_name(name, self.ctl.preset_names())
            if not chk.ok:
                self._warn_box("名前を確認してください", chk.message)
                default = name
                continue
            if chk.exists and not self._confirm("上書きの確認", f"「{chk.name}」はすでにあります。上書きしますか？"):
                default = chk.name
                continue
            try:
                self.ctl.save_picked(chk.name, overwrite=chk.exists)
            except Exception as e:
                self._warn_box("保存できません", str(e))
                return
            self.fill_presets()
            self.set_notice(f"宛先「{chk.name}」を保存して使います", "ok")
            return
