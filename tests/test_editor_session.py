# tests/test_editor_session.py
# EDDy のタブ＋メモ帳式セッション(控え)を確かめる。
#
#   - ファイルはタブとして開く。同じファイルは既存タブを前に出す
#   - 編集内容は控え(ドラフト)として自動で取られ、ファイル自体は
#     明示保存まで書き換わらない
#   - 窓を閉じても確認は出ず、次に開くとタブ・未保存の内容・
#     カーソル位置が戻る(無題も同じ)
#   - 控えがある間にファイルが外で書き換えられていたら、控えを
#     優先して戻し、外部変更として扱う
#   - セッション有効な窓は、未保存でも再起動の妨げにならない
#
# セッションの置き場はテストごとに一時フォルダのファイルへ向ける
# (session_path 引数)。本物の %LOCALAPPDATA% は汚さない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_session.py
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
from editor import session as sessionmod  # noqa: E402
from editor.window import dirty_window_state  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window(session_dir, path=None):
    app()
    window = editor.create_window(
        path, session_path=Path(session_dir) / "editor_session.json")
    window.show()
    return window


def close_window(window):
    window.close()
    window.deleteLater()
    app().processEvents()


def type_text(window, text):
    """実際の編集と同じ経路(アンドゥに乗り、変更ありになる)で入れる。"""
    window._edit.textCursor().insertText(text)


def set_cursor(window, position, anchor=None):
    cursor = window._edit.textCursor()
    cursor.setPosition(position if anchor is None else anchor)
    if anchor is not None:
        cursor.setPosition(position, QTextCursor.MoveMode.KeepAnchor)
    window._edit.setTextCursor(cursor)


