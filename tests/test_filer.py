# tests/test_filer.py
# 簡易ファイラ(filer.py / filer_core.py / filer_shell.py / filer_launch.py)を確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_filer.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_diagnostics.py と同じ形。pytest があればそのまま拾える)。
# 窓は offscreen で作る(実画面に出さない)。触るのは tempfile で作った一時フォルダだけで、
# ユーザーのファイルには触れない。ただし「ごみ箱へ削除」は本物のごみ箱へ1件送る
# (SHFileOperationW を実際に通すため。中身は空に近い一時ファイル)。
#
# 環境変数 FILER_SHOT に PNG のパスを入れると、窓を grab() した絵をそこへ書く。
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import filer_core as core  # noqa: E402


# ---------------------------------------------------------------
# キー割り当て
# ---------------------------------------------------------------
def test_scav_code():
    assert core.scav(67) == "0067"
    assert core.scav(67, alt=True) == "4067"
    assert core.scav(186, shift=True) == "1186"
    assert core.scav(33, ctrl=True) == "2033"
    assert core.scav(115, alt=True) == "4115"


def test_default_keymap_matches_afxw_key():
    km = core.DEFAULT_KEYMAP
    # ユーザーの AFXW.key に由来する割り当て
    assert km["0067"] == "copy_menu" and km["4067"] == "copy_direct"
    assert km["0077"] == "move_menu" and km["4077"] == "move_direct"
    assert km["0078"] == "desktop"
    assert km["4079"] == "swap_paths"
    assert km["1186"] == "mask_input" and km["0186"] == "mask_clear"
    assert km["0188"] == "back" and km["0190"] == "forward"
    assert km["4115"] == "quit"
    assert km["0074"] == "bookmark_jump" and km["1074"] == "path_jump"
    # ←/→ は窓のアクティブ化だけ。親へ行くのは BS だけ
    assert km["0037"] == "focus_left" and km["0039"] == "focus_right"
    assert km["0008"] == "parent"
    assert [code for code, cmd in km.items() if cmd == "parent"] == ["0008"]


def test_keymap_esc_layout_font():
    km = core.DEFAULT_KEYMAP
    # Esc は窓を閉じない(あふｗの Esc は「各処理の中止」)。閉じるのは Q と Alt+F4 だけ
    assert "0027" not in km
    assert sorted(code for code, cmd in km.items() if cmd == "quit") == ["0081", "4115"]
    # Alt+←→ と、AFXW.key でそれに置き換えている Ctrl+←→
    assert km["4037"] == km["2037"] == "split_left"
    assert km["4039"] == km["2039"] == "split_right"
    assert km["4038"] == "log_grow" and km["4040"] == "log_shrink"
    # Shift/Ctrl+↑ で縮小、Shift/Ctrl+↓ で拡大(AFXW.key の INS/DEL 置き換え)
    assert km["1038"] == km["2038"] == "font_smaller"
    assert km["1040"] == km["2040"] == "font_larger"


def test_layout_helpers():
    assert core.step_split(0.5, -1) == 0.47 and core.step_split(0.5, 1) == 0.53
    assert core.step_split(0.16, -1) == core.SPLIT_MIN
    assert core.step_split(0.84, 1) == 1 - core.SPLIT_MIN
    assert core.clamp_split("x") == 0.5 and core.clamp_split(5) == 1 - core.SPLIT_MIN
    assert core.clamp_log_lines(0, 10) == 1 and core.clamp_log_lines(30, 10) == 10
    assert core.clamp_log_lines("bad", 10) == core.DEFAULT_LOG_LINES
    assert core.clamp_log_lines(5, 0) == 1
    assert core.clamp_font_size(2) == core.FONT_SIZE_MIN
    assert core.clamp_font_size(99) == core.FONT_SIZE_MAX
    assert core.clamp_font_size(None) == core.DEFAULT_FONT_SIZE


def test_build_keymap_overrides():
    km = core.build_keymap({"0067": "", "0099": "quit", "bad": "quit", "0100": "nope",
                            "9000": "quit", "0080": None})
    assert "0067" not in km                 # 空文字で外せる
    assert km["0099"] == "quit"              # 足せる
    assert "0100" not in km                  # 知らないコマンドは捨てる
    assert "9000" not in km                  # 形の違うキーは捨てる
    assert km["4067"] == "copy_direct"       # 触っていないものは既定のまま
    assert core.build_keymap("garbage") == core.DEFAULT_KEYMAP


def test_arrow_target():
    assert core.arrow_target("left", "right") == "right"
    assert core.arrow_target("right", "left") == "left"
    assert core.arrow_target("left", "left") is None
    assert core.arrow_target("right", "right") is None


# ---------------------------------------------------------------
# パス
# ---------------------------------------------------------------
def test_normalize_and_parent():
    assert core.normalize_dir("C:") == "C:\\"
    assert core.normalize_dir("C:\\") == "C:\\"
    assert core.normalize_dir("C:\\foo\\bar\\") == "C:\\foo\\bar"
    assert core.normalize_dir("c:/foo//bar") == "c:\\foo\\bar"
    assert core.parent_of("C:\\foo\\bar") == "C:\\foo"
    assert core.parent_of("C:\\foo") == "C:\\"
    assert core.parent_of("C:\\") is None
    assert core.parent_of("\\\\host\\share\\dir") == "\\\\host\\share\\"
    assert core.parent_of("\\\\host\\share") is None
    assert core.is_root("D:\\") and not core.is_root("D:\\x")
    assert core.same_dir("C:\\Foo\\", "c:\\foo")
    assert not core.same_dir("C:\\foo", None)


def test_existing_ancestor():
    existing = {"C:\\", "C:\\a", "C:\\a\\b"}
    isdir = lambda p: p in existing  # noqa: E731
    assert core.existing_ancestor("C:\\a\\b\\c\\d", isdir) == "C:\\a\\b"
    assert core.existing_ancestor("C:\\a", isdir) == "C:\\a"
    assert core.existing_ancestor("Z:\\x\\y", isdir) is None
    assert core.existing_ancestor("", isdir) is None


def test_expand_typed_path():
    os.environ["FILER_TEST_DIR"] = "C:\\tmp"
    assert core.expand_typed_path('"%FILER_TEST_DIR%\\x"') == "C:\\tmp\\x"
    assert core.expand_typed_path("  ") is None
    assert core.expand_typed_path("") is None


# ---------------------------------------------------------------
# 一覧・並び・マスク
# ---------------------------------------------------------------
def E(name, is_dir=False, size=0, attrs=0):
    return core.Entry(name, is_dir, size, 0.0, attrs)


def test_sort_dirs_first_case_insensitive():
    entries = [E("b.txt"), E("Zdir", True), E("A.txt"), E("adir", True), E("c")]
    names = [e.name for e in core.sort_entries(entries)]
    assert names == ["adir", "Zdir", "A.txt", "b.txt", "c"], names


def F(name, size=0, mtime=0.0, attrs=0, is_dir=False):
    return core.Entry(name, is_dir, size, mtime, attrs)


