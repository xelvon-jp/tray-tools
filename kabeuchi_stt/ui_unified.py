# -*- coding: utf-8 -*-
"""統合 GUI（S3「3行窓」＋F3「1列＋引き出し」）。PySide6。画面案は stage0\\mockup_gui_unify.html。

構成
- UnifiedPanel（DialoguePanel を継承）: 下書き欄（DraftView。末尾の行に、喋っている長さのバー）・未配達の行・宛先／送信までする／前置き ▾・
  確定ボタン・通知の1行。AI対話のロジック（バッファとの同期・確定・配達・未配達・宛先）は DialoguePanel のものをそのまま使い、
  見た目の組み立てと、宛先「なし（記録だけ）」・前置きの挿入・送信中ブロック・録音全体の清書の進みの表示だけを足す。
  壁打ちモードは廃止した（画面は AI対話の流れ1つ。録音ボタン1つ＝セッション開始／停止。停止すると録音全体を清書して履歴に残す）。
- UnifiedView: 録音の行（録音・保留・経過時間・マイク状態チップ・最前面・⚙・引き出し）＋ メーター ＋ UnifiedPanel
  ＋ 右の引き出し（ui_drawer.Drawer）。録音・清書・履歴・辞書の実体は MainWindow（host）にあり、この窓は状態を読んで
  表示し、操作を host へ渡すだけ。
- 判定は ui_unified_logic（純関数）、マイクチップは ui_mic_chip。

守ること: 下書きの内容を、利用者の操作なしに宛先へ入れない（確定は［確定して届ける］・Ctrl+Enter・ヘッドセット・ホットキーだけ。
無音による自動確定は無い）。画面はこの統合 GUI だけ（従来の左右2ペインは廃止）。
このツールが文字をクリップボードへ入れることはしない（入れてよいのは、利用者が明示的に Ctrl+C／右クリック→コピーをしたときだけ）。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)
from PySide6.QtGui import QAction, QFontMetrics

import ui_dialogue_logic as L
import ui_icons as I
import ui_style as S
import ui_unified_logic as U
from geometry_util import clamp_geometry, parse_geometry
from ui_dialogue import DialoguePanel, make_und_text, safe
from ui_draft import DraftView, FixedBlock, WAVE_H
from ui_drawer import DRAWER_WIDTH, TAB_HISTORY, Drawer
from ui_mic_chip import ChipPopup, MicChip
import meter_state as MS
from ui_widgets import CheckBox, MeterWidget, WrapBarLayout

log = logging.getLogger("kabeuchi")

NONE_KEY = "__none__"                  # 宛先「なし（記録だけ）」: 何も届けず、クリップボードにも入れない
NONE_LABEL = "なし（記録だけ）"
PICK_KEY = "__pick__"                 # 「入力欄を選んで宛先を追加」（コンボの末尾の操作項目）
PICK_LABEL = "＋ 入力欄を選んで追加…"
UNIFIED_DEFAULT = (424, 480)          # 引き出しを閉じて、下書きを広げていないとき（録音の行に ピン・⚙・引き出し も並ぶぶん、幅を24広げ、上段をなくしたぶん高さを詰めた）
UNIFIED_EXPANDED_DEFAULT = (424, 680)
UNIFIED_DRAWER_DEFAULT = (424 + DRAWER_WIDTH, 600)
QMAX = 16777215


def _icon_btn(icon: str, tip: str, checkable: bool = False, w: int = 34, h: int = 32) -> QPushButton:
    b = QPushButton()
    b.setObjectName("ghostIcon")
    b.setIcon(I.make_icon(icon))
    b.setIconSize(QSize(16, 16))
    b.setToolTip(tip)
    b.setAccessibleName(tip)
    b.setFixedSize(w, h)
    b.setCursor(Qt.PointingHandCursor)
    b.setCheckable(checkable)
    b.setFocusPolicy(Qt.NoFocus)
    return b


# =====================================================================================================
class UnifiedPanel(DialoguePanel):
    """下書き＋下段（AI対話の流れ）。"""
    expandedChanged = Signal(bool)

    host = None
    _expanded = False
    _force_expanded = False
    _alert_text = ""
    _alert_kind = "stop"
    _hold_speak_since = None      # 一時停止中に、喋り続けている状態になった時刻（time.monotonic）
    _alert_start = 0.0
    _alert_until = 0.0            # 停止中の発話警告を出しているあいだ（time.monotonic）。過ぎたら点滅も止める
    _done_at = -1e9               # 直近の配達成功の時刻（状態バッジの「完了」用）
    _blink_timer = None

    def __init__(self, controller, host=None, parent=None, on_started=None, on_settings=None):
        super().__init__(controller, parent, on_started=on_started, on_settings=on_settings)
        self.host = host
        self.fill_prompts()
        self._apply_dest_text()
        self._apply_draft_height()
        self.refresh()

    # ---- 画面
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)
        self.lbl_warn = QLabel("")
        self.lbl_warn.setWordWrap(True)
        self.lbl_warn.setStyleSheet(f"color: {S.ORANGE}; font-weight: bold;")
        self.lbl_warn.setVisible(False)
        root.addWidget(self.lbl_warn)
        self.lbl_pick = QLabel("")
        self.lbl_pick.setWordWrap(True)
        self.lbl_pick.setStyleSheet(f"color: {S.ORANGE}; font-weight: bold;")
        self.lbl_pick.setVisible(False)
        root.addWidget(self.lbl_pick)

        # 送信中ブロック（確定して届けるを押したあと、届いて送信できたと確かめるまで、送っている文章をそのまま見せる。
        # 編集できない。続けて喋った内容は、このブロックの下の下書きに出る）
        self.box_send = QFrame()
        self.box_send.setObjectName("sendFrame")
        self.box_send.setAttribute(Qt.WA_StyledBackground, True)
        self.box_send.setStyleSheet(f"#sendFrame {{ background: {S.PANEL2}; border: 1px solid {S.BORDER}; border-radius: 8px; }}"
                                    f" #sendFrame QLabel {{ background: transparent; }}")
        sl = QVBoxLayout(self.box_send)
        sl.setContentsMargins(8, 6, 8, 6)
        sl.setSpacing(2)
        self.lbl_send_head = QLabel(L.SEND_HEAD_SENDING)
        self.lbl_send_head.setStyleSheet(f"color: {S.MUTED}; font-weight: bold;")
        self.txt_send = FixedBlock(4)
        sl.addWidget(self.lbl_send_head)
        sl.addWidget(self.txt_send)
        self.box_send.setVisible(False)
        for _w in (self.box_send, self.lbl_send_head, self.txt_send.viewport()):
            _w.installEventFilter(self)          # 赤字の警告は、クリックで消せる
        root.addWidget(self.box_send)
        # 挿入した前置き（下書きの先頭に1回だけ入る灰色の固定ブロック。×で外せる。実際に送られる文字列の一部＝プレビューを兼ねる）
        self.box_prefix = QFrame()
        self.box_prefix.setObjectName("prefixFrame")
        self.box_prefix.setAttribute(Qt.WA_StyledBackground, True)
        self.box_prefix.setStyleSheet(f"#prefixFrame {{ background: {S.PANEL2}; border: 1px solid {S.BORDER}; border-radius: 8px; }}"
                                      f" #prefixFrame QLabel {{ background: transparent; }}")
        pl = QVBoxLayout(self.box_prefix)
        pl.setContentsMargins(8, 4, 4, 6)
        pl.setSpacing(0)
        ph = QHBoxLayout()
        ph.setSpacing(4)
        self.lbl_prefix_name = QLabel("")
        self.lbl_prefix_name.setStyleSheet(f"color: {S.MUTED}; font-weight: bold;")
        self.btn_prefix_x = QPushButton("×")
        self.btn_prefix_x.setObjectName("ghost")
        self.btn_prefix_x.setFixedSize(26, 24)
        self.btn_prefix_x.setCursor(Qt.PointingHandCursor)
        self.btn_prefix_x.setFocusPolicy(Qt.NoFocus)
        self.btn_prefix_x.setToolTip("前置きを外す（この下書きには付けません）")
        self.btn_prefix_x.setAccessibleName("前置きを外す")
        ph.addWidget(self.lbl_prefix_name, 1)
        ph.addWidget(self.btn_prefix_x)
        self.txt_prefix = FixedBlock(3)
        pl.addLayout(ph)
        pl.addWidget(self.txt_prefix)
        self.box_prefix.setVisible(False)
        root.addWidget(self.box_prefix)

        # 下書き欄（主役）。幅が狭いので、見た目は枠なしで本体の背景になじませる
        self.txt_draft = DraftView()
        self.txt_draft.setPlaceholderText("")
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
        self.txt_und = make_und_text()                 # 未配達の文（選択して Ctrl+C／右クリック→コピー。自動ではコピーしない）
        ur = QHBoxLayout()
        ur.setSpacing(6)
        self.btn_retry = QPushButton("再試行")
        self.btn_discard = QPushButton("破棄")
        self.btn_discard.setObjectName("danger")
        for b in (self.btn_retry, self.btn_discard):
            ur.addWidget(b)
        ur.addStretch(1)
        # 宛先の選び直しは、宛先のコンボ（末尾の「入力欄を選んで追加…」）でする。この行には置かない（幅のため）
        ul.addWidget(self.lbl_und)
        ul.addWidget(self.txt_und)
        ul.addLayout(ur)
        self.box_und.setVisible(False)
        root.addWidget(self.box_und)

        # 宛先・送信までする
        self.row_dest = QWidget()
        wl = QHBoxLayout(self.row_dest)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(8)
        self.lbl_dest = QLabel("宛先")
        self.cmb_preset = QComboBox()
        self.cmb_preset.setMinimumWidth(110)
        self.cmb_preset.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cmb_preset.setToolTip("宛先（AI アプリなどの入力欄）。「なし（記録だけ）」は、何も届けず・クリップボードにも入れず、"
                                   "確定した内容を記録（確定履歴）に残すだけです。"
                                   "末尾の「入力欄を選んで追加…」で、入力欄をクリックして宛先を取り直せます")
        self.btn_repick = QPushButton("選び直す", self)      # 画面には置かない（コンボの末尾が同じ操作）
        self.btn_repick.setVisible(False)
        self.chk_send = CheckBox("送信までする")
        self.chk_send.setToolTip("入力欄へ届けて読み戻しで確認できたあと、送信ボタン（無ければ Enter）まで押します。"
                                 "ヘッドセットのボタン・Ctrl+Enter・ホットキーも同じ動きになります")
        # 前置き ▾: 選ぶと、いまの下書きの先頭に前置きが1回だけ入る（自動では付けない。選択は記憶しない）
        self.btn_prefix = QToolButton()
        self.btn_prefix.setText("前置き ▾")
        self.btn_prefix.setPopupMode(QToolButton.InstantPopup)
        self.btn_prefix.setCursor(Qt.PointingHandCursor)
        self.btn_prefix.setFocusPolicy(Qt.NoFocus)
        self.btn_prefix.setToolTip("保存してある前置きを、いまの下書きの先頭に1回だけ入れます（送るときは 前置き＋改行＋下書き）。"
                                   "自動では付きません。前置きの作成・編集は、引き出しの「前置き」タブ")
        self.btn_prefix.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.menu_prefix = QMenu(self.btn_prefix)
        self.btn_prefix.setMenu(self.menu_prefix)
        wl.addWidget(self.lbl_dest)
        wl.addWidget(self.cmb_preset, 1)
        wl.addWidget(self.chk_send)
        wl.addWidget(self.btn_prefix)
        root.addWidget(self.row_dest)

        # 下段: 確定して届ける（主ボタン）・下書きを広げる・全部消す／通知の1行
        foot = QHBoxLayout()
        foot.setSpacing(6)
        self.btn_commit = QPushButton("確定して届ける")
        self.btn_commit.setObjectName("primary")
        self.btn_expand = _icon_btn("chev_down", "下書きを広げる（直近3行 ⇄ 全体）", w=36, h=34)
        self.btn_draft_discard = QPushButton("全部消す")
        self.btn_draft_discard.setObjectName("ghost")
        foot.addWidget(self.btn_commit, 1)
        # 「下書きを広げる」「全部消す」は画面に出さない（窓を縦に広げれば下書き欄が伸びる。全部消すは事故の元）。
        # 部品は内部の処理が参照するので、親だけ付けて隠しておく（押せない）
        for b in (self.btn_expand, self.btn_draft_discard):
            b.setParent(self)
            b.setVisible(False)
        root.addLayout(foot)
        self.lbl_notice = QLabel("")
        self.lbl_notice.setWordWrap(True)
        root.addWidget(self.lbl_notice)
        root.addStretch(0)          # 直近3行のとき、窓が高くても部品の間が開かないよう、余りは末尾へ寄せる
        self._tail_index = root.count() - 1

        # ⚙ メニューの項目（ボタン自体は UnifiedView の録音の行にある）。確定履歴を開く・接続の状態（診断）
        self.menu = QMenu(self)
        self.act_settings = QAction("設定…", self.menu)
        self.act_history = QAction("確定履歴を開く…", self.menu)
        self.act_headset = QAction("接続の状態（診断）…", self.menu)

        self.chk_send.toggled.connect(lambda on: self.set_send_after_deliver(on))
        self.txt_draft.textChanged.connect(self._on_draft_edited)
        self.txt_draft.commitRequested.connect(lambda: self.commit("window"))
        self.txt_draft.aboutToEdit.connect(self._before_edit)
        self.btn_commit.clicked.connect(lambda: self.commit("window"))
        self.btn_draft_discard.clicked.connect(lambda: self.discard_draft())
        self.btn_expand.clicked.connect(lambda: self.set_expanded(not self._expanded))
        self.btn_repick.clicked.connect(lambda: self.repick())
        self.cmb_preset.activated.connect(lambda i: self.preset_chosen())
        self.btn_prefix_x.clicked.connect(lambda: self.remove_prefix())
        self.btn_retry.clicked.connect(lambda: self.retry())
        self.btn_discard.clicked.connect(lambda: self.discard_undelivered())
        self.act_settings.triggered.connect(lambda: self.open_settings())
        self.act_history.triggered.connect(lambda: self.open_history())
        self.act_headset.triggered.connect(lambda: self.show_headset_status())

    def _apply_dest_text(self) -> None:
        self.txt_draft.setPlaceholderText("録音を押すと、ここに出ます")
        self.txt_draft.setToolTip("喋っている途中は下の波形、区切れると「認識中…」、清書が終わると白い文字になります。"
                                  "キーボードで直せます。確定するまで入力先へは書きません")
        self._update_dest_rows()

    def _update_dest_rows(self) -> None:
        """宛先の行の中身の出し入れ（「なし（記録だけ）」のとき［送信までする］は意味が無い）。"""
        none = bool(self.ctl.record_only)
        if self.chk_send.isHidden() == (not none):
            self.chk_send.setVisible(not none)

    # ---- 宛先（「なし」を足す）
    def fill_presets(self) -> None:
        names = self.ctl.preset_names()
        cur = (self.ctl.profile or {}).get("name", "")
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        self.cmb_preset.addItem("（宛先を選ぶ）", "")
        for n in names:
            self.cmb_preset.addItem(n, n)
        self.cmb_preset.addItem(NONE_LABEL, NONE_KEY)
        self.cmb_preset.addItem(PICK_LABEL, PICK_KEY)
        i = self.cmb_preset.findData(NONE_KEY if self.ctl.record_only else cur)
        self.cmb_preset.setCurrentIndex(max(0, i))
        self.cmb_preset.blockSignals(False)

    @safe
    def preset_chosen(self) -> None:
        data = self.cmb_preset.currentData()
        if data == PICK_KEY:
            self.fill_presets()                  # 表示は今の宛先のまま。入力欄を選び終えたら、保存した宛先に切り替わる
            self.repick()
            return
        if data == NONE_KEY:
            self.ctl.set_record_only(True)
            self.set_notice("宛先なし: 確定すると、記録だけ残します（何も届けず、クリップボードにも入れません）", "muted")
        elif data:
            if self.ctl.record_only:
                self.ctl.set_record_only(False)
            self.ctl.select_preset(str(data))
        self._update_dest_rows()
        self.refresh()

    @safe
    def pick_target(self, name: str = "") -> None:
        if self.ctl.record_only:         # 入力欄を選び直すなら、「なし」はやめて、取った宛先を使う
            self.ctl.set_record_only(False)
            self.fill_presets()
        super().pick_target(name)

    # ---- 前置き（どの宛先にも。「前置き ▾」で選んだときだけ、いまの下書きの先頭に1回だけ入る。自動では付けない）
    _prefix_name = ""

    def _prompt_presets(self) -> list[dict]:
        if self.host is not None:
            return list(getattr(self.host, "presets", []) or [])
        from presets import load_presets
        return load_presets()

    def fill_prompts(self) -> None:
        """「前置き ▾」のメニューを、保存してある前置きの一覧（＋「（なし）」）に合わせる。選択は記憶しない。"""
        m = self.menu_prefix
        m.clear()
        for p in self._prompt_presets():
            a = QAction(p["name"], m)
            a.triggered.connect(lambda checked=False, n=p["name"]: self.insert_prefix(n))
            m.addAction(a)
        m.addSeparator()
        a = QAction("（なし）", m)
        a.triggered.connect(lambda checked=False: self.remove_prefix())
        m.addAction(a)

    @safe
    def insert_prefix(self, name: str) -> None:
        """前置き name を、いまの下書きの先頭に入れる（1回だけ。入れ直せば置き換わる）。送信が済むと下書きと一緒に消える。"""
        body = next((p["prompt"] for p in self._prompt_presets() if p["name"] == name), None)
        if body is None or not str(body).strip():
            self.set_notice(f"前置き「{name}」の本文が空です", "warn")
            return
        self._prefix_name = str(name)
        self.ctl.prefix_text = str(body)
        self._sync_prefix_block()
        self.set_notice(f"前置き「{name}」を下書きの先頭に入れました（確定すると 前置き＋改行＋下書き を送ります）", "muted")

    @safe
    def remove_prefix(self) -> None:
        self._prefix_name = ""
        self.ctl.prefix_text = ""
        self._sync_prefix_block()

    def _sync_prefix_block(self) -> None:
        """挿入した前置きのブロックを、コントローラの prefix_text に合わせる（確定で送る側へ移ったら消える）。"""
        body = self.ctl.prefix_text
        if not body:
            self._prefix_name = ""
            if not self.box_prefix.isHidden():
                self.box_prefix.setVisible(False)
            return
        if self.box_prefix.isHidden():
            self.box_prefix.setVisible(True)
        label = f"前置き: {self._prefix_name}" if self._prefix_name else "前置き"
        if self.lbl_prefix_name.text() != label:
            self.lbl_prefix_name.setText(label)
        self.txt_prefix.set_block_text(body.rstrip("\r\n"))

    # ---- 送信中ブロック（確定して届けるを押したあと、届いて送信できたことを確かめてから消す）
    FLASH_SEC = 1.5
    _flash = None                 # (見出し, 文章, 消える時刻, 赤字の警告か)
    _send_prefix = ""

    def _delivering_message(self):
        buf = self._buffer()
        if buf is None:
            return None
        for m in reversed(buf.history):
            if m.status == "delivering":
                return m
        return None

    def _show_send(self, head: str, text: str, done: bool, warn: bool = False) -> None:
        if self.box_send.isHidden():
            self.box_send.setVisible(True)
        if self.lbl_send_head.text() != head:
            self.lbl_send_head.setText(head)
        col = S.REC if warn else (S.OKGREEN if done else S.MUTED)
        sty = f"color: {col}; font-weight: bold;"
        if self.lbl_send_head.styleSheet() != sty:
            self.lbl_send_head.setStyleSheet(sty)
        tip = "クリックで消します" if warn else ""
        if self.lbl_send_head.toolTip() != tip:
            self.lbl_send_head.setToolTip(tip)
        self.txt_send.set_block_text(text)

    def dismiss_send_warning(self) -> None:
        """残してある「送信を確認できませんでした」の赤字を消す（×の代わりに、ブロックのクリックで）。"""
        fl = self._flash
        if fl is not None and fl[3]:
            self._flash = None
            self._sync_send_block()

    def eventFilter(self, obj, ev):
        try:
            t = ev.type()
            if obj is self.txt_send.viewport():
                if t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton \
                        and not self.txt_send.textCursor().hasSelection():
                    self.dismiss_send_warning()          # 文字を選んでコピーするときは消さない
            elif t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
                self.dismiss_send_warning()
        except Exception:
            log.exception("送信ブロックのクリック")
        return False

    def _sync_send_block(self) -> None:
        """配達中のメッセージがあるあいだ「送信中…」で送っている文章を見せ、済んだら「✓ …」を FLASH_SEC 秒だけ出して消す。
        失敗（未配達）なら消し、いまの未配達の行（赤い枠）が出る。続けて喋った内容は、下の下書きへ入る。"""
        now = time.monotonic()
        dm = self._delivering_message()
        if dm is not None:
            if self.ctl.sending_prefix:
                self._send_prefix = self.ctl.sending_prefix
            self._flash = None
            self._show_send(L.SEND_HEAD_SENDING, L.compose_with_prefix(self._send_prefix, dm.text), False)
            return
        fl = self._flash
        if fl is not None and now < fl[2]:
            self._show_send(fl[0], fl[1], True, warn=fl[3])
            return
        self._flash = None
        self._send_prefix = ""
        if not self.box_send.isHidden():
            self.box_send.setVisible(False)

    def _start_flash(self, info: dict) -> None:
        buf = self._buffer()
        ids = set(info.get("ids") or [])
        msgs = [m for m in (buf.history if buf is not None else []) if m.id in ids]
        text = "\n".join(m.text for m in msgs)
        if not text:
            return
        pre = str(info.get("prefix") or self._send_prefix or "")
        warn = L.send_unconfirmed(info)          # 送信を確認できなかったときは、赤字で残し続ける（次の確定かクリックで消える）
        until = float("inf") if warn else time.monotonic() + self.FLASH_SEC
        self._flash = (L.send_done_head(info), L.compose_with_prefix(pre, text), until, warn)

    @safe
    def _on_event(self, name: str, info: dict) -> None:
        if name == "delivered":
            self._done_at = time.monotonic()
            self._start_flash(info)
        elif name == "undelivered":
            self._flash = None
        DialoguePanel._on_event(self, name, info)

    @safe
    def discard_undelivered(self) -> None:
        super().discard_undelivered()
        buf = self._buffer()
        if buf is None or not buf.has_undelivered:
            self.ctl.clear_sending_prefix()

    def _warn_text(self) -> str:
        """宛先まわりの警告。宛先なし（記録だけ・未選択）のときは窓の確認が無いので、ヘッドセットの警告だけ。
        未選択は、宛先のコンボが「（宛先を選ぶ）」と出しているので、警告にはしない。"""
        if self.ctl.record_only or not self.ctl.profile:
            return L.headset_header_warn(self.ctl.headset_state(), bool(self.ctl.cfg.get("headset_button")),
                                         self.ctl.headset_reason)
        return super()._warn_text()

    # ---- 下書きを広げる
    def draft_h3(self) -> int:
        fm = QFontMetrics(self.txt_draft.font())
        return U.draft_lines_height(fm.lineSpacing(), WAVE_H + 6, int(self.txt_draft.document().documentMargin()))

    def _apply_draft_height(self) -> None:
        ed = self.txt_draft
        eff = self._expanded or self._force_expanded
        root = self.layout()
        # 下書き欄は常に窓の高さに合わせて伸びる（窓を縦に広げれば、その分たくさん見える）。
        # 最小は直近3行。広げる ▾ は「窓を大きくする」ための近道（最小を少し上げる）
        root.setStretchFactor(ed, 1)
        root.setStretch(self._tail_index, 0)
        ed.setMinimumHeight(self.draft_h3() + (80 if eff else 0))
        ed.setMaximumHeight(QMAX)
        ed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.btn_expand.setIcon(I.make_icon("chev_up" if self._expanded else "chev_down"))
        self.btn_expand.setToolTip("下書きを直近3行に戻す" if self._expanded else "下書きを広げる（直近3行 ⇄ 全体）")

    def set_force_expanded(self, on: bool) -> None:
        """引き出しを開いている間は、窓が大きいので下書きも広げる（閉じれば、広げる ▾ の状態に戻る）。"""
        on = bool(on)
        if on != self._force_expanded:
            self._force_expanded = on
            self._apply_draft_height()

    def set_expanded(self, on: bool) -> None:
        on = bool(on)
        if on == self._expanded:
            return
        self._expanded = on
        self._apply_draft_height()
        self.expandedChanged.emit(on)

    @property
    def expanded(self) -> bool:
        return self._expanded

    # ---- 表示の更新
    def _refresh_session_buttons(self, snap: dict, active: bool) -> None:
        pass     # 録音・一時停止は UnifiedView の上段にある

    def refresh(self) -> None:
        super().refresh()
        self._update_dest_rows()
        self._sync_prefix_block()
        self._sync_send_block()

    def _apply_commit_button(self, snap: dict, act: str) -> None:
        super()._apply_commit_button(snap, act)
        waiting = int(snap.get("pending_recognitions") or 0) if snap.get("committing") else 0
        text = U.commit_button_text(act, waiting)
        if act == L.PRESS_COMMIT and self.ctl.record_only:
            text = "確定して記録する" + (f"（認識待ち {waiting}件…）" if waiting else "")
        self.btn_commit.setText(text)

    # ---- いま喋っている長さのバー（下書き欄の末尾の行。100ms ごと。喋った長さに比例して伸び、無音のあいだは伸ばさない）
    def _sync_tail(self) -> None:
        """欄の後ろ側（清書待ちのバー＋いま喋っている途中のバー）を、セッションの状態へ追いつかせる。"""
        ds = self.ctl.ds
        waves = ds.pending_waves() if ds is not None else []
        try:
            from settings import dialogue_config
            model = str(dialogue_config(self.host.s).get("model") or "small") if self.host is not None else "small"
        except Exception:
            model = "small"
        for w in waves:
            w["label"] = U.recognizing_label(model, w.get("eta"))
        live = None
        if ds is not None and self.ctl.active:
            v = ds.live_view()
            if v.get("wave"):
                label = U.fmt_speaking(v.get("seconds")) if v.get("speaking") else U.fmt_speaking(None)
                cd = self._countdown_text()          # 黙ったら、同じ行が「無音 0.5秒（あと0.7秒で認識開始）」になる（バーは止めたまま）
                if cd:
                    label = cd
                live = {"env": tuple(v["wave"]), "label": label}
        ops = L.plan_tail(self.txt_draft.tail_blocks(), L.build_tail(waves, live))
        if ops:
            self.txt_draft.apply_tail_ops(ops)

    def update_live(self) -> None:
        if self._buffer() is not None:
            self._sync_tail()

    @safe
    def _tick(self) -> None:
        super()._tick()
        self.update_live()

    # ---- 停止中の発話警告（通知の行を赤く点滅させる。表示中だけタイマーを動かす）
    def set_alert(self, text: str, sec: float | None = U.ALERT_SEC, kind: str = "stop") -> None:
        """sec が None なら、clear_alert() するまで続ける。kind: "stop"（赤）／"hold"（アンバー）。"""
        now = time.monotonic()
        self._alert_kind = kind
        self._alert_text, self._alert_start = text, now
        self._alert_until = float("inf") if sec is None else now + float(sec)
        if self._blink_timer is None:
            self._blink_timer = QTimer(self)
            self._blink_timer.setInterval(250)
            self._blink_timer.timeout.connect(self._render_notice)
        self._blink_timer.start()
        self._render_notice()

    def clear_alert(self) -> None:
        self._alert_until = 0.0
        self._render_notice()

    def alert_active(self) -> bool:
        return time.monotonic() < self._alert_until

    def done_recently(self) -> bool:
        return (time.monotonic() - self._done_at) <= U.DONE_SHOW_SEC

    def _countdown_text(self) -> str:
        h = self.host
        d = h.s.get("dialogue") if h is not None else None
        gap = float(d.get("utterance_gap_sec", 1.2)) if isinstance(d, dict) else 1.2
        return U.silence_countdown_text(self.ctl.snapshot(), gap)

    def _notice_view(self) -> tuple[str, str, bool]:
        if self.alert_active():
            return self._alert_text, "alert", False
        """通知の行に出す (文言, tone, 薄くするか)。常設のヒントと、未配達の警告・一時的な操作の通知だけ。
        無音カウントダウンは下書き欄の末尾の行、清書の進みは状態バッジ、認識待ちは確定ボタン、配達の結果は送信ブロックに出す。"""
        return self._notice.view(time.monotonic(), L.idle_hint(self.ctl.snapshot()))

    def _render_notice(self) -> None:
        text, tone, faded = self._notice_view()
        if self.lbl_notice.text() != text:
            self.lbl_notice.setText(text)
            self.lbl_notice.setToolTip(text)
        from ui_dialogue import FADED_COLOR, TONE_COLOR
        if tone == "alert":
            fg, bg = U.alert_blink_colors(time.monotonic() - self._alert_start, self._alert_kind)
            sty = f"color: {fg}; background: {bg}; font-weight: bold; padding: 2px 4px; border-radius: 4px;"
            self._blink_draft(bg in (U.ALERT_BLINK_ON[1], U.ALERT_BLINK_ON_AMBER[1]))      # メモ欄（下書き欄）も赤く点滅させる（色だけ。枠の太さは変えない）
        else:
            self._blink_draft(None)
            sty = f"color: {FADED_COLOR if faded else TONE_COLOR.get(tone, S.MUTED)};"
            if self._blink_timer is not None and self._blink_timer.isActive():
                self._blink_timer.stop()           # 警告が消えたら点滅も止める
        if self.lbl_notice.styleSheet() != sty:
            self.lbl_notice.setStyleSheet(sty)

    def _blink_draft(self, bright) -> None:
        """停止中の発話警告のあいだ、下書き欄の背景と枠を赤く点滅させる（色だけ。レイアウトは変えない）。
        bright が None なら、元の見た目へ戻す。"""
        ed = self.txt_draft
        if bright is None:
            normal = getattr(self, "_draft_style_normal", None)
            if normal is not None:
                ed.setStyleSheet(normal)
                self._draft_style_normal = None
            return
        if getattr(self, "_draft_style_normal", None) is None:
            self._draft_style_normal = ed.styleSheet()
        if self._alert_kind == "hold":
            bg, edge = ("#6b4a08" if bright else "#2b2108"), "#f5a623"        # アンバー
        else:
            bg, edge = ("#6b1a1f" if bright else "#2b1215"), "#e5484d"        # 赤
        ed.setStyleSheet(f"QTextEdit {{ background-color: {bg}; border: 1px solid {edge}; }}")

    def notice_text(self) -> str:
        return self._notice_view()[0]

    # ---- セッションの終わり: 録音全体の清書（host が行う）
    def _on_stopped(self, report: dict) -> None:
        super()._on_stopped(report)
        h = self.host
        if h is not None:
            try:
                h.on_session_stopped(report)
            except Exception:
                log.exception("セッション終了後の清書の依頼")


BADGE_COLORS = {"good": S.OKGREEN, "warn": "#f0b429", "busy": S.ORANGE, "info": S.ACCENT, "stop": S.MUTED, "alert": S.REC}


# =====================================================================================================
class UnifiedView(QWidget):
    """窓の中身。上段の操作＋UnifiedPanel＋右の引き出し。"""

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self.host = host
        self.panel: UnifiedPanel | None = None
        s = host.s
        self._drawer_open = bool(s.get("unified_drawer_open", False))
        self._rec_key = None
        self._last_min = None
        self._chip_issues: list = []
        self._tick = 0

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.col = QWidget()
        self.col.setMinimumWidth(0)
        cl = QVBoxLayout(self.col)
        cl.setContentsMargins(8, 8, 8, 6)
        cl.setSpacing(6)
        self.col_layout = cl
        outer.addWidget(self.col, 1)

        # 行1: 録音・一時停止・経過時間 ／ マイク状態チップ・最前面・⚙・引き出し（上段の専用の行は無い）
        self.btn_pin = _icon_btn("pin", "最前面（他の窓より前に固定。前面化の強制はしません）", checkable=True, w=28)
        self.btn_gear = _icon_btn("gear", "設定・WAV清書・確定履歴・接続の状態（診断）など", w=28)
        self.btn_gear.setStyleSheet("QPushButton::menu-indicator { image: none; width: 0px; }")
        self.btn_drawer = _icon_btn("drawer", "引き出し（履歴・用語・前置き・清書の状況）を開く／閉じる", checkable=True, w=28)
        w1 = QWidget()
        r1 = QHBoxLayout(w1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(5)
        self.btn_rec = QPushButton("録音")
        self.btn_rec.setObjectName("recSmall")
        self.btn_rec.setCursor(Qt.PointingHandCursor)
        self.btn_rec.setFocusPolicy(Qt.NoFocus)
        self.btn_rec.setIconSize(QSize(14, 14))
        self.btn_rec.setMinimumWidth(80)
        self.btn_pause = _icon_btn("pause", "保留（取り込みを止める）", checkable=True, w=28)
        self.lbl_time = QLabel("--:--")
        self._fit_time()
        self.chip = MicChip()
        self.badge = QLabel("")
        self.badge.setTextFormat(Qt.RichText)
        self.badge.setMinimumWidth(0)       # 録音の行の空きに収める（足りなければ右が切れるだけで、窓の幅は広げない）
        self.badge.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._badge_key = None
        r1.addWidget(self.btn_rec)
        r1.addWidget(self.btn_pause)
        r1.addWidget(self.lbl_time)
        r1.addWidget(self.badge, 1)
        r1.addWidget(self.chip)
        for b in (self.btn_pin, self.btn_gear, self.btn_drawer):
            r1.addWidget(b)
        cl.addWidget(w1)
        self.row_top = w1

        self._alert_last = None

        # 行3: メーター（清書の進みは、状態バッジに出る）
        self.meter = MeterWidget()
        self.meter.setMinimumWidth(60)
        self.meter.setFixedHeight(14)
        cl.addWidget(self.meter)
        self._line_t = 0.0
        self.meter.lineDragged.connect(self._on_line_dragged)
        self.meter.lineReleased.connect(self._on_line_released)
        self.meter.lineReset.connect(lambda: self._on_line_released(MS.LINE_DB_DEFAULT, reset=True))

        self.pl = QVBoxLayout()
        self.pl.setContentsMargins(0, 0, 0, 0)
        cl.addLayout(self.pl, 1)

        # マイク状態の詳細（押したときだけ。窓の中に重ねる）
        self.popup = ChipPopup(self)
        self.popup.openSettings.connect(lambda: self.host.open_settings())
        self.chip.detailRequested.connect(self.toggle_chip_detail)

        # 右の引き出し
        self.drawer = Drawer(host)
        self.drawer.setAttribute(Qt.WA_StyledBackground, True)
        self.drawer.setStyleSheet(f"#drawer {{ background: {S.BG}; border-left: 1px solid {S.BORDER}; }}")
        self.drawer.setFixedWidth(DRAWER_WIDTH)
        self.drawer.setVisible(self._drawer_open)
        outer.addWidget(self.drawer)
        self.drawer.set_tab(int(s.get("unified_drawer_tab", TAB_HISTORY) or 0))

        self.btn_rec.clicked.connect(lambda: self.rec_pressed())
        self.btn_pause.clicked.connect(lambda: self.pause_pressed())
        self.btn_pin.toggled.connect(lambda v: host.set_always_on_top(bool(v)))
        self.btn_drawer.clicked.connect(lambda: self.set_drawer(not self._drawer_open))
        self.btn_drawer.setChecked(self._drawer_open)
        self.btn_pin.setChecked(bool(host._pinned))

    @staticmethod
    def _time_pt() -> int:
        return max(10, S.BASE_PT + 1)

    def _fit_time(self) -> None:
        """経過時間（h:mm:ss まで）が必ず収まる固定幅にする（桁が増えても他の部品が動かない）。"""
        lb = self.lbl_time
        lb.setStyleSheet(f"font-family: Consolas; font-size: {self._time_pt()}pt; font-weight: bold; color: {S.MUTED};")
        lb.ensurePolished()
        lb.setFixedWidth(QFontMetrics(lb.font()).horizontalAdvance("8:88:88") + 12)

    # ---- パネルの取り付け（MainWindow._ensure_dialogue から）
    def attach_panel(self, panel: UnifiedPanel) -> None:
        self.panel = panel
        self.pl.addWidget(panel, 1)
        panel.set_force_expanded(self._drawer_open)
        panel.set_expanded(bool(self.host.s.get("unified_expanded", False)))
        panel.expandedChanged.connect(self._on_expanded)
        self._build_menu()

    def _build_menu(self) -> None:
        h, p = self.host, self.panel
        m = QMenu(self.btn_gear)
        a = QAction("設定…", m)
        a.triggered.connect(lambda: h.open_settings())
        m.addAction(a)
        a = QAction("WAV を選んで清書…", m)
        a.triggered.connect(lambda: h.pick_wav())
        m.addAction(a)
        a = QAction("出力フォルダを開く", m)
        a.triggered.connect(lambda: h.open_out())
        m.addAction(a)
        m.addSeparator()
        if p is not None:
            for act in (p.act_history, p.act_headset):
                m.addAction(act)
        self.menu = m
        self.btn_gear.setMenu(m)

    def notice(self, text: str, tone: str = "muted") -> None:
        if self.panel is not None:
            self.panel.set_notice(text, tone)

    # ---- 録音（＝セッション開始／停止）・保留
    def rec_pressed(self) -> None:
        if self.panel is not None:
            self.panel.toggle_session()

    def pause_pressed(self) -> None:
        """一時停止は［保留］（取り込みを止める）。"""
        if self.panel is not None:
            self.panel.toggle_hold()

    # ---- 引き出し
    @property
    def drawer_open(self) -> bool:
        return self._drawer_open

    def set_drawer(self, on: bool, tab: int | None = None) -> None:
        if tab is not None:
            self.drawer.set_tab(tab)
        on = bool(on)
        if on == self._drawer_open:
            self.btn_drawer.setChecked(on)
            return

        def mutate():
            self._drawer_open = on
            self.drawer.setVisible(on)
            self.btn_drawer.setChecked(on)
            self.host.s["unified_drawer_open"] = on
            if self.panel is not None:
                self.panel.set_force_expanded(on)
        self.host._unified_relayout(mutate)
        self.host._save_settings()

    # ---- 認識開始ライン（音量バー上。設定 dialogue.voice_min_dbfs）
    def _apply_voice_min(self, db: float) -> None:
        MS.store_voice_min(self.host.s, db)
        ds = self.host.dialogue_ctl.ds
        if ds is not None:
            try:
                ds.set_voice_min_dbfs(db)
            except Exception:
                pass

    def _on_line_dragged(self, db: float) -> None:
        import time
        now = time.monotonic()
        if now - self._line_t >= 0.15:               # 動かしている間は間引いて区切り器へ反映（保存は離したとき）
            self._line_t = now
            ds = self.host.dialogue_ctl.ds
            if ds is not None:
                try:
                    ds.set_voice_min_dbfs(db)
                except Exception:
                    pass

    def _on_line_released(self, db: float, reset: bool = False) -> None:
        db = MS.clamp_line_db(db)
        if reset:
            self.meter.set_line(db)
        self._apply_voice_min(db)
        self.host._save_settings()

    def _on_expanded(self, on: bool) -> None:
        h = self.host
        h.s["unified_expanded"] = bool(on)
        h._save_settings()
        h._unified_resize_for_expand(bool(on))

    # ---- 毎 tick（MainWindow._tick から）
    def chip_inputs(self) -> dict:
        h = self.host
        sess = h.svc.session
        rec = sess.rec
        recording, paused = sess.is_recording, sess.is_paused
        speaking = bool(h.meter.unrecorded)            # 録音されていないのに話している（メーターが赤になる条件）
        if h._dialogue_active():
            snap = h.dialogue_ctl.snapshot()
            if snap.get("held"):                       # 保留中は、取り込みを止めている（一時停止と同じ扱い）
                paused, speaking = True, bool(snap.get("speaking"))
        if recording and not paused:
            no_audio = bool(rec is not None and rec.seconds_since_audio > 2.0)
        else:
            no_audio = bool(sess.monitor_active and sess.hub.seconds_since_audio > 3.0)
        busy = bool(sess.monitor_error)
        measured = recording or sess.idle_monitor.stats() is not None
        return dict(recording=recording, paused=paused, issues=list(h._issues), monitoring=recording or sess.monitor_active,
                    no_audio=no_audio, busy=busy, speaking_unrecorded=speaking, measured=bool(measured))

    def chip_key(self) -> str:
        return U.chip_state(**self.chip_inputs())

    def toggle_chip_detail(self) -> None:
        self.popup.toggle_for(self.chip, self.chip.key, self.host._issues)

    def refresh(self) -> None:
        h = self.host
        sess = h.svc.session
        p = self.panel
        dlg_active = h._dialogue_active()
        recording = sess.is_recording
        ending = bool(p is not None and p._ending)
        text, kind, enabled, tip = U.rec_button_view(dlg_active, ending)
        key = (text, kind, enabled, tip)
        if key != self._rec_key:
            self._rec_key = key
            b = self.btn_rec
            name = "recStopSmall" if kind == "stop" else "recSmall"
            if b.objectName() != name:
                b.setObjectName(name)
                b.style().unpolish(b)
                b.style().polish(b)
            b.setText(text)
            b.setIcon(I.make_icon("stop", fixed_color=S.REC) if kind == "stop" else I.make_icon("rec", fixed_color="#ffffff"))
            b.setEnabled(enabled)
            b.setToolTip(tip)
        held = bool(dlg_active and h.dialogue_ctl.snapshot().get("held"))
        en, on, ptip = U.pause_button_view(dlg_active, held)
        if self.btn_pause.isEnabled() != en:
            self.btn_pause.setEnabled(en)
        if self.btn_pause.isChecked() != on:
            self.btn_pause.blockSignals(True)
            self.btn_pause.setChecked(on)
            self.btn_pause.blockSignals(False)
            self.btn_pause.setIcon(I.make_icon("play" if on else "pause"))
        if self.btn_pause.toolTip() != ptip:
            self.btn_pause.setToolTip(ptip)
            self.btn_pause.setAccessibleName(ptip)
        # 経過時間
        rec = sess.rec
        t = U.fmt_clock(rec.elapsed) if (recording and rec is not None) else U.fmt_clock(None)
        if self.lbl_time.text() != t:
            self.lbl_time.setText(t)
        col = S.TEXT if recording else S.MUTED
        sty = f"font-family: Consolas; font-size: {self._time_pt()}pt; font-weight: bold; color: {col};"
        if self.lbl_time.styleSheet() != sty:
            self.lbl_time.setStyleSheet(sty)
        # メーター・マイク状態チップ
        self.meter.set_level(sess.level)
        try:
            self.meter.set_line(MS.clamp_line_db((h.s.get("dialogue") or {}).get("voice_min_dbfs")))
        except Exception:
            pass
        self.meter.set_unrecorded(h.meter.unrecorded)
        self._refresh_status(sess, dlg_active)
        self.chip.set_state(self.chip_key())
        if self.popup.isVisible():
            self.popup.refresh_text(self.chip.key, h._issues)
        if p is not None:
            p.update_live()                                 # いま喋っている長さと波形（喋っているあいだだけ動く）
        sh = self.minimumSizeHint()
        if sh != self._last_min:                        # 未配達の行・警告・折り返した通知などで、必要な最小サイズが変わった
            self._last_min = sh
            h._apply_unified_min()
        if self.btn_pin.isChecked() != bool(h._pinned):
            self.btn_pin.blockSignals(True)
            self.btn_pin.setChecked(bool(h._pinned))
            self.btn_pin.blockSignals(False)

    def _refresh_status(self, sess, dlg_active: bool) -> None:
        """状態バッジ・無音カウントダウン・モデル表示・停止中の発話警告。"""
        h, p = self.host, self.panel
        now = time.monotonic()
        # 停止中の発話警告: 録音していないのに、1秒以上続けて喋っていると認識したら、喋り終わるまでずっと点滅させ、
        # 喋り終わったと認識したらすぐ止める（待機中のマイク確認が無効なら monitor_active が False なので出ない）
        if p is not None:
            try:
                speaking_idle = bool(
                    not dlg_active and not sess.is_recording
                    and (bool(h.s.get("idle_prompt_enabled", True)) and bool(h.s.get("idle_monitor", True)))
                    and bool(sess.monitor_active) and bool(h.isVisible() and not h.isMinimized()))
                run = float(sess.idle_speech.run_sec) if speaking_idle else 0.0
            except Exception:
                speaking_idle, run = False, 0.0
            if speaking_idle and run >= U.ALERT_START_SEC and not p.alert_active():
                p.set_alert(U.ALERT_TEXT, sec=None)                  # 喋り終わるまで続く
            elif p.alert_active() and p._alert_kind == "stop" and (not speaking_idle or run <= 0.0):
                p.clear_alert()                                       # 喋り終わった（または条件が外れた）らすぐ止める
        # 一時停止（保留）中に喋り続けているとき: 録音されていると誤解して喋り続ける事故を防ぐため、アンバーで点滅させる
        if p is not None and dlg_active:
            try:
                hs = h.dialogue_ctl.snapshot()
                held = bool(hs.get("held"))
                talking = bool(hs.get("speaking")) and float(hs.get("silence_sec") or 0.0) < 0.6
            except Exception:
                held, talking = False, False
            if held and talking:
                if self._hold_speak_since is None:
                    self._hold_speak_since = now
                if now - self._hold_speak_since >= U.ALERT_START_SEC and not p.alert_active():
                    p.set_alert(U.HOLD_ALERT_TEXT, sec=None, kind="hold")
            else:
                self._hold_speak_since = None
                if p.alert_active() and p._alert_kind == "hold":
                    p.clear_alert()
        alert = bool(p is not None and p.alert_active())
        self.chip.set_alert(alert)
        snap = h.dialogue_ctl.snapshot() if dlg_active else {"active": False}
        fin = ""
        if not dlg_active:                      # 停止中だけ、録音全体の清書の進みをバッジに出す
            try:
                fin = str(h.progress_text() or "")
            except Exception:
                fin = ""
        text, tone = U.status_view(snap, done=bool(p is not None and p.done_recently()), alert=alert, finalize=fin)
        from settings import dialogue_config
        models = U.models_text(str(dialogue_config(h.s).get("model") or "small"), str(h.s.get("final_model") or "small"),
                               bool(h.s.get("finalize_on_end", True)))
        if (text, tone, models) != self._badge_key:
            self._badge_key = (text, tone, models)
            col = BADGE_COLORS[tone]
            dot = "" if text.startswith("✓") else "● "
            # 使用中のモデルは、下書き欄の「認識中（small・あと約N秒）」の行に出す（ここには出さない。情報を散らさない）
            self.badge.setText(f'<span style="color:{col}; font-weight:bold;">{dot}{text}</span>')
            self.badge.setToolTip(f"認識 {models}")
            self.badge.setToolTip(f"状態: {text}\n使用中のモデル: {models}")

    def refit(self) -> None:
        """表示サイズ（小・標準・大）が変わった: チップの固定サイズ・下書きの高さ・経過時間の幅を取り直す。"""
        self.chip.refit()
        self._fit_time()
        self.btn_expand_refit()
        self.updateGeometry()

    def btn_expand_refit(self) -> None:
        if self.panel is not None:
            self.panel._apply_draft_height()
