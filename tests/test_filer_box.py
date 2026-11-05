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

TOKEN = "abcdefghij1234567890"          # 作り物の鍵
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
    for i, (lid, typ, parent, name, box_id, box_type) in enumerate(rows):
        conn.execute("INSERT INTO local_item VALUES (?,?,?,?)", (lid, typ, parent, name))
        conn.execute("INSERT INTO item_mapping_between_fs VALUES (?,?,?,?)",
                     (i, lid, box_id, box_type))
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


def test_parse_shared_link():
    ok = fb.parse_shared_link
    assert ok(f"https://app.box.com/s/{TOKEN}") == ("app.box.com", TOKEN)
    assert ok(f"https://app.box.com/s/{TOKEN}/") == ("app.box.com", TOKEN)
    assert ok(f"  https://app.box.com/s/{TOKEN}?x=1#y ") == ("app.box.com", TOKEN)
    assert ok(f"https://app.box.com/s/{TOKEN}/folder/12345") == ("app.box.com", TOKEN)
    assert ok(f"https://acme.app.box.com/s/{TOKEN}") == ("acme.app.box.com", TOKEN)
    assert ok(f"https://ACME.app.box.com/s/{TOKEN}") == ("acme.app.box.com", TOKEN)
    for bad in ("", "abc", None, 5,
                f"http://app.box.com/s/{TOKEN}",              # https のみ
                f"https://example.com/s/{TOKEN}",             # box.com 以外
                f"https://evilbox.com/s/{TOKEN}",
                f"https://app.box.com.evil.com/s/{TOKEN}",
                "https://app.box.com/s/",                     # 鍵が無い
                "https://app.box.com/s/short",                # 短すぎる
                "https://app.box.com/folder/12345",
                f"https://app.box.com/file/{TOKEN}"):
        assert ok(bad) is None, bad


def test_build_share_text_single_file_and_folder():
    with Env() as env:
        links = [fb.make_link("9100", "Box/ProjA", "app.box.com", TOKEN)]
        f = item(env, "ProjA", "資料", "見積書.xlsx")
        assert fb.build_share_text([f], links) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/s/{TOKEN}/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx")
        d = item(env, "ProjA", "資料")
        assert fb.build_share_text([d], links) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/s/{TOKEN}/folder/9101")
        # 案件フォルダ自身
        assert fb.build_share_text([item(env, "ProjA")], links) == (
            "/Box/ProjA/\r\n" f"{IN}https://app.box.com/s/{TOKEN}/folder/9100")


def test_build_share_text_multiple():
    with Env() as env:
        links = [fb.make_link("9100", "Box/ProjA", "acme.app.box.com", TOKEN)]
        a = item(env, "ProjA", "資料", "見積書.xlsx")
        b = item(env, "ProjA", "資料", "memo.txt")
        c = item(env, "ProjA", "top_a.txt")
        # 同じフォルダの複数ファイルは1つの見出しにまとまる
        assert fb.build_share_text([a, b], links) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n{IN}{IN}memo.txt")
        # 別フォルダは初めて出た順に、空行で区切る。間に別フォルダが挟まっても束ねる
        assert fb.build_share_text([a, c, b], links) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n{IN}{IN}memo.txt\r\n\r\n"
            "/Box/ProjA/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9100\r\n"
            f"{IN}{IN}top_a.txt")
        # フォルダ対象は1つずつ自分の塊(ファイルの塊とも、ほかのフォルダとも空行で分ける)
        d1, d2 = item(env, "ProjA", "資料"), item(env, "ProjA")
        assert fb.build_share_text([a, d1, d2], links) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx\r\n\r\n"
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9101\r\n\r\n"
            "/Box/ProjA/\r\n"
            f"{IN}https://acme.app.box.com/s/{TOKEN}/folder/9100")
        assert fb.build_share_text([], links) == ""


def test_build_share_text_unregistered_plain():
    with Env() as env:
        a = item(env, "ProjA", "資料", "見積書.xlsx")
        assert fb.build_share_text([a], []) == (
            "/Box/ProjA/資料/\r\n"
            f"{IN}https://app.box.com/folder/9101\r\n"
            f"{IN}{IN}見積書.xlsx")
        # 登録が別の案件にあるだけなら通常の形のまま
        other = [fb.make_link("9110", "Box/ProjB", "app.box.com", TOKEN)]
        assert "/s/" not in fb.build_share_text([a], other)
        # 根直下のファイルは根(ID 0)の見出し
        assert fb.build_share_text([item(env, "top.txt")], other) == (
            "/Box/\r\n" f"{IN}https://app.box.com/folder/0\r\n" f"{IN}{IN}top.txt")
        # 壊れた登録は無視する
        assert "/s/" not in fb.build_share_text([a], [{"folder_id": "9100"}, "x", None])


def test_find_link_nearest_by_id():
    with Env() as env:
        far = fb.make_link("9100", "Box/ProjA", "app.box.com", "farfarfarfar")
        near = fb.make_link("9101", "Box/ProjA/資料", "app.box.com", "nearnearnear")
        f = item(env, "ProjA", "資料", "見積書.xlsx")
        assert fb.find_link(f, [far, near])["token"] == "nearnearnear"
        assert fb.find_link(f, [far])["token"] == "farfarfarfar"
        # フォルダ自身の登録も効く。登録は子孫にだけ効き、兄弟・親には効かない
        assert fb.find_link(item(env, "ProjA", "資料"), [near])["token"] == "nearnearnear"
        assert fb.find_link(item(env, "ProjA"), [near]) is None
        assert fb.find_link(item(env, "ProjA", "top_a.txt"), [near]) is None
        assert fb.find_link(item(env, "ProjB", "b.txt"), [far, near]) is None
        # 登録に残っている path が古くても(改名後でも)ID で当たる
        stale = fb.make_link("9100", "Box/旧名", "app.box.com", "stalestalestale")
        assert fb.find_link(f, [stale])["token"] == "stalestalestale"
        # 同じフォルダを登録し直すと置き換わる
        new = fb.make_link("9101", "Box/ProjA/資料", "h.box.com", "newnewnewnew")
        links = fb.with_link([far, near], new)
        assert [l["folder_id"] for l in links] == ["9100", "9101"]
        assert links[1]["token"] == "newnewnewnew"


def test_candidate_folders():
    with Env() as env:
        f = item(env, "ProjA", "資料", "見積書.xlsx")
        assert fb.candidate_folders(f) == [("Box/ProjA", "9100", "Box/ProjA"),
                                           ("Box/ProjA/資料", "9101", "Box/ProjA/資料")]
        assert [c[0] for c in fb.candidate_folders(item(env, "ProjA", "資料"))] == [
            "Box/ProjA", "Box/ProjA/資料"]
        assert fb.candidate_folders(item(env, "top.txt")) == []


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
