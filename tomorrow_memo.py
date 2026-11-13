# tomorrow_memo.py
# 「明日の自分へメモ」。就業前に書いたメモが、次回の常駐起動時にポップアップする、
# 昨日の自分から今日の自分への手紙のような機能。
#
# 仕様(Nagato と文章で合意済み):
#   - メモは1件だけ。データは PC ローカル(このモジュールと同じフォルダの
#     tomorrow_memo.json)に持つ。settings.json には混ぜない。
#   - 書いた内容は入力のたびに自動保存する(保存ボタンは無い)。
#   - 過去のメモは自動で履歴に残る。本文が失われる経路を作らない:
#       * 起動時に表示したメモは、閉じた時点で履歴へ移す(reason="seen")
#       * 書く窓で前の内容と違う内容に書き換えたら、窓を閉じた時点で
#         書き換える前の内容を履歴へ移す(reason="replaced")
#     保存は一時ファイル + os.replace の原子的書き換えで、途中で落ちても
#     ファイルが半端な状態にはならない。
#   - 起動時に、まだ見ていない(未表示の)メモがあれば窓で出す。空なら出さない。
#   - 窓から過去のメモも読める(読み取り専用)。
#
# データの持ち方と窓をこのモジュールにまとめ、データ部分(TomorrowMemoStore)は
# Qt に依存させない(テストと将来の使い回しのため)。
import json
import os
from datetime import datetime
from pathlib import Path

DATA_FILENAME = "tomorrow_memo.json"
DEFAULT_DATA_PATH = Path(__file__).resolve().parent / DATA_FILENAME

# 起動してから未表示メモを出すまでの待ち(ms)。トレイや他の起動処理が
# 落ち着いてから出す(呼び出し側 feature_screen が使う)。
STARTUP_SHOW_DELAY_MS = 3000

REASON_SEEN = "seen"        # 起動時に表示して閉じた
REASON_REPLACED = "replaced"  # 書く窓で書き換えられた

_REASON_LABELS = {
    REASON_SEEN: "表示済み",
    REASON_REPLACED: "書き換え前",
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_written_at(iso_text: str) -> str:
    """保存済みの ISO 日時を「2026-11-13 19:20」の形にする。解釈できなければ元の文字列。"""
    try:
        return datetime.fromisoformat(iso_text).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso_text or "")


def _empty_state() -> dict:
    return {"version": 1, "current": None, "history": []}


