import pytest
from fastapi.testclient import TestClient

import landrop.clipboard
from landrop.config import Settings
from landrop.server import create_app
from landrop.texts import ReceivedText, TextHub, TextToClipboard

TOKEN = "test-token"
H = {"X-LanDrop-Token": TOKEN}


@pytest.fixture
def hub():
    h = TextHub()
    h.got = []
    h.listeners.append(h.got.append)
    return h


def client(tmp_path, hub, **kw):
    return TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path, **kw), texts=hub))


def test_post_text_json(tmp_path, hub):
    r = client(tmp_path, hub).post("/api/text", headers=H, json={"text": "こんにちは"})
    assert r.status_code == 200
    assert [t.text for t in hub.got] == ["こんにちは"]
    assert hub.got[0].client == "testclient"


def test_post_text_plain_body(tmp_path, hub):
    h = {**H, "Content-Type": "text/plain; charset=utf-8"}
    client(tmp_path, hub).post("/api/text", headers=h, content="https://example.com/a".encode())
    assert hub.got[0].url == "https://example.com/a"


@pytest.mark.parametrize("kwargs", [{"json": {"text": "  "}}, {"json": {"x": 1}}, {"json": [1]}, {"content": b""}])
def test_post_text_rejects_empty_or_bad(tmp_path, hub, kwargs):
    assert client(tmp_path, hub).post("/api/text", headers=H, **kwargs).status_code == 400
    assert hub.got == []


def test_post_text_requires_token(tmp_path, hub):
    assert client(tmp_path, hub).post("/api/text", json={"text": "x"}).status_code == 401


def test_text_does_not_start_receive_session(tmp_path, hub):
    c = client(tmp_path, hub, date_folders=True)
    c.post("/api/text", headers=H, json={"text": "x"})
    assert c.app.state.sessions.current_batch_id == ""


def test_get_pc_text(tmp_path, hub, monkeypatch):
    monkeypatch.setattr(landrop.clipboard, "get_text", lambda: "PCの文字")
    r = client(tmp_path, hub, share_pc_clipboard=True).get("/api/text/pc", headers=H)
    assert r.json() == {"text": "PCの文字"}


def test_get_pc_text_disabled(tmp_path, hub, monkeypatch):
    monkeypatch.setattr(landrop.clipboard, "get_text", lambda: pytest.fail("読まれてはいけない"))
    assert client(tmp_path, hub).get("/api/text/pc", headers=H).status_code == 403


def test_history_keeps_latest_first_and_limited():
    h = TextHub()
    for i in range(15):
        h.receive(ReceivedText(str(i), "x"))
    assert [t.text for t in h.recent()] == [str(i) for i in range(14, 4, -1)]


@pytest.mark.parametrize("text, url", [
    ("https://example.com/x?y=1", "https://example.com/x?y=1"),
    ("  http://a.b/  \n", "http://a.b/"),
    ("見て https://example.com", None),
    ("ただのメモ", None),
])
def test_url_detection(text, url):
    assert ReceivedText(text, "x").url == url


def test_text_to_clipboard_with_url_button():
    copied, toasts = [], []
    t = TextToClipboard(notify=False, set_text=copied.append)
    t._toast = lambda *a, **kw: toasts.append((a, kw))
    t(ReceivedText("https://example.com", "x"))
    t(ReceivedText("メモ", "x"))
    assert copied == ["https://example.com", "メモ"]
    (_, kw1), (_, kw2) = toasts
    assert kw1["buttons"][0]["arguments"] == "https://example.com"
    assert "buttons" not in kw2 and 'activationType="foreground"' in kw2["xml"]


def test_toast_runs_in_subprocess_with_json_payload():
    import json
    import sys

    from landrop.toast import toast

    launched = []
    toast("タイトル", "本文", launcher=launched.append, on_click="file:///S:/x", buttons=[{"content": "開く"}])
    (cmd,) = launched
    assert cmd[:3] == [sys.executable, "-m", "landrop.toast"]
    assert json.loads(cmd[3]) == {"title": "タイトル", "body": "本文", "on_click": "file:///S:/x", "buttons": [{"content": "開く"}]}


def test_toast_child_passes_args_to_win11toast(monkeypatch):
    import json
    import types

    from landrop import toast as t

    got = {}
    monkeypatch.setitem(__import__("sys").modules, "win11toast", types.SimpleNamespace(notify=lambda **kw: got.update(kw)))
    t._main(["x", json.dumps({"title": "a", "body": "b", "on_click": None})])
    assert got == {"title": "a", "body": "b", "on_click": None}


def test_server_process_never_imports_winrt(tmp_path):
    """常駐プロセスで WinRT を読み込まない（落ちる原因になった）。"""
    import subprocess
    import sys

    code = (
        "import sys; from landrop.config import Settings; from landrop.server import create_app;"
        "from pathlib import Path; create_app(Settings(token='t', save_dir=Path('.'), notify=True, text_to_clipboard=True));"
        "import landrop.tray;"
        "print(any(m.startswith(('winrt', 'win11toast')) for m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"


def test_build_texts_popup_by_default_and_no_silent_copy(tmp_path):
    from landrop.texts import TextPopup, build_texts

    hub = build_texts(Settings(token=TOKEN, save_dir=tmp_path, text_popup=True))
    assert [type(l).__name__ for l in hub.listeners] == ["TextPopup"]  # クリップボードは書き換えない


def test_text_popup_launches_child_with_text(tmp_path):
    import json

    from landrop.texts import TextPopup

    launched = []
    TextPopup(launcher=launched.append)(ReceivedText("https://example.com", "1.2.3.4"))
    (cmd,) = launched
    assert cmd[1:3] == ["-m", "landrop.text_popup"]
    assert json.loads(cmd[3]) == {"text": "https://example.com", "client": "1.2.3.4"}
