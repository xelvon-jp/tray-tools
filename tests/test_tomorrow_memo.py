# tests/test_tomorrow_memo.py
# 「明日の自分へメモ」(tomorrow_memo.py)のテスト。
#
#   - データの保存と読み戻し(自動保存・書いた日時・表示済み)
#   - データの置き場(アプリデータ)と、移行前の置き場からの取り込み
#     (両方にファイルがあるときは履歴を統合する。旧ファイルを消せなくて
#     統合が繰り返し走っても履歴は増えない)
#   - 起動時の表示は日付で判定する(書いた日が今日より前で、最後に表示
#     した日より後の日付での最初の起動だけ。同じ日の再起動では出ない)
#   - バックアップ(履歴)への移動: 表示して閉じたとき／書き換えたとき
#     (最初の自動保存の時点で移る。閉じるときに二重に移らない)
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
from datetime import date
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


def test_default_path_uses_data_dir():
    # 既定の置き場はリポジトリのフォルダではなく、アプリデータ
    # (%LOCALAPPDATA%\traytools 相当。テストでは TRAYTOOLS_DATA_DIR で差す)。
    tmp = tempfile.TemporaryDirectory()
    old = os.environ.get("TRAYTOOLS_DATA_DIR")
    os.environ["TRAYTOOLS_DATA_DIR"] = tmp.name
    try:
        store = tomorrow_memo.TomorrowMemoStore()
        assert store.path == Path(tmp.name) / "tomorrow_memo.json"
        store.set_text("アプリデータ側に書かれる", now="2026-11-13T19:20:00")
        assert (Path(tmp.name) / "tomorrow_memo.json").exists()
        assert not (ROOT / "tomorrow_memo.json").exists()
    finally:
        if old is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = old
        tmp.cleanup()


def test_legacy_file_imported_on_first_load():
    # 移行前の置き場(リポジトリのフォルダ)に残っていたデータは、既定の
    # 置き場を使う Store の最初の読み込みで新しい置き場へ取り込まれる。
    new_tmp = tempfile.TemporaryDirectory()
    legacy_tmp = tempfile.TemporaryDirectory()
    legacy_path = Path(legacy_tmp.name) / "tomorrow_memo.json"
    legacy_store = tomorrow_memo.TomorrowMemoStore(legacy_path)
    legacy_store.set_text("移行前のメモ", now="2026-11-13T19:20:00")
    old_env = os.environ.get("TRAYTOOLS_DATA_DIR")
    old_legacy = tomorrow_memo.LEGACY_DATA_PATH
    os.environ["TRAYTOOLS_DATA_DIR"] = new_tmp.name
    tomorrow_memo.LEGACY_DATA_PATH = legacy_path
    try:
        store = tomorrow_memo.TomorrowMemoStore()
        current = store.current()
        assert current is not None
        assert current["text"] == "移行前のメモ"
        # 新しい置き場にファイルができ、元の場所からは消える
        assert (Path(new_tmp.name) / "tomorrow_memo.json").exists()
        assert not legacy_path.exists()
    finally:
        tomorrow_memo.LEGACY_DATA_PATH = old_legacy
        if old_env is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = old_env
        new_tmp.cleanup()
        legacy_tmp.cleanup()


def test_startup_visibility_depends_on_dates():
    tmp, store = make_store()
    store.set_text("昨夜のメモ", now="2026-11-13T19:20:00")
    # 書いた当日は出さない(その日のうちの再起動や自動反映で出直さない)
    assert store.has_unseen_for_startup(date(2026, 11, 13)) is False
    # 翌日なら出す(日付は文字列で渡してもよい)
    assert store.has_unseen_for_startup("2026-11-14") is True
    # 表示したことを覚えたら、その日はもう出ない(窓が閉じられずに
    # 未表示のまま残っても)
    store.note_shown("2026-11-14")
    assert store.last_shown_date() == "2026-11-14"
    assert store.has_unseen_for_startup(date(2026, 11, 14)) is False
    # 表示した日はファイルに残り、読み直しても効く
    reloaded = tomorrow_memo.TomorrowMemoStore(store.path)
    assert reloaded.last_shown_date() == "2026-11-14"
    assert reloaded.has_unseen_for_startup(date(2026, 11, 14)) is False
    # さらに翌日なら、未表示のままならまた出す
    assert store.has_unseen_for_startup(date(2026, 11, 15)) is True
    # 表示済み(履歴へ移動)になったら、日付によらず出さない
    store.mark_seen()
    assert store.has_unseen_for_startup(date(2026, 11, 15)) is False
    assert store.has_unseen_for_startup() is False
    tmp.cleanup()


