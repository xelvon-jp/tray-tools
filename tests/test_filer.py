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
    assert km["0120"] == "open_shell" and km["0121"] == "open_explorer"   # F9 / F10
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
    assert core.format_mtime(stamp + 7) == "26/10/23 09:05:07"
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


def test_hash_helpers():
    """ハッシュ計算とクリップボード用テキスト(表示なしで確かめられる部分)。"""
    import hashlib

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp, "data.bin")
        data = bytes(range(256)) * 5000  # 1MB を超えて分割読みの経路も通す
        p.write_bytes(data)
        assert core.sha256_of_file(str(p)) == hashlib.sha256(data).hexdigest()
        empty = Path(tmp, "empty")
        empty.write_bytes(b"")
        assert core.sha256_of_file(str(empty)) == \
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert core.hash_clip_text([("a.txt", "abc")]) == "abc"
    assert core.hash_clip_text([("a.txt", "aaa"), ("b.txt", "bbb")]) == "aaa  a.txt\r\nbbb  b.txt"
    assert core.log_selection_text("1行目\u20292行目") == "1行目\r\n2行目"
    assert core.log_selection_text("選択のみ") == "選択のみ"


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


def _run_stub(holder, dialog):
    """exec を差し替えたダイアログで holder["do"](dialog) を走らせる。

    ファイラはコマンドの中の例外を受け止めてメッセージ窓に出す(落ちないように)ので、
    ダイアログの中で assert が外れても、そのままではテストに伝わらない。ここで控えて、
    _checked_key がキーを送った後に投げ直す。"""
    holder["dialog"] = True
    try:
        holder["do"](dialog)
    except BaseException as e:  # noqa: BLE001
        holder["error"] = e
    holder["result"] = dialog.result()      # 戻ると deleteLater で消えるので先に控える
    return holder["result"]


def _checked_key(pane, holder):
    """キーを送り、その中で開いたダイアログで起きた失敗を投げ直す key 関数。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    def key(k, mods=Qt.NoModifier):
        QTest.keyClick(pane.view, k, mods)
        error = holder.pop("error", None)
        if error is not None:
            raise error
    return key


_TEST_TEMP = tempfile.mkdtemp(prefix="filer_test_tmp_")
# よく行くフォルダ・一括改名の履歴は、本物の記録(%LOCALAPPDATA%\traytools)に書かせない。
os.environ[core.DATA_DIR_ENV] = os.path.join(_TEST_TEMP, "data")


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
        assert "Mark 5 (31)" in L.footer.text(), L.footer.text()   # 10+20+1 バイト
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
        assert L.header.text() == L.path + "\\ txt", L.header.text()   # あふｗと同じく常にマスク
        key(Qt.Key_Colon)
        assert len(_names(L)) == 7 and L.header.text() == L.path + "\\ *.*"

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

        # R: ファイル情報の編集(キャレットは拡張子の手前・何も選ばない)。exec は差し替える
        L.set_cursor(L._row_of("gamma.txt"))
        seen = {}

        def fake_exec(dialog):
            seen.update(text=dialog.name_edit.text(), caret=dialog.name_edit.cursorPosition(),
                        selected=dialog.name_edit.hasSelectedText())
            dialog.name_edit.setText("gamma2.txt")
            dialog.try_accept()
            return dialog.result()
        w.exec_dialog = fake_exec
        key(Qt.Key_R)
        _wait(w)
        assert seen == {"text": "gamma.txt", "caret": 5, "selected": False}, seen
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

        # C メニューは choose_menu を差し替えて通す(文字キーの選択のみの動きは別テスト)
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


def test_shift_enter_and_menu_select_only():
    """Shift+Enter はフォルダを反対窓で開き、メニューの文字キーは選択のみ。

    - フォルダで Shift+Enter → 反対窓がそのフォルダに移る(自窓は動かない)。
      ファイルで Shift+Enter → 従来どおりエディタ。`..` → 親を反対窓で開く。
    - C → P では「フルパス」の項目が選ばれるだけで、クリップボードへは書かない。
      実行は Enter を押して初めて行われる(2026-10-26 ユーザー要望)。
    """
    import filer
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        try:
            assert core.DEFAULT_KEYMAP["1013"] == "open_in_other"
            assert core.DEFAULT_KEYMAP["0069"] == "edit"  # E は従来どおりエディタ

            # フォルダ: 反対窓で開く。自窓はそのまま、フォーカスも移らない
            L.set_cursor(L._row_of("sub1"))
            key(Qt.Key_Return, Qt.ShiftModifier)
            _wait(w)
            assert core.same_dir(R.path, left / "sub1"), R.path
            assert core.same_dir(L.path, left) and w.active_side == "left"

            # ファイル: エディタで開く(従来どおり)
            calls = []
            real_popen = filer.subprocess.Popen
            filer.subprocess.Popen = lambda args, **kw: calls.append(args)  # noqa: E731
            try:
                L.set_cursor(L._row_of("alpha.txt"))
                key(Qt.Key_Return, Qt.ShiftModifier)
            finally:
                filer.subprocess.Popen = real_popen
            assert [os.path.basename(a[1]) for a in calls] == ["alpha.txt"], calls

            # `..`: 親フォルダを反対窓で開く
            R.set_cursor(R._row_of(".."))
            w.activate("right")
            key(Qt.Key_Return, Qt.ShiftModifier)
            _wait(w)
            assert core.same_dir(L.path, left), L.path

            # メニュー: P は選択のみ。実行(triggered)は Enter を押して初めて起きる。
            # 実物の exec() はモーダルでテストのスレッドを止めるので、popup で開いて
            # triggered を直接見る(choose_menu / _exec_menu の中身は既存テストが通している)。
            menu, chosen = w.build_menu("複写", [
                ("O", "反対窓にコピー", "copy"),
                ("P", "フルパスをクリップボードへ", "path"),
                ("Q", "キャンセル", None),
            ])
            fired = []
            menu.triggered.connect(lambda action: fired.append(chosen[action]))
            menu.popup(w.mapToGlobal(QPoint(50, 50)))
            QTest.qWait(30)
            assert menu.isVisible()
            assert menu.activeAction().text().startswith("&O")  # 先頭が選ばれている
            QTest.keyClick(menu, Qt.Key_P)
            QTest.qWait(30)
            assert menu.isVisible(), "文字キーでメニューが閉じてしまった"
            assert menu.activeAction().text().startswith("&P")
            assert fired == [], "文字キーで実行されてしまった"
            QTest.keyClick(menu, Qt.Key_Return)
            QTest.qWait(30)
            assert fired == ["path"], fired
            assert not menu.isVisible()
        finally:
            w.close()
            w.deleteLater()


def test_hash_copy():
    """C→H: ファイルの SHA-256 を裏で計算し、クリップボードへ入れる。

    メニュー選択そのものは test_shift_enter_and_menu_select_only が見ているので、
    ここでは choose_menu を差し替えて「H を選んだ」ことにし、計算スレッドの
    完了(bridge.hashed → _on_hashed)までを確かめる。"""
    import hashlib

    import filer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        try:
            expected = hashlib.sha256(b"a" * 10).hexdigest()  # alpha.txt の中身
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            w.choose_menu = lambda title, items: "hash"
            w.cmd_copy_menu()
            deadline = time.monotonic() + 5.0
            while w._hash_busy and time.monotonic() < deadline:
                QTest.qWait(20)
            assert not w._hash_busy, "ハッシュ計算が終わらない"
            QTest.qWait(30)
            assert f"SHA-256 alpha.txt: {expected}" in w.log_view.toPlainText()
            assert "クリップボード: 1件のハッシュ" in w.log_view.toPlainText()
            assert QGuiApplication.clipboard().text() == expected

            # フォルダだけのときは計算しない(案内だけ出す)
            w.left.set_cursor(w.left._row_of("sub1"))
            w.cmd_copy_menu()
            assert "フォルダは計算しません" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()


def test_log_selection_ctrl_c():
    """ログで範囲選択して Ctrl+C すると、選択した文字列がクリップボードへ入る。

    ログは NoFocus でキーを受け取らないため、Ctrl+C は一覧側の handle_key に
    届く。選択が無いときの Ctrl+英字(頭文字ジャンプ)は従来どおり。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication, QTextCursor
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        try:
            w.log("1行目")
            w.log("2行目")
            cursor = w.log_view.textCursor()
            cursor.movePosition(QTextCursor.Start)
            cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
            w.log_view.setTextCursor(cursor)
            assert w.log_view.textCursor().hasSelection()
            QTest.keyClick(w.active.view, Qt.Key_C, Qt.ControlModifier)
            QTest.qWait(30)
            expected = w.log_view.toPlainText().replace("\n", "\r\n")
            assert QGuiApplication.clipboard().text() == expected, \
                QGuiApplication.clipboard().text()
        finally:
            w.close()
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