def test_sort_kinds():
    entries = [F("file10.txt", 5, 30), F("File2.md", 50, 10), F("file1.TXT", 1, 20),
               F("dirB", is_dir=True), F("dira", is_dir=True), F("noext", 9, 40, attrs=2)]
    names = lambda spec: [e.name for e in core.sort_entries(entries, spec)]  # noqa: E731
    assert names({"kind": "name"}) == ["dira", "dirB", "file1.TXT", "file10.txt", "File2.md", "noext"]
    assert names({"kind": "natural"}) == ["dira", "dirB", "file1.TXT", "File2.md", "file10.txt", "noext"]
    assert names({"kind": "ext"}) == ["dira", "dirB", "noext", "File2.md", "file1.TXT", "file10.txt"]
    assert names({"kind": "size"}) == ["dira", "dirB", "file1.TXT", "file10.txt", "noext", "File2.md"]
    assert names({"kind": "time"}) == ["dira", "dirB", "File2.md", "file1.TXT", "file10.txt", "noext"]
    assert names({"kind": "attr"})[-1] == "noext"
    # 降順でもフォルダは上のまま(塊の中で逆になる)
    assert names({"kind": "size", "descending": True}) == [
        "dirB", "dira", "File2.md", "noext", "file10.txt", "file1.TXT"]
    # フォルダを集めない
    assert names({"kind": "name", "dirs_first": False}) == [
        "dira", "dirB", "file1.TXT", "file10.txt", "File2.md", "noext"]
    # 大文字小文字を区別(大文字が先)
    assert names({"kind": "name", "ignore_case": False})[:2] == ["dirB", "dira"]
    # ソートなしは列挙順(降順なら逆)
    assert names({"kind": "none", "dirs_first": False}) == [e.name for e in entries]
    assert names({"kind": "none", "dirs_first": False, "descending": True}) == [
        e.name for e in reversed(entries)]
    assert core.natural_key("a2") < core.natural_key("a10")
    assert core.normalize_sort({"kind": "bogus", "descending": "yes"}) == core.DEFAULT_SORT
    assert core.DEFAULT_KEYMAP["0083"] == "sort_dialog" and core.DEFAULT_KEYMAP["1083"] == "sort_reverse"
    assert core.DEFAULT_KEYMAP["0086"] == "edit"


def test_compare_marks():
    mine = [core.PARENT_ENTRY, F("same.txt", 10, 100), F("Big.txt", 20, 100),
            F("old.txt", 10, 50), F("near.txt", 10, 101.5), F("only_here.txt", 1, 1),
            F("d", is_dir=True), F("mixed", 3, 3), F("photo.jpg", 1, 1)]
    theirs = [F("SAME.TXT", 10, 100), F("big.txt", 10, 100), F("old.txt", 10, 100),
              F("near.txt", 10, 100), F("d", is_dir=True), F("mixed", is_dir=True),
              F("photo.png", 99, 999)]
    cm = core.compare_marks
    # 無条件: 同名ならマーク(大文字小文字無視・ファイルとフォルダの組も)。フォルダ同士は除く
    assert cm(mine, theirs) == {"same.txt", "Big.txt", "old.txt", "near.txt", "mixed"}
    assert cm(mine, theirs, include_dirs=True) == {
        "same.txt", "Big.txt", "old.txt", "near.txt", "mixed", "d"}
    # サイズ
    assert cm(mine, theirs, "larger") == {"Big.txt", "mixed"}
    assert cm(mine, theirs, "same") == {"same.txt", "old.txt", "near.txt", "mixed"}
    assert cm(mine, theirs, "smaller") == {"mixed"}
    # 時刻(許容誤差2秒: near は同じ扱い)
    assert cm(mine, theirs, "any", "same") == {"same.txt", "Big.txt", "near.txt", "mixed"}
    assert cm(mine, theirs, "any", "older") == {"old.txt", "mixed"}
    assert cm(mine, theirs, "any", "different") == {"old.txt", "mixed"}
    assert cm(mine, theirs, "any", "different", tolerance=0) == {"old.txt", "near.txt", "mixed"}
    assert cm(mine, theirs, "any", "newer", tolerance=1) == {"near.txt", "mixed"}
    # 全同(A)・全異(N)
    assert cm(mine, theirs, "same", "same") == {"same.txt", "near.txt", "mixed"}
    assert cm(mine, theirs, "different", "different") == {"mixed"}
    # 拡張子を除く(条件は無効)
    assert "photo.jpg" in cm(mine, theirs, "same", "same", strip_ext=True)
    assert core.time_matches("same", 0, 2.0, 2.0) and not core.time_matches("same", 0, 2.1, 2.0)
    assert core.COMPARE_SHORTCUTS["S"] == ("same", "any", False)
    assert core.DEFAULT_KEYMAP["0087"] == "compare_mark"


def test_dialogs():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    colors = dict(filer.DEFAULT_COLORS)
    from PySide6.QtGui import QFont
    font = QFont()

    def compare(*keys):
        d = filer.CompareDialog(None, "同名ファイル比較マーク(左)", False, colors, font)
        for k, mods in keys:
            QTest.keyClick(d, k, mods)
        return d.result_value

    N = Qt.NoModifier
    assert compare((Qt.Key_A, N)) == {"size": "same", "time": "same", "include_dirs": False,
                                       "strip_ext": False, "unmark": False}
    assert compare((Qt.Key_T, Qt.ShiftModifier))["unmark"] is True
    assert compare((Qt.Key_E, N))["strip_ext"] is True
    # ←→で選択肢、↓で行、Space でフォルダ、Enter で決定
    r = compare((Qt.Key_Left, N), (Qt.Key_Left, N), (Qt.Key_Down, N), (Qt.Key_Right, N),
                (Qt.Key_Space, N), (Qt.Key_Return, N))
    assert r == {"size": "larger", "time": "same", "include_dirs": True,
                 "strip_ext": False, "unmark": False}, r
    r = compare((Qt.Key_Up, N), (Qt.Key_Return, Qt.ShiftModifier))   # 最下行(除拡張子)
    assert r["strip_ext"] and r["unmark"]
    assert compare((Qt.Key_Escape, N)) is None

    def sort(spec, *keys):
        d = filer.SortDialog(None, "ソート(右)", spec, colors, font)
        for k in keys:
            QTest.keyClick(d, k)
        return d.result_value

    assert sort(None, Qt.Key_X)["kind"] == "natural"
    r = sort(None, Qt.Key_Right, Qt.Key_G, Qt.Key_C, Qt.Key_S)
    assert r == {"kind": "size", "descending": True, "dirs_first": False, "ignore_case": False}, r
    assert sort({"kind": "time"}, Qt.Key_Down, Qt.Key_Tab, Qt.Key_Return) == {
        "kind": "attr", "descending": True, "dirs_first": True, "ignore_case": True}
    assert sort(None, Qt.Key_Escape) is None


def test_mask():
    assert core.parse_mask("*.*") == []
    assert core.parse_mask("") == []
    assert core.parse_mask("*.TXT;*.md") == ["*.txt", "*.md"]
    assert core.parse_mask("foo") == ["*foo*"]
    assert core.parse_mask("a.txt *") == []          # どこかに * があれば全部
    assert core.is_default_mask("*.*") and not core.is_default_mask("*.py")
    entries = [E("dir", True), E("a.txt"), E("b.PY"), E("foobar.md")]
    shown = [e.name for e in core.filter_entries(entries, core.parse_mask("*.py"))]
    assert shown == ["dir", "b.PY"], shown           # フォルダは常に残る
    shown = [e.name for e in core.filter_entries(entries, core.parse_mask("oob"))]
    assert shown == ["dir", "foobar.md"], shown


