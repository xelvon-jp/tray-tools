# tests/test_filer_box.py
# あっふの C→L「Box の共有用テキスト」(filer_box.py)を確かめる。Qt は使わない。
# 本物の Box Drive には触れず、手で作った sync.db(必要な2表だけ)と一時フォルダを使う。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_filer_box.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_claude_status.py と同じ形)。
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import filer_box as fb  # noqa: E402

IN = "\u3000"


def make_db(path):
    """根(0) / ProjA(100) / 資料(101) / 見積書.xlsx(102) / memo.txt(103) / top_a.txt(104) /
    ProjB(110) / b.txt(111) / 根直下の top.txt(120)。Box ID は 9100 台にして local_id と取り違えない。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE local_item (local_id INTEGER NOT NULL, native_item_type INTEGER,"
                 " parent_item_id INTEGER, name VARCHAR COLLATE NOCASE, PRIMARY KEY (local_id))")
    conn.execute("CREATE TABLE item_mapping_between_fs (id INTEGER, local_id INTEGER,"
                 " box_id VARCHAR, box_item_type INTEGER, PRIMARY KEY (id), UNIQUE (local_id))")
    rows = [
        (0, 1, None, "", "0", 1),
        (100, 1, 0, "ProjA", "9100", 1),
        (101, 1, 100, "資料", "9101", 1),
        (102, 0, 101, "見積書.xlsx", "9102", 0),
        (103, 0, 101, "memo.txt", "9103", 0),
        (104, 0, 100, "top_a.txt", "9104", 0),
        (110, 1, 0, "ProjB", "9110", 1),
        (111, 0, 110, "b.txt", "9111", 0),
        (120, 0, 0, "top.txt", "9120", 0),
    ]
    conn.execute("CREATE TABLE box_item (box_id VARCHAR, item_type INTEGER,"
                 " parent_item_id VARCHAR, name VARCHAR COLLATE NOCASE)")
    box_of = {lid: box_id for lid, _t, _p, _n, box_id, _bt in rows}
    for i, (lid, typ, parent, name, box_id, box_type) in enumerate(rows):
        conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (lid, typ, parent, name))
        conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                     (i, lid, box_id, box_type))
        conn.execute("INSERT INTO box_item VALUES (?,?,?,?)",
                     (box_id, box_type, None if parent is None else box_of[parent], name))
    conn.commit()
    conn.close()


class Env:
    """一時フォルダに偽の Box 根と sync.db を作り、モジュールをそこへ向ける。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "home" / "Box"
        self.root.mkdir(parents=True)
        self.db = base / "local" / "sync.db"
        self.db.parent.mkdir()
        make_db(str(self.db))
        self.cache = str(base / "cache")
        fb.ROOT_OVERRIDE, fb.DB_OVERRIDE = str(self.root), str(self.db)
        fb.discard_cache()
        return self

    def __exit__(self, *exc):
        fb.discard_cache()
        fb.ROOT_OVERRIDE = fb.DB_OVERRIDE = None
        self.tmp.cleanup()

    def p(self, *parts):
        return str(self.root.joinpath(*parts))


def item(env, *parts):
    return fb.resolve_item(env.p(*parts), cache_dir=env.cache)


def test_available_and_inside():
    with Env() as env:
        assert fb.available()
        assert fb.relative_parts(env.p("ProjA", "資料")) == ("ProjA", "資料")
        assert fb.relative_parts(str(env.root)) is None           # 根自身は対象外
        assert fb.relative_parts(str(env.root) + "X\\a") is None  # 前方一致だけの別フォルダ
        assert fb.all_inside([env.p("a"), env.p("b", "c")])
        assert not fb.all_inside([env.p("a"), "C:\\other\\x"])
        assert not fb.all_inside([])
        os.remove(env.db)
        assert not fb.available()


