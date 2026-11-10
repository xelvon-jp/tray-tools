# test_command_launcher.py
# 登録コマンドランチャ(command_launcher.py)の純粋な部分と、launcher.py に足した
# 拡張ブックマーク(upsert / update)のテスト。窓そのものは作らずに済む範囲を見る。
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import command_launcher as cl
import launcher


# ---------------------------------------------------------------
# 種別の判定
# ---------------------------------------------------------------
def test_classify():
    assert cl.classify("https://example.com/") == cl.KIND_URL
    assert cl.classify("http://example.com") == cl.KIND_URL
    assert cl.classify("onenote:https://example.com/page") == cl.KIND_URI
    assert cl.classify("onenote:///C:/note.one#page") == cl.KIND_URI
    assert cl.classify("mailto:a@example.com") == cl.KIND_URI
    # ドライブレターはスキーム扱いしない
    assert cl.classify(r"C:\tools\app.exe", is_dir=False) == cl.KIND_FILE
    assert cl.classify(r"C:\folder", is_dir=True) == cl.KIND_FOLDER
    assert cl.classify(r"\\server\share", is_dir=True) == cl.KIND_FOLDER
    with tempfile.TemporaryDirectory() as tmp:
        assert cl.classify(tmp) == cl.KIND_FOLDER
        file_path = os.path.join(tmp, "a.txt")
        Path(file_path).write_text("x", encoding="utf-8")
        assert cl.classify(file_path) == cl.KIND_FILE


def test_build_entries():
    with tempfile.TemporaryDirectory() as tmp:
        settings = {"launcher": {"bookmarks": [
            {"name": "作業", "path": tmp},
            {"name": "メモ", "path": "onenote:https://example.com/p"},
            {"name": "道具", "path": r"C:\tools\app.exe",
             "params": "/open %param%", "dir": r"C:\tools"},
            {"name": "壊れた行"},  # path が無い行は load_bookmarks が捨てる
        ]}}
        entries = cl.build_entries(settings)
        assert [e.name for e in entries] == ["作業", "メモ", "道具"]
        assert entries[0].kind == cl.KIND_FOLDER
        assert entries[1].kind == cl.KIND_URI
        assert entries[2].kind == cl.KIND_FILE
        assert entries[2].params == "/open %param%"
        assert entries[2].workdir == r"C:\tools"


# ---------------------------------------------------------------
# 補完まわり
# ---------------------------------------------------------------
def _entries():
    return [
        cl.Entry("Google", "https://example.com", kind=cl.KIND_URL),
        cl.Entry("Goo辞書", "https://example.com/d", kind=cl.KIND_URL),
        cl.Entry("仕事フォルダ", r"C:\work", kind=cl.KIND_FOLDER),
    ]


def test_prefix_candidates():
    entries = _entries()
    assert [e.name for e in cl.prefix_candidates(entries, "go")] == ["Google", "Goo辞書"]
    assert [e.name for e in cl.prefix_candidates(entries, "GOO")] == ["Google", "Goo辞書"]
    assert [e.name for e in cl.prefix_candidates(entries, "仕")] == ["仕事フォルダ"]
    assert cl.prefix_candidates(entries, "") == entries
    assert cl.prefix_candidates(entries, "x") == []


def test_common_prefix():
    assert cl.common_prefix(["Google", "Goo辞書"]) == "Goo"
    assert cl.common_prefix(["abc", "abd", "abe"]) == "ab"
    assert cl.common_prefix(["仕事A", "仕事B"]) == "仕事"
    assert cl.common_prefix(["単独"]) == "単独"
    assert cl.common_prefix([]) == ""


def test_parse_input():
    assert cl.parse_input("Google;keyhac") == ("Google", "keyhac")
    assert cl.parse_input("Google") == ("Google", "")
    assert cl.parse_input("名前 ; 引数;追加") == ("名前", " 引数;追加")
    assert cl.name_part("Google;abc") == "Google"


# ---------------------------------------------------------------
# 置換と実行コマンド
# ---------------------------------------------------------------
def test_expand():
    env = {"HOME": r"C:\Users\me", "DOCS": r"D:\docs"}
    assert cl.expand("%param%", "abc", env) == "abc"
    assert cl.expand("%HOME%", "", env) == r"C:\Users\me"
    assert cl.expand("%env[DOCS]%", "", env) == r"D:\docs"
    assert cl.expand("100%%", "", env) == "100%"
    assert cl.expand("%UNKNOWN%", "", env) == "%UNKNOWN%"
    assert cl.expand(r"%HOME%\tool %param%", "x y", env) == r"C:\Users\me\tool x y"
    # %% で守った中身は環境変数として展開しない
    assert cl.expand("%%HOME%%", "", env) == "%HOME%"


def test_build_command():
    folder = cl.Entry("仕事", r"C:\work", kind=cl.KIND_FOLDER)
    assert cl.build_command(folder) == ("jump", r"C:\work")
    url = cl.Entry("例", "https://example.com", kind=cl.KIND_URL)
    assert cl.build_command(url) == ("open", "https://example.com")
    note = cl.Entry("頁", "onenote:https://example.com/p", kind=cl.KIND_URI)
    assert cl.build_command(note) == ("open", "onenote:https://example.com/p")
    builtin = cl.Entry("Reload", "reload", kind=cl.KIND_BUILTIN)
    assert cl.build_command(builtin) == ("builtin", "reload")

    tool = cl.Entry("道具", r"C:\my tools\app.exe",
                    params="/open %param%", workdir=r"C:\my tools")
    action, (command, workdir) = cl.build_command(tool, "a b.txt")
    assert action == "exec"
    assert command == r'"C:\my tools\app.exe" /open a b.txt'
    assert workdir is None  # この環境には無いフォルダなので落とす

    # %param% の置き場が無いパラメタ無し登録では、引数を後ろに足す
    plain = cl.Entry("素", r"C:\tools\app.exe", kind=cl.KIND_FILE)
    action, (command, workdir) = cl.build_command(plain, "file.txt")
    assert command == r"C:\tools\app.exe file.txt"
    assert workdir is None

    with tempfile.TemporaryDirectory() as tmp:
        tool2 = cl.Entry("道具2", "app.exe", workdir=tmp, kind=cl.KIND_FILE)
        action, (command, workdir) = cl.build_command(tool2)
        assert workdir == tmp