def test_list_entries_and_read_folder():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "sub").mkdir()
        Path(tmp, "b.txt").write_bytes(b"12345")
        Path(tmp, "A.md").write_bytes(b"")
        entries = core.sort_entries(core.list_entries(tmp))
        assert [e.name for e in entries] == ["sub", "A.md", "b.txt"]
        assert entries[0].is_dir and entries[2].size == 5
        # ファイルのパスを渡すと置き場所を開いてファイル名に合わせる
        path, listed, focus, archive = core.read_folder(os.path.join(tmp, "b.txt"))
        assert core.same_dir(path, tmp) and focus == "b.txt" and archive is None
        # 無くなったフォルダは walk_up で残っている親まで遡る
        path, _listed, _focus, _a = core.read_folder(os.path.join(tmp, "gone", "deeper"),
                                                     walk_up=True)
        assert core.same_dir(path, tmp)
        try:
            core.read_folder(os.path.join(tmp, "gone"))
            raise AssertionError("無いフォルダで例外にならない")
        except OSError:
            pass


# ---------------------------------------------------------------
# マーク
# ---------------------------------------------------------------
def test_targets_and_invert():
    entries = [core.PARENT_ENTRY, E("d", True), E("x.txt", size=10), E("y.txt", size=5)]
    assert core.pick_targets(entries, set(), 0) == []                 # ".." は対象外
    assert [e.name for e in core.pick_targets(entries, set(), 2)] == ["x.txt"]
    assert [e.name for e in core.pick_targets(entries, {"y.txt", "d"}, 2)] == ["d", "y.txt"]
    assert core.invert_marks(entries, {"x.txt"}, include_dirs=False) == {"y.txt"}
    assert core.invert_marks(entries, {"x.txt"}, include_dirs=True) == {"d", "y.txt"}
    assert core.invert_marks(entries, {"d"}, include_dirs=False) == {"d", "x.txt", "y.txt"}
    assert core.marks_summary(entries, {"d", "x.txt", "y.txt"}) == (3, 15)


def test_formatting():
    assert core.split_name(E("a.tar.gz")) == ("a.tar", "gz")
    assert core.split_name(E(".gitignore")) == (".gitignore", "")
    assert core.split_name(E("dir.v2", True)) == ("dir.v2", "")
    assert core.format_size(E("d", True)) == "<DIR>"
    assert core.format_size(E("f", size=1234567)) == "1,234,567"
    stamp = time.mktime((2026, 10, 23, 9, 5, 0, 0, 0, -1))
    assert core.format_mtime(stamp) == "26-10-23 09:05"
    assert core.format_bytes(2048).startswith("2,048 (2.0KB")


def test_isearch_index():
    entries = [core.PARENT_ENTRY, E("alpha"), E("Beta"), E("gamma_b")]
    assert core.isearch_index(entries, "b") == 2           # 前方一致が先
    assert core.isearch_index(entries, "MA") == 3          # 無ければ含む
    assert core.isearch_index(entries, "zzz") is None
    assert core.isearch_index(entries, "") is None
    assert core.isearch_index(entries, ".") is None        # ".." は対象外


# ---------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------
def test_folder_history():
    h = core.FolderHistory()
    for p in ("C:\\a", "C:\\b", "C:\\c"):
        h.visit(p)
    h.visit("c:\\C\\")                 # 同じ場所は積まない
    assert h.back() == "C:\\b" and h.back() == "C:\\a" and h.back() is None
    assert h.forward() == "C:\\b"
    h.visit("C:\\x")                   # 戻った先で別の場所へ行くと進む側は消える
    assert h.forward() is None
    assert h.back() == "C:\\b"
    # 戻る移動に失敗したら1手前へ巻き戻す
    previous = "C:\\b"
    h2 = core.FolderHistory()
    for p in ("C:\\a", "C:\\b"):
        h2.visit(p)
    assert h2.back() == "C:\\a"
    h2.undo_step("back", previous)
    assert h2.current == "C:\\b" and h2.forward_stack == [] and h2.back_stack == ["C:\\a"]


def test_push_recent():
    recent = []
    for p in ("C:\\a", "C:\\b", "c:\\A\\", "C:\\c"):
        recent = core.push_recent(recent, p, limit=3)
    assert recent == ["C:\\c", "c:\\A", "C:\\b"], recent
    many = core.push_recent([f"C:\\{i}" for i in range(60)], "D:\\", limit=50)
    assert len(many) == 50 and many[0] == "D:\\"


# ---------------------------------------------------------------
# 名前
# ---------------------------------------------------------------
def test_names():
    assert core.validate_name("ok name") is None
    assert core.validate_name("") and core.validate_name("a/b") and core.validate_name("x.")
    assert core.unique_name(["新しいフォルダー"], "新しいフォルダー") == "新しいフォルダー (2)"
    assert core.unique_name(["a.txt", "A (2).txt"], "a.txt") == "a (3).txt"
    assert core.unique_name([], "a") == "a"
    assert core.rename_selection_length("report.final.docx", False) == len("report.final")
    assert core.rename_selection_length("folder.v2", True) == len("folder.v2")
    assert core.name_conflicts(["Foo.txt"], "foo.TXT")
    assert not core.name_conflicts(["Foo.txt"], "foo.txt", old_name="Foo.txt")  # 大小だけの改名


def test_clipboard_helpers():
    assert core.encode_drop_effect(True) == b"\x02\x00\x00\x00"
    assert core.decode_drop_effect(core.encode_drop_effect(True)) is True
    assert core.decode_drop_effect(core.encode_drop_effect(False)) is False
    assert core.decode_drop_effect(b"") is False
    assert core.double_null_join(["a", "b"]) == "a\0b\0\0"
    assert core.clip_text(["C:\\x\\a.txt", "C:\\x\\d"], names_only=True) == "a.txt\r\nd"


# ---------------------------------------------------------------
# Win32(SHFileOperationW / 既知フォルダ)
# ---------------------------------------------------------------
QUIET = None


def _quiet_flags():
    import filer_shell as sh
    return sh.FOF_NOCONFIRMATION | sh.FOF_SILENT | sh.FOF_NOERRORUI


def test_shell_copy_move_delete():
    import filer_shell as sh
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "src")
        dst = Path(tmp, "dst")
        src.mkdir()
        dst.mkdir()
        Path(src, "a.txt").write_text("a", encoding="utf-8")
        Path(src, "日本語 b.txt").write_text("b", encoding="utf-8")
        Path(src, "d").mkdir()
        Path(src, "d", "inner.txt").write_text("i", encoding="utf-8")
        items = [str(src / "a.txt"), str(src / "日本語 b.txt"), str(src / "d")]
        assert sh.copy_files(items, str(dst), 0, _quiet_flags()) == (0, False)
        assert (dst / "日本語 b.txt").exists() and (dst / "d" / "inner.txt").exists()
        assert (src / "a.txt").exists()
        assert sh.move_files([str(src / "a.txt")], str(dst / "d"), 0, _quiet_flags()) == (0, False)
        assert (dst / "d" / "a.txt").exists() and not (src / "a.txt").exists()
        # ごみ箱へ。本物のごみ箱に1件入り、回すたびに溜まっていくので、
        # 環境変数 FILER_TEST_TRASH=1 のときだけ確かめる。
        if os.environ.get("FILER_TEST_TRASH") == "1":
            trash = src / "filer_test_trash.txt"
            trash.write_text("x", encoding="utf-8")
            assert sh.delete_files([str(trash)], 0, permanent=False, extra_flags=_quiet_flags())[0] == 0
            assert not trash.exists()
        # 完全削除
        assert sh.delete_files([str(dst / "d")], 0, permanent=True, extra_flags=_quiet_flags())[0] == 0
        assert not (dst / "d").exists()


