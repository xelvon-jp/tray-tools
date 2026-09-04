# agent_loop_viewer.py
# エージェントループの実行の様子を常時表示するログ窓(Qt)。
#
# なぜ Qt 窓か
# ------------
# 陽太さんの要件は「PowerShell の画面を常時表示」。実際の PowerShell 窓を
# 1周ごとに Start-Process で開く案もあったが、周が増えるほど窓が増える・監視モード
# 終了で自動で閉じられない、という運用上の弱点があった。tray-tools 側で自前の
# ログ窓を持てば
#   - 監視モードの開始で開き、終了で自動で閉じる
#   - スニペット全文と stdout/stderr を1つに集約して振り返れる
#   - 応答受信・実行中・完了の状態が色分けで一目
#   - スクロールと文字選択がそのまま使える
# が全部揃う。実体は QPlainTextEdit + 上部の状態バー。
#
# スレッドの取り扱い
# ------------------
# on_agent_loop_event は agent_loop のワーカースレッドから呼ばれる。Qt のウィジェット
# はメインスレッドからしか触れないので、pyqtSignal(dict) で受けてメインスレッドの
# スロットに渡し直す(既定のシグナル/スロット接続はスレッドをまたぐと自動で
# Qt.QueuedConnection になる)。ワーカーが emit するだけならスレッド安全。
#
# tray-tools の CLAUDE.md にある「スロット内で例外が投げ切られると常駐ごと落ちる」
# 対策として、スロットは必ず try で受ける。
import html
import time
from datetime import datetime

from PySide6.QtCore import Qt, QObject, QRectF, Signal
from PySide6.QtGui import (
    QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget,
)

import settings as settings_module

# 既定の窓の大きさ。1周ぶんの実行結果(stdout+stderr)が読める程度。
DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 640

# 1行あたりの色の指定。QPlainTextEdit は HTML の appendHtml で入れると色が付く。
# 「読みやすい」より「一目で状態が分かる」を優先。
COLOR_TIME = "#888"
COLOR_INFO = "#cccccc"
COLOR_META = "#88c0ff"     # round / prompt など
COLOR_WAIT = "#c8c86e"     # 応答待ち
COLOR_RESP = "#a8d8a8"     # 応答受信
COLOR_CODE = "#e8e8e8"     # 抽出コードの見出し
COLOR_RUN = "#a8d8a8"      # exit=0
COLOR_RUN_ERR = "#f0a0a0"  # exit!=0 / timeout
COLOR_STDOUT = "#e8e8e8"
COLOR_STDERR = "#f0a0a0"
COLOR_STOP_OK = "#a8d8a8"  # dry-run / finish-word
COLOR_STOP_WARN = "#f0d078"  # no-snippet / timeout / cancel
COLOR_STOP_ERR = "#f0a0a0"   # risky / error

STOP_STYLES = {
    "dry-run": (COLOR_STOP_OK, "🟢 dry-run で停止"),
    "finish-word": (COLOR_STOP_OK, "🏁 完了語を検知して停止"),
    "no-snippet": (COLOR_STOP_WARN, "⚪ 応答にスニペット無し(完了とみなす)"),
    "max-rounds": (COLOR_STOP_WARN, "⚠️ 最大周回に達した"),
    "cancelled": (COLOR_STOP_WARN, "⏹️ 停止要求を検知"),
    "response-timeout": (COLOR_STOP_WARN, "⚠️ 応答待ちタイムアウト"),
    "loop-timeout": (COLOR_STOP_WARN, "⚠️ ループ全体タイムアウト"),
    "risky-code": (COLOR_STOP_ERR, "🛑 危険パターン検出で停止"),
    "error": (COLOR_STOP_ERR, "❌ エラーで停止"),
}


def _clip_head_tail(text, head=1200, tail=800):
    """stdout/stderr が長すぎるとき、頭と末尾を残して真ん中を省略する。"""
    text = text or ""
    if len(text) <= head + tail + 40:
        return text
    return (text[:head]
            + f"\n… (中略 {len(text) - head - tail} 文字省略) …\n"
            + text[-tail:])


