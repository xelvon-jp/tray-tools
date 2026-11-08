"""統合第2段: 通知の子プロセス（win11toast をやめ、標準ライブラリのバルーンへ）。"""

import json

from landrop import toast as t


def test_click_target_file_uri_becomes_path():
    target = t.click_target("file:///C:/Users/x/Downloads/LanDrop/2026-11-08")
    assert target.replace("\\", "/").endswith("C:/Users/x/Downloads/LanDrop/2026-11-08")


def test_click_target_url_and_plain_path_pass_through():
    assert t.click_target("https://example.com/page") == "https://example.com/page"
    assert t.click_target("C:/Users/x/file.txt") == "C:/Users/x/file.txt"


def test_click_target_none_and_empty():
    assert t.click_target(None) is None
    assert t.click_target("") is None


def test_main_routes_payload_to_balloon(monkeypatch):
    got = {}
    monkeypatch.setattr(
        t, "_show_balloon",
        lambda title, body, on_click: got.update(title=title, body=body, on_click=on_click),
    )
    # buttons / xml は旧互換の引数。受け取って無視できること
    t._main(["x", json.dumps({"title": "a", "body": "b", "on_click": None,
                              "buttons": [{"content": "開く"}], "xml": "<toast/>"})])
    assert got == {"title": "a", "body": "b", "on_click": None}