class TomorrowMemoStore:
    """tomorrow_memo.json の読み書き。Qt 非依存。

    state の形:
      {"version": 1,
       "current": {"text": str, "written_at": ISO, "seen": bool} | None,
       "history": [{"text": str, "written_at": ISO, "archived_at": ISO,
                    "reason": "seen" | "replaced"}, ...]}  # 新しい順
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else DEFAULT_DATA_PATH
        self._state = None

    # ---------------------------------------------------------------
    # 読み書き
    # ---------------------------------------------------------------
    def _load(self) -> dict:
        if self._state is not None:
            return self._state
        state = _empty_state()
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                current = data.get("current")
                if isinstance(current, dict) and isinstance(current.get("text"), str):
                    state["current"] = {
                        "text": current["text"],
                        "written_at": str(current.get("written_at") or ""),
                        "seen": bool(current.get("seen")),
                    }
                history = data.get("history")
                if isinstance(history, list):
                    state["history"] = [
                        {
                            "text": item["text"],
                            "written_at": str(item.get("written_at") or ""),
                            "archived_at": str(item.get("archived_at") or ""),
                            "reason": str(item.get("reason") or ""),
                        }
                        for item in history
                        if isinstance(item, dict) and isinstance(item.get("text"), str)
                    ]
        except (OSError, ValueError):
            # 無い・壊れている場合は空として扱う。壊れた元ファイルは、最初の
            # 保存で丸ごと新しい状態に置き換わる(履歴も含めて失うのはこの場合
            # だけだが、壊れた JSON からは復元できないので仕方がない)。
            pass
        self._state = state
        return state

    def _save(self) -> None:
        state = self._load()
        text = json.dumps(state, ensure_ascii=False, indent=2)
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.path)

    # ---------------------------------------------------------------
    # 現在のメモ
    # ---------------------------------------------------------------
    def current(self):
        """現在のメモ(写し)。無ければ None。"""
        current = self._load()["current"]
        return dict(current) if current else None

    def has_unseen(self) -> bool:
        """起動時に出すべき未表示メモがあるか。空のメモでは出さない。"""
        current = self._load()["current"]
        return bool(
            current
            and current["text"].strip()
            and not current["seen"]
        )

    def set_text(self, text: str, now: str = None) -> None:
        """自動保存。空(空白だけ)なら現在のメモは無しにする。

        書いた日時は、内容が変わったときだけ更新する(開いて眺めただけでは
        日時が新しくならないように)。"""
        state = self._load()
        if not (text or "").strip():
            if state["current"] is not None:
                state["current"] = None
                self._save()
            return
        current = state["current"]
        if current is not None and current["text"] == text:
            return
        state["current"] = {
            "text": text,
            "written_at": now or _now_iso(),
            "seen": False,
        }
        self._save()

    # ---------------------------------------------------------------
    # 履歴(バックアップ)
    # ---------------------------------------------------------------
    def history(self):
        """過去のメモ(写し)。新しい順。"""
        return [dict(item) for item in self._load()["history"]]

    def _archive(self, entry: dict, reason: str, now: str = None) -> None:
        state = self._load()
        state["history"].insert(0, {
            "text": entry["text"],
            "written_at": entry.get("written_at") or "",
            "archived_at": now or _now_iso(),
            "reason": reason,
        })

    def mark_seen(self, now: str = None) -> bool:
        """表示して閉じたので、現在のメモを履歴へ移す。移したら True。

        移したあとは現在のメモは無しになる(次回の起動では出ない)。
        本文は履歴に残るので、窓から読み直せる。"""
        state = self._load()
        current = state["current"]
        if not current or not current["text"].strip():
            state["current"] = None
            return False
        self._archive(current, REASON_SEEN, now)
        state["current"] = None
        self._save()
        return True

    def snapshot(self):
        """書く窓を開くときの控え。{text, written_at} か None。"""
        current = self._load()["current"]
        if not current:
            return None
        return {"text": current["text"], "written_at": current["written_at"]}

    def finish_edit_session(self, snapshot, now: str = None) -> bool:
        """書く窓を閉じたときの後始末。開いたときの内容と今の内容が違い、
        開いたときの内容が空でなければ、古い方を履歴へ移す。移したら True。

        窓が開いている間の自動保存は現在のメモを書き換え続けるので、古い内容は
        この控え(snapshot)からしか残せない。"""
        if not snapshot or not snapshot["text"].strip():
            return False
        current = self._load()["current"]
        current_text = current["text"] if current else ""
        if current_text == snapshot["text"]:
            return False
        self._archive(snapshot, REASON_REPLACED, now)
        self._save()
        return True


# ================================================================
# 窓
# ================================================================
# ここから下は Qt 依存。import はモジュール先頭にまとめず、Store だけを
# 使う側(テスト等)が Qt を引きずらないよう、窓のクラス定義の直前で行う。
from PySide6.QtCore import Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QDialog, QHBoxLayout, QLabel, QListWidget, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

# 入力が止まってから自動保存するまでの待ち(ms)。打つたびに書くと
# ディスクアクセスが増えるので、少しだけ溜める。閉じるときは待たずに書く。
AUTOSAVE_DELAY_MS = 400


class TomorrowMemoHistoryDialog(QDialog):
    """過去のメモを読む窓(読み取り専用)。"""

    def __init__(self, store: TomorrowMemoStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("過去のメモ")
        self.resize(480, 360)
        layout = QHBoxLayout(self)
        self._list = QListWidget(self)
        self._list.setMaximumWidth(210)
        layout.addWidget(self._list)
        self._view = QPlainTextEdit(self)
        self._view.setReadOnly(True)
        layout.addWidget(self._view)
        for item in store.history():
            label = format_written_at(item["written_at"])
            reason = _REASON_LABELS.get(item["reason"], "")
            if reason:
                label = f"{label}（{reason}）"
            self._list.addItem(label)
        self._items = store.history()
        self._list.currentRowChanged.connect(self._show_row)
        if self._items:
            self._list.setCurrentRow(0)
        else:
            self._view.setPlainText("（過去のメモはまだありません）")

    def _show_row(self, row: int) -> None:
        if 0 <= row < len(self._items):
            self._view.setPlainText(self._items[row]["text"])


class TomorrowMemoWindow(QWidget):
    """メモの窓。mode="read" は起動時の表示専用、mode="write" は書く用。

    閉じたら closed が出る(参照の始末と、スクリプトからの呼び出しへの
    応答は持ち主の役目)。
    """

    closed = Signal()

    def __init__(self, store: TomorrowMemoStore):
        super().__init__()
        self._store = store
        self._mode = "write"
        self._snapshot = None
        self.setWindowTitle("🌅 明日の自分へメモ")
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        self.resize(460, 330)

        layout = QVBoxLayout(self)
        self._date_label = QLabel(self)
        layout.addWidget(self._date_label)
        self._edit = QPlainTextEdit(self)
        layout.addWidget(self._edit)
        self._status_label = QLabel(self)
        layout.addWidget(self._status_label)
        buttons = QHBoxLayout()
        self._history_button = QPushButton("過去のメモ…", self)
        self._history_button.clicked.connect(self._show_history)
        buttons.addWidget(self._history_button)
        buttons.addStretch(1)
        self._write_button = QPushButton("メモを書く", self)
        self._write_button.clicked.connect(self.switch_to_write)
        buttons.addWidget(self._write_button)
        self._close_button = QPushButton("閉じる", self)
        self._close_button.clicked.connect(self.close)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._save_now)
        self._edit.textChanged.connect(self._on_text_changed)
        self._history_dialog = None

    # ---------------------------------------------------------------
    # 表示の切り替え
    # ---------------------------------------------------------------
    def show_write(self) -> None:
        """書く用に開く。開いた時点の内容を控え、閉じるときに変わっていたら
        古い方を履歴へ移す(finish_edit_session)。"""
        self._mode = "write"
        self._snapshot = self._store.snapshot()
        current = self._store.current()
        self._edit.blockSignals(True)
        self._edit.setPlainText(current["text"] if current else "")
        self._edit.blockSignals(False)
        self._edit.setReadOnly(False)
        if current:
            self._date_label.setText(
                f"{format_written_at(current['written_at'])} に書いたメモ")
        else:
            self._date_label.setText("明日の自分へのメモを書く")
        self._status_label.setText("書くと自動で保存されます")
        self._write_button.setVisible(False)
        self.show()
        self.raise_()
        self.activateWindow()
        self._edit.setFocus()

    def show_read(self) -> None:
        """起動時の表示用に開く(読み取り専用)。閉じると表示済みになり、
        内容は履歴へ移る。"""
        current = self._store.current()
        self._mode = "read"
        self._snapshot = None
        self._edit.blockSignals(True)
        self._edit.setPlainText(current["text"] if current else "")
        self._edit.blockSignals(False)
        self._edit.setReadOnly(True)
        if current:
            self._date_label.setText(
                f"{format_written_at(current['written_at'])} に書かれたメモ")
        self._status_label.setText("閉じると履歴に残ります")
        self._write_button.setVisible(True)
        self.show()
        self.raise_()
        self.activateWindow()

    def present_write(self) -> None:
        """外から「書く窓を開いて」と言われたときの入口。表示用の窓が
        出ている最中なら、表示していたメモを読んだものとして履歴へ
        移してから、書く用に切り替える。"""
        if self._mode == "read" and self.isVisible():
            self._store.mark_seen()
        self.show_write()

    def switch_to_write(self) -> None:
        """表示用の窓から、その場で書く用に切り替える。表示していたメモは
        読んだものとして履歴へ移してから、新しいメモを書き始める。"""
        if self._mode == "read":
            self._store.mark_seen()
        self.show_write()

    # ---------------------------------------------------------------
    # 自動保存
    # ---------------------------------------------------------------
    def _on_text_changed(self) -> None:
        if self._mode == "write":
            self._autosave_timer.start(AUTOSAVE_DELAY_MS)

    def _save_now(self) -> None:
        if self._mode != "write":
            return
        self._store.set_text(self._edit.toPlainText())
        current = self._store.current()
        if current:
            self._status_label.setText(
                f"保存しました（{format_written_at(current['written_at'])}）")
        else:
            self._status_label.setText("書くと自動で保存されます")

    # ---------------------------------------------------------------
    # 過去のメモ
    # ---------------------------------------------------------------
    def _show_history(self) -> None:
        if self._history_dialog is None:
            self._history_dialog = TomorrowMemoHistoryDialog(self._store, self)
            self._history_dialog.finished.connect(self._on_history_closed)
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _on_history_closed(self, _result) -> None:
        if self._history_dialog is not None:
            self._history_dialog.deleteLater()
            self._history_dialog = None

    # ---------------------------------------------------------------
    # 閉じる
    # ---------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt の作法)
        # 閉じる前に、溜まっている自動保存を必ず流す(本文を失わないため)。
        self._autosave_timer.stop()
        if self._mode == "write":
            self._save_now()
            self._store.finish_edit_session(self._snapshot)
            self._snapshot = None
        elif self._mode == "read":
            # 表示して閉じたので履歴へ移す(次回は出ない)。
            self._store.mark_seen()
        super().closeEvent(event)
        self.closed.emit()