def test_resolve_file_folder_root_missing():
    with Env() as env:
        got = fb.resolve(env.p("ProjA", "資料", "見積書.xlsx"), cache_dir=env.cache)
        assert got == ("9102", "file", ("ProjA", "資料", "見積書.xlsx"))
        got = fb.resolve(env.p("ProjA", "資料"), cache_dir=env.cache)
        assert got == ("9101", "folder", ("ProjA", "資料"))
        # 大文字小文字の違いは DB の綴りに直る
        it = item(env, "proja", "資料")
        assert it.rel_parts == ("ProjA", "資料") and it.ids == ("9100", "9101")
        # 根直下のファイル
        assert fb.resolve(env.p("top.txt"), cache_dir=env.cache)[:2] == ("9120", "file")
        # 見つからない
        try:
            fb.resolve(env.p("ProjA", "未アップロード.txt"), cache_dir=env.cache)
            raise AssertionError("NotSynced が出るはず")
        except fb.NotSynced as e:
            assert str(e) == "未アップロード.txt"
        # 根そのものは対象外
        try:
            fb.resolve(str(env.root), cache_dir=env.cache)
            raise AssertionError("NotSynced が出るはず")
        except fb.NotSynced:
            pass
        items, missing = fb.resolve_many(
            [env.p("ProjA", "資料", "memo.txt"), env.p("ProjA", "nope")], env.cache)
        assert [i.name for i in items] == ["memo.txt"] and missing == ["nope"]


def test_broken_db():
    with Env() as env:
        env.db.write_bytes(b"not a database" * 100)
        fb.discard_cache()
        try:
            fb.resolve(env.p("ProjA"), cache_dir=env.cache)
            raise AssertionError("BoxDbError が出るはず")
        except fb.BoxDbError:
            pass


def test_build_share_text_single_file_and_folder():
    with Env() as env:
        f = item(env, "ProjA", "資料", "見積書.xlsx")
        assert fb.build_share_text([f]) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx")
        d = item(env, "ProjA", "資料")
        assert fb.build_share_text([d]) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101")
        # 案件フォルダ自身
        assert fb.build_share_text([item(env, "ProjA")]) == (
            "/Box/ProjA/\r\n" f"{IN}https://app.box.com/folder/9100")
        # 共有リンク(/s/<鍵>)の形は出さない
        assert "/s/" not in fb.build_share_text([f, d])
        assert fb.folder_url("9101") == "https://app.box.com/folder/9101"


def test_build_share_text_multiple():
    with Env() as env:
        a = item(env, "ProjA", "資料", "見積書.xlsx")
        b = item(env, "ProjA", "資料", "memo.txt")
        c = item(env, "ProjA", "top_a.txt")
        # 同じフォルダの複数ファイルは1つの見出しにまとまる
        assert fb.build_share_text([a, b]) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n{IN}{IN}memo.txt")
        # 別フォルダは初めて出た順に、空行で区切る。間に別フォルダが挟まっても束ねる
        assert fb.build_share_text([a, c, b]) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n{IN}{IN}memo.txt\r\n\r\n"
            "/Box/ProjA/\r\n"
            f"{IN}https://app.box.com/folder/9100\r\n"
            f"{IN}{IN}top_a.txt")
        # フォルダ対象は1つずつ自分の塊(ファイルの塊とも、ほかのフォルダとも空行で分ける)
        d1, d2 = item(env, "ProjA", "資料"), item(env, "ProjA")
        assert fb.build_share_text([a, d1, d2]) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n\r\n"
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n\r\n"
            "/Box/ProjA/\r\n"
            f"{IN}https://app.box.com/folder/9100")
        assert fb.build_share_text([]) == ""


def test_build_share_text_root_file():
    with Env() as env:
        # 根直下のファイルは根(ID 0)の見出し
        assert fb.build_share_text([item(env, "top.txt")]) == (
            "/Box/\r\n" f"{IN}https://app.box.com/folder/0\r\n" f"{IN}{IN}top.txt")


def test_copy_cache_reuse_and_refresh():
    with Env() as env:
        first = fb.copy_db(env.cache)
        assert os.path.isfile(first) and first.startswith(env.cache)
        assert fb.copy_db(env.cache) == first            # 変わらなければ使い回す
        # 元が変わったら作り直す(旧コピーは消える)
        time.sleep(0.02)
        conn = sqlite3.connect(str(env.db))
        conn.execute("INSERT INTO local_item VALUES (130, 0, 101, 'new.txt')")
        conn.execute("INSERT INTO item_mapping_between_fs VALUES (99, 130, '9130', 0)")
        conn.execute("INSERT INTO box_item VALUES ('9130', 0, '9101', 'new.txt')")
        conn.commit()
        conn.close()
        second = fb.copy_db(env.cache)
        assert second != first and not os.path.exists(first)
        assert fb.resolve(env.p("ProjA", "資料", "new.txt"), cache_dir=env.cache)[0] == "9130"
        # -wal が現れたときも作り直し、-wal も写す
        Path(str(env.db) + "-wal").write_bytes(b"")
        third = fb.copy_db(env.cache)
        assert third != second and os.path.isfile(third + "-wal")
        # 捨てる
        fb.discard_cache()
        assert not os.path.exists(os.path.dirname(third))
        # 掃除などで消されても、次の呼び出しで作り直す
        assert os.path.isfile(fb.copy_db(env.cache))