# ---------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------
def test_history_record_and_forget():
    history = cl.record_history([], "Google;abc")
    history = cl.record_history(history, "仕事")
    assert history == ["仕事", "Google;abc"]
    # 重複は先頭へ寄せ直す(大文字小文字を無視)
    history = cl.record_history(history, "google;ABC")
    assert history == ["google;ABC", "仕事"]
    history = cl.forget_history(history, "GOOGLE;abc")
    assert history == ["仕事"]
    many = cl.record_history([], "x", limit=3)
    for i in range(5):
        many = cl.record_history(many, f"cmd{i}", limit=3)
    assert len(many) == 3 and many[0] == "cmd4"


def test_history_save_load():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hist.json")
        cl.save_history(["a;b", "仕事"], path)
        assert cl.load_history(path) == ["a;b", "仕事"]
        assert cl.load_history(os.path.join(tmp, "none.json")) == []


# ---------------------------------------------------------------
# launcher.py の拡張ブックマーク保存
# ---------------------------------------------------------------
def _settings_file(tmp, bookmarks):
    path = os.path.join(tmp, "settings.json")
    data = {"launcher": {"bookmarks": bookmarks}, "other": {"keep": 1}}
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_upsert_bookmark():
    with tempfile.TemporaryDirectory() as tmp:
        path = _settings_file(tmp, [{"name": "既存", "path": r"C:\a"}])
        app_settings = {"launcher": {"bookmarks": [{"name": "既存", "path": r"C:\a"}]}}
        entry = {"name": "新規", "path": r"C:\b", "params": "%param%"}
        assert launcher.upsert_bookmark(app_settings, path, entry)
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert [b["name"] for b in stored["launcher"]["bookmarks"]] == ["既存", "新規"]
        assert stored["launcher"]["bookmarks"][1]["params"] == "%param%"
        assert stored["other"] == {"keep": 1}  # 関係ないキーはそのまま
        # 同じ (name, path) は、位置を保って中身だけ差し替える
        entry2 = {"name": "新規", "path": r"C:\b", "params": "/x"}
        assert launcher.upsert_bookmark(app_settings, path, entry2)
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert len(stored["launcher"]["bookmarks"]) == 2
        assert stored["launcher"]["bookmarks"][1]["params"] == "/x"
        assert app_settings["launcher"]["bookmarks"][1]["params"] == "/x"


def test_update_bookmark():
    with tempfile.TemporaryDirectory() as tmp:
        path = _settings_file(tmp, [{"name": "旧名", "path": r"C:\a"}])
        app_settings = {"launcher": {"bookmarks": [{"name": "旧名", "path": r"C:\a"}]}}
        entry = {"name": "新名", "path": "https://example.com/"}
        assert launcher.update_bookmark(app_settings, path, "旧名", r"C:\a", entry)
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert stored["launcher"]["bookmarks"] == [entry]
        assert app_settings["launcher"]["bookmarks"] == [entry]
        # 対象が無ければ何もしない
        assert not launcher.update_bookmark(
            app_settings, path, "無い", r"C:\zzz", entry)
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert stored["launcher"]["bookmarks"] == [entry]


# ---------------------------------------------------------------
# 窓の補完の動き(offscreen で窓を作って確かめる)
# ---------------------------------------------------------------
def test_window_completion_and_enter():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    del app
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "会議室")
        os.mkdir(sub)
        entries = [
            cl.Entry("Google", "https://example.com", kind=cl.KIND_URL),
            cl.Entry("Goo辞書", "https://example.com/d", kind=cl.KIND_URL),
            cl.Entry("仕事フォルダ", tmp, kind=cl.KIND_FOLDER),
        ]
        chosen = []
        window = cl.create_window(entries, [])
        window.chosen.connect(lambda e, p, r: chosen.append((e, p, r)))

        # 打つと先頭候補が補完され、尻尾が選択状態になる
        window._on_text_edited("Go")
        assert window._input.text() == "Google"
        assert window._input.selectionStart() == 2
        # Space の巡回で次の候補へ
        assert window._cycle_candidate(1)
        assert window._input.text() == "Goo辞書"
        # Esc 相当(文字クリア)を挟んで、; の引数つきで実行できる
        window._on_escape()
        window._input.setText("Google;keyhac")
        window._on_enter()
        assert chosen and chosen[-1][0].name == "Google"
        assert chosen[-1][1] == "keyhac"

        # フォルダ名の確定後の Tab は掘り下げに入る
        window2 = cl.create_window(entries, [])
        window2._on_text_edited("仕")
        assert window2._input.text() == "仕事フォルダ"
        assert window2._tab()
        assert window2._mode == "path"
        assert window2._path_base == tmp
        picked = []
        window2.chosen.connect(lambda e, p, r: picked.append(e))
        window2._on_text_edited("会")
        window2._on_enter()
        assert picked and picked[-1].target == sub
        window2.close()
        window.close()


ALL_TESTS = [test_classify, test_build_entries, test_prefix_candidates,
             test_common_prefix, test_parse_input, test_expand,
             test_build_command, test_history_record_and_forget,
             test_history_save_load, test_upsert_bookmark, test_update_bookmark,
             test_window_completion_and_enter]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
