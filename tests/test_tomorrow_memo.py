# tests/test_tomorrow_memo.py
# 「明日の自分へメモ」(tomorrow_memo.py)のテスト。
#
#   - データの保存と読み戻し(自動保存・書いた日時・表示済み)
#   - バックアップ(履歴)への移動: 表示して閉じたとき／書き換えたとき
#   - 本文が失われる経路が無いこと(空にしても古い内容は履歴に残る)
#   - 窓の自動保存と、起動時の表示→閉じると表示済みになること
#   - feature_screen 側の接着(窓は1つだけ・閉じたら呼び出し側へ知らせる)
#
# main.py のコマンド登録そのものは import が Windows 依存(comtypes 等)で
# VM では読めないため、登録文字列の静的チェックに留める(口の実動作は
# 実機確認待ち)。feature_screen の import には mss が要るので、実機では
# 本物が入る前提で、テストの中だけ空モジュールを差す
# (test_editor_restart_blockers.py と同じ作法)。
#
#   python tests/test_tomorrow_memo.py
import os
import sys
import tempfile
import time
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import mss  # noqa: F401
except ImportError:
    sys.modules["mss"] = types.ModuleType("mss")

from PySide6.QtWidgets import QApplication  # noqa: E402

import feature_screen  # noqa: E402
import tomorrow_memo  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_store():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "tomorrow_memo.json"
    return tmp, tomorrow_memo.TomorrowMemoStore(path)


def make_feature(store):
    """ScreenFeature の接着メソッドだけを使うための最小インスタンス。
    __init__ は回さず、この機能が触る属性だけを持たせる。"""
    feature = object.__new__(feature_screen.ScreenFeature)
    feature.tomorrow_memo_window = None
    feature._tomorrow_memo_store = store
    feature._tomorrow_memo_waiters = []
    return feature


# ================================================================
# データ(TomorrowMemoStore)
# ================================================================
def test_save_and_reload():
    tmp, store = make_store()
    store.set_text("明日の自分へ\n今日の続きをやること", now="2026-11-13T19:20:00")
    reloaded = tomorrow_memo.TomorrowMemoStore(store.path)
    current = reloaded.current()
    assert current is not None
    assert current["text"] == "明日の自分へ\n今日の続きをやること"
    assert current["written_at"] == "2026-11-13T19:20:00"
    assert current["seen"] is False
    assert reloaded.has_unseen() is True
    tmp.cleanup()


def test_empty_text_clears_current():
    tmp, store = make_store()
    store.set_text("何か書いた", now="2026-11-13T19:20:00")
    store.set_text("   ", now="2026-11-13T19:21:00")
    assert store.current() is None
    assert store.has_unseen() is False
    tmp.cleanup()


def test_written_at_updates_only_when_text_changes():
    tmp, store = make_store()
    store.set_text("同じ文", now="2026-11-13T19:20:00")
    store.set_text("同じ文", now="2026-11-13T19:25:00")
    assert store.current()["written_at"] == "2026-11-13T19:20:00"
    store.set_text("変わった文", now="2026-11-13T19:26:00")
    assert store.current()["written_at"] == "2026-11-13T19:26:00"
    tmp.cleanup()


def test_mark_seen_moves_to_history():
    tmp, store = make_store()
    store.set_text("昨日のメモ", now="2026-11-13T19:20:00")
    assert store.mark_seen(now="2026-11-14T08:00:00") is True
    assert store.current() is None
    assert store.has_unseen() is False
    history = store.history()
    assert len(history) == 1
    assert history[0]["text"] == "昨日のメモ"
    assert history[0]["written_at"] == "2026-11-13T19:20:00"
    assert history[0]["archived_at"] == "2026-11-14T08:00:00"
    assert history[0]["reason"] == "seen"
    # もう一度呼んでも何も起きない
    assert store.mark_seen() is False
    assert len(store.history()) == 1
    tmp.cleanup()


def test_edit_session_replace_archives_old_text():
    tmp, store = make_store()
    store.set_text("古いメモ", now="2026-11-13T19:20:00")
    snapshot = store.snapshot()
    store.set_text("新しいメモ", now="2026-11-13T19:30:00")
    assert store.finish_edit_session(snapshot, now="2026-11-13T19:31:00") is True
    history = store.history()
    assert len(history) == 1
    assert history[0]["text"] == "古いメモ"
    assert history[0]["reason"] == "replaced"
    assert store.current()["text"] == "新しいメモ"
    tmp.cleanup()


def test_edit_session_no_change_keeps_history_empty():
    tmp, store = make_store()
    store.set_text("そのまま", now="2026-11-13T19:20:00")
    snapshot = store.snapshot()
    assert store.finish_edit_session(snapshot) is False
    assert store.history() == []
    tmp.cleanup()


def test_edit_session_clear_archives_old_text():
    # 全部消しても、本文が失われる経路は無い(古い内容は履歴に残る)。
    tmp, store = make_store()
    store.set_text("消してはいけないメモ", now="2026-11-13T19:20:00")
    snapshot = store.snapshot()
    store.set_text("")
    assert store.current() is None
    assert store.finish_edit_session(snapshot) is True
    assert store.history()[0]["text"] == "消してはいけないメモ"
    tmp.cleanup()