def test_known_folder_desktop():
    import filer_shell as sh
    path = sh.desktop_path()
    assert path and os.path.isabs(path), path


def test_drive_list():
    import filer_shell as sh
    drives = sh.list_drives(label_timeout=2.0)
    roots = [root for root, _label in drives]
    assert "C:\\" in roots, drives


# ---------------------------------------------------------------
# 窓(offscreen)
# ---------------------------------------------------------------
_app = None


_TEST_TEMP = tempfile.mkdtemp(prefix="filer_test_tmp_")


def _qapp():
    global _app
    import filer_archive
    from PySide6.QtWidgets import QApplication
    # 窓を閉じると一時フォルダを片付ける。本物のファイラが同じ場所を使っているかも
    # しれないので、テストでは別の場所を使わせる。
    filer_archive.TEMP_ROOT = _TEST_TEMP
    _app = QApplication.instance() or QApplication([])
    return _app


def _wait(window, timeout=5.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QTest.qWait(20)
        if not (window.left.loading or window.right.loading):
            QTest.qWait(10)
            return
    raise AssertionError("一覧の読み込みが終わらない")


def _names(pane):
    return [e.name for e in pane.entries]


def _cursor_name(pane):
    entry = pane.cursor_entry()
    return entry.name if entry else None


def _make_tree(tmp):
    left = Path(tmp, "L")
    right = Path(tmp, "R")
    for p in (left, right, left / "sub1", left / "sub2", left / "sub1" / "deep"):
        p.mkdir()
    for name, data in (("alpha.txt", b"a" * 10), ("beta.md", b"b" * 20), ("gamma.txt", b"c"),
                       ("photo.JPG", b"p" * 100)):
        Path(left, name).write_bytes(data)
    Path(right, "r.txt").write_bytes(b"r")
    return left, right


def test_window_keys_and_operations():
    import filer
    import filer_shell as sh
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text(json.dumps({"hotkeys": {"x": "y"}, "filer": {"editor": "x"}}),
                                 encoding="utf-8")
        app_settings = {"filer": {"left_path": str(left), "right_path": str(right / "gone" / "x"),
                                  "editor": "x"}}
        w = filer.FilerWindow(app_settings, str(settings_path))
        w.extra_op_flags = _quiet_flags()
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        assert core.same_dir(L.path, left), L.path
        assert core.same_dir(R.path, right), R.path            # 無くなったパスは親へ遡る
        assert _names(L) == ["..", "sub1", "sub2", "alpha.txt", "beta.md", "gamma.txt", "photo.JPG"]
        assert w.active_side == "left"

        key = lambda k, mods=Qt.NoModifier, pane=None: QTest.keyClick(  # noqa: E731
            (pane or w.active).view, k, mods)

        # 移動
        key(Qt.Key_Down)
        key(Qt.Key_Down)
        assert _cursor_name(L) == "sub2"
        key(Qt.Key_PageDown, Qt.ControlModifier)
        assert _cursor_name(L) == "photo.JPG"
        key(Qt.Key_PageUp, Qt.ControlModifier)
        assert _cursor_name(L) == ".."

        # ←/→ は窓のアクティブ化だけ(既にその窓なら何もしない。親へは行かない)
        key(Qt.Key_Left)
        assert w.active_side == "left" and core.same_dir(L.path, left)
        key(Qt.Key_Right)
        assert w.active_side == "right"
        key(Qt.Key_Right)
        assert w.active_side == "right" and core.same_dir(R.path, right)
        key(Qt.Key_Left)
        assert w.active_side == "left"
        key(Qt.Key_Tab)
        assert w.active_side == "right"
        key(Qt.Key_Tab)
        assert w.active_side == "left"

        # Enter でフォルダへ、BS で親へ(元いたフォルダにカーソル)
        L.set_cursor(1)  # sub1
        key(Qt.Key_Return)
        _wait(w)
        assert core.same_dir(L.path, left / "sub1"), L.path
        key(Qt.Key_Backspace)
        _wait(w)
        assert core.same_dir(L.path, left) and _cursor_name(L) == "sub1"

        # マーク: Space で付けて下へ、Shift+Space で付けて上へ
        L.set_cursor(3)  # alpha.txt
        key(Qt.Key_Space)
        assert L.marks == {"alpha.txt"} and _cursor_name(L) == "beta.md"
        key(Qt.Key_Space)
        assert L.marks == {"alpha.txt", "beta.md"}
        key(Qt.Key_Space, Qt.ShiftModifier)     # gamma をマークして上へ
        assert L.marks == {"alpha.txt", "beta.md", "gamma.txt"} and _cursor_name(L) == "beta.md"
        key(Qt.Key_A)                           # ファイルだけ反転
        assert L.marks == {"photo.JPG"}, L.marks
        key(Qt.Key_Home, Qt.ShiftModifier)      # フォルダ込みで反転
        assert L.marks == {"sub1", "sub2", "alpha.txt", "beta.md", "gamma.txt"}, L.marks
        assert "マーク 5" in L.footer.text(), L.footer.text()
        key(Qt.Key_End, Qt.ControlModifier)     # マーク保持の再読込
        _wait(w)
        assert len(L.marks) == 5
        key(Qt.Key_End)                         # マーククリアの再読込
        _wait(w)
        assert L.marks == set()

        # インクリメンタルサーチ
        key(Qt.Key_F)
        QTest.keyClicks(L.view, "ph")
        assert _cursor_name(L) == "photo.JPG" and "検索: ph" in L.footer.text()
        key(Qt.Key_Escape)                      # サーチ中の Esc は検索の終了だけ
        assert L.isearch_text is None and w.isVisible()
        key(Qt.Key_Escape)                      # サーチ外の Esc でも窓は閉じない
        assert w.isVisible()

        # マスク(Shift+: で入力、: で戻す)
        w.ask_text = lambda *a, **k: "txt"
        key(Qt.Key_Colon, Qt.ShiftModifier)
        assert _names(L) == ["..", "sub1", "sub2", "alpha.txt", "gamma.txt"], _names(L)
        assert "[txt]" in L.header.text()
        key(Qt.Key_Colon)
        assert len(_names(L)) == 7 and "[" not in L.header.text()

        # O: 自分を反対窓に合わせる / Shift+O: 反対窓をこちらへ / Alt+O: 入れ替え
        key(Qt.Key_O)
        _wait(w)
        assert core.same_dir(L.path, right)
        key(Qt.Key_B)                           # 戻る
        _wait(w)
        assert core.same_dir(L.path, left)
        key(Qt.Key_Period)                      # 進む(. でも)
        _wait(w)
        assert core.same_dir(L.path, right)
        key(Qt.Key_Comma)                       # 戻る(, でも)
        _wait(w)
        assert core.same_dir(L.path, left)
        key(Qt.Key_O, Qt.AltModifier)
        _wait(w)
        assert core.same_dir(L.path, right) and core.same_dir(R.path, left)
        key(Qt.Key_O, Qt.AltModifier)
        _wait(w)
        assert core.same_dir(L.path, left) and core.same_dir(R.path, right)
        key(Qt.Key_O, Qt.ShiftModifier)
        _wait(w)
        assert core.same_dir(R.path, left)
        R.load(str(right))
        _wait(w)

        # H: 履歴ジャンプ(picker で選ぶ)
        assert any(core.same_dir(p, left / "sub1") for p in w.recent)
        key(Qt.Key_H)
        picker = w._picker
        assert picker is not None
        rows = [picker.list.item(i).text() for i in range(picker.list.count())]
        target_row = next(i for i, t in enumerate(rows) if core.same_dir(t, left / "sub1"))
        picker.list.setCurrentRow(target_row)
        picker._commit()
        _wait(w)
        assert core.same_dir(L.path, left / "sub1") and w._picker is None
        key(Qt.Key_Backspace)
        _wait(w)

        # Shift+J: パス入力(ファイルのパスなら置き場所を開いてカーソルを合わせる)
        w.ask_text = lambda *a, **k: f'"{left / "sub1" / "deep"}"'
        key(Qt.Key_J, Qt.ShiftModifier)
        _wait(w)
        assert core.same_dir(L.path, left / "sub1" / "deep")
        w.ask_text = lambda *a, **k: str(left / "beta.md")
        key(Qt.Key_J, Qt.ShiftModifier)
        _wait(w)
        assert core.same_dir(L.path, left) and _cursor_name(L) == "beta.md"

        # 開けないフォルダへ行こうとしても元のフォルダに留まる
        L.load(str(left / "no_such_dir"))
        _wait(w)
        assert core.same_dir(L.path, left) and "開けません" in w.log_view.toPlainText()

        # K: フォルダ作成(作ったらそこにカーソル)
        w.ask_text = lambda *a, **k: "made by K"
        key(Qt.Key_K)
        _wait(w)
        assert (left / "made by K").is_dir() and _cursor_name(L) == "made by K"
        key(Qt.Key_K)                           # 同じ名前はメッセージだけ
        assert "同じ名前があります" in w.log_view.toPlainText()

        # R: 改名(拡張子の手前までを選ぶ)
        L.set_cursor(L._row_of("gamma.txt"))
        seen = {}

        def fake_ask(title, label, text="", select=None):
            seen.update(text=text, select=select)
            return "gamma2.txt"
        w.ask_text = fake_ask
        key(Qt.Key_R)
        _wait(w)
        assert seen == {"text": "gamma.txt", "select": 5}, seen
        assert (left / "gamma2.txt").exists() and _cursor_name(L) == "gamma2.txt"

        # Alt+C: 反対窓へコピー(マークしたもの)、Alt+M: 移動
        L.marks.clear()
        L.set_cursor(L._row_of("alpha.txt"))
        key(Qt.Key_Space)
        L.set_cursor(L._row_of("sub2"))
        key(Qt.Key_Space)
        key(Qt.Key_C, Qt.AltModifier)
        _wait(w)
        assert (right / "alpha.txt").exists() and (right / "sub2").is_dir()
        assert (left / "alpha.txt").exists() and L.marks == set()
        assert "r.txt" in _names(R) and "alpha.txt" in _names(R)
        assert "複写: 2件" in w.log_view.toPlainText()
        L.set_cursor(L._row_of("beta.md"))
        key(Qt.Key_M, Qt.AltModifier)
        _wait(w)
        assert (right / "beta.md").exists() and not (left / "beta.md").exists()

        # C メニュー(先頭文字で即決)は choose_menu を差し替えて通す
        w.choose_menu = lambda title, items: "copy"
        L.set_cursor(L._row_of("photo.JPG"))
        key(Qt.Key_C)
        _wait(w)
        assert (right / "photo.JPG").exists()

        # D: ごみ箱へ(確認あり)。いいえなら何もしない
        w.confirm = lambda *a, **k: False
        L.set_cursor(L._row_of("photo.JPG"))
        key(Qt.Key_D)
        assert (left / "photo.JPG").exists()
        asked = {}

        def yes(title, text, default_yes=True):
            asked["default_yes"] = default_yes
            return True
        w.confirm = yes
        key(Qt.Key_Delete, Qt.ShiftModifier)    # 完全削除は既定ボタンが「いいえ」
        _wait(w)
        assert asked["default_yes"] is False and not (left / "photo.JPG").exists()

        # W: 同名ファイル比較マーク(反対窓にもある alpha.txt だけ。sub2 はフォルダ同士なので外れる)
        assert L.marks == set()
        cond = {"size": "any", "time": "any", "include_dirs": False, "strip_ext": False,
                "unmark": False}
        w.ask_compare = lambda title, include_dirs: (seen.update(title=title), dict(cond))[1]
        key(Qt.Key_W)
        assert seen["title"] == "同名ファイル比較マーク(左)"
        assert L.marks == {"alpha.txt"}, L.marks
        assert "比較マーク: 1件" in w.log_view.toPlainText()
        w.ask_compare = lambda title, include_dirs: dict(cond, include_dirs=True)
        w.ask_overlap = lambda: "add"
        key(Qt.Key_W)
        assert L.marks == {"alpha.txt", "sub2"}, L.marks
        L.marks.add("gamma2.txt")
        w.ask_overlap = lambda: "clear"
        w.ask_compare = lambda title, include_dirs: dict(cond)
        key(Qt.Key_W)
        assert L.marks == {"alpha.txt"}, L.marks
        w.ask_compare = lambda title, include_dirs: dict(cond, unmark=True)
        key(Qt.Key_W)
        assert L.marks == set()
        # 左右が同じフォルダならダイアログも出さない
        w.ask_compare = lambda *a: (_ for _ in ()).throw(AssertionError("出してはいけない"))
        R.load(str(left))
        _wait(w)
        key(Qt.Key_W)
        assert "左右が同じフォルダです" in w.log_view.toPlainText()
        R.load(str(right))
        _wait(w)

        # S: ソート(窓ごと)。Shift+S で昇順/降順だけ反転
        w.ask_sort = lambda title, spec: (seen.update(title=title),
                                          {"kind": "size", "descending": False,
                                           "dirs_first": True, "ignore_case": True})[1]
        key(Qt.Key_S)
        assert seen["title"] == "ソート(左)"
        files = [e.name for e in L.entries if not e.is_dir]
        sizes = [e.size for e in L.entries if not e.is_dir]
        assert sizes == sorted(sizes) and L.entries[0].is_parent, files
        assert "(サイズ↑)" in L.header.text(), L.header.text()
        key(Qt.Key_S, Qt.ShiftModifier)
        sizes = [e.size for e in L.entries if not e.is_dir]
        assert sizes == sorted(sizes, reverse=True) and L.entries[0].is_parent
        assert L.sort["descending"] and R.sort == core.DEFAULT_SORT

        # V(と E): マークした各ファイルを1つずつエディタで開く
        calls = []
        real_popen = filer.subprocess.Popen
        filer.subprocess.Popen = lambda args, **kw: calls.append(args)
        try:
            L.marks.update({"alpha.txt", "gamma2.txt"})
            key(Qt.Key_V)
        finally:
            filer.subprocess.Popen = real_popen
        assert sorted(os.path.basename(a[1]) for a in calls) == ["alpha.txt", "gamma2.txt"], calls
        assert all(a[0] == "x" for a in calls)
        L.marks.clear()
        L.model.all_changed()

        # 未割り当てのキーは何もしない
        before = (L.path, _cursor_name(L), set(L.marks), w.active_side)
        key(Qt.Key_X)
        key(Qt.Key_Z, Qt.ControlModifier)
        assert before == (L.path, _cursor_name(L), set(L.marks), w.active_side)

        shot = os.environ.get("FILER_SHOT")
        if shot:
            L.set_cursor(L._row_of("alpha.txt"))
            key(Qt.Key_Space)
            QTest.qWait(50)
            w.grab().save(shot, "PNG")

        # Q で閉じると状態を保存(settings.json は filer のキーだけ差し替え)
        key(Qt.Key_Q)
        QTest.qWait(20)
        assert not w.isVisible()
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        assert stored["hotkeys"] == {"x": "y"}                  # 他のキーは残る
        assert stored["filer"]["editor"] == "x"
        assert core.same_dir(stored["filer"]["left_path"], left)
        assert core.same_dir(stored["filer"]["right_path"], right)
        assert len(stored["filer"]["geometry"]) == 4 and stored["filer"]["recent"]
        assert stored["filer"]["left_sort"]["kind"] == "size"
        assert stored["filer"]["left_sort"]["descending"] is True
        w.deleteLater()


def test_bookmark_add_delete_move():
    """J: 押すたびに settings.json を読み直し、「ここを登録」・削除・並べ替えができる。
    ほかの設定(hotkeys・filer)は消さない。"""
    import filer
    import launcher

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        stored = {"hotkeys": {"x": "y"}, "filer": {"editor": "x"},
                  "launcher": {"bookmarks": [{"name": "A", "path": str(right)}]}}
        settings_path.write_text(json.dumps(stored), encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)

        # 開いたあとに外(常駐側)で足されたブックマークも出る
        stored["launcher"]["bookmarks"].append({"name": "B", "path": str(left / "sub1")})
        settings_path.write_text(json.dumps(stored), encoding="utf-8")

        w.run_command("bookmark_jump")
        p = w._picker
        names = [n for n, _ in p._items]
        assert names[:2] == ["A", "B"], names
        assert names[2].startswith(launcher.ADD_ITEM_PREFIX) and str(left) in names[2], names

        # 「ここを登録」
        original = launcher._ask_name
        launcher._ask_name = lambda parent, default: default + "_new"
        try:
            p._on_accept(*p._items[2])
        finally:
            launcher._ask_name = original
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["A", "B", os.path.basename(str(left)) + "_new"]
        assert saved["hotkeys"] == {"x": "y"} and saved["filer"] == {"editor": "x"}

        # 並べ替え(B を上へ)と削除(A)
        w.run_command("bookmark_jump")
        p = w._picker
        p._on_move(*p._items[1], -1)
        assert [n for n, _ in p._items][:2] == ["B", "A"]
        w.confirm = lambda *a, **k: True
        # 削除の控えは本物の bookmark_deleted.log ではなく一時フォルダへ
        original_log = launcher.DELETED_LOG_PATH
        launcher.DELETED_LOG_PATH = str(Path(tmp, "bookmark_deleted.log"))
        try:
            p._on_delete(*p._items[1])
        finally:
            launcher.DELETED_LOG_PATH = original_log
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["B", os.path.basename(str(left)) + "_new"]
        p.close()
        w.close()


def test_single_instance_and_show():
    import filer
    import filer_launch
    from PySide6.QtTest import QTest

    _qapp()
    if filer_launch.is_running():
        # 本物のファイラが開いている。ミューテックスも待ち受けの名前も同じなので、ここで
        # 試すと本物に「前面に出して」を送ってしまう。閉じてから流すこと。
        print("skip  test_single_instance_and_show: ファイラが開いているので飛ばします")
        return
    assert filer.acquire_single_instance() is True
    assert filer_launch.is_running()
    assert filer.acquire_single_instance() is False      # 2つ目は弾かれる
    w = filer.FilerWindow({}, None)
    shown = []
    w.bring_to_front = lambda: shown.append(True)
    w.listen()
    assert w._server is not None
    # 送る側は返事を待って止まるので、受ける側(このスレッドのイベントループ)と
    # 別のスレッドから送る。実際も別プロセスから来る。
    import threading
    sent = []
    t = threading.Thread(target=lambda: sent.append(filer_launch.send_show(2000)))
    t.start()
    for _ in range(100):
        QTest.qWait(20)
        if shown and not t.is_alive():
            break
    t.join(3)
    assert sent == [True], sent
    assert shown, "show が届かない"
    w._server.close()
    w.deleteLater()


# ---------------------------------------------------------------
# 書庫(ZIP / 7z)
# ---------------------------------------------------------------
JP_NAME = "日本語.txt"            # cp932 で 10 バイト
JP_PLACEHOLDER = "QQQQQQ.txt"     # 同じ長さの ASCII。書いた後でバイト列を差し替える


def _make_zip(path):
    """テスト用の ZIP。
    - 日本語名を cp932 で格納(UTF-8 の印なし)。zipfile では作れないので後から差し替える
    - ディレクトリのエントリを持たない(docs/sub/deep.txt だけで docs と docs/sub は無い)
    - zip slip の危ないエントリ(.. / 絶対パス / ドライブ名)"""
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.txt", b"hello")
        z.writestr("docs/a.txt", b"aaaa")
        z.writestr("docs/sub/deep.txt", b"deep")
        z.writestr(JP_PLACEHOLDER, b"jp")
        for bad in ("../evil.txt", "/abs.txt", "C:/drive.txt", "ok/../../x.txt"):
            z.writestr(zipfile.ZipInfo(bad), b"bad")
    raw = Path(path).read_bytes()
    assert raw.count(JP_PLACEHOLDER.encode()) == 2  # ローカルヘッダと中央ディレクトリ
    Path(path).write_bytes(raw.replace(JP_PLACEHOLDER.encode(), JP_NAME.encode("cp932")))


def _make_7z(path):
    import py7zr
    with py7zr.SevenZipFile(path, "w") as z:
        z.writestr(b"seven", "s1.txt")
        z.writestr(b"inner", "folder/s2.txt")


def test_archive_core():
    import filer_archive as fa
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "test.zip")
        _make_zip(zpath)
        # パスの分け方(書庫の拡張子を持つ所だけ isfile で確かめる)
        assert fa.split_archive_path(zpath) == (zpath, "")
        assert fa.split_archive_path(os.path.join(zpath, "docs", "sub")) == (zpath, "docs/sub")
        assert fa.split_archive_path(tmp) is None
        assert fa.looks_like_archive_path(r"D:\x\foo.ZIP\sub") and not fa.looks_like_archive_path(tmp)
        assert fa.safe_relpath("a\\b/./c") == "a/b/c"
        for bad in ("../x", "/x", "C:/x", "a/../../x", "", "a:b"):
            assert fa.safe_relpath(bad) is None, bad

        index = fa.get_index(zpath)
        root = sorted(e.name for e in index.list_dir(""))
        assert root == sorted(["docs", "readme.txt", JP_NAME]), root   # cp932 を読み直せている
        assert index.skipped == 4                                      # 危ないものは出さない
        assert [e.name for e in index.list_dir("docs")] == ["a.txt", "sub"] or \
            sorted(e.name for e in index.list_dir("docs")) == ["a.txt", "sub"]
        assert index.has_dir("docs/sub")                               # 合成した中間フォルダ
        assert [e.name for e in index.list_dir("docs/sub")] == ["deep.txt"]

        # read_folder から書庫の中を読める
        path, entries, _f, info = core.read_folder(os.path.join(zpath, "docs"))
        assert path == os.path.join(zpath, "docs") and info[:2] == (zpath, "docs")
        _p, _e, _f, info = core.read_folder(os.path.join(zpath, "docs", "gone"), walk_up=True)
        assert info[1] == "docs"
        try:
            core.read_folder(os.path.join(zpath, "nope"))
            raise AssertionError("無い中のフォルダで例外にならない")
        except FileNotFoundError:
            pass

        # 展開(フォルダは中身ごと、いま見ているフォルダからの相対で置く)
        out = os.path.join(tmp, "out")
        os.mkdir(out)
        plan = fa.plan_extract(index, "", ["docs", JP_NAME])
        rels = sorted(p.rel for p in plan)
        assert rels == sorted(["docs", "docs/a.txt", "docs/sub", "docs/sub/deep.txt", JP_NAME]), rels
        result = fa.extract(index, plan, out)
        assert result.written == 3 and not result.errors, result
        assert Path(out, "docs", "sub", "deep.txt").read_bytes() == b"deep"
        assert Path(out, JP_NAME).read_bytes() == b"jp"
        # 上書きの検出と、スキップ指定
        Path(out, JP_NAME).write_bytes(b"mine")
        assert sorted(fa.find_conflicts(plan, out)) == sorted(
            ["docs/a.txt", "docs/sub/deep.txt", JP_NAME])
        result = fa.extract(index, plan, out, skip={JP_NAME})
        assert result.skipped == 1 and Path(out, JP_NAME).read_bytes() == b"mine"
        # 中のフォルダから: 相対はそこから
        plan = fa.plan_extract(index, "docs", ["sub"])
        assert sorted(p.rel for p in plan) == ["sub", "sub/deep.txt"]
        # zip slip: 計画に載らず、外に何もできない
        assert not os.path.exists(os.path.join(tmp, "evil.txt"))
        assert fa._target_path(out, "../x") is None
        # 中止
        cancel = threading.Event()
        cancel.set()
        result = fa.extract(index, fa.plan_extract(index, "", ["readme.txt"]),
                            os.path.join(tmp, "out2"), cancel=cancel)
        assert result.cancelled and result.written == 0

        # 7z
        spath = os.path.join(tmp, "t.7z")
        _make_7z(spath)
        sindex = fa.get_index(spath)
        assert sorted(e.name for e in sindex.list_dir("")) == ["folder", "s1.txt"]
        assert [e.name for e in sindex.list_dir("folder")] == ["s2.txt"]
        out7 = os.path.join(tmp, "out7")
        os.mkdir(out7)
        result = fa.extract(sindex, fa.plan_extract(sindex, "", ["folder", "s1.txt"]), out7)
        assert result.written == 2 and not result.errors, result.errors
        assert Path(out7, "folder", "s2.txt").read_bytes() == b"inner"

        # 一時フォルダの掃除
        old = fa.TEMP_ROOT
        fa.TEMP_ROOT = os.path.join(tmp, "tmproot")
        try:
            made = fa.make_temp_dir()
            Path(made, "f.txt").write_bytes(b"x")
            assert fa.cleanup_temp() == 1 and not os.path.exists(made)
        finally:
            fa.TEMP_ROOT = old