def _add_rows(env, rows, box_side=True):
    """env の sync.db に行を足す。rows = [(local_id, parent, name, box_id または None, 種別)]。
    box_id が None なら対応表には入れない(Box 側と結び付いていない古い行)。"""
    conn = sqlite3.connect(str(env.db))
    n = 1000
    for lid, parent, name, box_id, box_type in rows:
        conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (lid, 1, parent, name))
        if box_id is not None:
            n += 1
            conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                         (n, lid, box_id, box_type))
            if box_side and parent is not None:
                parent_box = conn.execute(
                    "SELECT box_id FROM item_mapping_between_fs WHERE local_id=?",
                    (parent,)).fetchone()[0]
                conn.execute("INSERT INTO box_item VALUES (?,?,?,?)",
                             (box_id, box_type, parent_box, name))
    conn.commit()
    conn.close()
    fb.discard_cache()


def test_ambiguous_same_name_both_mapped():
    with Env() as env:
        # 資料(101) と同じ親・同じ名前の行がもう1つ(削除→作り直しの残りなど)
        _add_rows(env, [(201, 100, "資料", "9201", 1)])
        for path in (("ProjA", "資料"), ("ProjA", "資料", "memo.txt")):
            try:
                item(env, *path)
            except fb.Ambiguous as e:
                assert str(e) == "資料", str(e)
            else:
                raise AssertionError("決められないのに通った")
        # Ambiguous は NotSynced の一種(区別しない呼び出し側では「見つからない」扱い)
        assert issubclass(fb.Ambiguous, fb.NotSynced)
        # resolve_many: ambiguous を渡せばそちらへ、渡さなければ missing へ
        paths = [env.p("ProjA", "資料"), env.p("ProjB", "b.txt")]
        amb = []
        items, missing = fb.resolve_many(paths, env.cache, amb)
        assert [i.name for i in items] == ["b.txt"] and missing == [] and amb == ["資料"]
        items, missing = fb.resolve_many(paths, env.cache)
        assert [i.name for i in items] == ["b.txt"] and missing == ["資料"]


def test_ambiguous_case_only_difference_picks_exact():
    with Env() as env:
        # 大文字小文字だけ違う行(NOCASE なので同じ name 検索に掛かる)。綴りが一致する方を採る
        _add_rows(env, [(202, 100, "PROJC", "9202", 1), (203, 100, "projc", "9203", 1)])
        assert item(env, "ProjA", "PROJC").box_id == "9202"
        assert item(env, "ProjA", "projc").box_id == "9203"
        # どちらとも綴りが違えば決められない
        try:
            item(env, "ProjA", "ProjC")
        except fb.Ambiguous:
            pass
        else:
            raise AssertionError("綴りが合わないのに通った")


def test_duplicate_one_unmapped_picks_mapped():
    with Env() as env:
        # 同名の行が2つあっても、対応表に載っているのが1つだけならそれを使う
        _add_rows(env, [(204, 100, "資料", None, 1)])
        got = item(env, "ProjA", "資料", "memo.txt")
        assert got.ids == ("9100", "9101", "9103")
        # 対応の付いた行が1つも無ければ従来どおり NotSynced(Ambiguous ではない)
        _add_rows(env, [(205, 100, "stale", None, 1), (206, 100, "stale", None, 1)])
        try:
            item(env, "ProjA", "stale")
        except fb.Ambiguous:
            raise AssertionError("対応が無いのに Ambiguous")
        except fb.NotSynced:
            pass


def test_ambiguous_two_roots():
    with Env() as env:
        _add_rows(env, [(300, None, "", "0", 1)])
        try:
            item(env, "ProjA")
        except fb.Ambiguous:
            pass
        else:
            raise AssertionError("根が2つなのに通った")