def test_open_shell_and_explorer():
    """F9 は PowerShell を cwd=いまのフォルダで、F10 はエクスプローラでいまのフォルダを。
    書庫の中なら書庫のあるフォルダ。実際には起動せず、渡す引数だけ確かめる。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        with zipfile.ZipFile(left / "pack.zip", "w") as z:
            z.writestr("in/a.txt", b"a")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "shell": "pwsh.exe"}}, None)
        w.resize(900, 500)
        w.show()
        w.start()
        _wait(w)
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append((args, kwargs.get("cwd")))

        original = filer.subprocess.Popen
        filer.subprocess.Popen = FakePopen
        try:
            QTest.keyClick(w.active.view, Qt.Key_F9)
            QTest.keyClick(w.active.view, Qt.Key_F10)
            assert calls[0] == (["pwsh.exe", "-NoExit"], str(left)), calls
            assert calls[1][0] == ["explorer.exe", os.path.normpath(str(left))], calls
            # 書庫の中では書庫のあるフォルダ
            w.active.load(str(left / "pack.zip" / "in"))
            _wait(w)
            assert w.active.archive is not None
            calls.clear()
            QTest.keyClick(w.active.view, Qt.Key_F9)
            QTest.keyClick(w.active.view, Qt.Key_F10)
            assert calls[0][1] == str(left), calls
            assert calls[1][0][1] == os.path.normpath(str(left)), calls
        finally:
            filer.subprocess.Popen = original
        w.close()
        w.deleteLater()


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

        # Ctrl+Enter(関連付け) / E は一時フォルダへ取り出して開く
        opened = []
        w._startfile = lambda path: opened.append(path)
        L.set_cursor(L._row_of(JP_NAME))
        key(Qt.Key_Return, Qt.ControlModifier)
        _wait_extract(w)
        assert len(opened) == 1 and Path(opened[0]).read_bytes() == b"jp"
        assert opened[0].startswith(_TEST_TEMP)
        # Enter は .txt なのでビュアで(取り出してから。表示名は書庫の中のパス)
        key(Qt.Key_Return)
        _wait_extract(w)
        assert len(opened) == 1 and len(w._viewers) == 1
        viewer = w._viewers[0]
        assert viewer.path.startswith(_TEST_TEMP) and viewer.title_name == str(zpath / JP_NAME)
        viewer.close()
        assert not w._viewers
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
    from PySide6.QtGui import QFontMetrics
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

        # 字の大きさは以前の共通の font_size から始まり、左右別々に変わる
        assert w.left.pane_font.pointSize() == w.right.pane_font.pointSize() == 10
        row_h = w.left.view.verticalHeader().defaultSectionSize()
        # 行間は空けない(行の高さ＝字の高さ)
        assert row_h == QFontMetrics(w.left.pane_font).height(), row_h
        key(Qt.Key_Down, Qt.ControlModifier)            # 拡大
        key(Qt.Key_Down, Qt.ShiftModifier)
        assert w.left.pane_font.pointSize() == 12
        assert w.left.view.font().pointSize() == 12
        assert w.left.view.verticalHeader().defaultSectionSize() > row_h
        assert w.right.pane_font.pointSize() == 10      # 反対窓には効かない
        assert w.right.view.font().pointSize() == 10
        key(Qt.Key_Up, Qt.ShiftModifier)                # 縮小
        assert w.left.pane_font.pointSize() == 11
        assert _cursor_name(w.left) == ".."             # カーソルは動かない(PgUp ではない)
        key(Qt.Key_Tab)                                 # 右窓だけを小さく。8 より小さくならない
        for _ in range(10):
            key(Qt.Key_Up, Qt.ShiftModifier)
        assert w.right.pane_font.pointSize() == core.FONT_SIZE_MIN == 8
        assert w.left.pane_font.pointSize() == 11
        key(Qt.Key_Tab)

        lines = w.log_lines
        key(Qt.Key_Q)
        QTest.qWait(20)
        stored = json.loads(settings_path.read_text(encoding="utf-8"))["filer"]
        assert stored["split_ratio"] == core.SPLIT_MIN, stored["split_ratio"]
        assert stored["log_lines"] == lines
        assert stored["left_font_size"] == 11 and stored["right_font_size"] == 8
        w.deleteLater()


# ---------------------------------------------------------------
# 属性(Shift+Z・表示色)・ドラッグ・マウス・カーソルの描画
# ---------------------------------------------------------------
import stat as _stat  # noqa: E402

HIDDEN = _stat.FILE_ATTRIBUTE_HIDDEN
SYSTEM = _stat.FILE_ATTRIBUTE_SYSTEM
READONLY = _stat.FILE_ATTRIBUTE_READONLY


def test_attr_filter_and_color_kind():
    entries = [core.PARENT_ENTRY, F("plain"), F("h", attrs=HIDDEN), F("s", attrs=SYSTEM),
               F("hs", attrs=HIDDEN | SYSTEM), F("hdir", is_dir=True, attrs=HIDDEN)]
    names = lambda h, s: [e.name for e in core.filter_attrs(entries, h, s)]  # noqa: E731
    assert names(True, True) == ["..", "plain", "h", "s", "hs", "hdir"]
    assert names(False, True) == ["..", "plain", "s"]          # 隠しフォルダも消える
    assert names(True, False) == ["..", "plain", "h", "hdir"]  # 両方持つものはどちらでも消える
    assert names(False, False) == ["..", "plain"]
    assert F("x").color_kind == "text"
    assert F("x", attrs=READONLY).color_kind == "readonly"
    assert F("x", attrs=HIDDEN | READONLY).color_kind == "hidden"
    assert F("x", attrs=SYSTEM | HIDDEN).color_kind == "system"
    assert F("d", is_dir=True, attrs=HIDDEN | READONLY).color_kind == "directory"  # 属性に関係なく
    assert core.DEFAULT_KEYMAP["1090"] == "settings_menu"


def test_zip_dos_attrs():
    import zipfile
    import filer_archive as fa

    class Info:
        def __init__(self, external_attr, create_system=0):
            self.external_attr = external_attr
            self.create_system = create_system

    assert fa.zip_dos_attrs(Info(0x02 | 0x20)) == HIDDEN               # アーカイブ属性は落とす
    assert fa.zip_dos_attrs(Info(0x07)) == HIDDEN | SYSTEM | READONLY
    assert fa.zip_dos_attrs(Info((0o100644 << 16) | 0x02, create_system=3)) == 0  # Unix 製は見ない
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "attrs.zip")
        with zipfile.ZipFile(path, "w") as z:
            for name, attr in (("plain.txt", 0x20), ("hidden.txt", 0x02), ("sys.txt", 0x04)):
                info = zipfile.ZipInfo(name)
                info.create_system = 0
                info.external_attr = attr
                z.writestr(info, b"x")
        index = fa.get_index(path)
        attrs = {e.name: e.attrs for e in index.list_dir("")}
        assert attrs == {"plain.txt": 0, "hidden.txt": HIDDEN, "sys.txt": SYSTEM}, attrs


def test_drag_paths():
    entries = [core.PARENT_ENTRY, F("a.txt"), F("d", is_dir=True), F("b.txt")]
    assert core.drag_paths("C:\\x", entries, set()) == []           # マークが無ければ運ばない
    assert core.drag_paths("C:\\x", entries, {"b.txt", "d", ".."}) == ["C:\\x\\d", "C:\\x\\b.txt"]


def _set_attrs(path, attrs):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL("kernel32", use_last_error=True).SetFileAttributesW
    fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    fn.restype = wintypes.BOOL
    assert fn(str(path), attrs), ctypes.get_last_error()


def test_window_menu_attrs_mouse_drag_drawing():
    import filer
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QColor, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        hidden = left / "zz_hidden.txt"
        system = left / "zz_system.txt"
        readonly = left / "zz_readonly.txt"
        for p in (hidden, system, readonly):
            p.write_bytes(b"x")
        _set_attrs(hidden, 0x02)
        _set_attrs(system, 0x04)
        _set_attrs(readonly, 0x01)
        many = right / "many"
        many.mkdir()
        for i in range(120):
            (many / f"f{i:03d}.txt").write_bytes(b"x")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(many)}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        try:
            # 1. メニューは先頭の項目が選ばれた状態で開く(Enter 一発で先頭)
            menu, chosen = w.build_menu("複写", [("O", "反対窓にコピー", "copy"),
                                                 ("Q", "キャンセル", None)])
            assert menu.activeAction() is menu.actions()[0]
            menu.popup(w.mapToGlobal(QPoint(50, 50)))
            QTest.qWait(30)
            assert menu.activeAction() is menu.actions()[0]
            menu.close()
            # choose_menu も同じ組み方を通る(Enter 相当＝いま選ばれている項目を返す)
            seen_first = []
            w._exec_menu = lambda m, c: (seen_first.append(m.activeAction().text()),
                                         c[m.activeAction()])[1]
            L.set_cursor(L._row_of("alpha.txt"))
            assert w.choose_menu("移動", [("O", "反対窓に移動", "move"),
                                          ("Q", "キャンセル", None)]) == "move"

            # 2. Shift+Z: 先頭(隠しファイル)を Enter → 隠しが消える。カーソルは付いていく
            assert {"zz_hidden.txt", "zz_system.txt"} <= set(_names(L))
            key(Qt.Key_Z, Qt.ShiftModifier)
            assert seen_first[-1].startswith("&H")
            assert "zz_hidden.txt" not in _names(L) and "zz_system.txt" in _names(L)
            assert _cursor_name(L) == "alpha.txt" and "〈隠 非表示〉" in L.header.text()
            w._exec_menu = lambda m, c: "system"
            key(Qt.Key_Z, Qt.ShiftModifier)
            assert "zz_system.txt" not in _names(L) and "〈隠/シ 非表示〉" in L.header.text()
            stored = json.loads(settings_path.read_text(encoding="utf-8"))["filer"]
            assert stored["show_hidden"] is False and stored["show_system"] is False
            w._exec_menu = lambda m, c: "hidden"
            key(Qt.Key_Z, Qt.ShiftModifier)
            w._exec_menu = lambda m, c: "system"
            key(Qt.Key_Z, Qt.ShiftModifier)
            assert {"zz_hidden.txt", "zz_system.txt"} <= set(_names(L))
            assert "〈" not in L.header.text()

            # 3. カーソル行は背景を塗らず下線だけ(アクティブはシアン、非アクティブは控えめ)
            L.set_cursor(L._row_of("beta.md"))
            QTest.qWait(30)
            image = L.view.viewport().grab().toImage()
            rect = L.view.visualRect(L.model.index(L.cursor_row(), 0))
            x = rect.right() - 3
            bg = QColor(filer.DEFAULT_COLORS["background"])
            top_px = QColor(image.pixel(x, rect.top() + 1))
            assert top_px == bg, top_px.name()
            assert QColor(image.pixel(x, rect.bottom())).name() == filer.DEFAULT_COLORS["cursor"]
            assert QColor(image.pixel(x, rect.bottom() - filer.CURSOR_LINE_ACTIVE)) == bg
            image_r = R.view.viewport().grab().toImage()
            rect_r = R.view.visualRect(R.model.index(R.cursor_row(), 0))
            assert QColor(image_r.pixel(rect_r.right() - 3, rect_r.bottom())).name() == \
                filer.DEFAULT_COLORS["cursor_inactive"]
            # マーク行はマーク背景(左端は mark の灰色)。カーソルが乗れば下線も
            L.marks.add("beta.md")
            L.model.all_changed()
            QTest.qWait(30)
            image = L.view.viewport().grab().toImage()
            mark_px = QColor(image.pixel(1, rect.top() + 1))
            assert mark_px != bg and abs(mark_px.red() - 0x7f) < 12, mark_px.name()
            assert QColor(image.pixel(x, rect.bottom())).name() == filer.DEFAULT_COLORS["cursor"]
            L.marks.clear()
            L.model.all_changed()

            # 4. マウス: クリック・ダブルクリック・右クリックは何もしない
            before = (L.path, _cursor_name(L), w.active_side)
            sub_rect = L.view.visualRect(L.model.index(L._row_of("sub1"), 0))
            QTest.mouseClick(L.view.viewport(), Qt.LeftButton, Qt.NoModifier, sub_rect.center())
            QTest.mouseDClick(L.view.viewport(), Qt.LeftButton, Qt.NoModifier, sub_rect.center())
            QTest.mouseClick(L.view.viewport(), Qt.RightButton, Qt.NoModifier, sub_rect.center())
            _wait(w)
            assert (L.path, _cursor_name(L), w.active_side) == before
            r_rect = R.view.visualRect(R.model.index(5, 0))
            QTest.mouseClick(R.view.viewport(), Qt.LeftButton, Qt.NoModifier, r_rect.center())
            assert w.active_side == "left" and R.cursor_row() == 0    # 窓も切り替わらない
            # ホイールは一覧のスクロール。カーソルは画面の外へ出そうなときだけ
            # 画面の端の行へ付いてくる(あふｗと同じ)
            bar = R.view.verticalScrollBar()
            assert bar.maximum() > 0 and bar.value() == 0

            def roll(delta):
                QApplication.sendEvent(R.view.viewport(), QWheelEvent(
                    QPointF(r_rect.center()),
                    QPointF(R.view.viewport().mapToGlobal(r_rect.center())),
                    QPoint(0, 0), QPoint(0, delta), Qt.NoButton, Qt.NoModifier,
                    Qt.NoScrollPhase, False))

            def top_visible_row():
                rows = [r for r in range(len(R.entries))
                        if R.view.viewport().rect().contains(
                            R.view.visualRect(R.model.index(r, 0)))]
                return rows[0], rows[-1]

            roll(-120)                                     # 先頭にカーソル → 下へ回す
            first, _last = top_visible_row()
            assert bar.value() > 0 and first > 0
            assert R.cursor_row() == first                 # 画面内の一番上の行へ
            # 画面の中にあるカーソルは動かさない
            R.set_cursor(first + 1)
            roll(120)                                      # 上へ戻す(カーソルはまだ画面内)
            assert R.cursor_row() == first + 1
            # カーソルが画面の一番下の行にあるとき上へ回すと、下端の外へ出そうになるので
            # 新しい画面の一番下の行へ付いてくる
            roll(-120 * 3)
            before_last = top_visible_row()[1]
            R.set_cursor(before_last)
            roll(120)
            new_last = top_visible_row()[1]
            assert new_last < before_last
            assert R.cursor_row() == new_last, (R.cursor_row(), new_last)

            # 5. ドラッグで運ぶのはマークしたものだけ。マークが無ければ始めない
            assert w.drag_mime(L) is None
            L.marks.update({"alpha.txt", "sub2"})
            mime = w.drag_mime(L)
            got = sorted(os.path.normcase(u.toLocalFile().replace("/", "\\")) for u in mime.urls())
            want = sorted(os.path.normcase(str(p)) for p in (left / "alpha.txt", left / "sub2"))
            assert got == want, got
            L.marks.clear()
            # 書庫の中からはドラッグしない
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            L.marks.add("readme.txt")
            assert w.drag_mime(L) is None
            assert "書庫内からはドラッグできません" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()
            for p in (hidden, system, readonly):
                _set_attrs(p, 0x80)   # FILE_ATTRIBUTE_NORMAL。後片付けで消せるように


# ---------------------------------------------------------------
# F7 コンテキストメニュー・頭文字ジャンプ・範囲マーク
# ---------------------------------------------------------------
def test_context_targets_and_args():
    entries = [core.PARENT_ENTRY, F("d", is_dir=True), F("a.txt"), F("b.txt")]
    folder = "C:\\x"
    assert core.context_targets(folder, entries, set(), 2) == ["C:\\x\\a.txt"]
    assert core.context_targets(folder, entries, {"b.txt", "d"}, 0) == ["C:\\x\\d", "C:\\x\\b.txt"]
    assert core.context_targets(folder, entries, set(), 0) == [folder]   # ".." ならフォルダ自身
    assert core.context_targets(folder, [], set(), -1) == [folder]
    assert core.DEFAULT_KEYMAP["0118"] == "context_menu"
    # Qt の論理座標 → 物理座標(画面の左上はそのまま、中は拡大率で)
    assert core.to_native_point(100, 50, 0, 0, 1.5) == (150, 75)
    assert core.to_native_point(1920 + 100, 50, 1920, 0, 2.0) == (2120, 100)
    assert core.to_native_point(10, 10, 0, 0, None) == (10, 10)
    # 引数の受け渡し(往復で同じものが戻る。"-" で始まる名前も取り違えない)
    paths = ["C:\\x\\a b.txt", "C:\\x\\-dash"]
    args = core.context_menu_args(paths, 10, -20, 1234)
    parsed = core.parse_context_args(args)
    assert parsed["paths"] == paths and (parsed["x"], parsed["y"], parsed["owner"]) == (10, -20, 1234)
    assert parsed["list"] is False and parsed["repeat"] == 1
    assert core.parse_context_args(["--list", "--repeat", "3", "--", "C:\\a"])["repeat"] == 3
    many = [f"C:\\folder\\{'x' * 100}_{i}.txt" for i in range(200)]
    assert core.context_menu_args(many, 0, 0) is None                    # 長すぎれば JSON で
    with tempfile.TemporaryDirectory() as tmp:
        jpath = os.path.join(tmp, "a.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(core.context_menu_payload(many, 5, 6, 7), f)
        parsed = core.parse_context_args(["--json", jpath])
        assert parsed["paths"] == many and (parsed["x"], parsed["y"], parsed["owner"]) == (5, 6, 7)


def test_contextmenu_child_list_mode():
    """子プロセスにメニューを組み立てさせて項目を出させる(画面には何も出さない)。
    gc.collect を挟んで繰り返しても落ちないこと(CLAUDE.md の COM の寿命の教訓)。"""
    import subprocess
    script = Path(__file__).resolve().parent.parent / "filer_contextmenu.py"
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "a.txt").write_text("a", encoding="utf-8")
        Path(tmp, "b.txt").write_text("b", encoding="utf-8")
        Path(tmp, "sub").mkdir()
        for targets, repeat in (([Path(tmp, "a.txt")], 20),
                                ([Path(tmp, "a.txt"), Path(tmp, "b.txt"), Path(tmp, "sub")], 20),
                                ([Path(tmp)], 5)):                     # フォルダ自身(".." の上)
            proc = subprocess.run(
                [sys.executable, str(script), "--list", "--repeat", str(repeat), "--"]
                + [str(p) for p in targets],
                capture_output=True, timeout=180)
            out = proc.stdout.decode("utf-8", "replace")
            assert proc.returncode == 0, (proc.returncode, proc.stderr.decode("utf-8", "replace"))
            assert out.rstrip().endswith("done"), out[-500:]
            rows = [line.split("\t") for line in out.splitlines() if line.count("\t") == 3]
            rounds = {r[0] for r in rows}
            assert len(rounds) == repeat, rounds
            labels = [r[3] for r in rows if r[0] == "0" and r[3]]
            assert len(labels) >= 3, labels
            assert any(("プロパティ" in l) or ("Properties" in l) for l in labels), labels


def test_initial_jump_and_range_mark_logic():
    entries = [core.PARENT_ENTRY, F("Apple", is_dir=True), F("banana"), F("apricot.txt"),
               F("1st.txt"), F("avocado")]
    assert core.initial_jump_char(65, ctrl=True) == "a"
    assert core.initial_jump_char(49, ctrl=True) == "1"
    assert core.initial_jump_char(97, ctrl=True) == "1"                 # テンキーの 1
    assert core.initial_jump_char(65, ctrl=True, shift=True) is None   # Ctrl+Shift は対象外
    assert core.initial_jump_char(65, ctrl=True, alt=True) is None
    assert core.initial_jump_char(65) is None
    assert core.initial_jump_char(35, ctrl=True) is None                # Ctrl+End などは別
    jump = core.initial_jump_index
    assert jump(entries, "a", 0) == 1 and jump(entries, "a", 1) == 3
    assert jump(entries, "a", 3) == 5 and jump(entries, "a", 5) == 1    # 末尾から先頭へ巡回
    assert jump(entries, "1", 0) == 4 and jump(entries, "z", 0) is None
    assert jump(entries, ".", 0) is None                                # ".." は対象外
    assert core.disabled_codes({"2065": "", "2066": "quit", "bad": ""}) == {"2065"}

    rows = [core.PARENT_ENTRY, F("d", is_dir=True), F("a"), F("b"), F("c"), F("e")]
    rm = core.range_mark_names
    assert rm(rows, {"d"}, 4, -1) == {"d", "a", "b", "c"}               # 直上のマークから
    assert rm(rows, {"e"}, 2, 1) == {"a", "b", "c", "e"}                # 直下のマークから
    assert rm(rows, {"e"}, 2, -1) == set()                              # その方向に無ければ何も
    assert rm(rows, {"c"}, 4, -1) == set()                              # カーソル行自身は数えない
    assert rm(rows, {"c", "a"}, 4, -1) == {"a", "b", "c"}
    assert rm(rows, set(), 3, -1) == set()
    assert core.DEFAULT_KEYMAP["2032"] == "mark_range_up"
    assert core.DEFAULT_KEYMAP["3032"] == "mark_range_down"


class _FakeProc:
    def __init__(self, argv, **kwargs):
        _FakeProc.calls.append(argv)

    def wait(self):
        return 0


def test_window_f7_jump_range():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "keys": {"2071": ""}}}, str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        real_popen = filer.subprocess.Popen
        _FakeProc.calls = []
        filer.subprocess.Popen = _FakeProc
        try:
            # F7: カーソル行 → 子プロセスを正しい引数で起こす
            L.set_cursor(L._row_of("alpha.txt"))
            key(Qt.Key_F7)
            argv = _FakeProc.calls[-1]
            assert argv[1].endswith("filer_contextmenu.py") and "--" in argv
            assert argv[argv.index("--") + 1:] == [os.path.join(L.path, "alpha.txt")]
            assert int(argv[argv.index("--owner") + 1]) == int(w.winId())
            _wait(w)
            # マークがあればマーク、".." ならいまのフォルダ自身
            L.marks.update({"beta.md", "sub1"})
            key(Qt.Key_F7)
            argv = _FakeProc.calls[-1]
            assert argv[argv.index("--") + 1:] == [os.path.join(L.path, n) for n in ("sub1", "beta.md")]
            L.marks.clear()
            L.set_cursor(0)
            key(Qt.Key_F7)
            argv = _FakeProc.calls[-1]
            assert argv[argv.index("--") + 1:] == [L.path]
            # 長すぎるときは一時 JSON で渡す
            old = core.CONTEXT_ARGS_MAX
            core.CONTEXT_ARGS_MAX = 10
            try:
                L.set_cursor(L._row_of("alpha.txt"))
                key(Qt.Key_F7)
            finally:
                core.CONTEXT_ARGS_MAX = old
            argv = _FakeProc.calls[-1]
            jpath = argv[argv.index("--json") + 1]
            parsed = core.parse_context_args(["--json", jpath])
            assert parsed["paths"] == [os.path.join(L.path, "alpha.txt")]
            os.remove(jpath)
            # 終わったら読み直す(エラーは出さない)
            _wait(w)
            assert "出せませんでした" not in w.log_view.toPlainText()
            # 書庫の中では起こさない
            count = len(_FakeProc.calls)
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            key(Qt.Key_F7)
            assert len(_FakeProc.calls) == count
            assert "書庫内ではコンテキストメニューは使えません" in w.log_view.toPlainText()
            key(Qt.Key_Backspace)
            _wait(w)
        finally:
            filer.subprocess.Popen = real_popen

        # 頭文字ジャンプ: Ctrl+S で sub1 → sub2 → sub1(巡回)。書庫の中でも効く
        L.set_cursor(0)
        key(Qt.Key_S, Qt.ControlModifier)
        assert _cursor_name(L) == "sub1"
        key(Qt.Key_S, Qt.ControlModifier)
        assert _cursor_name(L) == "sub2"
        key(Qt.Key_S, Qt.ControlModifier)
        assert _cursor_name(L) == "sub1"
        key(Qt.Key_P, Qt.ControlModifier)
        assert _cursor_name(L) == "pack.zip"                  # 上の書庫のテストで作ったもの
        key(Qt.Key_P, Qt.ControlModifier)
        assert _cursor_name(L) == "photo.JPG"                 # 大文字小文字は無視
        key(Qt.Key_Z, Qt.ControlModifier)                      # 一致なし: 動かない
        assert _cursor_name(L) == "photo.JPG"
        key(Qt.Key_G, Qt.ControlModifier)                      # filer.keys で外したキーは動かない
        assert _cursor_name(L) == "photo.JPG"
        key(Qt.Key_S, Qt.ControlModifier | Qt.ShiftModifier)  # Ctrl+Shift は対象外
        assert _cursor_name(L) == "photo.JPG"
        key(Qt.Key_F)                                          # サーチ中はサーチ側が先
        key(Qt.Key_B, Qt.ControlModifier)
        assert _cursor_name(L) == "photo.JPG"
        key(Qt.Key_Escape)

        # 範囲マーク: Ctrl+Space は直上のマークから、Shift+Ctrl+Space は直下のマークから
        L.marks.clear()
        L.marks.add("sub2")
        L.set_cursor(L._row_of("gamma.txt"))
        key(Qt.Key_Space, Qt.ControlModifier)
        assert L.marks == {"sub2", "alpha.txt", "beta.md", "gamma.txt"}, L.marks
        assert _cursor_name(L) == "gamma.txt"                  # カーソルは動かない
        L.marks.clear()
        L.marks.add("photo.JPG")
        L.set_cursor(L._row_of("alpha.txt"))
        key(Qt.Key_Space, Qt.ControlModifier | Qt.ShiftModifier)
        assert L.marks == {"alpha.txt", "beta.md", "gamma.txt", "pack.zip", "photo.JPG"}, L.marks
        L.marks.clear()
        L.set_cursor(L._row_of("alpha.txt"))
        key(Qt.Key_Space, Qt.ControlModifier)                  # マークが無ければ何もしない
        assert L.marks == set()
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------
# 簡易テキストビュア
# ---------------------------------------------------------------
def test_viewer_encoding_and_index_logic():
    import filer_viewer as fv
    from array import array
    assert fv.detect_encoding(b"\xef\xbb\xbfabc") == ("utf-8", 3)
    assert fv.detect_encoding(b"\xff\xfea\x00") == ("utf-16-le", 2)
    assert fv.detect_encoding(b"\xfe\xff\x00a") == ("utf-16-be", 2)
    assert fv.detect_encoding("日本語".encode("utf-8")) == ("utf-8", 0)
    assert fv.detect_encoding("日本語".encode("utf-8")[:-1]) == ("utf-8", 0)  # 末尾で切れても UTF-8
    assert fv.detect_encoding("日本語".encode("cp932")) == ("cp932", 0)
    assert fv.detect_encoding(b"plain ascii") == ("utf-8", 0)
    assert fv.bom_length(b"\xef\xbb\xbfx", "utf-8") == 3 and fv.bom_length(b"\xef\xbb\xbfx", "cp932") == 0
    assert fv.newline_of("utf-16-le") == b"\n\x00" and fv.newline_of("cp932") == b"\n"
    # UTF-16LE で「文字の途中に見える \n\x00」(U+0A00 系の上位バイトなど)を取り違えない
    data = "a\u0a0ab\nc".encode("utf-16-le")
    out = array("Q", [0])
    fv.scan_line_starts(data, 0, b"\n\x00", 2, out, len(data))
    starts = list(out)
    assert len(starts) == 2 and data[starts[1]:].decode("utf-16-le") == "c", starts
    assert fv.decode_line(b"abc\r\n", "utf-8") == "abc"
    assert fv.decode_line(b"\xff\xfe", "utf-8") == "\ufffd\ufffd"     # 読めないバイトは置換文字
    assert fv.find_in_line("Hello World", "WORLD") == 6 and fv.find_in_line("x", "y") is None
    # まとめ読みの検索(ブロックの境目をまたいでも、行と桁が合うこと)
    with tempfile.TemporaryDirectory() as tmp:
        for enc, start in (("utf-8", 0), ("utf-16-le", 2)):
            path = os.path.join(tmp, f"s_{enc}.txt")
            lines = [f"row {i} 本文" for i in range(10000)]
            lines[3] = "xx Needle here"
            lines[9001] = "\tthe NEEDLE"
            body = "\r\n".join(lines).encode(enc)
            with open(path, "wb") as f:
                f.write((b"\xff\xfe" if start else b"") + body)
            index = fv.LineIndex(path, enc, start)
            deadline = time.monotonic() + 10
            while not index.done and time.monotonic() < deadline:
                time.sleep(0.01)
            reader = fv.LineReader(index)
            never = threading.Event()
            try:
                assert index.count() == 10000
                assert fv.search_lines(reader, index, "needle", 0, False, never) == \
                    ("hit", (3, 3, 6))
                assert fv.search_lines(reader, index, "needle", 4, False, never) == \
                    ("hit", (9001, 5, 6))
                assert fv.search_lines(reader, index, "needle", 9000, True, never) == \
                    ("hit", (3, 3, 6))
                assert fv.search_lines(reader, index, "needle", 9999, True, never) == \
                    ("hit", (9001, 5, 6))
                assert fv.search_lines(reader, index, "nothing", 0, False, never)[0] == "none"
                assert fv.search_lines(reader, index, "row 9999", 9999, True, never)[1][0] == 9999
                stop = threading.Event()
                stop.set()
                assert fv.search_lines(reader, index, "nothing", 0, False, stop)[0] == "cancel"
            finally:
                reader.close()


def _wait_viewer(viewer, cond, timeout=20.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        QTest.qWait(20)
    raise AssertionError("ビュアが待ち時間内に終わらない")


def test_viewer_window_big_file():
    import filer_viewer as fv
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp, "big.log")
        with open(big, "w", encoding="utf-8", newline="\n") as f:
            for i in range(500000):
                f.write(f"line {i} 日本語\tタブ\n")
        edited = []
        t0 = time.monotonic()
        v = fv.TextViewer(str(big), editor_callback=edited.append)
        v.resize(800, 600)
        v.show()
        v.area.viewport().grab()                       # 索引を作っている最中でも描ける
        assert time.monotonic() - t0 < 2.0
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(v.area, k, mods)  # noqa: E731
        try:
            _check_big_viewer(v, key, big, edited)
        finally:
            v.close()                                  # 失敗しても掴んだファイルを手放す
        _check_viewer_encodings(tmp)


def _check_big_viewer(v, key, big, edited):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    if True:
        _wait_viewer(v, lambda: v.index.done)
        assert v.line_count() == 500000 and v.encoding == "utf-8"
        assert v.raw_line(0) == "line 0 日本語\tタブ" and v.raw_line(499999).startswith("line 499999 ")
        # 題名は 0.1 秒ごとに更新する(索引作りのスレッドから Qt を触らないため)
        _wait_viewer(v, lambda: "500,000行" in v.windowTitle())
        assert "UTF-8" in v.windowTitle()
        # 末尾・先頭・行ジャンプ
        key(Qt.Key_End)
        assert v.top_line() == 500000 - v.area.page_lines()
        key(Qt.Key_Home)
        assert v.top_line() == 0
        key(Qt.Key_PageDown)
        assert v.top_line() == v.area.page_lines()
        key(Qt.Key_Right, Qt.ControlModifier)
        assert v.top_line() == 500000 - v.area.page_lines()
        v.ask_line = lambda: 250001
        key(Qt.Key_J)
        assert v.top_line() == 250000
        # 検索(別スレッド)。ヒット行へ移って強調
        v.ask_word = lambda title: "LINE 400000 "     # 大文字小文字は無視
        key(Qt.Key_F)
        _wait_viewer(v, lambda: v.highlight is not None)
        assert v.highlight == (400000, 0, len("LINE 400000 "))
        assert v.top_line() <= 400000 < v.top_line() + v.area.page_lines()
        v.area.viewport().grab()
        # 上方次検索(Shift+Space): 上には無い
        key(Qt.Key_Space, Qt.ShiftModifier)
        _wait_viewer(v, lambda: not v.searching())
        QTest.qWait(150)
        assert "見つかりません" in v.status.text()
        # 上方検索で別の語
        v.ask_word = lambda title: "line 7 "
        key(Qt.Key_F, Qt.ShiftModifier)
        _wait_viewer(v, lambda: v.highlight is not None and v.highlight[0] == 7)
        # タブ幅・行番号・字の大きさ・エディタ・見えている行
        key(Qt.Key_8)
        assert v.tab_width == 8 and v.display_line(0) == "line 0 日本語\tタブ".expandtabs(8)
        key(Qt.Key_N, Qt.ShiftModifier)
        assert v.show_numbers
        size = v.area.font().pointSize()
        key(Qt.Key_Down, Qt.ControlModifier)
        assert v.area.font().pointSize() == size + 1
        key(Qt.Key_Insert)
        assert v.area.font().pointSize() == size
        key(Qt.Key_E)
        assert edited == [str(big)]
        assert v.visible_text().splitlines()[0] == v.raw_line(v.top_line())
        # 閉じる(Q)。閉じたら大きさを知らせる
        closed = []
        v.closed.connect(closed.append)
        key(Qt.Key_Q)
        assert closed and len(closed[0]) == 4


def _check_viewer_encodings(tmp):
    import filer_viewer as fv
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    if True:
        # 文字コード: cp932 は自動判定、Shift+O で UTF-8、Shift+S で戻す。UTF-16 BOM
        sjis = Path(tmp, "sjis.txt")
        sjis.write_bytes("一行目\r\n二行目\r\n".encode("cp932"))
        v = fv.TextViewer(str(sjis))
        _wait_viewer(v, lambda: v.index.done)
        assert v.encoding == "cp932" and [v.raw_line(i) for i in range(v.line_count())] == \
            ["一行目", "二行目"]
        QTest.keyClick(v.area, Qt.Key_O, Qt.ShiftModifier)
        _wait_viewer(v, lambda: v.index.done)
        assert v.encoding == "utf-8" and "\ufffd" in v.raw_line(0)
        QTest.keyClick(v.area, Qt.Key_S, Qt.ShiftModifier)
        _wait_viewer(v, lambda: v.index.done)
        assert v.encoding == "cp932" and v.raw_line(1) == "二行目"
        for letter, enc in (("U", "utf-16-le"), ("I", "utf-16-be"), ("E", "euc_jp"),
                            ("J", "iso2022_jp")):
            QTest.keyClick(v.area, getattr(Qt, f"Key_{letter}"), Qt.ShiftModifier)
            assert v.encoding == enc, (letter, v.encoding)
            _wait_viewer(v, lambda: v.index.done)
        QTest.keyClick(v.area, Qt.Key_Escape)        # 検索中でなければ Esc で閉じる
        assert not v.isVisible()
        u16 = Path(tmp, "u16.txt")
        u16.write_bytes(b"\xfe\xff" + "あ\nい".encode("utf-16-be"))
        v = fv.TextViewer(str(u16))
        _wait_viewer(v, lambda: v.index.done)
        assert v.encoding == "utf-16-be" and [v.raw_line(i) for i in range(v.line_count())] == \
            ["あ", "い"]
        v.close()
        empty = Path(tmp, "empty.txt")
        empty.write_bytes(b"")
        v = fv.TextViewer(str(empty))
        _wait_viewer(v, lambda: v.index.done)
        assert v.line_count() == 0
        v.area.viewport().grab()
        v.close()


def test_enter_viewer_routing():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    assert core.opens_in_viewer("a.LOG", core.normalize_viewer_extensions(None))
    assert not core.opens_in_viewer("a.exe", core.normalize_viewer_extensions(None))
    assert not core.opens_in_viewer("noext", core.normalize_viewer_extensions(None))
    assert core.normalize_viewer_extensions(".Foo, bar") == {"foo", "bar"}
    import settings as settings_module     # 既定値の二重持ちが食い違っていないこと
    assert set(settings_module.DEFAULT_SETTINGS["filer"]["viewer_extensions"]) == \
        set(core.DEFAULT_VIEWER_EXTENSIONS)
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        Path(left, "app.log").write_text("log line\n", encoding="utf-8")
        Path(left, "tool.exe").write_bytes(b"MZ")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        opened = []
        w._startfile = opened.append
        L.set_cursor(L._row_of("tool.exe"))
        QTest.keyClick(L.view, Qt.Key_Return)
        assert opened == [os.path.join(L.path, "tool.exe")] and not w._viewers
        L.set_cursor(L._row_of("app.log"))
        QTest.keyClick(L.view, Qt.Key_Return)
        assert len(w._viewers) == 1 and opened == [os.path.join(L.path, "tool.exe")]
        viewer = w._viewers[0]
        assert viewer.area.font().pointSize() == L.pane_font.pointSize()  # 字の大きさは窓から
        QTest.keyClick(L.view, Qt.Key_Return, Qt.ControlModifier)          # Ctrl+Enter は関連付け
        assert opened[-1] == os.path.join(L.path, "app.log")
        QTest.keyClick(viewer.area, Qt.Key_Return)                         # Enter で閉じる
        assert not w._viewers
        stored = json.loads(settings_path.read_text(encoding="utf-8"))["filer"]
        assert len(stored["viewer_geometry"]) == 4
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------
# ZIP 圧縮(P)
# ---------------------------------------------------------------
def test_zip_plan_and_create():
    import zipfile
    import filer_archive as fa
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "src")
        (src / "dir" / "empty").mkdir(parents=True)
        (src / "dir" / "中.txt").write_bytes(b"naka")
        (src / "a.txt").write_bytes(b"a" * 1000)
        hidden = src / "dir" / "hidden.txt"
        hidden.write_bytes(b"h")
        _set_attrs(hidden, 0x02)
        old = time.mktime((2020, 5, 6, 7, 8, 10, 0, 0, -1))
        os.utime(src / "a.txt", (old, old))
        dest = src / "out.zip"
        plan = core.zip_plan(str(src), ["a.txt", "dir", "out.zip"], exclude=str(dest))
        arcs = [arc for _p, arc, _d in plan]
        assert arcs == ["a.txt", "dir/", "dir/empty/", "dir/hidden.txt", "dir/中.txt"], arcs
        assert core.default_zip_name(str(src), [core.Entry("a.txt", False)]) == "a.zip"
        assert core.default_zip_name(str(src), [core.Entry("dir.v2", True)]) == "dir.v2.zip"
        assert core.default_zip_name(str(src), [core.Entry("a", False), core.Entry("b", False)]) == "src.zip"
        assert core.default_zip_name("C:\\", [core.Entry("a", False), core.Entry("b", False)]) == "C.zip"

        result = fa.create_zip(str(src), ["a.txt", "dir"], str(dest))
        assert not result.error and not result.cancelled and result.files == 3 and result.folders == 2
        assert result.source_bytes == 1000 + 4 + 1 and result.zip_bytes == dest.stat().st_size
        assert not Path(str(dest) + ".filer-part").exists()
        with zipfile.ZipFile(dest) as z:
            infos = {i.filename: i for i in z.infolist()}
            assert set(infos) == {"a.txt", "dir/", "dir/empty/", "dir/hidden.txt", "dir/中.txt"}
            assert infos["dir/中.txt"].flag_bits & 0x800                   # UTF-8 の印
            assert infos["a.txt"].compress_type == zipfile.ZIP_DEFLATED
            assert infos["a.txt"].date_time[:5] == (2020, 5, 6, 7, 8)       # 更新日時を保つ
            assert z.read("dir/中.txt") == b"naka"
        # 自分自身(作る ZIP)をいまのフォルダの対象に含めても入らない
        result = fa.create_zip(str(src), ["a.txt", "out.zip"], str(dest))
        with zipfile.ZipFile(dest) as z:
            assert z.namelist() == ["a.txt"]
        # 中止: 作りかけを残さない
        cancel = threading.Event()
        cancel.set()
        dest2 = src / "cancel.zip"
        result = fa.create_zip(str(src), ["a.txt"], str(dest2), cancel=cancel)
        assert result.cancelled and not dest2.exists() and not Path(str(dest2) + ".filer-part").exists()
        # 失敗(書けない場所): 何も残さない
        bad = Path(tmp, "nope", "x.zip")
        result = fa.create_zip(str(src), ["a.txt"], str(bad))
        assert result.error and not bad.exists()
        _set_attrs(hidden, 0x80)


def test_window_zip_create():
    import zipfile
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        asked = []
        w.ask_text = lambda title, label, text="", select=None: (asked.append(text), text)[1]
        L.marks.update({"alpha.txt", "sub1"})
        QTest.keyClick(L.view, Qt.Key_P)
        _wait_extract(w)
        _wait(w)
        assert asked == ["L.zip"]
        made = right / "L.zip"
        with zipfile.ZipFile(made) as z:
            assert set(z.namelist()) == {"alpha.txt", "sub1/", "sub1/deep/"}
        assert _cursor_name(R) == "L.zip" and "圧縮: 1ファイル 2フォルダ" in w.log_view.toPlainText()
        # 既にあれば上書きを確かめる(いいえなら作らない)
        L.marks.clear()
        L.set_cursor(L._row_of("beta.md"))
        w.ask_text = lambda *a, **k: "L.zip"
        w.confirm = lambda *a, **k: False
        before = made.stat().st_size
        QTest.keyClick(L.view, Qt.Key_P)
        assert w._extracting is None and made.stat().st_size == before
        w.confirm = lambda *a, **k: True
        QTest.keyClick(L.view, Qt.Key_P)
        _wait_extract(w)
        with zipfile.ZipFile(made) as z:
            assert z.namelist() == ["beta.md"]
        # 書庫の中では作らない(自窓・反対窓とも)
        zpath = left / "pack.zip"
        _make_zip(str(zpath))
        R.load(str(zpath))
        _wait(w)
        QTest.keyClick(L.view, Qt.Key_P)
        assert "書庫内へは書き込めません" in w.log_view.toPlainText()
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------
# WinMerge(Alt+W)
# ---------------------------------------------------------------
def test_winmerge_rules_and_find():
    pair = core.winmerge_pair
    assert pair(["a"], ["b"]) == [("left", "a"), ("right", "b")]
    assert pair(["a", "c"], []) == [("left", "a"), ("left", "c")]
    assert pair([], ["x", "y"]) == [("right", "x"), ("right", "y")]
    for left, right in (([], []), (["a"], []), ([], ["b"]), (["a", "b", "c"], []),
                        (["a", "b"], ["c"]), (["a"], ["b", "c"])):
        assert pair(left, right) is None, (left, right)
    env = {"ProgramFiles": r"C:\PF", "ProgramFiles(x86)": r"C:\PF86", "LOCALAPPDATA": r"C:\L"}
    exists = lambda p: p in present  # noqa: E731
    present = {r"C:\PF86\WinMerge\WinMergeU.exe", r"D:\reg\WinMergeU.exe", r"E:\set\WinMergeU.exe"}
    assert core.find_winmerge("", env, exists, lambda: []) == r"C:\PF86\WinMerge\WinMergeU.exe"
    assert core.find_winmerge("", env, exists, lambda: [r"D:\reg\WinMergeU.exe"]) == r"D:\reg\WinMergeU.exe"
    assert core.find_winmerge(r"E:\set\WinMergeU.exe", env, exists,
                              lambda: [r"D:\reg\WinMergeU.exe"]) == r"E:\set\WinMergeU.exe"
    present = set()
    assert core.find_winmerge("", env, exists, lambda: []) is None
    assert core.DEFAULT_KEYMAP["4087"] == "winmerge" and core.DEFAULT_KEYMAP["0080"] == "zip_create"


def test_window_winmerge():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        exe = r"C:\WM dir\WinMergeU.exe"
        real_find, real_popen = filer.core.find_winmerge, filer.subprocess.Popen
        calls = []
        filer.core.find_winmerge = lambda *a, **k: exe
        filer.subprocess.Popen = lambda argv, **kw: calls.append(argv)
        alt_w = lambda: QTest.keyClick(w.active.view, Qt.Key_W, Qt.AltModifier)  # noqa: E731
        try:
            alt_w()                                                     # マークなし
            assert not calls and core.WINMERGE_HINT in w.log_view.toPlainText()
            L.marks.add("alpha.txt")
            alt_w()                                                     # 1件と0件
            assert not calls
            R.marks.add("r.txt")
            alt_w()
            assert calls[-1] == [exe, "/dl", str(left / "alpha.txt"), "/dr", str(right / "r.txt"),
                                 str(left / "alpha.txt"), str(right / "r.txt")]
            R.marks.clear()
            L.marks.add("beta.md")
            alt_w()                                                     # 片側に2件
            assert calls[-1][-2:] == [str(left / "alpha.txt"), str(left / "beta.md")]
            L.marks.add("gamma.txt")
            count = len(calls)
            alt_w()                                                     # 3件は断る
            assert len(calls) == count
            # 書庫の中は取り出してから(表示名は書庫の中のパス)
            L.marks.clear()
            L.marks.add("alpha.txt")
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            R.load(str(zpath))
            _wait(w)
            R.marks.add("readme.txt")
            alt_w()
            _wait_extract(w)
            argv = calls[-1]
            assert argv[4] == str(zpath / "readme.txt") and argv[6].startswith(_TEST_TEMP)
            assert Path(argv[6]).read_bytes() == b"hello"
            # 見つからなければ知らせるだけ
            filer.core.find_winmerge = lambda *a, **k: None
            count = len(calls)
            alt_w()
            assert len(calls) == count and "WinMerge が見つかりません" in w.log_view.toPlainText()
        finally:
            filer.core.find_winmerge, filer.subprocess.Popen = real_find, real_popen
        w.close()
        w.deleteLater()


# ---------------------------------------------------------------
# 画面の構成(上の2行・下の行・ステータスバー・Fキーの段)
# ---------------------------------------------------------------
def test_screen_text_helpers():
    gb = 1 << 30
    assert core.format_gb(510770802688) == "475.69 GB" and core.format_gb(0) == "0.00 GB"
    assert core.footer_text(58, 0, 510770802688, 107780108288) == \
        "58 Dirs   0 File   475.69 GB ( 100.38 GB)"                   # あふｗの画面と同じ
    assert core.footer_text(7, 56, 510770802688, 107780108288).startswith("7 Dirs   56 Files")
    assert core.footer_text(1, 1) == "1 Dirs   1 File"                  # 容量が分からなければ出さない
    assert core.footer_text(0, 2, 2 * gb, gb, mark_count=3, mark_bytes=12345) == \
        "0 Dirs   2 Files   2.00 GB ( 1.00 GB)   Mark 3 (12,345)"
    entries = [core.PARENT_ENTRY, F("d", is_dir=True), F("a"), F("b")]
    assert core.count_dirs_files(entries) == (1, 2)                     # ".." は数えない
    assert core.attr_letters(F("d", is_dir=True)) == "D"
    assert core.attr_letters(F("x", attrs=READONLY | HIDDEN | SYSTEM | 0x20)) == "RHSA"
    stamp = time.mktime((2026, 4, 9, 19, 52, 53, 0, 0, -1))
    assert core.status_fields(F("7-Zip", is_dir=True, mtime=stamp)) == \
        ("7-Zip", "<DIR>", "26/04/09 19:52:53", "D")
    assert core.status_fields(F("a.txt", size=1234567, mtime=stamp))[1] == "1,234,567"
    assert core.status_fields(core.PARENT_ENTRY) == ("..", "", "", "")
    assert core.status_fields(None) == ("", "", "", "")
    assert core.drive_root(r"C:\Program Files\x") == "C:\\"
    assert core.drive_root(r"\\srv\share\dir") == "\\\\srv\\share\\"
    assert core.drive_root("") is None
    assert core.unc_label("\\\\srv\\share\\") == "srv\\share" and core.unc_label("C:\\") is None


def test_fkey_labels():
    km = core.build_keymap({"1121": "reload", "0112": ""})
    labels = core.fkey_labels(km)
    assert len(labels) == 12
    assert labels[6] == "Menu" and labels[8] == "PowerShell" and labels[9] == "Explorer"
    assert labels[0] == "" and labels[11] == ""                          # 割り当ての無いキーは空
    shift = core.fkey_labels(km, core.SHIFT)
    assert shift[9] == "Reload" and shift[6] == ""                       # Shift の割り当てに切り替わる
    assert core.fkey_labels(km, core.ALT)[3] == "Quit"                   # Alt+F4
    over = core.fkey_labels(km, 0, {"F7": "右クリ", "Shift+F10": "x"})
    assert over[6] == "右クリ" and over[9] == "Explorer"
    assert core.fkey_labels(km, core.SHIFT, {"Shift+F10": "再読込"})[9] == "再読込"
    assert core.fkey_labels(km, 0, {"0120": "PS"})[8] == "PS"            # あふｗの4桁でも
    assert core.fkey_command(km, 0, 10) == "open_explorer"
    assert core.fkey_command(km, core.SHIFT, 10) == "reload"
    assert core.fkey_command(km, 0, 1) is None


def test_window_screen_layout():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        reads = []
        real_label, real_space = filer.FilerWindow._read_drive_label, filer.FilerWindow._read_drive_space
        filer.FilerWindow._read_drive_label = staticmethod(
            lambda root: (reads.append(("label", root)), "TESTVOL")[1])
        filer.FilerWindow._read_drive_space = staticmethod(
            lambda root: (reads.append(("space", root)), (510770802688, 107780108288))[1])
        real_popen = filer.subprocess.Popen
        try:
            w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                             "keys": {"1121": "reload"}}}, str(settings_path))
            w.resize(1000, 600)
            w.show()
            w.start()
            _wait(w)
            QTest.qWait(100)                   # ラベル・容量の読み取り(別スレッド)を待つ
            L, R = w.left, w.right
            # 上の2行: パスとマスク / ドライブのラベル
            assert L.header.text() == f"{L.path}\\ *.*", L.header.text()
            assert L.header2.text() == "TESTVOL"
            # ラベルは同じドライブなら1回だけ読む(左右とも同じドライブ)
            assert [r for r in reads if r[0] == "label"] == [("label", core.drive_root(str(left)))]
            # 下の行: あふｗの書式
            assert L.footer.text() == "2 Dirs   4 Files   475.69 GB ( 100.38 GB)", L.footer.text()
            assert R.footer.text().startswith("0 Dirs   1 File   ")
            L.marks.update({"alpha.txt", "beta.md"})
            L.update_footer()
            assert L.footer.text().endswith("Mark 2 (30)")
            L.marks.clear()
            # ステータスバー: Ready. / 名前 / <DIR> かサイズ / 日時 / 属性。カーソルで変わる
            texts = lambda: [label.text() for label in w.status_labels]  # noqa: E731
            assert texts()[:2] == ["Ready.", ".."]
            QTest.keyClick(L.view, Qt.Key_Down)
            t = texts()
            assert t[1] == "sub1" and t[2] == "<DIR>" and t[4] == "D" and t[3].count("/") == 2
            L.set_cursor(L._row_of("photo.JPG"))
            assert texts()[1:3] == ["photo.JPG", "100"]
            QTest.keyClick(L.view, Qt.Key_Tab)                 # 窓を替えれば反対窓のカーソル
            assert texts()[1] == ".."
            QTest.keyClick(R.view, Qt.Key_Tab)
            # Fキーの段: 割り当てから作る。Shift を押している間は Shift の割り当て
            buttons = [b.text() for b in w.fkey_buttons]
            assert buttons[6] == "Menu" and buttons[8] == "PowerShell" and buttons[9] == "Explorer"
            QApplication.sendEvent(L.view, QKeyEvent(QEvent.KeyPress, Qt.Key_Shift, Qt.ShiftModifier))
            assert w.fkey_buttons[9].text() == "Reload" and w.fkey_buttons[6].text() == ""
            QApplication.sendEvent(L.view, QKeyEvent(QEvent.KeyRelease, Qt.Key_Shift, Qt.ShiftModifier))
            assert w.fkey_buttons[9].text() == "Explorer"
            assert all(b.focusPolicy() == Qt.NoFocus for b in w.fkey_buttons)
            # 区画をクリックすると、そのキーと同じコマンド(F10 = エクスプローラ)
            calls = []
            filer.subprocess.Popen = lambda argv, **kw: calls.append(argv)
            QTest.mouseClick(w.fkey_buttons[9], Qt.LeftButton)
            assert calls and calls[-1][0] == "explorer.exe"
            count = len(calls)
            QTest.mouseClick(w.fkey_buttons[0], Qt.LeftButton)  # 空の区画は何もしない
            assert len(calls) == count
            # 処理中はステータスバーの左端に出る
            w.set_status_state("複写中 3/10")
            assert texts()[0] == "複写中 3/10"
            w.set_status_state("")
            assert texts()[0] == "Ready."
            # 狭くしても窓の最小幅を押し広げない(深いパスでも)
            deep = left / ("x" * 60) / ("y" * 60)
            deep.mkdir(parents=True)
            L.load(str(deep))
            _wait(w)
            assert w.minimumSizeHint().width() < 600, w.minimumSizeHint().width()
            # 書庫の中は容量を出さない(件数だけ)。ラベルは書庫のあるドライブ
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            QTest.qWait(50)
            assert "GB" not in L.footer.text() and L.header2.text() == "TESTVOL"
            # Fキーの段は settings で隠せる
            w.close()
            w.deleteLater()
            w2 = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                              "show_fkey_bar": False}}, None)
            assert w2.fkey_bar.isHidden()
            w2.deleteLater()
        finally:
            filer.FilerWindow._read_drive_label = real_label
            filer.FilerWindow._read_drive_space = real_space
            filer.subprocess.Popen = real_popen


# ---------------------------------------------------------------
# R: ファイル情報の編集 / 一括変更、F5
# ---------------------------------------------------------------
def test_file_info_logic():
    assert core.caret_position("1778326141-Wk.webp", False) == len("1778326141-Wk")
    assert core.caret_position("noext", False) == 5
    assert core.caret_position(".gitignore", False) == len(".gitignore")
    assert core.caret_position("dir.v2", True) == len("dir.v2")
    assert core.convert_case("Photo.WebP", "all", "lower") == "photo.webp"
    assert core.convert_case("Photo.WebP", "all", "upper") == "PHOTO.WEBP"
    assert core.convert_case("Photo.WebP", "all", "swap") == "pHOTO.wEBp"
    assert core.convert_case("Photo.WebP", "ext", "upper") == "Photo.WEBP"
    assert core.convert_case("Photo.WebP", "ext", "lower") == "Photo.webp"
    assert core.convert_case("Dir.Ext", "ext", "upper", is_dir=True) == "Dir.Ext"   # フォルダは不変
    assert core.compose_attrs(0x20, {"readonly": True}) == 0x21
    assert core.compose_attrs(0x21, {"readonly": False, "archive": False}) == core.FILE_ATTRIBUTE_NORMAL
    assert core.compose_attrs(0x10 | 0x02, {"system": True}) == 0x06       # フォルダの印は渡さない
    assert core.attrs_to_flags(0x23) == {"readonly": True, "system": False, "hidden": True,
                                         "archive": True}
    stamp, problem = core.make_timestamp(2026, 5, 30, 0, 9, 36)
    assert problem is None and time.localtime(stamp)[:6] == (2026, 5, 30, 0, 9, 36)
    assert core.make_timestamp(2026, 2, 30, 0, 0, 0)[0] is None              # 存在しない日付
    assert "存在しない" in core.make_timestamp(2026, 2, 30, 0, 0, 0)[1]
    assert core.make_timestamp(2026, 13, 1, 0, 0, 0)[0] is None
    assert core.make_timestamp(1970, 1, 2, 0, 0, 0)[0] is None
    base = time.mktime((2020, 7, 8, 9, 10, 11, 0, 0, -1))
    stamp, problem = core.make_timestamp(0, 0, 0, 12, 0, 0, base=base)     # 年月日 0 は時刻だけ
    assert problem is None and time.localtime(stamp)[:6] == (2020, 7, 8, 12, 0, 0)
    assert core.make_timestamp(0, 0, 0, 12, 0, 0)[0] is None
    e = core.Entry("a.txt", False, 1, base, 0x20)
    assert core.plan_file_info(e, "a.txt", base, {"archive": True}) == {}  # 何も変わらない
    assert core.plan_file_info(e, "A.TXT") == {"old": "a.txt", "new": "A.TXT"}
    plan = core.plan_file_info(e, None, base + 60, {"readonly": True})
    assert plan == {"old": "a.txt", "mtime": base + 60, "attrs": 0x21}
    entries = [core.PARENT_ENTRY, core.Entry("A.Txt", False, 1, base, 0x20),
               core.Entry("b.txt", False, 1, base, 0x20), core.Entry("Dir", True, 0, base, 0x10)]
    plans, problems = core.plan_bulk_info(entries, case_all="lower", case_ext="upper",
                                          stamp_fields=(0, 0, 0, 1, 2, 3),
                                          attr_changes={"hidden": True})
    assert not problems and [p["old"] for p in plans] == ["A.Txt", "b.txt", "Dir"]
    assert [p.get("new") for p in plans] == ["a.TXT", "b.TXT", "dir"]
    assert all(time.localtime(p["mtime"])[3:6] == (1, 2, 3) for p in plans)
    assert all(p["attrs"] & 0x02 for p in plans)
    assert core.plan_bulk_info(entries, stamp_fields=(2026, 2, 30, 0, 0, 0))[1]
    assert core.rename_collision(["a.txt", "B.txt"], {"a.txt": "A.TXT"}) is None
    assert core.rename_collision(["a.txt", "B.txt"], {"a.txt": "b.TXT"}) == "b.TXT"
    assert core.rename_collision(["A.txt", "a.TXT"], {"A.txt": "a.txt", "a.TXT": "a.txt"})
    assert core.rename_collision(["x", "y"], {"x": "y", "y": "x"}) is None    # 入れ替えは重ならない
    assert "日時" in core.describe_file_info({"old": "a", "mtime": base})
    assert core.describe_file_info({"old": "a", "new": "b", "attrs": 0x03}) == "a → b  属性 RH"
    assert core.DEFAULT_KEYMAP["0116"] == "reload_keep"
    assert core.fkey_labels(core.DEFAULT_KEYMAP)[4] == "Reload"


def _stat_attrs(path):
    return os.stat(path).st_file_attributes


def test_file_info_focus_and_checkbox_look():
    """実機で起きた2件: ファイル名欄がフォーカスで全選択される / 濃紺の地でチェックの
    四角が見えない。offscreen でも窓を表示してフォーカスの出来事を起こして確かめる
    (実画面には出ない)。Tab で戻ってきたときの出来事は QFocusEvent を直接送って代用。"""
    import filer_fileinfo
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QColor, QFocusEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "マスターキー_設定メモ.md").write_bytes(b"x")
        entry = core.Entry("マスターキー_設定メモ.md", False, 1, time.time(), 0x20)
        d = filer_fileinfo.FileInfoDialog(None, tmp, [entry])
        d.show()
        QTest.qWait(50)
        caret = len("マスターキー_設定メモ")
        assert d.name_edit.selectedText() == "" and d.name_edit.cursorPosition() == caret
        # Tab でフォーカスが来たとき(QLineEdit は既定ではここで全選択する)も全選択しない
        d.name_edit.clearFocus()
        QApplication.sendEvent(d.name_edit, QFocusEvent(QEvent.FocusIn, Qt.TabFocusReason))
        assert d.name_edit.selectedText() == "" and d.name_edit.cursorPosition() == caret
        # 自分で動かした位置は、離れて戻っても保つ
        d.name_edit.setCursorPosition(3)
        QApplication.sendEvent(d.name_edit, QFocusEvent(QEvent.FocusOut, Qt.TabFocusReason))
        QApplication.sendEvent(d.name_edit, QFocusEvent(QEvent.FocusIn, Qt.BacktabFocusReason))
        assert d.name_edit.cursorPosition() == 3 and not d.name_edit.hasSelectedText()

        def colors_of(check):
            image = check.grab().toImage()
            return {QColor(image.pixel(x, y)).name()
                    for x in range(image.width()) for y in range(image.height())}
        # 未チェックでも明るい枠が描かれる / ON は水色で塗る
        assert "#c8c8f0" in colors_of(d.attr_checks["readonly"])
        assert "#40d0ff" in colors_of(d.attr_checks["archive"])
        d.close()
        # 一括変更の「変えない」(中間)は灰色の帯で見分けられる
        other = core.Entry("b.txt", False, 1, time.time(), 0x20)
        Path(tmp, "b.txt").write_bytes(b"x")
        bulk = filer_fileinfo.FileInfoDialog(None, tmp, [entry, other])
        bulk.show()
        QTest.qWait(30)
        assert bulk.attr_checks["hidden"].checkState() == Qt.PartiallyChecked
        assert "#a0a0a0" in colors_of(bulk.attr_checks["hidden"])
        bulk.close()


def test_window_file_info():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        for name in ("Image.webp", "Case.txt", "ro.txt", "b1.txt", "b2.txt", "b3.txt"):
            Path(left, name).write_bytes(b"x")
        _set_attrs(left / "ro.txt", 0x01 | 0x20)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        steps = {}
        key = _checked_key(L, steps)

        def run_dialog(dialog):
            # 窓を出さずに操作する。steps["do"] が入力と OK を行う。
            return _run_stub(steps, dialog)
        w.exec_dialog = run_dialog
        try:
            # 1件: 変換ボタン・Alt+頭文字・日時・OK で実際に変わる
            def single(d):
                assert d.name_edit.cursorPosition() == len("Image")
                assert not d.name_edit.hasSelectedText()
                assert d.current_date.text().count("/") == 2
                d.case_buttons[("all", "lower")].click()
                assert d.name_edit.text() == "image.webp"
                d.case_buttons[("ext", "upper")].click()
                assert d.name_edit.text() == "image.WEBP"
                d.case_buttons[("all", "swap")].click()
                assert d.name_edit.text() == "IMAGE.webp"
                before = d.attr_checks["readonly"].isChecked()
                QTest.keyClick(d, Qt.Key_R, Qt.AltModifier)
                assert d.attr_checks["readonly"].isChecked() != before
                QTest.keyClick(d, Qt.Key_H, Qt.AltModifier)
                for box, value in zip(d.stamp_edits, (2021, 2, 3, 4, 5, 6)):
                    box.setValue(value)
                d.try_accept()
            steps["do"] = single
            L.set_cursor(L._row_of("Image.webp"))
            key(Qt.Key_R)
            _wait(w)
            assert steps["result"] == QDialog.Accepted
            assert "IMAGE.webp" in os.listdir(left)
            st = os.stat(left / "IMAGE.webp")
            assert time.localtime(st.st_mtime)[:6] == (2021, 2, 3, 4, 5, 6)
            assert st.st_file_attributes & 0x01 and st.st_file_attributes & 0x02
            assert _cursor_name(L) == "IMAGE.webp"
            assert "IMAGE.webp" in w.log_view.toPlainText()
            _set_attrs(left / "IMAGE.webp", 0x80)

            # 大文字小文字だけの改名
            def case_only(d):
                d.name_edit.setText("CASE.TXT")
                d.try_accept()
            steps["do"] = case_only
            L.set_cursor(L._row_of("Case.txt"))
            key(Qt.Key_R)
            _wait(w)
            assert "CASE.TXT" in os.listdir(left) and "Case.txt" not in os.listdir(left)

            # 読み込み専用のファイルでも名前と日時を変えられ、読み込み専用は残る
            def readonly(d):
                assert d.attr_checks["readonly"].isChecked()
                d.name_edit.setText("ro2.txt")
                d.stamp_edits[0].setValue(2022)
                d.try_accept()
            steps["do"] = readonly
            L.set_cursor(L._row_of("ro.txt"))
            key(Qt.Key_R)
            _wait(w)
            st = os.stat(left / "ro2.txt")
            assert time.localtime(st.st_mtime).tm_year == 2022 and st.st_file_attributes & 0x01

            # 存在しない日付: 閉じずに理由を出し、何も変えない
            def bad_date(d):
                d.name_edit.setText("never.txt")
                d.stamp_edits[1].setValue(2)
                d.stamp_edits[2].setValue(30)
                d.try_accept()
                assert "存在しない" in d.error_text and d.result() != QDialog.Accepted
            steps["do"] = bad_date
            L.set_cursor(L._row_of("b1.txt"))
            key(Qt.Key_R)
            assert not (left / "never.txt").exists() and (left / "b1.txt").exists()

            # 名前が重なる: 閉じずに理由を出す
            def clash(d):
                d.name_edit.setText("B2.TXT")
                d.try_accept()
                assert "同じ名前" in d.error_text and d.result() != QDialog.Accepted
            steps["do"] = clash
            key(Qt.Key_R)

            # 一括変更: 大文字に・時刻だけ(年月日 0)・隠しを ON
            def bulk(d):
                assert d.bulk and not d.name_edit.isEnabled()
                assert "3 件" in d.original_label.text()
                d.case_buttons[("all", "upper")].click()
                assert d.chosen_case("all") == "upper"
                d.case_buttons[("all", "lower")].click()      # 行ごとにどれか1つ
                d.case_buttons[("all", "upper")].click()
                assert d.chosen_case("all") == "upper" and d.chosen_case("ext") is None
                d.stamp_check.setChecked(True)
                for box, value in zip(d.stamp_edits, (0, 0, 0, 12, 34, 56)):
                    box.setValue(value)
                QTest.keyClick(d, Qt.Key_H, Qt.AltModifier)   # 中間 → ON
                assert d.attr_changes() == {"hidden": True}
                d.try_accept()
            steps["do"] = bulk
            L.marks.clear()
            L.marks.update({"b1.txt", "b2.txt", "b3.txt"})
            key(Qt.Key_R)
            _wait(w)
            names = os.listdir(left)
            for n in ("B1.TXT", "B2.TXT", "B3.TXT"):
                assert n in names, names
                st = os.stat(left / n)
                assert time.localtime(st.st_mtime)[3:6] == (12, 34, 56) and st.st_file_attributes & 0x02
                _set_attrs(left / n, 0x80)

            # F5: 最新にするだけでマークは消さない
            L.marks.add("alpha.txt")
            key(Qt.Key_F5)
            _wait(w)
            assert L.marks == {"alpha.txt"}
            L.marks.clear()

            # 書庫の中では開かない
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            steps.clear()
            key(Qt.Key_R)
            assert "dialog" not in steps and "書庫内では編集できません" in w.log_view.toPlainText()
        finally:
            for p in left.iterdir():
                if p.is_file():
                    _set_attrs(p, 0x80)
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# 拡張子別表示色・Z の設定ダイアログ
# ---------------------------------------------------------------
def test_ext_color_logic():
    assert core.normalize_exts("*.Zip .7z  rar,zip;LZH") == ["zip", "7z", "rar", "lzh"]
    assert core.normalize_exts(["TXT", "", ".md"]) == ["txt", "md"]
    rules = core.normalize_ext_colors(None)                      # 書かれていなければ既定
    assert rules[0] == {"color": "#0000ff", "exts": core.DEFAULT_EXT_COLORS[0]["exts"]}
    assert core.normalize_ext_colors([]) == []                   # 空は「色を付けない」
    assert core.normalize_ext_colors([{"color": "red", "exts": "a"},
                                      {"color": "#ABCDEF", "exts": ".Foo bar"},
                                      {"color": "#000000", "exts": []}]) == \
        [{"color": "#abcdef", "exts": ["foo", "bar"]}]
    cmap = core.ext_color_map([{"color": "#111111", "exts": ["a", "b"]},
                               {"color": "#222222", "exts": ["b", "c"]}])
    assert cmap == {"a": "#111111", "b": "#111111", "c": "#222222"}   # 先に一致した行が勝つ
    dmap = core.ext_color_map(core.normalize_ext_colors(None))
    assert core.ext_color_for(F("a.TXT"), dmap) == "#800040"
    assert core.ext_color_for(F("x.lnk"), dmap) == "#808080"
    assert core.ext_color_for(F("x.ZIP"), dmap) == "#0000ff"
    assert core.ext_color_for(F("x.html"), dmap) == "#0080ff"
    assert core.ext_color_for(F("noext"), dmap) is None
    assert core.ext_color_for(F("d.zip", is_dir=True), dmap) is None         # フォルダは付けない
    assert core.ext_color_for(F("desktop.ini", attrs=HIDDEN | SYSTEM), dmap) is None  # 属性色が優先
    assert core.ext_color_for(F("ro.txt", attrs=READONLY), dmap) is None
    assert core.ext_color_for(F("arc.txt", attrs=0x20), dmap) == "#800040"   # アーカイブ属性は関係ない
    rows = [{"color": "#1", "exts": ["a", "b"]}, {"color": "#2", "exts": ["b", "c"]},
            {"color": "#3", "exts": ["a"]}]
    assert core.ext_duplicates(rows, 0) == {"a": [2], "b": [1]}
    assert core.remove_exts_from_others(rows, 0, ["a", "b"]) == \
        [{"color": "#1", "exts": ["a", "b"]}, {"color": "#2", "exts": ["c"]}]  # 空の行は消える
    assert core.ext_colors_to_settings(rules)[0]["exts"] == rules[0]["exts"]
    assert core.DEFAULT_KEYMAP["0090"] == "settings_dialog"


def test_window_ext_colors_and_settings_dialog():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        Path(left, "notes.txt").write_bytes(b"x")
        Path(left, "app.lnk").write_bytes(b"x")
        Path(left, "desktop.ini").write_bytes(b"x")
        _set_attrs(left / "desktop.ini", 0x02 | 0x04)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text(json.dumps({"other": 1}), encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        try:
            entry = lambda name: L.entries[L._row_of(name)]  # noqa: E731
            colors = filer.DEFAULT_COLORS
            # 拡張子の列だけに色。名前の列は種類の色のまま
            assert L.text_color(entry("notes.txt"), filer.COL_EXT) == "#800040"
            assert L.text_color(entry("notes.txt"), filer.COL_NAME) == colors["text"]
            assert L.text_color(entry("app.lnk"), filer.COL_EXT) == "#808080"
            assert L.text_color(entry("photo.JPG"), filer.COL_EXT) == "#804000"
            assert L.text_color(entry("sub1"), filer.COL_EXT) == colors["directory"]
            # 隠し+システムの行は行全体の属性色(拡張子の色は付けない)
            assert L.text_color(entry("desktop.ini"), filer.COL_EXT) == colors["system"]
            # 描いた絵でも: 拡張子の列に、えんじの字の画素がある
            QTest.qWait(30)
            image = L.view.viewport().grab().toImage()
            rect = L.view.visualRect(L.model.index(L._row_of("notes.txt"), filer.COL_EXT))
            found = {QColor(image.pixel(x, y)).name()
                     for x in range(rect.left(), rect.right()) for y in range(rect.top(), rect.bottom())}
            assert "#800040" in found, found

            # Z: 新規・変更・削除・上下・色 → 決定で保存され、その場で反映
            answers = {}

            def run(dialog):
                d = dialog
                d.ask_exts = lambda title, text="": answers.pop("exts")
                d.ask_color = lambda initial: answers.pop("color", None)
                d.confirm = lambda text: answers.pop("confirm", True)
                return _run_stub(answers, d)
            w.exec_dialog = run

            def edit(d):
                assert d.tree.topLevelItemCount() == len(core.DEFAULT_EXT_COLORS)
                # 新規: txt を入れる。ほかの行(えんじ)にもあるので外す
                d.tree.setCurrentItem(d.tree.topLevelItem(0))
                answers.update(exts="*.TXT .foo", color="#123456", confirm=True)
                d.new_button.click()
                assert d.rules[1] == {"color": "#123456", "exts": ["txt", "foo"]}
                assert all("txt" not in r["exts"] for i, r in enumerate(d.rules) if i != 1)
                # 上へ(先頭に)、色を変える
                d.up_button.click()
                assert d.rules[0]["exts"] == ["txt", "foo"]
                answers["color"] = "#ABCDEF"
                d.color_button.click()
                assert d.rules[0]["color"] == "#abcdef"
                # 変更: 拡張子を差し替え
                answers.update(exts="txt foo bar")
                d.edit_button.click()
                assert d.rules[0]["exts"] == ["txt", "foo", "bar"]
                # 下へ・削除(確認付き。いいえなら消えない)
                d.down_button.click()
                assert d.rules[1]["exts"] == ["txt", "foo", "bar"] and d.current_row() == 1
                count = len(d.rules)
                answers["confirm"] = False
                d.delete_button.click()
                assert len(d.rules) == count
                d.tree.setCurrentItem(d.tree.topLevelItem(2))
                removed = d.rules[2]["exts"]
                answers["confirm"] = True
                d.delete_button.click()
                assert len(d.rules) == count - 1 and removed not in [r["exts"] for r in d.rules]
                d.ok_button.click()
            answers["do"] = edit
            _checked_key(L, answers)(Qt.Key_Z)
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            assert stored["other"] == 1                                 # 他のキーはそのまま
            assert stored["filer"]["ext_colors"][1] == {"color": "#abcdef", "exts": ["txt", "foo", "bar"]}
            assert L.text_color(entry("notes.txt"), filer.COL_EXT) == "#abcdef"   # その場で反映

            # 中止なら何も変わらない
            def cancel(d):
                answers.update(confirm=True)
                d.reset_button.click()
                assert d.rules == core.normalize_ext_colors(None)
                d.cancel_button.click()
            answers["do"] = cancel
            _checked_key(L, answers)(Qt.Key_Z)
            assert L.text_color(entry("notes.txt"), filer.COL_EXT) == "#abcdef"
            assert json.loads(settings_path.read_text(encoding="utf-8"))["filer"]["ext_colors"][1][
                "color"] == "#abcdef"

            # 一覧の上の Enter は変更、Del は削除
            def keys(d):
                d.tree.setCurrentItem(d.tree.topLevelItem(0))
                answers.update(exts="zzz")
                QTest.keyClick(d.tree, Qt.Key_Return)
                assert d.rules[0]["exts"] == ["zzz"] and d.result() != QDialog.Accepted
                answers.update(confirm=True)
                QTest.keyClick(d.tree, Qt.Key_Delete)
                assert d.rules[0]["exts"] != ["zzz"]
                d.cancel_button.click()
            answers["do"] = keys
            _checked_key(L, answers)(Qt.Key_Z)
        finally:
            _set_attrs(left / "desktop.ini", 0x80)
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# A. ライブ絞り込み(/)
# ---------------------------------------------------------------
def test_live_filter_logic():
    assert core.parse_live_filter("  Foo  bar ") == ["foo", "bar"]
    assert core.parse_live_filter("") == []
    entries = [F("dir", is_dir=True), F("Foo_bar.txt"), F("foo.txt"), F("bar.md")]
    names = lambda words: [e.name for e in core.live_filter_entries(entries, words)]  # noqa: E731
    assert names(["foo", "bar"]) == ["dir", "Foo_bar.txt"]          # AND・フォルダは残す
    assert names(["md"]) == ["dir", "bar.md"]
    assert names([]) == ["dir", "Foo_bar.txt", "foo.txt", "bar.md"]
    assert core.DEFAULT_KEYMAP["0191"] == "live_filter"


def _type(widget, text):
    from PySide6.QtTest import QTest
    QTest.keyClicks(widget, text)


def test_window_live_filter():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        try:
            L.marks.update({"alpha.txt", "beta.md"})
            key(Qt.Key_Slash)
            assert L.filter_edit.isVisible() and L.filter_edit.hasFocus()
            _type(L.filter_edit, "a t")                          # "a" と "t" を両方含む
            assert _names(L) == ["..", "sub1", "sub2", "alpha.txt", "beta.md", "gamma.txt"], _names(L)
            for _ in range(3):
                QTest.keyClick(L.filter_edit, Qt.Key_Backspace)
            _type(L.filter_edit, "PH")                           # 大文字小文字は無視
            assert _names(L) == ["..", "sub1", "sub2", "alpha.txt", "photo.JPG"], _names(L)
            QTest.keyClick(L.filter_edit, Qt.Key_Return)         # 確定: 絞ったまま閉じる
            assert not L.filter_edit.isVisible() and L.live_filter == "PH"
            assert "[絞込: PH]" in L.header.text()
            # 見えなくなったマーク(beta.md)は持っているが、操作の対象は見えているものだけ
            assert L.marks == {"alpha.txt", "beta.md"}
            assert [e.name for e in L.targets()] == ["alpha.txt"]
            # 絞ったままカーソル・マークが普通に動く
            L.set_cursor(L._row_of("photo.JPG"))
            key(Qt.Key_Space)
            assert "photo.JPG" in L.marks
            key(Qt.Key_Escape)                                   # 入力欄が閉じていれば解かない
            assert L.live_filter == "PH"
            # / で再編集 → Esc で解除
            key(Qt.Key_Slash)
            assert L.filter_edit.text() == "PH"
            QTest.keyClick(L.filter_edit, Qt.Key_Escape)
            assert L.live_filter == "" and len(_names(L)) == 7 and "絞込" not in L.header.text()
            # 空にして Enter でも解除、: でも解除
            key(Qt.Key_Slash)
            _type(L.filter_edit, "zz")
            assert _names(L) == ["..", "sub1", "sub2"]
            QTest.keyClick(L.filter_edit, Qt.Key_Return)
            key(Qt.Key_Colon)
            assert L.live_filter == "" and len(_names(L)) == 7
            # マスクとは重ねがけ
            w.ask_text = lambda *a, **k: "*.txt"
            key(Qt.Key_Colon, Qt.ShiftModifier)
            L.set_live_filter("al")
            assert _names(L) == ["..", "sub1", "sub2", "alpha.txt"]
            key(Qt.Key_Colon)
            # フォルダを移ったら解除
            L.set_live_filter("alpha")
            L.set_cursor(L._row_of("sub1"))
            key(Qt.Key_Return)
            _wait(w)
            assert L.live_filter == "" and "絞込" not in L.header.text()
            # 数万件では打ち終わりを待ってから絞る(打鍵ごとには並べ直さない)
            many = Path(tmp, "many")
            many.mkdir()
            for i in range(filer.LIVE_FILTER_IMMEDIATE + 10):
                (many / f"f{i:05d}.txt").write_bytes(b"")
            # 作った直後の大量のファイルは、ウイルス対策の検査で列挙が待たされることがある。
            # このテストで見たいのは絞り込みなので、読み取りの見切りは長めにしておく。
            w.list_timeout = 60
            L.load(str(many))
            _wait(w, timeout=60)
            key(Qt.Key_Slash)
            _type(L.filter_edit, "f0001")
            assert L._filter_timer.isActive()                      # 打鍵ごとには絞らず待っている
            QTest.qWait(filer.LIVE_FILTER_DELAY_MS + 200)
            assert [e.name for e in L.entries if not e.is_parent] == \
                [f"f0001{i}.txt" for i in range(10)]
            QTest.keyClick(L.filter_edit, Qt.Key_Escape)
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# B. よく行くフォルダ(Shift+H)
# ---------------------------------------------------------------
def test_frecency_logic():
    now = 1_000_000_000.0
    assert core.frecency_weight(10) == 4 and core.frecency_weight(7200) == 2
    assert core.frecency_weight(3 * 86400) == 0.5 and core.frecency_weight(30 * 86400) == 0.25
    data = {}
    core.frecency_add(data, "C:\\Work\\Proj\\", now - 40 * 86400)
    core.frecency_add(data, "c:\\work\\proj", now - 40 * 86400)     # 大小違いは同じもの
    assert list(data) == ["c:\\work\\proj"] and data["c:\\work\\proj"][0] == 2
    core.frecency_add(data, "C:\\Work\\Other", now - 60)
    core.frecency_add(data, "D:\\proj-archive", now - 60)
    # 古くて回数が多いもの(2×0.25)より、最近のもの(1×4)が上
    assert core.frecency_query(data, "", now)[-1] == "c:\\work\\proj"
    # 語は AND、最後の語がフォルダ名にあるものが先
    assert core.frecency_query(data, "work", now) == ["C:\\Work\\Other", "c:\\work\\proj"]
    assert core.frecency_query(data, "proj", now) == ["D:\\proj-archive", "c:\\work\\proj"]
    assert core.frecency_query(data, "work proj", now) == ["c:\\work\\proj"]
    assert core.frecency_query(data, "nothing", now) == []
    # 上限を超えたらスコアの低いものから間引く
    small = {}
    for i in range(5):
        core.frecency_add(small, f"C:\\d{i}", now - (5 - i) * 86400 * 10, limit=3)
    assert sorted(small) == ["C:\\d2", "C:\\d3", "C:\\d4"]
    core.frecency_remove(small, "c:\\D3")
    assert sorted(small) == ["C:\\d2", "C:\\d4"]
    # 置き場所はリポジトリの外(テストでは差し替えてある)
    assert not core.data_dir().startswith(str(Path(__file__).resolve().parent.parent))
    core.save_store("t.json", {"a": [1, 2]})
    assert core.load_store("t.json", {}) == {"a": [1, 2]}
    assert core.load_store("missing.json", {}) == {}
    assert core.load_store("t.json", []) == []                         # 形が違えば既定
    assert core.DEFAULT_KEYMAP["1072"] == "frecency_jump"


def test_window_frecency_jump():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        store = Path(core.data_dir(), core.FRECENCY_STORE)
        if store.exists():
            store.unlink()
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        try:
            for _ in range(3):                       # sub1 に3回、deep に1回行く
                L.load(str(left / "sub1"))
                _wait(w)
                L.load(str(left))
                _wait(w)
            L.load(str(left / "sub1" / "deep"))
            _wait(w)
            key(Qt.Key_F5)                           # 再読込は数えない
            _wait(w)
            saved = core.load_store(core.FRECENCY_STORE, {})
            sub1 = [v for p, v in saved.items() if p.casefold() == str(left / "sub1").casefold()]
            deep = [v for p, v in saved.items() if p.casefold().endswith("deep")]
            assert sub1 and sub1[0][0] == 3 and deep and deep[0][0] == 1, saved
            # 書庫の中は記録しない
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            assert not any(p.casefold().endswith("pack.zip") for p in w.frecency)
            # Shift+H: 語で絞ってスコア順。Enter でそこへ
            key(Qt.Key_H, Qt.ShiftModifier)
            picker = w._picker
            assert picker is not None
            picker.search.setText("sub")
            rows = [picker.list.item(i).text() for i in range(picker.list.count())]
            assert rows[0].casefold() == str(left / "sub1").casefold(), rows
            picker._commit()
            _wait(w)
            assert core.same_dir(L.path, left / "sub1")
            # 無くなったフォルダは候補から外し、記録からも消す
            shutil.rmtree(left / "sub1" / "deep")
            key(Qt.Key_H, Qt.ShiftModifier)
            picker = w._picker
            rows = [picker.list.item(i).text() for i in range(picker.list.count())]
            assert not any(r.casefold().endswith("deep") for r in rows), rows
            picker.close()
            assert not any(p.casefold().endswith("deep") for p in w.frecency)
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# C. フラット表示(Alt+B)
# ---------------------------------------------------------------
def _make_flat_tree(root):
    """フラット表示の試験用。サブフォルダ・隠しファイル・隠しフォルダ・ジャンクション。"""
    import subprocess
    root = Path(root)
    (root / "2026-10").mkdir()
    (root / "2026-10" / "log_001.txt").write_bytes(b"1" * 10)
    (root / "2026-10" / "deep").mkdir()
    (root / "2026-10" / "deep" / "x.md").write_bytes(b"22")
    (root / "top.txt").write_bytes(b"333")
    (root / "hid.txt").write_bytes(b"4")
    _set_attrs(root / "hid.txt", 0x02)
    (root / ".secret").mkdir()
    (root / ".secret" / "in_hidden.txt").write_bytes(b"5")
    _set_attrs(root / ".secret", 0x02 | 0x10)
    # ジャンクション(たどらないこと)。作れない環境では飛ばす
    subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "loop"), str(root)],
                   capture_output=True)
    return root


def test_collect_flat_logic():
    import threading as th
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_flat_tree(Path(tmp, "r").mkdir() or Path(tmp, "r"))
        entries, status = core.collect_flat(str(root))
        names = sorted(e.name for e in entries)
        assert status == "done"
        assert names == sorted(["2026-10\\log_001.txt", "2026-10\\deep\\x.md", "top.txt",
                                "hid.txt", ".secret\\in_hidden.txt"]), names
        assert not any(n.startswith("loop") for n in names)               # ジャンクションはたどらない
        assert all(not e.is_dir for e in entries)                          # フォルダ自体は入れない
        assert {e.name: e.size for e in entries}["2026-10\\deep\\x.md"] == 2
        entries, _ = core.collect_flat(str(root), show_hidden=False)        # Shift+Z に従う
        assert sorted(e.name for e in entries) == sorted(
            ["2026-10\\log_001.txt", "2026-10\\deep\\x.md", "top.txt"])
        entries, status = core.collect_flat(str(root), limit=2)
        assert status == "limit" and len(entries) == 2
        stop = th.Event()
        stop.set()
        assert core.collect_flat(str(root), cancel=stop)[1] == "cancel"
        seen = []
        core.collect_flat(str(root), progress=seen.append)
        assert seen and seen[-1] == 5
        assert core.flat_footer_text(3, 1234, 1, 10) == "3 Files   1,234 bytes   Mark 1 (10)"
        # ZIP でもフォルダ構成を保つ(書庫の中の区切りは /)
        plan = core.zip_plan(str(root), ["2026-10\\log_001.txt"])
        assert [arc for _p, arc, _d in plan] == ["2026-10/log_001.txt"]
        for p in (root / "hid.txt", root / ".secret"):
            _set_attrs(p, 0x80 if p.is_file() else 0x10)
        if (root / "loop").exists():
            os.rmdir(root / "loop")                              # ジャンクションだけ消す
    assert core.DEFAULT_KEYMAP["4066"] == "flat_view"


def test_window_flat_view():
    import zipfile
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "src")
        root.mkdir()
        _make_flat_tree(root)
        right = Path(tmp, "dst")
        right.mkdir()
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(root), "right_path": str(right)}},
                              str(settings_path))
        w.extra_op_flags = _quiet_flags()
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        try:
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w, timeout=30)
            assert L.flat and "[フラット]" in L.header.text()
            assert ".." not in _names(L)
            assert "2026-10\\deep\\x.md" in _names(L) and "top.txt" in _names(L)
            assert L.footer.text().startswith("5 Files   ")
            # 絞り込み・マーク・コピーが普通に効く(対象は相対パスを解決したフルパス)
            L.set_live_filter("2026")
            assert sorted(_names(L)) == ["2026-10\\deep\\x.md", "2026-10\\log_001.txt"]
            assert L.targets() and L.target_paths()[0].startswith(str(root))
            L.marks.update({"2026-10\\deep\\x.md", "2026-10\\log_001.txt"})
            key(Qt.Key_C, Qt.AltModifier)
            _wait(w, timeout=30)
            assert (right / "x.md").exists() and (right / "log_001.txt").exists()
            assert L.flat                                       # 再読込してもフラットのまま
            # ZIP はフォルダ構成を保って入る
            L.set_live_filter("")
            L.marks.clear()
            L.marks.update({"2026-10\\deep\\x.md", "top.txt"})
            w.ask_text = lambda *a, **k: "flat.zip"
            key(Qt.Key_P)
            _wait_extract(w)
            _wait(w, timeout=30)
            with zipfile.ZipFile(right / "flat.zip") as z:
                assert sorted(z.namelist()) == ["2026-10/deep/x.md", "top.txt"]
            # R・K はフラット表示中は断る
            key(Qt.Key_R)
            key(Qt.Key_K)
            assert w.log_view.toPlainText().count("フラット表示中は使えません") == 2
            # BS で通常の表示へ(親へは行かない)
            key(Qt.Key_Backspace)
            _wait(w)
            assert not L.flat and core.same_dir(L.path, root) and "[フラット]" not in L.header.text()
            # Alt+B でもう一度入り、Alt+B で戻る
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w, timeout=30)
            assert L.flat
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w)
            assert not L.flat and ".." in _names(L)
            # 集めている間の Esc は中止(通常の表示に戻る)
            real = filer.core.collect_flat

            def slow(root_, show_hidden, show_system, limit, cancel, progress):
                progress(7)
                cancel.wait(10)
                return [], "cancel" if cancel.is_set() else "done"
            filer.core.collect_flat = slow
            try:
                key(Qt.Key_B, Qt.AltModifier)
                QTest.qWait(300)
                assert L.flat_loading and "収集中 7件" in L.footer.text()
                key(Qt.Key_Escape)
                _wait(w, timeout=15)
                QTest.qWait(50)
                _wait(w, timeout=15)
                assert not L.flat and "中止しました" in w.log_view.toPlainText()
            finally:
                filer.core.collect_flat = real
            # 上限で打ち切ったら知らせる
            w.flat_limit = 2
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w, timeout=30)
            assert len(_names(L)) == 2 and "打ち切りました" in w.log_view.toPlainText()
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w)
            # 書庫の中では断る
            zpath = root / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            key(Qt.Key_B, Qt.AltModifier)
            assert not L.flat and "書庫内ではフラット表示できません" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()
            for p in (root / "hid.txt",):
                if p.exists():
                    _set_attrs(p, 0x80)
            if (root / ".secret").exists():
                _set_attrs(root / ".secret", 0x10)
            if (root / "loop").exists():
                os.rmdir(root / "loop")                          # ジャンクションだけ消す(中身は消さない)


# ---------------------------------------------------------------
# D. プレビュー(F3)
# ---------------------------------------------------------------
def test_preview_logic():
    exts = core.normalize_viewer_extensions(None)
    assert core.preview_kind(None, exts) == "none"
    assert core.preview_kind(core.PARENT_ENTRY, exts) == "none"
    assert core.preview_kind(F("d", is_dir=True), exts) == "folder"
    assert core.preview_kind(F("a.LOG"), exts) == "text"
    assert core.preview_kind(F("p.JPEG"), exts) == "image"
    assert core.preview_kind(F("x.bin"), exts) == "info"
    assert core.fit_size(4000, 3000, 400, 400) == (400, 300)
    assert core.fit_size(100, 50, 400, 400) == (100, 50)               # 拡大はしない
    assert core.fit_size(0, 0, 10, 10) == (0, 0)
    assert core.info_lines("x.bin", F("x.bin", size=1234))[2].startswith("サイズ  1,234")
    assert core.DEFAULT_KEYMAP["0114"] == "preview_toggle"
    assert core.fkey_labels(core.DEFAULT_KEYMAP)[2] == "Preview"


def _wait_preview(w, title, timeout=15.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QTest.qWait(30)
        widget = w.preview_widget
        if widget is not None and widget.last and widget.last.get("title") == title:
            return widget.last
    raise AssertionError(f"プレビューが出ない: {title}")


def test_window_preview():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        Path(left, "notes.txt").write_text("\n".join(f"line {i}" for i in range(500)),
                                           encoding="utf-8")
        big = QImage(3000, 2000, QImage.Format_RGB32)
        big.fill(QColor("#336699"))
        assert big.save(str(left / "pic.png"))
        Path(left, "x.bin").write_bytes(b"\x00\x01")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        try:
            right_path = R.path
            L.set_cursor(L._row_of("notes.txt"))
            key(Qt.Key_F3)
            assert w.preview_on and w.preview_widget.isVisible() and not R.isVisible()
            result = _wait_preview(w, "notes.txt")
            assert result["kind"] == "text" and len(result["lines"]) == core.PREVIEW_TEXT_LINES
            assert result["lines"][0] == "line 0" and result["note"] == "UTF-8"
            assert w.preview_widget.text.toPlainText().startswith("line 0\nline 1")
            # 画像は窓に収まる大きさで読む(元の 3000×2000 のままにしない)
            L.set_cursor(L._row_of("pic.png"))
            result = _wait_preview(w, "pic.png")
            box = w.preview_widget.image_box()
            assert result["image"].width() <= box[0] and result["image"].height() <= box[1]
            assert result["note"] == "3000×2000"
            # フォルダ・その他
            L.set_cursor(L._row_of("sub1"))
            result = _wait_preview(w, "sub1")
            assert result["lines"][0] == "1 フォルダ / 0 ファイル" and "deep\\" in result["lines"]
            L.set_cursor(L._row_of("x.bin"))
            result = _wait_preview(w, "x.bin")
            assert result["kind"] == "info" and result["lines"][0] == "x.bin"
            # 書庫の中の小さなファイルは取り出して見せる
            zpath = left / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            L.set_cursor(L._row_of("readme.txt"))
            result = _wait_preview(w, "readme.txt")
            assert result["lines"] == ["hello"]
            # キーはアクティブ窓に効いたまま(プレビュー中もカーソルが動く)
            key(Qt.Key_Backspace)
            _wait(w)
            assert L.archive is None and w.preview_on
            # プレビューの側へ移ろうとしたら、プレビューを終えてその窓へ
            key(Qt.Key_Tab)
            assert not w.preview_on and w.active_side == "right" and R.isVisible()
            assert R.path == right_path and w.preview_widget is None
            # F3 でもう一度 → F3 で戻る
            QTest.keyClick(R.view, Qt.Key_F3)
            assert w.preview_on and not L.isVisible()
            QTest.keyClick(R.view, Qt.Key_F3)
            assert not w.preview_on and L.isVisible()
            assert w.fkey_buttons[2].text() == "Preview"
        finally:
            w.close()
            w.deleteLater()


def test_preview_esc_and_image_enter():
    """Esc でプレビューを抜ける／画像の Enter は内部ビューア(プレビュー)で開く。

    - 画像で Enter → 関連付けではなくプレビューが開く(2026-10-27 ユーザー要望)
    - Esc → プレビューが閉じて反対窓が戻る。プレビューが無いときの Esc は何もしない
    - 画像のプレビューは待ち時間なしで読み始める(schedule_preview の遅延が 0)"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        img = QImage(120, 80, QImage.Format_RGB32)
        img.fill(QColor("#cc6633"))
        assert img.save(str(left / "pic.png"))
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        try:
            # 画像のプレビューは待たない(タイマーの遅延が 0 になる)
            L.set_cursor(L._row_of("photo.JPG"))
            assert not w.preview_on
            key(Qt.Key_F3)
            assert w.preview_on
            w.schedule_preview()
            assert not w._preview_timer.isActive() or w._preview_timer.interval() == 0

            # Esc で抜ける
            key(Qt.Key_Escape)
            assert not w.preview_on and R.isVisible() and w.preview_widget is None
            # プレビューが無いときの Esc は無害(窓は閉じない)
            key(Qt.Key_Escape)
            assert w.isVisible()

            # 画像で Enter → プレビューが開いて画像が出る
            L.set_cursor(L._row_of("pic.png"))
            key(Qt.Key_Return)
            assert w.preview_on
            result = _wait_preview(w, "pic.png")
            assert result["kind"] == "image" and result["image"] is not None

            # プレビュー中に別の画像へ Enter しても開いたまま切り替わる
            L.set_cursor(L._row_of("photo.JPG"))
            key(Qt.Key_Return)
            result = _wait_preview(w, "photo.JPG")
            assert result["kind"] == "image" and w.preview_on
            key(Qt.Key_Escape)
            assert not w.preview_on
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# E. 一括改名(Renemo 風。Shift+R / Alt+R)
# ---------------------------------------------------------------
def test_rename_logic():
    import filer_rename as fr
    assert fr.to_halfwidth("ＡＢＣ１２３　ガパ") == "ABC123 ｶﾞﾊﾟ"
    assert fr.to_fullwidth("ABC 123 ｶﾞﾊﾟｱ") == "ＡＢＣ　１２３　ガパア"
    assert fr.to_halfwidth("漢字ひらがな") == "漢字ひらがな"
    assert fr.convert("Ab", "upper") == "AB" and fr.convert("Ab", "lower") == "ab"
    assert fr.replace_name("a.b.txt", ".", "_", keep_ext=True) == "a_b.txt"
    assert fr.replace_name("a.b.txt", ".", "_") == "a_b_txt"
    assert fr.replace_name("photo12.jpg", r"(\d+)", r"_\1", regex=True) == "photo_12.jpg"
    assert fr.replace_name("Abc", "a", "x") == "xbc" and fr.replace_name("Abc", "a", "x", case_sensitive=True) == "Abc"
    assert fr.replace_name("a1", "1", r"\1") == "a\\1"                    # 正規表現でなければそのまま
    try:
        fr.replace_name("a", "(", "x", regex=True)
        raise AssertionError("壊れた正規表現で止まらない")
    except ValueError:
        pass
    assert fr.format_number("p_{n:03}", 7) == "p_007" and fr.format_number("p{n}", 7, 2) == "p07"
    assert fr.numbered_names(["a.jpg", "b.PNG", "d"], "photo_{n:03}", 1, 1, 1, True,
                             [False, False, True]) == ["photo_001.jpg", "photo_002.PNG", "photo_003"]
    assert fr.numbered_names(["a.jpg"], "p_", 5, 2, 3) == ["p_005.jpg"]   # {n} が無ければ末尾に
    assert fr.name_problem("con.txt") and fr.name_problem("LPT1") and fr.name_problem("a ")
    assert fr.name_problem("a?b") and fr.name_problem("") and fr.name_problem("ok.txt") is None
    items = [("C:\\d", "a.txt"), ("C:\\d", "b.txt"), ("C:\\d", "c.txt")]
    existing = {"C:\\d": ["a.txt", "b.txt", "c.txt", "other.txt"]}
    assert fr.validate_batch(items, ["b.txt", "a.txt", "c.txt"], existing) == {}   # 入れ替えは通る
    assert fr.validate_batch(items, ["A.TXT", "b.txt", "c.txt"], existing) == {}   # 大小だけも通る
    assert fr.validate_batch(items, ["x.txt", "x.TXT", "c.txt"], existing) == {
        0: "結果の名前が重なっています", 1: "結果の名前が重なっています"}
    assert fr.validate_batch(items, ["other.txt", "b.txt", "c.txt"], existing) == {
        0: "同じ名前のファイルが既にあります"}
    assert fr.validate_batch(items, ["a?.txt", "b.txt", "con"], existing).keys() == {0, 2}
    with tempfile.TemporaryDirectory() as tmp:
        a, b, c = (os.path.join(tmp, n) for n in ("a.txt", "b.txt", "c.txt"))
        for path, data in ((a, b"A"), (b, b"B"), (c, b"C")):
            Path(path).write_bytes(data)
        pairs = [(a, b), (b, a), (c, os.path.join(tmp, "C.TXT"))]      # 入れ替えと大小だけ
        count, error = fr.execute_plan(fr.plan_renames(pairs))
        assert error is None and count == 6
        assert Path(a).read_bytes() == b"B" and Path(b).read_bytes() == b"A"
        assert "C.TXT" in os.listdir(tmp)
        # 途中で失敗したら、それまでの改名を巻き戻す
        calls = []

        def flaky(src, dst):
            calls.append((src, dst))
            if len(calls) == 3:
                raise OSError(13, "使用中")
            os.rename(src, dst)
        before = sorted(os.listdir(tmp))
        count, error = fr.execute_plan(fr.plan_renames(
            [(a, os.path.join(tmp, "x.txt")), (b, os.path.join(tmp, "y.txt"))]), rename=flaky)
        assert count == 0 and "元に戻しました" in error and sorted(os.listdir(tmp)) == before
    history = fr.add_history([], [("C:\\d\\a", "C:\\d\\b")], now=100)
    history = fr.add_history(history, [("C:\\d\\x", "C:\\d\\x")], now=200)   # 変化なしは記録しない
    assert len(history) == 1 and fr.undo_pairs(history[0]) == [("C:\\d\\b", "C:\\d\\a")]
    exists = {"C:\\d\\b"}.__contains__
    assert fr.undo_problems(history[0], exists) == []
    assert fr.undo_problems(history[0], lambda p: True)                      # 元の名前が埋まっている
    assert "1件" in fr.describe_history(history[0])
    many = []
    for i in range(fr.HISTORY_LIMIT + 5):
        many = fr.add_history(many, [(f"a{i}", f"b{i}")], now=i)
    assert len(many) == fr.HISTORY_LIMIT and many[0]["pairs"] == [["a54", "b54"]]
    assert core.DEFAULT_KEYMAP["1082"] == "batch_rename" and core.DEFAULT_KEYMAP["4082"] == "rename_menu"