def test_archive_password_and_missing_py7zr():
    import filer_archive as fa
    import py7zr
    with tempfile.TemporaryDirectory() as tmp:
        spath = os.path.join(tmp, "locked.7z")
        with py7zr.SevenZipFile(spath, "w", password="pw") as z:
            z.writestr(b"secret", "s.txt")
        index = fa.get_index(spath)
        plan = fa.plan_extract(index, "", ["s.txt"])
        assert index.needs_password and fa.needs_password(index, plan)
        result = fa.extract(index, plan, os.path.join(tmp, "ok"), password="pw")
        assert result.written == 1 and Path(tmp, "ok", "s.txt").read_bytes() == b"secret"
        for wrong in ("nope", None):                   # 違う・無いパスワードでも落ちない
            result = fa.extract(index, plan, os.path.join(tmp, f"ng_{wrong}"), password=wrong)
            assert result.written == 0 and result.errors, result

        # 上書きを選んだ ZIP の展開が途中で失敗しても、元のファイルは消えない
        # (壊れた書庫で CRC が合わない場合で確かめる。パスワード違いも同じ経路)
        bad = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(bad, "w", zipfile.ZIP_STORED) as z:
            z.writestr("k.txt", b"NEWDATA-NEWDATA")
        raw = Path(bad).read_bytes()
        Path(bad).write_bytes(raw.replace(b"NEWDATA-NEWDATA", b"XEWDATA-NEWDATA", 1))
        keep_dir = Path(tmp, "keep")
        keep_dir.mkdir()
        (keep_dir / "k.txt").write_bytes(b"original")
        bindex = fa.get_index(bad)
        result = fa.extract(bindex, fa.plan_extract(bindex, "", ["k.txt"]), str(keep_dir))
        assert result.written == 0 and result.errors, result
        assert (keep_dir / "k.txt").read_bytes() == b"original"
        assert sorted(p.name for p in keep_dir.iterdir()) == ["k.txt"]   # 書きかけも残らない

        # py7zr が無い環境: 7z を開こうとしたら知らせるだけ(ZIP は使える)
        other = os.path.join(tmp, "other.7z")
        _make_7z(other)
        saved = sys.modules.get("py7zr")
        sys.modules["py7zr"] = None
        try:
            try:
                fa.get_index(other)
                raise AssertionError("py7zr が無いのに開けた")
            except fa.ArchiveError as e:
                assert "py7zr が必要です" in str(e)
        finally:
            sys.modules["py7zr"] = saved