def test_broken_file_is_treated_as_empty():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "tomorrow_memo.json"
    path.write_text("{壊れたJSON", encoding="utf-8")
    store = tomorrow_memo.TomorrowMemoStore(path)
    assert store.current() is None
    assert store.has_unseen() is False
    store.set_text("書き直せる", now="2026-11-13T19:20:00")
    assert store.current()["text"] == "書き直せる"
    tmp.cleanup()


# ================================================================
# 窓(TomorrowMemoWindow)
# ================================================================
def test_window_write_autosaves():
    app()
    tmp, store = make_store()
    window = tomorrow_memo.TomorrowMemoWindow(store)
    closed = []
    window.closed.connect(lambda: closed.append(True))
    window.show_write()
    window._edit.setPlainText("窓から書いたメモ")
    # 自動保存のタイマーが実際に効くこと(少し待ってイベントを回す)。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app().processEvents()
        current = store.current()
        if current and current["text"] == "窓から書いたメモ":
            break
        time.sleep(0.05)
    assert store.current()["text"] == "窓から書いたメモ"
    window.close()
    app().processEvents()
    assert closed == [True]
    tmp.cleanup()


def test_window_read_close_marks_seen():
    app()
    tmp, store = make_store()
    store.set_text("昨日の自分から", now="2026-11-13T19:20:00")
    window = tomorrow_memo.TomorrowMemoWindow(store)
    window.show_read()
    assert window._edit.isReadOnly()
    assert window._edit.toPlainText() == "昨日の自分から"
    window.close()
    app().processEvents()
    assert store.current() is None
    assert len(store.history()) == 1
    assert store.history()[0]["text"] == "昨日の自分から"
    tmp.cleanup()


def test_window_switch_to_write_archives_read_memo():
    app()
    tmp, store = make_store()
    store.set_text("読んだメモ", now="2026-11-13T19:20:00")
    window = tomorrow_memo.TomorrowMemoWindow(store)
    window.show_read()
    window.switch_to_write()
    assert not window._edit.isReadOnly()
    assert store.current() is None  # 読んだ分は履歴へ
    assert store.history()[0]["text"] == "読んだメモ"
    window._edit.setPlainText("今日のメモ")
    window.close()
    app().processEvents()
    assert store.current()["text"] == "今日のメモ"
    tmp.cleanup()


# ================================================================
# feature_screen 側の接着
# ================================================================
def test_feature_opens_write_window_and_notifies_waiter():
    app()
    tmp, store = make_store()
    feature = make_feature(store)
    calls = []
    assert feature.open_tomorrow_memo_for_call(lambda: calls.append(True)) is True
    window = feature.tomorrow_memo_window
    assert window is not None
    # もう一度開いても窓は1つだけ(前面に出すだけ)
    assert feature.open_tomorrow_memo() is True
    assert feature.tomorrow_memo_window is window
    window._edit.setPlainText("スクリプトから書いた")
    window.close()
    app().processEvents()
    assert calls == [True]
    assert feature.tomorrow_memo_window is None
    assert store.current()["text"] == "スクリプトから書いた"
    tmp.cleanup()


def test_feature_startup_shows_only_unseen():
    app()
    tmp, store = make_store()
    feature = make_feature(store)
    feature._show_unseen_tomorrow_memo()
    assert feature.tomorrow_memo_window is None  # 空なら出さない
    store.set_text("未表示のメモ", now="2026-11-13T19:20:00")
    feature._show_unseen_tomorrow_memo()
    window = feature.tomorrow_memo_window
    assert window is not None
    assert window._edit.isReadOnly()  # 起動時は表示用
    window.close()
    app().processEvents()
    assert store.has_unseen() is False
    tmp.cleanup()


def test_command_registration_strings():
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"tomorrow-memo"' in main_text
    assert "_tomorrow_memo_command" in main_text
    send_text = (ROOT / "traytools_send.py").read_text(encoding="utf-8")
    assert '"tomorrow-memo": 3660.0' in send_text
    menu_text = (ROOT / "feature_screen.py").read_text(encoding="utf-8")
    assert "明日の自分へメモ" in menu_text


if __name__ == "__main__":
    tests = [
        test_save_and_reload,
        test_empty_text_clears_current,
        test_written_at_updates_only_when_text_changes,
        test_mark_seen_moves_to_history,
        test_edit_session_replace_archives_old_text,
        test_edit_session_no_change_keeps_history_empty,
        test_edit_session_clear_archives_old_text,
        test_broken_file_is_treated_as_empty,
        test_window_write_autosaves,
        test_window_read_close_marks_seen,
        test_window_switch_to_write_archives_read_memo,
        test_feature_opens_write_window_and_notifies_waiter,
        test_feature_startup_shows_only_unseen,
        test_command_registration_strings,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