def test_window_renemo():
    import filer
    import filer_rename as fr
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QDialog

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp, "work")
        folder.mkdir()
        for name in ("data.txt", "memo.md", "Photo.JPG", "keep.bin"):
            Path(folder, name).write_bytes(name.encode())
        Path(folder, "sub").mkdir()
        right = Path(tmp, "other")
        right.mkdir()
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        history_file = Path(core.data_dir(), fr.HISTORY_STORE)
        if history_file.exists():
            history_file.unlink()
        w = filer.FilerWindow({"filer": {"left_path": str(folder), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        steps = {}
        key = _checked_key(L, steps)

        def run(dialog):
            return _run_stub(steps, dialog)
        w.exec_dialog = run
        try:
            # Alt+R のメニューは先頭(一括改名)が選ばれた状態 → Enter 相当で開く
            seen = []
            w._exec_menu = lambda m, c: (seen.append(m.activeAction().text()), c[m.activeAction()])[1]

            def first_look(d):
                # 名前の列は拡張子付き、編集名は拡張子を隠す
                assert d.names_view.toPlainText().split("\n") == ["sub", "data.txt", "keep.bin",
                                                                   "memo.md", "Photo.JPG"]
                assert d.editor_lines() == ["sub", "data", "keep", "memo", "Photo"]
                # 種類はエクスプローラと同じ名前(この PC では「テキスト ドキュメント」)
                info_lines = d.info_view.toPlainText().splitlines()
                import filer_shell
                assert info_lines[1].endswith(filer_shell.file_type_name("data.txt"))
                assert filer_shell.file_type_name("data.txt")
                # 編集 → .ext を出しても編集は残る、拡張子も直せる
                d.editor.setPlainText("sub\nDATA2\nkeep\nmemo\nPhoto")
                d.act_ext.setChecked(True)
                d.toggle_ext()
                assert d.editor_lines() == ["sub", "DATA2.txt", "keep.bin", "memo.md", "Photo.JPG"]
                lines = d.editor_lines()
                lines[4] = "Photo.jpg"
                d.editor.setPlainText("\n".join(lines))
                # フルパス表示
                d.act_fullpath.setChecked(True)
                d.toggle_fullpath()
                assert d.names_view.toPlainText().split("\n")[1] == str(folder / "data.txt")
                d.reject()
            steps["do"] = first_look
            key(Qt.Key_R, Qt.AltModifier)
            assert seen and seen[0].startswith("&B") and steps["result"] == QDialog.Rejected

            # 変換(全行)・置換・連番・並べ替え → 実行
            def convert_and_run(d):
                d.convert_selection("upper")                         # 選択が無ければ全行
                assert d.editor_lines() == ["SUB", "DATA", "KEEP", "MEMO", "PHOTO"]
                d.editor.undo()                                       # 取り消せる
                assert d.editor_lines() == ["sub", "data", "keep", "memo", "Photo"]
                d.ask_replace = lambda: {"find": "e", "repl": "E", "regex": False, "case": True,
                                         "keep_ext": True}
                d.replace_dialog()
                assert d.editor_lines() == ["sub", "data", "kEEp", "mEmo", "Photo"]
                # 3〜4行目だけに連番
                c = d.editor.textCursor()
                c.setPosition(d.editor.document().findBlockByNumber(2).position())
                c.setPosition(d.editor.document().findBlockByNumber(3).position() + 1,
                              QTextCursor.KeepAnchor)
                d.editor.setTextCursor(c)
                d.ask_numbering = lambda: {"fmt": "file_{n:02}", "start": 1, "step": 1, "digits": 1}
                d.number_dialog()
                assert d.editor_lines() == ["sub", "data", "file_01", "file_02", "Photo"]
                # 選んだ部分だけ全角に
                c = d.editor.textCursor()
                block = d.editor.document().findBlockByNumber(1)
                c.setPosition(block.position())
                c.setPosition(block.position() + 4, QTextCursor.KeepAnchor)
                d.editor.setTextCursor(c)
                d.convert_selection("full")
                assert d.editor_lines()[1] == "ｄａｔａ"
                # 並べ替えても元の名前との組は崩れない
                d.sort_rows("edited")
                assert d.editor_lines()[:3] == ["ｄａｔａ", "file_01", "file_02"]
                assert d.names_view.toPlainText().split("\n")[:3] == ["data.txt", "keep.bin", "memo.md"]
                d.execute()
            steps["do"] = convert_and_run
            key(Qt.Key_R, Qt.ShiftModifier)
            _wait(w)
            names = sorted(os.listdir(folder))
            assert names == sorted(["sub", "ｄａｔａ.txt", "file_01.bin", "file_02.md", "Photo.JPG"]), names
            assert "一括改名: 3件" in w.log_view.toPlainText()
            assert len(fr.load_history()) == 1

            # 問題のある行は赤で示して実行しない(既存のファイルとの衝突・予約名・行数)
            Path(right, "dummy").write_bytes(b"")

            def problems(d):
                lines = d.editor_lines()
                lines[d.editor_lines().index("Photo")] = "file_01"      # 名前の部分が重なる
                d.act_ext.setChecked(True)
                d.editor.setPlainText("\n".join(lines))
                d.toggle_ext()
                lines = d.editor_lines()
                lines[0] = "con"
                d.editor.setPlainText("\n".join(lines))
                d.execute()
                assert d.result() != QDialog.Accepted and "問題のある行" in d.message
                assert len(d.problems) == 1 and len(d.editor.extraSelections()) >= 1
                d.editor.setPlainText("\n".join(lines[:-1]))          # 行を消した
                d.execute()
                assert "行数が合いません" in d.message
                d.reject()
            steps["do"] = problems
            L.marks.clear()
            L.marks.update({"sub", "Photo.JPG"})
            key(Qt.Key_R, Qt.ShiftModifier)
            assert sorted(os.listdir(folder)) == names

            # 入れ替え(file_01 ⇔ file_02 の名前部分)と大文字小文字だけの変更
            def swap(d):
                d.act_ext.setChecked(True)
                d.toggle_ext()
                lines = d.editor_lines()
                lines = ["file_02.bin" if l == "file_01.bin" else "file_01.md" if l == "file_02.md"
                         else "photo.jpg" if l == "Photo.JPG" else l for l in lines]
                d.editor.setPlainText("\n".join(lines))
                d.execute()
            steps["do"] = swap
            L.marks.clear()
            key(Qt.Key_R, Qt.ShiftModifier)
            _wait(w)
            names2 = sorted(os.listdir(folder))
            assert "file_02.bin" in names2 and "file_01.md" in names2 and "photo.jpg" in names2
            assert Path(folder, "file_02.bin").read_bytes() == b"keep.bin"

            # 途中で失敗したら巻き戻す(何も変わらない)
            count = [0]

            def flaky(src, dst):
                count[0] += 1
                if count[0] == 2:
                    raise OSError(13, "使用中")
                os.rename(src, dst)
            w.rename_func = flaky

            def fail(d):
                d.editor.setPlainText("\n".join(l + "_x" for l in d.editor_lines()))
                d.execute()
                assert d.result() != QDialog.Accepted and "元に戻しました" in d.message
                d.reject()
            steps["do"] = fail
            key(Qt.Key_R, Qt.ShiftModifier)
            _wait(w)
            assert sorted(os.listdir(folder)) == names2
            w.rename_func = os.rename

            # 履歴から元に戻す(最後の一括改名 = 入れ替えを戻す)
            def undo(d):
                assert len(d.history) == 2
                d.choose_history = lambda: 0
                d.history_dialog()
            steps["do"] = undo
            key(Qt.Key_R, Qt.ShiftModifier)
            _wait(w)
            assert sorted(os.listdir(folder)) == names
            assert len(fr.load_history()) == 1

            # Esc: 編集があれば確かめる(いいえなら閉じない)
            def esc(d):
                d.editor.setPlainText("\n".join(l + "!" for l in d.editor_lines()))
                finished = []
                d.finished.connect(finished.append)    # result() は最初から 0(=Rejected)なので
                asked = []
                d.confirm = lambda text: (asked.append(text), False)[1]
                QTest.keyClick(d.editor, Qt.Key_Escape)
                assert asked and not finished          # 確かめて、いいえなら閉じない
                d.confirm = lambda text: True
                QTest.keyClick(d.editor, Qt.Key_Escape)
                assert finished == [QDialog.Rejected]
            steps["do"] = esc
            key(Qt.Key_R, Qt.ShiftModifier)
            # Enter は改行しない(行の数を変えない)
            def no_newline(d):
                QTest.keyClick(d.editor, Qt.Key_Return)
                assert len(d.editor_lines()) == len(d.order)
                d.reject()
            steps["do"] = no_newline
            key(Qt.Key_R, Qt.ShiftModifier)
            # 書庫の中・フラット表示中は断る
            steps.clear()
            zpath = folder / "pack.zip"
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            key(Qt.Key_R, Qt.ShiftModifier)
            assert "dialog" not in steps and "書庫内では使えません" in w.log_view.toPlainText()
        finally:
            w.close()
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
