# tests/test_editor_greplace.py
# フォルダ内の横断置換(editor/greplace.py の純粋部分と
# greplace_dialog.py の窓)を確かめる。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_greplace.py
import os
import sys
import tempfile
import threading
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

from editor import greplace as replacemod  # noqa: E402
from editor import rxfind  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        from PySide6.QtWidgets import QApplication
        APP = QApplication([])
    return APP


def _wait_dialog(dialog, done, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        app().processEvents()
        if dialog._thread is None and done():
            return
        time.sleep(0.02)
    app().processEvents()
    raise AssertionError("ダイアログの処理が終わらない")


def _tree(tmp):
    root = Path(tmp)
    (root / "a.txt").write_text("探す語 あり\nまた探す語 探す語\n3行目\n",
                                encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "b.py").write_text("# 探す語\nprint('x')\n", encoding="utf-8")
    (root / "c.dat").write_text("探す語\n", encoding="utf-8")  # 対象外の拡張子
    return root


# ---------------------------------------------------------------
# プレビュー(純粋部分)
# ---------------------------------------------------------------
def test_preview_counts_files_and_lines():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        by_name = {os.path.basename(t.path): t for t in preview.targets}
        assert set(by_name) == {"a.txt", "b.py"}, by_name
        assert by_name["a.txt"].count == 3        # 1行目1回 + 2行目2回
        assert by_name["a.txt"].first_line == 1
        assert by_name["b.py"].count == 1
        assert preview.total_count == 4
        assert preview.files_scanned == 2         # a.txt と b.py(c.dat は外)
        assert not preview.truncated
        assert preview.skipped_open == []
        # プレビューでは何も書かれていない。
        assert "探す語" in (root / "a.txt").read_text(encoding="utf-8")


def test_preview_case_sensitive_and_literal():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("Foo\nfoo\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "foo", "bar")
        assert preview.targets[0].count == 2
        preview = replacemod.preview_replace(
            str(root), "foo", "bar", case_sensitive=True)
        assert preview.targets[0].count == 1
        # 文字どおりのときは、正規表現のメタ文字は効かない。
        (root / "b.txt").write_text("a.c\naxc\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "a.c", "z")
        names = {os.path.basename(t.path): t.count for t in preview.targets}
        assert names["b.txt"] == 1


def test_preview_regex_and_faults():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("100円と200円\n", encoding="utf-8")
        preview = replacemod.preview_replace(
            str(root), r"(\d+)円", r"\1ドル", regex=True)
        assert preview.targets[0].count == 2
        # 不正な式は RegexFault(利用者向けの文)。
        try:
            replacemod.preview_replace(str(root), "([", "x", regex=True)
        except rxfind.RegexFault:
            pass
        else:
            raise AssertionError("不正な式で RegexFault にならない")
        # 参照先の無いグループ参照も、ヒットするファイルがあれば表に出る。
        try:
            replacemod.preview_replace(str(root), r"(\d+)円", r"\2ドル",
                                       regex=True)
        except rxfind.RegexFault:
            pass
        else:
            raise AssertionError("不正な置換文字列で RegexFault にならない")


def test_preview_excludes_open_files():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        opened = str(root / "a.txt")
        preview = replacemod.preview_replace(
            str(root), "探す語", "置換語", exclude_paths=[opened])
        assert [os.path.basename(t.path) for t in preview.targets] == ["b.py"]
        assert preview.skipped_open == [opened]


def test_preview_skips_binary_and_lossy():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 本文に U+0000 を含む(バイナリらしい)。
        (root / "nul.txt").write_bytes(
            "探す語\x00探す語\n".encode("utf-8"))
        # cp932 の末尾に孤立したリードバイトがあり、読み替えが起きる。
        (root / "lossy.txt").write_bytes("探す語".encode("cp932") + b"\x82")
        (root / "ok.txt").write_text("探す語\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        assert [os.path.basename(t.path) for t in preview.targets] == ["ok.txt"]
        skipped = {os.path.basename(p) for p in preview.skipped_binary}
        assert skipped == {"nul.txt", "lossy.txt"}, skipped


def test_preview_skips_unencodable():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "e.txt"
        path.write_bytes("あ abc abc\r\n".encode("cp932"))
        preview = replacemod.preview_replace(str(root), "abc", "🦄")
        assert preview.targets == []
        assert len(preview.skipped_unencodable) == 1
        skipped_path, bad = preview.skipped_unencodable[0]
        assert skipped_path == str(path) and bad == "🦄"


# ---------------------------------------------------------------
# 実行(純粋部分)
# ---------------------------------------------------------------
def test_preview_skips_files_without_extension():
    """拡張子の無いファイルは既定で対象から外れ、件数と理由が残る。
    Makefile や LICENSE のような文章ファイルも、実行形式も同じ扱い。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Makefile").write_text("探す語\n", encoding="utf-8")
        (root / "LICENSE").write_text("探す語\n", encoding="utf-8")
        (root / "run").write_bytes(b"\x7fELF" + "探す語".encode("utf-8"))
        (root / "ok.txt").write_text("探す語\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        assert [os.path.basename(t.path) for t in preview.targets] == ["ok.txt"]
        skipped = {os.path.basename(p)
                   for p in preview.skipped_no_extension}
        assert skipped == {"Makefile", "LICENSE", "run"}, skipped
        assert preview.files_scanned == 4
        # 外すのをやめれば、文章ファイルは対象に入る。
        preview = replacemod.preview_replace(
            str(root), "探す語", "置換語", exclude_no_extension=False)
        names = {os.path.basename(t.path) for t in preview.targets}
        assert names == {"Makefile", "LICENSE", "run", "ok.txt"}, names


def test_execute_replaces_preserving_format_and_backup():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "e.txt"
        original = "探す語 あり\r\n次\r\n".encode("cp932")
        path.write_bytes(original)
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        result = replacemod.execute_replace(preview, "探す語", "置換語")
        assert result.files_written == 1
        assert result.total_replaced == 1
        outcome = result.outcomes[0]
        assert outcome.status == replacemod.STATUS_DONE and outcome.count == 1
        data = path.read_bytes()
        assert "置換語 あり".encode("cp932") in data
        assert b"\r\n" in data
        assert b"\n" not in data.replace(b"\r\n", b"")   # 改行は CRLF のまま
        # .bak に置換前の内容がそのまま残る。
        assert Path(str(path) + ".bak").read_bytes() == original


def test_execute_regex_with_group_reference():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "a.txt"
        path.write_text("100円と200円\n", encoding="utf-8")
        preview = replacemod.preview_replace(
            str(root), r"(\d+)円", r"\1ドル", regex=True)
        result = replacemod.execute_replace(
            preview, r"(\d+)円", r"\1ドル", regex=True)
        assert result.total_replaced == 2
        assert path.read_text(encoding="utf-8") == "100ドルと200ドル\n"


def test_execute_skips_file_changed_after_preview():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "a.txt"
        path.write_text("探す語\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        # プレビューの後に、誰かが書き換えた(長さも変わる)。
        path.write_text("探す語 と新しい内容が足された\n", encoding="utf-8")
        result = replacemod.execute_replace(preview, "探す語", "置換語")
        assert result.files_written == 0
        assert result.outcomes[0].status == replacemod.STATUS_CHANGED
        # 書き換えられた内容はそのまま(上書きしていない)。
        assert path.read_text(encoding="utf-8") == "探す語 と新しい内容が足された\n"
        assert not Path(str(path) + ".bak").exists()


def test_execute_skips_newly_opened_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "a.txt"
        path.write_text("探す語\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        result = replacemod.execute_replace(
            preview, "探す語", "置換語", exclude_paths=[str(path)])
        assert result.files_written == 0
        assert result.outcomes[0].status == replacemod.STATUS_OPEN
        assert path.read_text(encoding="utf-8") == "探す語\n"


def test_execute_skips_unencodable_without_writing():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "e.txt"
        original = "あ abc\r\n".encode("cp932")
        path.write_bytes(original)
        # プレビューでは対象外になっているので、実行にも載らない。
        preview = replacemod.preview_replace(str(root), "abc", "🦄")
        result = replacemod.execute_replace(preview, "abc", "🦄")
        assert result.outcomes == []
        assert path.read_bytes() == original


def test_no_hits_preview_and_execute():
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        preview = replacemod.preview_replace(str(root), "無い語", "置換語")
        assert preview.targets == [] and preview.total_count == 0
        result = replacemod.execute_replace(preview, "無い語", "置換語")
        assert result.outcomes == [] and result.files_written == 0
        assert result.total_replaced == 0


# ---------------------------------------------------------------
# 窓
# ---------------------------------------------------------------
def test_dialog_preview_lists_targets():
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        try:
            assert not dialog._execute_button.isEnabled()
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            assert dialog._results.count() == 2
            status = dialog._status_label.text()
            assert "対象 2 ファイル" in status and "合計 4 件" in status
            assert dialog._execute_button.isEnabled()
            # 条件を変えると、プレビューは古くなり実行は押せなくなる。
            dialog._word_edit.setText("別の語")
            assert dialog._preview is None
            assert not dialog._execute_button.isEnabled()
        finally:
            dialog.deleteLater()


def test_dialog_preview_reports_no_extension_files():
    """プレビューの状態文に、拡張子が無いために外した件数が
    他の除外と同じ扱いで出る。"""
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "Makefile").write_text("探す語\n", encoding="utf-8")
        (root / "ok.txt").write_text("探す語\n", encoding="utf-8")
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        try:
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            assert [os.path.basename(t.path)
                    for t in dialog._preview.targets] == ["ok.txt"]
            assert "拡張子がない 1 件" in dialog._status_label.text()
        finally:
            dialog.deleteLater()


def test_dialog_execute_replaces_and_refreshes():
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        dialog._confirm_execute = lambda preview: True
        try:
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            dialog.start_execute()
            # 実行のあと、一覧は自動で確認し直される(対象は無くなる)。
            _wait_dialog(
                dialog,
                lambda: (dialog._preview is not None
                         and not dialog._preview.targets))
            assert "置換語" in (root / "a.txt").read_text(encoding="utf-8")
            assert "置換しました" in dialog._status_label.text()
            assert "見つかりませんでした" in dialog._status_label.text()
            # 置換したファイルには .bak が残る。
            assert Path(str(root / "a.txt") + ".bak").exists()
        finally:
            dialog.deleteLater()


def test_dialog_cannot_close_while_executing():
    """置換の実行中は、× も Esc も閉じない。終われば閉じられる。"""
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        dialog._confirm_execute = lambda preview: True
        started = threading.Event()
        release = threading.Event()
        original = replacemod.execute_replace

        def slow_execute(*args, **kwargs):
            started.set()
            release.wait(timeout=30)
            return original(*args, **kwargs)

        replacemod.execute_replace = slow_execute
        try:
            dialog.show()
            app().processEvents()
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            dialog.start_execute()
            assert started.wait(timeout=10)
            app().processEvents()
            assert dialog._busy == "execute"
            # × は閉じない。
            dialog.close()
            app().processEvents()
            assert dialog.isVisible()
            assert "終わるまで閉じられません" in dialog._status_label.text()
            # Esc も閉じない。
            dialog.reject()
            app().processEvents()
            assert dialog.isVisible()
            assert dialog._busy == "execute"
            release.set()
            _wait_dialog(dialog, lambda: (dialog._preview is not None
                                          and not dialog._preview.targets))
            # 終われば閉じられる。
            dialog.close()
            app().processEvents()
            assert not dialog.isVisible()
        finally:
            replacemod.execute_replace = original
            release.set()
            dialog.deleteLater()


def test_dialog_execute_cancelled_by_confirmation():
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        before = (root / "a.txt").read_bytes()
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        dialog._confirm_execute = lambda preview: False
        try:
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            dialog.start_execute()
            app().processEvents()
            assert dialog._thread is None       # 実行は始まっていない
            assert (root / "a.txt").read_bytes() == before
        finally:
            dialog.deleteLater()


def test_dialog_excludes_window_open_files():
    app()
    import editor
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = _tree(tmp)
        window = editor.create_window(str(root / "a.txt"))
        window.show()
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語",
            open_paths=window.open_paths)
        try:
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            names = [os.path.basename(t.path)
                     for t in dialog._preview.targets]
            assert names == ["b.py"], names
            assert len(dialog._preview.skipped_open) == 1
            assert "開いている 1 件" in dialog._status_label.text()
        finally:
            dialog.deleteLater()
            window.close()
            window.deleteLater()
            app().processEvents()


def test_window_menu_has_grep_replace():
    app()
    import editor
    window = editor.create_window()
    window.show()
    try:
        labels = []
        from PySide6.QtWidgets import QMenu
        for menu in window.findChildren(QMenu):
            labels.extend(a.text() for a in menu.actions())
        assert any("検索して置換" in label for label in labels), labels
    finally:
        window.close()
        window.deleteLater()
        app().processEvents()


def test_execute_preserves_mixed_newlines_on_untouched_lines():
    """混ざったファイルでも、置換していない行の改行を変えない。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "mixed.txt"
        original = "aaa\r\nbbb\nccc\r\nddd\n".encode("utf-8")
        path.write_bytes(original)
        preview = replacemod.preview_replace(str(root), "bbb", "BBB")
        result = replacemod.execute_replace(preview, "bbb", "BBB")
        assert result.files_written == 1
        # 置換した行は LF のまま、他の行も元の種別のまま。
        assert path.read_bytes() == \
            "aaa\r\nBBB\nccc\r\nddd\n".encode("utf-8")
        # CRLF の行を置換しても、LF の行は LF のまま。
        preview = replacemod.preview_replace(str(root), "ccc", "CCC")
        result = replacemod.execute_replace(preview, "ccc", "CCC")
        assert result.files_written == 1
        assert path.read_bytes() == \
            "aaa\r\nBBB\nCCC\r\nddd\n".encode("utf-8")


def test_execute_replacement_spanning_lines_keeps_other_newlines():
    """行をまたぐ置換でも、関係ない行の改行は変えない。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "span.txt"
        path.write_bytes("one\ntwo\r\nthree\nfour\r\n".encode("utf-8"))
        # 「two\r\nthree」(正規化後は "two\nthree")を1行に潰す。
        preview = replacemod.preview_replace(
            str(root), "two\nthree", "merged", regex=True)
        result = replacemod.execute_replace(
            preview, "two\nthree", "merged", regex=True)
        assert result.files_written == 1
        # 潰れた行の末尾に残るのは、取り込まれなかった側(three の
        # 行)の改行(LF)。前後の置換していない行は元のまま。
        assert path.read_bytes() == \
            "one\nmerged\nfour\r\n".encode("utf-8")


def test_second_execute_keeps_first_backup():
    """2回目の置換でも、最初の .bak(置換前の内容)が残る。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / "twice.txt"
        original = "語1 と 語2\n".encode("utf-8")
        path.write_bytes(original)
        preview = replacemod.preview_replace(str(root), "語1", "置換1")
        result = replacemod.execute_replace(preview, "語1", "置換1")
        assert result.files_written == 1
        after_first = path.read_bytes()
        assert Path(str(path) + ".bak").read_bytes() == original
        preview = replacemod.preview_replace(str(root), "語2", "置換2")
        result = replacemod.execute_replace(preview, "語2", "置換2")
        assert result.files_written == 1
        # 最初の控えは潰れず、2回目の控えは連番へ。
        assert Path(str(path) + ".bak").read_bytes() == original
        assert Path(str(path) + ".bak2").read_bytes() == after_first
        assert path.read_bytes() == "置換1 と 置換2\n".encode("utf-8")


def test_preview_skips_link_files():
    # シンボリックリンクとハードリンクは対象から外れ、件数と理由
    # が残る。実ファイルは対象のまま。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "plain.txt").write_text("探す語 あり\n", encoding="utf-8")
        (root / "orig.txt").write_text("探す語 あり\n", encoding="utf-8")
        os.symlink(root / "plain.txt", root / "link.txt")
        os.link(root / "orig.txt", root / "hard.txt")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        assert [os.path.basename(t.path) for t in preview.targets] == [
            "plain.txt"]
        assert {os.path.basename(p) for p in preview.skipped_link} == {
            "link.txt", "orig.txt", "hard.txt"}
        assert "探す語" in (root / "plain.txt").read_text(encoding="utf-8")


def test_execute_skips_link_created_after_preview():
    # プレビューの後でリンクになったファイルも、実行の段で止める
    # (プレビューだけの判定では間の変化を拾えないため)。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "a.txt"
        target.write_text("探す語 あり\n", encoding="utf-8")
        other = root / "other.txt"
        other.write_text("探す語 あり\n", encoding="utf-8")
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        assert len(preview.targets) == 2
        target.unlink()
        os.symlink(other, target)
        result = replacemod.execute_replace(preview, "探す語", "置換語")
        by_path = {os.path.basename(o.path): o for o in result.outcomes}
        assert by_path["a.txt"].status == replacemod.STATUS_LINK
        assert os.path.islink(target)
        assert by_path["other.txt"].status == replacemod.STATUS_DONE
        assert "置換語" in other.read_text(encoding="utf-8")


def test_execute_skips_hardlink_without_breaking_it():
    # ハードリンクは置換で切れて別の中身になるので、実行の段でも
    # 止めて、リンクを壊さない。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real = root / "real.txt"
        real.write_text("探す語 あり\n", encoding="utf-8")
        hard = root / "hard.txt"
        os.link(real, hard)
        ino_before = os.stat(real).st_ino
        preview = replacemod.preview_replace(str(root), "探す語", "置換語")
        assert preview.targets == []
        assert len(preview.skipped_link) == 2
        # 手で作ったプレビューで実行しても、リンクは壊れない。
        target = replacemod.ReplaceTarget(
            path=str(real), count=1, first_line=1,
            mtime=real.stat().st_mtime, size=real.stat().st_size)
        fake = replacemod.ReplacePreview(targets=[target])
        result = replacemod.execute_replace(fake, "探す語", "置換語")
        assert result.outcomes[0].status == replacemod.STATUS_LINK
        assert os.stat(real).st_ino == ino_before
        assert os.stat(hard).st_ino == ino_before
        assert "探す語" in real.read_text(encoding="utf-8")


def test_dialog_preview_reports_link_files():
    app()
    from editor.greplace_dialog import GrepReplaceDialog
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.txt").write_text("探す語 あり\n", encoding="utf-8")
        os.symlink(root / "a.txt", root / "link.txt")
        dialog = GrepReplaceDialog(
            None, folder=str(root), word="探す語", repl="置換語")
        try:
            dialog.start_preview()
            _wait_dialog(dialog, lambda: dialog._preview is not None)
            status = dialog._status_label.text()
            assert "対象 1 ファイル" in status, status
            assert "リンク 1 件" in status, status
        finally:
            dialog.deleteLater()


def test_execute_summary_reports_link_files():
    from editor.greplace_dialog import GrepReplaceDialog
    result = replacemod.ReplaceRunResult(outcomes=[
        replacemod.ReplaceOutcome("x.txt", replacemod.STATUS_DONE, count=1),
        replacemod.ReplaceOutcome(
            "y.txt", replacemod.STATUS_LINK,
            detail="リンクのファイルのため飛ばしました"),
    ])
    assert "リンク 1 件" in GrepReplaceDialog._execute_summary(result)


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
