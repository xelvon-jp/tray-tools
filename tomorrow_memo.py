# tomorrow_memo.py
# 「明日の自分へメモ」。就業前に書いたメモが、次回の常駐起動時にポップアップする、
# 昨日の自分から今日の自分への手紙のような機能。
#
# 仕様(Nagato と文章で合意済み):
#   - メモは1件だけ。データは PC ローカル(%LOCALAPPDATA%\traytools\
#     tomorrow_memo.json)に持つ。settings.json には混ぜない。公開リポジトリの
#     フォルダには置かない(移行前の置き場に残っていたら、読み込み時に
#     新しい置き場へ取り込む。両方にあれば履歴を統合する)。
#   - 書いた内容は入力のたびに自動保存する(保存ボタンは無い)。
#   - 過去のメモは自動で履歴に残る。本文が失われる経路を作らない:
#       * 起動時に表示したメモは、閉じた時点で履歴へ移す(reason="seen")
#       * 書く窓で前の内容と違う内容に書き換えたら、最初の自動保存の時点で
#         書き換える前の内容を履歴へ移す(reason="replaced")。窓を閉じるまで
#         待たないので、書き換えの途中で PC が落ちても前の内容が残る
#     保存は一時ファイル + os.replace の原子的書き換えで、途中で落ちても
#     ファイルが半端な状態にはならない。
#   - 起動時に、まだ見ていない(未表示の)メモがあれば窓で出す。空なら出さない。
#     ただし出すのは「書いた日が今日より前」で「今日が最後に表示した日より
#     後」のときだけ(最後に表示した日はデータに覚えておく)。同じ日の再起動や
#     自動反映では出直さず、書いた当日にも出ない。判定は PC の起動時刻では
#     しない(長期連続稼働・休止・高速スタートアップでは起動時刻が戻らず、
#     当てにならないため)。
#   - 窓から過去のメモも読める(読み取り専用)。
#
# データの持ち方と窓をこのモジュールにまとめ、データ部分(TomorrowMemoStore)は
# Qt に依存させない(テストと将来の使い回しのため)。
import json
import os
from datetime import date, datetime
from pathlib import Path

DATA_FILENAME = "tomorrow_memo.json"
# 移行前の置き場(モジュールと同じフォルダ)。公開リポジトリの中なので、
# 今はここには置かない。残っていたら読み込み時に新しい置き場へ取り込む。
LEGACY_DATA_PATH = Path(__file__).resolve().parent / DATA_FILENAME


def data_dir() -> Path:
    """アプリデータの置き場(%LOCALAPPDATA% の下の traytools フォルダ)。

    photometa/config.py と同じ作法で、環境変数 TRAYTOOLS_DATA_DIR が
    あればそこを使う(テストが一時フォルダへ向けるため)。"""
    override = os.environ.get("TRAYTOOLS_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "traytools"


def default_data_path() -> Path:
    return data_dir() / DATA_FILENAME


DEFAULT_DATA_PATH = default_data_path()


def _today_str() -> str:
    return datetime.now().date().isoformat()


def _as_date_str(value) -> str:
    """日付の指定を "YYYY-MM-DD" にそろえる。date / datetime / ISO 文字列
    (日付だけでも日時でもよい)を受け、None なら今日。"""
    if value is None:
        return _today_str()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]

# 起動してから未表示メモを出すまでの待ち(ms)。トレイや他の起動処理が
# 落ち着いてから出す(呼び出し側 feature_screen が使う)。
STARTUP_SHOW_DELAY_MS = 3000

REASON_SEEN = "seen"        # 起動時に表示して閉じた
REASON_REPLACED = "replaced"  # 書く窓で書き換えられた
REASON_MERGED = "merged"    # 置き場の移行で統合された(古い方の現在のメモ)

