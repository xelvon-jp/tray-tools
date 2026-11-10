# tests/test_filer.py
# あっふ(filer.py / filer_core.py / filer_shell.py / filer_launch.py)を確かめる。
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

# CI(windows-latest)のコンソールは cp1252 で、日本語を含む traceback を
# print すると UnicodeEncodeError になり、テストが全部通っていても
# ジョブが失敗扱いになる(2026-10-27 以降の Actions 失敗の原因)。
# 流れのエンコーディングを UTF-8 に寄せて、環境に依らず落とさない。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

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
    # 境界は端まで寄せられる(片方を畳み切れる)
    assert core.SPLIT_MIN == 0.0
    assert core.step_split(0.02, -1) == 0.0 and core.step_split(0.98, 1) == 1.0
    assert core.clamp_split("x") == 0.5 and core.clamp_split(5) == 1.0
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


def test_existing_ancestor_stuck_parent():
    """親が自分と変わらない形(parent_of の不動点)でも、無限に回らず None で終わる。
    Windows 以外の os.path では実際に起こる(Linux の VM で検証が止まった)。"""
    original = core.parent_of
    core.parent_of = lambda p: p  # noqa: E731
    try:
        assert core.existing_ancestor("C:\\a\\b", lambda p: False) is None
        assert core.existing_ancestor("C:\\a\\b", lambda p: True) == "C:\\a\\b"
    finally:
        core.parent_of = original


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


def test_sync_sources():
    """フォルダ同期コピー(Shift+W)の送る側の選別。相手に無いもの・サイズ違い・
    こちらが新しいものだけ。古いものでは上書きせず、両方にあるフォルダと
    名前が衝突するファイル⇔フォルダの組は送らない。"""
    mine = [core.PARENT_ENTRY, F("same.txt", 10, 100), F("newer.txt", 10, 200),
            F("older.txt", 10, 50), F("near.txt", 10, 101.5), F("diff.txt", 99, 100),
            F("only_here.txt", 1, 1), F("only_dir", is_dir=True),
            F("both_dir", is_dir=True), F("clash", 3, 3)]
    theirs = [F("SAME.TXT", 10, 100), F("newer.txt", 10, 100), F("older.txt", 10, 100),
              F("near.txt", 10, 100), F("diff.txt", 10, 100),
              F("both_dir", is_dir=True), F("clash", is_dir=True)]
    names = [e.name for e in core.sync_sources(mine, theirs)]
    assert names == ["newer.txt", "diff.txt", "only_here.txt", "only_dir"], names
    # 許容誤差を狭めれば、誤差の外に出た near(1.5秒新しい)も対象になる
    assert [e.name for e in core.sync_sources(mine, theirs, tolerance=1)] == [
        "newer.txt", "near.txt", "diff.txt", "only_here.txt", "only_dir"]
    # サイズが違っても、相手のほうが新しければ送らない(古いもので
    # 新しいものを上書きしない)。時刻が誤差の範囲内ならサイズ違いは送る。
    mine2 = [F("stale_big.txt", 99, 50), F("resized.txt", 99, 101),
             F("fresh_same.txt", 10, 300)]
    theirs2 = [F("stale_big.txt", 10, 100), F("resized.txt", 10, 100),
               F("fresh_same.txt", 10, 100)]
    assert [e.name for e in core.sync_sources(mine2, theirs2)] == [
        "resized.txt", "fresh_same.txt"]
    assert core.DEFAULT_KEYMAP["1087"] == "sync_copy"
    assert "sync_copy" in core.COMMANDS
    assert core.COMMAND_SHORT_NAMES["sync_copy"] == "Sync"


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
    # 文字キーはプリセットの選択だけ。決定は Enter(メニューと同じ作法)
    assert compare((Qt.Key_A, N)) is None
    assert compare((Qt.Key_A, N), (Qt.Key_Return, N)) == {
        "size": "same", "time": "same", "include_dirs": False,
        "strip_ext": False, "unmark": False}
    assert compare((Qt.Key_T, N), (Qt.Key_Return, Qt.ShiftModifier))["unmark"] is True
    assert compare((Qt.Key_E, N), (Qt.Key_Return, N))["strip_ext"] is True
    # E で最下行(除拡張子)を選んだあと A を押すと、最下行から離れて除拡張子は外れる
    r = compare((Qt.Key_E, N), (Qt.Key_A, N), (Qt.Key_Return, N))
    assert r["strip_ext"] is False and r["size"] == "same"
    # ←→で選択肢、↓で行、Space でフォルダ、Enter で決定
    r = compare((Qt.Key_Left, N), (Qt.Key_Left, N), (Qt.Key_Down, N), (Qt.Key_Right, N),
                (Qt.Key_Space, N), (Qt.Key_Return, N))
    assert r == {"size": "larger", "time": "same", "include_dirs": True,
                 "strip_ext": False, "unmark": False}, r
    r = compare((Qt.Key_Up, N), (Qt.Key_Return, Qt.ShiftModifier))   # 最下行(除拡張子)
    assert r["strip_ext"] and r["unmark"]
    assert compare((Qt.Key_Escape, N)) is None

    # 見た目: 選択肢は〔〕でなく色の帯、チェックは ☑/☐、枠線の色がある
    d = filer.CompareDialog(None, "同名ファイル比較マーク(左)", True, colors, font)
    size_text = d.rows[0].text()
    assert "<span" in size_text and "〔" not in size_text, size_text
    assert "同じ" in size_text
    assert d.rows[2].text().startswith("☑"), d.rows[2].text()
    assert "dialog_border" in colors
    d2 = filer.SortDialog(None, "ソート(右)", None, colors, font)
    assert "<span" in d2.order_label.text(), d2.order_label.text()
    assert d2.check_labels[0].text().startswith("☑")   # 既定はフォルダを上に集める
    assert d2.check_labels[1].text().startswith("☑")   # 既定は大文字小文字区別無し

    def sort(spec, *keys):
        d = filer.SortDialog(None, "ソート(右)", spec, colors, font)
        for k in keys:
            QTest.keyClick(d, k)
        return d.result_value

    # 文字キーは押した時点で即決定(あふｗと同じ。本人の指定)
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


def test_stash_entries():
    """一時置き場の項目づくり: 実在するものだけが残り、名前はフルパス。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "a.txt").write_text("hello", encoding="utf-8")
        (base / "sub").mkdir()
        entries, missing = core.stash_entries(
            [str(base / "a.txt"), str(base / "sub"), str(base / "gone.txt")])
        assert [e.name for e in entries] == [str(base / "a.txt"), str(base / "sub")]
        assert entries[0].size == 5 and not entries[0].is_dir
        assert entries[1].is_dir
        assert missing == [str(base / "gone.txt")]


def test_stash_pane():
    """一時置き場: 登録 → アクティブ窓に出す(背景色が変わる) → 外す → 戻る。

    操作は通常時を踏襲し、削除キーは実ファイルを消さず登録だけを外す。
    J の一覧にも置き場の項目が出る(2026-10-27 ユーザー仕様)。"""
    import filer
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k: QTest.keyClick(L.view, k)  # noqa: E731
        try:
            target = str(left / "alpha.txt")
            L.set_cursor(L._row_of("alpha.txt"))
            key(Qt.Key_T)              # T だけでは登録されない(開く/戻る)
            assert L.stash and L.entries == []
            key(Qt.Key_T)              # もう一度で元のフォルダへ
            _wait(w)
            assert not L.stash and L.path == str(left)
            L.set_cursor(L._row_of("alpha.txt"))
            w.cmd_stash_add()
            assert w.stash_paths == [target]
            saved = Path(settings_path).read_text(encoding="utf-8")
            assert "alpha.txt" in saved   # filer.stash に保存される
            key(Qt.Key_T)              # 置き場を開く
            assert L.stash
            assert [e.name for e in L.entries] == [target]
            assert L.path == ""
            style = L.view.styleSheet()
            assert w.colors["stash_background"] in style, style
            # 削除キー = 登録を外すだけ(実ファイルは残る)
            w.confirm = lambda *a, **k: True
            L.set_cursor(0)
            key(Qt.Key_Delete)
            assert w.stash_paths == []
            assert (left / "alpha.txt").exists()
            assert L.stash and L.entries == []
            key(Qt.Key_T)              # 戻る
            _wait(w)
            # 地の色も通常へ戻る(クリックするまで緑のまま、の回帰防止)
            style = L.view.styleSheet()
            assert w.colors["background"] in style, style
            assert w.colors["stash_background"] not in style, style
            assert not L.stash and L.path == str(left)
        finally:
            w.close()
            w.deleteLater()


def test_stash_register_keys_and_cursor():
    """置き場: 反対窓が置き場なら C/M は登録として働く(実体は動かない)。
    登録解除後のカーソルは通常窓の削除と同じく、元の名前→元の行に留まる。"""
    import filer
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
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
        key = lambda k, m=Qt.NoModifier: QTest.keyClick(L.view, k, m)  # noqa: E731
        try:
            paths = [str(left / n) for n in ("alpha.txt", "beta.md", "gamma.txt")]
            w.add_stash_paths(paths)
            L.show_stash()
            assert [Path(e.name).name for e in L.entries] == ["alpha.txt", "beta.md", "gamma.txt"]
            w.confirm = lambda *a, **k: True
            L.set_cursor(1)                       # beta.md を外す
            key(Qt.Key_Delete)
            assert w.stash_paths == [paths[0], paths[2]]
            assert L.cursor_row() == 1 and _cursor_name(L).endswith("gamma.txt")
            L.set_cursor(1)                       # gamma.txt(末尾)を外す
            key(Qt.Key_Delete)
            assert w.stash_paths == [paths[0]]
            assert L.cursor_row() == 0 and _cursor_name(L).endswith("alpha.txt")
            L.close_stash()
            _wait(w)
            R.show_stash()                        # 反対窓を置き場にする
            L.set_cursor(L._row_of("beta.md"))
            key(Qt.Key_C, Qt.AltModifier)         # 複写キーで登録
            assert str(left / "beta.md") in w.stash_paths
            assert (left / "beta.md").exists()
            L.set_cursor(L._row_of("photo.JPG"))
            key(Qt.Key_M, Qt.AltModifier)         # 移動キーでも登録(移動はしない)
            assert str(left / "photo.JPG") in w.stash_paths
            assert (left / "photo.JPG").exists()
            assert any(e.name.endswith("photo.JPG") for e in R.entries)
        finally:
            w.close()
            w.deleteLater()


def test_transfer_cursor_follows_to_other_pane():
    """あふｗ準拠: 反対窓へ移動/複写したら、反対窓のカーソルは送った
    ファイルの行に置かれる(後からフォーカスを移したときにそこに居る)。"""
    import filer
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
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
        key = lambda k, m=Qt.NoModifier: QTest.keyClick(L.view, k, m)  # noqa: E731
        try:
            R.set_cursor(R._row_of("r.txt"))
            L.set_cursor(L._row_of("gamma.txt"))
            key(Qt.Key_M, Qt.AltModifier)         # 移動
            _wait(w)
            assert (right / "gamma.txt").exists() and not (left / "gamma.txt").exists()
            assert _cursor_name(R) == "gamma.txt"
            L.set_cursor(L._row_of("alpha.txt"))
            key(Qt.Key_C, Qt.AltModifier)         # 複写でも同じ
            _wait(w)
            assert (right / "alpha.txt").exists()
            assert _cursor_name(R) == "alpha.txt"
        finally:
            w.close()
            w.deleteLater()


def test_window_transfer_com_init():
    """転送キューの裏スレッドは、シェル操作の前に COM(STA) を初期化し、
    終わったら対で戻す(初期化 → 転送 → 後始末 の順で各1回)。"""
    import ctypes

    import filer
    import filer_shell as sh
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
        calls = []
        ole32 = ctypes.windll.ole32
        orig_init, orig_uninit = ole32.CoInitializeEx, ole32.CoUninitialize
        orig_copy = sh.copy_files

        def fake_init(ptr, flags):
            calls.append(("init", ptr, flags))
            return 0  # S_OK

        def fake_copy(paths, dest, hwnd, flags):
            calls.append(("copy",))
            return (0, False)

        ole32.CoInitializeEx = fake_init
        ole32.CoUninitialize = lambda: calls.append(("uninit",))
        sh.copy_files = fake_copy
        try:
            w._enqueue_transfer(False, [str(left / "alpha.txt")], L, R)
            for _ in range(100):
                QTest.qWait(20)
                if w._op_running is None and not w._op_queue:
                    break
        finally:
            ole32.CoInitializeEx, ole32.CoUninitialize = orig_init, orig_uninit
            sh.copy_files = orig_copy
        assert calls == [("init", None, 0x2 | 0x4), ("copy",), ("uninit",)], calls
        w.close()
        w.deleteLater()


def test_window_transfer_queue():
    """転送キュー(案A): 積んだ順に裏で実行され、Shift+Q で待ちだけを
    取り消せる。実行中の1件は取り消されずに完了する。"""
    import filer
    import filer_shell as sh
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.extra_op_flags = _quiet_flags()
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        gate = threading.Event()
        orig_copy = sh.copy_files

        def gated_copy(paths, dest, hwnd, flags):
            gate.wait(10)
            return orig_copy(paths, dest, hwnd, flags)

        sh.copy_files = gated_copy
        try:
            w._enqueue_transfer(False, [str(left / "alpha.txt")], L, R)
            assert w._op_running is not None          # 1件目は実行中(ゲートで停止)
            w._enqueue_transfer(False, [str(left / "beta.md")], L, R)
            assert len(w._op_queue) == 1              # 2件目は待ち
            w.cmd_queue_clear()                        # 待ちだけ取り消す
            assert not w._op_queue and w._op_running is not None
        finally:
            gate.set()
            sh.copy_files = orig_copy
        _wait(w)                                       # 実行中の完了を待つ
        assert w._op_running is None and not w._op_queue
        assert (right / "alpha.txt").exists()          # 実行中だった1件は届く
        assert not (right / "beta.md").exists()        # 取り消した待ちは届かない
        assert "キュー: 待ち 1件を取り消しました" in w.log_view.toPlainText()
        assert core.DEFAULT_KEYMAP["1081"] == "queue_clear"
        w.close()
        w.deleteLater()


def test_window_copy_renamed():
    """C の名前を変える複写4種: _cp 付与とリネーム入力が、キュー経由で
    (元, 先フルパス) の組としてシェルへ渡り、宛先に新しい名前で届く。"""
    import filer
    import filer_shell as sh
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
        L = w.left
        calls = []
        orig_named = sh.copy_files_named

        def fake_named(sources, dests, hwnd, flags):
            calls.append((list(sources), list(dests)))
            for s, d in zip(sources, dests):
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
            return (0, False)

        def drain():
            for _ in range(100):
                QTest.qWait(20)
                if w._op_running is None and not w._op_queue:
                    break
            assert w._op_running is None and not w._op_queue

        menu_items = []
        state = {"choice": None, "answer": None}

        def choose(title, items):
            menu_items.extend(items)
            return state["choice"]

        w.choose_menu = choose
        w.ask_text = lambda title, label, text="", select=None: state["answer"]
        sh.copy_files_named = fake_named
        try:
            # D: ここにコピー(_cp) — ファイルは拡張子の前
            L.set_cursor(_names(L).index("alpha.txt"))
            state["choice"] = "suffix_here"
            w.cmd_copy_menu()
            drain()
            assert calls[-1] == ([str(left / "alpha.txt")],
                                 [str(left / "alpha_cp.txt")]), calls
            assert (left / "alpha_cp.txt").exists()
            # B: 反対窓にコピー(_cp)
            L.set_cursor(_names(L).index("beta.md"))
            state["choice"] = "suffix_other"
            w.cmd_copy_menu()
            drain()
            assert calls[-1] == ([str(left / "beta.md")],
                                 [str(right / "beta_cp.md")]), calls
            assert (right / "beta_cp.md").exists()
            # D: ここにコピー(_cp) — フォルダは末尾に付く
            L.set_cursor(_names(L).index("sub1"))
            state["choice"] = "suffix_here"
            w.cmd_copy_menu()
            drain()
            assert calls[-1][1] == [str(left / "sub1_cp")], calls
            assert (left / "sub1_cp" / "deep").is_dir()
            # R: 反対窓にリネームしてコピー
            L.set_cursor(_names(L).index("gamma.txt"))
            state["choice"] = "rename_other"
            state["answer"] = "renamed.txt"
            w.cmd_copy_menu()
            drain()
            assert calls[-1] == ([str(left / "gamma.txt")],
                                 [str(right / "renamed.txt")]), calls
            assert (right / "renamed.txt").exists()
            # T: ここにリネームで同名 → 飛ばして何も積まない
            before = len(calls)
            L.set_cursor(_names(L).index("gamma.txt"))
            state["choice"] = "rename_here"
            state["answer"] = "gamma.txt"
            w.cmd_copy_menu()
            drain()
            assert len(calls) == before
            assert "同じ名前のため飛ばしました" in w.log_view.toPlainText()
            # T: 入力をキャンセル → 何も積まない
            state["answer"] = None
            w.cmd_copy_menu()
            drain()
            assert len(calls) == before
            # メニューに4項目が並ぶ
            values = [item[2] for item in menu_items]
            for v in ("rename_other", "suffix_other", "rename_here", "suffix_here"):
                assert v in values, values
        finally:
            sh.copy_files_named = orig_named
            w.close()
            w.deleteLater()

        # 左右が同じフォルダでも、名前を変える複写はできる
        w2 = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(left),
                                          "editor": "x"}},
                               str(settings_path))
        w2.show()
        w2.start()
        _wait(w2)
        calls.clear()
        w2.choose_menu = choose
        w2.ask_text = lambda title, label, text="", select=None: state["answer"]
        sh.copy_files_named = fake_named
        try:
            L2 = w2.left
            # B: _cp 付与なら同フォルダでも届く
            L2.set_cursor(_names(L2).index("beta.md"))
            state["choice"] = "suffix_other"
            w2.cmd_copy_menu()
            for _ in range(100):
                QTest.qWait(20)
                if w2._op_running is None and not w2._op_queue:
                    break
            assert calls[-1] == ([str(left / "beta.md")],
                                 [str(left / "beta_cp.md")]), calls
            assert (left / "beta_cp.md").exists()
            # R: 同フォルダでリネーム入力が同名 → 飛ばして何も積まない
            before = len(calls)
            L2.set_cursor(_names(L2).index("beta.md"))
            state["choice"] = "rename_other"
            state["answer"] = "beta.md"
            w2.cmd_copy_menu()
            assert len(calls) == before
            assert "同じ名前のため飛ばしました" in w2.log_view.toPlainText()
        finally:
            sh.copy_files_named = orig_named
            w2.close()
            w2.deleteLater()


def test_window_mkdir_stamp():
    """K: 入力欄に日時プレフィックスが選択状態で入る。そのまま Enter なら
    日時だけ(末尾の "_" は落ちる)、続きを打てば日時付き、打ち替えれば通常名。"""
    import filer

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
        seen = {}
        answers = []

        def ask(title, label, text="", select=None):
            seen.update(text=text, select=select)
            return answers.pop(0)

        w.ask_text = ask
        try:
            # そのまま Enter(スタブは入力欄の初期値をそのまま返す)
            def ask_echo(title, label, text="", select=None):
                seen.update(text=text, select=select)
                return text
            w.ask_text = ask_echo
            w.cmd_mkdir()
            prefix = seen["text"]
            assert prefix.endswith("_") and len(prefix) == len("20261105_1548_"), prefix
            assert seen["select"] == len(prefix)
            assert (left / prefix[:-1]).is_dir()          # 日時だけ・"_" なし
            assert not (left / prefix).exists()
            # 続きを打った形
            w.ask_text = ask
            answers.append(prefix + "写真")
            w.cmd_mkdir()
            assert (left / (prefix + "写真")).is_dir()
            # 打ち替えた形(通常名)
            answers.append("ふつうの名前")
            w.cmd_mkdir()
            assert (left / "ふつうの名前").is_dir()
        finally:
            w.close()
            w.deleteLater()


def test_window_newtext_stamp():
    """Shift+E: 空のテキストを作ってエディタで開く。拡張子なしなら .txt、あれば維持、
    日時だけなら末尾の "_" は落ちる。同名は作らず、キャンセルなら何もしない。"""
    import filer

    _qapp()
    assert core.DEFAULT_KEYMAP["1069"] == "newtext"
    assert core.DEFAULT_KEYMAP["0069"] == "edit"  # E は従来どおり
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
        seen = {}
        answers = []
        opened = []

        def ask(title, label, text="", select=None):
            seen.update(text=text, select=select)
            return answers.pop(0)

        w.ask_text = ask
        w._open_in_editor = lambda paths: opened.append(list(paths))
        try:
            before = set(os.listdir(left))
            # 拡張子なし → .txt
            answers.append("メモ")
            w.cmd_newtext()
            prefix = seen["text"]
            assert prefix.endswith("_") and len(prefix) == len("20261105_1548_"), prefix
            assert seen["select"] == len(prefix)
            p = left / "メモ.txt"
            assert p.is_file() and p.read_bytes() == b""
            assert opened == [[str(p)]], opened
            # 拡張子あり → そのまま
            answers.append("memo.md")
            w.cmd_newtext()
            assert (left / "memo.md").is_file() and not (left / "memo.md.txt").exists()
            assert opened[-1] == [str(left / "memo.md")]
            # 日時だけ → 末尾の _ なし
            answers.append(prefix)
            w.cmd_newtext()
            assert (left / (prefix[:-1] + ".txt")).is_file()
            assert not (left / (prefix + ".txt")).exists()
            # 同名 → 上書きしない・エディタも開かない
            p.write_text("keep", encoding="utf-8")
            n = len(opened)
            answers.append("メモ")
            w.cmd_newtext()
            assert p.read_text(encoding="utf-8") == "keep" and len(opened) == n
            # キャンセル → 何もしない
            files = set(os.listdir(left))
            answers.append(None)
            w.cmd_newtext()
            assert set(os.listdir(left)) == files and len(opened) == n
            assert files - before == {"メモ.txt", "memo.md", prefix[:-1] + ".txt"}
        finally:
            w.close()
            w.deleteLater()


def test_picker_auto_width_and_elide():
    """ピッカー: 長い項目に合わせて幅が広がり(上限は画面の9割)、
    一覧の省略は中央(先頭と末尾が見える形)になる。"""
    from picker import PickerWindow, WINDOW_WIDTH
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    _qapp()
    short = PickerWindow("短い", [("a", "a"), ("bb", "bb")], lambda n, d: None)
    try:
        assert short.width() == WINDOW_WIDTH
        assert short.list.textElideMode() == Qt.ElideMiddle
    finally:
        short.close()
    long_path = "C:\\" + "あ" * 60 + "\\写真\\2026"
    wide = PickerWindow("フォルダ履歴", [(long_path, long_path)], lambda n, d: None)
    try:
        assert wide.width() > WINDOW_WIDTH, wide.width()
        screen_w = QGuiApplication.primaryScreen().availableGeometry().width()
        assert wide.width() <= int(screen_w * 0.9) + 1, wide.width()
    finally:
        wide.close()


def test_picker_input_guard_pure():
    """ピッカーの入力ガードの判定(純粋関数)。候補が0件になる編集だけを止める。

    候補の数え方は _refresh_list と同じ(前方一致・大小無視・前後の空白は無視)。
    すでに0件の入力からは止めない(詰まらせない)。純粋な削除はいつでも通す。"""
    from picker import accepts_input, filter_names, is_deletion

    names = ["Claude", "camera", "Club", "memo"]
    assert filter_names(names, "") == names
    assert filter_names(names, "c") == ["Claude", "camera", "Club"]
    assert filter_names(names, " CL ") == ["Claude", "Club"]   # strip + lower
    assert filter_names(names, "cr") == []

    # 候補が残る編集は通す / 0件になる編集は止める(work-board#15 の例)
    assert accepts_input("c", "cl", 3, 1) is True
    assert accepts_input("c", "cr", 3, 0) is False
    assert accepts_input("", "z", 4, 0) is False    # 最初の1文字でも同じ
    assert accepts_input("c", "cameraX", 3, 0) is False   # 貼り付けも丸ごと判定
    # 何も打っていない状態で候補が0件のときは何も無効にしない(決定事項6)
    assert accepts_input("", "z", 0, 0) is True
    assert accepts_input("ab", "abx", 0, 0) is True   # すでに0件なら止めない
    # 変更なし
    assert accepts_input("c", "c", 3, 3) is True
    # 純粋な削除(BackSpace / Delete 相当)はいつでも通す(決定事項3)
    assert accepts_input("cr", "c", 0, 3) is True
    assert accepts_input("ab", "a", 1, 0) is True
    assert is_deletion("abc", "ab") and is_deletion("abc", "ac")
    assert is_deletion("abc", "a") and is_deletion("abc", "")
    assert not is_deletion("abc", "ax") and not is_deletion("abc", "abcd")
    assert not is_deletion("abc", "abc")


def test_picker_rejects_input_to_zero():
    """ピッカー窓: 候補が0件になる打ち込みは入力欄が直前のままになり、
    一瞬だけ色で知らせる。候補が残る打ち込み・削除はそのまま通る。"""
    from picker import PickerWindow

    _qapp()
    items = [("claude", 1), ("camera", 2), ("club", 3), ("memo", 4)]
    picker = PickerWindow("フォルダブックマーク", items, lambda n, d: None)
    try:
        picker.search.setText("c")
        assert picker.search.text() == "c" and picker.list.count() == 3
        # r を足すと候補が0件 → 無効。入力は c のまま、色の合図が出る
        picker.search.setText("cr")
        assert picker.search.text() == "c", picker.search.text()
        assert picker.list.count() == 3
        assert picker.search.styleSheet() != ""
        picker._clear_search_flash()
        assert picker.search.styleSheet() == ""
        # 貼り付け(まとまった差し替え)も、結果が0件なら丸ごと無効
        picker.search.setText("cameraX")
        assert picker.search.text() == "c"
        # 候補が残る続きは通る
        picker.search.setText("cl")
        assert picker.search.text() == "cl" and picker.list.count() == 2
        # 削除は常に応じる
        picker.search.setText("c")
        assert picker.search.text() == "c" and picker.list.count() == 3
        picker.search.setText("")
        assert picker.search.text() == "" and picker.list.count() == 4
    finally:
        picker.close()


def test_picker_guard_counts_dynamic_items():
    """動的項目(打ったパスから作られる行)も候補に数える。パスの打ち込みが
    ブックマーク名の絞り込みに引っかかって止められてはいけない。"""
    from picker import PickerWindow

    _qapp()

    def dyn(text):
        return [("→ C:\\\\", ("jump", "C:\\\\"))] if text == "c:" else []

    picker = PickerWindow("フォルダブックマーク", [("claude", 1)],
                          lambda n, d: None, dynamic_items=dyn)
    try:
        picker.search.setText("c")
        assert picker.list.count() == 1
        # 静的な候補は0件になるが、動的項目が1件出るので通す
        picker.search.setText("c:")
        assert picker.search.text() == "c:", picker.search.text()
        assert picker.list.count() == 1
    finally:
        picker.close()


def test_phone_share_server():
    """スマホ送信のサーバ: 合言葉が無いと何も出ず、画像はページに
    そのまま表示され、ダウンロードは attachment で届く。"""
    import urllib.request
    import urllib.error
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        img = base / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        txt = base / "メモ.txt"
        txt.write_bytes("hello".encode("utf-8"))
        server = filer_phone.ShareServer(
            [str(img), str(txt), str(base / "gone.bin")], use_https=False)
        assert len(server.files) == 2       # 無いファイルは落ちる
        server.start()
        try:
            url = f"http://127.0.0.1:{server.port}/k/{server.token}/"
            page = urllib.request.urlopen(url).read().decode("utf-8")
            assert '<img src="f/0"' in page and page.count("<img") == 1
            assert "photo.png" in page and "ダウンロード" in page
            r = urllib.request.urlopen(url + "f/0")
            assert r.headers["Content-Type"] == "image/png"
            assert "inline" in r.headers["Content-Disposition"]
            assert r.read() == img.read_bytes()
            r = urllib.request.urlopen(url + "d/1")
            assert "attachment" in r.headers["Content-Disposition"]
            assert r.read() == "hello".encode("utf-8")
            for bad in (f"http://127.0.0.1:{server.port}/",
                        f"http://127.0.0.1:{server.port}/k/wrong/",
                        url + "f/9"):
                try:
                    urllib.request.urlopen(bad)
                    raise AssertionError(f"404のはず: {bad}")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
            kinds = [kind for kind, _name in server.pop_events()]
            assert kinds == ["view", "download"], kinds
        finally:
            server.stop()
    assert filer_phone.is_image("a.JPG") and not filer_phone.is_image("a.txt")


def _phone_ca_context(ca_dir, check_hostname=True):
    """生成したローカル CA だけを信頼する、テスト用のクライアント側 SSLContext。"""
    import ssl
    import filer_phone

    context = ssl.create_default_context(
        cafile=os.path.join(ca_dir, filer_phone.CA_CERT_FILE))
    context.check_hostname = check_hostname
    return context


def test_phone_ca_and_server_cert():
    """スマホ送信のローカル CA: 作れて、2回目は読み直す(作り直さない)。
    CA は名前制約(critical)付きの CA:TRUE、サーバ証明書は SAN に IP・EKU は
    serverAuth・期限は iOS の上限(825日)以下で、CA の署名が通る。"""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        ca_dir = os.path.join(tmp, "phone_ca")
        cert, key, created = filer_phone.ensure_ca(ca_dir, name_constraints=True)
        assert created
        assert sorted(os.listdir(ca_dir)) == [filer_phone.CA_CERT_FILE,
                                              filer_phone.CA_KEY_FILE]
        again, _key, created2 = filer_phone.ensure_ca(ca_dir, name_constraints=True)
        assert not created2 and again.serial_number == cert.serial_number
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
        assert bc.critical and bc.value.ca
        ku = cert.extensions.get_extension_for_class(x509.KeyUsage).value
        assert ku.key_cert_sign
        nc = cert.extensions.get_extension_for_class(x509.NameConstraints)
        assert nc.critical
        permitted = nc.value.permitted_subtrees
        nets = {str(g.value) for g in permitted if isinstance(g, x509.IPAddress)}
        assert nets == {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
        assert [g.value for g in permitted if isinstance(g, x509.DNSName)] == ["local"]
        days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
        assert days >= 3650
        assert filer_phone.ip_permitted(cert, "192.168.1.23")
        assert filer_phone.ip_permitted(cert, "10.1.2.3")
        assert not filer_phone.ip_permitted(cert, "127.0.0.1")
        assert not filer_phone.ip_permitted(cert, "8.8.8.8")

        server_cert, _server_key = filer_phone.issue_server_cert(
            cert, key, "192.168.1.23")
        san = server_cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value
        assert san.get_values_for_type(x509.IPAddress) == [
            ipaddress.ip_address("192.168.1.23")]
        eku = server_cert.extensions.get_extension_for_class(
            x509.ExtendedKeyUsage).value
        assert list(eku) == [ExtendedKeyUsageOID.SERVER_AUTH]
        assert not server_cert.extensions.get_extension_for_class(
            x509.BasicConstraints).value.ca
        life = server_cert.not_valid_after_utc - server_cert.not_valid_before_utc
        assert life <= datetime.timedelta(days=825)
        assert server_cert.issuer == cert.subject
        server_cert.verify_directly_issued_by(cert)   # 署名が CA のもの

        # 名前制約を外す設定(iOS でうまくいかなかったとき用)
        plain_dir = os.path.join(tmp, "plain_ca")
        plain, _k, _c = filer_phone.ensure_ca(plain_dir, name_constraints=False)
        try:
            plain.extensions.get_extension_for_class(x509.NameConstraints)
            raise AssertionError("名前制約は付かないはず")
        except x509.ExtensionNotFound:
            pass
        assert filer_phone.ip_permitted(plain, "127.0.0.1")

        # 壊れた CA は作り直す
        Path(ca_dir, filer_phone.CA_CERT_FILE).write_text("broken", encoding="utf-8")
        _cert, _key, created3 = filer_phone.ensure_ca(ca_dir)
        assert created3


def test_phone_https_share():
    """HTTPS で出すと、生成した CA を信頼すればページもファイルも取れる。
    ページには共有ボタンの処理が入る。証明書の入口(HTTP・別の合言葉)は
    CA の公開証明書だけを返し、鍵は出さない。合言葉が違えば 404。"""
    import ssl
    import urllib.error
    import urllib.request
    from cryptography import x509
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ca_dir = str(base / "phone_ca")
        img = base / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff" + b"x" * 200)
        txt = base / "メモ.txt"
        txt.write_bytes(b"hello")
        # 名前制約なしの CA で、127.0.0.1 宛てにホスト名の確認まで通す
        filer_phone.ensure_ca(ca_dir, name_constraints=False)
        server = filer_phone.ShareServer([str(img), str(txt)], ca_dir=ca_dir,
                                         host_ip="127.0.0.1")
        assert server.https, server.https_error
        assert server.url.startswith("https://127.0.0.1:")
        assert server.ca_url.startswith("http://127.0.0.1:")
        assert server.ca_token != server.token
        # サーバ証明書の一時ファイルは読んだらすぐ消えている
        assert sorted(os.listdir(ca_dir)) == [filer_phone.CA_CERT_FILE,
                                              filer_phone.CA_KEY_FILE]
        key_pem = Path(ca_dir, filer_phone.CA_KEY_FILE).read_bytes()
        ca_cert = x509.load_pem_x509_certificate(
            Path(ca_dir, filer_phone.CA_CERT_FILE).read_bytes())
        context = _phone_ca_context(ca_dir)
        server.start()
        try:
            page = urllib.request.urlopen(server.url, context=context).read().decode("utf-8")
            assert '<img src="f/0"' in page
            assert "navigator.share" in page and "navigator.canShare" in page
            assert "まとめて写真に保存（共有）" in page
            assert "var CHUNK = 10" in page
            assert '"name": "photo.jpg"' in page and "メモ.txt" in page
            assert '"image/jpeg"' in page and '"i": 1' not in page   # txt は共有に入れない
            r = urllib.request.urlopen(server.url + "f/0", context=context)
            assert r.headers["Content-Type"] == "image/jpeg"
            assert r.read() == img.read_bytes()
            r = urllib.request.urlopen(server.url + "d/1", context=context)
            assert "attachment" in r.headers["Content-Disposition"]
            assert r.read() == b"hello"
            https_root = f"https://127.0.0.1:{server.port}"
            for bad in (https_root + "/", https_root + "/k/wrong/",
                        f"{https_root}/k/{server.ca_token}/ca.crt", server.url + "f/9"):
                try:
                    urllib.request.urlopen(bad, context=context)
                    raise AssertionError(f"404のはず: {bad}")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
            # 信頼していない(既定の)コンテキストでは繋がらない
            try:
                urllib.request.urlopen(server.url, context=ssl.create_default_context())
                raise AssertionError("証明書の検証で失敗するはず")
            except urllib.error.URLError as e:
                assert isinstance(e.reason, ssl.SSLCertVerificationError), e

            # 証明書の入口: 案内ページと CA の公開証明書(DER)だけ
            guide = urllib.request.urlopen(server.ca_url).read().decode("utf-8")
            assert 'href="ca.crt"' in guide and "証明書信頼設定" in guide
            r = urllib.request.urlopen(server.ca_url + "ca.crt")
            assert r.headers["Content-Type"] == "application/x-x509-ca-cert"
            body = r.read()
            assert x509.load_der_x509_certificate(body) == ca_cert
            assert b"PRIVATE KEY" not in body and key_pem not in body
            ca_root = f"http://127.0.0.1:{server.ca_port}"
            for bad in (ca_root + "/", ca_root + "/k/wrong/ca.crt",
                        f"{ca_root}/k/{server.token}/",          # 本体の合言葉は効かない
                        f"{ca_root}/k/{server.token}/f/0",
                        server.ca_url + filer_phone.CA_KEY_FILE,
                        server.ca_url + "../" + filer_phone.CA_KEY_FILE):
                try:
                    urllib.request.urlopen(bad)
                    raise AssertionError(f"404のはず: {bad}")
                except urllib.error.HTTPError as e:
                    assert e.code == 404
            kinds = [kind for kind, _name in server.pop_events()]
            assert kinds == ["view", "download", "ca"], kinds
        finally:
            server.stop()


def test_phone_https_name_constraints():
    """名前制約付きの CA: LAN の IP の証明書は OpenSSL の検証が通り、範囲外
    (127.0.0.1)の証明書は CA の制約で弾かれる。範囲外の IP では HTTP に戻る。"""
    import ssl
    import urllib.error
    import urllib.request
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ca_dir = str(base / "phone_ca")
        img = base / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
        filer_phone.ensure_ca(ca_dir, name_constraints=True)
        # 範囲外の IP では HTTPS にしない(iPhone で必ず弾かれるため)
        fallback = filer_phone.ShareServer([str(img)], ca_dir=ca_dir,
                                           host_ip="127.0.0.1")
        try:
            assert not fallback.https and "範囲外" in fallback.https_error
            assert fallback.url.startswith("http://") and fallback.ca_url is None
        finally:
            fallback.stop()
        # LAN の IP の証明書。待ち受けは 0.0.0.0 なので 127.0.0.1 から繋ぎ、
        # ホスト名の確認だけ外して、鎖と名前制約の検証を通す
        server = filer_phone.ShareServer([str(img)], ca_dir=ca_dir,
                                         host_ip="192.168.1.23")
        assert server.https, server.https_error
        server.start()
        try:
            local = f"https://127.0.0.1:{server.port}/k/{server.token}/f/0"
            body = urllib.request.urlopen(
                local, context=_phone_ca_context(ca_dir, check_hostname=False)).read()
            assert body == img.read_bytes()
            # 範囲外の IP の証明書に差し替えると、CA の名前制約で検証に失敗する
            ca_cert, ca_key, _created = filer_phone.ensure_ca(ca_dir)
            bad_cert, bad_key = filer_phone.issue_server_cert(ca_cert, ca_key, "127.0.0.1")
            server._httpd.ssl_context = filer_phone.server_ssl_context(
                bad_cert, bad_key, ca_dir)
            try:
                urllib.request.urlopen(
                    local, context=_phone_ca_context(ca_dir, check_hostname=False))
                raise AssertionError("名前制約で弾かれるはず")
            except urllib.error.URLError as e:
                assert isinstance(e.reason, ssl.SSLCertVerificationError), e
        finally:
            server.stop()


def test_phone_share_page_split():
    """件数が多いと分けて共有するボタンになる。共有できる種類が無ければ
    共有の処理は入れず、長押しの案内だけ。"""
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        paths = []
        for n in range(25):
            p = base / f"img{n:02d}.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            paths.append(str(p))
        movie = base / "clip.mov"
        movie.write_bytes(b"\x00" * 10)
        paths.append(str(movie))
        server = filer_phone.ShareServer(paths, use_https=False)
        try:
            page = server._index_html().decode("utf-8")
        finally:
            server.stop()
        assert page.count('"type": "image/png"') == 25
        assert '"type": "video/quicktime"' in page          # 動画も共有に入る
        assert "var CHUNK = 10, ALL_MAX = 30" in page
        assert "件を共有" in page and "MEDIA.slice(start, start + CHUNK)" in page
        assert "NotAllowedError" in page and "AbortError" in page
        assert "window.isSecureContext" in page
        assert "長押しすると「写真に追加」" in page          # 使えないときの案内
        assert '<div id="share" hidden>' in page

        note = base / "note.txt"
        note.write_bytes(b"x")
        server = filer_phone.ShareServer([str(note)], use_https=False)
        try:
            page = server._index_html().decode("utf-8")
        finally:
            server.stop()
        assert "<script>" not in page and "長押しすると" in page


def test_phone_https_fallback():
    """cryptography が無い・証明書が作れないときは、これまでどおり HTTP で出す。"""
    import urllib.request
    import filer_phone

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        img = base / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        ca_dir = str(base / "phone_ca")
        saved = {k: v for k, v in sys.modules.items()
                 if k == "cryptography" or k.startswith("cryptography.")}
        for k in saved:
            del sys.modules[k]
        sys.modules["cryptography"] = None      # import すると ImportError になる
        try:
            server = filer_phone.ShareServer([str(img)], ca_dir=ca_dir,
                                             host_ip="127.0.0.1")
        finally:
            del sys.modules["cryptography"]
            sys.modules.update(saved)
        try:
            assert not server.https and "cryptography" in server.https_error
            assert server.url.startswith("http://127.0.0.1:")
            assert server.ca_url is None and server.ca_port is None
            assert not os.path.exists(ca_dir)        # 何も作らない
            server.start()
            page = urllib.request.urlopen(server.url).read().decode("utf-8")
            assert '<img src="f/0"' in page
        finally:
            server.stop()

        # CA の置き場所がファイルで塞がっている(作れない)ときも HTTP
        blocked = base / "blocked"
        blocked.write_bytes(b"x")
        server = filer_phone.ShareServer([str(img)], ca_dir=str(blocked),
                                         host_ip="192.168.1.23")
        try:
            assert not server.https and "証明書を用意できませんでした" in server.https_error
            assert server.url.startswith("http://")
        finally:
            server.stop()


def test_phone_menu_gating():
    """スマホ送信の項目は「環境1」のときだけ複写メニューに出る。"""
    import filer

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        section = {"left_path": str(left), "right_path": str(right), "editor": "x"}
        seen = {}

        def capture(title, items):
            seen["items"] = items
            return None

        for app_settings, expected in (
                ({"environment": 1, "filer": section}, True),
                ({"environment": 2, "filer": section}, False),
                ({"filer": section}, False)):
            w = filer.FilerWindow(app_settings, str(settings_path))
            try:
                w.show()
                w.start()
                _wait(w)
                assert w.phone_enabled is expected
                w.choose_menu = capture
                w.left.set_cursor(w.left._row_of("alpha.txt"))
                w.cmd_copy_menu()
                values = [item[2] for item in seen["items"]]
                assert ("phone" in values) is expected, values
            finally:
                w.close()
                w.deleteLater()


def test_desktop_path():
    """デスクトップのパスが取れる(N キーの移動先)。

    filer_shell の _guid / GUID の型が噛み合わないと ArgumentError で
    落ちる(タスクバー identity 用の同名定義が巻き込んだ事故の回帰防止)。"""
    import filer_shell
    path = filer_shell.desktop_path()
    assert path and Path(path).is_dir(), path


def test_ext_column_fits_long_ext():
    """長い拡張子(.safetensors 等)は … で省かず、列を広げて全部見せる。

    あふｗに合わせた表示(2026-10-27 ユーザー要望)。列の幅はそのフォルダで
    一番長い拡張子に追随し、短い拡張子だけのフォルダでは従来の幅に戻る。"""
    import filer
    from PySide6.QtGui import QFontMetrics

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        (left / "model.safetensors").write_bytes(b"x")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        try:
            L, R = w.left, w.right
            fm = QFontMetrics(L.pane_font)
            need = fm.horizontalAdvance(".safetensors") + 8
            assert L._col_widths[filer.COL_EXT] >= need, L._col_widths
            assert L.view.columnWidth(filer.COL_EXT) >= need
            base = fm.horizontalAdvance(".MMMMM") + 8
            assert R._col_widths[filer.COL_EXT] == base   # 右は従来の幅のまま
        finally:
            w.close()
            w.deleteLater()


def test_folder_signature():
    """自動監視の指紋: 触らなければ同じ、追加・サイズ変更・改名・削除で変わる。"""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "a.txt").write_text("aa", encoding="utf-8")
        (base / "sub").mkdir()
        s1 = core.folder_signature(str(base))
        assert s1 == core.folder_signature(str(base))
        assert len(s1) == 2
        (base / "b.txt").write_text("b", encoding="utf-8")
        s2 = core.folder_signature(str(base))
        assert s2 != s1                       # 追加
        (base / "a.txt").write_text("aaaa", encoding="utf-8")
        s3 = core.folder_signature(str(base))
        assert s3 != s2                       # サイズ変更
        (base / "b.txt").rename(base / "c.txt")
        s4 = core.folder_signature(str(base))
        assert s4 != s3                       # 改名
        (base / "c.txt").unlink()
        assert core.folder_signature(str(base)) != s4   # 削除
        try:
            core.folder_signature(str(base / "nope"))
            raise AssertionError("無いフォルダは例外のはず")
        except OSError:
            pass


def test_stamp_prefix():
    """フォルダ作成の日時プレフィックス: YYYYMMDD_hhmm_ の形。"""
    from datetime import datetime
    assert core.stamp_prefix(datetime(2026, 11, 5, 15, 48)) == "20261105_1548_"
    assert core.stamp_prefix(datetime(2026, 1, 2, 3, 4)) == "20260102_0304_"


def test_cp_name():
    """_cp 付与の名前: 拡張子の前に差し込む。無ければ末尾。"""
    assert core.cp_name("alpha.txt") == "alpha_cp.txt"
    assert core.cp_name("beta") == "beta_cp"
    assert core.cp_name("a.b.txt") == "a.b_cp.txt"
    assert core.cp_name(".gitignore") == ".gitignore_cp"
    assert core.cp_name("alpha_cp.txt") == "alpha_cp_cp.txt"


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


def test_is_network_path():
    """ネットワークの場所の判定: UNC は即ネットワーク。ローカルの
    ドライブはネットワークではない(ドライブ種別で見る)。"""
    import filer_shell as sh

    assert sh.is_network_path("\\\\server\\share\\dir") is True
    assert sh.is_network_path("") is False
    assert sh.is_network_path(None) is False
    import os
    local_root = os.path.splitdrive(os.path.abspath(os.sep))[0] + "\\"
    assert sh.is_network_path(local_root) is False


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
        # 名前を変えたコピー(FOF_MULTIDESTFILES)。元は残る
        assert sh.copy_files_named([str(src / "日本語 b.txt")], [str(dst / "renamed b.txt")],
                                   0, _quiet_flags()) == (0, False)
        assert (dst / "renamed b.txt").exists() and (src / "日本語 b.txt").exists()
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
        # 転送キューが残っている間も待つ(転送は裏のスレッドで走り、完了で
        # 両窓が再読込される。読み込みだけ見ると完了前に戻ってしまう)。
        busy = (window.left.loading or window.right.loading
                or getattr(window, "_op_running", None) is not None
                or bool(getattr(window, "_op_queue", None)))
        if not busy:
            QTest.qWait(10)
            return
    raise AssertionError("一覧の読み込みが終わらない")


def _names(pane):
    return [e.name for e in pane.entries]


def _cursor_name(pane):
    entry = pane.cursor_entry()
    return entry.name if entry else None


def _pick_menu(prefix):
    """_exec_menu の差し替え: 表示が prefix で始まる項目を選んだことにする(exec はモーダルで
    止まるので使わない)。"""
    def run(menu, chosen):
        for action, value in chosen.items():
            if action.text().startswith(prefix):
                return value
        raise AssertionError(f"項目が無い: {prefix} / {[a.text() for a in chosen]}")
    return run


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
        key(Qt.Key_Delete, Qt.ShiftModifier)    # 完全削除も既定ボタンは「はい」
        _wait(w)
        assert asked["default_yes"] is True and not (left / "photo.JPG").exists()

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


def test_grep_file_large():
    """巨大ファイル用のチャンク読み検索。小さい chunk_size で継ぎ目を踏ませ、
    grep_in_bytes と同じ数え方になることを確かめる。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "big.log")

        def run(data: bytes, needle: str, case_sensitive=False, chunk=16):
            path.write_bytes(data)
            return core.grep_file_large(str(path), needle, case_sensitive,
                                        chunk_size=chunk)

        data = b"Hello World\nsecond HELLO line\nnothing here\nlast hello\n"
        expect = core.grep_in_bytes(data, "hello")
        assert run(data, "hello") == expect == (3, "Hello World")
        assert run(data, "HELLO", case_sensitive=True) == \
            core.grep_in_bytes(data, "HELLO", True) == (1, "second HELLO line")
        assert run(data, "absent") is None
        # 同じ行に2回出ても1行
        assert run(b"hello hello\n", "hello") == (1, "hello hello")
        # 針がチャンクの継ぎ目をまたぐ(13 バイト目から始めて 16 の境界を越える)
        seam = b"x" * 13 + b"NEEDLE\n" + b"y" * 40 + b"\nneedle again\n"
        assert run(seam, "needle") == (2, "x" * 13 + "NEEDLE")
        # 行がチャンクより長い
        long_line = b"a" * 100 + b" hello " + b"b" * 100 + b"\nshort hello\n"
        assert run(long_line, "hello") == (2, long_line.split(b"\n")[0].decode()[:200])
        # CRLF の行でも数える
        assert run(b"one hello\r\ntwo\r\nthree hello\r\n", "hello") == (2, "one hello")
        # cp932 の日本語(大小区別なし・あり)
        jp = "テスト HELLO\n次のテスト\n".encode("cp932")
        assert run(jp, "テスト") == core.grep_in_bytes(jp, "テスト") == (2, "テスト HELLO")
        assert run(jp, "hello") == (1, "テスト HELLO")
        # バイナリ(どちらの文字コードでも読めない)はバイト列で数える
        binary = b"\xff\xfe hello \x80\x81\n\xff hello\n"
        assert run(binary, "hello") == (2, "")
        # 末尾に改行が無い行
        assert run(b"tail hello", "hello") == (1, "tail hello")
        # 中止したら None(結果なし)
        import threading
        cancel = threading.Event()
        cancel.set()
        path.write_bytes(b"hello\n" * 100)
        assert core.grep_file_large(str(path), "hello", cancel=cancel,
                                    chunk_size=16) is None


def test_grep_search():
    """G: 文字列検索。core の照合(UTF-8/cp932・大小区別・中止)と、窓側で
    結果がフラット表示の形(相対パス)で並び、通常表示へ戻れること。"""
    import filer
    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "sub").mkdir()
        (base / "a.txt").write_text("Hello World\nsecond HELLO line\n", encoding="utf-8")
        (base / "sub" / "b.txt").write_bytes("テスト hello\n".encode("cp932"))
        (base / "c.txt").write_text("nothing\n", encoding="utf-8")

        entries, matches, status = core.grep_files(str(base), "hello")
        assert status == "done"
        names = {e.name for e in entries}
        assert names == {"a.txt", str(Path("sub") / "b.txt")}, names
        assert matches["a.txt"] == (2, "Hello World")
        entries, matches, _ = core.grep_files(str(base), "HELLO", case_sensitive=True)
        assert matches["a.txt"][0] == 1 and str(Path("sub") / "b.txt") not in matches

        win = filer.FilerWindow({}, base / "settings.json")
        try:
            win.left.load(str(base)); win.right.load(str(base))
            _wait(win)
            win.ask_text = lambda title, label, text="", select=None: "hello"
            win.cmd_grep()
            _wait(win)
            pane = win.left
            assert pane.grep_query == "hello" and pane.flat
            shown = {e.name for e in pane.entries if not e.is_parent}
            assert shown == {"a.txt", str(Path("sub") / "b.txt")}, shown
            assert "文字列検索" in win.log_view.toPlainText()
            assert "2件" in win.log_view.toPlainText()
            # Alt+B で通常表示へ戻る
            win.cmd_flat_view()
            _wait(win)
            assert pane.grep_query is None and not pane.flat
        finally:
            win.close()


def test_preload_modules():
    """起動後の先読みで launcher / picker が読み込み済みになり、初回の J が重くならない。"""
    import sys

    import filer
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
            w._preload_modules()
            assert w._preloaded
            assert "launcher" in sys.modules and "picker" in sys.modules
            w._preload_modules()    # 二度呼んでも何もしない
            # 先読み後も J のピッカーは通常どおり開く
            w.run_command("bookmark_jump")
            QTest.qWait(30)
            assert w._picker is not None
            w._picker.close()
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
        # 先頭は一時置き場の行(仕様)。ブックマークはその次から並ぶ
        assert names[0].startswith("🧺 一時置き場"), names
        assert names[1:3] == ["A", "B"], names
        assert names[3].startswith(launcher.ADD_ITEM_PREFIX) and str(left) in names[3], names

        # 「ここを登録」
        original = launcher._ask_name
        launcher._ask_name = lambda parent, default: default + "_new"
        try:
            p._on_accept(*p._items[3])
        finally:
            launcher._ask_name = original
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["A", "B", os.path.basename(str(left)) + "_new"]
        assert saved["hotkeys"] == {"x": "y"} and saved["filer"] == {"editor": "x"}

        # 並べ替え(B を上へ)と削除(A)。先頭の一時置き場の行は動かない
        w.run_command("bookmark_jump")
        p = w._picker
        p._on_move(*p._items[2], -1)
        assert [n for n, _ in p._items][1:3] == ["B", "A"]
        w.confirm = lambda *a, **k: True
        # 削除の控えは本物の bookmark_deleted.log ではなく一時フォルダへ
        original_log = launcher.DELETED_LOG_PATH
        launcher.DELETED_LOG_PATH = str(Path(tmp, "bookmark_deleted.log"))
        try:
            p._on_delete(*p._items[2])
        finally:
            launcher.DELETED_LOG_PATH = original_log
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == \
            ["B", os.path.basename(str(left)) + "_new"]
        p.close()

        # 登録済みのフォルダで「ここを登録」→ 増やさず、知らせて、その行を選んだまま開けておく
        new_name = os.path.basename(str(left)) + "_new"
        w.run_command("bookmark_jump")
        p = w._picker
        add_item = next(e for e in p._items if e[1][0] == launcher.KIND_ADD)
        asked = []
        original = launcher._ask_name
        launcher._ask_name = lambda parent, default: asked.append(default) or "dup"
        try:
            assert p.select_data(add_item[1])
            p._commit()
        finally:
            launcher._ask_name = original
        assert asked == [] and p.isVisible() and w._picker is p
        assert p._entry_at(p.list.currentItem()) == (new_name, (launcher.KIND_JUMP, str(left)))
        assert "登録済みです" in w.log_view.toPlainText()
        saved = json.loads(settings_path.read_text(encoding="utf-8"))
        assert [b["name"] for b in saved["launcher"]["bookmarks"]] == ["B", new_name]
        p.close()
        # 大文字小文字・/ と \・末尾の \ の違いは同じフォルダとみなす
        found = launcher.find_bookmark_by_path(saved, str(left).upper().replace("\\", "/") + "/")
        assert found is not None and found["name"] == new_name
        assert launcher.find_bookmark_by_path(saved, str(left / "sub2")) is None
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


def test_archive_tar_aes_whole():
    import io
    import tarfile
    import filer_archive as fa
    with tempfile.TemporaryDirectory() as tmp:
        # tar.gz: 一覧も展開も tarfile で(書庫として開く閲覧も同じ索引を通る)
        tpath = os.path.join(tmp, "bundle.tar.gz")
        with tarfile.open(tpath, "w:gz") as tf:
            for name, data in (("pkg/a.txt", b"A"), ("pkg/sub/b.txt", b"B")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        assert fa.archive_kind(tpath) == "tar" and fa.is_archive_name("x.TGZ")
        index = fa.get_index(tpath)
        assert index.kind == "tar"
        assert [e.name for e in index.list_dir("")] == ["pkg"]
        assert sorted(e.name for e in index.list_dir("pkg")) == ["a.txt", "sub"]
        out = os.path.join(tmp, "out")
        os.mkdir(out)
        result = fa.extract(index, fa.plan_extract(index, "", ["pkg"]), out)
        assert result.written == 2 and not result.errors, result
        assert Path(out, "pkg", "sub", "b.txt").read_bytes() == b"B"
        # tar(無圧縮)も
        ppath = os.path.join(tmp, "plain.tar")
        with tarfile.open(ppath, "w") as tf:
            info = tarfile.TarInfo("one.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"1"))
        pindex = fa.get_index(ppath)
        assert [e.name for e in pindex.list_dir("")] == ["one.txt"]

        # 書庫名のフォルダ名(複合拡張子はまとめて除く)
        assert fa.archive_stem("a.tar.gz") == "a" and fa.archive_stem("a.tgz") == "a"
        assert fa.archive_stem("a.tar.bz2") == "a" and fa.archive_stem("a.zip") == "a"
        # whole_plan: 単一フォルダだけならそのまま(単位はそのフォルダ)
        plan, unit = fa.whole_plan(index, "bundle.tar.gz")
        assert unit == "pkg"
        assert sorted(p.rel for p in plan if not p.is_dir) == ["pkg/a.txt", "pkg/sub/b.txt"]
        # バラなら書庫名のフォルダが単位
        zpath = os.path.join(tmp, "loose.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("x.txt", b"x")
            z.writestr("y.txt", b"y")
        plan, unit = fa.whole_plan(fa.get_index(zpath), "loose.zip")
        assert unit == "loose" and sorted(p.rel for p in plan) == ["loose/x.txt", "loose/y.txt"]
        # 単一ファイルだけでもフォルダを作る側
        spath = os.path.join(tmp, "single.zip")
        with zipfile.ZipFile(spath, "w") as z:
            z.writestr("only.txt", b"o")
        plan, unit = fa.whole_plan(fa.get_index(spath), "single.zip")
        assert unit == "single" and [p.rel for p in plan] == ["single/only.txt"]

        # AES 形式の ZIP(pyzipper): 一覧・要パスワード判定・展開・違いの報告
        import pyzipper
        apath = os.path.join(tmp, "aes.zip")
        with pyzipper.AESZipFile(apath, "w", compression=pyzipper.ZIP_DEFLATED,
                                 encryption=pyzipper.WZ_AES) as z:
            z.setpassword(b"pw-123")
            z.writestr("secret.txt", b"topsecret")
        aindex = fa.get_index(apath)
        aplan = fa.plan_extract(aindex, "", ["secret.txt"])
        assert fa.needs_password(aindex, aplan)
        ok_out = os.path.join(tmp, "aes_out")
        os.mkdir(ok_out)
        result = fa.extract(aindex, aplan, ok_out, password="pw-123")
        assert result.written == 1, result
        assert Path(ok_out, "secret.txt").read_bytes() == b"topsecret"
        result = fa.extract(aindex, aplan, os.path.join(tmp, "aes_ng"), password="wrong")
        assert result.written == 0 and result.errors, result
        assert all("パスワード" in e for e in result.errors), result.errors

        # ファイル名まで暗号化された 7z: パスワード無しでは一覧から PasswordRequired、
        # パスワードを渡せば一覧も展開もできる(U の入口が使う経路)
        import py7zr
        hpath = os.path.join(tmp, "hdr.7z")
        with py7zr.SevenZipFile(hpath, "w", password="pw-9", header_encryption=True) as z:
            z.writestr(b"secret", "s.txt")
        try:
            fa.get_index(hpath)
            raise AssertionError("パスワード無しで一覧が出てしまった")
        except fa.PasswordRequired:
            pass
        hindex = fa.get_index(hpath, password="pw-9")
        assert [e.name for e in hindex.list_dir("")] == ["s.txt"]
        hout = os.path.join(tmp, "hdr_out")
        os.mkdir(hout)
        result = fa.extract(hindex, fa.plan_extract(hindex, "", ["s.txt"]), hout,
                            password="pw-9")
        assert result.written == 1 and Path(hout, "s.txt").read_bytes() == b"secret"


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
        w.ask_overwrite = lambda rel, remaining, **_: (asked.append(rel), (filer.SKIP, False))[1]
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
        w.ask_overwrite = lambda rel, remaining, **_: (asked.append(rel), (filer.OVERWRITE, True))[1]
        L.marks.update({"readme.txt", "docs"})
        key(Qt.Key_C, Qt.AltModifier)
        _wait_extract(w)
        _wait(w)
        assert len(asked) == 1 and Path(right, "readme.txt").read_bytes() == b"hello"
        # 取消なら何もしない
        Path(right, "readme.txt").write_bytes(b"old")
        w.ask_overwrite = lambda rel, remaining, **_: (filer.CANCEL, False)
        L.set_cursor(L._row_of("readme.txt"))
        key(Qt.Key_C, Qt.AltModifier)
        assert w._extracting is None and Path(right, "readme.txt").read_bytes() == b"old"
        assert not Path(tmp, "evil.txt").exists() and not Path(right, "evil.txt").exists()

        # Ctrl+Enter のメニューの「関連付けで開く」(&OPEN) / E は一時フォルダへ取り出して開く
        opened = []
        # 書庫から取り出して開くものは履歴に残さない(record=False)ので、その引数も受ける
        w._startfile = lambda path, **_kw: opened.append(path)
        w._exec_menu = _pick_menu("&O:")
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


def test_window_extract_archive_u():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        with zipfile.ZipFile(str(left / "single.zip"), "w") as z:
            z.writestr("inner/a.txt", b"ia")
            z.writestr("inner/b.txt", b"ib")
        _make_zip(str(left / "pack.zip"))   # トップがバラ(フォルダ+ファイル)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "ed"}}, str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        w.choose_menu = lambda title, items: "other"

        # 単一フォルダの書庫は、そのまま反対窓へ
        L.set_cursor(L._row_of("single.zip"))
        key(Qt.Key_U)
        _wait_extract(w)
        _wait(w)
        assert Path(right, "inner", "a.txt").read_bytes() == b"ia"
        assert Path(right, "inner", "b.txt").read_bytes() == b"ib"

        # 単位フォルダが既にある: いいえでスキップ、はいで上書き
        confirms = []
        w.confirm = lambda title, text, default_yes=True, **_: confirms.append(text) or False
        L.set_cursor(L._row_of("single.zip"))
        key(Qt.Key_U)
        _wait(w)
        assert confirms and Path(right, "inner", "a.txt").read_bytes() == b"ia"
        w.confirm = lambda title, text, default_yes=True, **_: True
        Path(right, "inner", "a.txt").write_bytes(b"changed")
        L.set_cursor(L._row_of("single.zip"))
        key(Qt.Key_U)
        _wait_extract(w)
        _wait(w)
        assert Path(right, "inner", "a.txt").read_bytes() == b"ia"

        # バラの書庫は書庫名のフォルダの中へ
        L.set_cursor(L._row_of("pack.zip"))
        key(Qt.Key_U)
        _wait_extract(w)
        _wait(w)
        assert Path(right, "pack", "docs", "sub", "deep.txt").read_bytes() == b"deep"

        # この窓へも展開できる
        w.choose_menu = lambda title, items: "here"
        L.set_cursor(L._row_of("single.zip"))
        key(Qt.Key_U)
        _wait_extract(w)
        _wait(w)
        assert Path(left, "inner", "b.txt").read_bytes() == b"ib"


def test_ask_overwrite_defaults():
    """展開の上書き確認: 既定ボタンは「上書き」、「残りも同じ」は最初から ON。"""
    import filer
    from PySide6.QtWidgets import QMessageBox

    _qapp()
    seen = {}
    original_exec = QMessageBox.exec

    def fake_exec(box):
        check = box.checkBox()
        seen["checked"] = check.isChecked() if check is not None else None
        seen["default"] = box.defaultButton().text()
        box.defaultButton().click()
        return box.result()

    QMessageBox.exec = fake_exec
    try:
        decision, same = filer.FilerWindow.ask_overwrite(None, "a.txt", 3)
    finally:
        QMessageBox.exec = original_exec
    assert seen["default"].startswith("上書き"), seen
    assert seen["checked"] is True and same is True
    assert decision == filer.OVERWRITE


def test_extract_conflicts_compare():
    """展開の上書き確認: 大きさと日時が同じものは尋ねずに飛ばし、違うものは日時・大きさを
    並べて新しいほうに印を付ける。残りのうち今あるもののほうが新しい件数も出す。"""
    import filer
    import filer_archive as fa
    from PySide6.QtWidgets import QMessageBox

    with tempfile.TemporaryDirectory() as tmp:
        zpath = Path(tmp, "a.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            for name, data in (("same.txt", b"same"), ("newer.txt", b"zip"),
                               ("older.txt", b"zip-older"), ("size.txt", b"12345")):
                info = zipfile.ZipInfo(name, date_time=(2026, 11, 1, 12, 0, 0))
                z.writestr(info, data)
        index = fa.get_index(str(zpath))
        plan = fa.plan_extract(index, "", ["same.txt", "newer.txt", "older.txt", "size.txt"])
        out = Path(tmp, "out")
        out.mkdir()
        stamp = index.members["same.txt"].mtime
        for name, data, mtime in (("same.txt", b"same", stamp + 1),     # 2秒以内は同じ
                                  ("newer.txt", b"local", stamp + 86400),
                                  ("older.txt", b"zip-older", stamp - 86400),
                                  ("size.txt", b"1234", stamp)):
            Path(out, name).write_bytes(data)
            os.utime(Path(out, name), (mtime, mtime))
        got = {c.rel: c for c in fa.describe_conflicts(index, plan, str(out))}
        assert set(got) == {"same.txt", "newer.txt", "older.txt", "size.txt"}
        assert got["same.txt"].same and not got["same.txt"].existing_newer
        assert not got["newer.txt"].same and got["newer.txt"].existing_newer
        assert not got["older.txt"].same and not got["older.txt"].existing_newer
        assert not got["size.txt"].same          # 日時が同じでも大きさが違えば尋ねる
        text = got["newer.txt"].describe()
        assert "今あるもの" in text.splitlines()[1] and "こちらが新しい" in text.splitlines()[1]
        assert "こちらが新しい" in got["older.txt"].describe().splitlines()[0]

        # ダイアログ: 日時・大きさと警告、残りの「新しい」件数が出る
        _qapp()
        seen = {}
        original_exec = QMessageBox.exec

        def fake_exec(box):
            seen["text"] = box.text()
            seen["check"] = box.checkBox().text()
            box.defaultButton().click()
            return box.result()

        QMessageBox.exec = fake_exec
        try:
            filer.FilerWindow.ask_overwrite(None, "newer.txt", 2, conflict=got["newer.txt"],
                                            rest_newer=1)
        finally:
            QMessageBox.exec = original_exec
        assert "今あるもののほうが新しい" in seen["text"], seen
        assert "うち 1件は今あるもののほうが新しい" in seen["check"], seen


def test_zip_update_logic():
    """ZIP 更新: 検証・計画(manifest 方式の削除)・控え・適用・pip の要否。"""
    import filer_zipupdate as zu

    def make_zip(path, files, top="tray-tools-main", when=(2026, 11, 4, 10, 0, 0)):
        with zipfile.ZipFile(path, "w") as zf:
            for rel, data in files.items():
                zf.writestr(zipfile.ZipInfo(f"{top}/{rel}", date_time=when), data)
        return path

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp, "repo")
        repo.mkdir()
        store = Path(tmp, "store")
        (repo / "settings.json").write_text("mine", encoding="utf-8")
        (repo / "my_tool.py").write_text("user file", encoding="utf-8")
        (repo / "filer.py").write_text("old filer", encoding="utf-8")

        v1 = make_zip(Path(tmp, "v1.zip"), {
            "main.py": "main", "filer.py": "filer v1",
            "requirements.txt": "req1", "photometa/__init__.py": ""})
        info = zu.inspect_archive(v1)
        assert info["label"] == "ZIP 20261104" and info["top"] == "tray-tools-main"
        assert set(info["entries"]) == {"main.py", "filer.py", "requirements.txt",
                                        "photometa/__init__.py"}
        # 検証: 目印が無い ZIP / 外へ出るエントリ / 最上位が2つ は弾く
        for bad_files in ({"main.py": "x", "requirements.txt": "y"},
                          {"../evil.py": "x", "main.py": "m", "filer.py": "f",
                           "requirements.txt": "r"}):
            bad = make_zip(Path(tmp, "bad.zip"), bad_files)
            try:
                zu.inspect_archive(bad)
                raise AssertionError("弾くはず")
            except zu.ZipUpdateError:
                pass
        two_tops = Path(tmp, "two.zip")
        with zipfile.ZipFile(two_tops, "w") as zf:
            zf.writestr("a/main.py", "x")
            zf.writestr("b/filer.py", "y")
        try:
            zu.inspect_archive(two_tops)
            raise AssertionError("弾くはず")
        except zu.ZipUpdateError:
            pass

        # 初回: manifest が無いので削除なし。requirements は追加なので pip 対象
        prepared = zu.check(repo, v1, store_dir=store)
        assert prepared["state"] == "ready" and not prepared["manifest_present"]
        plan = prepared["plan"]
        assert plan["add"] == ["main.py", "photometa/__init__.py", "requirements.txt"]
        assert plan["overwrite"] == ["filer.py"] and plan["delete"] == []
        assert plan["requirements_changed"]
        pip_calls = []
        result = zu.apply(prepared, repo, store_dir=store,
                          run=lambda args, timeout: (pip_calls.append(args), (0, "", ""))[1])
        assert result["ok"] and len(pip_calls) == 1
        assert (repo / "filer.py").read_text(encoding="utf-8") == "filer v1"
        assert (repo / "settings.json").read_text(encoding="utf-8") == "mine"
        assert (repo / "my_tool.py").read_text(encoding="utf-8") == "user file"
        # 上書きされた filer.py の控えが取れている
        assert result["backup"] and zipfile.ZipFile(result["backup"]).read(
            "filer.py") == b"old filer"
        assert zu.load_manifest(store)["files"] == sorted(info["entries"])
        # 同じ ZIP では最新
        assert zu.check(repo, v1, store_dir=store)["state"] == "uptodate"

        # v2: filer.py 変更・newmod.py 追加・photometa/__init__.py 削除(上流で消えた)
        v2 = make_zip(Path(tmp, "v2.zip"), {
            "main.py": "main", "filer.py": "filer v2",
            "requirements.txt": "req1", "newmod.py": "new"})
        prepared = zu.check(repo, v2, store_dir=store)
        plan = prepared["plan"]
        assert plan["add"] == ["newmod.py"] and plan["overwrite"] == ["filer.py"]
        assert plan["delete"] == ["photometa/__init__.py"]
        assert not plan["requirements_changed"]
        result = zu.apply(prepared, repo, store_dir=store,
                          run=lambda args, timeout: (pip_calls.append(args), (0, "", ""))[1])
        assert result["ok"] and len(pip_calls) == 1     # pip は増えない
        assert not (repo / "photometa").exists()        # 空になったフォルダも消える
        assert (repo / "newmod.py").read_text(encoding="utf-8") == "new"
        backup = zipfile.ZipFile(result["backup"])
        assert backup.read("filer.py") == b"filer v1"
        assert backup.read("photometa/__init__.py") == b""
        assert zu.check(repo, v2, store_dir=store)["state"] == "uptodate"

        # ダウンロードフォルダから探す: 名前ではなく更新日時が一番新しいもの
        downloads = Path(tmp, "Downloads")
        downloads.mkdir()
        assert zu.find_downloaded_zip(downloads) is None
        older = downloads / "tray-tools-main.zip"
        older.write_bytes(b"x")
        os.utime(older, (1_700_000_000, 1_700_000_000))
        newer = downloads / "tray-tools-main (1).zip"
        newer.write_bytes(b"y")
        os.utime(newer, (1_800_000_000, 1_800_000_000))
        (downloads / "tray-tools-main.txt").write_bytes(b"z")
        (downloads / "other.zip").write_bytes(b"z")
        assert zu.find_downloaded_zip(downloads) == newer

        # 大文字小文字だけ違う名前は同じファイルとして扱い、削除で消さない
        # (一覧に NewMod.py、新しい ZIP に newmod.py があるケース)
        case_zip = make_zip(Path(tmp, "case.zip"), {
            "main.py": "main", "filer.py": "filer v2",
            "requirements.txt": "req1", "newmod.py": "new"})
        case_plan = zu.plan_update(zu.inspect_archive(case_zip), repo, ["NewMod.py"])
        assert case_plan["delete"] == []
        # ドライブ名を含むエントリは弾く
        drive_zip = make_zip(Path(tmp, "drive.zip"), {
            "main.py": "m", "filer.py": "f", "requirements.txt": "r",
            "C:/evil.py": "x"})
        try:
            zu.inspect_archive(drive_zip)
            raise AssertionError("弾くはず")
        except zu.ZipUpdateError:
            pass
        # 版の新旧: 古い ZIP はダウングレード扱い
        assert zu.is_downgrade("ZIP 20261104", "ZIP 20261031")
        assert not zu.is_downgrade("ZIP 20261031", "ZIP 20261104")
        assert not zu.is_downgrade("ZIP 20261104", "ZIP 20261104")
        assert not zu.is_downgrade("", "ZIP 20261104")
        # 控えは新しい順に5件だけ残る
        backups = Path(tmp, "backups")
        backups.mkdir()
        for i in range(7):
            old = backups / f"update_backup_2026010{i}_000000.zip"
            old.write_bytes(b"x")
            os.utime(old, (1_700_000_000 + i, 1_700_000_000 + i))
        zu._prune_backups(backups)
        assert len(list(backups.glob("*.zip"))) == zu.BACKUP_KEEP == 5

        # pip が失敗しても適用自体は成功として返り、pip の失敗が載る
        v3 = make_zip(Path(tmp, "v3.zip"), {
            "main.py": "main", "filer.py": "filer v2",
            "requirements.txt": "req2", "newmod.py": "new"})
        prepared = zu.check(repo, v3, store_dir=store)
        assert prepared["plan"]["requirements_changed"]
        result = zu.apply(prepared, repo, store_dir=store,
                          run=lambda args, timeout: (1, "", "boom"))
        assert result["ok"] and result["pip"]["ok"] is False


def test_collapsed_pane_is_never_active():
    """境界を端まで寄せて畳んだ窓はアクティブにしない(2026-10-29 本人と相談)。

    - 畳まれた窓がアクティブなら、見えている方の窓へ移る(キー・マウスどちらで畳んでも)
    - 畳まれた窓へ ←→ / Tab で移ろうとしたら、境界を中央へ戻してから移る
    - 仕切りのダブルクリックで中央へ戻る"""
    import filer
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "split_ratio": 0.5}}, None)
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        QTest.qWait(30)
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        try:
            assert w.active_side == "left"
            # キーで左窓を畳むと、アクティブは見えている右窓へ移る
            for _ in range(25):
                key(Qt.Key_Left, Qt.AltModifier)
            assert w.split_ratio == 0.0 and w.pane_collapsed("left")
            assert w.active_side == "right", w.active_side
            # 見えない左窓では何も起きない(↓ と Space は右窓に効く)
            left_cursor = w.left.cursor_row()
            key(Qt.Key_Down)
            key(Qt.Key_Space)
            assert w.left.cursor_row() == left_cursor and not w.left.marks
            assert w.right.marks
            # 畳まれた左窓へ ← で移ろうとすると、中央へ戻してから移る
            key(Qt.Key_Left)
            assert w.split_ratio == 0.5 and not w.pane_collapsed("left")
            assert w.active_side == "left"
            # 右窓を畳んでから Tab でも同じ
            for _ in range(25):
                key(Qt.Key_Right, Qt.AltModifier)
            assert w.pane_collapsed("right") and w.active_side == "left"
            key(Qt.Key_Tab)
            assert w.split_ratio == 0.5 and w.active_side == "right"
            # マウスで畳んだとき(splitterMoved の経路)も見える側へ移る
            total = sum(w.hsplit.sizes())
            w.hsplit.moveSplitter(total, 1)            # 右窓を畳む
            QTest.qWait(20)
            assert w.pane_collapsed("right"), w.hsplit.sizes()
            assert w.active_side == "left"
            # 仕切りのダブルクリックで中央へ
            handle = w.hsplit.handle(1)
            QTest.mouseDClick(handle, Qt.LeftButton, Qt.NoModifier,
                              QPoint(handle.width() // 2, handle.height() // 2))
            assert w.split_ratio == 0.5 and not w.pane_collapsed("right"), w.hsplit.sizes()
        finally:
            w.close()
            w.deleteLater()


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
        assert w.split_ratio == core.SPLIT_MIN == 0.0   # 端まで寄せられる
        QTest.qWait(30)
        assert w.hsplit.sizes()[0] <= 4                 # 左窓が畳み切れる
        # 畳まれた左窓はアクティブにしない(test_collapsed_pane_is_never_active)
        assert w.active_side == "right"
        for _ in range(16):
            key(Qt.Key_Right, Qt.AltModifier)
        assert w.split_ratio == 0.48                    # 戻しても普通に動く
        w.activate("left")                              # 以降は左窓で字の大きさを見る

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
        # 一覧以外(パス行・ラベル行・メッセージ窓)は 10pt 固定で連動しない
        assert w.left.header.font().pointSize() == 10
        assert w.left.header2.font().pointSize() == 10
        assert w.log_font.pointSize() == 10
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
        # 途中で端まで寄せたあと 0.48 まで戻しているので、保存されるのは最後の値
        assert stored["split_ratio"] == 0.48, stored["split_ratio"]
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

            # 4. マウス: クリックでは行も中身も動かないが、押した側の窓が
            # アクティブになる(窓の切り替えだけはマウスでもできる。本人の指定)
            before = (L.path, _cursor_name(L), w.active_side)
            sub_rect = L.view.visualRect(L.model.index(L._row_of("sub1"), 0))
            QTest.mouseClick(L.view.viewport(), Qt.LeftButton, Qt.NoModifier, sub_rect.center())
            QTest.mouseDClick(L.view.viewport(), Qt.LeftButton, Qt.NoModifier, sub_rect.center())
            QTest.mouseClick(L.view.viewport(), Qt.RightButton, Qt.NoModifier, sub_rect.center())
            _wait(w)
            assert (L.path, _cursor_name(L), w.active_side) == before
            r_rect = R.view.visualRect(R.model.index(5, 0))
            QTest.mouseClick(R.view.viewport(), Qt.LeftButton, Qt.NoModifier, r_rect.center())
            assert w.active_side == "right" and R.cursor_row() == 0   # 窓は切り替わるが行は動かない
            QTest.mouseClick(L.view.viewport(), Qt.LeftButton, Qt.NoModifier, sub_rect.center())
            assert w.active_side == "left"                            # 逆側クリックで戻る
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
    assert parsed["items"] == []
    # --item は何度でも(Box の「共有リンクをコピー」を言語ごとの綴りで探す)
    parsed = core.parse_context_args(["--owner", "9", "--item", "共有リンクをコピー",
                                      "--item", "Copy Shared Link", "--", "C:\\Box\\-p"])
    assert parsed["items"] == ["共有リンクをコピー", "Copy Shared Link"]
    assert parsed["paths"] == ["C:\\Box\\-p"] and parsed["owner"] == 9 and parsed["verb"] is None
    args = core.context_menu_args(["C:\\Box\\a"], 0, 0, 5, items=("甲", "B c"))
    assert args == ["--x", "0", "--y", "0", "--owner", "5", "--item", "甲", "--item", "B c",
                    "--", "C:\\Box\\a"], args
    assert core.parse_context_args(args)["items"] == ["甲", "B c"]
    # 名前で項目を探す(一番上の階層だけ・& を除いて比べる・ちょうど1つのときだけ)
    menu = [(0, 10, "開く(&O)"), (0, 0, ""), (0, -1, "Box"), (1, 20, "共有リンクをコピー"),
            (0, 30, "共有リンクを&コピー"), (0, 40, "Copy &Shared Link\tCtrl+L"),
            (0, 50, "A && B")]
    find = core.find_menu_item
    assert find(menu, ["共有リンクをコピー"]) == (30, None)
    assert find(menu, ["Copy Shared Link"]) == (40, None)
    assert find(menu, ["開く"]) == (10, None)
    assert find(menu, ["A & B"]) == (50, None)
    assert find(menu, ["共有リンクをコピー", "Copy Shared Link"])[0] is None   # 2つ当たる
    assert "2 個" in find(menu, ["共有リンクをコピー", "Copy Shared Link"])[1]
    assert find(menu, ["Box"])[0] is None              # サブメニューの見出しは実行できない
    assert find(menu, ["無い"])[0] is None and find(menu, [])[0] is None
    assert core.plain_menu_label("コピー(&C)") == "コピー"
    many = [f"C:\\folder\\{'x' * 100}_{i}.txt" for i in range(200)]
    assert core.context_menu_args(many, 0, 0) is None                    # 長すぎれば JSON で
    with tempfile.TemporaryDirectory() as tmp:
        jpath = os.path.join(tmp, "a.json")
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(core.context_menu_payload(many, 5, 6, 7), f)
        parsed = core.parse_context_args(["--json", jpath])
        assert parsed["paths"] == many and (parsed["x"], parsed["y"], parsed["owner"]) == (5, 6, 7)
        assert parsed["items"] == []
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(core.context_menu_payload(many, 0, 0, 7, items=["甲"]), f)
        assert core.parse_context_args(["--json", jpath])["items"] == ["甲"]


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
            # ニーモニックの & の位置は環境で違う(P&roperties 等)ので、外して比べる
            plain = [l.replace("&", "") for l in labels]
            assert any(("プロパティ" in l) or ("Properties" in l) for l in plain), labels


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


def test_viewer_markup_render_mode():
    import filer_viewer as fv
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    # 種類の判定(純粋な部分)
    assert fv.markup_kind("a.html") == "html" and fv.markup_kind("a.HTM") == "html"
    assert fv.markup_kind("a.md") == "markdown" and fv.markup_kind("a.markdown") == "markdown"
    assert fv.markup_kind("a.txt") is None and fv.markup_kind("a.py") is None

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp, "page.html")
        page.write_text("<h1>見出しテスト</h1><p>本文<b>太字</b>です</p>",
                        encoding="utf-8")
        v = fv.TextViewer(str(page))
        v.show()
        try:
            # HTML はソースでなく整形表示が既定。タグは見えない
            assert v.render_mode and v.rich is not None
            assert v.rich.isVisible() and not v.area.isVisible()
            plain = v.rich.toPlainText()
            assert "見出しテスト" in plain and "太字" in plain and "<h1>" not in plain
            assert "整形" in v.status.text()
            # R でソースに切り替え、もう一度 R で整形に戻る
            QTest.keyClick(v.rich, Qt.Key_R)
            assert not v.render_mode and v.area.isVisible() and not v.rich.isVisible()
            assert "ソース" in v.status.text()
            QTest.keyClick(v.area, Qt.Key_R)
            assert v.render_mode and v.rich.isVisible()
            # 整形表示のまま Esc で閉じられる(キーがビュアに届く)
            QTest.keyClick(v.rich, Qt.Key_Escape)
            assert not v.isVisible()
        finally:
            v.close()
        # Markdown も整形が既定
        note = Path(tmp, "note.md")
        note.write_text("# 題です\n\n- 項目ひとつ\n", encoding="utf-8")
        v = fv.TextViewer(str(note))
        v.show()
        try:
            assert v.render_mode and "題です" in v.rich.toPlainText()
        finally:
            v.close()
        # ただのテキストは従来どおりソース(テキスト)表示だけ
        plain_file = Path(tmp, "plain.txt")
        plain_file.write_text("hello\n", encoding="utf-8")
        v = fv.TextViewer(str(plain_file))
        v.show()
        try:
            assert v.rich is None and not v.render_mode and v.area.isVisible()
        finally:
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
        w._exec_menu = _pick_menu("&O:")       # Ctrl+Enter のメニューの「関連付けで開く」
        QTest.keyClick(L.view, Qt.Key_Return, Qt.ControlModifier)
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


def test_create_7z():
    """P の 7z 版(create_7z)。作って、自分自身の読み取りで中身と展開を確かめる。"""
    import filer_archive as fa
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp, "src")
        (src / "dir" / "empty").mkdir(parents=True)
        (src / "dir" / "中.txt").write_bytes(b"naka")
        (src / "a.txt").write_bytes(b"a" * 1000)
        dest = src / "out.7z"

        assert core.default_archive_name(
            str(src), [core.Entry("a.txt", False)], ".7z") == "a.7z"
        assert core.default_archive_name(
            str(src), [core.Entry("a", False), core.Entry("b", False)], ".7z") == "src.7z"
        # 既存の .zip 版の名前は変わらない
        assert core.default_zip_name(str(src), [core.Entry("a.txt", False)]) == "a.zip"

        result = fa.create_7z(str(src), ["a.txt", "dir"], str(dest))
        assert not result.error and not result.cancelled, result.error
        assert result.files == 2 and result.folders == 2
        assert result.source_bytes == 1000 + 4 and result.zip_bytes == dest.stat().st_size
        assert not Path(str(dest) + ".filer-part").exists()

        index = fa.get_index(str(dest))
        assert sorted(e.name for e in index.list_dir("")) == ["a.txt", "dir"]
        assert sorted(e.name for e in index.list_dir("dir")) == ["empty", "中.txt"]
        out = Path(tmp, "out")
        out.mkdir()
        result = fa.extract(index, fa.plan_extract(index, "", ["a.txt", "dir"]), str(out))
        assert result.written == 2 and not result.errors, result.errors
        assert (out / "dir" / "中.txt").read_bytes() == b"naka"

        # 自分自身(作る 7z)をいまのフォルダの対象に含めても入らない
        result = fa.create_7z(str(src), ["a.txt", "out.7z"], str(dest))
        assert not result.error
        index = fa.get_index(str(dest))
        assert [e.name for e in index.list_dir("")] == ["a.txt"]

        # 中止: 作りかけを残さない
        cancel = threading.Event()
        cancel.set()
        dest2 = src / "cancel.7z"
        result = fa.create_7z(str(src), ["a.txt"], str(dest2), cancel=cancel)
        assert result.cancelled and not dest2.exists() and not Path(str(dest2) + ".filer-part").exists()
        # 失敗(書けない場所): 何も残さない
        bad = Path(tmp, "nope", "x.7z")
        result = fa.create_7z(str(src), ["a.txt"], str(bad))
        assert result.error and not bad.exists()


def test_auto_refresh_on_external_change():
    """表示中のフォルダが外部で変わったら、監視が検めて自動で再読込する。"""
    import time as _time

    import filer
    from PySide6.QtTest import QTest

    _qapp()

    def wait_until(pred, timeout=6.0):
        end = _time.time() + timeout
        while _time.time() < end:
            QTest.qWait(50)
            if pred():
                return True
        return pred()

    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        win = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                                str(settings_path))
        win.resize(1000, 600)
        win.show()
        win.start()
        _wait(win)
        try:
            # 最初の監視は基準を取るだけ(再読込はしない)
            win._watch_tick()
            assert wait_until(lambda: win.left.watch_signature is not None
                              and win.right.watch_signature is not None)
            # 外部でファイルが増えたら、次の一覧に出る
            (left / "external.txt").write_text("x", encoding="utf-8")
            win._watch_tick()
            assert wait_until(lambda: "external.txt" in _names(win.left))
            _wait(win)
            # 外部で消えたら、一覧からも消える
            (left / "alpha.txt").unlink()
            win._watch_tick()
            assert wait_until(lambda: "alpha.txt" not in _names(win.left))
            # 変わっていない右窓はそのまま(カーソルも動かない)
            row = win.right.cursor_row()
            win._watch_tick()
            QTest.qWait(300)
            assert win.right.cursor_row() == row
        finally:
            win.close()


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
    assert "swap" not in core.CASE_MODES   # 大小の入れ替えという変換は無い
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


def test_file_info_arrow_key_focus():
    """R ダイアログの ↑↓←→: 名前 → 変換(全体) → 変換(拡張子) → タイムスタンプ →
    属性(R/S/H/A) とフォーカスが移り、値やチェックは変わらない。変換の行の ←→ は
    1件ではその場で変換を当て、一括ではフォーカスだけ移る。"""
    import filer_fileinfo
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Photo.JPG").write_bytes(b"x")
        entry = core.Entry("Photo.JPG", False, 1, time.time(), 0x20)
        d = filer_fileinfo.FileInfoDialog(None, tmp, [entry])
        # フォーカスのあるボタンが目で見て分かる枠を持つ(スタイルシート側の保証)
        assert "QPushButton:focus" in d.styleSheet()
        assert "#ffd400" in d.styleSheet()
        d.show()
        QTest.qWait(50)
        try:
            focus = lambda: QApplication.focusWidget()  # noqa: E731
            down = lambda: QTest.keyClick(focus(), Qt.Key_Down)  # noqa: E731
            up = lambda: QTest.keyClick(focus(), Qt.Key_Up)  # noqa: E731
            # 始点は名前欄。先頭で ↑ は何もしない
            assert focus() is d.name_edit
            up()
            assert focus() is d.name_edit
            # ↓: 全体 → 拡張子 → タイムスタンプ → 属性 R へ
            down()
            assert focus() is d.case_buttons[("all", "none")]
            down()
            assert focus() is d.case_buttons[("ext", "none")]
            down()
            assert focus() is d.stamp_edits[0]
            values = [b.value() for b in d.stamp_edits]
            down()
            assert focus() is d.attr_checks["readonly"]
            # タイムスタンプの中で ↑↓ しても値は変わらず、グループを出るだけ
            d.stamp_edits[2].setFocus()
            up()
            assert focus() is d.case_buttons[("ext", "none")]
            down()
            assert focus() is d.stamp_edits[0]
            assert [b.value() for b in d.stamp_edits] == values
            # 属性を ↓ で最後まで(末尾の A で ↓ は何もしない)。チェックは変わらない
            down()
            for key in ("readonly", "system", "hidden", "archive"):
                assert focus() is d.attr_checks[key], key
                down()
            assert focus() is d.attr_checks["archive"]
            assert d.attr_checks["archive"].isChecked()
            assert not d.attr_checks["readonly"].isChecked()
            # ↑ で名前まで戻る(属性 → タイムスタンプ → 拡張子 → 全体 → 名前)
            up()
            up()
            up()
            up()
            assert focus() is d.stamp_edits[0]
            up()
            assert focus() is d.case_buttons[("ext", "none")]
            up()
            assert focus() is d.case_buttons[("all", "none")]
            up()
            assert focus() is d.name_edit
            # ←→: 全体の行で移ると、その場で名前へ当たる(1件)。端では止まる。
            # 変換は元の名前へ当て直すので、⇔(何もしない)に戻れば元の名前になる
            d.case_buttons[("all", "none")].setFocus()
            QTest.keyClick(focus(), Qt.Key_Left)
            assert focus() is d.case_buttons[("all", "lower")]
            assert d.name_edit.text() == "photo.jpg"
            QTest.keyClick(focus(), Qt.Key_Left)
            assert focus() is d.case_buttons[("all", "lower")]
            QTest.keyClick(focus(), Qt.Key_Right)
            assert focus() is d.case_buttons[("all", "none")]
            assert d.name_edit.text() == "Photo.JPG"
            QTest.keyClick(focus(), Qt.Key_Right)
            assert focus() is d.case_buttons[("all", "upper")]
            assert d.name_edit.text() == "PHOTO.JPG"
            QTest.keyClick(focus(), Qt.Key_Right)
            assert focus() is d.case_buttons[("all", "upper")]
            # 行を離れて戻っても、着地は ⇔(当てた大文字には戻らない)
            down()
            assert focus() is d.case_buttons[("ext", "none")]
            up()
            assert focus() is d.case_buttons[("all", "none")]
        finally:
            d.close()

        # 一括: ←→ はフォーカスだけ(チェックは変わらない)。着地は ⇔
        other = core.Entry("b.txt", False, 1, time.time(), 0x20)
        Path(tmp, "b.txt").write_bytes(b"x")
        bulk = filer_fileinfo.FileInfoDialog(None, tmp, [entry, other])
        bulk.show()
        QTest.qWait(30)
        try:
            bulk.case_buttons[("all", "upper")].click()
            assert bulk.chosen_case("all") == "upper"
            bulk.case_buttons[("ext", "lower")].setFocus()
            QTest.keyClick(bulk.case_buttons[("ext", "lower")], Qt.Key_Up)
            assert QApplication.focusWidget() is bulk.case_buttons[("all", "none")]
            QTest.keyClick(bulk.case_buttons[("all", "upper")], Qt.Key_Left)
            assert QApplication.focusWidget() is bulk.case_buttons[("all", "none")]
            assert bulk.chosen_case("all") == "upper"
            # ↓↓: 拡張子 → タイムスタンプの止まり先は「変更する」チェック
            QTest.keyClick(bulk.case_buttons[("all", "none")], Qt.Key_Down)
            assert QApplication.focusWidget() is bulk.case_buttons[("ext", "none")]
            QTest.keyClick(bulk.case_buttons[("ext", "none")], Qt.Key_Down)
            assert QApplication.focusWidget() is bulk.stamp_check
            values = [b.value() for b in bulk.stamp_edits]
            QTest.keyClick(bulk.stamp_check, Qt.Key_Down)
            assert QApplication.focusWidget() is bulk.attr_checks["readonly"]
            assert [b.value() for b in bulk.stamp_edits] == values
        finally:
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
                d.case_buttons[("all", "none")].click()
                assert d.name_edit.text() == "Image.WEBP"
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
            assert "Image.WEBP" in os.listdir(left)
            st = os.stat(left / "Image.WEBP")
            assert time.localtime(st.st_mtime)[:6] == (2021, 2, 3, 4, 5, 6)
            assert st.st_file_attributes & 0x01 and st.st_file_attributes & 0x02
            assert _cursor_name(L) == "Image.WEBP"
            assert "Image.WEBP" in w.log_view.toPlainText()
            _set_attrs(left / "Image.WEBP", 0x80)

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


def test_default_apps():
    apps = core.normalize_default_apps({
        ".PDF": "C:/apps/SumatraPDF.exe",
        "*.log": "notepad++",
        "txt": "",
        "": "x.exe",
    })
    assert apps == {"pdf": "C:/apps/SumatraPDF.exe", "log": "notepad++"}, apps
    assert core.normalize_default_apps(None) == {}
    assert core.normalize_default_apps(["pdf"]) == {}
    assert core.default_app_for("a.PDF", apps) == "C:/apps/SumatraPDF.exe"
    assert core.default_app_for("a.txt", apps) is None
    assert core.default_app_for("noext", apps) is None
    assert core.default_app_for("a.pdf", {}) is None


def test_row_color_rules():
    now = 1_800_000_000.0
    day = 86400.0
    # 検証: 色だけの行・壊れた行は捨てる。条件の形が揃う
    rules = core.normalize_row_color_rules([
        {"color": "#FF8800", "name": "*.log"},
        {"color": "red", "name": "*.tmp"},
        {"color": "#00ff00"},
        {"color": "#0000ff", "min_size": 1024, "folder": False},
        "garbage",
    ])
    assert rules == [
        {"color": "#ff8800", "name": "*.log"},
        {"color": "#0000ff", "min_size": 1024, "folder": False},
    ], rules
    assert core.normalize_row_color_rules(None) == []
    assert core.normalize_row_color_rules("x") == []

    rules = core.normalize_row_color_rules([
        {"color": "#111111", "name": "backup_*"},
        {"color": "#222222", "exts": ["log"]},
        {"color": "#333333", "min_size": 1000},
        {"color": "#444444", "newer_days": 1},
        {"color": "#555555", "older_days": 30},
        {"color": "#666666", "attrs": "h"},
        {"color": "#777777", "folder": True},
    ])
    # 先に一致した行が勝つ・条件ごとに効く
    assert core.row_color_for(F("backup_2026.zip", 10, now), rules, now) == "#111111"
    assert core.row_color_for(F("a.LOG", 10, now), rules, now) == "#222222"
    assert core.row_color_for(F("big.bin", 5000, now - 10 * day), rules, now) == "#333333"
    assert core.row_color_for(F("new.txt", 1, now - 3600), rules, now) == "#444444"
    assert core.row_color_for(F("old.txt", 1, now - 40 * day), rules, now) == "#555555"
    assert core.row_color_for(F("hid.txt", 1, now - 10 * day, attrs=HIDDEN), rules, now) == "#666666"
    assert core.row_color_for(F("dir", 0, now - 10 * day, is_dir=True), rules, now) == "#777777"
    # どれにも合わない・親・ルール無しは None
    assert core.row_color_for(F("plain.txt", 1, now - 10 * day), rules, now) is None
    assert core.row_color_for(core.PARENT_ENTRY, rules, now) is None
    assert core.row_color_for(F("a.log"), [], now) is None
    # 大きさと拡張子の条件はフォルダに付けない(日数の条件はフォルダにも効く)
    assert core.row_color_for(F("x.log", 0, now - 10 * day, is_dir=True), rules, now) == "#777777"
    assert core.row_color_for(F("fresh", 0, now, is_dir=True), rules, now) == "#444444"
    assert core.DEFAULT_KEYMAP["4068"] == "diff_view"


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


def test_history_logic():
    now = 1_000_000_000.0
    # 開いたファイルの記録: 同じパスは1件(大小違いも同じ)、古いものは間引く
    data = {}
    core.file_history_add(data, "C:\\x\\a.txt", now)
    core.file_history_add(data, "c:\\X\\A.txt", now + 10)
    assert list(data) == ["c:\\X\\A.txt"] and data["c:\\X\\A.txt"] == now + 10
    core.file_history_add(data, "C:\\x\\b.txt", now + 20)
    assert "C:\\x\\a.txt" not in data and len(data) == 2
    pruned = {"C:\\old.txt": now - 40 * 86400, "C:\\new.txt": now}
    core.file_history_prune(pruned, now)
    assert list(pruned) == ["C:\\new.txt"]
    # フォルダ行: 30日より前の訪問は出さない
    frec = {"C:\\Work": [3, now - 60], "C:\\Old": [1, now - 40 * 86400]}
    rows = core.history_folder_rows(frec, now)
    assert [r["path"] for r in rows] == ["C:\\Work"] and rows[0]["kind"] == "folder"
    # 走査対象: 直近3日の表示フォルダ + ダウンロード(深さ1) + 手動追加。重複は除く
    roots = core.history_scan_roots(
        {"C:\\Work": [3, now - 60], "C:\\Gone": [1, now - 9 * 86400],
         "c:\\work": [1, now - 30]},
        now, "C:\\DL", ["C:\\Extra", "c:\\dl"])
    assert roots == [("C:\\Work", False), ("C:\\DL", True), ("C:\\Extra", False)], roots
    # 統合: 同じファイルは「開いた」が勝ち、全体は時刻の新しい順。ファイルは上限まで
    opened = [{"kind": "file", "path": "C:\\DL\\f.txt", "ts": now, "size": None,
               "attrs": None, "origin": "開いた"}]
    scanned = [{"kind": "file", "path": "c:\\dl\\F.txt", "ts": now - 100, "size": 5,
                "attrs": 0, "origin": "C:\\DL"},
               {"kind": "file", "path": "C:\\DL\\g.txt", "ts": now - 50, "size": 5,
                "attrs": 0, "origin": "C:\\DL"}]
    merged = core.merge_history_rows(rows, opened, scanned)
    files = [r for r in merged if r["kind"] == "file"]
    assert [r["path"] for r in files] == ["C:\\DL\\f.txt", "C:\\DL\\g.txt"], merged
    assert files[0]["origin"] == "開いた"
    many = [{"kind": "file", "path": f"C:\\DL\\{i}.txt", "ts": now - i, "size": 1,
             "attrs": 0, "origin": "C:\\DL"} for i in range(10)]
    assert len([r for r in core.merge_history_rows([], [], many, limit=3)
                if r["kind"] == "file"]) == 3
    # 絞り込み(段のチップ)
    assert core.history_row_match(files[0], "all", 30, now)
    assert not core.history_row_match(files[0], "folder", 30, now)
    assert core.history_row_match(files[0], "file", 30, now)
    assert not core.history_row_match(files[1], "file", 0, now)  # 期間0日では過去は出ない
    old_row = {"kind": "file", "path": "C:\\x", "ts": now - 5 * 86400}
    assert not core.history_row_match(old_row, "all", 3, now)
    assert core.history_row_match(old_row, "all", 30, now)
    assert not core.history_row_match(None, "all", 30, now)
    # 経過の文字
    assert core.history_age_text(now - 30, now) == "たった今"
    assert core.history_age_text(now - 300, now) == "5分前"
    assert core.history_age_text(now - 3 * 3600, now).endswith("時間前")


def test_history_collect():
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub").mkdir()
        (root / "new.txt").write_bytes(b"x")
        (root / "sub" / "deep.txt").write_bytes(b"y")
        (root / "dl.crdownload").write_bytes(b"z")          # ダウンロード中は出さない
        rows, partial = core.collect_history_files([(str(root), True)], now + 5)
        paths = {r["path"] for r in rows}
        assert str(root / "new.txt") in paths and str(root / "sub" / "deep.txt") in paths, paths
        assert str(root / "dl.crdownload") not in paths
        assert not partial
        # 期間を0日にすれば、作られたばかりのものも含めて全部が対象から外れる
        rows0, _ = core.collect_history_files([(str(root), True)], now + 5, days=0)
        assert rows0 == []
        # 深さ0ではサブフォルダの中は見ない
        rows2, _ = core.collect_history_files([(str(root), False)], now + 5)
        assert all(r["path"] != str(root / "sub" / "deep.txt") for r in rows2)
        # 予算0なら打ち切りになる
        _rows3, partial3 = core.collect_history_files(
            [(str(root), True)], now + 5, budget=-1)
        assert partial3


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
            # Shift+H: 履歴ビューがアクティブ窓に出る。フォルダ行の Enter でそこへ
            key(Qt.Key_H, Qt.ShiftModifier)
            _wait(w)
            assert L.hist
            names = [e.name for e in L.entries]
            assert str(left / "sub1") in names, names
            L.set_cursor(L._row_of(str(left / "sub1")))
            key(Qt.Key_Return)
            _wait(w)
            assert not L.hist and core.same_dir(L.path, left / "sub1")
            # 無くなったフォルダは行に出ず、記録からも消える(開くときの掃除)
            L.load(str(left))
            _wait(w)
            shutil.rmtree(left / "sub1" / "deep")
            key(Qt.Key_H, Qt.ShiftModifier)
            _wait(w)
            assert L.hist
            names = [e.name for e in L.entries]
            assert not any(n.casefold().endswith("deep") for n in names), names
            assert not any(p.casefold().endswith("deep") for p in w.frecency)
            key(Qt.Key_H, Qt.ShiftModifier)   # もう一度押すと閉じる
            _wait(w)
            assert not L.hist
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
def test_window_history_view():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        for store_name in (core.FRECENCY_STORE, core.FILE_HISTORY_STORE):
            store = Path(core.data_dir(), store_name)
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
            L.load(str(left / "sub1"))
            _wait(w)
            L.load(str(left))
            _wait(w)
            # 関連付けで開いた記録(開いた時刻が基準)
            w.record_opened_file(str(left / "alpha.txt"))
            assert str(left / "alpha.txt") in w.file_history
            key(Qt.Key_H, Qt.ShiftModifier)
            _wait(w)
            assert L.hist and L.history_bar.isVisible()
            names = [e.name for e in L.entries]
            assert str(left / "sub1") in names, names            # フォルダ行
            assert str(left / "alpha.txt") in names, names       # 開いたファイル行
            assert str(right / "r.txt") in names, names          # 表示したフォルダの走査
            assert names[0] == str(left / "alpha.txt"), names    # 開いたものが先頭(最新)
            # 段のチップ: 種別で絞る
            L._on_history_filter("file", 30)
            assert L.entries and all(not e.is_dir for e in L.entries)
            L._on_history_filter("folder", 30)
            assert L.entries and all(e.is_dir for e in L.entries)
            L._on_history_filter("all", 30)
            # フォルダ行の Enter で移動し、履歴は閉じる
            L.set_cursor(L._row_of(str(left / "sub1")))
            key(Qt.Key_Return)
            _wait(w)
            assert not L.hist and core.same_dir(L.path, left / "sub1")
            # BS で開く前にいたフォルダへ戻る
            key(Qt.Key_H, Qt.ShiftModifier)
            _wait(w)
            assert L.hist
            key(Qt.Key_Backspace)
            _wait(w)
            assert not L.hist and core.same_dir(L.path, left / "sub1")
        finally:
            w.close()
            w.deleteLater()


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
            assert w.log_view.toPlainText().count("フラット表示・検索結果では使えません") == 2
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
    # PDF・HTML・Markdown は整形して出す(テキストの表に載っていてもそちらが先)
    assert core.preview_kind(F("doc.PDF"), exts) == "pdf"
    assert core.preview_kind(F("page.html"), exts) == "html"
    assert core.preview_kind(F("note.md"), exts) == "markdown"
    assert core.fit_size(4000, 3000, 400, 400) == (400, 300)
    assert core.fit_size(100, 50, 400, 400) == (100, 50)               # 拡大はしない
    assert core.fit_size(0, 0, 10, 10) == (0, 0)
    assert core.info_lines("x.bin", F("x.bin", size=1234))[2].startswith("サイズ  1,234")


def test_preview_rich_and_pdf():
    """プレビューの整形表示(HTML / Markdown)と PDF の1ページ目。"""
    import filer_preview
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp, "note.md")
        md.write_text("# 見出し\n\n本文です\n", encoding="utf-8")
        result = filer_preview.load_preview(str(md), "markdown", F("note.md"))
        assert result["rich"] is not None and result["rich"][0] == "markdown"
        assert "見出し" in result["rich"][1]

        html = Path(tmp, "page.html")
        html.write_text("<h1>題</h1><p>中身</p>", encoding="utf-8")
        result = filer_preview.load_preview(str(html), "html", F("page.html"))
        assert result["rich"] is not None and result["rich"][0] == "html"

        pdf = Path(tmp, "doc.pdf")
        pdf.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF")
        result = filer_preview.load_preview(str(pdf), "pdf", F("doc.pdf"), box=(200, 200))
        assert result["image"] is not None and not result["image"].isNull()
        assert "1ページ目" in (result.get("note") or "")

        # 書庫の中の PDF は中身を出さない
        result = filer_preview.load_preview(str(pdf), "pdf", F("doc.pdf"),
                                            archive=(str(pdf), ""))
        assert result["kind"] == "info" and result["image"] is None


def test_preview_image_orientation():
    """EXIF 回転つき(スマホの縦撮り)の画像は、回転後の縦横で箱に収まる。

    保存は横長・表示は縦長の写真を、横長で短い箱で読む。回転を考えずに
    縮めると、回転したぶん高さが箱からはみ出し、窓の大きさまで変わった。"""
    import filer_preview
    from PIL import Image as PilImage

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "portrait.jpg")
        exif = PilImage.Exif()
        exif[274] = 6   # Orientation: 表示のときに時計回り90度
        PilImage.new("RGB", (400, 300), (30, 120, 200)).save(str(path), exif=exif)
        result = filer_preview.load_preview(str(path), "image", F("portrait.jpg"),
                                            box=(200, 100))
        image = result["image"]
        assert image is not None and not image.isNull()
        assert image.height() <= 100 and image.width() <= 200
        assert image.height() > image.width()   # 縦長として出ている
    assert core.DEFAULT_KEYMAP["0114"] == "preview_toggle"
    assert core.fkey_labels(core.DEFAULT_KEYMAP)[2] == "Preview"


def test_preview_applies_edits():
    """補正（crs:）のある写真は、プレビューにも当てて出す（右半分の切り抜き）。"""
    import struct

    import filer_preview
    from PIL import Image as PilImage

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "edited.jpg")
        PilImage.new("RGB", (400, 300), (30, 120, 200)).save(str(path))
        packet = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
                  'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                  '<rdf:Description rdf:about="" '
                  'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
                  'crs:HasCrop="True" crs:CropTop="0" crs:CropLeft="0.5" '
                  'crs:CropBottom="1" crs:CropRight="1"/>'
                  "</rdf:RDF></x:xmpmeta>").encode("utf-8")
        data = path.read_bytes()
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        path.write_bytes(data[:2] + seg + data[2:])
        result = filer_preview.load_preview(str(path), "image", F("edited.jpg"),
                                            box=(800, 600))
        image = result["image"]
        assert image is not None and not image.isNull()
        assert image.width() == 200 and image.height() == 300, image.size().toTuple()
        # 元画像ボタン用に、補正なしの絵（元の大きさ）も一緒に返る
        orig = result["orig_image"]
        assert orig is not None and orig.width() == 400 and orig.height() == 300, \
            None if orig is None else orig.size().toTuple()


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
            # プレビュー中の BS は、まずプレビューを抜ける(書庫は開いたまま)
            key(Qt.Key_Backspace)
            _wait(w)
            assert not w.preview_on and L.archive is not None
            # プレビューを抜けた後の BS で書庫を出る
            key(Qt.Key_Backspace)
            _wait(w)
            assert L.archive is None
            key(Qt.Key_F3)      # 後段のためにプレビューを開き直す
            assert w.preview_on
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
        img.fill(QColor("#3366cc"))
        assert img.save(str(left / "pic2.png"))
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

            # 画像ビューア中の Enter はビューアを抜ける(2026-10-29 要望。
            # 以前は「開いたまま別の画像へ切り替わる」だった)
            L.set_cursor(L._row_of("pic2.png"))
            key(Qt.Key_Return)
            assert not w.preview_on and w.preview_widget is None
        finally:
            w.close()
            # pic2 の読み取りスレッドが残っていると、一時フォルダの後片付けで
            # WinError 32 になる（Windows）。終わるまで待ってから閉じる
            deadline = time.monotonic() + 5
            while getattr(w, "_preview_worker_on", False) \
                    and time.monotonic() < deadline:
                QTest.qWait(20)
            w.deleteLater()


def test_window_vertical_scrollbar():
    """ファイルが多くて入りきらないときは縦スクロールバーが出る(あふｗと同じ)。

    少ないときは出ない(AsNeeded)。"""
    import filer
    from PySide6.QtCore import Qt

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        for i in range(200):
            (left / f"bulk{i:03}.txt").write_text("x", encoding="utf-8")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 500)
        w.show()
        w.start()
        _wait(w)
        try:
            bar = w.left.view.verticalScrollBar()
            assert w.left.view.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded
            assert bar.maximum() > 0                    # 200件は入りきらない
            assert w.right.view.verticalScrollBar().maximum() == 0   # こちらは入る
        finally:
            w.close()
            w.deleteLater()


def test_preview_fullscreen_from_right():
    """右窓からビューア(プレビュー)に入るとフルスクリーン(あふｗ準拠)。

    左窓からのときは従来どおり反対窓の場所だけ。抜ければ元の大きさに戻る。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        img = QImage(120, 80, QImage.Format_RGB32)
        img.fill(QColor("#3366cc"))
        assert img.save(str(right / "rpic.png"))
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
        before_normal = w.normalGeometry()
        try:
            # 左窓から: フルスクリーンにはならない
            QTest.keyClick(L.view, Qt.Key_F3)
            assert w.preview_on and not w.isFullScreen()
            QTest.keyClick(L.view, Qt.Key_F3)
            assert not w.preview_on
            # 右窓から: フルスクリーンになる。抜ければ元の大きさへ戻る
            w.activate("right")
            R.set_cursor(R._row_of("rpic.png"))
            QTest.keyClick(R.view, Qt.Key_F3)
            assert w.preview_on and w.isFullScreen()
            result = _wait_preview(w, "rpic.png")
            assert result["kind"] == "image"
            QTest.keyClick(R.view, Qt.Key_Escape)
            assert not w.preview_on and not w.isFullScreen()
            # 元の大きさに戻ること。位置は比べない: offscreen では前に走ったテストの
            # 窓の置き方に左右され、単独では一致してもファイル全体では食い違う
            # (CI と手元の両方で確認)。確かめたいのは「フルスクリーンが解けて
            # 元の大きさに戻る」ことなので、大きさだけを見る。
            assert w.normalGeometry().size() == before_normal.size(), \
                (w.normalGeometry(), before_normal)
        finally:
            w.close()
            w.deleteLater()


def test_preview_image_lr_keys():
    """画像ビューア(プレビューで画像を表示中)は ←→ で前・次のファイルへ移る。

    本家 AfxW のビューアと同じ(2026-10-27 ユーザー要望)。プレビューを
    閉じているときの → は従来どおり右窓への切り替えのまま。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        img = QImage(120, 80, QImage.Format_RGB32)
        for i, name in enumerate(("pic.png", "pic2.png", "pic3.png")):
            img.fill(QColor.fromHsv(i * 60, 200, 200))
            assert img.save(str(left / name))
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k: QTest.keyClick(L.view, k)  # noqa: E731
        try:
            L.set_cursor(L._row_of("pic.png"))
            key(Qt.Key_F3)
            assert w.preview_on
            result = _wait_preview(w, "pic.png")
            assert result["kind"] == "image"
            row = L.cursor_row()
            key(Qt.Key_Right)          # → 次のファイルへ
            assert L.cursor_row() == row + 1, (row, L.cursor_row())
            assert w.active_side == "left"   # 窓の切り替えにはならない
            key(Qt.Key_Left)           # ← 前のファイルへ
            assert L.cursor_row() == row
            # 画像でないファイルの上でも、プレビュー中の → はファイル移動の
            # まま(窓の切り替えになってプレビューが終わらない。混在フォルダ用)
            L.set_cursor(L._row_of("alpha.txt"))
            result = _wait_preview(w, "alpha.txt")
            assert result["kind"] == "text"
            row = L.cursor_row()
            key(Qt.Key_Right)
            assert L.cursor_row() == row + 1 and w.preview_on
            assert w.active_side == "left"
            # プレビューを閉じたら → は従来どおり右窓への切り替え
            key(Qt.Key_Escape)
            assert not w.preview_on
            key(Qt.Key_Right)
            assert w.active_side == "right"
        finally:
            w.close()
            w.deleteLater()


def test_window_bs_at_drive_root():
    """ドライブのルートで BS を押すと、ドライブの選択が開く。"""
    import filer
    from PySide6.QtCore import Qt
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
        L = w.left
        key = lambda k: QTest.keyClick(L.view, k)  # noqa: E731
        calls = []
        w.cmd_drive = lambda: calls.append(1)
        try:
            # ルートでない所の BS は従来どおり親へ(ドライブ選択は開かない)
            L.load(str(left / "sub1"))
            _wait(w)
            key(Qt.Key_Backspace)
            _wait(w)
            assert core.same_dir(L.path, str(left)) and not calls
            # ルートの BS はドライブ選択へ(移動はしない)
            root = os.path.splitdrive(str(left))[0] + "\\"
            L.load(root)
            _wait(w)
            key(Qt.Key_Backspace)
            _wait(w)
            assert calls == [1] and core.same_dir(L.path, root)
        finally:
            w.close()
            w.deleteLater()


def test_preview_catches_up_with_cursor():
    """プレビューは、カーソルを素早く動かしても最後はカーソルの画像で止まる。

    まだ読み始めていない古い要求は捨てて最新だけ読む(2026-10-31 に「行列で
    全部出す」から変更。長押しは読み込み中のオートリピートを捨てて抑えるので、
    通り過ぎる画像が無くなった)。出た順は通った順のまま(逆戻りしない)。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        pics = Path(tmp, "pics")
        pics.mkdir()
        names = [f"img{i:02d}.png" for i in range(1, 7)]
        img = QImage(400, 300, QImage.Format_RGB32)
        for i, name in enumerate(names):
            img.fill(QColor.fromHsv(i * 50, 200, 200))
            assert img.save(str(pics / name))
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(pics), "right_path": tmp,
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        try:
            shown = []
            L.set_cursor(L._row_of(names[0]))
            QTest.keyClick(L.view, Qt.Key_F3)
            assert w.preview_on
            real_show = w.preview_widget.show_result

            def record(result):
                if result.get("kind") == "image":
                    shown.append(result.get("title"))
                real_show(result)

            w.preview_widget.show_result = record
            L.set_cursor(L._row_of(".."))
            for name in names:
                L.set_cursor(L._row_of(name))
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                QTest.qWait(30)
                if shown and shown[-1] == names[-1] and w._image_wait_seq is None:
                    break
            assert shown and shown[-1] == names[-1], shown
            order = [names.index(t) for t in shown]
            assert order == sorted(order), shown
            assert w._image_wait_seq is None
        finally:
            w.close()
            w.deleteLater()


def _repeat_key(widget, key, modifiers=None):
    """オートリピート(押しっぱなし)のキーを1つ送る。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    event = QKeyEvent(QEvent.KeyPress, key, modifiers or Qt.NoModifier, "", True)
    QApplication.sendEvent(widget, event)


def test_preview_image_repeat_waits_for_display():
    """画像の読み込み中は、オートリピートのカーソル移動を捨てる(長押しの速さ =
    画像を出せる速さ。カーソルと表示がずれない)。単発の押下は受ける。閉じたら・
    失敗したら・上限時間を過ぎたら解ける。テキストのプレビューでは捨てない。"""
    import filer
    import filer_preview
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        pics = Path(tmp, "pics")
        pics.mkdir()
        names = [f"img{i:02d}.png" for i in range(1, 8)]
        img = QImage(200, 100, QImage.Format_RGB32)
        for i, name in enumerate(names):
            img.fill(QColor.fromHsv(i * 40, 200, 200))
            assert img.save(str(pics / name))
        for i in range(3):
            Path(pics, f"t{i}.txt").write_text("x\n", encoding="utf-8")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(pics), "right_path": tmp,
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        real_load = filer_preview.load_preview
        gate = threading.Event()
        gate.set()

        def slow_load(*args, **kwargs):
            gate.wait(10)
            return real_load(*args, **kwargs)
        filer_preview.load_preview = slow_load
        try:
            L.set_cursor(L._row_of(names[0]))
            QTest.keyClick(L.view, Qt.Key_F3)
            _wait_preview(w, names[0])
            _wait_until(lambda: w._image_wait_seq is None)
            gate.clear()                              # ここから読み込みが終わらない
            QTest.keyClick(L.view, Qt.Key_Down)       # 単発は受ける
            assert L.cursor_entry().name == names[1]
            assert w._image_wait_seq is not None
            _repeat_key(L.view, Qt.Key_Down)          # 読み込み中のリピートは捨てる
            _repeat_key(L.view, Qt.Key_PageDown)
            _repeat_key(L.view, Qt.Key_Right)         # 画像の ←→ も同じ
            assert L.cursor_entry().name == names[1]
            QTest.keyClick(L.view, Qt.Key_Down)       # 単発はやはり受ける
            assert L.cursor_entry().name == names[2]
            gate.set()                                # 表示が終われば
            _wait_until(lambda: w._image_wait_seq is None)
            assert w.preview_widget.last["title"] == names[2]   # カーソルと表示が一致
            _repeat_key(L.view, Qt.Key_Down)          # 次のリピートで1つ進む
            assert L.cursor_entry().name == names[3]
            _wait_until(lambda: w._image_wait_seq is None)
            # 読み込みに失敗しても解ける
            filer_preview.load_preview = lambda *a, **k: (_ for _ in ()).throw(OSError("壊れ"))
            _repeat_key(L.view, Qt.Key_Down)
            assert L.cursor_entry().name == names[4]
            _wait_until(lambda: w._image_wait_seq is None)
            assert w.preview_widget.last["kind"] == "info"
            filer_preview.load_preview = slow_load
            # 上限時間を過ぎたら、読み込み中でもリピートを受ける(固まらない保険)
            gate.clear()
            QTest.keyClick(L.view, Qt.Key_Down)
            assert L.cursor_entry().name == names[5] and w._image_wait_seq is not None
            w._image_wait_since -= filer.IMAGE_WAIT_LIMIT + 1
            _repeat_key(L.view, Qt.Key_Down)
            assert L.cursor_entry().name == names[6]
            # 閉じたら解ける
            assert w._image_wait_seq is not None
            QTest.keyClick(L.view, Qt.Key_Escape)
            assert not w.preview_on and w._image_wait_seq is None
            _repeat_key(L.view, Qt.Key_Up)           # プレビューを閉じればリピートは普通に効く
            assert L.cursor_entry().name == names[5]
            gate.set()
            QTest.qWait(100)
            # テキストのプレビューでは捨てない(従来の遅延タイマーのまま)
            L.set_cursor(L._row_of("t0.txt"))
            QTest.keyClick(L.view, Qt.Key_F3)
            _repeat_key(L.view, Qt.Key_Down)
            _repeat_key(L.view, Qt.Key_Down)
            assert L.cursor_entry().name == "t2.txt" and w._image_wait_seq is None
            QTest.keyClick(L.view, Qt.Key_Escape)
        finally:
            gate.set()
            filer_preview.load_preview = real_load
            w.close()
            w.deleteLater()


def _quadrants(image):
    """絵の4隅寄り(左上・右上・左下・右下)の色。向きの判定に使う。"""
    w, h = image.width(), image.height()
    points = [(w // 4, h // 4), (3 * w // 4, h // 4), (w // 4, 3 * h // 4), (3 * w // 4, 3 * h // 4)]
    result = []
    for x, y in points:
        c = image.pixelColor(x, y)
        result.append((c.red(), c.green(), c.blue()))
    return result


def _pil_quadrants(pil):
    w, h = pil.size
    points = [(w // 4, h // 4), (3 * w // 4, h // 4), (w // 4, 3 * h // 4), (3 * w // 4, 3 * h // 4)]
    return [pil.getpixel(p)[:3] for p in points]


def _close_colors(a, b, tolerance=60):
    return all(abs(x - y) <= tolerance for ca, cb in zip(a, b) for x, y in zip(ca, cb))


def _quad_image(size=(160, 80)):
    """4つの区画が違う色の絵(どの向き・裏返しでも見分けられる)。"""
    from PIL import Image as PilImage
    w, h = size
    pil = PilImage.new("RGB", size)
    colors = [(230, 30, 30), (30, 200, 30), (30, 30, 230), (240, 240, 240)]
    for i, color in enumerate(colors):
        x0, y0 = (i % 2) * w // 2, (i // 2) * h // 2
        pil.paste(color, (x0, y0, x0 + w // 2, y0 + h // 2))
    return pil


def test_image_turn_logic():
    """R / L / H / V と回転固定(Shift+R / Shift+L)の状態の動き。"""
    t = core.ImageTurn()
    ident = core.TURN_IDENTITY
    t.press("R")
    assert t.turn == core.TURN_OPS["R"] and core.turn_label(t.turn) == "右90°"
    assert core.turn_swaps_size(t.turn)
    t.press("R")
    assert core.turn_label(t.turn) == "180°" and not core.turn_swaps_size(t.turn)
    t.press("L")
    t.press("L")
    assert t.turn == ident and core.turn_label(ident) == ""
    for _ in range(4):
        t.press("L")
    assert t.turn == ident
    t.press("H")
    assert core.turn_label(t.turn) == "左右反転"
    t.press("H")
    t.press("V")
    assert core.turn_label(t.turn) == "上下反転"
    t.press("V")
    t.press("R")
    t.press("H")       # 右に回してから(見た目を)左右反転
    assert core.turn_label(t.turn) == "右90°+左右反転"
    t.reset()
    # 固定していなければ、別のファイルへ移ると戻る
    t.on_path("a")
    t.press("R")
    assert not t.on_path("a") and t.turn != ident       # 同じファイルの読み直しでは戻さない
    assert t.on_path("b") and t.turn == ident
    # Shift+R: 右に回して固定。次のファイルでも同じ向き。もう一度で解除して元の向き
    t.press("R", shift=True)
    assert t.lock == "R" and t.suffix() == "_R" and t.turn == core.TURN_OPS["R"]
    assert not t.on_path("c") and t.turn == core.TURN_OPS["R"]
    t.press("R", shift=True)
    assert t.lock is None and t.suffix() == "" and t.turn == ident
    # 固定中に逆の Shift+L: 戻して解除
    t.press("R", shift=True)
    t.press("L", shift=True)
    assert t.lock is None and t.turn == ident
    t.press("L", shift=True)
    assert t.lock == "L" and t.suffix() == "_L" and core.turn_label(t.turn) == "左90°"
    # 固定中の R / H は固定したまま向きを変え、次のファイルにも持ち越す
    t.press("R")
    t.press("H")
    assert t.lock == "L" and core.turn_label(t.turn) == "左右反転"
    t.on_path("d")
    assert core.turn_label(t.turn) == "左右反転"
    t.press("H", shift=True)           # Shift+H は回転固定ではない(何もしない)
    assert t.lock == "L"
    t.reset()
    assert t.turn == ident and t.lock is None
    # EXIF の表は8通りすべてが別の向き
    assert len(set(core.EXIF_ORIENTATION_TURNS.values())) == 8
    assert all(core.turn_label(m) or m == ident for m in core.EXIF_ORIENTATION_TURNS.values())


def test_preview_exif_orientation_all_formats():
    """EXIF の向き(1〜8)が、JPEG(Qt の自動回転)・PNG / WebP(Qt は読まないので自前)の
    どれでも正しく当たる。Pillow の exif_transpose と色の並びで比べる。縮めて読んでも同じ。"""
    import filer_preview
    from PIL import Image as PilImage, ImageOps

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        base = _quad_image()
        for ext, options in (("jpg", {"quality": 95}), ("png", {}), ("webp", {"lossless": True})):
            for orientation in range(1, 9):
                path = Path(tmp, f"o{orientation}.{ext}")
                exif = PilImage.Exif()
                exif[274] = orientation
                base.save(str(path), exif=exif.tobytes(), **options)
                with PilImage.open(path) as opened:
                    expected = ImageOps.exif_transpose(opened).convert("RGB")
                for box in ((1000, 1000), (40, 40)):
                    result = filer_preview.load_preview(str(path), "image", F(path.name), box=box)
                    image = result["image"]
                    assert image is not None and not image.isNull(), (ext, orientation, result)
                    landscape = expected.size[0] > expected.size[1]
                    assert (image.width() > image.height()) == landscape, (ext, orientation, box)
                    assert image.width() <= box[0] and image.height() <= box[1]
                    assert _close_colors(_quadrants(image), _pil_quadrants(expected)), \
                        (ext, orientation, box, _quadrants(image), _pil_quadrants(expected))
                    # 見出しの大きさは表示の向きで
                    assert result["note"] == f"{expected.size[0]}×{expected.size[1]}", result["note"]
        assert core.container_exif_orientation(str(Path(tmp, "o6.png"))) == 6
        assert core.container_exif_orientation(str(Path(tmp, "o8.webp"))) == 8
        plain = Path(tmp, "plain.png")
        base.save(str(plain))
        assert core.container_exif_orientation(str(plain)) == 1
        assert core.container_exif_orientation(str(Path(tmp, "none.png"))) == 1   # 無いファイル
        assert core.exif_orientation_from_tiff(b"") == 1
        assert core.exif_orientation_from_tiff(b"Exif\0\0MM\0*\0\0\0\x08\0\x01"
                                               b"\x01\x12\0\x03\0\0\0\x01\0\x03\0\0") == 3


def test_preview_image_rotate_keys():
    """画像ビュアの R / L / H / V と回転固定。表示だけ回してファイルは書き換えない。
    画像ビュアの外(一覧・テキストのプレビュー)では R / L / H / V は従来どおり。
    右窓からの全画面(画像で Enter)でも EXIF の自動回転と手動回転が効く。"""
    import filer
    from PIL import Image as PilImage
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        for name in ("p1.png", "p2.png", "p3.png"):
            _quad_image().save(str(left / name))
        stamps = {name: (left / name).read_bytes() for name in ("p1.png", "p2.png", "p3.png")}
        exif = PilImage.Exif()
        exif[274] = 6
        _quad_image((400, 300)).save(str(right / "cam.jpg"), exif=exif.tobytes(), quality=95)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}},
                              str(settings_path))
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        calls = []
        w.cmd_rename = lambda: calls.append("rename")
        w.cmd_drive = lambda: calls.append("drive")
        w.cmd_history_jump = lambda: calls.append("history")
        w.cmd_edit = lambda: calls.append("edit")

        def key(k, mods=Qt.NoModifier, view=None):
            QTest.keyClick(view or w.active.view, k, mods)

        def shown():
            pixmap = w.preview_widget.image.pixmap()
            return pixmap.toImage()

        def header():
            return w.preview_widget.header.text()
        original = None
        try:
            # 一覧では R / L / H / V は従来どおり
            L.set_cursor(L._row_of("p1.png"))
            for k in (Qt.Key_R, Qt.Key_L, Qt.Key_H, Qt.Key_V):
                key(k)
            assert calls == ["rename", "drive", "history", "edit"], calls
            calls.clear()
            key(Qt.Key_F3)
            _wait_preview(w, "p1.png")
            original = _quadrants(shown())
            assert shown().width() > shown().height()
            tl, tr, bl, br = original
            key(Qt.Key_R)                                   # 右90°
            assert shown().height() > shown().width() and "右90°" in header(), header()
            assert _close_colors(_quadrants(shown()), [bl, tl, br, tr], 10)
            key(Qt.Key_L)                                   # 左90° で元へ
            assert _close_colors(_quadrants(shown()), original, 10) and "90°" not in header()
            key(Qt.Key_L)
            assert _close_colors(_quadrants(shown()), [tr, br, tl, bl], 10)
            key(Qt.Key_R)
            key(Qt.Key_H)                                   # 左右反転
            assert _close_colors(_quadrants(shown()), [tr, tl, br, bl], 10) and "左右反転" in header()
            key(Qt.Key_H)
            key(Qt.Key_V)                                   # 上下反転
            assert _close_colors(_quadrants(shown()), [bl, br, tl, tr], 10) and "上下反転" in header()
            assert calls == []                              # ビュアでは改名などにならない
            # 次の画像へ移ると(固定していなければ)元の向き
            key(Qt.Key_Right)
            _wait_preview(w, "p2.png")
            assert _close_colors(_quadrants(shown()), original, 10) and "反転" not in header()
            # Shift+R: 回転固定。次の画像も右90°、見出しの末尾に _R
            key(Qt.Key_R, Qt.ShiftModifier)
            assert header().endswith("_R") and shown().height() > shown().width(), header()
            key(Qt.Key_Right)
            _wait_preview(w, "p3.png")
            assert header().endswith("_R") and "右90°" in header(), header()
            _wait_until(lambda: w._image_wait_seq is None)
            img = shown()
            assert img.height() > img.width()
            # 回したぶん縦横を入れ替えた箱で読むので、窓に収まり、小さくもならない
            box_w, box_h = w.preview_widget.image_box()
            assert img.width() <= box_w and img.height() <= box_h
            key(Qt.Key_R, Qt.ShiftModifier)                 # もう一度で解除・元の向き
            assert not header().endswith("_R") and shown().width() > shown().height()
            key(Qt.Key_L, Qt.ShiftModifier)                 # _L
            assert header().endswith("_L")
            key(Qt.Key_R, Qt.ShiftModifier)                 # 逆で解除
            assert not header().endswith("_L") and shown().width() > shown().height()
            # 画像ビュアでもテキストの上では R は改名のまま
            L.set_cursor(L._row_of("alpha.txt"))
            _wait_preview(w, "alpha.txt")
            key(Qt.Key_R)
            assert calls == ["rename"], calls
            calls.clear()
            key(Qt.Key_Escape)
            assert not w.preview_on and w.image_turn.lock is None
            # ファイルは書き換えない
            assert all((left / n).read_bytes() == b for n, b in stamps.items())
            # 右窓で画像に Enter → 全画面のビュア。EXIF の向き(縦撮り)が当たっている
            key(Qt.Key_Right)
            assert w.active_side == "right"
            R = w.right
            R.set_cursor(R._row_of("cam.jpg"))
            key(Qt.Key_Return)
            assert w.preview_on and w.isFullScreen()
            _wait_preview(w, "cam.jpg")
            _wait_until(lambda: w._image_wait_seq is None)
            img = shown()
            assert img.height() > img.width(), (img.width(), img.height())   # 300×400 で出る
            assert "300×400" in header(), header()
            key(Qt.Key_L)                                   # 手動は自動回転の後の向きにかかる
            img = shown()
            assert img.width() > img.height() and "左90°" in header()
            assert _close_colors(_quadrants(img), original, 60)   # 元の保存の向きに戻る
            key(Qt.Key_Escape)
            assert not w.preview_on and not w.isFullScreen()
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
    # マスク: 部品の展開・拡張子の保持・{ext} を含むときは付け直さない
    import time as _time
    _mt = _time.mktime((2026, 10, 28, 9, 5, 3, 0, 0, -1))
    assert fr.expand_mask("{name}_{n:03}", "photo.jpg", _mt, 7) == "photo_007"
    assert fr.expand_mask("{date}_{name}.{ext}", "photo.jpg", _mt, 1) == "20261028_photo.jpg"
    assert fr.expand_mask("{time}", "a.txt", _mt, 1) == "090503"
    assert fr.mask_names(["a.txt", "b.txt"], "img_{n}", start=5,
                         mtimes=[_mt, _mt]) == ["img_5.txt", "img_6.txt"]
    assert fr.mask_names(["a.txt"], "{name}_copy.{ext}",
                         mtimes=[_mt]) == ["a_copy.txt"]
    try:
        fr.expand_mask("{size}", "a.txt", _mt, 1)
        raise AssertionError("知らない部品で止まらない")
    except ValueError:
        pass
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
                # 3〜4行目だけに連番(書式で付け直す)
                c = d.editor.textCursor()
                c.setPosition(d.editor.document().findBlockByNumber(2).position())
                c.setPosition(d.editor.document().findBlockByNumber(3).position() + 1,
                              QTextCursor.KeepAnchor)
                d.editor.setTextCursor(c)
                d.ask_format = lambda rows: {"fmt": "file_{n:02}", "start": 1, "step": 1,
                                             "digits": 1}
                d.format_dialog()
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


def test_window_renemo_block_and_gutter():
    """Renemo: ガター余白の追随と、Alt+ドラッグの矩形選択の編集。"""
    import filer_renemo as rm
    from PySide6.QtGui import QFont

    _qapp()
    items = [{"folder": "C:\\x", "name": n, "is_dir": False, "size": 1, "mtime": 0}
             for n in ("DSC0001.jpg", "DSC0002.jpg", "DSC0003.jpg")]
    existing = {"C:\\x": {i["name"] for i in items}}
    d = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    d.show()
    try:
        ed = d.editor
        # 左端の欠け対策: 文字の開始位置(余白)が、ガターの絵の幅と一致する
        assert ed.viewportMargins().left() == ed.gutter_width()
        assert d.editor_lines() == ["DSC0001", "DSC0002", "DSC0003"]

        # 矩形選択 → 全行まとめて打ち替え・削除(行の数は変わらない)
        ed.set_block_selection((0, 3), (2, 7))
        assert ed.block_rows() == [0, 1, 2]
        assert ed.block_text() == "0001\n0002\n0003"
        ed.block_insert_text("99")
        assert d.editor_lines() == ["DSC99", "DSC99", "DSC99"]
        ed.block_delete(backward=True)      # 幅0の矩形での BS は前1文字を全行で
        assert d.editor_lines() == ["DSC9", "DSC9", "DSC9"]
        # 矩形の編集は1回の取り消しで戻る
        ed.set_block_selection((0, 0), (2, 3))
        ed.block_insert_text("IMG")
        assert d.editor_lines() == ["IMG9", "IMG9", "IMG9"]
        ed.undo()
        assert d.editor_lines() == ["DSC9", "DSC9", "DSC9"]
    finally:
        d.close()


def test_window_renemo_maximize_and_drag_scroll():
    """Renemo: 最大化ボタンがあること。矩形のドラッグが欄の下へ出たら送って伸ばすこと。"""
    import filer_renemo as rm
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest

    _qapp()
    items = [{"folder": "C:\\x", "name": f"DSC{i:04}.jpg", "is_dir": False, "size": 1, "mtime": 0}
             for i in range(200)]
    existing = {"C:\\x": {i["name"] for i in items}}
    d = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    assert d.windowFlags() & Qt.WindowMaximizeButtonHint
    d.resize(600, 400)
    d.show()
    try:
        ed = d.editor
        vp = ed.viewport()
        bar = ed.verticalScrollBar()
        assert bar.maximum() > 0 and bar.value() == 0
        QTest.mousePress(vp, Qt.LeftButton, Qt.AltModifier, QPoint(80, 5))
        QTest.mouseMove(vp, QPoint(80, vp.height() + 60))
        for _ in range(5):
            ed._auto_scroll()       # タイマーの代わりに直接回す
        assert ed._scroll_timer.isActive()
        assert bar.value() > 0
        first, last = ed.block_rows()[0], ed.block_rows()[-1]
        assert first == 0 and last > 10      # 見えていなかった行まで伸びた
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.AltModifier, QPoint(80, vp.height() + 60))
        assert not ed._scroll_timer.isActive()
        # 欄の中へ戻れば送るのをやめる
        QTest.mousePress(vp, Qt.LeftButton, Qt.AltModifier, QPoint(80, 5))
        QTest.mouseMove(vp, QPoint(80, vp.height() + 60))
        QTest.mouseMove(vp, QPoint(80, 20))
        assert not ed._scroll_timer.isActive()
        QTest.mouseRelease(vp, Qt.LeftButton, Qt.AltModifier, QPoint(80, 20))
    finally:
        d.close()


def _renemo_items():
    """Renemo のテスト用の項目(実在しないフォルダ。窓は改名しない)。"""
    import time as _time
    mt = _time.mktime((2026, 10, 28, 9, 5, 3, 0, 0, -1))
    names = (("DSC0001.jpg", False), ("DSC0002.jpg", False), ("a.b.txt", False), ("sub", True))
    items = [{"folder": "C:\\x", "name": n, "is_dir": is_dir, "size": 1, "mtime": mt}
             for n, is_dir in names]
    return items, {"C:\\x": {i["name"] for i in items}}


def test_window_renemo_toolbar_and_keys():
    """Renemo: メニューバーが無く、入口はツールバー1段。キーはそのまま効く。
    右クリックは「一括で直す」と同じ中身。実行はモック(改名しない)。"""
    import filer_renemo as rm
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMenuBar

    _qapp()
    items, existing = _renemo_items()
    d = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    try:
        assert d.layout().menuBar() is None and not d.findChildren(QMenuBar)
        assert d.toolbar_labels() == [
            "▶ 実行 (Ctrl+Enter)", "|",
            "一括で直す:", "置換… (Ctrl+H)", "書式で付け直す… (Ctrl+M)", "文字の変換 ▾", "|",
            "☐ 拡張子も編集", "☐ フルパス表示", "並べ替え ▾", "|",
            "↶ 元に戻す (Ctrl+Z)", "履歴…"]
        # 実行のボタンだけ緑
        run = d.toolbar.widgetForAction(d.act_execute)
        _show_active(d)
        img = run.grab().toImage()
        c = img.pixelColor(img.width() // 2, 3)
        assert c.green() > c.red() + 40 and c.green() > c.blue() + 40, c.name()
        other = d.toolbar.widgetForAction(d.act_replace).grab().toImage()
        oc = other.pixelColor(other.width() // 2, 3)
        assert abs(oc.green() - oc.red()) < 20, oc.name()
        # 文字の変換 ▾ と右クリック(一括で直す と同じ中身 + 元に戻す)
        convert = [a.text() for a in d.convert_button.menu().actions()]
        assert convert == ["大文字(&U)", "小文字(&L)", "全角→半角(&H)", "半角→全角(&F)"]
        assert d.act_upper.shortcut().toString() == "Ctrl+Shift+U"
        assert d.act_lower.shortcut().toString() == "Ctrl+U"
        menu = d.build_context_menu()
        top = [a.text() if not a.isSeparator() else "|" for a in menu.actions()]
        assert top == ["置換(&R)…", "書式で付け直す(&M)…", "文字の変換(&C)", "|", "元に戻す(&Z)"]
        assert [a.text() for a in menu.actions()[2].menu().actions()] == convert
        assert [a.text() for a in d.sort_button.menu().actions()] == [l for _k, l in rm.SORT_KINDS]
        # キー: Ctrl+U(小文字)・Ctrl+Shift+U(大文字)・Ctrl+H(置換)・Ctrl+M(書式)
        QTest.keyClick(d.editor, Qt.Key_U, Qt.ControlModifier | Qt.ShiftModifier)
        assert d.editor_lines() == ["DSC0001", "DSC0002", "A.B", "SUB"]
        QTest.keyClick(d.editor, Qt.Key_U, Qt.ControlModifier)
        assert d.editor_lines() == ["dsc0001", "dsc0002", "a.b", "sub"]
        asked = []
        d.ask_replace = lambda: (asked.append("H"), {"find": "dsc", "repl": "IMG", "regex": False,
                                                     "case": False, "keep_ext": True})[1]
        QTest.keyClick(d.editor, Qt.Key_H, Qt.ControlModifier)
        assert asked == ["H"] and d.editor_lines()[:2] == ["IMG0001", "IMG0002"]
        d.ask_format = lambda rows: (asked.append(("M", rows)), None)[1]
        QTest.keyClick(d.editor, Qt.Key_M, Qt.ControlModifier)
        assert asked[-1] == ("M", [0, 1, 2, 3])
        # 元に戻す(ツールバー) = 編集欄の取り消し
        d.act_undo.trigger()
        assert d.editor_lines()[:2] == ["dsc0001", "dsc0002"]
        # Ctrl+Enter で実行(on_execute はモック。1回だけ呼ばれる)
        calls = []
        d.on_execute = lambda pairs: (calls.append(pairs), None)[1]
        QTest.keyClick(d.editor, Qt.Key_Return, Qt.ControlModifier)
        assert len(calls) == 1 and calls[0][0] == ("C:\\x\\DSC0001.jpg", "C:\\x\\dsc0001.jpg")
    finally:
        d.close()


def test_window_renemo_ext_fullpath_checks():
    """Renemo: 「拡張子も編集」「フルパス表示」のチェックボックスは、今までの toggle と同じ結果。"""
    import filer_renemo as rm
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QCheckBox

    _qapp()
    items, existing = _renemo_items()
    old = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    new = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    try:
        assert isinstance(new.check_ext, QCheckBox) and new.check_ext.text() == "拡張子も編集"
        assert isinstance(new.check_fullpath, QCheckBox) and new.check_fullpath.text() == "フルパス表示"
        new.show()
        for d in (old, new):
            d.editor.setPlainText("DSC0001x\nDSC0002\na.b\nsub")      # 編集してから切り替える
        # 今までのやり方(アクションを切り替えて toggle を呼ぶ)
        old.act_ext.setChecked(True)
        old.toggle_ext()
        # チェックボックスをクリック
        box = new.check_ext
        QTest.mouseClick(box, Qt.LeftButton, Qt.NoModifier, QPoint(8, box.height() // 2))
        assert new.check_ext.isChecked() and new.act_ext.isChecked() and new.show_ext
        assert new.editor_lines() == old.editor_lines() == [
            "DSC0001x.jpg", "DSC0002.jpg", "a.b.txt", "sub"]
        assert "☑ 拡張子も編集" in new.toolbar_labels()
        # アクション側を変えてもチェックボックスに写る
        assert old.check_ext.isChecked()
        old.act_fullpath.setChecked(True)
        old.toggle_fullpath()
        new.check_fullpath.click()
        assert new.full_path and new.act_fullpath.isChecked()
        assert new.names_view.toPlainText() == old.names_view.toPlainText()
        assert new.names_view.toPlainText().split("\n")[0] == "C:\\x\\DSC0001.jpg"
        # 外すと元に戻る(編集は残る)
        new.check_ext.click()
        old.act_ext.setChecked(False)
        old.toggle_ext()
        assert not new.show_ext and new.editor_lines() == old.editor_lines() == [
            "DSC0001x", "DSC0002", "a.b", "sub"]
        assert new.new_names() == old.new_names()
    finally:
        old.close()
        new.close()


def test_window_renemo_format_dialog():
    """Renemo: 書式で付け直す(連番とマスクを1つにしたもの)。よく使う形・見本・誤りで OK を
    押せないこと・選んだ行だけ・{ext} の扱い・今までの連番/マスクと同じ結果。"""
    import filer_rename as fr
    import filer_renemo as rm
    from PySide6.QtGui import QFont

    _qapp()
    items, existing = _renemo_items()
    d = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    try:
        rows = d._format_targets()
        assert rows == [0, 1, 2, 3]
        dlg = d.make_format_dialog(rows)
        assert dlg.fmt_edit.text() == "{name}_{n}" and dlg.digits_spin.value() == 3
        assert dlg.ok_button.isEnabled()
        # 見本は最初の3件の「元 → 新」(拡張子まで見せる)
        assert dlg.preview_label.text().split("\n") == [
            "DSC0001.jpg  →  DSC0001_001.jpg", "DSC0002.jpg  →  DSC0002_002.jpg",
            "a.b.txt  →  a.b_003.txt"]
        # よく使う形のボタン → 書式の欄に入って、見本も変わる
        assert [b.text() for b in dlg.preset_buttons] == [
            "連番 photo_{n}", "日付_名前 {date}_{name}", "名前_連番 {name}_{n}", "連番_名前 {n}_{name}"]
        dlg.preset_buttons[0].click()
        assert dlg.fmt_edit.text() == "photo_{n}"
        assert dlg.preview_label.text().split("\n")[0] == "DSC0001.jpg  →  photo_001.jpg"
        dlg.preset_buttons[1].click()
        assert dlg.preview_label.text().split("\n")[0] == "DSC0001.jpg  →  20261028_DSC0001.jpg"
        dlg.start_spin.setValue(10)
        dlg.digits_spin.setValue(2)
        dlg.preset_buttons[3].click()
        assert dlg.preview_label.text().split("\n")[1] == "DSC0002.jpg  →  11_DSC0002.jpg"
        # 誤った書式は赤字で理由を出し、OK を押せない
        dlg.fmt_edit.setText("{size}_{n}")
        assert not dlg.ok_button.isEnabled() and "{size}" in dlg.error_label.text()
        assert dlg.preview_label.text() == ""
        dlg.fmt_edit.setText("   ")
        assert not dlg.ok_button.isEnabled() and "空" in dlg.error_label.text()
        # 名前が重なる書式は注意だけ(OK は押せる)
        dlg.fmt_edit.setText("same")
        assert dlg.ok_button.isEnabled() and "重なる" in dlg.error_label.text()
        dlg.fmt_edit.setText("x_{n}")
        assert dlg.ok_button.isEnabled() and dlg.error_label.text() == ""
        dlg.deleteLater()

        # 実行すると全行(選択なし)。前回の値を覚えている
        d.ask_format = lambda rows: {"fmt": "img_{n}", "start": 5, "step": 2, "digits": 2}
        d.format_dialog()
        assert d.editor_lines() == ["img_05", "img_07", "img_09", "img_11"]
        assert d.sync_from_editor()
        assert d.new_names() == ["img_05.jpg", "img_07.jpg", "img_09.txt", "img_11"]
        dlg = d.make_format_dialog(rows)
        assert dlg.options() == {"fmt": "img_{n}", "start": 5, "step": 2, "digits": 2}
        dlg.deleteLater()
        d.editor.undo()
        assert d.editor_lines() == ["DSC0001", "DSC0002", "a.b", "sub"]

        # 選んだ行だけ(矩形選択の行)
        d.editor.set_block_selection((1, 0), (2, 1))
        seen = []
        d.ask_format = lambda rows: (seen.append(rows),
                                     {"fmt": "{name}_{n}", "start": 1, "step": 1, "digits": 3})[1]
        d.format_dialog()
        assert seen == [[1, 2]]
        assert d.editor_lines() == ["DSC0001", "DSC0002_001", "a.b_002", "sub"]
        d.editor.undo()

        # {ext}: 拡張子を隠しているときは空として扱い、元の拡張子を残す(二重にならない)。
        # 拡張子を出しているときに同じ書式を使った結果と同じ名前になる。
        hidden = d.format_lines({"fmt": "{name}_x.{ext}", "start": 1, "step": 1, "digits": 1},
                                [0, 2])
        assert hidden == ["DSC0001_x", "a.b_x"]
        d.act_ext.setChecked(True)
        d.toggle_ext()
        shown = d.format_lines({"fmt": "{name}_x.{ext}", "start": 1, "step": 1, "digits": 1},
                               [0, 2])
        assert shown == ["DSC0001_x.jpg", "a.b_x.txt"]
        assert [h + d.exts[i] for h, i in zip(hidden, (0, 2))] == shown
        d.act_ext.setChecked(False)
        d.toggle_ext()

        # 今までの連番(numbered_names)・マスク(mask_names)と同じ結果
        for show_ext in (False, True):
            d.act_ext.setChecked(show_ext)
            d.toggle_ext()
            lines = d.editor_lines()
            flags = [i["is_dir"] for i in d.items]
            mtimes = [i["mtime"] for i in d.items]
            for fmt, start, step, digits in (("photo_{n:03}", 1, 1, 1), ("file_{n}", 5, 2, 2),
                                             ("{n}", -1, 1, 3)):
                got = d.format_lines({"fmt": fmt, "start": start, "step": step,
                                      "digits": digits}, rows)
                want = fr.numbered_names(lines, fmt, start, step, digits, keep_ext=show_ext,
                                         dir_flags=flags)
                assert got == want, (show_ext, fmt, got, want)
            if show_ext:
                # 今までのマスクは拡張子を出しているときだけ {ext} を使えた(桁数は1)
                for mask in ("{date}_{name}_{n:03}", "{name}_copy.{ext}", "{time}_{n}"):
                    got = d.format_lines({"fmt": mask, "start": 3, "step": 1, "digits": 1}, rows)
                    want = fr.mask_names(lines, mask, 3, 1, 1, mtimes=mtimes, dir_flags=flags)
                    assert got == want, (mask, got, want)
    finally:
        d.close()


def test_window_renemo_target_line():
    """Renemo: ツールバーの下の「対象」の行。選択なし・範囲選択・矩形選択・編集のたびに変わる。"""
    import filer_renemo as rm
    from PySide6.QtGui import QFont, QTextCursor

    _qapp()
    items, existing = _renemo_items()
    d = rm.RenemoWindow(None, items, existing, font=QFont("Consolas", 14))
    try:
        label = d.target_label
        assert label.text() == "対象: 全 4 行　｜　変更あり 0 件"
        c = d.editor.textCursor()
        c.setPosition(d.editor.document().findBlockByNumber(1).position())
        c.setPosition(d.editor.document().findBlockByNumber(2).position() + 1,
                      QTextCursor.KeepAnchor)
        d.editor.setTextCursor(c)
        assert label.text() == "対象: 選んだ 2 行　｜　変更あり 0 件"
        c.clearSelection()
        d.editor.setTextCursor(c)
        assert label.text().startswith("対象: 全 4 行")
        d.editor.set_block_selection((0, 0), (2, 3))
        assert label.text() == "対象: 選んだ 3 行　｜　変更あり 0 件"
        d.editor.block_insert_text("Z")         # 矩形の編集で3行が変わる
        assert label.text() == "対象: 選んだ 3 行　｜　変更あり 3 件"
        d.editor.clear_block_selection()
        assert label.text() == "対象: 全 4 行　｜　変更あり 3 件"
        d.editor.undo()
        assert label.text() == "対象: 全 4 行　｜　変更あり 0 件"
        # 隠している拡張子だけを変えた行も「変更あり」に数える
        d.act_ext.setChecked(True)
        d.toggle_ext()
        d.editor.setPlainText("DSC0001.png\nDSC0002.jpg\na.b.txt\nsub")
        d.act_ext.setChecked(False)
        d.toggle_ext()
        assert label.text() == "対象: 全 4 行　｜　変更あり 1 件"
    finally:
        d.close()


# ---------------------------------------------------------------
# Ctrl+Enter の拡張子ごとのメニュー(filer_extmenu.py)
# ---------------------------------------------------------------
def test_extmenu_parse_and_format():
    import filer_extmenu as m
    text = ('afx jpg\n'
            '# コメント\n'
            '\n'
            '"I:IrfanView" C:\\soft\\i_view64.exe $MF\n'
            '"V:Avipreview"C:\\soft\\a.exe $MF\n'           # 空白の無い書き方
            '"h:秀丸" hidemaru.exe $MF\n'                    # 小文字のキー
            '"---------------------------"\n'
            '"9:キャンセル" &NOP\n'
            '"X:コマンド無し"\n'                              # コマンドの無い項目は捨てる
            'でたらめな行\n')
    items = m.parse_menu_text(text)
    assert [(i.key, i.label) for i in items if not i.separator] == [
        ("I", "IrfanView"), ("V", "Avipreview"), ("H", "秀丸"), ("9", "キャンセル")], items
    assert items[3].separator
    assert items[1].command == "C:\\soft\\a.exe $MF"
    assert m.format_line("V", 'V"LC', "{tool:vlc} $MF") == '"V:V\'LC" {tool:vlc} $MF'
    assert m.parse_line(m.format_line("A", "表示", "&NOP")).label == "表示"
    assert m.kind_of_name("A.JPG") == "jpg" and m.kind_of_name("README") == ""
    assert m.kind_of_name(".gitignore") == "" and m.kind_of_name("a.tar.gz") == "gz"
    assert m.menu_kind(core.PARENT_ENTRY) == "folder"
    assert m.menu_kind(F("d", is_dir=True)) == "folder" and m.menu_kind(F("x.Md")) == "md"


def test_extmenu_split_command():
    import filer_extmenu as m
    assert m.split_command('"C:\\a b\\x.exe" -m "日本 語" x') == ["C:\\a b\\x.exe", "-m", "日本 語", "x"]
    # あふｗで多い、引用符無しの空白入り exe パス
    assert m.split_command("C:\\Program Files\\Hidemaru\\Hidemaru.exe $MF") == [
        "C:\\Program Files\\Hidemaru\\Hidemaru.exe", "$MF"]
    # \ は逃がしではない(パス末尾の \ が残る)
    assert m.split_command('cmd.exe /k "cd $MF\\"') == ["cmd.exe", "/k", "cd $MF\\"]
    assert m.split_command('explorer.exe /select, $MF') == ["explorer.exe", "/select,", "$MF"]
    assert m.split_command('a "" b') == ["a", "", "b"]
    assert m.split_command("") == []


def test_extmenu_macros_and_excd():
    import filer_extmenu as m
    ctx = {"targets": ["C:\\日本 語\\a b.jpg", "C:\\日本 語\\c.jpg"], "P": "C:\\日本 語",
           "F": "a b.jpg", "O": "D:\\other", "E": "jpg", "cwd": "C:\\日本 語"}
    # まるごと $MF はマークの数だけ引数に。"$MF" と書いても二重に囲まない
    assert m.expand_args(m.split_command('$MF'), ctx) == ctx["targets"]
    assert m.expand_args(m.split_command('"$MF"'), ctx) == ctx["targets"]
    assert m.expand_args(["$MF\\"], {"targets": ["C:\\x"]}) == ["C:\\x\\"]
    # 文字列の中の $MF は、複数なら各々 "" で囲んで空白区切り(あふｗと同じ)
    assert m.expand_args(["cd $MF"], ctx) == ['cd "C:\\日本 語\\a b.jpg" "C:\\日本 語\\c.jpg"']
    assert m.expand_args(["$P\\$F", "-e=$E", "$O"], ctx) == [
        "C:\\日本 語\\a b.jpg", "-e=jpg", "D:\\other"]
    # &EXCD: -P/-O 大文字は「末尾 \ なら中を開く・無ければ親を開いて合わせる」
    one = {"targets": ["C:\\x\\sub"], "P": "C:\\x"}
    assert m.parse_excd("-P$MF\\", one) == ("P", "C:\\x\\sub", True)
    assert m.parse_excd("-O$MF\\", one) == ("O", "C:\\x\\sub", True)
    assert m.parse_excd("-P$MF", one) == ("P", "C:\\x\\sub", False)
    assert m.parse_excd('-P"$MF\\"', one) == ("P", "C:\\x\\sub", True)
    assert m.parse_excd("-pC:\\y\\", one) == ("P", "C:\\y", True)
    assert m.parse_excd("-PC:\\", one) == ("P", "C:\\", True)
    assert m.parse_excd("C:\\y", one) is None
    assert m.internal_name("&excd -P$MF") == "EXCD" and m.internal_name("x.exe") is None
    assert m.internal_targets("$MF", ctx) == ctx["targets"]
    assert m.internal_targets("$MF\\", {"targets": ["C:\\x\\d"]}) == ["C:\\x\\d"]


def _fake_resolver(m, tools=None, files=(), app_paths=None, on_path=None):
    files = {os.path.normcase(f) for f in files}
    app_paths = app_paths or {}
    on_path = on_path or {}
    return m.Resolver(tools or {}, app_path=lambda name: app_paths.get(name.lower()),
                      which=lambda name: on_path.get(name.lower()),
                      exists=lambda p: os.path.normcase(p) in files)


def test_extmenu_tools_and_visibility():
    import filer_extmenu as m
    py = "C:\\Py\\pythonw.exe"
    r = _fake_resolver(m, tools={"photoculler": {"command": [py, "-m", "photo_culler"],
                                                 "cwd": "C:\\work"},
                                 "simple": f'"{py}" -x',
                                 "gone": "C:\\nowhere\\x.exe"},
                       files=[py, "C:\\Irf\\i_view64.exe", "C:\\Win\\explorer.exe"],
                       app_paths={"i_view64.exe": "C:\\Irf\\i_view64.exe"},
                       on_path={"explorer.exe": "C:\\Win\\explorer.exe"})
    assert r.tool("photoculler") == ([py, "-m", "photo_culler"], "C:\\work")
    assert r.tool("simple") == ([py, "-x"], None)            # 文字列だけの簡易形
    assert r.tool("gone") is None                             # 場所が無い PC では出ない
    assert r.tool("i_view64") == (["C:\\Irf\\i_view64.exe"], None)   # App Paths
    assert r.tool("vlc") is None
    assert r.exe("explorer.exe") == "C:\\Win\\explorer.exe"   # PATH
    lines = m.parse_menu_text(
        '"P:開く" &EXCD -P$MF\\\n"C:PhotoCuller" {tool:photoculler} $MF\n'
        '"V:VLC" {tool:vlc} $MF\n"---"\n"Z:謎" &UNKNOWN x\n"E:エクスプローラ" explorer.exe $MF\n'
        '"R:プロパティ" &PROPERTY $MF\n')
    visible = m.visible_items(lines, None, r)
    assert [l.label for l in visible if not l.separator] == [
        "開く", "PhotoCuller", "エクスプローラ", "プロパティ"], visible
    # 書庫の中は外部ツールと &PROPERTY を出さない
    inside = m.visible_items(lines, None, r, in_archive=True)
    assert [l.label for l in inside] == ["開く"], inside
    # 区切りの詰め方: 先頭・末尾・連続は落とす。この PC の分は区切りを挟んで後ろに
    local = m.parse_menu_text('"---"\n"A:追加" &NOP\n"---"\n')
    joined = m.visible_items(m.parse_menu_text('"B:共有" &NOP'), local, r)
    assert [("-" if l.separator else l.label) for l in joined] == ["共有", "-", "追加"]
    # 外部コマンドの組み立て(日本語・空白入りのパス、作業フォルダはツールの cwd が勝つ)
    ctx = {"targets": ["C:\\写真 2026\\a b"], "P": "C:\\写真 2026", "cwd": "C:\\写真 2026"}
    argv, cwd = m.build_external(lines[1], ctx, r)
    assert argv == [py, "-m", "photo_culler", "C:\\写真 2026\\a b"] and cwd == "C:\\work"
    argv, cwd = m.build_external(m.parse_line('"S:選択" explorer.exe /select, $MF'), ctx, r)
    assert argv == ["C:\\Win\\explorer.exe", "/select,", "C:\\写真 2026\\a b"]
    assert cwd == "C:\\写真 2026"
    assert m.build_external(lines[2], ctx, r) is None


def test_extmenu_find_menus_aliases_local():
    import filer_extmenu as m
    with tempfile.TemporaryDirectory() as tmp:
        shared, local = Path(tmp, "shared"), Path(tmp, "local")
        shared.mkdir()
        local.mkdir()
        (shared / "_aliases.txt").write_text("# c\nimage: jpg PNG\n", encoding="utf-8")
        (shared / "image.txt").write_text('"A:共通" &NOP\n', encoding="utf-8")
        (shared / "jpg.txt").write_text('"B:jpgだけ" &NOP\n', encoding="utf-8")
        (shared / "folder.txt").write_text('"P:開く" &NOP\n', encoding="utf-8")
        (local / "jpg.txt").write_text('"C:この PC" &NOP\n', encoding="utf-8-sig")
        assert m.load_aliases(shared) == {"jpg": "image", "png": "image"}
        s, l = m.find_menus("jpg", shared, local)
        assert [i.label for i in s] == ["共通", "jpgだけ"] and [i.label for i in l] == ["この PC"]
        s, l = m.find_menus("png", shared, local)
        assert [i.label for i in s] == ["共通"] and l is None
        assert m.find_menus("xyz", shared, local) == (None, None)
        assert m.find_menus("", shared, local) == (None, None)
        # 雛形: 無ければ作る(説明だけ)。あれば触らない
        path = m.ensure_local_file("png", local)
        assert Path(path).read_text(encoding="utf-8").startswith("#")
        assert m.find_menus("png", shared, local)[1] == []
        assert m.ensure_local_file("jpg", local) == str(local / "jpg.txt")
        assert "この PC" in (local / "jpg.txt").read_text(encoding="utf-8-sig")
    # リポジトリの初期メニュー: 個人のパスが無く、folder・画像・動画・テキストが引ける
    for name in ("folder", "jpg", "heic", "mp4", "avi", "md", "json"):
        s, _l = m.find_menus(name, m.MENU_DIR, os.path.join(_TEST_TEMP, "no_local"))
        assert s, name
    for path in m.MENU_DIR.glob("*.txt"):
        data = path.read_bytes()
        assert b"\r\n" not in data, path
        for line in data.decode("utf-8").splitlines():
            if not line.startswith("#"):
                assert not m.has_personal_path(line), (path, line)


def test_extmenu_editor_files_and_tools():
    import filer_extmenu as m
    with tempfile.TemporaryDirectory() as tmp:
        shared, local = Path(tmp, "shared"), Path(tmp, "local")
        shared.mkdir()
        (shared / "txt.txt").write_text('"V:見る" &VIEW $MF\n', encoding="utf-8")
        local.mkdir()
        original = '# 自分のメモ\n"A:一" &NOP\n\n"B:二" &NOP\n'
        (local / "txt.txt").write_bytes(original.encode("utf-8"))
        exe = "C:\\Tools\\My App\\app.exe"
        ed = m.MenuEditor({"old": {"command": ["C:\\x.exe"]}}, shared, local)
        assert "txt" in ed.kinds() and ed.kinds()[0] == "folder"
        assert [l.label for _s, l in ed.readonly_items("txt")] == ["見る"]
        assert ed.used_keys("txt") == {"V", "A", "B"}
        line = ed.add_item("txt", {"label": "アプリ", "key": "P", "program": ("exe", exe),
                                   "args": "$MF", "cwd": ""})
        assert line == '"P:アプリ" {tool:app} $MF'
        assert ed.tool_updates == {"app": {"command": [exe]}}
        # 同じ exe は使い回す(名前も値も増えない)。作業フォルダが違えば別名で
        ed.add_item("txt", {"label": "再", "key": "Q", "program": ("exe", exe.lower()),
                            "args": "", "cwd": ""})
        ed.add_item("txt", {"label": "別", "key": "R", "program": ("exe", exe),
                            "args": "$P", "cwd": "C:\\w"})
        assert set(ed.tool_updates) == {"app", "app2"}
        assert ed.tool_updates["app2"] == {"command": [exe], "cwd": "C:\\w"}
        ed.add_item("txt", {"label": "既存", "key": "O", "program": ("tool", "old"), "args": "$MF"})
        ed.move_local("txt", 0, 1)                 # 一 ↔ 二
        ed.replace_local("txt", 2, {"label": "アプリ改", "key": "P", "program": ("tool", "app"),
                                    "args": '"$MF"'})
        ed.delete_local("txt", 3)                  # 再 を消す
        # 共有には個人のパスを書かない(exe は tools へ、行は {tool:} だけ)
        shared_line = ed.add_item("txt", {"label": "共有", "key": "S", "program": ("exe", exe),
                                          "args": "$MF"}, scope="shared")
        assert shared_line == '"S:共有" {tool:app} $MF'
        try:
            ed.add_item("txt", {"label": "x", "key": "X", "program": ("raw", exe),
                                "args": ""}, scope="shared")
            raise AssertionError("個人のパスが共有に入った")
        except ValueError:
            pass
        # 決定前はファイルに触らない
        assert (local / "txt.txt").read_bytes() == original.encode("utf-8")
        written = ed.commit()
        assert written["local"] == [str(local / "txt.txt")]
        assert written["shared"] == [str(shared / "txt.txt")]
        assert set(written["tools"]) == {"app", "app2"}
        text = (local / "txt.txt").read_bytes().decode("utf-8")
        assert "\r" not in text and text.endswith("\n")
        assert text.splitlines() == ['# 自分のメモ', '"B:二" &NOP', '', '"A:一" &NOP',
                                     '"P:アプリ改" {tool:app} "$MF"',
                                     '"R:別" {tool:app2} $P', '"O:既存" {tool:old} $MF'], text
        assert (local / "txt.txt.bak").read_bytes() == original.encode("utf-8")   # 1世代
        assert not (shared / "txt.txt.bak").exists()                               # 共有は残さない
        assert (shared / "txt.txt").read_text(encoding="utf-8").splitlines()[-1] == shared_line
        # 書き換えた行を編集用に分け直せる
        assert m.describe_program('{tool:app} "$MF"') == (("tool", "app"), '"$MF"')
        assert m.describe_program('C:\\Program Files\\x.exe $MF') == (
            ("raw", '"C:\\Program Files\\x.exe"'), "$MF")
    assert m.suggest_key("VLC media player", {"V"}) == "L"
    assert m.suggest_key("日本語", {"A"}) == "B"
    assert m.tool_name_from_exe("C:\\x\\Photo Culler.EXE") == "photo_culler"
    assert m.has_personal_path("{tool:x} C:\\Users\\me\\a") and m.has_personal_path("x \\\\srv\\s")
    assert not m.has_personal_path('"P:開く" &EXCD -P$MF\\')


class _FakeRegistry:
    def __init__(self, values=None, subkeys=None):
        self._values = values or {}
        self._subkeys = subkeys or {}

    def values(self, hive, path):
        return dict(self._values.get((hive, path.lower()), {}))

    def subkeys(self, hive, path):
        return list(self._subkeys.get((hive, path.lower()), []))

    def default(self, hive, path):
        return self.values(hive, path).get("")


def test_open_with_candidates():
    import filer_extmenu as m
    user = m.FILE_EXTS_KEY.lower() + "\\.mp4"
    reg = _FakeRegistry(values={
        ("HKCU", user + "\\openwithlist"): {"a": "vlc.exe", "b": "mpc.exe", "MRUList": "ba"},
        ("HKCU", user + "\\openwithprogids"): {"VLC.mp4": b""},
        ("HKCR", "applications\\vlc.exe\\shell\\open\\command"): {"": '"C:\\VLC\\vlc.exe" "%1"'},
        ("HKCR", "vlc.mp4\\shell\\open\\command"): {"": '"C:\\VLC\\vlc.exe" --started "%1"'},
        ("HKCR", ".mp4"): {"": "WMP.mp4"},
        ("HKCR", "wmp.mp4\\shell\\open\\command"): {"": "C:\\Program Files\\WMP\\wmp.exe /x %1"},
        ("HKCR", ".mp4\\openwithprogids"): {"AppX123": b""},
        ("HKCR", "directory\\shell\\code\\command"): {"": '"C:\\Code\\Code.exe" "%V"'},
        ("HKCR", "folder\\shell\\open\\command"): {"": "%SystemRoot%\\Explorer.exe"},
    }, subkeys={("HKCR", "directory\\shell"): ["code", "none"],
                ("HKCR", "folder\\shell"): ["open"]})
    files = {"C:\\VLC\\vlc.exe", "C:\\MPC\\mpc.exe", "C:\\Program Files\\WMP\\wmp.exe",
             "C:\\Code\\Code.exe", os.path.expandvars("%SystemRoot%\\Explorer.exe")}
    found = m.open_with_candidates("mp4", registry=reg, exists=lambda p: p in files,
                                   app_path=lambda n: {"mpc.exe": "C:\\MPC\\mpc.exe"}.get(n))
    # MRUList 順(mpc → vlc)、ProgId は重複を除く、空白入りの引用符無しパスも取れる
    assert found == ["C:\\MPC\\mpc.exe", "C:\\VLC\\vlc.exe", "C:\\Program Files\\WMP\\wmp.exe"], found
    folder = m.open_with_candidates("folder", registry=reg, exists=lambda p: p in files)
    assert folder == ["C:\\Code\\Code.exe", os.path.expandvars("%SystemRoot%\\Explorer.exe")], folder
    assert m.exe_from_command('"C:\\a b\\x.exe" "%1"') == "C:\\a b\\x.exe"
    assert m.exe_from_command(None) == ""
    # 本物の exe の FileDescription(読めること・落ちないことだけ)
    assert isinstance(m.file_description(os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                                                      "explorer.exe")), str)
    assert m.file_description("C:\\no\\such.exe") == ""


# ---------------------------------------------------------------
# 版表記と更新(filer_update.py)
# ---------------------------------------------------------------
def _make_git(root, packed=False, with_commit=True):
    import zlib
    sha = "2f74ed1" + "a" * 33
    git = Path(root, ".git")
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    if packed:
        (git / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/main\n",
                                         encoding="utf-8")
    else:
        (git / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    if with_commit:
        stamp = int(time.mktime((2026, 10, 29, 12, 0, 0, 0, 0, -1)))
        body = (f"tree {'b' * 40}\nauthor a <a@b> {stamp} +0900\n"
                f"committer a <a@b> {stamp} +0900\n\nmsg\n").encode()
        obj = git / "objects" / sha[:2]
        obj.mkdir(parents=True)
        (obj / sha[2:]).write_bytes(zlib.compress(b"commit %d\0" % len(body) + body))
    return sha


def _no_git(args, timeout):
    import filer_update as u
    raise u.GitError("git が見つかりません")


def test_version_label_reads_git_and_zip():
    """git コマンドが無い(業務PC)・失敗したときのフォールバック: .git を読んで日付だけ、
    .git が無ければ ZIP と一番新しい .py の日付。"""
    import filer_update as u
    with tempfile.TemporaryDirectory() as tmp:
        sha = _make_git(tmp)
        assert u.version_label(tmp, run=_no_git) == "20261029"
        assert u.quick_version(tmp) == {"label": "20261029", "sha": sha, "source": "git-file"}
        assert u.version_info(tmp, run=_no_git)["sha"] == sha   # ハッシュは残す
    with tempfile.TemporaryDirectory() as tmp:
        _make_git(tmp, packed=True)                 # packed-refs だけ
        assert u.version_label(tmp, run=_no_git) == "20261029"
    with tempfile.TemporaryDirectory() as tmp:
        _make_git(tmp, with_commit=False)           # コミットが読めなければ ref の更新時刻
        label = u.version_label(tmp, run=_no_git)
        assert len(label) == 8 and label.isdigit(), label
    with tempfile.TemporaryDirectory() as tmp:
        _make_git(tmp)                              # git が失敗(終了コード≠0)も同じ
        assert u.version_label(tmp, run=lambda a, t: (128, "", "fatal")) == "20261029"
    with tempfile.TemporaryDirectory() as tmp:      # ZIP 展開(.git 無し)
        py = Path(tmp, "a.py")
        py.write_text("x = 1\n", encoding="utf-8")
        stamp = time.mktime((2026, 10, 28, 9, 0, 0, 0, 0, -1))
        os.utime(py, (stamp, stamp))
        called = []
        assert u.version_label(tmp, run=lambda a, t: called.append(a)) == "ZIP 20261028"
        assert not u.has_git(tmp) and not called    # .git が無ければ git は呼ばない
        assert u.quick_version(tmp)["sha"] == ""
    with tempfile.TemporaryDirectory() as tmp:      # .git が壊れている(読めない)
        Path(tmp, ".git").mkdir()
        Path(tmp, ".git", "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
        py = Path(tmp, "a.py")
        py.write_text("x = 1\n", encoding="utf-8")
        stamp = time.mktime((2026, 10, 27, 9, 0, 0, 0, 0, -1))
        os.utime(py, (stamp, stamp))
        # 空にはせず、.py の日付で版を出す
        assert u.version_label(tmp, run=_no_git) == "20261027"
        assert u.quick_version(tmp)["source"] == "file"
    with tempfile.TemporaryDirectory() as tmp:
        assert u.version_label(tmp) == ""           # どちらも取れない
    assert u.window_title("C:\\x", "20261031-02") == "C:\\x - あっふ 20261031-02"
    assert u.window_title("", "") == "あっふ" and u.window_title("C:\\x", "") == "C:\\x - あっふ"


def test_ask_name_dialog_stays_on_top():
    """J の「ここを登録」の名前入力: 親のピッカー(最前面)の裏に隠れないよう、
    ダイアログ自身も最前面(WindowStaysOnTopHint)で出す。"""
    try:
        import launcher
    except ImportError:
        # VM(Windows 専用の comtypes が無い)では explorer_nav を差し替えて読む
        import types as _types
        sys.modules.setdefault("explorer_nav", _types.ModuleType("explorer_nav"))
        import launcher
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog

    made = {}

    class FakeDialog:
        def __init__(self, parent):
            made["flags"] = Qt.WindowFlags()
            made["parent"] = parent

        def setWindowTitle(self, t):
            made["title"] = t

        def setLabelText(self, t):
            made["label"] = t

        def setTextValue(self, t):
            made["value"] = t

        def windowFlags(self):
            return made["flags"]

        def setWindowFlags(self, flags):
            made["flags"] = flags

        def exec(self):
            return QDialog.Accepted

        def textValue(self):
            return "  なまえ  "

    original = launcher.QInputDialog
    launcher.QInputDialog = FakeDialog
    try:
        assert launcher._ask_name(object(), "既定") == "なまえ"
    finally:
        launcher.QInputDialog = original
    assert made["flags"] & Qt.WindowStaysOnTopHint
    assert made["value"] == "既定"


def test_version_sequence_label():
    """版の連番: HEAD の日付(ローカル時刻)と同じ日付の first-parent のコミットの数。"""
    import filer_update as u

    def at(*t):
        return int(time.mktime(t + (0, 0, -1)))
    lines = [f"{'c' * 40} {at(2026, 10, 31, 1, 28, 0)}",
             f"{'b' * 40} {at(2026, 10, 31, 0, 5, 0)}",
             f"{'a' * 40} {at(2026, 10, 30, 23, 59, 0)}",
             f"{'d' * 40} {at(2026, 10, 30, 12, 0, 0)}"]
    assert u.sequence_label(lines) == ("20261031-02", "c" * 40)
    assert u.sequence_label(lines[1:]) == ("20261031-01", "b" * 40)   # その日の最初が 01
    assert u.sequence_label(lines[2:]) == ("20261030-02", "a" * 40)
    assert u.sequence_label([]) == ("", "") and u.sequence_label(["壊れた行"]) == ("", "")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, ".git").mkdir()
        calls = []

        def run(args, timeout):
            calls.append((args, timeout))
            return 0, "\n".join(lines) + "\n", ""
        assert u.git_version(tmp, run=run) == {"label": "20261031-02", "sha": "c" * 40,
                                               "source": "git"}
        args, timeout = calls[0]
        assert args[0] == "log" and "--first-parent" in args and args[-1] == "HEAD"
        assert timeout == u.VERSION_TIMEOUT <= 5        # 固まっても長く待たない
        assert u.git_version(tmp, run=_no_git) is None
        assert u.git_version(tmp, run=lambda a, t: (128, "", "fatal")) is None
        assert u.git_version(tmp, run=lambda a, t: (0, "", "")) is None
    # ログ・確認文にはハッシュを添える。比べるのはハッシュ(表記は git の有無で変わる)
    assert u.describe_version({"label": "20261031-02", "sha": "067f1b1" + "0" * 33}) == \
        "20261031-02（067f1b1）"
    assert u.describe_version({"label": "ZIP 20261028", "sha": ""}) == "ZIP 20261028"
    assert u.same_version({"label": "20261031-02", "sha": "a" * 40},
                          {"label": "20261031", "sha": "a" * 40})
    assert not u.same_version({"label": "20261031-02", "sha": "a" * 40},
                              {"label": "20261031-02", "sha": "b" * 40})
    assert u.same_version({"label": "ZIP 1", "sha": ""}, {"label": "ZIP 1", "sha": ""})


def test_version_with_real_git():
    """本物の git で: 枝のコミットは数えず(first-parent)、同じ日の数が連番になる。"""
    import subprocess
    import filer_update as u
    if not shutil.which("git"):
        print("  (git が無いので飛ばす)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        def git(*args, when=None):
            env = dict(os.environ)
            if when:
                env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "-c", "commit.gpgsign=false", *args], cwd=tmp, env=env,
                           check=True, capture_output=True,
                           creationflags=u.CREATE_NO_WINDOW)

        git("init", "-q", "-b", "main")
        git("commit", "-q", "--allow-empty", "-m", "c1", when="2026-10-30T23:00:00")
        git("commit", "-q", "--allow-empty", "-m", "c2", when="2026-10-31T00:30:00")
        git("checkout", "-q", "-b", "side")
        git("commit", "-q", "--allow-empty", "-m", "s1", when="2026-10-31T00:40:00")
        git("checkout", "-q", "main")
        git("commit", "-q", "--allow-empty", "-m", "c3", when="2026-10-31T00:50:00")
        git("merge", "-q", "--no-ff", "-m", "merge", "side", when="2026-10-31T01:00:00")
        found = u.git_version(tmp)
        assert found and found["label"] == "20261031-03", found   # merge, c3, c2(s1 は数えない)
        assert found["sha"] == u.quick_version(tmp)["sha"]       # どちらで読んでも同じコミット
        assert u.version_label(tmp) == "20261031-03"


def _fake_git(outputs, calls=None):
    """git の呼び出しの差し替え。outputs は {最初の引数: (code, out, err) か例外}。"""
    import filer_update as u

    def run(args, timeout):
        if calls is not None:
            calls.append(list(args))
        value = outputs.get(args[0], (0, "", ""))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(args)
        return value
    return run


def test_update_decide_and_check():
    import filer_update as u
    assert u.decide(0, 0, []) == "uptodate" and u.decide(2, 0, ["x"]) == "uptodate"
    assert u.decide(0, 3, []) == "behind"
    assert u.decide(1, 3, []) == "diverged" and u.decide(0, 3, ["a.py"]) == "dirty"
    assert u.resident_files(["filer.py", "filer_menus/a.txt", "main.py", "tests/test_x.py",
                             "README.md", "feature_screen.py", "tools/x.py"]) == [
        "main.py", "feature_screen.py"]
    # 起動時の版よりディスクの版が新しいか(常駐のトレイ表示)
    assert u.disk_is_newer({"sha": "a" * 40, "label": "20261104"},
                           {"sha": "b" * 40, "label": "20261104"})
    assert not u.disk_is_newer({"sha": "a" * 40}, {"sha": "a" * 40, "label": "x"})
    # ZIP 展開は同じ日付でも、一番新しい .py の時刻で見分ける
    assert u.disk_is_newer({"label": "ZIP 20261104", "stamp": 100.0},
                           {"label": "ZIP 20261104", "stamp": 200.0})
    assert not u.disk_is_newer({"label": "ZIP 20261104", "stamp": 200.0},
                               {"label": "ZIP 20261104", "stamp": 200.0})
    assert not u.disk_is_newer({}, {"sha": "b" * 40}) and not u.disk_is_newer({"label": "x"}, {})
    # 常駐の再起動: --check で差し支えないと言われたときだけ restart を送る
    sent = []

    def exchange_with(check_answer, restart_answer="OK 再起動します（数秒で戻ります）"):
        def exchange(command, args):
            sent.append((command, list(args)))
            return check_answer if args else restart_answer
        return exchange
    ok, message = u.restart_resident(exchange_with("OK いま再起動して差し支えありません"))
    assert ok and message == "再起動します（数秒で戻ります）"
    assert sent == [("restart", ["--check"]), ("restart", [])], sent
    sent.clear()
    ok, message = u.restart_resident(exchange_with("OK 再起動は見送るべきです: 付箋 2枚・画面ミラー"))
    assert not ok and "付箋 2枚・画面ミラー" in message and sent == [("restart", ["--check"])]
    sent.clear()
    ok, message = u.restart_resident(exchange_with(
        "OK いま再起動して差し支えありません", "ERR 起動し直せませんでした（このまま動き続けます）"))
    assert not ok and message.startswith("起動し直せませんでした")
    ok, message = u.restart_resident(exchange_with(None))
    assert not ok and "返事がありません" in message

    def not_running(command, args):
        raise FileNotFoundError
    ok, message = u.restart_resident(not_running)
    assert not ok and "起動していない" in message
    with tempfile.TemporaryDirectory() as tmp:
        assert u.check_updates(tmp, run=_fake_git({}))["state"] == "nogit"
        Path(tmp, ".git").mkdir()
        calls = []
        info = u.check_updates(tmp, run=_fake_git({
            "rev-parse": (0, "origin/main\n", ""),
            "rev-list": (0, "0\t2\n", ""),
            "status": (0, "", ""),
            "log": (0, "abc1234 あっふ: 直す\ndef5678 常駐: 足す\n", ""),
            "diff": (0, "filer.py\nmain.py\n", ""),
        }, calls))
        assert info["state"] == "behind" and info["behind"] == 2, info
        assert info["commits"] == ["abc1234 あっふ: 直す", "def5678 常駐: 足す"]
        assert info["files"] == ["filer.py", "main.py"]
        assert calls[0][0] == "fetch" and ["pull", "--ff-only"] not in calls
        info = u.check_updates(tmp, run=_fake_git({"rev-parse": (0, "origin/main", ""),
                                                   "rev-list": (0, "1\t2", "")}))
        assert info["state"] == "diverged" and "分岐" in u.describe_block(info)
        info = u.check_updates(tmp, run=_fake_git({"rev-parse": (0, "origin/main", ""),
                                                   "rev-list": (0, "0\t1", ""),
                                                   "status": (0, " M filer.py\n", "")}))
        assert info["state"] == "dirty" and "filer.py" in u.describe_block(info)
        info = u.check_updates(tmp, run=_fake_git({"rev-list": (0, "0\t0", "")}))
        assert info["state"] == "uptodate"
        # 失敗は落ちずに一言で
        info = u.check_updates(tmp, run=_fake_git({"fetch": (128, "", "fatal: unable to access "
                                                                     "'https://x': Could not resolve host")}))
        assert info["state"] == "error" and "ネットワーク" in info["message"], info
        info = u.check_updates(tmp, run=_fake_git({"fetch": (128, "", "fatal: Authentication failed")}))
        assert "認証" in info["message"]
        info = u.check_updates(tmp, run=_fake_git({"fetch": u.GitError("git が見つかりません")}))
        assert info == {"state": "error", "message": "git が見つかりません"}
        info = u.check_updates(tmp, run=_fake_git({"rev-parse": (128, "", "no upstream")}))
        assert info["state"] == "error" and "追跡" in info["message"]
        assert u.pull(tmp, run=_fake_git({"pull": (0, "Fast-forward\n", "")})) == {
            "ok": True, "message": "Fast-forward"}
        result = u.pull(tmp, run=_fake_git({"pull": (1, "", "fatal: Not possible to fast-forward")}))
        assert result["ok"] is False and "pull" in result["message"]
    # git が無い環境でも GitError になる(落ちない)
    real = u.subprocess.run

    def missing(*_a, **_k):
        raise FileNotFoundError("git")
    u.subprocess.run = missing
    try:
        try:
            u.run_git(["status"])
            raise AssertionError("GitError にならない")
        except u.GitError as e:
            assert "git" in str(e)
    finally:
        u.subprocess.run = real


# ---------------------------------------------------------------
# 窓: Ctrl+Enter / ツールの追加 / 設定タブ / タイトル / Shift+F5
# ---------------------------------------------------------------
def _menu_window(tmp, tools=None, settings_path=None):
    import filer
    left, right = _make_tree(tmp)
    jp = Path(left, "写真 フォルダ")
    jp.mkdir()
    Path(left, "data.xyz").write_bytes(b"x")
    section = {"left_path": str(left), "right_path": str(right), "editor": "x"}
    if tools is not None:
        section["tools"] = tools
    w = filer.FilerWindow({"filer": section}, settings_path)
    w.menu_local_dir = os.path.join(tmp, "localmenus")
    w.menu_app_path = lambda name: None
    w.menu_which = lambda name: ("C:\\Windows\\explorer.exe" if name.lower() == "explorer.exe"
                                 else None)
    w.menu_exists = lambda p: os.path.isfile(p) or p == "C:\\Windows\\explorer.exe"
    w.show()
    w.start()
    _wait(w)
    return w, left, right, jp


def _drive_menu(w, letters, seen=None):
    """_exec_menu の差し替え: 実物の _ChooserMenu を popup で出し、文字キー → Enter を送って
    triggered した値を返す(exec はモーダルで止まるので使わない)。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    def run(menu, chosen):
        fired = []
        menu.triggered.connect(lambda action: fired.append(chosen.get(action)))
        if seen is not None:
            seen.append([a.text() for a in menu.actions() if not a.isSeparator()])
        menu.popup(w.mapToGlobal(QPoint(40, 40)))
        QTest.qWait(30)
        for letter in letters:
            QTest.keyClick(menu, getattr(Qt, f"Key_{letter}"))
            QTest.qWait(10)
        assert not fired, "文字キーで実行された"
        QTest.keyClick(menu, Qt.Key_Return)
        QTest.qWait(30)
        menu.hide()
        return fired[0] if fired else None
    return run


def test_window_ext_menu():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        pyw = Path(tmp, "tools dir", "pythonw.exe")
        pyw.parent.mkdir()
        pyw.write_bytes(b"")
        workdir = Path(tmp, "work")
        workdir.mkdir()
        w, left, right, jp = _menu_window(tmp)
        L, R = w.left, w.right
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append((list(args), kwargs.get("cwd")))
        real_popen = filer.subprocess.Popen
        filer.subprocess.Popen = FakePopen
        try:
            assert core.DEFAULT_KEYMAP["2013"] == "ext_menu"
            # フォルダ: 頭文字 O で「反対側で開く」を選び、Enter で &EXCD -O が効く
            seen = []
            w._exec_menu = _drive_menu(w, "O", seen)
            L.set_cursor(L._row_of("sub1"))
            key(Qt.Key_Return, Qt.ControlModifier)
            _wait(w)
            assert core.same_dir(R.path, left / "sub1"), R.path
            assert core.same_dir(L.path, left)
            labels = seen[0]
            assert labels[0].startswith("&P:開く"), labels
            # PhotoCuller は tools に無く App Paths でも見つからないので出ない
            assert not any("PhotoCuller" in t for t in labels), labels
            assert labels[-2:] == ["&T:ツールを追加…", "&M:メニューを編集"], labels
            # P(先頭のまま Enter)は今の窓でそのフォルダを開く(末尾 \ 付き → 中を開く)
            w._exec_menu = _drive_menu(w, "")
            L.set_cursor(L._row_of("sub2"))
            key(Qt.Key_Return, Qt.ControlModifier)
            _wait(w)
            assert core.same_dir(L.path, left / "sub2"), L.path
            # ".." の上は今のフォルダ自身が対象(E でエクスプローラへ今のフォルダ)
            L.set_cursor(L._row_of(".."))
            w._exec_menu = _drive_menu(w, "E")
            key(Qt.Key_Return, Qt.ControlModifier)
            assert calls[-1] == (["C:\\Windows\\explorer.exe", str(left / "sub2")],
                                 str(left / "sub2")), calls
            # 定義の無い拡張子は今までどおり関連付けで開く
            L.load(str(left))
            _wait(w)
            opened = []
            w._startfile = opened.append
            w._exec_menu = lambda m, c: (_ for _ in ()).throw(AssertionError("メニューが出た"))
            L.set_cursor(L._row_of("data.xyz"))
            key(Qt.Key_Return, Qt.ControlModifier)
            assert opened == [str(left / "data.xyz")], opened
        finally:
            filer.subprocess.Popen = real_popen
            w.close()
            w.deleteLater()

        # tools に書けば出る。外部コマンドは日本語・空白入りのパスでも1引数のまま
        tools = {"photoculler": {"command": [str(pyw), "-m", "photo_culler"], "cwd": str(workdir)}}
        second = os.path.join(tmp, "b")
        os.makedirs(second)
        w, left, right, jp = _menu_window(second, tools=tools)
        calls.clear()
        filer.subprocess.Popen = FakePopen
        try:
            seen = []
            w._exec_menu = _drive_menu(w, "C", seen)
            w.left.set_cursor(w.left._row_of("写真 フォルダ"))
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert any("PhotoCuller" in t for t in seen[0]), seen
            assert calls == [([str(pyw), "-m", "photo_culler", str(jp)], str(workdir))], calls
            # 同じ文字の項目が複数あれば押すたびに次へ(この PC の分に P を足す)
            Path(w.menu_local_dir).mkdir(parents=True, exist_ok=True)
            Path(w.menu_local_dir, "folder.txt").write_text(
                '"P:この PC の P" explorer.exe /select, $MF\n', encoding="utf-8")
            calls.clear()
            w._exec_menu = _drive_menu(w, "PP")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert calls == [(["C:\\Windows\\explorer.exe", "/select,", str(jp)],
                              str(Path(jp).parent))], calls
            # 「メニューを編集」: この PC の追加ファイルをエディタで(無ければ雛形を作る)
            calls.clear()
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            w._exec_menu = _drive_menu(w, "M")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            made = Path(w.menu_local_dir, "txt.txt")
            assert made.exists() and calls == [(["x", str(made)], None)], calls
            # テキストの &EDIT はエディタ(filer.editor)で対象を
            calls.clear()
            w._exec_menu = _drive_menu(w, "E")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert calls == [(["x", str(Path(w.left.path, "alpha.txt"))], None)], calls
        finally:
            filer.subprocess.Popen = real_popen
            w.close()
            w.deleteLater()


def test_window_ext_menu_property_and_archive():
    """&PROPERTY は子プロセス(filer_contextmenu.py --verb properties)で。書庫の中では
    外部ツール・&PROPERTY を出さない。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        w, left, right, jp = _menu_window(tmp)
        with zipfile.ZipFile(left / "pack.zip", "w") as z:
            z.writestr("in/a.txt", b"a")
        calls = []
        real_popen = filer.subprocess.Popen
        filer.subprocess.Popen = lambda args, **kw: calls.append(list(args))  # noqa: E731
        try:
            w.left.reload()
            _wait(w)
            w.left.set_cursor(w.left._row_of("sub1"))
            w._exec_menu = _drive_menu(w, "R")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert calls and calls[0][1].endswith("filer_contextmenu.py"), calls
            args = core.parse_context_args(calls[0][2:])
            assert args["verb"] == "properties" and args["paths"] == [str(left / "sub1")], args
            # 書庫の中
            w.left.load(str(left / "pack.zip" / "in"))
            _wait(w)
            seen = []
            w._exec_menu = lambda m, c: (seen.append([a.text() for a in m.actions()]), None)[1]
            w.left.set_cursor(w.left._row_of("a.txt"))
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            texts = " ".join(seen[0])
            assert "内蔵ビュア" in texts and "エディタ" in texts, texts
            assert "メモ帳" not in texts and "プロパティ" not in texts and "選択" not in texts, texts
        finally:
            filer.subprocess.Popen = real_popen
            w.close()
            w.deleteLater()


def test_window_add_tool_from_menu():
    """Ctrl+Enter → ツールを追加: この PC だけ / 全 PC で共有 のそれぞれで、メニューの
    ファイルと settings.json の filer.tools(キーだけ)がどうなるか。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text(json.dumps({"hotkeys": {"x": "y"}, "filer": {
            "editor": "x", "tools": {"keep": "C:\\keep.exe"}}}), encoding="utf-8")
        shared = Path(tmp, "shared")
        shutil.copytree(Path(__file__).resolve().parent.parent / "filer_menus", shared)
        w, left, right, jp = _menu_window(tmp, settings_path=str(settings_path))
        w.menu_shared_dir = shared
        exe = str(Path(tmp, "My Tool", "tool.exe"))
        Path(exe).parent.mkdir()
        Path(exe).write_bytes(b"")
        try:
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            asked = []

            def ask(kind, editor, initial=None):
                asked.append(kind)
                return {"label": "マイツール", "key": "Y", "program": ("exe", exe),
                        "args": "$MF", "cwd": "", "scope": "local"}
            w.ask_add_tool = ask
            w._exec_menu = _drive_menu(w, "T")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert asked == ["txt"]
            local = Path(w.menu_local_dir, "txt.txt")
            assert local.read_text(encoding="utf-8") == '"Y:マイツール" {tool:tool} $MF\n'
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            assert stored["filer"]["tools"] == {"keep": "C:\\keep.exe",
                                                "tool": {"command": [exe]}}, stored
            assert stored["hotkeys"] == {"x": "y"} and stored["filer"]["editor"] == "x"
            assert w.tools["tool"] == {"command": [exe]}
            # 次の Ctrl+Enter ですぐ出る
            seen = []
            w._exec_menu = lambda m, c: (seen.append([a.text() for a in m.actions()]), None)[1]
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert "&Y:マイツール" in seen[0], seen
            # 全 PC で共有: リポジトリ側(ここでは写し)に {tool:} で。ツールは使い回し
            w.ask_add_tool = lambda kind, editor, initial=None: {
                "label": "共有ツール", "key": "K", "program": ("exe", exe), "args": "$MF",
                "cwd": "", "scope": "shared"}
            w._exec_menu = _drive_menu(w, "T")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert (shared / "txt.txt").read_text(encoding="utf-8") == '"K:共有ツール" {tool:tool} $MF\n'
            assert "commit・push" in w.log_view.toPlainText()
            assert not (shared / "txt.txt.bak").exists()
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            assert set(stored["filer"]["tools"]) == {"keep", "tool"}
            # 個人のパスが共有に入る書き方は断る(ファイルは変わらない)
            w.ask_add_tool = lambda kind, editor, initial=None: {
                "label": "直書き", "key": "Z", "program": ("raw", exe), "args": "",
                "cwd": "", "scope": "shared"}
            w._exec_menu = _drive_menu(w, "T")
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert "直書き" not in (shared / "txt.txt").read_text(encoding="utf-8")
            assert "追加できません" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()


def test_add_tool_dialog():
    import filer_extmenu as m
    import filer_settings
    from PySide6.QtWidgets import QWidget

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        ed = m.MenuEditor({"vlc": {"command": ["C:\\VLC\\vlc.exe"]}}, Path(tmp, "s"),
                          Path(tmp, "l"))
        parent = QWidget()
        cand = ["C:\\VLC\\vlc.exe", "C:\\MPC\\mpc.exe"]
        d = filer_settings.AddToolDialog(
            parent, "mp4", ed, candidates=lambda kind: cand,
            describe=lambda p: {"C:\\MPC\\mpc.exe": "Media Player Classic"}.get(p, ""))
        try:
            texts = [d.program_combo.itemText(i) for i in range(d.program_combo.count())]
            assert texts[1].startswith("★ vlc"), texts
            assert "Media Player Classic（mpc.exe）" in texts and "vlc.exe" in texts, texts
            assert d.error_text() == "実行するものを選んでください"
            d.program_combo.setCurrentIndex(texts.index("Media Player Classic（mpc.exe）"))
            assert d.label_edit.text() == "Media Player Classic" and d.key_edit.text() == "M"
            assert d.args_edit.text() == "$MF" and d.error_text() is None
            d.insert_macro("$P")
            assert d.args_edit.text() == "$MF $P"
            # 参照で選んだ exe は候補に足されて選ばれる
            d.pick_exe = lambda: "C:/Other/new app.exe"
            d.browse_exe()
            assert d.program_combo.currentData() == ("exe", os.path.normpath("C:/Other/new app.exe"))
            d.scope_shared.setChecked(True)
            value = d.result_value()
            # 表示名を自分で変えていなければ、選び直した exe の名前に付いていく
            assert value["label"] == "new app" and value["key"] == "N", value
            assert value["scope"] == "shared" and value["args"] == "$MF $P", value
            d.label_edit.setText("自分の名前")
            d._label_edited("自分の名前")
            d.add_exe("C:\\MPC\\mpc.exe")
            assert d.label_edit.text() == "自分の名前"
            d.key_edit.setText("あ")
            assert "英数字" in d.error_text()
        finally:
            d.deleteLater()
        # 変更: 初期値が入り、保存先は この PC に固定
        d = filer_settings.AddToolDialog(parent, "mp4", ed, initial={
            "label": "VLC", "key": "V", "program": ("tool", "vlc"), "args": '"$MF"'})
        try:
            assert d.program_combo.currentData() == ("tool", "vlc")
            assert d.label_edit.text() == "VLC" and d.args_edit.text() == '"$MF"'
            d.scope_shared.setChecked(True)
            assert d.result_value()["scope"] == "local"
        finally:
            d.deleteLater()
            parent.deleteLater()


def test_window_settings_tool_tab():
    """Z の「拡張子とツール」: 追加・変更・削除・上下。共有の分は変えられない。決定で書く。"""
    import filer
    from PySide6.QtWidgets import QDialog

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text(json.dumps({"filer": {"editor": "x"}}), encoding="utf-8")
        shared = Path(tmp, "shared")
        shared.mkdir()
        (shared / "_aliases.txt").write_text("text: txt md\n", encoding="utf-8")
        (shared / "text.txt").write_text('"V:見る" &VIEW $MF\n', encoding="utf-8")
        w, left, right, jp = _menu_window(tmp, settings_path=str(settings_path))
        w.menu_shared_dir = shared
        Path(w.menu_local_dir).mkdir(parents=True)
        Path(w.menu_local_dir, "md.txt").write_text('# memo\n"A:一" &NOP\n', encoding="utf-8")
        exe = str(Path(tmp, "app.exe"))
        Path(exe).write_bytes(b"")
        state = {}

        def drive(dialog):
            if not hasattr(dialog, "kind_list"):
                return QDialog.Rejected
            try:
                kinds = [dialog.kind_list.item(i).text() for i in range(dialog.kind_list.count())]
                assert kinds[0] == "folder" and "md" in kinds and "txt" in kinds, kinds
                assert "text" not in kinds                       # グループ名は出さない
                dialog.select_kind("md")
                rows = [dialog.tool_tree.topLevelItem(i).text(1)
                        for i in range(dialog.tool_tree.topLevelItemCount())]
                assert rows == ["見る", "一"], rows
                dialog.select_tool_row(0)
                assert dialog.current_local() is None            # 共有の分は編集できない
                dialog.confirm = lambda text: True
                dialog.tool_delete()
                dialog.tool_edit()
                assert dialog.tool_tree.topLevelItemCount() == 2
                answers = [{"label": "アプリ", "key": "P", "program": ("exe", exe),
                            "args": "$MF", "cwd": "", "scope": "local"},
                           {"label": "二", "key": "B", "program": ("raw", "&NOP"), "args": ""}]
                dialog.ask_tool = lambda kind, initial=None: (
                    state.setdefault("initial", []).append(initial), answers.pop(0))[1]
                dialog.tool_add()                                 # 一, アプリ
                assert dialog.current_local() == 1
                dialog.tool_edit()                                # アプリ → 二(&NOP)
                assert state["initial"][1]["program"] == ("tool", "app"), state
                dialog.tool_move(-1)                              # 二, 一
                assert dialog.current_local() == 0
                dialog.select_tool_row(2)                         # 一
                dialog.tool_delete()
                rows = [dialog.tool_tree.topLevelItem(i).text(1)
                        for i in range(dialog.tool_tree.topLevelItemCount())]
                assert rows == ["見る", "二"], rows
                # 決定前はファイルに触らない
                assert Path(w.menu_local_dir, "md.txt").read_text(encoding="utf-8") == \
                    '# memo\n"A:一" &NOP\n'
                dialog.ask_kind = lambda: ".PDF"
                dialog.tool_add_kind()
                assert dialog.current_kind() == "pdf"
            except BaseException as e:  # noqa: BLE001
                state["error"] = e
                return QDialog.Rejected
            return QDialog.Accepted
        w.exec_dialog = drive
        try:
            w.cmd_settings_dialog()
            if "error" in state:
                raise state["error"]
            text = Path(w.menu_local_dir, "md.txt").read_text(encoding="utf-8")
            assert text == '# memo\n"B:二" &NOP\n', text
            assert Path(w.menu_local_dir, "md.txt.bak").read_text(encoding="utf-8") == \
                '# memo\n"A:一" &NOP\n'
            stored = json.loads(settings_path.read_text(encoding="utf-8"))
            assert stored["filer"]["tools"] == {"app": {"command": [exe]}}, stored
            assert stored["filer"]["editor"] == "x"
            assert (shared / "text.txt").read_text(encoding="utf-8") == '"V:見る" &VIEW $MF\n'
        finally:
            w.close()
            w.deleteLater()


def test_window_title_shows_version():
    import filer
    import filer_update

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        w, left, right, jp = _menu_window(tmp)
        try:
            import re
            _wait_until(lambda: w._version_done)
            assert w.version == filer_update.version_label(filer_update.REPO_DIR)
            assert w.windowTitle() == filer_update.window_title(w.left.path, w.version)
            if filer_update.has_git():
                assert w.version and " - あっふ " in w.windowTitle(), w.windowTitle()
                if shutil.which("git"):
                    assert re.fullmatch(r"\d{8}-\d{2}", w.version), w.version
                # タイトルは版だけ。ハッシュはメッセージ窓のログに残す
                sha = w.version_info["sha"][:7]
                assert sha not in w.windowTitle()
                assert f"版: {w.version}（{sha}）" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()


def _wait_until(cond, timeout=5.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        QTest.qWait(20)
    raise AssertionError("待ちきれない")


def test_window_update_restart():
    """Shift+F5: 最新 / 新コミットあり→pull→再起動 / 早送り不可 / ZIP / 失敗。
    本物の git・本物の再起動は使わず、呼び出しを差し替えて順序を見る。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w, left, right, jp = _menu_window(tmp, settings_path=str(settings_path))
        repo = Path(tmp, "repo")
        (repo / ".git").mkdir(parents=True)
        w.repo_dir = repo
        order = []
        asked = []
        answers = []
        w.confirm = lambda title, text, default_yes=True: (asked.append(text), answers.pop(0))[1]
        w.release_instance = lambda: order.append("release")
        w.spawn_successor = lambda: order.append("spawn")
        w.exit_app = lambda: order.append("exit")
        w.reacquire_instance = lambda: order.append("reacquire")
        real_save = w.save_state
        w.save_state = lambda: (order.append("save"), real_save())
        pulls = []
        w.pull_updates = lambda repo_dir: (pulls.append(repo_dir), {"ok": True,
                                                                    "message": "Fast-forward"})[1]

        _wait_until(lambda: w._version_done)    # 起動時の版の読み込みと競らない

        def press():
            QTest.keyClick(w.active.view, Qt.Key_F5, Qt.ShiftModifier)
            _wait_until(lambda: not w._update_busy)
            QTest.qWait(30)
        try:
            assert core.DEFAULT_KEYMAP["1116"] == "update_restart"
            assert core.fkey_labels(w.keymap, core.SHIFT)[4] == "更新"
            # 最新 → 再起動だけ(OK)。順序は 保存 → 手放す → 起こす → 終わる
            w.check_updates = lambda repo_dir: {"state": "uptodate"}
            answers[:] = [True]
            press()
            assert "最新です" in asked[-1] and order == ["save", "release", "spawn", "exit"], order
            assert w._restarting and json.loads(settings_path.read_text(encoding="utf-8"))["filer"]
            w._restarting = False
            # 取り込むものは無いが、起動後にこの PC で commit/pull していてファイルの方が新しい
            # → 版を並べて「再起動して反映」を案内(既定は はい)
            order.clear()
            defaults = []
            w.confirm = lambda title, text, default_yes=True: (
                asked.append(text), defaults.append(default_yes), answers.pop(0))[2]
            w.version = "20261030-01"
            w.version_info = {"label": "20261030-01", "sha": "a" * 40, "source": "git"}
            w.disk_version = lambda repo_dir: {"label": "20261031-02", "sha": "c" * 40}
            answers[:] = [True]
            press()
            assert "20261031-02（ccccccc）" in asked[-1], asked[-1]
            assert "20261030-01（aaaaaaa）" in asked[-1], asked[-1]
            assert "最新です" not in asked[-1] and defaults[-1] is True
            assert order == ["save", "release", "spawn", "exit"], order
            w._restarting = False
            # 版が同じなら従来どおり「最新です。再起動だけ」(既定は いいえ)。断れば何もしない
            order.clear()
            w.disk_version = lambda repo_dir: {"label": "20261030-01", "sha": "a" * 40}
            answers[:] = [False]
            press()
            assert "最新です" in asked[-1] and defaults[-1] is False and order == []
            # 同じコミットなら、表記が違っても(git が使えず日付だけになった等)同じ版
            w.disk_version = lambda repo_dir: {"label": "20261030", "sha": "a" * 40}
            answers[:] = [False]
            press()
            assert "最新です" in asked[-1] and order == []
            # 新しいコミットあり → 一覧を見せて OK → pull → 常駐のファイルがあれば
            # 「常駐も再起動しますか？」(断ればログで案内) → あっふを再起動
            order.clear()
            resident_calls = []
            informed = []
            w.restart_resident = lambda: (resident_calls.append(1),
                                          (True, "再起動します（数秒で戻ります）"))[1]
            w.inform = lambda title, text: informed.append(text)
            w.check_updates = lambda repo_dir: {
                "state": "behind", "behind": 2, "ahead": 0, "dirty": [],
                "commits": ["abc1234 あっふ: 直す", "def5678 常駐: 足す"],
                "files": ["filer.py", "main.py"]}
            answers[:] = [True, False]
            press()
            _wait_until(lambda: order)
            assert "abc1234 あっふ: 直す" in asked[-2] and "2件" in asked[-2], asked[-2]
            assert "常駐も再起動しますか" in asked[-1] and "main.py" in asked[-1], asked[-1]
            assert "filer.py" not in asked[-1]
            assert pulls == [repo] and order == ["save", "release", "spawn", "exit"], order
            assert resident_calls == [] and "常駐側も更新されました" in w.log_view.toPlainText()
            w._restarting = False
            # 「はい」なら常駐へ頼んでから、あっふを再起動する
            order.clear()
            answers[:] = [True, True]
            press()
            _wait_until(lambda: order)
            assert resident_calls == [1] and informed == []
            assert "常駐: 再起動します" in w.log_view.toPlainText()
            assert order == ["save", "release", "spawn", "exit"], order
            w._restarting = False
            # 常駐に断られたら、その理由を窓で見せる(あっふの再起動は続ける)
            order.clear()
            w.restart_resident = lambda: (False, "常駐は再起動しませんでした（再起動は見送る"
                                                 "べきです: 付箋 1枚）。トレイの 🔄 から行ってください")
            answers[:] = [True, True]
            press()
            _wait_until(lambda: order)
            assert informed and "付箋 1枚" in informed[-1], informed
            assert order == ["save", "release", "spawn", "exit"], order
            w._restarting = False
            # 常駐のファイルが無ければ聞かない
            order.clear()
            w.check_updates = lambda repo_dir: {
                "state": "behind", "behind": 1, "ahead": 0, "dirty": [],
                "commits": ["abc1234 あっふ: 直す"], "files": ["filer.py", "README.md"]}
            answers[:] = [True]
            press()
            _wait_until(lambda: order)
            assert "常駐も再起動" not in asked[-1] and len(informed) == 1
            w._restarting = False
            # 取り込みを断れば何もしない
            order.clear()
            answers[:] = [False]
            press()
            assert order == [] and len(pulls) == 4   # 上の4回ぶん。断った分は増えない
            # 早送りできない → 何もせず理由を出す(確認も出さない)
            asked.clear()
            w.check_updates = lambda repo_dir: {"state": "diverged", "ahead": 1, "behind": 2,
                                                "upstream": "origin/main"}
            press()
            assert asked == [] and order == [] and "分岐" in w.log_view.toPlainText()
            w.check_updates = lambda repo_dir: {"state": "error",
                                                "message": "git が見つかりません"}
            press()
            assert order == [] and "git が見つかりません" in w.log_view.toPlainText()
            # 調べる側が例外でも落ちない
            w.check_updates = lambda repo_dir: 1 / 0
            press()
            assert order == [] and "更新を確かめられません" in w.log_view.toPlainText()
            # ZIP 展開(.git 無し) → ダウンロードフォルダの ZIP で更新する。
            # 見つからなければ案内(ファイル選択→再起動だけ、両方断れば何もしない)
            w.repo_dir = Path(tmp, "zip")
            w.repo_dir.mkdir()
            w.find_update_zip = lambda: None
            answers[:] = [False, False]
            QTest.keyClick(w.active.view, Qt.Key_F5, Qt.ShiftModifier)
            assert "見つかりませんでした" in asked[-2] and "再起動だけ" in asked[-1], asked
            assert order == []
            # ZIP が読めない場合も、別のファイルを選ぶ案内→再起動だけ、で何もしない
            w.find_update_zip = lambda: Path(tmp, "update.zip")
            w.zip_check = lambda zip_path: {"state": "error", "zip_path": str(zip_path),
                                            "message": "tray-tools の ZIP ではありません"}
            answers[:] = [False, False]
            press()
            assert "別の ZIP ファイルを選んで" in asked[-2] and "再起動だけ" in asked[-1], asked
            assert order == []
            # ZIP に差分あり → 件数を見せて OK → 適用 → 再起動
            w.zip_check = lambda zip_path: {
                "state": "ready", "zip_path": "", "label": "ZIP 20261104",
                "archive": {"entries": {"new.py": {}}, "label": "ZIP 20261104"},
                "plan": {"add": ["new.py"], "overwrite": [], "delete": [],
                         "unchanged": 3, "requirements_changed": False},
                "manifest_present": True}
            applied = []
            w.zip_apply = lambda prepared: (applied.append(prepared), {
                "ok": True, "added": 1, "overwritten": 0, "deleted": 0,
                "backup": "", "pip": None, "files": ["new.py"]})[1]
            answers[:] = [True, False]   # new.py は常駐が読むファイル → 常駐の再起動は断る
            press()
            _wait_until(lambda: len(order) >= 4)
            assert "常駐も再起動しますか" in asked[-1] and "new.py" in asked[-1], asked[-1]
            assert applied and "ZIP 20261104" in asked[-2] and "追加 1件" in asked[-2], asked
            assert order == ["save", "release", "spawn", "exit"], order
            assert "ZIP で更新しました" in w.log_view.toPlainText()
            w._restarting = False
            # 以後のケース(転送の残り・起こす失敗)は「最新」の応答で進める
            order.clear()
            w.zip_check = lambda zip_path: {"state": "uptodate", "zip_path": ""}
            # 転送が残っていれば確かめる(断れば再起動しない)
            w._op_queue = [object()]
            answers[:] = [True, False]
            QTest.keyClick(w.active.view, Qt.Key_F5, Qt.ShiftModifier)
            _wait_until(lambda: asked and "ファイル転送が残っています" in asked[-1])
            assert order == [], order
            w._op_queue = []
            # 起こせなければ取り戻して残る
            w.spawn_successor = lambda: (order.append("spawn"), (_ for _ in ()).throw(OSError("x")))
            answers[:] = [True]
            QTest.keyClick(w.active.view, Qt.Key_F5, Qt.ShiftModifier)
            _wait_until(lambda: "reacquire" in order)
            assert order == ["save", "release", "spawn", "reacquire"], order
            assert not w._restarting
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# Everything 検索(Shift+F) / フォルダ構成をコピー(C→I) / ワークスペース(Alt+J)
# ---------------------------------------------------------------
# es.exe の代わりに起こす小さな偽物。本物の Everything が無い CI でも通るように、
# 引数を本物と同じ形で受け取り、-path(無ければ FAKE_ES_ROOT)の下を歩いて名前で探し、
# -export-txt のファイルへ書く(-utf8-bom なら BOM 付き UTF-8)。
#   FAKE_ES_EXIT … その終了コードで終わる(8 = Everything が起動していない)
#   FAKE_ES_SLEEP … 応答までの秒数(タイムアウト・中止の試験)
#   FAKE_ES_LOG … 受け取った引数を JSON で書く
_FAKE_ES = r'''
import json, os, sys, time
args = sys.argv[1:]
if os.environ.get("FAKE_ES_LOG"):
    with open(os.environ["FAKE_ES_LOG"], "w", encoding="utf-8") as f:
        json.dump(args, f, ensure_ascii=False)
if os.environ.get("FAKE_ES_SLEEP"):
    time.sleep(float(os.environ["FAKE_ES_SLEEP"]))
if os.environ.get("FAKE_ES_EXIT"):
    sys.stderr.write("Error: fake\n")
    sys.exit(int(os.environ["FAKE_ES_EXIT"]))
limit, out, bom, root, words = 1000000, None, False, None, []
i = 0
while i < len(args):
    a = args[i]
    if a == "-n":
        limit = int(args[i + 1]); i += 2; continue
    if a == "-export-txt":
        out = args[i + 1]; i += 2; continue
    if a == "-path":
        root = args[i + 1]; i += 2; continue
    if a == "-utf8-bom":
        bom = True; i += 1; continue
    if a.startswith("-"):
        sys.exit(6)          # 本物と同じく、知らないスイッチは 6
    words.append(a.strip("*").casefold()); i += 1
root = root or os.environ["FAKE_ES_ROOT"]
found = []
for folder, dirs, files in os.walk(root):
    for name in sorted(dirs) + sorted(files):
        if all(w in name.casefold() for w in words):
            path = os.path.join(folder, name)
            found.append(path + ("\\" if name in dirs else ""))
text = "".join(p + "\r\n" for p in found[:limit])
with open(out, "wb") as f:
    f.write((b"\xef\xbb\xbf" + text.encode("utf-8")) if bom else text.encode("cp932", "replace"))
'''


def _fake_es(tmp):
    path = Path(tmp, "fake_es.py")
    path.write_text(_FAKE_ES, encoding="utf-8")
    return [sys.executable, str(path)]


class _FakeEnv:
    """FAKE_ES_* を一時的に立てる(終わったら必ず戻す)。"""

    def __init__(self, **values):
        self.values = values

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.values}
        for k, v in self.values.items():
            os.environ[k] = str(v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_everything_keys_and_names():
    km = core.DEFAULT_KEYMAP
    assert km["1070"] == "everything" and km["0070"] == "isearch"
    assert km["4074"] == "workspace" and km["0074"] == "bookmark_jump"
    assert core.COMMAND_SHORT_NAMES["everything"] and core.COMMAND_SHORT_NAMES["workspace"]
    import filer
    for command in ("everything", "workspace"):
        assert hasattr(filer.FilerWindow, f"cmd_{command}"), command


def test_everything_query_args_and_parse():
    import filer_everything as fe
    # 空白区切りで1語1引数。"..." は1つの語句。- / で始まる語はスイッチと取り違えないよう *語*
    assert fe.split_query("写真  春") == ["写真", "春"]
    assert fe.split_query('"a b" ext:pdf') == ["a b", "ext:pdf"]
    assert fe.split_query("-dash /ad") == ["*-dash*", "*/ad*"]
    assert fe.split_query('  "" ') == []
    args = fe.build_args(["es.exe", "-instance", "1.5a"], ["x"], "C:\\Users\\a\\", 1000, "o.txt")
    assert args[:3] == ["es.exe", "-instance", "1.5a"]
    assert args[args.index("-n") + 1] == "1001"          # 上限の判定のため1件多く取る
    assert args[args.index("-export-txt") + 1] == "o.txt" and "-utf8-bom" in args
    assert args[args.index("-path") + 1] == "C:\\Users\\a"   # 末尾の \ は落とす
    assert args[-1] == "x"
    assert "-path" not in fe.build_args(["es.exe"], ["x"], None, 10, "o.txt")   # 全体
    assert fe.build_args(["es.exe"], ["x"], "D:\\", 10, "o")[-2] == "D:\\"     # ドライブ直下
    # 書き出しの読み取り: BOM 付き UTF-8(日本語・cp932 に無い字も化けない)、フォルダの \ を落とす
    text = "C:\\写真\\春\\\r\nC:\\写真\\🍣メモ.txt\r\n\r\n"
    assert fe.parse_export(b"\xef\xbb\xbf" + text.encode("utf-8")) == [
        "C:\\写真\\春", "C:\\写真\\🍣メモ.txt"]
    # BOM 無しで UTF-8 として読めなければ cp932
    assert fe.parse_export("D:\\表\\".encode("cp932")) == ["D:\\表"]
    assert fe.parse_export(b"\xef\xbb\xbf") == []
    assert "起動していません" in fe.error_message(8)
    assert "99" in fe.error_message(99, b"Error: x")


def test_everything_find_es():
    import filer_everything as fe

    class R:
        def __init__(self, spec):
            self.spec = spec

        def tool(self, name):
            assert name == "es"
            return self.spec

    # filer.tools の "es"(追加の引数ごと)が最優先
    assert fe.find_es(R((["D:\\t\\es.exe", "-instance", "1.5a"], None)), {}) == [
        "D:\\t\\es.exe", "-instance", "1.5a"]
    # 無ければ既定のインストール先
    env = {"ProgramFiles": "C:\\PF", "ProgramFiles(x86)": "C:\\PF86"}
    assert fe.find_es(R(None), env, exists=lambda p: p == "C:\\PF86\\Everything\\es.exe") == [
        "C:\\PF86\\Everything\\es.exe"]
    assert fe.find_es(R(None), env, exists=lambda p: False) is None
    assert fe.find_es(None, {}, exists=lambda p: False) is None


def test_everything_run_search_fake_exe():
    """偽の es.exe(Python スクリプト)を本当に子プロセスで起こして、引数・UTF-8 の
    受け取り・上限・エラー・タイムアウト・中止を確かめる。"""
    import filer_everything as fe
    with tempfile.TemporaryDirectory() as tmp:
        es = _fake_es(tmp)
        root = Path(tmp, "写真")
        (root / "春").mkdir(parents=True)
        (root / "春" / "メモ🍣.txt").write_text("x", encoding="utf-8")
        (root / "メモ2.txt").write_text("x", encoding="utf-8")
        (root / "-dash メモ.txt").write_text("x", encoding="utf-8")
        log = Path(tmp, "args.json")
        with _FakeEnv(FAKE_ES_LOG=log):
            found = fe.run_search(es, "メモ", str(root), temp_dir=tmp)
        assert found.status == "done", found.message
        assert sorted(found.paths) == sorted([str(root / "春" / "メモ🍣.txt"),
                                              str(root / "メモ2.txt"),
                                              str(root / "-dash メモ.txt")]), found.paths
        sent = json.loads(log.read_text(encoding="utf-8"))
        assert sent[sent.index("-path") + 1] == str(root) and "-utf8-bom" in sent
        assert not list(Path(tmp).glob("affx_es_*"))         # 一時ファイルは片付ける
        # - で始まる語もスイッチにならない
        found = fe.run_search(es, "-dash", str(root), temp_dir=tmp)
        assert found.status == "done" and [os.path.basename(p) for p in found.paths] == [
            "-dash メモ.txt"], (found.status, found.message, found.paths)
        # 上限: 2件までにすると limit(2件で打ち切り)
        found = fe.run_search(es, "メモ", str(root), limit=2, temp_dir=tmp)
        assert found.status == "limit" and len(found.paths) == 2
        # 全体(-path 無し)
        with _FakeEnv(FAKE_ES_ROOT=root, FAKE_ES_LOG=log):
            found = fe.run_search(es, "春", None, temp_dir=tmp)
        assert found.paths == [str(root / "春")]
        assert "-path" not in json.loads(log.read_text(encoding="utf-8"))
        # Everything が起動していない(終了コード 8)
        with _FakeEnv(FAKE_ES_EXIT=8):
            found = fe.run_search(es, "メモ", str(root), temp_dir=tmp)
        assert found.status == "error" and "起動していません" in found.message
        # タイムアウト・中止(子プロセスは止める)
        with _FakeEnv(FAKE_ES_SLEEP=20):
            started = time.monotonic()
            found = fe.run_search(es, "メモ", str(root), timeout=1.0, temp_dir=tmp)
            assert found.status == "timeout" and time.monotonic() - started < 8
            cancel = threading.Event()
            threading.Timer(0.5, cancel.set).start()
            found = fe.run_search(es, "メモ", str(root), timeout=30, cancel=cancel, temp_dir=tmp)
            assert found.status == "cancel"
        # 起動できない exe
        found = fe.run_search([str(Path(tmp, "nothing.exe"))], "x", str(root), temp_dir=tmp)
        assert found.status == "error" and "起動できません" in found.message
        # 黒い窓を出さない(CREATE_NO_WINDOW)・shell を通さない
        seen = {}

        class FakePopen:
            def __init__(self, args, **kw):
                seen.update(kw, args=args)
                self.returncode = 0

            def communicate(self, timeout=None):
                Path(seen["args"][seen["args"].index("-export-txt") + 1]).write_bytes(
                    b"\xef\xbb\xbfC:\\x\r\n")
                return b"", b""

        found = fe.run_search(["es.exe"], "x", None, popen=FakePopen, temp_dir=tmp)
        assert found.paths == ["C:\\x"]
        assert seen["creationflags"] & 0x08000000 and not seen.get("shell")
        assert isinstance(seen["args"], list)


def test_everything_to_entries():
    import filer_everything as fe
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "r")
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.txt").write_bytes(b"abc")
        other = Path(tmp, "other.txt")
        other.write_bytes(b"1")
        entries, missing = fe.to_entries(
            [str(root / "sub"), str(root / "sub" / "a.txt"), str(other),
             str(root / "gone.txt"), str(root / "sub" / "a.txt")], str(root))
        assert missing == 1
        got = {e.name: (e.is_dir, e.size) for e in entries}
        # root より下は相対パス、外はフルパス(どちらも join で実パスになる)。重複は1つに
        assert got == {"sub": (True, 0), "sub\\a.txt": (False, 3), str(other): (False, 1)}, got
        assert all(os.path.exists(os.path.join(str(root), n)) for n in got)


def test_window_everything_search():
    """Shift+F: 結果がフラット表示と同じ一覧で出て、マーク・コピー・Enter・BS・F5 が効く。
    es.exe が無い・Everything が起動していないときはメッセージだけで落ちない。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        es = _fake_es(tmp)
        left = Path(tmp, "写真")
        right = Path(tmp, "dst")
        (left / "春").mkdir(parents=True)
        right.mkdir()
        (left / "春" / "メモ.txt").write_bytes(b"12345")
        (left / "メモ帳.md").write_bytes(b"1")
        (left / "zz.txt").write_bytes(b"1")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.extra_op_flags = _quiet_flags()
        w.resize(1000, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        asked = []
        # Shift+F のメニューでは E(Everything 検索)を選ぶ(メニューは出さずに値だけ返す)
        w._exec_menu = lambda menu, chosen: "everything_search"
        try:
            # es.exe が見つからない: 入力窓も出さずにメッセージだけ
            w.find_es = lambda: None
            w.ask_everything = lambda text, scope_all: (asked.append(1), ("メモ", False))[1]
            key(Qt.Key_F, Qt.ShiftModifier)
            assert not asked and "es.exe が見つかりません" in w.log_view.toPlainText()
            assert not L.flat

            w.find_es = lambda: list(es)
            key(Qt.Key_F, Qt.ShiftModifier)
            _wait(w, timeout=20)
            assert L.flat and L.es_query == "メモ", (L.flat, L.es_query, w.log_view.toPlainText())
            assert "Everything" in L.header.text() and "メモ" in L.header.text()
            assert ".." not in _names(L)
            assert sorted(_names(L)) == sorted(["春\\メモ.txt", "メモ帳.md"]), _names(L)
            assert L.footer.text().startswith("Everything「メモ」 2件"), L.footer.text()
            assert "Everything検索: 「メモ」 2件" in w.log_view.toPlainText()
            # R・K は断る(フラット表示と同じ)
            key(Qt.Key_K)
            assert "フラット表示・検索結果では使えません" in w.log_view.toPlainText()
            # マークして反対窓へコピー(対象は相対パスを解決したフルパス)
            L.marks.add("春\\メモ.txt")
            key(Qt.Key_C, Qt.AltModifier)
            _wait(w, timeout=30)
            assert (right / "メモ.txt").read_bytes() == b"12345"
            assert L.flat and L.es_query == "メモ"            # 再読込しても検索結果のまま
            # BS で通常の表示へ(親へは行かない)
            key(Qt.Key_Backspace)
            _wait(w)
            assert not L.flat and L.es_query is None and core.same_dir(L.path, left)
            assert "Everything" not in L.header.text()
            # 範囲を全体に: 外のものはフルパスで出る
            with _FakeEnv(FAKE_ES_ROOT=tmp):
                w.ask_everything = lambda text, scope_all: ("メモ", True)
                key(Qt.Key_F, Qt.ShiftModifier)
                _wait(w, timeout=20)
            assert L.es_all and "全体" in L.header.text() and w.es_scope_all
            names = _names(L)
            assert str(right / "メモ.txt") in names and "春\\メモ.txt" in names, names
            # フォルダの結果で Enter → そのフォルダへ入る(検索結果を抜ける)
            w.ask_everything = lambda text, scope_all: ("春", False)
            key(Qt.Key_F, Qt.ShiftModifier)
            _wait(w, timeout=20)
            assert _names(L) == ["春"], _names(L)
            L.set_cursor(0)
            key(Qt.Key_Return)
            _wait(w)
            assert not L.flat and core.same_dir(L.path, left / "春")
            key(Qt.Key_Backspace)
            _wait(w)
            # 上限を超えたら打ち切って知らせる
            w.es_limit = 1
            w.ask_everything = lambda text, scope_all: ("メモ", False)
            key(Qt.Key_F, Qt.ShiftModifier)
            _wait(w, timeout=20)
            assert len(_names(L)) == 1 and "上限で打ち切り" in L.footer.text()
            assert "1件で打ち切りました" in w.log_view.toPlainText()
            w.es_limit = 1000
            # Everything が起動していない: 通常の表示へ戻ってメッセージ
            with _FakeEnv(FAKE_ES_EXIT=8):
                key(Qt.Key_F, Qt.ShiftModifier)
                _wait(w, timeout=20)
            assert not L.flat and L.es_query is None and core.same_dir(L.path, left)
            assert "Everything が起動していません" in w.log_view.toPlainText()
            # Esc で中止
            with _FakeEnv(FAKE_ES_SLEEP=20):
                key(Qt.Key_F, Qt.ShiftModifier)
                QTest.qWait(300)
                assert L.es_loading and "検索中" in L.footer.text()
                key(Qt.Key_Escape)
                _wait(w, timeout=10)
            assert not L.flat and "Everything検索: 中止しました" in w.log_view.toPlainText()
            assert w.isVisible()                                 # Esc で窓は閉じない
        finally:
            w.close()
            w.deleteLater()


def test_folder_tree_text():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "写真")
        for d in ("2026/春", "2026/秋", "a10", "a9", "z_deep/1/2/3/4/5/6"):
            (root / d).mkdir(parents=True)
        (root / "memo.txt").write_text("x", encoding="utf-8")
        (root / "b.txt").write_text("x", encoding="utf-8")
        (root / "2026" / "春" / "p.jpg").write_bytes(b"")
        (root / "hid.txt").write_bytes(b"")
        _set_attrs(root / "hid.txt", 0x02)
        lines, cut = core.folder_tree(str(root), show_hidden=False, max_depth=10)
        assert not cut
        assert lines[:8] == [
            "写真",
            "├── 2026",
            "│   ├── 春",
            "│   │   └── p.jpg",
            "│   └── 秋",
            "├── a9",                 # 数は数の大きさで(a9 < a10)
            "├── a10",
            "├── z_deep",
        ], lines
        assert lines[-2:] == ["├── b.txt", "└── memo.txt"], lines   # フォルダが先、ファイルは後
        assert "hid.txt" not in "\n".join(lines)                     # Shift+Z に従う
        lines, _ = core.folder_tree(str(root), show_hidden=True, max_depth=10)
        assert any(line.endswith("hid.txt") for line in lines)
        # 深さの上限: 5段目より下は書かず、末尾に省略の行
        lines, cut = core.folder_tree(str(root), max_depth=5)
        assert cut and lines[-1].startswith(core.TREE_MORE) and "5段" in lines[-1]
        text = "\n".join(lines)
        assert "── 4" in text and "── 5" not in text, text
        # 行数の上限
        lines, cut = core.folder_tree(str(root), max_lines=4)
        assert cut and len(lines) == 5 and lines[-1].startswith(core.TREE_MORE), lines
        assert "4行" in lines[-1]
        # ジャンクションは名前だけ(中へは潜らない＝ループしない)
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "2026" / "loop"),
                               str(root)], capture_output=True).returncode == 0
        if made:
            lines, cut = core.folder_tree(str(root), max_depth=50, max_lines=100000)
            assert not cut
            assert any(line.endswith("loop" + core.TREE_LINK) for line in lines), lines
            assert len(lines) < 40, len(lines)
        # 読めないフォルダは印を付けて先へ(全体を止めない)
        real = os.scandir

        def scandir(path):
            if os.path.basename(path) == "秋":
                raise PermissionError(5, "アクセスが拒否されました")
            return real(path)

        lines, _ = core.folder_tree(str(root), max_depth=10, scandir=scandir)
        assert "│   └── 秋" + core.TREE_DENIED in lines, lines
        assert lines[-1].endswith("memo.txt")
        # ドライブ直下は名前の代わりにパス
        assert core.folder_tree("C:\\", max_depth=1, max_lines=2)[0][0] == "C:\\"
        # 根が読めなければ OSError
        try:
            core.folder_tree(str(root / "nothing"))
        except OSError:
            pass
        else:
            raise AssertionError("OSError にならない")


def test_window_copy_tree():
    """C→I: カーソルがフォルダならそのフォルダ、ファイルならアクティブ窓のフォルダを
    ツリーにしてクリップボードへ(裏のスレッドで作る)。"""
    import filer
    from PySide6.QtGui import QGuiApplication

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
        items = []

        def choose(title, menu_items):
            items.extend(menu_items)
            return "tree"

        w.choose_menu = choose
        try:
            L.set_cursor(_names(L).index("sub1"))
            QGuiApplication.clipboard().setText("")
            w.run_command("copy_menu")
            assert ("I", "フォルダ構成をコピー", "tree") in items
            _wait_until(lambda: QGuiApplication.clipboard().text() != "", timeout=10)
            assert QGuiApplication.clipboard().text() == "sub1\r\n└── deep"
            L.set_cursor(_names(L).index("alpha.txt"))
            QGuiApplication.clipboard().setText("")
            w.run_command("copy_menu")
            _wait_until(lambda: QGuiApplication.clipboard().text() != "", timeout=10)
            text = QGuiApplication.clipboard().text()
            assert text.split("\r\n")[:4] == ["L", "├── sub1", "│   └── deep", "├── sub2"], text
            assert "クリップボード: フォルダ構成" in w.log_view.toPlainText()
        finally:
            w.close()
            w.deleteLater()


def _add_box_item_table(conn, rows):
    """Box 側の木(box_item)を、(local_id, 種別, 親の local_id, 名前, box_id, box 種別) の
    並びから作る(親は親の box_id で指す。根の box_id は "0")。"""
    conn.execute("CREATE TABLE box_item (box_id VARCHAR, item_type INTEGER,"
                 " parent_item_id VARCHAR, name VARCHAR COLLATE NOCASE)")
    box_of = {lid: box_id for lid, _t, _p, _n, box_id, _bt in rows}
    for lid, _typ, parent, name, box_id, box_type in rows:
        conn.execute("INSERT INTO box_item VALUES (?,?,?,?)",
                     (box_id, box_type, None if parent is None else box_of[parent], name))


def test_window_copy_box_share():
    """Box の共有用テキスト: C メニューに独立した項目は無く、C→P の窓の中で選ぶ。
    URL は常に https://app.box.com/folder/<ID> の形(共有リンクは作らない・聞かない)。
    コピー後の確認窓は無い(プレビューで事前に確かめる)。同じ名前の記録が複数あって
    決められないものは作らない。Box の根と sync.db は一時フォルダに作った偽物
    (本物の Box Drive には触れない)。"""
    import sqlite3
    import filer
    import filer_box
    from PySide6.QtGui import QGuiApplication

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)       # Box の外(通常のフォルダ)
        box_root = Path(tmp, "home", "Box")
        folder = box_root / "ProjA" / "資料"
        folder.mkdir(parents=True)
        for name in ("見積書.xlsx", "memo.txt", "notsynced.txt"):
            (folder / name).write_bytes(b"x")
        db = Path(tmp, "local", "sync.db")
        db.parent.mkdir()
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE local_item (local_id INTEGER NOT NULL, native_item_type INTEGER,"
                     " parent_item_id INTEGER, name VARCHAR COLLATE NOCASE, PRIMARY KEY (local_id))")
        conn.execute("CREATE TABLE item_mapping_between_fs (id INTEGER, local_id INTEGER,"
                     " box_id VARCHAR, box_item_type INTEGER, PRIMARY KEY (id), UNIQUE (local_id))")
        rows = [(0, 1, None, "", "0", 1), (100, 1, 0, "ProjA", "9100", 1),
                (101, 1, 100, "資料", "9101", 1), (102, 0, 101, "見積書.xlsx", "9102", 0),
                (103, 0, 101, "memo.txt", "9103", 0)]
        for i, (lid, typ, parent, name, box_id, box_type) in enumerate(rows):
            conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (lid, typ, parent, name))
            conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                         (i, lid, box_id, box_type))
        _add_box_item_table(conn, rows)
        conn.commit()
        conn.close()
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        # 昔の版が残した box_links は読まない・触らない
        old_links = [{"folder_id": "9100", "path": "Box/ProjA", "host": "app.box.com",
                      "token": "abcdefghij1234567890"}]
        app_settings = {"filer": {"left_path": str(left), "right_path": str(right),
                                  "box_links": old_links}}
        seen = {}

        def capture(title, items):
            seen["items"] = items
            return None

        filer_box.ROOT_OVERRIDE, filer_box.DB_OVERRIDE = str(box_root), str(db)
        filer_box.discard_cache()
        w = filer.FilerWindow(app_settings, str(settings_path))
        try:
            w.show()
            w.start()
            _wait(w)
            # C メニューに Box の独立項目は無い(Box の中でも外でも)
            w.choose_menu = capture
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            w.cmd_copy_menu()
            assert "box" not in [i[2] for i in seen["items"]]
            w.left.load(str(folder))
            _wait(w)
            w.left.set_cursor(w.left._row_of("見積書.xlsx"))
            w.cmd_copy_menu()
            assert "box" not in [i[2] for i in seen["items"]]

            # 常に /folder/<ID> の形。登録を聞くことも、共有リンクを取りに行くこともない
            assert not hasattr(w, "ask_box_link") and not hasattr(w, "fetch_box_link")
            # コピー後の確認窓は廃止された(プレビューに置き換え)
            assert not hasattr(w, "show_box_copied")
            w.choose_menu = lambda title, items: "path"
            w.ask_copy_kind = lambda count, full_text, data: "box"
            clip = QGuiApplication.clipboard()
            w.left.set_cursor(w.left._row_of("見積書.xlsx"))
            w.cmd_copy_menu()
            expected = ("/Box/ProjA/資料/\r\n"
                        "　https://app.box.com/folder/9101\r\n"
                        "　　見積書.xlsx")
            assert clip.text() == expected, repr(clip.text())
            assert "Box: 1件をクリップボードへ" in w.log_view.toPlainText()
            # 古い box_links は読まず、書き換えもしない
            assert "/s/" not in clip.text()
            assert app_settings["filer"]["box_links"] == old_links
            assert json.loads(settings_path.read_text(encoding="utf-8")) == {}

            clip.setText("")
            w.left.set_cursor(w.left._row_of("memo.txt"))
            w.cmd_copy_menu()
            assert clip.text().endswith("　　memo.txt"), clip.text()

            # 同期の記録に無いものは飛ばしてログに出す。全部だめなら何もしない
            clip.setText("keep")
            w.left.set_cursor(w.left._row_of("notsynced.txt"))
            w.cmd_copy_menu()
            assert clip.text() == "keep"
            assert "Box: 同期の記録に見つかりません: notsynced.txt" in w.log_view.toPlainText()

            # --- 同じ名前の記録が複数ある(古い行の残りなど)と、リンクを作らない ---
            conn = sqlite3.connect(str(db))
            conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (190, 0, 101, "memo.txt"))
            conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                         (90, 190, "9190", 0))
            conn.commit()
            conn.close()
            filer_box.discard_cache()
            clip.setText("keep")
            w.left.set_cursor(w.left._row_of("memo.txt"))
            w.cmd_copy_menu()
            assert clip.text() == "keep"
            assert ("Box: 同じ名前の記録が複数あるため特定できません: memo.txt"
                    "（リンクは作りません）") in w.log_view.toPlainText()
            assert "同期の記録に見つかりません: memo.txt" not in w.log_view.toPlainText()

            # --- 手元の記録が Box 側の木と食い違う(古い対応が別の ID を指す)と、作らない ---
            conn = sqlite3.connect(str(db))
            conn.execute("DELETE FROM item_mapping_between_fs WHERE local_id=190")
            conn.execute("DELETE FROM local_item WHERE local_id=190")
            conn.execute("UPDATE item_mapping_between_fs SET box_id='9777' WHERE local_id=103")
            conn.commit()
            conn.close()
            filer_box.discard_cache()
            clip.setText("keep")
            w.cmd_copy_menu()
            assert clip.text() == "keep"
            assert ("Box: Box 側の記録と一致しないため、リンクを作りません: memo.txt"
                    in w.log_view.toPlainText())
        finally:
            filer_box.ROOT_OVERRIDE = filer_box.DB_OVERRIDE = None
            w.close()
            w.deleteLater()

def test_window_ext_menu_box_open():
    """Ctrl+Enter に「Box.comで開く」が出るのは、対象が Box Drive の中のときだけ。選ぶと
    Box 自身の右クリック項目「Box.comで表示」を子プロセス(filer_contextmenu.py --item)で
    実行する(Popen は差し替え。ブラウザは開かない)。"""
    import filer
    import filer_box

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        box_root = Path(tmp, "home", "Box")
        folder = box_root / "ProjA"
        (folder / "資料").mkdir(parents=True)
        (folder / "memo.txt").write_bytes(b"x")
        db = Path(tmp, "local", "sync.db")
        db.parent.mkdir()
        db.write_bytes(b"")         # 項目を出す条件(記録のファイルがある)だけ見る
        shared = Path(tmp, "shared")
        shared.mkdir()
        (shared / "folder.txt").write_text('"B:ひとつ目" &NOP\n', encoding="utf-8")
        (shared / "txt.txt").write_text('"W:別の項目" &NOP\n', encoding="utf-8")
        app_settings = {"filer": {"left_path": str(left), "right_path": str(right)}}
        filer_box.ROOT_OVERRIDE, filer_box.DB_OVERRIDE = str(box_root), str(db)
        filer_box.discard_cache()
        w = filer.FilerWindow(app_settings, None)
        w.menu_shared_dir = shared
        w.menu_local_dir = os.path.join(tmp, "localmenus")
        launched = []

        class FakeProc:
            def wait(self):
                return 0

        def fake_popen(argv, *a, **kw):
            launched.append(argv)
            return FakeProc()

        original_popen = filer.subprocess.Popen
        original_allow = filer.filer_launch._allow_foreground
        seen = {}

        def pick(title, items):
            seen["items"] = items
            return seen.get("choice")

        try:
            w.show()
            w.start()
            _wait(w)
            w.choose_menu = pick
            filer.subprocess.Popen = fake_popen
            filer.filer_launch._allow_foreground = lambda: None

            # Box の外: 出ない
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            w.cmd_ext_menu()
            assert "Box.comで開く" not in [i[1] for i in seen["items"] if i], seen["items"]

            # Box の中のフォルダ: 出る。既存の項目(B)と衝突しない文字になる
            w.left.load(str(folder))
            _wait(w)
            w.left.set_cursor(w.left._row_of("資料"))
            w.cmd_ext_menu()
            entry = [i for i in seen["items"] if i and i[1] == "Box.comで開く"]
            assert len(entry) == 1 and entry[0][2] == ("box_open",), seen["items"]
            keys = [i[0] for i in seen["items"] if i]
            assert keys.count(entry[0][0]) == 1 and entry[0][0] != "B", keys
            # ファイル(txt のメニュー)でも出て、W とは衝突しない
            w.left.set_cursor(w.left._row_of("memo.txt"))
            w.cmd_ext_menu()
            entry = [i for i in seen["items"] if i and i[1] == "Box.comで開く"]
            assert len(entry) == 1 and entry[0][0] != "W", seen["items"]

            # 選ぶと、Box の項目を実行する子プロセスを起こす
            seen["choice"] = ("box_open",)
            w.left.set_cursor(w.left._row_of("資料"))
            w.cmd_ext_menu()
            target = str(folder / "資料")
            assert launched == [[filer.pythonw_executable(), str(filer.CONTEXT_MENU_SCRIPT),
                                 "--x", "0", "--y", "0", "--owner", str(w._hwnd()),
                                 "--item", "Box.comで表示", "--item", "View on Box.com",
                                 "--", target]], launched
            assert f"Box.comで開く: {target}" in w.log_view.toPlainText()
            # 子の終了コード 3 = 右クリックに項目が無い
            w._on_box_open_done((target, 3))
            assert "Box: 右クリックに「Box.comで表示」が見つかりません" in w.log_view.toPlainText()
            w._on_box_open_done((target, 1))
            assert "終了コード 1" in w.log_view.toPlainText()
        finally:
            filer.subprocess.Popen = original_popen
            filer.filer_launch._allow_foreground = original_allow
            filer_box.ROOT_OVERRIDE = filer_box.DB_OVERRIDE = None
            w.close()
            w.deleteLater()


def test_window_copy_path_box_dialog():
    """C→P: 対象がすべて Box の中なら、プレビュー付きの窓で何をコピーするか聞く
    (フルパス/共有用テキスト/両方)。選択だけではコピーせず、確定で初めて入る。
    プレビューの URL はリンクで、ホバー情報とクリック(ブラウザで開く)を持つ。
    コピー後の確認窓は無い。Box の外では聞かずに従来どおり即フルパス。"""
    import sqlite3
    import filer
    import filer_box
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QLabel, QPushButton

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)       # Box の外(通常のフォルダ)
        box_root = Path(tmp, "home", "Box")
        folder = box_root / "ProjA" / "資料"
        folder.mkdir(parents=True)
        (folder / "memo.txt").write_bytes(b"x")
        db = Path(tmp, "local", "sync.db")
        db.parent.mkdir()
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE local_item (local_id INTEGER NOT NULL, native_item_type INTEGER,"
                     " parent_item_id INTEGER, name VARCHAR COLLATE NOCASE, PRIMARY KEY (local_id))")
        conn.execute("CREATE TABLE item_mapping_between_fs (id INTEGER, local_id INTEGER,"
                     " box_id VARCHAR, box_item_type INTEGER, PRIMARY KEY (id), UNIQUE (local_id))")
        rows = [(0, 1, None, "", "0", 1), (100, 1, 0, "ProjA", "9100", 1),
                (101, 1, 100, "資料", "9101", 1), (102, 0, 101, "memo.txt", "9103", 0)]
        for i, (lid, typ, parent, name, box_id, box_type) in enumerate(rows):
            conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (lid, typ, parent, name))
            conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                         (i, lid, box_id, box_type))
        _add_box_item_table(conn, rows)
        conn.commit()
        conn.close()
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        app_settings = {"filer": {"left_path": str(left), "right_path": str(right)}}
        filer_box.ROOT_OVERRIDE, filer_box.DB_OVERRIDE = str(box_root), str(db)
        filer_box.discard_cache()
        w = filer.FilerWindow(app_settings, str(settings_path))
        try:
            w.show()
            w.start()
            _wait(w)
            clip = QGuiApplication.clipboard()
            full = str(folder / "memo.txt")
            share = ("/Box/ProjA/資料/\r\n"
                     "　https://app.box.com/folder/9101\r\n"
                     "　　memo.txt")
            w.choose_menu = lambda title, items: "path"
            w.left.load(str(folder))
            _wait(w)
            w.left.set_cursor(w.left._row_of("memo.txt"))

            # フルパス(通常)
            w.ask_copy_kind = lambda count, full_text, data: "path"
            clip.setText("")
            w.cmd_copy_menu()
            assert clip.text() == full, repr(clip.text())

            # 共有用テキスト
            w.ask_copy_kind = lambda count, full_text, data: "box"
            w.cmd_copy_menu()
            assert clip.text() == share, repr(clip.text())

            # 両方: フルパス → 空行 → 共有用テキスト
            w.ask_copy_kind = lambda count, full_text, data: "both"
            w.cmd_copy_menu()
            assert clip.text() == full + "\r\n\r\n" + share, repr(clip.text())
            assert "フルパス 1件＋Box の共有用テキスト 1件" in w.log_view.toPlainText()

            # キャンセル: 何も入らない
            w.ask_copy_kind = lambda count, full_text, data: None
            clip.setText("keep")
            w.cmd_copy_menu()
            assert clip.text() == "keep"

            # 両方で共有用テキストが作れなかったら、フルパスだけ入る
            real_data = w._box_share_data
            w._box_share_data = lambda: None
            w.ask_copy_kind = lambda count, full_text, data: "both"
            clip.setText("")
            w.cmd_copy_menu()
            assert clip.text() == full, repr(clip.text())
            w._box_share_data = real_data

            # 本物の窓: 選択では確定せずプレビューが切り替わり、「コピー」で確定する。
            # リンクのホバー情報と、クリックでブラウザが開くこと(窓は閉じない)も見る
            seen_dialog = {}
            opened = []
            w.open_url = lambda url: opened.append(url)

            def drive(dialog):
                seen_dialog["names"] = {b.objectName()
                                        for b in dialog.findChildren(QPushButton)}
                preview = dialog.findChild(filer._LinkPreview, "copyKindPreview")
                seen_dialog["preview_path"] = preview.toPlainText().strip()
                dialog.findChild(QPushButton, "copyKindBoth").click()
                seen_dialog["still_open"] = dialog.result() != filer.QDialog.Accepted
                both_text = preview.toPlainText()
                seen_dialog["both_has_full"] = full in both_text
                seen_dialog["both_has_share"] = "https://app.box.com/folder/9101" in both_text
                url = "https://app.box.com/folder/9101"
                seen_dialog["tip"] = preview.tip_for(url)
                preview.anchorClicked.emit(QUrl(url))
                seen_dialog["opened_in_dialog"] = list(opened)
                dialog.findChild(QPushButton, "copyKindOk").click()
                return dialog.result()

            del w.ask_copy_kind
            w.exec_dialog = drive
            clip.setText("")
            w.cmd_copy_menu()
            assert seen_dialog["names"] == {"copyKindPath", "copyKindBox",
                                            "copyKindBoth", "copyKindOk",
                                            "copyKindCancel"}, seen_dialog
            assert seen_dialog["preview_path"] == full, seen_dialog
            assert seen_dialog["still_open"]
            assert seen_dialog["both_has_full"] and seen_dialog["both_has_share"]
            assert "資料（フォルダ）" in seen_dialog["tip"]
            assert "パス：/Box/ProjA/資料/" in seen_dialog["tip"]
            assert "フォルダ ID：9101" in seen_dialog["tip"]
            assert seen_dialog["opened_in_dialog"] == ["https://app.box.com/folder/9101"]
            assert clip.text() == full + "\r\n\r\n" + share, repr(clip.text())
            del w.exec_dialog

            # 共有用テキストが作れないとき: 「共有用テキスト」は確定できず、
            # 「両方」はフルパスだけのプレビューになる
            w._box_share_data = lambda: {"text": None, "count": 0, "blocks": [],
                                         "problems": ["memo.txt：同期の記録に見つかりません"]}
            seen2 = {}

            def drive2(dialog):
                preview = dialog.findChild(filer._LinkPreview, "copyKindPreview")
                dialog.findChild(QPushButton, "copyKindBox").click()
                seen2["ok_enabled"] = dialog.findChild(QPushButton, "copyKindOk").isEnabled()
                dialog.findChild(QPushButton, "copyKindBoth").click()
                seen2["preview_both"] = preview.toPlainText().strip()
                seen2["note"] = dialog.findChild(QLabel, "copyKindNote").text()
                dialog.findChild(QPushButton, "copyKindOk").click()
                return dialog.result()

            w.exec_dialog = drive2
            clip.setText("")
            w.cmd_copy_menu()
            assert seen2["ok_enabled"] is False
            assert seen2["preview_both"] == full, seen2
            assert "リンクを作れなかったもの" in seen2["note"]
            assert "memo.txt：同期の記録に見つかりません" in seen2["note"]
            assert clip.text() == full, repr(clip.text())
            del w.exec_dialog
            w._box_share_data = real_data

            # Box の外では聞かない
            def no_ask(count, full_text, data):
                raise AssertionError("Box の外で聞かれた")

            w.ask_copy_kind = no_ask
            w.left.load(str(left))
            _wait(w)
            w.left.set_cursor(w.left._row_of("alpha.txt"))
            clip.setText("")
            w.cmd_copy_menu()
            assert clip.text() == str(left / "alpha.txt"), repr(clip.text())
        finally:
            filer_box.ROOT_OVERRIDE = filer_box.DB_OVERRIDE = None
            w.close()
            w.deleteLater()

def test_workspace_store():
    """ワークスペースの保存先はデータ置き場(テストでは一時フォルダ。本物の
    %LOCALAPPDATA% には書かない)。一時ファイル→置き換えで書く。"""
    data = core.data_dir()
    assert data.startswith(_TEST_TEMP), data
    path = os.path.join(data, core.WORKSPACE_STORE)
    if os.path.exists(path):
        os.remove(path)
    assert core.load_workspaces() == []
    items = core.workspace_put([], "仕事", "C:\\a", "D:\\b")
    items = core.workspace_put(items, "家", "C:\\h", "C:\\i")
    items = core.workspace_put(items, "仕事", "C:\\a2", "D:\\b2")     # 同じ名前は差し替え
    assert [i["name"] for i in items] == ["仕事", "家"] and items[0]["left"] == "C:\\a2"
    core.save_workspaces(items)
    assert os.path.exists(path) and not os.path.exists(path + ".tmp")
    assert core.load_workspaces() == items
    assert core.workspace_find(items, "家") == 1 and core.workspace_find(items, "x") is None
    assert [i["name"] for i in core.workspace_remove(items, "仕事")] == ["家"]
    # 壊れた・形の違う記録は空として読む
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"workspaces": [{"name": 1}, {"name": "ok", "left": "a", "right": "b"}]}')
    assert core.load_workspaces() == [{"name": "ok", "left": "a", "right": "b"}]
    with open(path, "w", encoding="utf-8") as f:
        f.write("{broken")
    assert core.load_workspaces() == []
    os.remove(path)


def test_chooser_menu_delete_key():
    """Del を押すと、選んでいる項目を「削除の依頼」として残して閉じる(実行はしない)。
    deletable でないメニューでは Del は何もしない。"""
    import filer
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent

    _qapp()
    w = filer.FilerWindow({}, None)
    try:
        menu, chosen = w.build_menu("t", [("S", "保存", "save"), ("1", "仕事", "ws1")])
        menu.deletable = True
        second = [a for a in menu.actions() if chosen.get(a) == "ws1"][0]
        menu.setActiveAction(second)
        menu.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        assert menu.delete_action is second
        plain, _ = w.build_menu("t", [("S", "保存", "save")])
        plain.setActiveAction(plain.actions()[0])
        plain.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        assert plain.delete_action is None
        # choose_menu_deletable は Del の項目を ("delete", 値) で返す
        menu2, chosen2 = w.build_menu("t", [("1", "仕事", "ws1")])
        target = menu2.actions()[0]
        w.build_menu = lambda title, items: (menu2, chosen2)
        w._exec_menu = lambda m, c: (setattr(m, "delete_action", target), None)[1]
        assert w.choose_menu_deletable("t", []) == ("delete", "ws1")
        assert menu2.deletable
    finally:
        w.close()
        w.deleteLater()


def test_window_workspace():
    """Alt+J: 今の左右を保存 → 別の場所へ移ってから呼び出すと左右が一度に戻る。
    存在しないパスの側だけは移さず知らせる。Del で削除(確認あり)。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    store = os.path.join(core.data_dir(), core.WORKSPACE_STORE)
    if os.path.exists(store):
        os.remove(store)
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text('{"filer": {"editor": "x"}}', encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L, R = w.left, w.right
        answers = []
        menus = []

        def choose(title, items):
            menus.append([i for i in items if i is not None])
            return answers.pop(0)(items)

        def pick(prefix, kind="choose"):
            def run(items):
                for item in items:
                    if item is not None and item[1].startswith(prefix):
                        return kind, item[2]
                raise AssertionError(f"項目が無い: {prefix}")
            return run

        w.choose_menu_deletable = choose
        try:
            # 保存(最初の項目は「今の左右を保存…」)
            answers.append(pick("今の左右を保存"))
            w.ask_text = lambda *a, **k: "作業A"
            QTest.keyClick(L.view, Qt.Key_J, Qt.AltModifier)
            assert menus[-1][0][:2] == ("S", "今の左右を保存…")
            saved = core.load_workspaces()
            assert saved == [{"name": "作業A", "left": core.normalize_dir(str(left)),
                              "right": core.normalize_dir(str(right))}], saved
            assert "ワークスペース: 保存しました" in w.log_view.toPlainText()
            assert "作業A" not in settings_path.read_text(encoding="utf-8")   # settings には入れない
            # 別の場所へ移ってから呼び出す
            L.load(str(left / "sub1"))
            R.load(str(left / "sub2"))
            _wait(w)
            answers.append(pick("作業A"))
            w.run_command("workspace")
            _wait(w)
            assert any("作業A" in i[1] and "→" in i[1] for i in menus[-1])  # 名前と 左→右
            assert core.same_dir(L.path, left) and core.same_dir(R.path, right)
            # 右だけ消えたパスにすると、右は移らず知らせる
            gone = str(Path(tmp, "gone"))
            core.save_workspaces([{"name": "壊れ", "left": str(left / "sub2"), "right": gone}])
            answers.append(pick("壊れ"))
            w.run_command("workspace")
            _wait(w)
            assert core.same_dir(L.path, left / "sub2") and core.same_dir(R.path, right)
            assert f"右は移動しません（開けません: {gone}）" in w.log_view.toPlainText()
            # Del で削除(確認で「いいえ」なら残る、「はい」なら消える。消したら一覧を出し直す)
            core.save_workspaces([{"name": "消す", "left": str(left), "right": str(right)}])
            asked = []
            w.confirm = lambda title, text, default_yes=True: (asked.append(text), False)[1]
            answers.extend([pick("消す", "delete"), lambda items: ("choose", None)])
            w.run_command("workspace")
            assert asked and "消す" in asked[0] and core.load_workspaces()
            w.confirm = lambda *a, **k: True
            answers.extend([pick("消す", "delete"), lambda items: ("choose", None)])
            w.run_command("workspace")
            assert core.load_workspaces() == [] and not answers
            assert "ワークスペース: 削除しました（消す）" in w.log_view.toPlainText()
            # 保存の行の Del は何もしない(メニューはそのまま)
            answers.extend([pick("今の左右を保存", "delete"), lambda items: ("choose", None)])
            w.run_command("workspace")
            assert not answers
        finally:
            w.close()
            w.deleteLater()
            if os.path.exists(store):
                os.remove(store)


# ---------------------------------------------------------------
# 写真の★(写真フォルダモード・★の列・R の★の欄・★の絞り込み・変更記録)
# ---------------------------------------------------------------
def test_photo_mode_judgement():
    def entries(photos, others, dirs=0):
        return ([E(f"d{i}", is_dir=True) for i in range(dirs)] + [core.PARENT_ENTRY]
                + [E(f"p{i}.jpg") for i in range(photos)] + [E(f"o{i}.txt") for i in range(others)])

    # 10枚以上かつ5割以上
    assert core.is_photo_folder(entries(10, 10))
    assert not core.is_photo_folder(entries(10, 11))          # 5割を切る
    assert not core.is_photo_folder(entries(9, 9))            # 10枚に届かない(8割にも)
    # 3枚以上かつ8割以上
    assert core.is_photo_folder(entries(4, 1))                # 0.8 ちょうど
    assert not core.is_photo_folder(entries(3, 1))            # 0.75
    assert core.is_photo_folder(entries(3, 0))
    assert not core.is_photo_folder(entries(2, 0))            # 2枚だけは写真モードにしない
    # フォルダと ".." は数えない。ファイルが無ければ False
    assert core.is_photo_folder(entries(3, 0, dirs=50))
    assert not core.is_photo_folder(entries(0, 0, dirs=3))
    assert core.count_photos(entries(3, 2, dirs=4)) == (3, 5)
    # RAW・HEIC も写真。大文字の拡張子も
    assert core.count_photos([E("a.ARW"), E("b.HEIC"), E("c.Jpg"), E("d.gif")]) == (3, 4)
    # 数値は settings.json の filer.photo_mode で変えられる(壊れた値は項目ごとに既定)
    params = {"min_count": 2, "min_ratio": 0.4, "small_min_count": "x", "small_min_ratio": 3}
    norm = core.normalize_photo_mode(params)
    assert norm == {"min_count": 2, "min_ratio": 0.4, "small_min_count": 3, "small_min_ratio": 0.8}
    assert core.is_photo_folder(entries(2, 3), params)
    assert not core.is_photo_folder(entries(2, 3))
    assert core.normalize_photo_mode(None) == core.PHOTO_MODE_DEFAULTS
    assert core.normalize_photo_mode({"min_count": True}) == core.PHOTO_MODE_DEFAULTS
    # 手動の切り替え
    assert core.photo_mode_on("auto", True) and not core.photo_mode_on("auto", False)
    assert core.photo_mode_on("on", False) and not core.photo_mode_on("off", True)
    # キー: I / Shift+I はあふｗ標準の合計ファイルサイズ。写真メニュー(photo_menu)と
    # Alt+1〜9 の絞り込みのプリセット(photo_preset_1〜9)は無くした(★は R、絞り込みは
    # ★の範囲の段だけ)。Alt+I・Alt+1〜9 は空き
    assert "4073" not in core.DEFAULT_KEYMAP and "photo_menu" not in core.COMMANDS
    assert core.DEFAULT_KEYMAP["0073"] == "size_total"
    assert core.DEFAULT_KEYMAP["1073"] == "size_total_simple"
    assert core.DEFAULT_KEYMAP["0082"] == "rename" and core.DEFAULT_KEYMAP["1090"] == "settings_menu"
    # ユーザーの上書き設定に photo_menu が残っていても落ちず、黙って捨てる
    keymap = core.build_keymap({"4073": "photo_menu", "0082": "rename"})
    assert "4073" not in keymap and keymap["0082"] == "rename"
    alt_digits = [f"40{49 + i}" for i in range(9)]
    assert not any(code in core.DEFAULT_KEYMAP for code in alt_digits)
    assert not any(c.startswith("photo_preset") for c in core.COMMANDS)
    # 上書き設定に Alt+1〜9 の photo_preset_N が残っていても落ちず、黙って捨てる
    keymap = core.build_keymap({code: f"photo_preset_{i + 1}" for i, code in enumerate(alt_digits)}
                               | {"0082": "rename"})
    assert not any(code in keymap for code in alt_digits) and keymap["0082"] == "rename"


def _photo_packet(rating=None, keywords=()):
    attrs = f' xmp:Rating="{rating}"' if rating is not None else ""
    body = ""
    if keywords:
        body = ("<dc:subject><rdf:Bag>" + "".join(f"<rdf:li>{k}</rdf:li>" for k in keywords)
                + "</rdf:Bag></dc:subject>")
    return ("<x:xmpmeta xmlns:x='adobe:ns:meta/'>"
            "<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>"
            "<rdf:Description rdf:about='' xmlns:xmp='http://ns.adobe.com/xap/1.0/'"
            f" xmlns:dc='http://purl.org/dc/elements/1.1/'{attrs}>{body}</rdf:Description>"
            "</rdf:RDF></x:xmpmeta>").encode("utf-8")


def _photo_jpeg(path, rating=None, keywords=()):
    """XMP(★・キーワード)入りの小さな JPEG を作る。"""
    import struct
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(64, 48, QImage.Format_RGB32)
    image.fill(QColor(200, 120, 40))
    data_ba = QByteArray()
    buf = QBuffer(data_ba)
    buf.open(QIODevice.WriteOnly)
    image.save(buf, "JPG")
    buf.close()
    data = bytes(data_ba)
    if rating is not None or keywords:
        payload = b"http://ns.adobe.com/xap/1.0/\x00" + _photo_packet(rating, keywords)
        seg = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        data = data[:2] + seg + data[2:]      # SOI の直後に差し込む
    Path(path).write_bytes(data)
    return str(path)


class _FakePhotoSession:
    """ExifToolSession の代わり。ファイルには書かず、書いたことにして結果を返す。
    fail に入れた名前は「読み取り専用」で失敗させる。"""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)
        self.closed = False

    def _results(self, paths, op, change):
        from photometa import writer, xmp
        out = []
        for p in paths:
            before = xmp.read_meta(p)
            if os.path.basename(p) in self.fail:
                out.append(writer.WriteResult(p, False, error="読み取り専用です",
                                              before=before, op=op))
            else:
                out.append(writer.WriteResult(p, True, before=before,
                                              after=change(before or xmp.PhotoMeta()), op=op))
        return out

    def set_rating(self, paths, rating):
        from photometa import xmp
        paths = list(paths)
        self.calls.append(("rating", paths, rating))
        return self._results(paths, ("rating", rating),
                             lambda m: xmp.PhotoMeta(rating, m.keywords))

    def set_keyword(self, paths, keyword, on):
        from photometa import xmp
        paths = list(paths)
        self.calls.append(("keyword", paths, keyword, on))

        def change(m):
            kws = tuple(k for k in m.keywords if k != keyword)
            return xmp.PhotoMeta(m.rating, kws + ((keyword,) if on else ()))
        return self._results(paths, ("keyword", keyword, on), change)

    def close(self):
        self.closed = True


def _wait_until(cond, timeout=5.0, what="条件"):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        QTest.qWait(20)
    raise AssertionError(f"{what} にならない")


def _make_photo_tree(tmp):
    photos = Path(tmp, "photos")
    sub = photos / "sub"
    sub.mkdir(parents=True)
    _photo_jpeg(photos / "a5.jpg", 5)
    _photo_jpeg(photos / "b4.jpg", 4)
    _photo_jpeg(photos / "c3.jpg", 3, ["みてね済"])
    _photo_jpeg(photos / "d_reject.jpg", -1)
    _photo_jpeg(photos / "e_none.jpg")
    (photos / "memo.txt").write_text("x", encoding="utf-8")
    _photo_jpeg(sub / "one.jpg", 5)
    for i in range(5):
        (sub / f"t{i}.txt").write_text("x", encoding="utf-8")
    other = Path(tmp, "other")
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")
    return photos, sub, other


def _rating_texts(pane):
    import filer
    return {e.name: pane.model.data(pane.model.index(row, filer.COL_RATING))
            for row, e in enumerate(pane.entries)}


def _photo_window(photos, other, settings_path):
    import filer
    w = filer.FilerWindow({"filer": {"left_path": str(photos), "right_path": str(other)}},
                          str(settings_path))
    w.resize(1100, 600)
    w.show()
    w.start()
    _wait(w)
    _wait_until(lambda: len(w.left.photo_meta) >= 5 and not w.left._photo_requested,
                what="★の読み取りが終わる")
    return w


def _press_settings_menu(w, pane):
    """Shift+Z(各種設定一時切替)を押す。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.keyClick(pane.view, Qt.Key_Z, Qt.ShiftModifier)


def test_photo_mode_window_and_rating_column():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L, R = w.left, w.right
        try:
            # 5枚 / 6ファイル(8割以上・3枚以上)で写真フォルダモード。右(テキストだけ)は通常
            assert L.photo_mode and not R.photo_mode
            assert L.header.text().startswith("📷 ") and not R.header.text().startswith("📷")
            hh = L.view.horizontalHeader()
            assert not hh.isSectionHidden(filer.COL_RATING)
            assert R.view.horizontalHeader().isSectionHidden(filer.COL_RATING)
            # ★の列は拡張子の右(列の並びも、見た目の位置も)
            assert hh.sectionPosition(filer.COL_EXT) < hh.sectionPosition(filer.COL_RATING) \
                < hh.sectionPosition(filer.COL_SIZE)
            texts = _rating_texts(L)
            assert texts["a5.jpg"] == "みてね用" and texts["b4.jpg"] == "フォトフレーム用"
            assert texts["c3.jpg"] == "ベストショット 済" and texts["d_reject.jpg"] == "❌削除候補"
            assert texts["e_none.jpg"] == "" and texts["memo.txt"] == "" and texts["sub"] == ""
            # 描くのは段階ごとの角丸バッジ(文字・色の種類・みてね済の ✓)。ツールチップは正式名
            badges = {e.name: L.model.data(L.model.index(row, filer.COL_RATING), filer.BADGE_ROLE)
                      for row, e in enumerate(L.entries)}
            assert badges["a5.jpg"] == ("💌 みてね", "rating_5", False)
            assert badges["b4.jpg"] == ("🖼️ フレーム", "rating_4", False)
            assert badges["c3.jpg"] == ("⭐ ベスト", "rating_3", True)
            assert badges["d_reject.jpg"] == ("❌ 削除", "rating_reject", False)
            assert badges["e_none.jpg"] is None and badges["memo.txt"] is None
            assert badges["sub"] is None and badges[".."] is None
            tip = lambda n: L.model.data(  # noqa: E731
                L.model.index(L._row_of(n), filer.COL_RATING), Qt.ToolTipRole)
            assert tip("c3.jpg") == "ベストショット 済" and tip("e_none.jpg") is None
            # 列の幅はバッジの字(RATING_BADGE_PX)で測る。正式名の幅より狭い
            from PySide6.QtGui import QFontMetrics
            assert L._col_widths[filer.COL_RATING] < QFontMetrics(L.pane_font).horizontalAdvance(
                "フォトフレーム用 済")
            assert filer.rating_badge_font(L.pane_font).pixelSize() == filer.RATING_BADGE_PX
            # バッジは delegate が描く(落ちずに描けること)
            L.view.viewport().repaint()
            # 段階ごとの色
            entry = lambda n: L.entries[L._row_of(n)]  # noqa: E731
            assert L.text_color(entry("a5.jpg"), filer.COL_RATING) == filer.DEFAULT_COLORS["rating_5"]
            assert L.text_color(entry("b4.jpg"), filer.COL_RATING) == filer.DEFAULT_COLORS["rating_4"]
            assert L.text_color(entry("d_reject.jpg"), filer.COL_RATING) \
                == filer.DEFAULT_COLORS["rating_reject"]
            # 未選別でみてね済だけなら ✓ だけ。★1・★2 は灰色のバッジ
            from photometa import xmp as _xmp
            key = filer.photo_key(L.entry_path(entry("e_none.jpg")))
            saved = L.photo_meta[key]
            try:
                L.photo_meta[key] = _xmp.PhotoMeta(0, ("みてね済",))
                assert L.rating_badge(entry("e_none.jpg")) == ("", "rating_other", True)
                L.photo_meta[key] = _xmp.PhotoMeta(2, ())
                assert L.rating_badge(entry("e_none.jpg")) == ("★2", "rating_other", False)
                L.view.viewport().repaint()
            finally:
                L.photo_meta[key] = saved
            # 写真のボタン段は無い(★は R の欄で付け、★の範囲の段で絞る)
            assert not hasattr(L, "photo_bar") and not hasattr(L, "photo_buttons")
            assert L.rating_bar.isVisible() and not R.rating_bar.isVisible()
            # 窓が狭いときは ★ の列を拡張子より先に隠す(日時 → サイズ → ★ → 拡張子)
            name_min = L._name_min
            L._name_min = L.view.viewport().width() - L._col_widths[filer.COL_EXT] - 2
            L.fit_columns()
            assert hh.isSectionHidden(filer.COL_RATING) and not hh.isSectionHidden(filer.COL_EXT)
            L._name_min = name_min
            L.fit_columns()
            assert not hh.isSectionHidden(filer.COL_RATING)

            if os.environ.get("FILER_PHOTO_SHOT"):
                QTest.qWait(50)
                w.grab().save(os.environ["FILER_PHOTO_SHOT"])

            # Shift+Z: 写真フォルダモードを手動で切り替える(今の窓だけ)
            seen = []
            w._exec_menu = lambda m, c: (seen.append(
                {v: a.isChecked() for a, v in c.items()}), "photo_off")[1]
            QTest.keyClick(L.view, Qt.Key_Z, Qt.ShiftModifier)
            assert seen[-1]["photo_auto"] and not seen[-1]["photo_off"]
            assert not L.photo_mode and not L.header.text().startswith("📷")
            assert hh.isSectionHidden(filer.COL_RATING) and not L.rating_bar.isVisible()
            assert _rating_texts(L)["a5.jpg"] == ""
            assert R.photo_override == "auto"           # 反対窓は変わらない
            w._exec_menu = lambda m, c: "photo_on"
            w.activate("right")
            QTest.keyClick(R.view, Qt.Key_Z, Qt.ShiftModifier)
            assert R.photo_mode and R.header.text().startswith("📷 ")
            w.activate("left")
            # 別のフォルダへ移ったら自動に戻る(sub は写真1枚・テキスト5つ → 通常)
            L.load(str(sub))
            _wait(w)
            assert L.photo_override == "auto" and not L.photo_mode
            w._exec_menu = lambda m, c: "photo_on"
            QTest.keyClick(L.view, Qt.Key_Z, Qt.ShiftModifier)
            assert L.photo_mode
            _wait_until(lambda: _rating_texts(L).get("one.jpg") == "みてね用", what="sub の★")
            QTest.keyClick(L.view, Qt.Key_Backspace)
            _wait(w)
            assert core.same_dir(L.path, photos)
            assert L.photo_override == "auto" and L.photo_mode
            _wait_until(lambda: _rating_texts(L).get("a5.jpg") == "みてね用", what="戻ったあとの★")
        finally:
            w.close()
            w.deleteLater()


def _rate_with_R(w, rating=None, keyword_presses=0):
    """R のダイアログの「写真の★」の欄で★を選び(と、みてね済のボタンを keyword_presses 回
    押して)OK する。exec_dialog を差し替えるので、ダイアログは出さない。"""
    def step(d):
        assert d.photo_box is not None
        if rating is not None:
            d.pick_rating(rating)
        for _ in range(keyword_presses):
            d.cycle_keyword()
    opened = _rename_dialog_runner(w, [step])
    w.run_command("rename")
    del w.exec_dialog
    assert opened, w.log_view.toPlainText()
    return opened[-1]


def test_photo_write_targets():
    """R の「写真の★」の欄で★・みてね済を付ける。対象はマーク、無ければカーソル位置。画像で
    ないものは飛ばす。書けなかったものは表示を戻す。画像ビュア(F3)の隅の★も変わる。
    I は占有合計のまま。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L = w.left
        fake = _FakePhotoSession(fail={"d_reject.jpg"})
        w._photo_session = fake
        path = lambda n: str(photos / n)  # noqa: E731
        log = lambda: w.log_view.toPlainText()  # noqa: E731
        try:
            # I は写真のメニューではなく占有合計(写真フォルダモードでも意味は変えない)
            seen = []
            w._exec_menu = lambda menu, chosen: (seen.append(menu.title()), None)[1]
            L.set_cursor(L._row_of("a5.jpg"))
            QTest.keyClick(L.view, Qt.Key_I)
            _wait_until(lambda: "占有合計（a5.jpg）: 1 ファイル" in log(), what="I の占有合計")
            assert seen == [] and w._size_job is None

            # カーソル位置の1枚
            _rate_with_R(w, 4)
            assert fake.calls[-1] == ("rating", [path("a5.jpg")], 4)
            _wait_until(lambda: "★4 を 1 枚に付けました" in log(), what="書き込みのログ")
            assert _rating_texts(L)["a5.jpg"] == "フォトフレーム用"

            # マーク中のファイル(写真でないものは飛ばす)。実行後はマークを解く
            L.marks.update({"b4.jpg", "c3.jpg", "memo.txt"})
            _rate_with_R(w, 5)
            assert fake.calls[-1] == ("rating", [path("b4.jpg"), path("c3.jpg")], 5)
            assert not L.marks
            _wait_until(lambda: "★5 を 2 枚に付けました" in log(), what="2枚のログ")
            assert _rating_texts(L)["b4.jpg"] == "みてね用" and _rating_texts(L)["c3.jpg"] == "みてね用 済"

            # 失敗したものは表示を戻して理由を出す
            L.marks.update({"d_reject.jpg", "e_none.jpg"})
            _rate_with_R(w, 3)
            _wait_until(lambda: "★3 を 1 枚に付けました（失敗 1：読み取り専用です）" in log(),
                        what="失敗のログ")
            assert _rating_texts(L)["d_reject.jpg"] == "❌削除候補" and _rating_texts(L)["e_none.jpg"] == "ベストショット"

            # みてね済: 1回押すと「付ける」、2回で「外す」
            L.marks.update({"b4.jpg", "c3.jpg"})
            _rate_with_R(w, keyword_presses=1)
            assert fake.calls[-1] == ("keyword", [path("b4.jpg"), path("c3.jpg")], "みてね済", True)
            _wait_until(lambda: _rating_texts(L)["b4.jpg"] == "みてね用 済", what="キーワードが付く")
            L.marks.update({"b4.jpg", "c3.jpg"})
            _rate_with_R(w, keyword_presses=2)
            assert fake.calls[-1] == ("keyword", [path("b4.jpg"), path("c3.jpg")], "みてね済", False)
            _wait_until(lambda: _rating_texts(L)["c3.jpg"] == "みてね用", what="キーワードが外れる")

            # 画像ビュア(F3)の間: マークが無ければ表示中の1枚。隅に★
            L.set_cursor(L._row_of("a5.jpg"))
            w.run_command("preview_toggle")
            _wait_until(lambda: w.preview_widget is not None
                        and w.preview_widget.rating.text() == "🖼️ フォトフレーム用", what="ビュアの★")
            # 一覧のバッジと同じ色の角丸
            sheet = w.preview_widget.rating.styleSheet()
            assert filer.DEFAULT_COLORS["rating_4_bg"] in sheet and "border-radius" in sheet
            _rate_with_R(w, 5)
            assert fake.calls[-1] == ("rating", [path("a5.jpg")], 5)
            _wait_until(lambda: w.preview_widget.rating.text() == "💌 みてね用", what="ビュアの★の更新")
            # ビュアで見ながらマークしたら、マークした全部(ビュア中でもマーク優先)
            L.marks.update({"a5.jpg", "b4.jpg"})
            _rate_with_R(w, 3)
            assert fake.calls[-1] == ("rating", [path("a5.jpg"), path("b4.jpg")], 3)
            _wait_until(lambda: w.preview_widget.rating.text() == "⭐ ベストショット", what="ビュアの★の更新")
            w.preview_off()

            # ExifTool の書き込み途中の一時ファイル(名前_exiftool_tmp)は一覧に出さない
            Path(photos, "a5.jpg_exiftool_tmp").write_bytes(b"x")
            w.run_command("reload_keep")
            _wait_until(lambda: L._row_of("b4.jpg") is not None, what="読み直し")
            assert all(not e.name.endswith("_exiftool_tmp") for e in L.entries)
            Path(photos, "a5.jpg_exiftool_tmp").unlink()

            # ExifTool の場所は分かるのに起こせない: 書くときにログで知らせる
            w._photo_session = None
            original = (filer.photo_writer.open_session, filer.photo_config.find_exiftool)
            filer.photo_writer.open_session = lambda config=None, **k: None
            filer.photo_config.find_exiftool = lambda config=None: "exiftool.exe"
            try:
                L.set_cursor(L._row_of("a5.jpg"))
                _rate_with_R(w, 5)
            finally:
                filer.photo_writer.open_session, filer.photo_config.find_exiftool = original
            assert filer.EXIFTOOL_NOT_FOUND in log()
            assert "%LOCALAPPDATA%\\traytools\\photo_ratings.json" in filer.EXIFTOOL_NOT_FOUND
            w._photo_session = fake
        finally:
            w.close()
            w.deleteLater()
        assert fake.closed                       # あっふを閉じたら ExifTool も閉じる


def _rename_dialog_runner(w, steps):
    """exec_dialog の差し替え。R のダイアログを受け取り、steps の先頭の関数で操作して
    try_accept(OK)する。関数が False を返したらキャンセル。開いたダイアログを積んで返す。"""
    from PySide6.QtWidgets import QDialog
    opened = []

    def run(dialog):
        opened.append(dialog)
        step = steps.pop(0)
        if step(dialog) is False:
            return dialog.reject() or dialog.result()
        if dialog.result() != QDialog.Accepted:   # キー(Enter)で閉じていなければ OK を押す
            dialog.try_accept()
        return dialog.result()
    w.exec_dialog = run
    return opened


def _show_active(dialog):
    """ダイアログを(offscreen で)出してアクティブにする。QShortcut(Alt+数字)は
    アクティブな窓でしか効かないので、キーを送るテストの前に呼ぶ。"""
    from PySide6.QtTest import QTest
    dialog.show()
    dialog.activateWindow()
    QTest.qWaitForWindowActive(dialog)
    QTest.qWait(30)


def test_rename_photo_section():
    """R: 画像を含むときだけ、ファイル情報の編集ダイアログの下部に「写真の★」の欄。★は★の列の
    バッジと同じ色のボタン(❌ 削除 / 未選別 / ★1 / ★2 / ⭐ ベスト / 🖼️ フレーム / 💌 みてね)を
    クリックか Alt+-・Alt+0〜5 で1つ選ぶ(同じものをもう一度で「変えない」)。みてね済はボタン
    1つで 変えない → 付ける → 外す(Alt+K)。ボタンはフォーカスを取らない。OK でファイル情報の
    変更を当てた後、★・キーワードを画像だけに書く(名前を変えたら新しい名前に)。"""
    import filer
    import filer_fileinfo
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QGroupBox, QLabel

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L = w.left
        fake = _FakePhotoSession()
        w._photo_session = fake
        c = filer.DEFAULT_COLORS
        path = lambda n: str(photos / n)  # noqa: E731
        log = lambda: w.log_view.toPlainText()  # noqa: E731
        try:
            # 画像が無ければ欄は無い(従来と同じ: 変換・タイムスタンプ・属性だけ)
            steps = [lambda d: False]
            opened = _rename_dialog_runner(w, steps)
            L.set_cursor(L._row_of("memo.txt"))
            QTest.keyClick(L.view, Qt.Key_R)
            d = opened[-1]
            assert d.photo_box is None and not d.rating_buttons and d.keyword_button is None
            assert not d.findChildren(QComboBox)
            assert [g.title() for g in d.findChildren(QGroupBox)] == ["変換", "タイムスタンプ", "属性"]
            assert d.photo_changes() == {}
            # フォルダの上も同じ
            steps.append(lambda d: False)
            L.set_cursor(L._row_of("sub"))
            QTest.keyClick(L.view, Qt.Key_R)
            assert opened[-1].photo_box is None

            # 1件(★3・みてね済): 今の★とキーワードを見せる。「変えない」が既定
            steps.append(lambda d: False)
            L.set_cursor(L._row_of("c3.jpg"))
            QTest.keyClick(L.view, Qt.Key_R)
            d = opened[-1]
            assert d.photo_box is not None
            assert d.photo_box.title() == "写真の★（画像 1 件）", d.photo_box.title()
            assert d.rating_now_label.text() == "今: ベストショット"
            assert d.keyword_now_label.text() == "今: 付いている"
            buttons = d.rating_buttons
            assert list(buttons) == [-1, 0, 1, 2, 3, 4, 5]
            labels = [b.text() for b in buttons.values()]
            assert labels == ["❌ 削除", "未選別", "★1", "★2", "⭐ ベスト", "🖼️ フレーム", "💌 みてね"], labels
            assert buttons[4].toolTip() == "フォトフレーム用"
            hints = [lab.text() for lab in d.photo_box.findChildren(QLabel)]
            for hint in ("Alt+-", "Alt+0", "Alt+1", "Alt+2", "Alt+3", "Alt+4", "Alt+5", "Alt+K"):
                assert hint in hints, (hint, hints)
            # ★の列のバッジと同じ色。選んだ枠はカーソルの色
            for value, key in ((-1, "rating_reject"), (0, "rating_other"), (2, "rating_other"),
                               (3, "rating_3"), (5, "rating_5")):
                sheet = buttons[value].styleSheet()
                assert c[key] in sheet and c[f"{key}_bg"] in sheet and c["cursor"] in sheet, sheet
            assert d.rating_choice is None and not any(b.isChecked() for b in buttons.values())
            assert d.rating_choice_label.text() == "★: 変えない"
            assert d.keyword_button.text() == "みてね済: 変えない"
            assert not d.keyword_button.isChecked()
            # ボタンはフォーカスを取らない(Tab の移動を増やさない)
            assert all(b.focusPolicy() == Qt.NoFocus for b in [*buttons.values(), d.keyword_button])
            assert d.photo_changes() == {}
            assert not fake.calls                         # 開いて閉じただけでは書かない

            # クリック・Alt+数字・Alt+K(ダイアログを出してキーを送る)。キャンセルで閉じる
            def keys(d):
                _show_active(d)
                b = d.rating_buttons
                edit = d.name_edit
                assert QApplication.focusWidget() is edit
                QTest.mouseClick(b[3], Qt.LeftButton)
                assert d.rating_choice == 3 and b[3].isChecked() and not b[4].isChecked()
                assert d.rating_choice_label.text() == "★: ベストショットにする"
                # 選んだボタンにはカーソルの色のはっきりした枠(左端の真ん中)。ほかは地の色のまま
                QTest.qWait(10)

                def edge(button):
                    image = button.grab().toImage()
                    dpr = image.devicePixelRatio()
                    return QColor(image.pixel(int(1 * dpr), int(button.height() / 2 * dpr)))
                assert edge(b[3]) == QColor(c["cursor"]), edge(b[3]).name()
                assert edge(b[4]) == QColor(c["rating_4_bg"]), edge(b[4]).name()
                QTest.mouseClick(b[5], Qt.LeftButton)       # 選べるのは1つだけ
                assert d.rating_choice == 5 and b[5].isChecked() and not b[3].isChecked()
                QTest.mouseClick(b[5], Qt.LeftButton)       # 同じボタンでもう一度 →「変えない」
                assert d.rating_choice is None and not any(x.isChecked() for x in b.values())
                assert d.rating_choice_label.text() == "★: 変えない"
                assert QApplication.focusWidget() is edit   # クリックでもフォーカスは動かない
                # Alt+数字: 名前の欄にフォーカスがあっても効き、欄に字は入らない
                name = edit.text()
                QTest.keyClick(edit, Qt.Key_4, Qt.AltModifier)
                assert d.rating_choice == 4 and b[4].isChecked() and edit.text() == name
                QTest.keyClick(edit, Qt.Key_Minus, Qt.AltModifier)
                assert d.rating_choice == -1 and d.rating_choice_label.text() == "★: 削除候補にする"
                QTest.keyClick(edit, Qt.Key_0, Qt.AltModifier)
                assert d.rating_choice == 0 and d.rating_choice_label.text() == "★: 未選別にする"
                QTest.keyClick(edit, Qt.Key_0, Qt.AltModifier)   # 同じキーでもう一度 →「変えない」
                assert d.rating_choice is None and edit.text() == name
                # 日時の欄・属性のチェックにフォーカスがあっても効く(値は変わらない)
                box = d.stamp_edits[0]
                box.setFocus()
                year = box.value()
                QTest.keyClick(box, Qt.Key_2, Qt.AltModifier)
                assert d.rating_choice == 2 and box.value() == year
                check = d.attr_checks["readonly"]
                check.setFocus()
                state = check.checkState()
                QTest.keyClick(check, Qt.Key_1, Qt.AltModifier)
                assert d.rating_choice == 1 and check.checkState() == state
                # 名前の欄に数字を打っても★は変わらない(数字はそのまま欄に入る)
                edit.setFocus()
                QTest.keyClick(edit, Qt.Key_5)
                QTest.keyClick(edit, Qt.Key_Minus)
                QTest.keyClick(edit, Qt.Key_0)
                assert d.rating_choice == 1 and "5-0" in edit.text(), edit.text()
                # Alt+頭文字(属性)は今までどおり。★の選択は変えない
                QTest.keyClick(d, Qt.Key_H, Qt.AltModifier)
                assert d.attr_checks["hidden"].isChecked() and d.rating_choice == 1
                # Alt+K: みてね済 変えない → 付ける ✓ → 外す → 変えない(クリックでも同じ)
                kb = d.keyword_button
                QTest.keyClick(edit, Qt.Key_K, Qt.AltModifier)
                assert d.keyword_choice is True and kb.text() == "みてね済: 付ける ✓" and kb.isChecked()
                QTest.keyClick(edit, Qt.Key_K, Qt.AltModifier)
                assert d.keyword_choice is False and kb.text() == "みてね済: 外す" and kb.isChecked()
                QTest.keyClick(edit, Qt.Key_K, Qt.AltModifier)
                assert d.keyword_choice is None and kb.text() == "みてね済: 変えない"
                assert not kb.isChecked() and "k" not in edit.text().lower()
                QTest.mouseClick(kb, Qt.LeftButton)
                QTest.mouseClick(kb, Qt.LeftButton)
                assert d.keyword_choice is False
                assert d.photo_changes() == {"rating": 1, "keywords": {"みてね済": False}}
                assert QApplication.focusWidget() is edit
                return False
            steps.append(keys)
            n = len(fake.calls)
            QTest.keyClick(L.view, Qt.Key_R)
            assert opened[-1].result() == QDialog.Rejected and len(fake.calls) == n
            assert Path(photos, "c3.jpg").exists()

            # 複数(★5 と ★4)と画像でないもの: 「今: いろいろ」。★は画像だけに付く(memo.txt は飛ばす)
            def pick3(d):
                assert d.photo_box.title() == "写真の★（画像 2 件）", d.photo_box.title()
                assert d.rating_now_label.text() == "今: いろいろ"
                assert d.keyword_now_label.text() == "今: 付いていない"
                QTest.mouseClick(d.rating_buttons[3], Qt.LeftButton)
            steps.append(pick3)
            L.marks.update({"a5.jpg", "b4.jpg", "memo.txt"})
            QTest.keyClick(L.view, Qt.Key_R)
            assert opened[-1].photo_changes() == {"rating": 3}
            assert fake.calls == [("rating", [path("a5.jpg"), path("b4.jpg")], 3)], fake.calls
            _wait_until(lambda: "★3 を 2 枚に付けました" in log(), what="R の★のログ")
            assert _rating_texts(L)["a5.jpg"] == "ベストショット" and _rating_texts(L)["memo.txt"] == ""
            assert not L.marks

            # 複数で★が同じなら、その名前を見せる。みてね済は Alt+K で「付ける」
            def keyword_on(d):
                assert d.rating_now_label.text() == "今: ベストショット", d.rating_now_label.text()
                _show_active(d)
                QTest.keyClick(d, Qt.Key_K, Qt.AltModifier)
                assert d.keyword_choice is True and d.photo_changes() == {"keywords": {"みてね済": True}}
            steps.append(keyword_on)
            L.marks.update({"a5.jpg", "b4.jpg"})
            QTest.keyClick(L.view, Qt.Key_R)
            assert fake.calls[-1] == ("keyword", [path("a5.jpg"), path("b4.jpg")], "みてね済", True)
            _wait_until(lambda: _rating_texts(L)["b4.jpg"] == "ベストショット 済", what="キーワードが付く")

            # みてね済を「外す」(★は変えない)
            def keyword_off(d):
                d.cycle_keyword()
                d.cycle_keyword()
            steps.append(keyword_off)
            n = len(fake.calls)
            L.set_cursor(L._row_of("c3.jpg"))
            QTest.keyClick(L.view, Qt.Key_R)
            assert fake.calls[n:] == [("keyword", [path("c3.jpg")], "みてね済", False)], fake.calls[n:]

            # 「変えない」なら★を書かない(名前だけ変えても書かない)
            def rename_only(d):
                d.name_edit.setText("d_renamed.jpg")
            steps.append(rename_only)
            n = len(fake.calls)
            L.set_cursor(L._row_of("d_reject.jpg"))
            QTest.keyClick(L.view, Qt.Key_R)
            assert Path(photos, "d_renamed.jpg").exists() and len(fake.calls) == n
            _wait_until(lambda: L._row_of("d_renamed.jpg") is not None, what="改名の読み直し")

            # 名前と★を同時に変えたら、新しい名前のファイルに書く
            def rename_and_rate(d):
                d.name_edit.setText("e_renamed.jpg")
                d.pick_rating(5)
            steps.append(rename_and_rate)
            L.set_cursor(L._row_of("e_none.jpg"))
            QTest.keyClick(L.view, Qt.Key_R)
            assert not Path(photos, "e_none.jpg").exists() and Path(photos, "e_renamed.jpg").exists()
            assert fake.calls[-1] == ("rating", [path("e_renamed.jpg")], 5), fake.calls[-1]
            _wait_until(lambda: _rating_texts(L).get("e_renamed.jpg") == "みてね用",
                        what="新しい名前の★")

            # R → Alt+3 → Enter の3打で★3(本物の exec。offscreen なので画面には出ない)
            del w.exec_dialog
            pressed = []

            def press(tries=0):
                try:
                    d = QApplication.activeModalWidget()
                    if d is None:
                        if tries < 100:
                            QTimer.singleShot(20, lambda: press(tries + 1))
                        return
                    d.activateWindow()
                    QTest.qWaitForWindowActive(d)
                    QTest.qWait(30)
                    QTest.keyClick(QApplication.focusWidget(), Qt.Key_3, Qt.AltModifier)
                    pressed.append(d.rating_choice)
                    QTest.keyClick(QApplication.focusWidget(), Qt.Key_Return)
                except Exception as e:  # noqa: BLE001  タイマーの中で投げると落ちる
                    pressed.append(e)

            def give_up():
                # キーが効かなくてもテストが止まらないように(開いたままなら閉じる)
                d = QApplication.activeModalWidget()
                if isinstance(d, filer_fileinfo.FileInfoDialog):
                    d.reject()
            L.set_cursor(L._row_of("a5.jpg"))
            n = len(fake.calls)
            QTimer.singleShot(0, press)
            QTimer.singleShot(5000, give_up)
            QTest.keyClick(L.view, Qt.Key_R)
            assert pressed == [3], pressed
            assert fake.calls[n:] == [("rating", [path("a5.jpg")], 3)], fake.calls[n:]
            assert Path(photos, "a5.jpg").exists()          # 名前の欄に「3」は入らない
            _wait_until(lambda: "★3 を 1 枚に付けました" in log(), what="3打の★")

            # ExifTool が見つからなければ欄を出さない
            opened = _rename_dialog_runner(w, steps)
            w._photo_session = None
            original = filer.photo_config.find_exiftool
            filer.photo_config.find_exiftool = lambda config=None: None
            try:
                steps.append(lambda d: False)
                QTest.keyClick(L.view, Qt.Key_R)
                assert opened[-1].photo_box is None
            finally:
                filer.photo_config.find_exiftool = original
                w._photo_session = fake

            # ダイアログ単体: photo を渡さなければ欄は無く、Alt+数字も何もしない
            d = filer_fileinfo.FileInfoDialog(None, str(photos), [L.entries[L._row_of("b4.jpg")]])
            assert d.photo_box is None and d.photo_changes() == {}
            _show_active(d)
            name = d.name_edit.text()
            QTest.keyClick(d.name_edit, Qt.Key_3, Qt.AltModifier)
            assert d.photo_changes() == {} and d.rating_choice is None
            d.close()
            d.deleteLater()
        finally:
            w.close()
            w.deleteLater()


def test_photo_rating_slider_only():
    """★で絞り込むのは★の範囲スライダーの段だけ(状態は窓の rating_range 1つ)。範囲は窓ごとに
    保ち、フォルダを移っても解けない(写真フォルダモードでないフォルダでは効かず、写真フォルダへ
    戻ればまた効く)。解くのは ✕ と :。★が未読み取りの写真は「不明」で隠れ、読めたら出る。"""
    import filer
    import threading as _threading
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L, R = w.left, w.right
        bar, slider = L.rating_bar, L.rating_bar.slider
        log = lambda: w.log_view.toPlainText()  # noqa: E731
        try:
            # スライダーだけで絞り込める。フォルダと .. は残る
            _drag_rating(slider, 1, 3)
            assert L.rating_range == (3, 5) and L.rating_expr() == "★>=3"
            assert _names(L) == ["..", "sub", "a5.jpg", "b4.jpg", "c3.jpg"]
            assert "★" not in L.header2.text()                # 見出しには出さない(段に出す)
            # : (全部見せる)で解く
            QTest.keyClick(L.view, Qt.Key_Colon)
            assert L.rating_range == (-1, 5) and L.rating_node is None
            assert "memo.txt" in _names(L) and slider.values() == (-1, 5)
            assert bar.range_label.text() == "すべて" and not bar.clear_button.isEnabled()

            # フォルダを移っても保つ。写真フォルダでない sub では段が隠れ、★では絞らない
            _drag_rating(slider, 1, 3)
            L.load(str(sub))
            _wait(w)
            assert L.rating_range == (3, 5) and not L.photo_mode and not bar.isVisible()
            assert {"one.jpg", "t0.txt"} <= set(_names(L))
            # そのフォルダで写真フォルダモードをオンにすれば効く(one.jpg は★5)
            L.set_photo_override("on")
            assert bar.isVisible() and slider.values() == (3, 5)
            _wait_until(lambda: _names(L) == ["..", "one.jpg"], what="sub で★の範囲が効く")
            L.set_photo_override("off")
            assert "t0.txt" in _names(L) and not bar.isVisible()
            # 写真フォルダへ戻れば、同じ範囲がまた効く
            QTest.keyClick(L.view, Qt.Key_Backspace)
            _wait(w)
            assert core.same_dir(L.path, photos) and L.photo_mode
            _wait_until(lambda: _names(L) == ["..", "sub", "a5.jpg", "b4.jpg", "c3.jpg"],
                        what="戻ったあとの★の範囲")
            assert bar.isVisible() and slider.values() == (3, 5)
            assert bar.range_label.text() == "ベストショット以上" and bar.count_label.text() == "表示 3 / 5枚"
            assert R.rating_range == (-1, 5)                  # 反対の窓は別
            # 写真フォルダモードでない窓には効かない(書庫も同じ: 写真フォルダモードにならない)
            R.set_rating_range(3, 5)
            assert "x.txt" in _names(R) and not R.rating_bar.isVisible()
            R.clear_rating()
            # ✕ で解く(ログで分かるように)
            QTest.mouseClick(bar.clear_button, Qt.LeftButton)
            assert L.rating_range == (-1, 5) and L.rating_node is None
            assert "memo.txt" in _names(L) and "★の絞り込みを解きました" in log()

            # ★が未読み取りの写真は「不明」で隠れ、読めたら掛け直して出る。
            # 永続キャッシュが効いていると読み直さないので、この間だけ空のキャッシュに替える
            gate = _threading.Event()
            original = filer._read_photo_meta
            filer._read_photo_meta = lambda p: (gate.wait(5), original(p))[1]
            saved_cache = filer._photo_metacache
            filer._photo_metacache = filer.photo_metacache.MetaCache(
                str(Path(tmp, "slider_gate_cache.sqlite")))
            try:
                L.reset_photo()
                L.set_rating_range(4, 5)
                QTest.qWait(50)
                assert _names(L) == ["..", "sub"]
                gate.set()
                _wait_until(lambda: _names(L) == ["..", "sub", "a5.jpg", "b4.jpg"],
                            what="読み取り後に掛け直す")
            finally:
                gate.set()
                filer._read_photo_meta = original
                cur = filer._photo_metacache
                filer._photo_metacache = saved_cache
                if cur is not None and cur is not saved_cache:
                    cur.close()   # 開いたままだと一時ファイルが消せない（Windows）
            L.clear_rating()
        finally:
            w.close()
            w.deleteLater()


def _drag_rating(slider, *values, release=True):
    """★の範囲スライダーを、values[0] の目盛りで押して values[1:] へ順に動かす。"""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    y = slider.height() // 2
    at = lambda v: QPoint(int(slider.x_of(v)), y)  # noqa: E731
    QTest.mousePress(slider, Qt.LeftButton, Qt.NoModifier, at(values[0]))
    for v in values[1:]:
        QTest.mouseMove(slider, at(v))
    if release:
        QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier, at(values[-1]))


def test_photo_rating_range_bar():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L, R = w.left, w.right
        bar = L.rating_bar
        slider = bar.slider
        log = lambda: w.log_view.toPlainText()  # noqa: E731
        try:
            # 段は窓ごとに一覧の真下。写真フォルダモードの窓(左)にだけ出る
            assert bar is not R.rating_bar and not hasattr(w, "rating_bar")
            assert bar.parent() is L and R.rating_bar.parent() is R
            assert bar.isVisible() and not R.rating_bar.isVisible()
            assert bar.geometry().top() >= L.view.geometry().bottom()
            assert slider._ticks == ["❌", "未", "1", "2", "ベスト", "フレーム", "みてね"]
            assert slider.values() == (-1, 5) and bar.range_label.text() == "すべて"
            assert not hasattr(bar, "filter_label")         # 「絞り込み: …」の欄は無い
            assert bar.count_label.text() == "表示 5 / 5枚"
            assert not bar.clear_button.isEnabled()
            # どの部品もフォーカスを取らない
            assert slider.focusPolicy() == Qt.NoFocus
            assert bar.clear_button.focusPolicy() == Qt.NoFocus

            # 近い方のつまみが動く(1 は下限の方が近い)。離したらすぐ掛かる
            _drag_rating(slider, 1, 2, 3)
            assert L.rating_range == (3, 5) and slider.values() == (3, 5)
            assert _names(L) == ["..", "sub", "a5.jpg", "b4.jpg", "c3.jpg"]
            assert bar.range_label.text() == "ベストショット以上"
            assert bar.count_label.text() == "表示 3 / 5枚" and bar.clear_button.isEnabled()
            assert "★" not in L.header2.text()               # 見出しの2行目には出さない

            # ドラッグ中は間引く(名前はすぐ、絞り込みは少し後)
            _drag_rating(slider, 5, 4, release=False)
            assert bar.range_label.text() == "ベストショット〜フォトフレーム用"
            assert L.rating_range == (3, 5)
            _wait_until(lambda: L.rating_range == (3, 4), what="ドラッグ中の絞り込み")
            assert _names(L) == ["..", "sub", "b4.jpg", "c3.jpg"]
            assert slider.values() == (3, 4)            # 掛け直してもつまみは戻らない
            QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier)
            assert L.rating_expr() == "★>=3 && ★<=4" and bar.count_label.text() == "表示 2 / 5枚"

            # 2つを重ねると「〜だけ」。重なったつまみは動かした向きで下限か上限かを決める
            _drag_rating(slider, 3, 4)
            assert L.rating_range == (4, 4) and bar.range_label.text() == "フォトフレーム用だけ"
            assert _names(L) == ["..", "sub", "b4.jpg"]
            _drag_rating(slider, 4, 2)
            assert L.rating_range == (2, 4) and bar.range_label.text() == "★2〜フォトフレーム用"
            _drag_rating(slider, 4, 4)                  # 重なっていない上限を掴んで動かさない
            assert L.rating_range == (2, 4)
            assert _names(L) == ["..", "sub", "b4.jpg", "c3.jpg"]

            # ✕ は範囲を「すべて」に戻す
            QTest.mouseClick(bar.clear_button, Qt.LeftButton)
            assert L.rating_range == (-1, 5) and L.rating_node is None
            assert "memo.txt" in _names(L) and "d_reject.jpg" in _names(L)
            assert slider.values() == (-1, 5) and bar.range_label.text() == "すべて"
            assert not bar.clear_button.isEnabled() and "★の絞り込みを解きました" in log()

            # 左右とも写真フォルダなら両方に出て、別々に表示・操作できる
            _drag_rating(slider, 1, 3)
            assert L.rating_range == (3, 5)
            R.load(str(photos))
            _wait(w)
            _wait_until(lambda: len(R.photo_meta) >= 5 and not R._photo_requested,
                        what="右の★の読み取り")
            rbar = R.rating_bar
            assert R.photo_mode and rbar.isVisible() and bar.isVisible()
            assert rbar.slider.values() == (-1, 5) and rbar.range_label.text() == "すべて"
            assert rbar.count_label.text() == "表示 5 / 5枚"
            assert bar.count_label.text() == "表示 3 / 5枚"       # 左は左のまま
            # アクティブでない窓の段も動かせる(押した窓がアクティブになる)
            assert w.active is L
            _drag_rating(rbar.slider, 5, 0)
            assert w.active is R
            assert R.rating_range == (-1, 0) and L.rating_range == (3, 5)
            assert _names(R) == ["..", "sub", "d_reject.jpg", "e_none.jpg"]
            assert rbar.range_label.text() == "未選別まで" and rbar.count_label.text() == "表示 2 / 5枚"
            assert bar.range_label.text() == "ベストショット以上" and slider.values() == (3, 5)
            assert _names(L) == ["..", "sub", "a5.jpg", "b4.jpg", "c3.jpg"]
            # 間引きも窓ごと(左をドラッグ中に右を動かしても、左の値は左に掛かる)
            _drag_rating(slider, 5, 4, release=False)
            assert w.active is L
            _drag_rating(rbar.slider, 0, 1)
            _wait_until(lambda: L.rating_range == (3, 4), what="左の間引き")
            QTest.mouseRelease(slider, Qt.LeftButton, Qt.NoModifier)
            assert R.rating_range == (-1, 1) and L.rating_range == (3, 4)
            # 右の ✕ は右だけを解く
            w.activate("left")
            QTest.mouseClick(rbar.clear_button, Qt.LeftButton)
            assert w.active is R
            assert R.rating_range == (-1, 5)
            assert L.rating_range == (3, 4) and bar.clear_button.isEnabled()

            # プレビュー(F3)で窓が隠れれば、その窓の段も一緒に隠れる
            w.activate("left")
            w.run_command("preview_toggle")
            assert not R.isVisible() and not rbar.isVisible() and bar.isVisible()
            w.run_command("preview_toggle")
            assert R.isVisible() and rbar.isVisible()

            # 写真フォルダモードでない窓では隠れる(片方だけ)
            R.load(str(other))
            _wait(w)
            assert not R.photo_mode and not rbar.isVisible() and bar.isVisible()
            L.set_photo_override("off")
            assert not bar.isVisible()
            L.set_photo_override("auto")
            assert bar.isVisible() and slider.values() == (3, 4)

            # フォルダを移っても保つ(sub は写真フォルダでないので段は隠れ、戻ればまた効く)
            L.load(str(sub))
            _wait(w)
            assert L.rating_range == (3, 4) and not bar.isVisible()
            L.load(str(photos))
            _wait(w)
            assert bar.isVisible() and slider.values() == (3, 4)
            _wait_until(lambda: _names(L) == ["..", "sub", "b4.jpg", "c3.jpg"], what="戻ったあと")
        finally:
            w.close()
            w.deleteLater()


def test_photo_keyword_chips_and_dialog():
    """キーワードが複数あるとき: ★の範囲の段にチップが出て、押したキーワードで絞れる
    (複数は AND、✕ で全部解ける)。R のダイアログではキーワードごとに付け外しできる。"""
    import copy

    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos = Path(tmp, "photos")
        photos.mkdir()
        _photo_jpeg(photos / "a.jpg", 5, ["カレンダー向け"])
        _photo_jpeg(photos / "b.jpg", 4)
        _photo_jpeg(photos / "c.jpg", 3, ["みてね済", "カレンダー向け"])
        _photo_jpeg(photos / "d.jpg", 2)
        _photo_jpeg(photos / "e.jpg", 1, ["みてね済"])
        (photos / "memo.txt").write_text("x", encoding="utf-8")
        other = Path(tmp, "other")
        other.mkdir()
        (other / "x.txt").write_text("x", encoding="utf-8")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")

        cfg = filer.photo_config.load_config()
        cfg["keywords"] = list(cfg["keywords"]) + [{"key": "C", "name": "カレンダー向け"}]
        original_load = filer.photo_config.load_config
        filer.photo_config.load_config = lambda path=None: copy.deepcopy(cfg)
        try:
            w = _photo_window(photos, other, settings_path)
            L = w.left
            fake = _FakePhotoSession()
            w._photo_session = fake
            bar = L.rating_bar
            try:
                # チップは共通設定のキーワードの並びで出る
                assert list(bar.keyword_buttons) == ["みてね済", "カレンダー向け"]
                assert _names(L) == ["..", "a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "memo.txt"], _names(L)
                # カレンダー向けで絞る
                bar.keyword_buttons["カレンダー向け"].click()
                _wait_until(lambda: _names(L) == ["..", "a.jpg", "c.jpg"],
                            what="キーワードで絞り込み")
                assert L.rating_keywords == {"カレンダー向け"}
                # みてね済も押すと AND(両方付いている c.jpg だけ)
                bar.keyword_buttons["みてね済"].click()
                _wait_until(lambda: _names(L) == ["..", "c.jpg"], what="AND の絞り込み")
                # ✕ で全部解ける
                bar.clear_button.click()
                _wait_until(lambda: _names(L) == ["..", "a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "memo.txt"],
                            what="絞り込み解除")
                assert L.rating_keywords == set() and not L.rating_node
                assert not bar.keyword_buttons["カレンダー向け"].isChecked()

                # R のダイアログ: キーワードごとにボタンがあり、Alt+文字も割り当たる
                def step(d):
                    assert list(d.keyword_buttons) == ["みてね済", "カレンダー向け"]
                    assert d.keyword_keys == {"みてね済": "K", "カレンダー向け": "C"}
                    d.cycle_keyword("カレンダー向け")
                    assert d.photo_changes() == {"keywords": {"カレンダー向け": True}}
                opened = _rename_dialog_runner(w, [step])
                L.set_cursor(L._row_of("b.jpg"))
                QTest.keyClick(L.view, Qt.Key_R)
                assert opened, w.log_view.toPlainText()
                path = lambda name: str(photos / name)  # noqa: E731
                assert fake.calls[-1] == ("keyword", [path("b.jpg")], "カレンダー向け", True), \
                    fake.calls
                meta = L.photo_meta[filer.photo_key(path("b.jpg"))]
                assert "カレンダー向け" in meta.keywords, meta
            finally:
                w.close()
                w.deleteLater()
        finally:
            filer.photo_config.load_config = original_load


def test_window_settings_label_tab():
    """Z の設定ダイアログの「ラベル」タブ: 追加・削除(最初のラベルは消せない・同名は不可)。
    決定で初めて共通設定に保存され、★の範囲の段のチップに出る。"""
    import filer
    from PySide6.QtWidgets import QDialog

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        # 共通設定は一時ファイルに差し替える(本物には触らない)
        original_path = filer.photo_config.CONFIG_PATH
        filer.photo_config.CONFIG_PATH = str(Path(tmp, "photo_ratings.json"))
        try:
            w = _photo_window(photos, other, settings_path)
            L = w.left
            state = {}

            def drive(dialog):
                if not hasattr(dialog, "label_list"):
                    return QDialog.Rejected
                try:
                    names = lambda: [dialog.label_list.item(i).text()  # noqa: E731
                                     for i in range(dialog.label_list.count())]
                    assert names() == ["みてね済（キー K）"], names()
                    errors = []
                    dialog.label_error = errors.append
                    dialog.ask_label_name = lambda: "2028年カレンダー向け"
                    dialog.label_add()
                    assert names() == ["みてね済（キー K）", "2028年カレンダー向け（キー C）"], names()
                    dialog.label_add()       # 同じ名前は作れない
                    assert errors == ["ラベル「2028年カレンダー向け」は既にあります"], errors
                    assert len(names()) == 2
                    dialog.ask_label_name = lambda: "お気に入り"
                    dialog.label_add()
                    assert names()[-1] == "お気に入り（キー L）", names()
                    dialog.label_list.setCurrentRow(0)
                    dialog.label_delete()    # 最初のラベルは消せない
                    assert len(names()) == 3 and "最初のラベル" in errors[-1], errors
                    dialog.label_confirm = lambda text: True
                    dialog.label_list.setCurrentRow(2)
                    dialog.label_delete()
                    assert names() == ["みてね済（キー K）", "2028年カレンダー向け（キー C）"], names()
                    # 決定前はファイルに書かれていない
                    assert not Path(tmp, "photo_ratings.json").exists()
                except BaseException as e:  # noqa: BLE001
                    state["error"] = e
                    return QDialog.Rejected
                return QDialog.Accepted
            w.exec_dialog = drive
            try:
                w.cmd_settings_dialog()
                if "error" in state:
                    raise state["error"]
                saved = filer.photo_config.load_config()
                assert saved["keywords"] == [{"key": "K", "name": "みてね済"},
                                             {"key": "C", "name": "2028年カレンダー向け"}], saved
                assert list(L.rating_bar.keyword_buttons) == ["みてね済", "2028年カレンダー向け"]
                assert "ラベルを保存しました" in w.log_view.toPlainText()
            finally:
                w.close()
                w.deleteLater()
        finally:
            filer.photo_config.CONFIG_PATH = original_path


def test_photo_journal_reflects_changes():
    import filer
    from photometa import writer

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        journal = Path(tmp, "journal.jsonl")
        w = _photo_window(photos, other, settings_path)
        w._photo_journal = writer.JournalReader(str(journal))
        L = w.left
        try:
            line = lambda p, r, k=(), pid=1: json.dumps(  # noqa: E731
                {"path": str(p), "rating": r, "keywords": list(k), "time": 0, "pid": pid},
                ensure_ascii=False) + "\n"
            with open(journal, "a", encoding="utf-8") as f:
                f.write(line(photos / "e_none.jpg", 5, ["みてね済"]))
                f.write(line(Path(tmp, "elsewhere.jpg"), 3))          # 一覧に無いもの
                f.write(line(photos / "a5.jpg", 1, pid=os.getpid()))  # 自分の書き込みは除く
                f.write("{壊れた行\n")
            w._poll_photo_journal()
            texts = _rating_texts(L)
            assert texts["e_none.jpg"] == "みてね用 済" and texts["a5.jpg"] == "みてね用"
            # タイマー(1秒ごと)でも拾う。絞り込み中なら掛け直す
            L.set_rating_range(2, 2)
            assert _names(L) == ["..", "sub"]
            with open(journal, "a", encoding="utf-8") as f:
                f.write(line(str(photos / "b4.jpg").upper(), 2))     # 大文字小文字は問わない
            _wait_until(lambda: _names(L) == ["..", "sub", "b4.jpg"], timeout=4,
                        what="変更記録の反映")
            assert filer.PHOTO_JOURNAL_MS <= 1000
            L.clear_rating()
            # 更新日時が変わったファイル(エクスプローラーで★を変えたなど)は、読み直しで読み直す
            target = photos / "e_none.jpg"
            old = os.stat(target).st_mtime
            _photo_jpeg(target, 2)
            os.utime(target, (old + 100, old + 100))
            L.reload(keep_marks=True)
            _wait(w)
            _wait_until(lambda: _rating_texts(L)["e_none.jpg"] == "★2", what="日時の変わった写真")
            assert _rating_texts(L)["b4.jpg"] == "★2"     # 日時の変わらないものは記録の値のまま
        finally:
            w.close()
            w.deleteLater()


def test_photo_journal_replay_on_startup():
    """起動時のなめ読み: 閉じている間に書かれた変更記録を先頭から読み直し、
    永続キャッシュと一覧へ反映する(末尾から読む通常の読み取りでは取りこぼす分)。"""
    import filer
    from photometa import writer

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        journal = Path(tmp, "journal.jsonl")
        line = lambda p, r, k=(), pid=1: json.dumps(  # noqa: E731
            {"path": str(p), "rating": r, "keywords": list(k), "time": 0, "pid": pid},
            ensure_ascii=False) + "\n"
        # 閉じている間に書かれた、という想定で先に記録を用意しておく
        with open(journal, "w", encoding="utf-8") as f:
            f.write(line(photos / "e_none.jpg", 5, ["みてね済"]))
            f.write(line(photos / "e_none.jpg", 2))      # 同じ写真は最後の記録が勝つ
            f.write(line(photos / "b4.jpg", 5))
        cache = filer.photo_metacache.MetaCache(str(Path(tmp, "cache.sqlite")))
        saved_cache = filer._photo_metacache
        filer._photo_metacache = cache
        w = _photo_window(photos, other, settings_path)
        # 通常の読み取りは末尾から(上の記録は全部「過去の」分になる)
        w._photo_journal = writer.JournalReader(str(journal))
        L = w.left
        try:
            w._poll_photo_journal()
            got = cache.get(str(photos / "b4.jpg"))
            assert got is not None and got.rating == 4     # ファイルの値のまま
            w._replay_photo_journal()
            texts = _rating_texts(L)
            assert texts["b4.jpg"] == "みてね用" and texts["e_none.jpg"] == "★2", texts
            # 永続キャッシュにも入っている(次に開いたときの読み取りで効く)
            assert cache.get(str(photos / "b4.jpg")).rating == 5
            assert cache.get(str(photos / "e_none.jpg")).rating == 2
        finally:
            w.close()
            w.deleteLater()
            filer._photo_metacache = saved_cache
            cache.close()


def test_photo_write_with_exiftool():
    """実際に ExifTool で書く(環境変数 PHOTOMETA_EXIFTOOL があるときだけ。一時フォルダのコピーに1件)。"""
    exe_setting = os.environ.get("PHOTOMETA_EXIFTOOL")
    if not exe_setting:
        print("skip  test_photo_write_with_exiftool: PHOTOMETA_EXIFTOOL がありません")
        return
    from photometa import config as pc, writer, xmp
    exe = pc.find_exiftool({"exiftool": exe_setting})
    assert exe, exe_setting
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        session = writer.ExifToolSession(exe, journal_path=str(Path(tmp, "journal.jsonl")))
        w._photo_session = session
        L = w.left
        target = photos / "e_none.jpg"
        mtime = os.stat(target).st_mtime_ns
        try:
            L.set_cursor(L._row_of("e_none.jpg"))
            _rate_with_R(w, 4)
            _wait_until(lambda: "★4 を 1 枚に付けました" in w.log_view.toPlainText(),
                        timeout=30, what="ExifTool の書き込み")
            assert xmp.read_meta(target).rating == 4
            assert os.stat(target).st_mtime_ns == mtime          # 更新日時は変えない
            assert _rating_texts(L)["e_none.jpg"] == "フォトフレーム用"
        finally:
            w.close()
            w.deleteLater()
        assert not session.alive()


def test_photo_no_extra_entries():
    """★の入口は R の「写真の★」の欄(付ける)と★の範囲の段(絞り込む)だけ。写真メニュー
    (Alt+I)・写真のボタン段・Alt+1〜9 のプリセット・Shift+Z の「★で絞り込む ▸」・文字の式の
    絞り込み・書き出しの UI は無い(文字の式と書き出しは PhotoCuller)。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _photo_window(photos, other, settings_path)
        L = w.left
        fake = _FakePhotoSession()
        w._photo_session = fake
        opened = []
        w._exec_menu = lambda menu, chosen: (opened.append(menu.title()), None)[1]
        w.exec_dialog = lambda dialog: (opened.append(dialog.windowTitle()), 0)[1]
        try:
            # Alt+I・Alt+1〜9 では何も起きない
            L.set_cursor(L._row_of("a5.jpg"))
            # ログは★・絞り込みの行だけ比べる(版の読み取りなど、裏の処理の行が届くことがある)
            state = lambda: ([t for t in w.log_view.toPlainText().splitlines()  # noqa: E731
                              if "★" in t or "絞り込み" in t or "プリセット" in t],
                             L.rating_range, set(L.marks), L.path, _names(L))
            before = state()
            for key in (Qt.Key_I, Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4, Qt.Key_5,
                        Qt.Key_6, Qt.Key_7, Qt.Key_8, Qt.Key_9):
                QTest.keyClick(L.view, key, Qt.AltModifier)
            QTest.qWait(30)
            assert opened == [] and not fake.calls, opened
            assert state() == before
            assert not any(f"40{49 + i}" in w.keymap for i in range(9))
            gone = ["cmd_photo_menu", "run_photo_export", "photo_export_menu", "photo_frame_sync",
                    "photo_mitene_collect", "photo_mitene_finish", "photo_manual_delete_list",
                    "photo_bar_items", "press_photo_button", "run_photo_action", "photo_targets",
                    "_photo_filter_items", "photo_filter_menu", "run_photo_filter",
                    "apply_rating_filter", "apply_photo_preset", "ask_filter_expr",
                    "filter_expr_error"] + [f"cmd_photo_preset_{i}" for i in range(1, 10)]
            for name in gone:
                assert not hasattr(w, name), name
            for name in ("PHOTO_EXPORT_KINDS", "PHOTO_FILTER_KINDS", "_SubMenu"):
                assert not hasattr(filer, name), name
            # ボタン段は無い。窓の中のボタンは★の範囲の段(✕ とキーワードのチップ)だけ。
            # 文字の式の状態も無い
            for pane in (L, w.right):
                for name in ("photo_bar", "photo_buttons", "build_photo_bar", "rating_filter",
                             "set_rating_filter"):
                    assert not hasattr(pane, name), name
                bar = pane.rating_bar
                # 履歴ビュー(Shift+H)のフィルタの段の ✕ とチップは別の段なので除いて数える
                history = set(pane.history_bar.findChildren(QPushButton))
                assert set(pane.findChildren(QPushButton)) - history == {
                    bar.clear_button, *bar.keyword_buttons.values()}
            # Shift+Z: 隠し/システムファイルと写真フォルダモードだけ。★・絞り込み・書き出しの行や
            # 小メニューは無い
            rows = []

            def look(menu, chosen):
                actions = [a for a in menu.actions() if not a.isSeparator()]
                rows.extend(a.text() for a in actions)
                assert all(a.menu() is None for a in actions)
                return None
            w._exec_menu = look
            _press_settings_menu(w, L)
            assert [t[:3] for t in rows] == ["&H:", "&S:", "&A:", "&N:", "&F:"], rows
            assert not any(("★" in t or "絞り込" in t or "書き出し" in t) for t in rows), rows
        finally:
            w.close()
            w.deleteLater()


def test_menu_selected_row_color():
    """C / M / Shift+Z などのメニュー(_ChooserMenu)の選んでいる行は、地とはっきり違う色
    (dialog_cursor)で、字(text)とのコントラストも十分。"""
    import filer
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QColor
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        w, left, right, jp = _menu_window(tmp)
        try:
            c = w.colors

            def luminance(name):
                def channel(v):
                    v /= 255
                    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
                q = QColor(name)
                return (0.2126 * channel(q.red()) + 0.7152 * channel(q.green())
                        + 0.0722 * channel(q.blue()))

            def contrast(a, b):
                hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
                return (hi + 0.05) / (lo + 0.05)

            assert QColor(c["dialog_cursor"]) != QColor(c["background"])
            assert contrast(c["text"], c["dialog_cursor"]) >= 4.5
            assert contrast(c["text"], c["background"]) >= 4.5

            menu, chosen = w.build_menu("t", [
                ("O", "反対窓にコピー", "copy"), ("P", "フルパス", "path")])
            first, second = [a for a in menu.actions() if not a.isSeparator()]
            assert set(chosen.values()) == {"copy", "path"}
            menu.popup(w.mapToGlobal(QPoint(40, 40)))
            QTest.qWait(30)
            assert menu.activeAction() is first
            image = menu.grab().toImage()
            dpr = image.devicePixelRatio()

            def color_at(action):
                rect = menu.actionGeometry(action)
                x, y = rect.right() - 6, rect.center().y()   # 字の無い右の余白
                return image.pixelColor(int(x * dpr), int(y * dpr))

            selected, plain = color_at(first), color_at(second)
            assert selected == QColor(c["dialog_cursor"]), selected.name()
            assert plain == QColor(c["background"]), plain.name()
            menu.hide()
        finally:
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# 合計ファイルサイズ(I = 占有合計 / Shift+I = 単純合計)と E / V の画像の確認
# ---------------------------------------------------------------
def _make_size_tree(root):
    """一時フォルダに、サイズの分かったファイルとフォルダを作る。"""
    root = Path(root)
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "empty").mkdir()
    for rel, size in (("zero.bin", 0), ("one.bin", 1), ("five.bin", 5000),
                      ("sub/c.bin", 4096), ("sub/deep/e.bin", 4097)):
        (root / rel).write_bytes(b"x" * size)
    return root


def _make_junction(link, target) -> bool:
    """ジャンクションを作る(管理者権限は要らない)。作れなければ False。"""
    import subprocess
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return os.path.isdir(link)


def _denied_scandir(name):
    """name という名前のフォルダだけ読めない scandir(アクセス拒否の代わり)。"""
    def scandir(path):
        if os.path.basename(path) == name:
            raise PermissionError(5, "アクセスが拒否されました", path)
        return os.scandir(path)
    return scandir


def test_tally_sizes_core():
    import filer_shell as sh
    assert core.round_to_cluster(0, 4096) == 0
    assert core.round_to_cluster(1, 4096) == 4096
    assert core.round_to_cluster(4096, 4096) == 4096
    assert core.round_to_cluster(4097, 4096) == 8192
    assert core.round_to_cluster(5, None) == 5      # クラスタが分からなければそのまま
    assert core.bytes_unit(1023) == "" and core.bytes_unit(5003) == "4.9KB"
    assert core.format_bytes(5003) == "5,003 (4.9KB)" and core.format_bytes(10) == "10"
    t = core.SizeTally(files=1234, dirs=56, logical=1, allocated=12345678901)
    assert core.size_total_text(t, True, "sub") == (
        "占有合計（sub）: 1,234 ファイル / 56 フォルダ　12,345,678,901 バイト（11.5GB）")
    assert core.size_total_text(t, False) == "単純合計: 1,234 ファイル / 56 フォルダ　1 バイト"

    cluster = lambda path: 4096  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_size_tree(tmp)
        # フォルダ全体(根は数えない): ファイル 5・フォルダ 3(sub / deep / empty)
        r = core.tally_sizes([str(root)], cluster, count_roots=False)
        assert (r.files, r.dirs, r.unreadable, r.cancelled) == (5, 3, 0, False), r
        assert r.logical == 0 + 1 + 5000 + 4096 + 4097
        assert r.allocated == 0 + 4096 + 8192 + 4096 + 8192      # 0 バイトは 0
        # 指定したフォルダ自身も数える・ファイル1つ・両方
        r = core.tally_sizes([str(root / "sub")], cluster)
        assert (r.files, r.dirs, r.logical, r.allocated) == (2, 2, 8193, 12288), r
        r = core.tally_sizes([str(root / "one.bin")], cluster)
        assert (r.files, r.dirs, r.logical, r.allocated) == (1, 0, 1, 4096), r
        r = core.tally_sizes([str(root / "one.bin"), str(root / "empty")], None)
        assert (r.files, r.dirs, r.logical, r.allocated) == (1, 1, 1, 1), r
        # 消えたものは飛ばす
        r = core.tally_sizes([str(root / "gone.bin")], cluster)
        assert (r.files, r.dirs, r.unreadable) == (0, 0, 0)
        # 読めないフォルダ: 数を飛ばして unreadable に数える(フォルダ自身は数える)
        r = core.tally_sizes([str(root / "sub")], cluster, scandir=_denied_scandir("deep"))
        assert (r.files, r.dirs, r.unreadable, r.logical) == (1, 2, 1, 4096), r
        # 中止: 立っていれば何も数えずに cancelled
        stop = threading.Event()
        stop.set()
        r = core.tally_sizes([str(root)], cluster, cancel=stop, count_roots=False)
        assert r.cancelled and r.files == 0
        # 途中で中止(2件目を見たところで立てる)。途中経過は増えていく件数
        stop = threading.Event()
        seen_count = []
        old = core.SIZE_PROGRESS_SEC
        core.SIZE_PROGRESS_SEC = 0
        try:
            def progress(n):
                seen_count.append(n)
                if n >= 2:
                    stop.set()
            r = core.tally_sizes([str(root)], cluster, cancel=stop, progress=progress,
                                 count_roots=False)
        finally:
            core.SIZE_PROGRESS_SEC = old
        assert r.cancelled and r.files + r.dirs < 8, r
        assert seen_count and seen_count == sorted(seen_count)
        # ジャンクションはフォルダとして1件数えるが、中へは潜らない
        link = root / "link"
        if _make_junction(link, root / "sub"):
            try:
                r = core.tally_sizes([str(root)], cluster, count_roots=False)
                assert (r.files, r.dirs) == (5, 4), r
                r = core.tally_sizes([str(link)], cluster)     # 根がジャンクションでも潜らない
                assert (r.files, r.dirs) == (0, 1), r
            finally:
                os.rmdir(link)
        # 本物のクラスタの大きさ(一時フォルダのドライブ)
        size = sh.cluster_size(core.drive_root(str(root)))
        assert size and size % 512 == 0, size

    # 書庫の中: 展開したときのサイズの単純合計
    import filer_archive
    with tempfile.TemporaryDirectory() as tmp:
        zpath = os.path.join(tmp, "a.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("top.txt", b"t" * 10)
            z.writestr("dir/in.txt", b"i" * 20)
            z.writestr("dir/more/x.txt", b"x" * 30)
        index = filer_archive.get_index(zpath)
        r = core.tally_archive(index, "")
        assert (r.files, r.dirs, r.logical, r.allocated) == (3, 2, 60, 60), r
        r = core.tally_archive(index, "", ["dir"])
        assert (r.files, r.dirs, r.logical) == (2, 2, 50), r
        r = core.tally_archive(index, "dir", ["in.txt", "nothing"])
        assert (r.files, r.dirs, r.logical) == (1, 0, 20), r
        r = core.tally_archive(index, "dir")
        assert (r.files, r.dirs, r.logical) == (2, 1, 50), r


def _size_window(left, right, settings_path):
    import filer
    w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                     "editor": "ed"}}, str(settings_path))
    w.resize(1000, 600)
    w.show()
    w.start()
    _wait(w)
    return w


def _wait_size_threads(w, timeout=10.0):
    _wait_until(lambda: not any(t.is_alive() and t.name == "filer-size-total"
                                for t in w._tracked_threads),
                timeout=timeout, what="合計ファイルサイズのスレッドが終わる")


def test_size_total_keys():
    import filer_shell as sh
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        (left / "sub1" / "s.txt").write_bytes(b"s" * 3)
        (left / "sub1" / "deep" / "x.bin").write_bytes(b"x" * 5000)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _size_window(left, right, settings_path)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        log = lambda: w.log_view.toPlainText()  # noqa: E731
        cluster = sh.cluster_size(core.drive_root(str(left)))
        up = lambda n: core.round_to_cluster(n, cluster)  # noqa: E731
        menus = []
        w._exec_menu = lambda menu, chosen: menus.append(menu.title())   # 開いたら記録だけ
        real_tally = core.tally_sizes
        try:
            # I: カーソル行の1ファイル(占有はクラスタの倍数に切り上げ)。写真メニューは開かない
            L.set_cursor(L._row_of("alpha.txt"))
            key(Qt.Key_I)
            _wait_until(lambda: w._size_job is None and "占有合計（alpha.txt）" in log(),
                        what="I の結果")
            assert f"占有合計（alpha.txt）: 1 ファイル / 0 フォルダ　{up(10):,} バイト" in log(), log()
            assert w.status_state == "Ready." and menus == []

            # Shift+I: カーソル行のフォルダ(中まで数え、フォルダ自身も数える)
            L.set_cursor(L._row_of("sub1"))
            key(Qt.Key_I, Qt.ShiftModifier)
            _wait_until(lambda: "単純合計（sub1）" in log(), what="Shift+I の結果")
            assert "単純合計（sub1）: 2 ファイル / 2 フォルダ　5,003 バイト（4.9KB）" in log(), log()

            # マークあり: マークしたもの全部(件数を添える)
            L.marks.update({"alpha.txt", "sub1"})
            L.set_cursor(L._row_of("beta.md"))
            key(Qt.Key_I)
            _wait_until(lambda: "占有合計（マーク 2件）" in log(), what="マークの結果")
            total = up(10) + up(3) + up(5000)
            assert f"占有合計（マーク 2件）: 3 ファイル / 2 フォルダ　{total:,} バイト" in log(), log()
            L.marks.clear()
            L.model.all_changed()

            # "..": いまのフォルダ全体(フォルダ自身は数えない)
            L.set_cursor(0)
            assert L.cursor_entry().is_parent
            key(Qt.Key_I, Qt.ShiftModifier)
            _wait_until(lambda: f"単純合計（{L.path} 全体）" in log(), what=".. の結果")
            assert (f"単純合計（{L.path} 全体）: 6 ファイル / 3 フォルダ　"
                    f"{10 + 20 + 1 + 100 + 3 + 5000:,} バイト（5.0KB）") in log(), log()

            # 読めないフォルダ: 飛ばして2行目に件数
            core.tally_sizes = lambda *a, **k: real_tally(*a, scandir=_denied_scandir("deep"), **k)
            L.set_cursor(L._row_of("sub1"))
            key(Qt.Key_I, Qt.ShiftModifier)
            _wait_until(lambda: "読めなかったフォルダ 1 件" in log(), what="読めないフォルダ")
            assert "単純合計（sub1）: 1 ファイル / 2 フォルダ　3 バイト" in log(), log()

            # Esc で中止: 結果を待たずにすぐ知らせ、後から届いた結果は出さない。窓は閉じない
            gate = threading.Event()

            def slow(path):
                gate.wait(10)
                return os.scandir(path)
            core.tally_sizes = lambda *a, **k: real_tally(*a, scandir=slow, **k)
            before = log().count("占有合計（sub1）")
            key(Qt.Key_I)
            assert w._size_job is not None and w.status_state.startswith("数えています…")
            key(Qt.Key_I, Qt.ShiftModifier)            # 数えている間はもう1つ始めない
            assert "単純合計: 数えている途中です（Esc で中止）" in log()
            key(Qt.Key_Escape)
            assert w._size_job is None and w.status_state == "Ready."
            assert log().rstrip().endswith("占有合計: 中止しました"), log()
            gate.set()
            _wait_size_threads(w)
            QTest.qWait(50)
            assert log().count("占有合計（sub1）") == before
            assert w.isVisible()
            core.tally_sizes = real_tally

            # 書庫の中: 中の項目のサイズを足す(その場で出る)
            zpath = left / "pack.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("top.txt", b"t" * 10)
                z.writestr("dir/in.txt", b"i" * 20)
            L.load(str(left))
            _wait(w)
            L.set_cursor(L._row_of("pack.zip"))
            key(Qt.Key_Return)
            _wait(w)
            assert L.archive == (str(zpath), "")
            L.set_cursor(0)
            key(Qt.Key_I)
            assert "全体）: 2 ファイル / 1 フォルダ　30 バイト　※書庫の中は展開後のサイズ" in log(), log()
            L.set_cursor(L._row_of("dir"))
            key(Qt.Key_I, Qt.ShiftModifier)
            assert "単純合計（dir）: 1 ファイル / 1 フォルダ　20 バイト" in log(), log()
            assert menus == []
        finally:
            core.tally_sizes = real_tally
            _wait_size_threads(w)
            w.close()


def test_edit_confirms_images():
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = _make_tree(tmp)
        (left / "shot.CR2").write_bytes(b"raw")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = _size_window(left, right, settings_path)
        L = w.left
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(w.active.view, k, mods)  # noqa: E731
        asked, answers, calls = [], [], []

        def confirm(title, text, default_yes=True, parent=None):
            asked.append((text, default_yes))
            return answers.pop(0)
        w.confirm = confirm
        real_popen = filer.subprocess.Popen
        filer.subprocess.Popen = lambda args, **kw: calls.append(args)  # エディタは起こさない
        try:
            # 画像を含むマーク + E: 確かめる(既定は「いいえ」)。いいえなら開かない
            L.marks.update({"alpha.txt", "photo.JPG"})
            answers.append(False)
            key(Qt.Key_E)
            assert asked == [("画像 1 件を含む 2 件をエディタで開きますか？", False)], asked
            assert calls == []
            # はいなら全部開く
            answers.append(True)
            key(Qt.Key_E)
            assert len(asked) == 2
            assert sorted(os.path.basename(a[1]) for a in calls) == ["alpha.txt", "photo.JPG"]
            # 画像を含まなければ確認なし(V も同じ)
            calls.clear()
            L.marks.clear()
            L.marks.add("beta.md")
            key(Qt.Key_V)
            assert len(asked) == 2 and [os.path.basename(a[1]) for a in calls] == ["beta.md"]
            # V でカーソル行の RAW: 確かめる。いいえなら開かない
            calls.clear()
            L.marks.clear()
            L.set_cursor(L._row_of("shot.CR2"))
            answers.append(False)
            key(Qt.Key_V)
            assert asked[-1] == ("画像 1 件を含む 1 件をエディタで開きますか？", False), asked
            assert calls == [] and not answers
        finally:
            filer.subprocess.Popen = real_popen
            w.close()


# ---------------------------------------------------------------
# Shift+F のメニュー(E: Everything 検索 / P: ★写真抽出)
# ---------------------------------------------------------------
def test_search_menu_shift_f():
    """Shift+F でメニュー(頭の文字で選んで Enter)が出て、E で今までどおり Everything 検索
    (es.exe を探す → 入力窓 → 検索)へ進む。es.exe の呼び出しはモックする。"""
    import filer
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = Path(tmp, "l"), Path(tmp, "r")
        left.mkdir()
        right.mkdir()
        (left / "a.txt").write_text("x", encoding="utf-8")
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right)}},
                              str(settings_path))
        w.show()
        w.start()
        _wait(w)
        L = w.left
        menus, searched = [], []
        try:
            assert core.DEFAULT_KEYMAP["1070"] == "everything"
            assert core.COMMAND_SHORT_NAMES["photo_pick"]
            w.find_es = lambda: ["es.exe"]
            w.ask_everything = lambda text, scope_all: ("メモ", False)
            L.load_everything = lambda query, scope_all=False, keep_marks=False: \
                searched.append((query, scope_all))

            def choose(value):
                def run(menu, chosen):
                    menus.append((menu.title(), [(filer._menu_key_of(a), v)
                                                 for a, v in chosen.items()]))
                    return value
                return run

            w._exec_menu = choose("everything_search")
            QTest.keyClick(L.view, Qt.Key_F, Qt.ShiftModifier)
            title, items = menus[-1]
            assert title == "検索", title
            assert items == [("E", "everything_search"), ("P", "photo_pick"), ("Q", None)], items
            assert searched == [("メモ", False)], searched
            # キャンセル(Q / Esc)では何もしない
            w._exec_menu = choose(None)
            QTest.keyClick(L.view, Qt.Key_F, Qt.ShiftModifier)
            assert searched == [("メモ", False)] and not L.pick_loading and not L.flat
        finally:
            w.close()
            w.deleteLater()


class _FakeStat:
    def __init__(self, attrs=0, size=1, is_dir=False, tag=0):
        import stat as _stat
        self.st_file_attributes = attrs | (_stat.FILE_ATTRIBUTE_DIRECTORY if is_dir else 0)
        self.st_mode = _stat.S_IFDIR if is_dir else _stat.S_IFREG
        self.st_size = size
        self.st_mtime = 1.0
        self.st_reparse_tag = tag


class _FakeDirEntry:
    def __init__(self, folder, name, st):
        self.name = name
        self.path = os.path.join(folder, name)
        self._st = st

    def stat(self, follow_symlinks=True):
        return self._st


def test_collect_photos_logic():
    """★写真抽出の走査: 写真だけ・相対パス。ジャンクション/シンボリックリンクはたどらず、
    OneDrive のフォルダはたどる。オンライン専用の写真は入れずに数える。中止・上限。"""
    import stat as _stat
    import threading as _threading

    root = "C:\\pics"
    reparse = _stat.FILE_ATTRIBUTE_REPARSE_POINT
    tree = {
        root: [("a.jpg", _FakeStat()), ("memo.txt", _FakeStat()),
               ("cloud.jpg", _FakeStat(core.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | reparse,
                                       tag=0x9000701A)),
               ("off.png", _FakeStat(_stat.FILE_ATTRIBUTE_OFFLINE)),
               ("hid.jpg", _FakeStat(_stat.FILE_ATTRIBUTE_HIDDEN)),
               ("sub", _FakeStat(is_dir=True)),
               ("od", _FakeStat(reparse, is_dir=True, tag=0x9000701A)),
               ("junc", _FakeStat(reparse, is_dir=True, tag=core.IO_REPARSE_TAG_MOUNT_POINT)),
               ("link", _FakeStat(reparse, is_dir=True, tag=core.IO_REPARSE_TAG_SYMLINK))],
        root + "\\sub": [("b.HEIC", _FakeStat()), ("c.arw", _FakeStat())],
        root + "\\od": [("d.jpg", _FakeStat())],
        root + "\\junc": [("never.jpg", _FakeStat())],
        root + "\\link": [("never2.jpg", _FakeStat())],
    }
    opened = []

    class _It(list):
        def __enter__(self):
            return iter(self)

        def __exit__(self, *a):
            return False

    def fake_scandir(folder):
        opened.append(folder)
        return _It(_FakeDirEntry(folder, n, st) for n, st in tree[folder])

    found, offline, status = core.collect_photos(root, scandir=fake_scandir)
    names = sorted(e.name for e in found)
    assert status == "done" and offline == 2, (status, offline)
    assert names == ["a.jpg", "hid.jpg", "od\\d.jpg", "sub\\b.HEIC", "sub\\c.arw"], names
    assert root + "\\junc" not in opened and root + "\\link" not in opened
    # 隠しを見せない設定なら入れない
    found, _o, _s = core.collect_photos(root, show_hidden=False, scandir=fake_scandir)
    assert "hid.jpg" not in [e.name for e in found]
    # 上限・中止
    found, _o, status = core.collect_photos(root, limit=2, scandir=fake_scandir)
    assert status == "limit" and len(found) == 2
    cancel = _threading.Event()
    cancel.set()
    assert core.collect_photos(root, cancel=cancel, scandir=fake_scandir)[2] == "cancel"
    assert core.is_online_only(_stat.FILE_ATTRIBUTE_OFFLINE)
    assert not core.is_online_only(_stat.FILE_ATTRIBUTE_ARCHIVE | reparse)
    # 窓の下の行
    assert core.pick_footer_text("D:\\p", 5, 5, 10) == "★写真抽出: D:\\p（5 枚）   10 bytes"
    assert core.pick_footer_text("D:\\p", 3, 5, 10, 1, 4).startswith("★写真抽出: D:\\p（3 / 5 枚）")


def _make_pick_tree(tmp):
    """入れ子のフォルダに★いろいろの写真(★は XMP に埋めた小さな JPEG)。"""
    root = Path(tmp, "pics")
    (root / "y2023" / "deep").mkdir(parents=True)
    (root / "y2024").mkdir()
    _photo_jpeg(root / "a3.jpg", 3)
    _photo_jpeg(root / "top_none.jpg")
    (root / "memo.txt").write_text("x", encoding="utf-8")
    (root / "memo2.txt").write_text("x", encoding="utf-8")
    _photo_jpeg(root / "y2023" / "b5.jpg", 5)
    _photo_jpeg(root / "y2023" / "c1.jpg", 1)
    _photo_jpeg(root / "y2023" / "d_reject.jpg", -1)
    _photo_jpeg(root / "y2023" / "deep" / "e4.jpg", 4, ["みてね済"])
    _photo_jpeg(root / "y2023" / "deep" / "f0.jpg", 0)
    _photo_jpeg(root / "y2024" / "g2.jpg", 2)
    other = Path(tmp, "dst")
    other.mkdir()
    return root, other


PICKED = ["a3.jpg", "y2023\\b5.jpg", "y2023\\c1.jpg", "y2023\\deep\\e4.jpg", "y2024\\g2.jpg"]


def test_photo_read_metacache_progress_online():
    """★の列の読み取り: 永続キャッシュ(2回目はファイルを開かない)・1秒以上で中央に
    進み具合を出す・オンライン専用の写真は開かずに飛ばす。"""
    import stat as _stat
    import threading as _threading

    import filer
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        photos, sub, other = _make_photo_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        saved_cache = filer._photo_metacache
        original = filer._read_photo_meta
        try:
            # 1回目: 並行読みで★が入り、永続キャッシュにも入る
            cache = filer.photo_metacache.MetaCache(str(Path(tmp, "meta.sqlite")))
            filer._photo_metacache = cache
            w = _photo_window(photos, other, settings_path)
            L = w.left
            try:
                texts = _rating_texts(L)
                assert texts["a5.jpg"] == "みてね用" and texts["b4.jpg"] == "フォトフレーム用"
                assert texts["c3.jpg"] == "ベストショット 済" and texts["e_none.jpg"] == ""
                hit = cache.get(str(photos / "a5.jpg"))
                assert hit is not None and hit.rating == 5
            finally:
                w.close()
                w.deleteLater()

            # 2回目: 読み取り関数を数えるものに替えても、キャッシュで済んで呼ばれない
            calls = []

            def counting(p):
                calls.append(p)
                return original(p)

            filer._read_photo_meta = counting
            try:
                w = _photo_window(photos, other, settings_path)
                try:
                    assert calls == [], calls
                    assert _rating_texts(w.left)["b4.jpg"] == "フォトフレーム用"
                finally:
                    w.close()
                    w.deleteLater()
            finally:
                filer._read_photo_meta = original

            # 進み具合: 空のキャッシュ＋遅い読み取りで、1秒たつと中央表示が出て、
            # 読み終わると消える
            filer._photo_metacache = filer.photo_metacache.MetaCache(
                str(Path(tmp, "meta_slow.sqlite")))
            gate = _threading.Event()
            filer._read_photo_meta = lambda p: (gate.wait(10), original(p))[1]
            try:
                w = filer.FilerWindow(
                    {"filer": {"left_path": str(photos), "right_path": str(other)}},
                    str(settings_path))
                w.resize(1100, 600)
                w.show()
                w.start()
                try:
                    _wait_until(lambda: w.left.read_progress.isVisible(), timeout=8,
                                what="中央の進み具合表示")
                    assert "★を読んでいます…" in w.left.read_progress.label.text()
                    assert "/ 5" in w.left.read_progress.label.text()
                    gate.set()
                    _wait_until(
                        lambda: len(w.left.photo_meta) >= 5 and not w.left._photo_requested,
                        what="★の読み取りが終わる")
                    _wait_until(lambda: not w.left.read_progress.isVisible(), timeout=5,
                                what="中央表示が消える")
                finally:
                    gate.set()
                    w.close()
                    w.deleteLater()
            finally:
                filer._read_photo_meta = original

            # オンライン専用: 属性の立った写真は開かず、★は不明のまま。ログに枚数が出る
            filer._photo_metacache = filer.photo_metacache.MetaCache(
                str(Path(tmp, "meta_online.sqlite")))
            filer._read_photo_meta = counting
            calls.clear()
            try:
                w = _photo_window(photos, other, settings_path)
                L = w.left
                try:
                    # 窓を開いた時点でもうキャッシュに入っているので、空のキャッシュに
                    # 替えてから読み直させる(替えないと4枚もキャッシュで済んでしまう)
                    filer._photo_metacache = filer.photo_metacache.MetaCache(
                        str(Path(tmp, "meta_online2.sqlite")))
                    L.reset_photo()
                    calls.clear()
                    # Entry は frozen なので、属性を立てた複製に入れ替える
                    import dataclasses as _dc
                    for lst in (L.entries, L.all_entries):
                        for i, e in enumerate(lst):
                            if e.name == "a5.jpg":
                                lst[i] = _dc.replace(
                                    e, attrs=e.attrs | _stat.FILE_ATTRIBUTE_OFFLINE)
                    L.start_photo_read()
                    _wait_until(
                        lambda: len(L.photo_meta) >= 5 and not L._photo_requested,
                        what="オンライン専用を除く読み取り")
                    key = filer.photo_key(str(photos / "a5.jpg"))
                    assert L.photo_meta[key] is None            # ★は不明として扱う
                    assert not any(p.endswith("a5.jpg") for p in calls), calls
                    assert len(calls) == 4                      # 残り4枚だけ読んだ
                    assert "オンライン専用の写真 1 枚は飛ばしました" in w.log_view.toPlainText()
                finally:
                    w.close()
                    w.deleteLater()
            finally:
                filer._read_photo_meta = original
        finally:
            cur = filer._photo_metacache
            filer._photo_metacache = saved_cache
            if cur is not None and cur is not saved_cache:
                cur.close()
            cache.close()


def test_photo_pick():
    """Shift+F → P(★写真抽出): 配下の★1以上の写真をフラット表示の形で並べ、写真フォルダモード
    (★の列・★の段)にする。スライダーとは AND。R で★を外すと消える。C の複写の後も抽出のまま。
    Esc で中止・書庫の中では使えない・BS / Alt+B / フォルダの移動で元に戻る。"""
    import filer
    import threading as _threading
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        root, other = _make_pick_tree(tmp)
        settings_path = Path(tmp, "settings.json")
        settings_path.write_text("{}", encoding="utf-8")
        # ★の永続キャッシュはテスト用の一時ファイルに差し替える(本物を汚さない)
        saved_metacache = filer._photo_metacache
        filer._photo_metacache = filer.photo_metacache.MetaCache(
            str(Path(tmp, "pick_cache.sqlite")))
        w = filer.FilerWindow({"filer": {"left_path": str(root), "right_path": str(other)}},
                              str(settings_path))
        w.extra_op_flags = _quiet_flags()
        w.resize(1100, 600)
        w.show()
        w.start()
        _wait(w)
        L = w.left
        fake = _FakePhotoSession()
        w._photo_session = fake
        w._exec_menu = lambda menu, chosen: "photo_pick"
        key = lambda k, mods=Qt.NoModifier: QTest.keyClick(L.view, k, mods)  # noqa: E731
        log = lambda: w.log_view.toPlainText()  # noqa: E731

        def pick():
            key(Qt.Key_F, Qt.ShiftModifier)
            _wait(w, timeout=20)

        try:
            # 写真2枚・テキスト2つ: 抽出の前は写真フォルダモードではない
            assert not L.photo_mode and not L.rating_bar.isVisible()
            before = sorted(_names(L))
            pick()
            assert L.pick_root is not None and core.same_dir(L.pick_root, root)
            assert L.flat and core.same_dir(L.path, root)
            assert sorted(_names(L)) == sorted(PICKED), _names(L)      # ★1以上だけ。.. も無い
            assert L.photo_mode and L.rating_bar.isVisible()
            assert not L.view.horizontalHeader().isSectionHidden(filer.COL_RATING)
            assert L.header.text().startswith("📷 ") and "[★写真抽出]" in L.header.text()
            assert L.footer.text().startswith(f"★写真抽出: {root}（5 枚）"), L.footer.text()
            assert "★1以上を 5 枚（★を読んだ写真 8 枚）" in log(), log()
            # 完了ログに所要時間とキャッシュ/読み取りの内訳が出る(初回は全部読み取り)
            assert "（キャッシュ 0 / 読み取り 8）" in log(), log()
            texts = _rating_texts(L)
            assert texts["y2023\\b5.jpg"] == "みてね用" and texts["y2023\\deep\\e4.jpg"] == "フォトフレーム用 済"
            assert L.rating_bar.count_label.text() == "表示 5 / 5枚"

            # スライダーを「ベストショット以上」に: ★3以上だけ(基準とは AND)
            _drag_rating(L.rating_bar.slider, 1, 3)
            assert L.rating_range == (3, 5)
            assert L.rating_bar.range_label.text() == "ベストショット以上"
            assert sorted(_names(L)) == ["a3.jpg", "y2023\\b5.jpg", "y2023\\deep\\e4.jpg"], _names(L)
            assert "（3 / 5 枚）" in L.footer.text() and L.rating_bar.count_label.text() == "表示 3 / 5枚"
            # 削除候補だけ・未選別だけでは 0 件(基準が★1以上なので)
            L.set_rating_range(-1, -1)
            assert _names(L) == []
            L.set_rating_range(0, 0)
            assert _names(L) == []
            L.clear_rating()
            assert sorted(_names(L)) == sorted(PICKED)
            # Shift+Z で写真フォルダモードを切っても、抽出の表示では外れない
            L.set_photo_override("off")
            assert L.photo_mode and sorted(_names(L)) == sorted(PICKED)
            L.set_photo_override("auto")

            # R: ★の欄だけのダイアログ。★を外した写真は一覧から消える
            L.set_cursor(L._row_of("y2023\\c1.jpg"))
            dialog = _rate_with_R(w, 0)
            assert dialog.photo_only and dialog.windowTitle() == "写真の★"
            assert dialog.name_edit.isHidden() and dialog.photo_box is not None
            assert fake.calls[-1] == ("rating", [str(root / "y2023" / "c1.jpg")], 0)
            _wait_until(lambda: "y2023\\c1.jpg" not in _names(L), what="★を外した写真が消える")
            assert len(_names(L)) == 4 and L.pick_root is not None and "（4 枚）" in L.footer.text()
            # K(フォルダ作成)はフラット表示と同じく断る
            key(Qt.Key_K)
            assert "フラット表示・検索結果では使えません" in log()

            # マークして C(複写)。転送の後の再読込でも抽出の表示のまま(★は読んだものを使う)
            L.marks.add("y2023\\b5.jpg")
            key(Qt.Key_C, Qt.AltModifier)
            _wait(w, timeout=30)
            _wait_until(lambda: not L.pick_loading, timeout=10, what="集め直し")
            assert (other / "b5.jpg").is_file()
            assert L.pick_root is not None and L.flat and L.photo_mode
            assert len(_names(L)) == 4 and "y2023\\c1.jpg" not in _names(L), _names(L)

            # F3(ビュア)は抽出の写真を開ける
            L.set_cursor(L._row_of("y2023\\deep\\e4.jpg"))
            w.run_command("preview_toggle")
            _wait_until(lambda: w.preview_widget is not None
                        and w.preview_widget.rating.text().startswith("🖼️ フォトフレーム用"),
                        what="ビュアの★")
            w.preview_off()

            # BS で元の表示へ(親へは行かない)
            key(Qt.Key_Backspace)
            _wait(w)
            assert L.pick_root is None and not L.flat and core.same_dir(L.path, root)
            assert sorted(_names(L)) == before and not L.photo_mode
            assert "★写真抽出" not in L.header.text() and not L.rating_bar.isVisible()
            # Alt+B でも抜ける
            pick()
            assert L.pick_root is not None
            key(Qt.Key_B, Qt.AltModifier)
            _wait(w)
            assert L.pick_root is None and not L.flat and sorted(_names(L)) == before
            # フォルダを移っても抜ける
            pick()
            L.load(str(root / "y2024"))
            _wait(w)
            assert L.pick_root is None and not L.flat and _names(L) == ["..", "g2.jpg"]
            L.load(str(root))
            _wait(w)

            # Esc で中止(★の読み取りを止めて、元の表示へ)。
            # 永続キャッシュが効いていると読み直さないので、この間だけ空のキャッシュに替える
            L.reset_photo()                     # 読んだ★を捨てて、読み取りを待たせる
            gate = _threading.Event()
            original = filer._read_photo_meta
            filer._read_photo_meta = lambda p: (gate.wait(5), original(p))[1]
            pick_cache = filer._photo_metacache
            filer._photo_metacache = filer.photo_metacache.MetaCache(
                str(Path(tmp, "pick_cache_esc.sqlite")))
            try:
                key(Qt.Key_F, Qt.ShiftModifier)
                _wait_until(lambda: "★を読んでいます…" in L.footer.text(), what="読み取り中の表示")
                assert L.pick_loading and "/ 8（Esc で中止）" in L.footer.text(), L.footer.text()
                # 読み取り中は一覧の中央にも進み具合が出る
                assert L.read_progress.isVisible()
                assert "★を読んでいます…" in L.read_progress.label.text()
                # 進み具合には、中止しても読んだ分はキャッシュに残る旨の注意書きが出る
                assert filer.PICK_READ_NOTE in L.read_progress.label.text()
                assert filer.PICK_READ_NOTE in L.footer.text()
                key(Qt.Key_Escape)
                gate.set()
                _wait(w, timeout=10)
            finally:
                gate.set()
                filer._read_photo_meta = original
                filer._photo_metacache = pick_cache
            assert not L.read_progress.isVisible()      # 終われば中央表示は消える
            assert "★写真抽出: 中止しました" in log()
            # 中止のログにも完了時と同じ内訳(キャッシュ/読み取り)と、残る旨が出る
            assert "★写真抽出: 中止しました（★を読んだ写真" in log()
            assert "読んだ分はキャッシュに残り、次回の処理はその分高速化されます" in log()
            assert not L.pick_loading and L.pick_root is None and not L.flat
            assert sorted(_names(L)) == before and w.isVisible()

            # 書庫の中では使えない
            zpath = Path(tmp, "pack.zip")
            _make_zip(str(zpath))
            L.load(str(zpath))
            _wait(w)
            assert L.archive is not None
            key(Qt.Key_F, Qt.ShiftModifier)
            assert "★写真抽出: 書庫内では使えません" in log()
            assert not L.pick_loading and L.pick_root is None
        finally:
            w.close()
            w.deleteLater()
            cur = filer._photo_metacache
            filer._photo_metacache = saved_metacache
            if cur is not None and cur is not saved_metacache:
                cur.close()
            pick_cache = locals().get("pick_cache")
            if pick_cache is not None and pick_cache is not saved_metacache:
                pick_cache.close()


# 窓を立てる重いテストのうち、中核（FULL_ONLY に無い窓テスト）以外は普段回さない。
# CI（GITHUB_ACTIONS）と --full のときだけ全部回す。守る対象は変えず、手元の確認を
# 軽くするための仕分け（2026-11-03 Nagato の「最低限にして」による）。
# pytest で集めた場合は main() を通らないので全部回る。
FULL_ONLY = frozenset({
    "test_archive_password_and_missing_py7zr",
    "test_auto_refresh_on_external_change",
    "test_chooser_menu_delete_key",
    "test_dialogs",
    "test_drive_list",
    "test_edit_confirms_images",
    "test_enter_viewer_routing",
    "test_ext_column_fits_long_ext",
    "test_file_info_focus_and_checkbox_look",
    "test_live_filter_logic",
    "test_log_selection_ctrl_c",
    "test_menu_selected_row_color",
    "test_open_shell_and_explorer",
    "test_photo_mode_judgement",
    "test_photo_mode_window_and_rating_column",
    "test_photo_no_extra_entries",
    "test_photo_rating_range_bar",
    "test_photo_rating_slider_only",
    "test_photo_read_metacache_progress_online",
    "test_photo_write_targets",
    "test_preload_modules",
    "test_preview_applies_edits",
    "test_preview_catches_up_with_cursor",
    "test_preview_fullscreen_from_right",
    "test_preview_image_lr_keys",
    "test_preview_image_repeat_waits_for_display",
    "test_preview_image_rotate_keys",
    "test_rename_photo_section",
    "test_single_instance_and_show",
    "test_size_total_keys",
    "test_stash_pane",
    "test_stash_register_keys_and_cursor",
    "test_tally_sizes_core",
    "test_transfer_cursor_follows_to_other_pane",
    "test_update_decide_and_check",
    "test_viewer_encoding_and_index_logic",
    "test_viewer_window_big_file",
    "test_window_add_tool_from_menu",
    "test_window_bs_at_drive_root",
    "test_window_copy_tree",
    "test_window_everything_search",
    "test_window_ext_colors_and_settings_dialog",
    "test_window_ext_menu",
    "test_window_ext_menu_property_and_archive",
    "test_window_f7_jump_range",
    "test_window_flat_view",
    "test_window_frecency_jump",
    "test_window_history_view",
    "test_window_layout_and_font",
    "test_window_live_filter",
    "test_window_menu_attrs_mouse_drag_drawing",
    "test_window_renemo_block_and_gutter",
    "test_window_renemo_ext_fullpath_checks",
    "test_window_renemo_format_dialog",
    "test_window_renemo_maximize_and_drag_scroll",
    "test_window_renemo_target_line",
    "test_window_renemo_toolbar_and_keys",
    "test_window_screen_layout",
    "test_window_title_shows_version",
    "test_window_transfer_com_init",
    "test_window_update_restart",
    "test_window_vertical_scrollbar",
    "test_window_winmerge",
    "test_window_workspace",
    "test_window_zip_create",
})


def test_update_zip_folder_setting():
    """Shift+F5 の ZIP 確認フォルダ: 未設定なら Downloads、保存すると filer の他のキーを残して差し替わる。"""
    import types
    import filer
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "settings.json")
        path.write_text('{"filer": {"left_path": "x"}, "other": 1}', encoding="utf-8")
        logs = []
        obj = types.SimpleNamespace(app_settings={"filer": {}}, settings_path=str(path),
                                    log=logs.append)
        assert filer.FilerWindow.update_zip_folder(obj) == Path.home() / "Downloads"
        obj.app_settings["filer"]["update_zip_folder"] = "   "   # 空白だけは未設定扱い
        assert filer.FilerWindow.update_zip_folder(obj) == Path.home() / "Downloads"
        target = Path(tmp, "dl")
        filer.FilerWindow.save_update_zip_folder(obj, target)
        assert filer.FilerWindow.update_zip_folder(obj) == target
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored == {"filer": {"left_path": "x", "update_zip_folder": str(target)},
                          "other": 1}, stored
        assert logs == []


def main() -> int:
    full = "--full" in sys.argv or os.environ.get("FILER_FULL") \
        or os.environ.get("GITHUB_ACTIONS")
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    if not full:
        skipped = [name for name, _fn in tests if name in FULL_ONLY]
        tests = [(name, fn) for name, fn in tests if name not in FULL_ONLY]
        print(f"（重い窓テスト {len(skipped)} 件は省略しました。"
              "全部回すときは --full を付けてください）")
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
            if os.environ.get("GITHUB_ACTIONS"):
                # CI では失敗内容を run ページの注釈に出す(ログを開かずに読めるように)
                tb = traceback.format_exc().replace("%", "%25").replace("\r", "%0D")
                print(f"::error title={name}::" + tb[-900:].replace("\n", "%0A"))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice::{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        # CI の pwsh は、テスト本体が標準エラーへ書いた分(picker の警告など)でも
        # ステップを失敗にしてしまう(テストが全部通ってもジョブが赤くなる)。
        # CI のときだけ標準エラーを標準出力へ寄せる(fd ごと寄せるので、
        # Qt など C 側の警告も一緒に流れる)。
        try:
            os.dup2(1, 2)
            sys.stderr = sys.stdout
        except Exception:
            pass
    code = main()
    # os._exit は後始末を走らせないので、テスト用の一時フォルダはここで消す。
    shutil.rmtree(_TEST_TEMP, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # os._exit でも Windows では DLL の切り離し処理が走り、offscreen のクリップボードが
    # 抱えた QMimeData を Qt の静的デストラクタが解放しようとして 0xC0000005 で落ちる
    # (README「検証時の注意」。CI では全部通ったあとにこれで失敗扱いになっていた)。
    # TerminateProcess は DLL の切り離しを走らせないので、終了コードがそのまま残る。
    try:
        import ctypes
        _k32 = ctypes.WinDLL("kernel32")
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        _k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _k32.TerminateProcess.restype = ctypes.c_int
        _k32.TerminateProcess(_k32.GetCurrentProcess(), code)
    except Exception:  # noqa: BLE001  Windows 以外や失敗時は従来どおり
        pass
    os._exit(code)