def test_legacy_merged_when_both_files_exist():
    # 新しい置き場にも移行前の置き場にもファイルがある場合は、履歴が
    # 統合され(重複は1つに)、現在のメモは書いた日時が新しい方が残り、
    # 古い方は履歴へ移る。移行前のファイルは消える。
    new_tmp = tempfile.TemporaryDirectory()
    legacy_tmp = tempfile.TemporaryDirectory()
    legacy_path = Path(legacy_tmp.name) / "tomorrow_memo.json"
    legacy_store = tomorrow_memo.TomorrowMemoStore(legacy_path)
    legacy_store.set_text("共通のメモ", now="2026-11-09T19:00:00")
    legacy_store.mark_seen(now="2026-11-10T08:00:00")
    legacy_store.set_text("移行前の古いメモ", now="2026-11-10T19:00:00")
    legacy_store.mark_seen(now="2026-11-11T08:00:00")
    legacy_store.set_text("移行前の現在のメモ", now="2026-11-12T19:00:00")
    new_path = Path(new_tmp.name) / "tomorrow_memo.json"
    new_store = tomorrow_memo.TomorrowMemoStore(new_path)
    new_store.set_text("共通のメモ", now="2026-11-09T19:00:00")
    new_store.mark_seen(now="2026-11-10T08:00:00")
    new_store.set_text("新しい側の古いメモ", now="2026-11-11T19:00:00")
    new_store.mark_seen(now="2026-11-12T08:00:00")
    new_store.set_text("新しい側の現在のメモ", now="2026-11-13T19:00:00")
    old_env = os.environ.get("TRAYTOOLS_DATA_DIR")
    old_legacy = tomorrow_memo.LEGACY_DATA_PATH
    os.environ["TRAYTOOLS_DATA_DIR"] = new_tmp.name
    tomorrow_memo.LEGACY_DATA_PATH = legacy_path
    try:
        store = tomorrow_memo.TomorrowMemoStore()
        # 現在のメモは、新しい側(書いた日時が新しい方)が残る
        current = store.current()
        assert current is not None
        assert current["text"] == "新しい側の現在のメモ"
        history = store.history()
        texts = [item["text"] for item in history]
        # 両方の履歴が統合され、共通の記録は1つだけ
        assert texts.count("共通のメモ") == 1
        assert "移行前の古いメモ" in texts
        assert "新しい側の古いメモ" in texts
        # 負けた側の現在のメモも、履歴(reason=merged)に残る
        merged = [item for item in history if item["reason"] == "merged"]
        assert len(merged) == 1
        assert merged[0]["text"] == "移行前の現在のメモ"
        assert len(history) == 4
        # 移行前のファイルは消え、読み直しても二重に統合されない
        assert not legacy_path.exists()
        reloaded = tomorrow_memo.TomorrowMemoStore()
        assert len(reloaded.history()) == 4
        assert reloaded.current()["text"] == "新しい側の現在のメモ"
    finally:
        tomorrow_memo.LEGACY_DATA_PATH = old_legacy
        if old_env is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = old_env
        new_tmp.cleanup()
        legacy_tmp.cleanup()


def test_legacy_merge_does_not_grow_when_legacy_file_stays():
    # 旧ファイル(移行元)を消せなかった場合、次回の起動でも統合が走る。
    # そのときに履歴が増え続けないこと(統合で足す記録の archived_at は
    # 書いた日時で固定され、重複判定が毎回同じ結果になる)。
    new_tmp = tempfile.TemporaryDirectory()
    legacy_tmp = tempfile.TemporaryDirectory()
    legacy_path = Path(legacy_tmp.name) / "tomorrow_memo.json"
    legacy_store = tomorrow_memo.TomorrowMemoStore(legacy_path)
    legacy_store.set_text("共通のメモ", now="2026-11-09T19:00:00")
    legacy_store.mark_seen(now="2026-11-10T08:00:00")
    legacy_store.set_text("移行前の現在のメモ", now="2026-11-12T19:00:00")
    new_path = Path(new_tmp.name) / "tomorrow_memo.json"
    new_store = tomorrow_memo.TomorrowMemoStore(new_path)
    new_store.set_text("共通のメモ", now="2026-11-09T19:00:00")
    new_store.mark_seen(now="2026-11-10T08:00:00")
    new_store.set_text("新しい側の現在のメモ", now="2026-11-13T19:00:00")
    old_env = os.environ.get("TRAYTOOLS_DATA_DIR")
    old_legacy = tomorrow_memo.LEGACY_DATA_PATH
    os.environ["TRAYTOOLS_DATA_DIR"] = new_tmp.name
    tomorrow_memo.LEGACY_DATA_PATH = legacy_path
    # 旧ファイルを消せない状態を模す(unlink が失敗しても統合自体は進む)。
    # あわせて現在時刻が毎回違う値を返すようにし、「archived_at が現在
    # 時刻だと重複判定をすり抜ける」不具合を確実に再現できるようにする。
    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        raise OSError("消せない状態を模す")

    ticks = [0]
    real_now_iso = tomorrow_memo._now_iso

    def advancing_now():
        ticks[0] += 1
        return f"2026-11-15T08:00:{ticks[0]:02d}"

    Path.unlink = failing_unlink
    tomorrow_memo._now_iso = advancing_now
    try:
        store = tomorrow_memo.TomorrowMemoStore()
        history = store.history()
        assert len(history) == 2
        merged = [item for item in history if item["reason"] == "merged"]
        assert len(merged) == 1
        assert merged[0]["text"] == "移行前の現在のメモ"
        # archived_at は書いた日時で固定されている
        assert merged[0]["archived_at"] == merged[0]["written_at"]
        assert legacy_path.exists()  # 消せていない
        # 起動し直すたびに統合が走っても、履歴は増えない
        for _ in range(3):
            reloaded = tomorrow_memo.TomorrowMemoStore()
            assert len(reloaded.history()) == 2
            merged = [item for item in reloaded.history()
                      if item["reason"] == "merged"]
            assert len(merged) == 1
            assert reloaded.current()["text"] == "新しい側の現在のメモ"
    finally:
        Path.unlink = real_unlink
        tomorrow_memo._now_iso = real_now_iso
        tomorrow_memo.LEGACY_DATA_PATH = old_legacy
        if old_env is None:
            os.environ.pop("TRAYTOOLS_DATA_DIR", None)
        else:
            os.environ["TRAYTOOLS_DATA_DIR"] = old_env
        new_tmp.cleanup()
        legacy_tmp.cleanup()


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