def _wait_extract(w, timeout=10.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QTest.qWait(20)
        if w._extracting is None:
            return
    raise AssertionError("展開が終わらない")


def test_window_archive():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        zpath = left / "pack.zip"
        _make_zip(str(zpath))
        _make_7z(str(left / "seven.7z"))
        Path(right, "readme.txt").write_bytes(b"old")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "ed"}}, str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731

        # Enter で書庫の中へ。パス表示は書庫＋中のパス
        L.set_cursor(L._row_of("pack.zip"))
        key(Qt.Key_Return)
        _wait(w)
        assert L.archive == (str(zpath), "") and L.path == str(zpath)
        assert L.header.text().startswith(str(zpath) + "\\")
        assert _names(L) == ["..", "docs", JP_NAME, "readme.txt"] or \
            sorted(_names(L)) == sorted(["..", "docs", JP_NAME, "readme.txt"]), _names(L)
        assert "危ないパスのエントリ 4件" in w.log_view.toPlainText()
        # 中を移動・BS で一つ上・書庫のルートで BS すると書庫ファイルにカーソル
        L.set_cursor(L._row_of("docs"))
        key(Qt.Key_Return)
        _wait(w)
        assert L.archive == (str(zpath), "docs") and sorted(_names(L)) == ["..", "a.txt", "sub"]
        key(Qt.Key_Backspace)
        _wait(w)
        assert L.archive == (str(zpath), "") and _cursor_name(L) == "docs"
        key(Qt.Key_Backspace)
        _wait(w)
        assert L.archive is None and core.same_dir(L.path, left) and _cursor_name(L) == "pack.zip"
        key(Qt.Key_B)                                   # 履歴で書庫の中へ戻れる
        _wait(w)
        assert L.archive == (str(zpath), "")

        # マーク・マスク・サーチ・ソート・W も書庫の中で効く
        key(Qt.Key_F)
        QTest.keyClicks(L.view, "rea")
        key(Qt.Key_Escape)
        assert _cursor_name(L) == "readme.txt"
        w.ask_compare = lambda title, include_dirs: {
            "size": "any", "time": "any", "include_dirs": False, "strip_ext": False,
            "unmark": False}
        key(Qt.Key_W)                                   # 反対窓に readme.txt がある
        assert L.marks == {"readme.txt"}, L.marks
        L.marks.clear()

        # 書き込み系は断る
        for k, mods in ((Qt.Key_D, Qt.NoModifier), (Qt.Key_K, Qt.NoModifier),
                        (Qt.Key_R, Qt.NoModifier), (Qt.Key_M, Qt.AltModifier),
                        (Qt.Key_Delete, Qt.ControlModifier), (Qt.Key_Insert, Qt.ShiftModifier)):
            before = w.log_view.toPlainText().count("書庫内")
            key(k, mods)
            assert w.log_view.toPlainText().count("書庫内") == before + 1, (k, mods)
        assert zpath.exists()

        # Alt+C: 反対窓へ展開。上書きは尋ねる(スキップ)、フォルダは中身ごと
        asked = []
        w.ask_overwrite = lambda rel, remaining: (asked.append(rel), (filer.SKIP, False))[1]
        L.marks.update({"readme.txt", "docs"})
        key(Qt.Key_C, Qt.AltModifier)
        _wait_extract(w)
        _wait(w)
        assert asked == ["readme.txt"], asked
        assert Path(right, "readme.txt").read_bytes() == b"old"          # スキップした
        assert Path(right, "docs", "sub", "deep.txt").read_bytes() == b"deep"
        assert "展開: 2件" in w.log_view.toPlainText()
        # 上書き＋「残りも同じ」
        asked.clear()
        w.ask_overwrite = lambda rel, remaining: (asked.append(rel), (filer.OVERWRITE, True))[1]
        L.marks.update({"readme.txt", "docs"})
        key(Qt.Key_C, Qt.AltModifier)
        _wait_extract(w)
        _wait(w)
        assert len(asked) == 1 and Path(right, "readme.txt").read_bytes() == b"hello"
        # 取消なら何もしない
        Path(right, "readme.txt").write_bytes(b"old")
        w.ask_overwrite = lambda rel, remaining: (filer.CANCEL, False)
        L.set_cursor(L._row_of("readme.txt"))
        key(Qt.Key_C, Qt.AltModifier)
        assert w._extracting is None and Path(right, "readme.txt").read_bytes() == b"old"
        assert not Path(tmp, "evil.txt").exists() and not Path(right, "evil.txt").exists()

        # Enter / E は一時フォルダへ取り出して開く
        opened = []
        w._startfile = lambda path: opened.append(path)
        L.set_cursor(L._row_of(JP_NAME))
        key(Qt.Key_Return)
        _wait_extract(w)
        assert len(opened) == 1 and Path(opened[0]).read_bytes() == b"jp"
        assert opened[0].startswith(_TEST_TEMP)
        calls = []
        real_popen = filer.subprocess.Popen
        filer.subprocess.Popen = lambda args, **kw: calls.append(args)
        try:
            key(Qt.Key_E)
            _wait_extract(w)
        finally:
            filer.subprocess.Popen = real_popen
        assert calls and calls[0][0] == "ed" and calls[0][1].endswith(JP_NAME)

        # O: 反対窓を書庫の中へ(Shift+O)。反対窓が書庫の中なら Alt+C は断る
        key(Qt.Key_O, Qt.ShiftModifier)
        _wait(w)
        assert R.archive == (str(zpath), "")
        key(Qt.Key_Backspace)
        _wait(w)
        assert L.archive is None
        L.set_cursor(L._row_of("alpha.txt"))
        key(Qt.Key_C, Qt.AltModifier)
        assert "書庫内へは書き込めません" in w.log_view.toPlainText()
        R.load(str(right))
        _wait(w)

        # 7z も同じように入れて、展開できる
        L.set_cursor(L._row_of("seven.7z"))
        key(Qt.Key_Return)
        _wait(w)
        assert L.archive == (str(left / "seven.7z"), "") and "folder" in _names(L)
        L.set_cursor(L._row_of("folder"))
        key(Qt.Key_C, Qt.AltModifier)
        _wait_extract(w)
        _wait(w)
        assert Path(right, "folder", "s2.txt").read_bytes() == b"inner"

        # 閉じたとき、書庫の中のパスをそのまま保存する(次回もそこが開く)
        key(Qt.Key_Q)
        QTest.qWait(20)
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        assert stored["filer"]["left_path"] == str(left / "seven.7z")
        w.deleteLater()

        # 次回: 書庫の中のパスから開ける
        w2 = filer.FilerWindow({"filer": {"left_path": str(zpath / "docs" / "sub"),
                                          "right_path": str(right)}}, None)
        w2.show()
        w2.start()
        _wait(w2)
        assert w2.left.archive == (str(zpath), "docs/sub")
        w2.close()
        w2.deleteLater()


