# test_filer_weblinks.py
# filer_weblinks(同期フォルダ→Web の URL の割り当て)のテスト。Qt 不要。
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import filer_weblinks as wl

BASES = wl.normalize_web_link_bases([
    {"name": "SharePoint", "root": r"C:\Users\me\SharePoint\docs",
     "url_base": "https://example.sharepoint.com/sites/docs/"},
    {"name": "OneDrive", "root": r"C:\Users\me\OneDrive",
     "url_base": "https://example-my.sharepoint.com/personal/me/Documents"},
    {"name": "壊れ", "root": "", "url_base": "https://x.example/"},
    {"name": "壊れ2", "root": r"C:\x", "url_base": "ftp://x.example/"},
])


def test_normalize():
    assert [b["name"] for b in BASES] == ["SharePoint", "OneDrive"]
    assert BASES[0]["url_base"].endswith("/docs")   # 末尾の / は落ちる
    assert wl.normalize_web_link_bases(None) == []
    assert wl.normalize_web_link_bases([{"root": 1}]) == []


def test_web_link_for():
    name, url = wl.web_link_for(r"C:\Users\me\SharePoint\docs\a b.txt", BASES)
    assert name == "SharePoint"
    assert url == "https://example.sharepoint.com/sites/docs/a%20b.txt"
    # 日本語の区切りごとにエンコード
    _name, url = wl.web_link_for(r"C:\Users\me\OneDrive\議事録\2026 年.docx", BASES)
    assert url.startswith(
        "https://example-my.sharepoint.com/personal/me/Documents/")
    assert "%E8%AD%B0%E4%BA%8B%E9%8C%B2" in url and "\\" not in url
    # root そのもの
    assert wl.web_link_for(r"C:\Users\me\OneDrive", BASES)[1].endswith("/Documents")
    # 大文字小文字が違っても当てる(URL 側の綴りは元のまま)
    found = wl.web_link_for(r"c:\users\me\onedrive\File.TXT", BASES)
    assert found is not None and found[1].endswith("/File.TXT")
    # 外のパス・無関係な前方一致(境界でない)は外す
    assert wl.web_link_for(r"C:\Users\me\OneDrive2\x.txt", BASES) is None
    assert wl.web_link_for(r"D:\elsewhere\x.txt", BASES) is None
    assert wl.web_link_for("", BASES) is None
    assert wl.web_link_for(r"C:\x", []) is None


def test_longest_root_wins():
    bases = wl.normalize_web_link_bases([
        {"name": "浅い", "root": r"C:\sync", "url_base": "https://a.example/"},
        {"name": "深い", "root": r"C:\sync\team", "url_base": "https://b.example/"},
    ])
    assert wl.web_link_for(r"C:\sync\team\f.txt", bases)[0] == "深い"
    assert wl.web_link_for(r"C:\sync\other\f.txt", bases)[0] == "浅い"


ALL_TESTS = [test_normalize, test_web_link_for, test_longest_root_wins]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