_REASON_LABELS = {
    REASON_SEEN: "表示済み",
    REASON_REPLACED: "書き換え前",
    REASON_MERGED: "統合",
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
    return {"version": 1, "current": None, "history": [],
            "last_shown_date": None}


def _parse_state(data) -> dict:
    """ファイルから読んだ dict を state の形に整える。想定外の要素は捨てる。"""
    state = _empty_state()
    if not isinstance(data, dict):
        return state
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
    last_shown = data.get("last_shown_date")
    if isinstance(last_shown, str) and last_shown:
        state["last_shown_date"] = last_shown
    return state


def _read_state_file(path):
    """state ファイルを読む。無い・壊れている・dict でなければ None。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _parse_state(data)


def _merge_histories(*histories):
    """複数の履歴を1つに統合する。同じ記録は1つにし、新しい順に並べる。"""
    seen_keys = set()
    merged = []
    for history in histories:
        for item in history:
            key = (item["text"], item["written_at"],
                   item["archived_at"], item["reason"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(item)
    merged.sort(
        key=lambda item: (item["archived_at"], item["written_at"]),
        reverse=True)
    return merged


class TomorrowMemoStore:
    """tomorrow_memo.json の読み書き。Qt 非依存。

    state の形:
      {"version": 1,
       "current": {"text": str, "written_at": ISO, "seen": bool} | None,
       "history": [{"text": str, "written_at": ISO, "archived_at": ISO,
                    "reason": "seen" | "replaced" | "merged"}, ...],  # 新しい順
       "last_shown_date": "YYYY-MM-DD" | None}  # 起動時表示した最後の日
    """

    def __init__(self, path=None):
        # path を省略したとき(既定の置き場を使うとき)だけ、移行前の
        # 置き場からの取り込みを行う。テスト等が場所を指定した場合はしない。
        self._using_default_path = path is None
        self.path = Path(path) if path else default_data_path()
        self._state = None
        self._legacy_checked = False

    # ---------------------------------------------------------------
    # 読み書き
    # ---------------------------------------------------------------
    def _merge_legacy(self) -> None:
        """移行前の置き場(リポジトリのフォルダ)にデータが残っていたら、
        新しい置き場へ取り込む。

        新しい置き場にまだ何も無ければそのまま移る。両方にデータがある
        場合は統合する(履歴は重複を除いてまとめ、現在のメモは書いた日時が
        新しい方を残して、古い方は履歴へ移す)。本文が失われる経路を作ら
        ないため。取り込み終えたら元のファイルは消す(個人のメモを公開
        リポジトリのフォルダに残さないため。消せなくても、新しい置き場の
        ほうを読むので支障は無い)。"""
        if self._legacy_checked:
            return
        self._legacy_checked = True
        legacy_path = LEGACY_DATA_PATH
        if legacy_path == self.path:
            return
        legacy = _read_state_file(legacy_path)
        if legacy is None:
            return
        state = self._state
        changed = False
        # 現在のメモ: 書いた日時が新しい方を残し、古い方は履歴へ回す
        loser = None
        new_current = state["current"]
        legacy_current = legacy["current"]
        if legacy_current is not None:
            if new_current is None:
                state["current"] = legacy_current
                changed = True
            elif (new_current["text"] == legacy_current["text"]
                    and new_current["written_at"]
                    == legacy_current["written_at"]):
                # 同じメモが両方にある。表示済みかどうかだけそろえる
                if legacy_current["seen"] and not new_current["seen"]:
                    new_current["seen"] = True
                    changed = True
            elif legacy_current["written_at"] > new_current["written_at"]:
                loser = new_current
                state["current"] = legacy_current
                changed = True
            else:
                loser = legacy_current
        extra = []
        if loser is not None and loser["text"].strip():
            extra.append({
                "text": loser["text"],
                "written_at": loser["written_at"],
                "archived_at": _now_iso(),
                "reason": REASON_MERGED,
            })
        # 履歴: 重複を除いて統合する
        merged_history = _merge_histories(
            state["history"], legacy["history"] + extra)
        if merged_history != state["history"]:
            state["history"] = merged_history
            changed = True
        # 最後に表示した日: 遅い方を残す
        legacy_shown = legacy["last_shown_date"]
        if legacy_shown and (not state["last_shown_date"]
                             or legacy_shown > state["last_shown_date"]):
            state["last_shown_date"] = legacy_shown
            changed = True
        if changed:
            try:
                self._save()
            except OSError:
                return  # 保存できなければ元のファイルも残す(次回また試す)
        try:
            legacy_path.unlink()
        except OSError:
            pass

    def _load(self) -> dict:
        if self._state is not None:
            return self._state
        # 無い・壊れている場合は空として扱う。壊れた元ファイルは、最初の
        # 保存で丸ごと新しい状態に置き換わる(履歴も含めて失うのはこの場合
        # だけだが、壊れた JSON からは復元できないので仕方がない)。
        state = _read_state_file(self.path)
        self._state = state if state is not None else _empty_state()
        if self._using_default_path:
            self._merge_legacy()
        return self._state

    def _save(self) -> None:
        state = self._load()
        text = json.dumps(state, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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

    def has_unseen_for_startup(self, today=None) -> bool:
        """起動時に出すべき未表示メモがあるか(日付で判定する)。

        出すのは次の両方を満たすときだけ:
          - メモを書いた日が今日より前
          - 今日が、最後に表示した日より後(まだ表示したことが無ければ満たす)
        つまり「最後に表示した日より後の日付での、最初の起動」で1回だけ
        出す。同じ日の再起動では出ないし、書いた当日にも出ないので、自動
        反映や再起動のたびに、書いたばかりのメモが誰も見ていないのに
        表示済みになることもない。
        PC の起動時刻では判定しない(長期連続稼働や休止・高速スタート
        アップでは起動時刻が戻らず、当てにならないため)。
        書いた日時が読めない場合は出す側に倒す。
        today はテスト用で、date / datetime / "YYYY-MM-DD" のどれでも
        よく、省略時は今日。"""
        if not self.has_unseen():
            return False
        today_str = _as_date_str(today)
        last_shown = self._load()["last_shown_date"]
        if last_shown and today_str <= last_shown:
            return False
        try:
            written_day = datetime.fromisoformat(
                self._load()["current"]["written_at"]).date().isoformat()
        except (TypeError, ValueError):
            return True
        return written_day < today_str

    def last_shown_date(self):
        """最後に起動時表示した日("YYYY-MM-DD")。まだ無ければ None。"""
        return self._load()["last_shown_date"]

    def note_shown(self, today=None) -> None:
        """起動時に表示したことを覚えておく(同じ日には二度出さないため)。
        today はテスト用で、省略時は今日。"""
        state = self._load()
        day = _as_date_str(today)
        if state["last_shown_date"] != day:
            state["last_shown_date"] = day
            self._save()

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

    def archive_snapshot(self, snapshot, now: str = None) -> bool:
        """控え(書く窓を開いた時点の内容)を履歴へ移す。移したら True。

        窓が開いている間の自動保存は現在のメモを書き換え続けるので、古い内容は
        この控え(snapshot)からしか残せない。"""
        if not snapshot or not snapshot["text"].strip():
            return False
        self._archive(snapshot, REASON_REPLACED, now)
        self._save()
        return True

    def finish_edit_session(self, snapshot, now: str = None) -> bool:
        """書く窓を閉じたときの後始末。開いたときの内容と今の内容が違い、
        開いたときの内容が空でなければ、古い方を履歴へ移す。移したら True。"""
        if not snapshot or not snapshot["text"].strip():
            return False
        current = self._load()["current"]
        current_text = current["text"] if current else ""
        if current_text == snapshot["text"]:
            return False
        return self.archive_snapshot(snapshot, now)


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
        self._snapshot_archived = False
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
        """書く用に開く。開いた時点の内容を控え、内容が変わったら最初の
        自動保存の時点で古い方を履歴へ移す(_archive_snapshot_if_changed)。"""
        self._mode = "write"
        self._snapshot = self._store.snapshot()
        self._snapshot_archived = False
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

    def _archive_snapshot_if_changed(self) -> None:
        """開いた時点の内容(控え)と今の内容が違えば、控えを履歴へ移す。

        最初の自動保存の時点で移すのが肝で、窓を閉じるまで待つと、書き換えの
        途中で PC が落ちたときに前のメモが失われる。一度移したら、閉じるときに
        二重に移さないよう印を付ける。"""
        if self._snapshot_archived or not self._snapshot:
            return
        if not self._snapshot["text"].strip():
            return
        if self._edit.toPlainText() == self._snapshot["text"]:
            return
        if self._store.archive_snapshot(self._snapshot):
            self._snapshot_archived = True

    def _save_now(self) -> None:
        if self._mode != "write":
            return
        self._archive_snapshot_if_changed()
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
            if not self._snapshot_archived:
                # 自動保存が一度も走らなかった場合の保険(開いてすぐ閉じた等)。
                # 既に移してある場合は二重に移さない。
                self._store.finish_edit_session(self._snapshot)
            self._snapshot = None
        elif self._mode == "read":
            # 表示して閉じたので履歴へ移す(次回は出ない)。
            self._store.mark_seen()
        super().closeEvent(event)
        self.closed.emit()
