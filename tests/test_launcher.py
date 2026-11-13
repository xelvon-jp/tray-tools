# test_launcher.py
# フォルダブックマーク(launcher.py)の URL 対応と、ブラウザのお気に入りの
# 取り込みのテスト。ピッカーの窓は作らず、純粋な部分だけを見る。
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import launcher


def test_is_url():
    assert launcher.is_url("https://example.com/")
    assert launcher.is_url("http://example.com")
    assert launcher.is_url("HTTPS://EXAMPLE.COM")
    assert not launcher.is_url(r"C:\folder")
    assert not launcher.is_url("https:example")
    assert not launcher.is_url("")
    assert not launcher.is_url(None)


def test_jump_url_opens_browser():
    opened = []
    with mock.patch.object(os, "startfile", opened.append, create=True):
        assert launcher.jump("https://example.com/", target=launcher.TARGET_AFXW)
    assert opened == ["https://example.com/"]


def test_tab_text_url_is_none():
    assert launcher.tab_text("x", (launcher.KIND_JUMP, "https://example.com/")) is None
    assert launcher.tab_text("x", (launcher.KIND_JUMP, r"C:\a")) == "C:\\a\\"


SAMPLE = {
    "roots": {
        "bookmark_bar": {
            "type": "folder",
            "children": [
                {"type": "url", "name": "例", "url": "https://example.com/"},
                {"type": "folder", "name": "仕事", "children": [
                    {"type": "url", "name": "社内", "url": "https://intra.example.jp/"},
                    {"type": "url", "name": "", "url": "https://noname.example.jp/"},
                ]},
                {"type": "url", "name": "変なURL", "url": "ftp://example.com/"},
            ],
        },
        "other": {"type": "folder", "children": [
            {"type": "url", "name": "他", "url": "https://other.example.com/"},
        ]},
    }
}


def test_parse_browser_bookmarks():
    found = launcher.parse_browser_bookmarks(SAMPLE)
    assert [e["path"] for e in found] == [
        "https://example.com/",
        "https://intra.example.jp/",
        "https://noname.example.jp/",       # 名前が無ければ URL が名前
        "https://other.example.com/",
    ], found
    assert launcher.parse_browser_bookmarks({}) == []
    assert launcher.parse_browser_bookmarks(None) == []


def test_import_browser_bookmarks():
    with tempfile.TemporaryDirectory() as tmp:
        bookmarks_file = Path(tmp, "Bookmarks")
        bookmarks_file.write_text(json.dumps(SAMPLE), encoding="utf-8")
        settings_path = Path(tmp, "settings.json")
        app_settings = {"launcher": {"bookmarks": [
            {"name": "既存", "path": "https://example.com"},   # 末尾 / の差は同一扱い
            {"name": "フォルダ", "path": r"C:\keep"},
        ]}}
        # 実運用では登録済みの分はファイル側にもある(保存はファイルが元)
        settings_path.write_text(json.dumps(app_settings), encoding="utf-8")
        added, discovered = launcher.import_browser_bookmarks(
            app_settings, str(settings_path), files=[str(bookmarks_file)])
        assert discovered == 4
        assert [e["path"] for e in added] == [
            "https://intra.example.jp/",
            "https://noname.example.jp/",
            "https://other.example.com/",
        ], added
        stored = json.loads(settings_path.read_text(encoding="utf-8"))
        paths = [e["path"] for e in stored["launcher"]["bookmarks"]]
        assert r"C:\keep" in paths and len(paths) == 2 + 3
        # 2回目は新しいものが無い
        added2, _ = launcher.import_browser_bookmarks(
            app_settings, str(settings_path), files=[str(bookmarks_file)])
        assert added2 == []
        # 壊れたファイルは飛ばす
        broken = Path(tmp, "Broken")
        broken.write_text("{oops", encoding="utf-8")
        added3, discovered3 = launcher.import_browser_bookmarks(
            {"launcher": {"bookmarks": []}}, str(settings_path),
            files=[str(broken)])
        assert added3 == [] and discovered3 == 0


def test_is_path_prefix():
    # パスの打ちかけとして受け付ける形(候補0件でも巻き戻さない)
    assert launcher.is_path_prefix("C:")
    assert launcher.is_path_prefix("c:")
    assert launcher.is_path_prefix("\\")
    assert launcher.is_path_prefix(" C: ")   # 前後の空白は除いて見る
    # それ以外は受け付けない
    for text in ("", "Ch", "C:\\", ":", "CC:", "1:", "あ"):
        assert not launcher.is_path_prefix(text), text
    # 1文字は、その文字のドライブが実在するときだけ打ちかけとして扱う
    # (Q: の最初の Q が巻き戻されると、Q: まで到達できないため)
    from unittest import mock

    with mock.patch("os.path.exists", return_value=True):
        assert launcher.is_path_prefix("Q")
    with mock.patch("os.path.exists", return_value=False):
        assert not launcher.is_path_prefix("Q")


ALL_TESTS = [test_is_url, test_is_path_prefix, test_jump_url_opens_browser, test_tab_text_url_is_none,
             test_parse_browser_bookmarks, test_import_browser_bookmarks]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