def test_tabs_open_and_reuse():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.txt")
        b = os.path.join(tmp, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(tmp, a)
        try:
            assert window.tab_count() == 1
            assert window.open_path(b)
            assert window.tab_count() == 2
            assert window.current_document().path == b
            # 同じファイルをもう一度開いても、タブは増えず前に出る。
            assert window.open_path(a)
            assert window.tab_count() == 2
            assert window.current_document().path == a
            assert window.contains_path(b)
            assert window.open_paths() == [a, b]
            # Ctrl+Tab 相当の前後移動。
            window.activate_next_tab()
            assert window.current_document().path == b
            window.activate_prev_tab()
            assert window.current_document().path == a
        finally:
            close_window(window)


def test_draft_restores_unsaved_text_and_cursor():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        original = "元の文です\n".encode("utf-8")
        Path(path).write_bytes(original)
        window = make_window(tmp, path)
        type_text(window, "追記")
        set_cursor(window, 3)
        close_window(window)
        # ファイル自体は書き換わっていない(控えだけが取れた)。
        assert Path(path).read_bytes() == original
        window2 = make_window(tmp)
        try:
            assert window2.tab_count() == 1
            assert window2.current_document().path == path
            assert window2.text() == "追記元の文です\n"
            assert window2.is_dirty()  # 未保存のまま戻る
            assert window2._edit.textCursor().position() == 3
        finally:
            close_window(window2)


def test_draft_restores_untitled():
    with tempfile.TemporaryDirectory() as tmp:
        window = make_window(tmp)
        type_text(window, "無題の書きかけ")
        close_window(window)
        window2 = make_window(tmp)
        try:
            assert window2.tab_count() == 1
            assert window2.current_document().path is None
            assert window2.text() == "無題の書きかけ"
            assert window2.is_dirty()
        finally:
            close_window(window2)


def test_clean_tabs_restore_from_disk():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.txt")
        b = os.path.join(tmp, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(tmp, a)
        assert window.open_path(b)
        close_window(window)
        window2 = make_window(tmp)
        try:
            assert window2.tab_count() == 2
            assert window2.open_paths() == [a, b]
            # 最後に前に出ていたタブ(b)がアクティブで戻る。
            assert window2.current_document().path == b
            assert not window2.is_dirty()
        finally:
            close_window(window2)


def test_explicit_save_writes_file_and_clears_draft():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("前\n".encode("utf-8"))
        window = make_window(tmp, path)
        type_text(window, "書")
        close_window(window)
        window2 = make_window(tmp)
        try:
            assert window2.is_dirty()
            assert window2.save()
            assert Path(path).read_bytes() == "書前\n".encode("utf-8")
            assert not window2.is_dirty()
        finally:
            close_window(window2)
        # 保存後は、控えも保存済みの状態で記録される(戻しても
        # 未保存にはならない)。
        window3 = make_window(tmp)
        try:
            assert window3.text() == "書前\n"
            assert not window3.is_dirty()
        finally:
            close_window(window3)


def test_external_change_draft_wins():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("元の文\n".encode("utf-8"))
        window = make_window(tmp, path)
        type_text(window, "編")
        close_window(window)
        # 閉じている間に、外のソフトが書き換えた。
        Path(path).write_bytes("外部の新しい文（長い）\n".encode("utf-8"))
        window2 = make_window(tmp)
        try:
            # 控えを優先して戻す(外部の文にはならない)。
            assert window2.text() == "編元の文\n"
            assert window2.is_dirty()
            assert window2.external_change_pending
            assert "外部で変更されました" in (
                window2.statusBar().currentMessage())
        finally:
            close_window(window2)


def test_missing_file_clean_tab_dropped_dirty_tab_kept():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.txt")
        b = os.path.join(tmp, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(tmp, a)
        assert window.open_path(b)
        type_text(window, "追")  # b は未保存の控えあり
        close_window(window)
        os.unlink(a)  # 変更の無いタブのファイルが消えた
        os.unlink(b)  # 控えのあるタブのファイルも消えた
        window2 = make_window(tmp)
        try:
            # 変更の無いタブは静かに落ち、控えのあるタブは内容が戻る。
            assert window2.tab_count() == 1
            assert window2.current_document().path == b
            assert window2.text() == "追い\n"
            assert window2.is_dirty()
        finally:
            close_window(window2)


def test_close_tab_forgets_it():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.txt")
        b = os.path.join(tmp, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(tmp, a)
        assert window.open_path(b)
        type_text(window, "追")
        # タブを閉じたら、そのタブはセッションから外れる(窓を
        # 閉じた場合と違い、次回は戻らない。メモ帳と同じ)。
        assert window.current_document().path == b
        window.close_tab()
        assert window.tab_count() == 1
        close_window(window)
        window2 = make_window(tmp)
        try:
            assert window2.tab_count() == 1
            assert window2.open_paths() == [a]
        finally:
            close_window(window2)


def test_session_window_is_not_restart_blocker():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "a.txt")
        Path(path).write_bytes("文\n".encode("utf-8"))
        window = make_window(tmp, path)
        try:
            type_text(window, "未保存")
            assert window.is_dirty()
            # 控えが常に取れているので、失われる件数は 0。
            assert window.restart_loss_count() == 0
            assert dirty_window_state([window]) == (0, 0)
        finally:
            close_window(window)
    # セッション無しの窓(従来の作り)は、未保存のタブ数が数えられる。
    app()
    plain = editor.create_window()
    plain.show()
    try:
        type_text(plain, "x")
        assert plain.restart_loss_count() == 1
        assert dirty_window_state([plain]) == (1, 0)
    finally:
        close_window(plain)


def test_session_window_with_failed_save_is_restart_blocker():
    """控えの書き込みが失敗している窓だけは、再起動の妨げに数える。
    書けている間の未保存タブは、従来どおり数えない。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        try:
            type_text(window, "控えが書けない編集")
            assert window.restart_loss_count() == 0
            assert dirty_window_state([window]) == (0, 0)
            original = _break_session_save()
            try:
                assert window._flush_session() is False
                assert window.restart_loss_count() == 1
                assert dirty_window_state([window]) == (1, 0)
            finally:
                sessionmod.save = original
            # 書けるようになれば、次に書けた時点で数えなくなる。
            assert window._flush_session() is True
            assert window.restart_loss_count() == 0
            assert dirty_window_state([window]) == (0, 0)
        finally:
            close_window(window)


def test_session_save_max_wait_after_three_seconds():
    """入力が続いても、最初の変更から3秒で必ず控えを書く。静かな
    場面では従来どおり 500ms で書く。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        try:
            clock = {"now": 1000.0}
            original = time.monotonic
            time.monotonic = lambda: clock["now"]
            try:
                window._schedule_session_save()
                assert window._session_timer.interval() == 500
                # 2.6秒後も、残りは 400ms に縮む。
                clock["now"] = 1002.6
                window._schedule_session_save()
                assert window._session_timer.interval() == 400
                clock["now"] = 1002.9
                window._schedule_session_save()
                assert window._session_timer.interval() == 100
                # 3秒経ったら、止まるのを待たずにすぐ書く。
                clock["now"] = 1003.0
                window._schedule_session_save()
                assert window._session_timer.interval() == 0
                # 書き出したら、次の待ちはまた 500ms から。
                window._flush_session()
                clock["now"] = 1003.1
                window._schedule_session_save()
                assert window._session_timer.interval() == 500
            finally:
                time.monotonic = original
        finally:
            close_window(window)


def test_session_file_is_written_outside_repo():
    with tempfile.TemporaryDirectory() as tmp:
        window = make_window(tmp)
        type_text(window, "控えの文")
        close_window(window)
        session_file = Path(tmp) / "editor_session.json"
        assert session_file.is_file()
        windows = sessionmod.load_session_file(session_file)
        assert len(windows) == 1
        tabs = windows[0]["tabs"]
        assert len(tabs) == 1
        assert tabs[0]["path"] is None
        assert tabs[0]["dirty"] is True
        assert tabs[0]["text"] == "控えの文"


def test_sanitize_session_drops_garbage():
    assert sessionmod.sanitize_session(None) == []
    assert sessionmod.sanitize_session({"windows": "変"}) == []
    assert sessionmod.sanitize_session(
        {"windows": [{"tabs": [{"path": 3}], "active": 0}]}) == []
    # 控えが要るのに本文の無い記録は使えない。
    assert sessionmod.sanitize_session(
        {"windows": [{"tabs": [{"path": None, "dirty": True}],
                      "active": 0}]}) == []
    good = sessionmod.sanitize_session({"windows": [{
        "tabs": [{"path": None, "dirty": True, "text": "文",
                  "encoding": "utf-8", "bom": False, "newline": "\n",
                  "cursor": 1, "anchor": 1}],
        "active": 0}]})
    assert len(good) == 1
    assert good[0]["tabs"][0]["text"] == "文"


def test_corrupt_session_file_is_evacuated_not_overwritten():
    """壊れた控えは、次の保存で黙って上書きせず、別名へ退避する。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        path.write_bytes("{壊れた json".encode("utf-8"))
        assert sessionmod.load_session_file(path) == []
        # 元の場所からは退避され、壊れた中身がそのまま残る。
        assert not path.exists()
        backup = Path(str(path) + ".broken")
        assert backup.read_bytes() == "{壊れた json".encode("utf-8")
        # 知らせは1回だけ受け取れる。
        notice = sessionmod.take_corrupt_notice(path)
        assert notice is not None and notice["backup"] == str(backup)
        assert sessionmod.take_corrupt_notice(path) is None
        # 退避の後に書いた控えは、新しいファイルとして残る。
        sessionmod.save_session_file(path, [{"tabs": [{
            "path": None, "dirty": True, "text": "文",
            "encoding": "utf-8", "bom": False, "newline": "\n",
            "cursor": 1, "anchor": 1}], "active": 0}])
        loaded = sessionmod.load_session_file(path)
        assert loaded[0]["tabs"][0]["text"] == "文"
        assert backup.exists()  # 退避した控えは消えていない


def test_misshapen_session_file_is_evacuated_with_serial():
    """JSON としては読めても形の崩れた控えも退避し、連番にする。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert sessionmod.load_session_file(path) == []
        assert Path(str(path) + ".broken").exists()
        sessionmod.take_corrupt_notice(path)
        path.write_text('{"version": 1}', encoding="utf-8")
        assert sessionmod.load_session_file(path) == []
        # 2回目の退避は、最初の退避を上書きしない。
        assert Path(str(path) + ".broken").read_text(
            encoding="utf-8") == "[1, 2, 3]"
        assert Path(str(path) + ".broken-2").read_text(
            encoding="utf-8") == '{"version": 1}'


def test_valid_session_file_is_not_evacuated():
    """形の合った控え(空でも)は退避されず、知らせも出ない。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        path.write_text('{"version": 1, "windows": []}',
                        encoding="utf-8")
        assert sessionmod.load_session_file(path) == []
        assert path.exists()
        assert sessionmod.take_corrupt_notice(path) is None


def test_window_reports_corrupt_session():
    """壊れた控えで開いた窓は、退避したことをステータスに残す。"""
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "editor_session.json").write_bytes(b"not json")
        window = make_window(d)
        try:
            message = window.statusBar().currentMessage()
            assert "退避しました" in message
            assert (Path(d) / "editor_session.json.broken").exists()
        finally:
            close_window(window)


def test_newer_version_session_file_is_evacuated():
    """起動する前に新しい版が書いた控え(version が大きい)は、
    知らないフィールドを落として読み込まず、.newer へ退避する。
    次の保存で version 1 として黙って上書きすることもない。"""
    import json as jsonmod

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        payload = {"version": sessionmod.SESSION_VERSION + 1,
                   "windows": [{"tabs": [{
                       "path": None, "dirty": True, "text": "新しい版の控え",
                       "encoding": "utf-8", "bom": False, "newline": "\n",
                       "cursor": 0, "anchor": 0, "future_field": 1}],
                       "active": 0}],
                   "future_top": True}
        path.write_text(jsonmod.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
        assert sessionmod.load_session_file(path) == []
        assert not path.exists()
        backup = Path(str(path) + ".newer")
        assert backup.exists()
        # 退避先には、新しい版の控えがそのまま残る。
        kept = jsonmod.loads(backup.read_text(encoding="utf-8"))
        assert kept["version"] == sessionmod.SESSION_VERSION + 1
        assert kept["windows"][0]["tabs"][0]["text"] == "新しい版の控え"
        # 知らせは kind つきで1回だけ受け取れる。
        notice = sessionmod.take_corrupt_notice(path)
        assert notice is not None and notice["kind"] == "newer"
        assert notice["backup"] == str(backup)
        assert sessionmod.take_corrupt_notice(path) is None


def test_window_reports_newer_version_session():
    """新しい版が書いた控えで開いた窓は、その旨をステータスに
    残す(壊れた控えとは文面を分ける)。"""
    import json as jsonmod

    with tempfile.TemporaryDirectory() as d:
        payload = {"version": sessionmod.SESSION_VERSION + 1,
                   "windows": [{"tabs": [{
                       "path": None, "dirty": True, "text": "新しい版の控え",
                       "encoding": "utf-8", "bom": False, "newline": "\n",
                       "cursor": 0, "anchor": 0}], "active": 0}]}
        (Path(d) / "editor_session.json").write_text(
            jsonmod.dumps(payload, ensure_ascii=False), encoding="utf-8")
        window = make_window(d)
        try:
            message = window.statusBar().currentMessage()
            assert "新しい版の EDDy が書いた控え" in message, message
            assert "退避しました" in message, message
            assert (Path(d) / "editor_session.json.newer").exists()
            # タブは戻らない(空の無題から始まる)。
            assert window.open_paths() == []
            assert window.text() == ""
        finally:
            close_window(window)


def test_evacuations_are_pruned_to_keep_limit():
    """退避ファイルは EVACUATION_KEEP 件まで残し、古いものから
    消す(読み戻す口が無いので、溜め続けない)。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        base = 1_700_000_000_000_000_000
        for i in range(sessionmod.EVACUATION_KEEP + 2):
            path.write_bytes(f"壊れた控え{i}".encode("utf-8"))
            assert sessionmod.load_session_file(path) == []
            sessionmod.take_corrupt_notice(path)
            # 今回退避されたファイルの時刻だけを決め打ちにする
            # (古い順の並びを安定させる)。
            for item in Path(d).glob("editor_session.json.broken*"):
                if item.read_bytes() == f"壊れた控え{i}".encode("utf-8"):
                    stamp = base + i * 1_000_000_000
                    os.utime(item, ns=(stamp, stamp))
        kept = sorted(Path(d).glob("editor_session.json.broken*"))
        assert len(kept) == sessionmod.EVACUATION_KEEP, kept
        contents = {item.read_bytes() for item in kept}
        # 新しい方だけが残り、いちばん古い2件は消えている。
        total = sessionmod.EVACUATION_KEEP + 2
        assert contents == {
            f"壊れた控え{i}".encode("utf-8")
            for i in range(total - sessionmod.EVACUATION_KEEP, total)}, contents


def _tab_record(text):
    """控えの要るタブ1件分の記録(session.save へ渡す形)。"""
    return {"path": None, "dirty": True, "text": text,
            "encoding": "utf-8", "bom": False, "newline": "\n",
            "cursor": 0, "anchor": 0}


def test_newer_session_file_is_evacuated_not_overwritten():
    """よそが先に書いた新しい控えは、保存で黙って上書きせず、
    別名(.newer)へ退避して残す。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        slot = sessionmod.claim(path)
        slot.update({"tabs": [_tab_record("自分の控え")], "active": 0})
        # 自分の控えを自分で更新する通常の保存は、知らせ無しで通る。
        assert sessionmod.save(path) is None
        # 別の EDDy が先に、新しい控えを書いた(中身も大きさも違う)。
        sessionmod.save_session_file(path, [{"tabs": [
            _tab_record("他の側の新しい控えです")], "active": 0}])
        notice = sessionmod.save(path)
        assert notice is not None and notice["backup"]
        backup = Path(notice["backup"])
        assert backup.name == "editor_session.json.newer"
        # 新しい側の内容は退避先に残り、表の控えは自分の内容になる。
        assert sessionmod.load_session_file(
            backup)[0]["tabs"][0]["text"] == "他の側の新しい控えです"
        assert sessionmod.load_session_file(
            path)[0]["tabs"][0]["text"] == "自分の控え"
        # 続けての保存では、退避も知らせも繰り返さない。
        assert sessionmod.save(path) is None
        assert not Path(str(path) + ".newer-2").exists()


def test_newer_session_created_after_load_is_evacuated():
    """読み込みの時点では無かった控えが、よそに先に作られていた
    場合も、黙って上書きせず退避する。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        slot = sessionmod.claim(path)  # この時点ではファイルは無い
        slot.update({"tabs": [_tab_record("自分の控え")], "active": 0})
        sessionmod.save_session_file(path, [{"tabs": [
            _tab_record("先に書いた他の側")], "active": 0}])
        notice = sessionmod.save(path)
        assert notice is not None and notice["backup"]
        assert sessionmod.load_session_file(
            Path(notice["backup"]))[0]["tabs"][0]["text"] \
            == "先に書いた他の側"
        assert sessionmod.load_session_file(
            path)[0]["tabs"][0]["text"] == "自分の控え"


def test_repeated_saves_by_two_slots_do_not_evacuate():
    """複数窓(スロット)が順に保存する通常の使い方は止まらず、
    退避も起きない。"""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "editor_session.json"
        slot1 = sessionmod.claim(path)
        slot2 = sessionmod.claim(path)
        slot1.update({"tabs": [_tab_record("窓1の控え")], "active": 0})
        assert sessionmod.save(path) is None
        slot2.update({"tabs": [_tab_record("窓2の控え")], "active": 0})
        assert sessionmod.save(path) is None
        assert sessionmod.save(path) is None
        assert not Path(str(path) + ".newer").exists()
        loaded = sessionmod.load_session_file(path)
        assert [w["tabs"][0]["text"] for w in loaded] \
            == ["窓1の控え", "窓2の控え"]


def test_window_reports_newer_session_evacuated():
    """よその新しい控えと重なった窓は、退避したことをステータスに
    残し、自分の編集は表の控えに残る。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        try:
            type_text(window, "自分の編集")
            # 窓が開いている間に、他の EDDy が新しい控えを書いた。
            path = Path(d) / "editor_session.json"
            sessionmod.save_session_file(path, [{"tabs": [
                _tab_record("他の側の新しい控えです")], "active": 0}])
            assert window._flush_session()
            message = window.statusBar().currentMessage()
            assert "退避しました" in message
            backup = Path(str(path) + ".newer")
            assert backup.exists()
            assert sessionmod.load_session_file(
                backup)[0]["tabs"][0]["text"] == "他の側の新しい控えです"
            assert sessionmod.load_session_file(
                path)[0]["tabs"][0]["text"] == "自分の編集"
        finally:
            close_window(window)


def _break_session_save():
    """sessionmod.save を失敗させる差し替え。元を返す。"""
    original = sessionmod.save

    def broken(path):
        raise OSError("書けない(テスト)")

    sessionmod.save = broken
    return original


def test_close_asks_when_session_save_fails_and_cancel_keeps_window():
    """控えが書けないとき、無言で閉じず「閉じない」で窓が残る。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        original = _break_session_save()
        try:
            type_text(window, "消えたら困る編集")
            window.ask_session_save_failed = lambda: "cancel"
            closed = []
            window.closed.connect(closed.append)
            window.close()
            app().processEvents()
            assert closed == []          # 閉じていない
            assert window.isVisible()
        finally:
            sessionmod.save = original
        close_window(window)


def test_close_discards_only_when_user_chooses():
    """控えが書けなくても、「そのまま閉じる」を選べば閉じられる。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        original = _break_session_save()
        try:
            type_text(window, "捨てる編集")
            window.ask_session_save_failed = lambda: "discard"
            closed = []
            window.closed.connect(closed.append)
            window.close()
            app().processEvents()
            assert len(closed) == 1
        finally:
            sessionmod.save = original
            window.deleteLater()
            app().processEvents()


def test_close_retry_writes_session_and_closes():
    """「もう一度試す」で控えが書ければ、内容を残して閉じられる。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        original = sessionmod.save
        attempts = {"count": 0}

        def flaky(path):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("一時的に書けない(テスト)")
            return original(path)

        sessionmod.save = flaky
        try:
            type_text(window, "残したい編集")
            window.ask_session_save_failed = lambda: "retry"
            closed = []
            window.closed.connect(closed.append)
            window.close()
            app().processEvents()
            assert len(closed) == 1
        finally:
            sessionmod.save = original
            window.deleteLater()
            app().processEvents()
        loaded = sessionmod.load_session_file(
            Path(d) / "editor_session.json")
        assert loaded[0]["tabs"][0]["text"] == "残したい編集"


def test_close_without_edits_does_not_ask_when_save_fails():
    """未保存の編集が無ければ、控えが書けなくても聞かずに閉じる。"""
    with tempfile.TemporaryDirectory() as d:
        window = make_window(d)
        original = _break_session_save()
        try:
            def forbidden():
                raise AssertionError("聞かれてはいけない")

            window.ask_session_save_failed = forbidden
            closed = []
            window.closed.connect(closed.append)
            window.close()
            app().processEvents()
            assert len(closed) == 1
        finally:
            sessionmod.save = original
            window.deleteLater()
            app().processEvents()


def _two_tab_window(d):
    """ファイル2つのタブを開いた窓を返す(2つ目は未保存の編集つき)。"""
    a = os.path.join(d, "a.txt")
    b = os.path.join(d, "b.txt")
    Path(a).write_bytes("あ\n".encode("utf-8"))
    Path(b).write_bytes("い\n".encode("utf-8"))
    window = make_window(d, a)
    assert window.open_path(b)
    type_text(window, "追")
    return window, a, b


def test_close_tab_asks_when_session_save_fails_and_cancel_keeps_tab():
    """タブを閉じるときも、控えが書けなければ無言で消さず、
    「閉じない」でタブと編集が残る(窓を閉じるときと対)。"""
    with tempfile.TemporaryDirectory() as d:
        window, _a, b = _two_tab_window(d)
        original = _break_session_save()
        try:
            window.ask_session_save_failed = lambda: "cancel"
            window.close_tab()
            assert window.tab_count() == 2
            assert window.current_document().path == b
            assert window.text() == "追い\n"
        finally:
            sessionmod.save = original
        close_window(window)


def test_close_tab_discards_only_when_user_chooses():
    """タブを閉じるときも、「そのまま閉じる」を選べば閉じられる。"""
    with tempfile.TemporaryDirectory() as d:
        window, a, _b = _two_tab_window(d)
        original = _break_session_save()
        try:
            window.ask_session_save_failed = lambda: "discard"
            window.close_tab()
            assert window.tab_count() == 1
            assert window.open_paths() == [a]
        finally:
            sessionmod.save = original
        close_window(window)


def test_close_tab_retry_writes_session_and_closes():
    """タブを閉じるときも、「もう一度試す」で控えが書ければ閉じ
    られる(閉じたタブは控えからも外れる)。"""
    with tempfile.TemporaryDirectory() as d:
        window, a, _b = _two_tab_window(d)
        original = sessionmod.save
        attempts = {"count": 0}

        def flaky(path):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("一時的に書けない(テスト)")
            return original(path)

        sessionmod.save = flaky
        try:
            window.ask_session_save_failed = lambda: "retry"
            window.close_tab()
            assert window.tab_count() == 1
            assert window.open_paths() == [a]
        finally:
            sessionmod.save = original
        close_window(window)
        window2 = make_window(d)
        try:
            assert window2.open_paths() == [a]
        finally:
            close_window(window2)


def test_close_tab_without_edits_does_not_ask_when_save_fails():
    """未保存の編集が無いタブは、控えが書けなくても聞かずに閉じる。"""
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "a.txt")
        b = os.path.join(d, "b.txt")
        Path(a).write_bytes("あ\n".encode("utf-8"))
        Path(b).write_bytes("い\n".encode("utf-8"))
        window = make_window(d, a)
        assert window.open_path(b)   # b は編集していない
        original = _break_session_save()
        try:
            def forbidden():
                raise AssertionError("聞かれてはいけない")

            window.ask_session_save_failed = forbidden
            window.close_tab()
            assert window.tab_count() == 1
            assert window.open_paths() == [a]
        finally:
            sessionmod.save = original
        close_window(window)


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