# settings.json に覚える。窓を開くたびに使う人が「最前面固定」を設定し直さないで
# 済むようにするため。位置は毎回中央に置き直すので保存しない(モニタ構成が変わっても
# 迷子にならない)。
SETTINGS_SECTION = "agent_loop_viewer"
SETTINGS_ALWAYS_ON_TOP = "always_on_top"


def _make_emoji_icon(emoji: str, size: int = 64) -> QIcon:
    """絵文字1文字を大きめに描いた QIcon を作る。

    タスクバー/タイトルバーに tray-tools 本体のドット絵(天むす)ではなく、
    この窓が何者かを一目で示すロボット等を出したいときに使う。フォントは
    Segoe UI Emoji(Windows 標準)で描くと絵文字が出る(Meiryo だと豆腐になる)。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)
        font = QFont("Segoe UI Emoji", int(size * 0.7))
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, emoji)
    finally:
        painter.end()
    return QIcon(pixmap)


class LogViewer(QWidget):
    """エージェントループの実行状況を常時表示する窓。

    on_agent_loop_event(payload) を agent_loop.run_loop() の on_event に渡す。
    ワーカースレッドから呼ばれても pyqtSignal で受け直すので安全。"""

    _event_signal = Signal(dict)

    def __init__(self, app_settings=None, settings_path=None):
        super().__init__()
        # 陽太さんの要望: タスクバー/タイトルバーには天むす(Rapture)ではなく🤖を出す。
        # 「これはエージェントループの窓」と一目で分かるように、tray アイコンとは
        # 別のアイコンにする。絵文字から作れば追加ファイルが要らない。
        self.setWindowIcon(_make_emoji_icon("🤖"))
        self.setWindowTitle("エージェントループ")
        self.setObjectName("agentLoopViewer")
        # 既定は「最前面固定なし」。上のチェックボックスで陽太さんが決められる。
        self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

        self._app_settings = app_settings
        self._settings_path = settings_path

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # 状態バーと最前面固定のチェックを1行に並べる。チェックは右寄せ。
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(8)
        self.status = QLabel("待機中")
        self.status.setFont(QFont("Meiryo", 10))
        self.status.setObjectName("agentLoopStatus")
        status_row.addWidget(self.status, 1)

        self.top_check = QCheckBox("📌 最前面固定")
        self.top_check.setFont(QFont("Meiryo", 9))
        self.top_check.setObjectName("agentLoopTopCheck")
        self.top_check.setChecked(self._load_always_on_top())
        # setChecked(True) では setWindowFlags を効かせられる前に窓が構築される。
        # 描画順が固まる setSingleShot(0) 相当を、show の後にする(closeEvent 側で
        # 保存も同じ)。ここでは接続だけしておく。
        self.top_check.toggled.connect(self._on_top_toggled)
        status_row.addWidget(self.top_check, 0)
        layout.addLayout(status_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.setFont(QFont("Consolas", 9))
        # 上限を大きめに(1周で3000〜5000文字入るので、20周でも収まる)。
        self.log.setMaximumBlockCount(100000)
        layout.addWidget(self.log, 1)

        self.setStyleSheet(
            "#agentLoopViewer { background-color: #1a1a1a; color: #ddd; }"
            "#agentLoopStatus { color: #eee; padding: 4px 8px; "
            "background-color: #262626; border-radius: 4px; }"
            "#agentLoopTopCheck { color: #ccc; padding: 4px 8px; }"
            "QPlainTextEdit { background-color: #111; color: #ddd; "
            "border: 1px solid #333; border-radius: 4px; padding: 6px; }"
        )

        self._center_on_cursor_screen()
        # 保存されていた「最前面固定」を今の窓フラグに反映する。
        self._apply_always_on_top(self.top_check.isChecked())

        # スレッドをまたぐシグナル。Qt.QueuedConnection が自動で選ばれる。
        self._event_signal.connect(self._handle_event)

    def _center_on_cursor_screen(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.center().y() - self.height() // 2
        self.move(max(area.left(), x), max(area.top(), y))

    # ------- 「最前面固定」チェックの挙動と永続化 --------------------
    def _load_always_on_top(self) -> bool:
        section = (self._app_settings or {}).get(SETTINGS_SECTION)
        if not isinstance(section, dict):
            return False
        return bool(section.get(SETTINGS_ALWAYS_ON_TOP, False))

    def _save_always_on_top(self, checked: bool) -> None:
        """settings.json を丸ごと書き戻さず、該当キーだけ差し替える。
        (snippets.push_recent と同じ流儀。既定値まで焼き込まれてファイルの姿が
        変わるのを避けるため。)"""
        if isinstance(self._app_settings, dict):
            section = self._app_settings.get(SETTINGS_SECTION)
            if not isinstance(section, dict):
                section = self._app_settings[SETTINGS_SECTION] = {}
            section[SETTINGS_ALWAYS_ON_TOP] = bool(checked)
        if not self._settings_path:
            return
        import json
        import os
        try:
            stored = {}
            if os.path.exists(self._settings_path):
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
            section = stored.get(SETTINGS_SECTION)
            if not isinstance(section, dict):
                section = stored[SETTINGS_SECTION] = {}
            section[SETTINGS_ALWAYS_ON_TOP] = bool(checked)
            settings_module.save_settings(stored, self._settings_path)
        except OSError as e:
            print(f"[agent_loop_viewer] 最前面固定の保存に失敗: {e}")

    def _apply_always_on_top(self, checked: bool) -> None:
        """WindowStaysOnTopHint フラグを付け外しする。setWindowFlags は
        いったん hide → show が必要になる場合があるが、visible な間は Qt が
        面倒を見てくれるので、show を呼び直すだけで済む。"""
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        # まだ show されていないタイミングで呼ばれても壊れないよう、
        # setWindowFlags の後で isVisible なら再度 show する。
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def _on_top_toggled(self, checked: bool) -> None:
        try:
            self._apply_always_on_top(checked)
            self._save_always_on_top(checked)
        except Exception as e:  # noqa: BLE001  設定保存失敗で窓を落とさない
            print(f"[agent_loop_viewer] 最前面固定の切替に失敗: {e}")

    # ------- 外から呼ぶ入口 --------------------------------------------
    def on_agent_loop_event(self, payload: dict) -> None:
        """agent_loop.run_loop() の on_event として渡す。ワーカースレッドから呼ばれる。

        直接ウィジェットを触ってはいけない。シグナルに載せてメインスレッドへ渡す。"""
        try:
            self._event_signal.emit(payload)
        except Exception:  # noqa: BLE001  受け側が閉じていても止まらない
            pass

    def append_note(self, text: str) -> None:
        """外部から任意の一行を追記する(監視モード開始のあいさつなど)。"""
        self._append(text, COLOR_INFO)

    # ------- 内部 -----------------------------------------------------
    def _append(self, text: str, color: str = COLOR_INFO) -> None:
        """1行を色付きで追記して末尾へスクロール。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        line = (f'<span style="color:{COLOR_TIME}">{stamp}</span> '
                f'<span style="color:{color}">{html.escape(text)}</span>')
        self.log.appendHtml(line)
        self._scroll_to_end()

    def _append_block(self, header: str, header_color: str,
                      body: str, body_color: str = COLOR_STDOUT) -> None:
        """見出し + 複数行の本文を追記。コードや stdout/stderr 用。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        head = (f'<span style="color:{COLOR_TIME}">{stamp}</span> '
                f'<span style="color:{header_color}">{html.escape(header)}</span>')
        self.log.appendHtml(head)
        # 本文は等幅で色を付ける。改行を残したいので <pre> で囲む。
        body_html = html.escape(body).replace("\n", "<br>")
        self.log.appendHtml(
            f'<pre style="color:{body_color};margin:0 0 4px 12px;'
            f'font-family:Consolas,monospace;font-size:9pt;">{body_html}</pre>')
        self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _handle_event(self, payload: dict) -> None:
        """メインスレッドで受けるスロット。ここで例外を投げ切ると常駐が落ちる
        (tray-tools の CLAUDE.md 参照)ので、必ず try で囲む。"""
        try:
            self._render(payload)
        except Exception as e:  # noqa: BLE001
            self._append(f"[viewer] イベントの描画に失敗: {e}", COLOR_STOP_ERR)

    def _render(self, payload: dict) -> None:
        event = payload.get("event", "")
        if event == "loop_start":
            mode = "監視" if payload.get("watch") else "通常"
            auto = "auto" if payload.get("auto_run") else "dry-run"
            self.status.setText(f"エージェントループ 開始 ({mode} / {auto})")
            self._append(
                f"■ 開始 [{mode}モード / {auto}] "
                f"max={payload.get('max_rounds')}周 / "
                f"finish_word={payload.get('finish_word') or '(なし)'}",
                COLOR_META,
            )
        elif event == "round_start":
            r = payload.get("round")
            self.status.setText(f"round {r} 開始")
            preview = payload.get("prompt_preview") or ""
            if payload.get("skip_send"):
                self._append(f"── round {r} 開始（送信はスキップ、人が投稿済み）", COLOR_META)
            else:
                self._append_block(f"── round {r} 送信するプロンプト",
                                   COLOR_META, preview, COLOR_INFO)
        elif event == "response":
            r = payload.get("round")
            self.status.setText(f"round {r} 応答受信済み")
            head = payload.get("response_head") or ""
            self._append(
                f"応答受信 (round={r}, {payload.get('chars')} 文字, "
                f"{payload.get('wait_seconds')} 秒)",
                COLOR_RESP,
            )
            if head:
                self._append_block("応答の先頭", COLOR_RESP, head, COLOR_INFO)
        elif event == "snippet":
            r = payload.get("round")
            sid = payload.get("id")
            self.status.setText(f"round {r} スニペット抽出 #{sid}")
            code = payload.get("code") or ""
            risks = payload.get("risks", 0)
            header = f"抽出 #{sid} ({payload.get('chars')} 文字、危険 {risks} 件)"
            self._append_block(header, COLOR_CODE, code, COLOR_CODE)
        elif event == "run":
            r = payload.get("round")
            sid = payload.get("id")
            exit_code = payload.get("exit_code")
            timed_out = payload.get("timed_out")
            color = COLOR_RUN if (exit_code == 0 and not timed_out) else COLOR_RUN_ERR
            status = "TIMEOUT" if timed_out else f"exit={exit_code}"
            self.status.setText(f"round {r} 実行 #{sid} {status}")
            self._append(f"実行 #{sid} {status} "
                         f"(stdout {payload.get('stdout_chars')} 文字 / "
                         f"stderr {payload.get('stderr_chars')} 文字)",
                         color)
            stdout = _clip_head_tail(payload.get("stdout") or "")
            stderr = _clip_head_tail(payload.get("stderr") or "")
            if stdout.strip():
                self._append_block("=== STDOUT ===", COLOR_STDOUT, stdout, COLOR_STDOUT)
            if stderr.strip():
                self._append_block("=== STDERR ===", COLOR_STDERR, stderr, COLOR_STDERR)
        elif event == "dry_run":
            self.status.setText("dry-run で停止（コードは実行しませんでした）")
            self._append(f"dry-run 停止: #{payload.get('id')} は実行せず、"
                         "コードをログに残しました",
                         COLOR_STOP_OK)
        elif event == "round_end":
            reason = payload.get("reason")
            if reason:
                # 途中で終わった round(タイムアウト等)。loop_end で改めて出るので簡潔に。
                self._append(f"round {payload.get('round')} 終了 ({reason})",
                             COLOR_STOP_WARN)
            # 普通の完了(reason なし)はうるさいので何も出さない
        elif event == "loop_end":
            reason = payload.get("reason") or ""
            color, label = STOP_STYLES.get(reason, (COLOR_INFO, f"停止: {reason}"))
            detail = payload.get("detail") or ""
            self.status.setText(f"{label}  周回 {payload.get('rounds')} / "
                                f"経過 {payload.get('elapsed')} 秒")
            self._append(f"■ 終了 {label} — {detail} "
                         f"(周回 {payload.get('rounds')} / "
                         f"経過 {payload.get('elapsed')} 秒)", color)
        else:
            # 未知のイベントは黙って出す(将来のイベント追加でも壊さない)
            self._append(f"[{event}] {payload}", COLOR_INFO)

    def closeEvent(self, event):
        # 手動で閉じられた場合。監視モードが実行中なら次のイベントで再表示することが
        # あるが、そこは呼び出し側(feature_screen)の判断に任せる。
        super().closeEvent(event)