def test_window_first_autosave_archives_previous():
    # 書き換えたら、窓を閉じる前(最初の自動保存の時点)にもう前の内容が
    # 履歴へ移っていること。途中で落ちても前のメモが残るための作り。
    app()
    tmp, store = make_store()
    store.set_text("前のメモ", now="2026-11-13T19:20:00")
    window = tomorrow_memo.TomorrowMemoWindow(store)
    window.show_write()
    window._edit.setPlainText("書き換え中のメモ")
    assert store.history() == []  # まだ保存が走っていない
    window._save_now()  # 自動保存(タイマーの代わりに直接呼ぶ)
    history = store.history()
    assert len(history) == 1
    assert history[0]["text"] == "前のメモ"
    assert history[0]["reason"] == "replaced"
    assert store.current()["text"] == "書き換え中のメモ"
    # さらに書き換えて閉じても、二重には移らない
    window._edit.setPlainText("さらに書き換えた")
    window.close()
    app().processEvents()
    assert len(store.history()) == 1
    assert store.current()["text"] == "さらに書き換えた"
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
    feature._show_unseen_tomorrow_memo(today=date(2026, 11, 14))
    assert feature.tomorrow_memo_window is None  # 空なら出さない
    store.set_text("未表示のメモ", now="2026-11-13T19:20:00")
    feature._show_unseen_tomorrow_memo(today=date(2026, 11, 14))
    window = feature.tomorrow_memo_window
    assert window is not None
    assert window._edit.isReadOnly()  # 起動時は表示用
    window.close()
    app().processEvents()
    assert store.has_unseen() is False
    tmp.cleanup()


def test_feature_startup_respects_dates():
    app()
    tmp, store = make_store()
    feature = make_feature(store)
    store.set_text("昨夜のメモ", now="2026-11-13T19:20:00")
    # 書いた当日は出さない
    feature._show_unseen_tomorrow_memo(today=date(2026, 11, 13))
    assert feature.tomorrow_memo_window is None
    # 翌日なら出す
    feature._show_unseen_tomorrow_memo(today=date(2026, 11, 14))
    window = feature.tomorrow_memo_window
    assert window is not None
    assert window._edit.isReadOnly()
    # 表示した日が記録される。窓が閉じられないまま失われて、同じ日に
    # 起動し直しても出直さない
    assert store.last_shown_date() == "2026-11-14"
    feature.tomorrow_memo_window = None  # 閉じずに窓が失われた状態を模す
    feature._show_unseen_tomorrow_memo(today=date(2026, 11, 14))
    assert feature.tomorrow_memo_window is None
    window.close()
    app().processEvents()
    tmp.cleanup()


def test_command_registration_strings():
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"tomorrow-memo"' in main_text
    assert "_tomorrow_memo_command" in main_text
    send_text = (ROOT / "traytools_send.py").read_text(encoding="utf-8")
    assert '"tomorrow-memo": 3660.0' in send_text
    menu_text = (ROOT / "feature_screen.py").read_text(encoding="utf-8")
    assert "明日の自分へメモ" in menu_text


def test_gitignore_lists_tomorrow_memo():
    # 置き場はアプリデータへ移したが、移行前の置き場に残った場合の保険として
    # .gitignore にも名前が入っていること(public リポジトリへの混入防止)。
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "tomorrow_memo.json" in gitignore


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
        test_default_path_uses_data_dir,
        test_legacy_file_imported_on_first_load,
        test_legacy_merged_when_both_files_exist,
        test_legacy_merge_does_not_grow_when_legacy_file_stays,
        test_startup_visibility_depends_on_dates,
        test_window_write_autosaves,
        test_window_first_autosave_archives_previous,
        test_window_read_close_marks_seen,
        test_window_switch_to_write_archives_read_memo,
        test_feature_opens_write_window_and_notifies_waiter,
        test_feature_startup_shows_only_unseen,
        test_feature_startup_respects_dates,
        test_command_registration_strings,
        test_gitignore_lists_tomorrow_memo,
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