def test_share_blocks():
    with Env() as env:
        a = item(env, "ProjA", "資料", "見積書.xlsx")
        b = item(env, "ProjA", "資料", "memo.txt")
        c = item(env, "ProjA", "top_a.txt")
        u1 = "https://app.box.com/folder/9101"
        u0 = "https://app.box.com/folder/9100"
        assert fb.share_blocks([a]) == [("/Box/ProjA/資料/", u1)]
        assert fb.share_blocks([a, c, b]) == [("/Box/ProjA/資料/", u1), ("/Box/ProjA/", u0)]
        d1, d2 = item(env, "ProjA", "資料"), item(env, "ProjA")
        assert fb.share_blocks([d1]) == [("/Box/ProjA/資料/", u1)]
        assert fb.share_blocks([a, d1, d2]) == [
            ("/Box/ProjA/資料/", u1), ("/Box/ProjA/資料/", u1), ("/Box/ProjA/", u0)]
        # 根直下のファイルは根(ID 0)
        assert fb.share_blocks([item(env, "top.txt")]) == [
            ("/Box/", "https://app.box.com/folder/0")]
        assert fb.share_blocks([]) == []
        # build_share_text と塊の数・見出し・URL が食い違わない
        for items in ([a], [a, c, b], [a, d1, d2]):
            text = fb.build_share_text(items)
            blocks = fb.share_blocks(items)
            parts = text.split("\r\n\r\n")
            assert len(parts) == len(blocks)
            for part, (shown, url) in zip(parts, blocks):
                lines = part.split("\r\n")
                assert lines[0] == shown and lines[1] == IN + url


def test_mismatch_and_box_side_checks():
    with Env() as env:
        # 一致していれば通る(各階層の ID が Box 側の木と同じ)
        assert item(env, "ProjA", "資料", "memo.txt").ids == ("9100", "9101", "9103")
    # 手元の対応が古くて別の box_id を指している → Mismatch(食い違った階層の名前を含む)
    with Env() as env:
        conn = sqlite3.connect(str(env.db))
        conn.execute("UPDATE item_mapping_between_fs SET box_id='9999' WHERE local_id=101")
        conn.commit()
        conn.close()
        fb.discard_cache()
        for path in (("ProjA", "資料", "memo.txt"), ("ProjA", "資料")):
            try:
                item(env, *path)
            except fb.Mismatch as e:
                assert e.part == "資料" and "資料" in str(e) and "9999" in str(e), str(e)
            else:
                raise AssertionError("食い違いを見逃した")
        # 祖先が食い違えば、最後の階層が一致していても拒否する(全階層を見る)
        paths = [env.p("ProjA", "資料", "memo.txt"), env.p("ProjB", "b.txt")]
        mism = []
        items, missing = fb.resolve_many(paths, env.cache, [], mism)
        assert [i.name for i in items] == ["b.txt"] and missing == [] and mism == ["memo.txt"]
        items, missing = fb.resolve_many(paths, env.cache)
        assert missing == ["memo.txt"]
    # Box 側の木に無い → 拒否
    with Env() as env:
        conn = sqlite3.connect(str(env.db))
        conn.execute("DELETE FROM box_item WHERE box_id='9103'")
        conn.commit()
        conn.close()
        fb.discard_cache()
        try:
            item(env, "ProjA", "資料", "memo.txt")
        except fb.Mismatch as e:
            assert e.part == "memo.txt"
        else:
            raise AssertionError("Box 側に無いのに通った")
    # Box 側で同じ名前が複数 → Ambiguous(綴りが完全一致する1つがあればそれを使う)
    with Env() as env:
        conn = sqlite3.connect(str(env.db))
        conn.execute("INSERT INTO box_item VALUES ('9555', 0, '9101', 'MEMO.txt')")
        conn.commit()
        conn.close()
        fb.discard_cache()
        assert item(env, "ProjA", "資料", "memo.txt").box_id == "9103"      # 綴りが一致する方
    with Env() as env:
        conn = sqlite3.connect(str(env.db))
        conn.execute("INSERT INTO box_item VALUES ('9556', 0, '9101', 'memo.txt')")
        conn.commit()
        conn.close()
        fb.discard_cache()
        try:
            item(env, "ProjA", "資料", "memo.txt")
        except fb.Ambiguous as e:
            assert not isinstance(e, fb.Mismatch)
        else:
            raise AssertionError("Box 側が曖昧なのに通った")
    # box_item の表が無い記録は読めない扱い
    with Env() as env:
        conn = sqlite3.connect(str(env.db))
        conn.execute("DROP TABLE box_item")
        conn.commit()
        conn.close()
        fb.discard_cache()
        try:
            item(env, "ProjA")
        except fb.BoxDbError:
            pass
        else:
            raise AssertionError("box_item が無いのに通った")


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