def test_window_layout_and_font():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "log_lines": 3, "split_ratio": 0.5, "font_size": 10}},
                              str(settings_path))
        w.resize(1000, 700)
        w.show()
        w.start()
        _wait(w)
        QTest.qWait(30)
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731

        width0 = w.left.width()
        key(Qt.Key_Left, Qt.AltModifier)
        key(Qt.Key_Left, Qt.ControlModifier)            # AFXW.key で Ctrl+← = Alt+←
        assert w.split_ratio == 0.44 and w.left.width() < width0, (w.split_ratio, w.left.width())
        assert w.active_side == "left"                  # 窓の切り替えは起きない
        key(Qt.Key_Right, Qt.AltModifier)
        assert w.split_ratio == 0.47
        for _ in range(30):
            key(Qt.Key_Left, Qt.AltModifier)
        assert w.split_ratio == core.SPLIT_MIN          # 端に寄りすぎない

        log0 = w.log_view.height()
        key(Qt.Key_Up, Qt.AltModifier)                  # メッセージ窓を大きく
        assert w.log_lines == 4 and w.log_view.height() > log0
        for _ in range(10):
            key(Qt.Key_Down, Qt.AltModifier)
        assert w.log_lines == 1
        for _ in range(200):
            key(Qt.Key_Up, Qt.AltModifier)
        assert w.log_lines == w._max_log_lines() and w.log_view.height() <= w.height() // 2 + 20
        key(Qt.Key_Down, Qt.AltModifier)

        row_h = w.left.view.verticalHeader().defaultSectionSize()
        key(Qt.Key_Down, Qt.ControlModifier)            # 拡大
        key(Qt.Key_Down, Qt.ShiftModifier)
        assert w.list_font.pointSize() == 12
        assert w.left.view.verticalHeader().defaultSectionSize() >= row_h
        assert w.right.view.font().pointSize() == 12    # 両窓に効く
        key(Qt.Key_Up, Qt.ShiftModifier)                # 縮小
        assert w.list_font.pointSize() == 11
        assert _cursor_name(w.left) == ".."             # カーソルは動かない(PgUp ではない)

        lines = w.log_lines
        key(Qt.Key_Q)
        QTest.qWait(20)
        stored = json.loads(settings_path.read_text(encoding="utf-8"))["filer"]
        assert stored["split_ratio"] == core.SPLIT_MIN, stored["split_ratio"]
        assert stored["log_lines"] == lines and stored["font_size"] == 11
        w.deleteLater()


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    # os._exit は後始末を走らせないので、テスト用の一時フォルダはここで消す。
    shutil.rmtree(_TEST_TEMP, ignore_errors=True)
    sys.stdout.flush()
    os._exit(code)
