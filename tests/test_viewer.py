from fastapi.testclient import TestClient

from landrop.config import Settings
from landrop.server import create_app
from landrop.viewer import ShowBoard, kind_of

TOKEN = "test-token"
H = {"X-LanDrop-Token": TOKEN}


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def make(tmp_path):
    opened = []
    clock = Clock()
    board = ShowBoard(open_viewer=lambda: opened.append(1), clock=clock)
    c = TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path), board=board))
    return c, board, opened, clock


def test_upload_returns_id_and_show_opens_viewer_once(tmp_path):
    c, board, opened, clock = make(tmp_path)
    file_id = c.post("/api/upload", headers=H, files=[("files", ("a.jpg", b"x"))]).json()["saved"][0]["id"]
    assert board.path_of(file_id) == tmp_path / "a.jpg"

    assert c.post("/api/show", headers=H, json={"id": file_id}).status_code == 200
    assert c.post("/api/show", headers=H, json={"id": file_id}).status_code == 200  # 連打
    assert opened == [1]  # ビューアーが繋がるまでの猶予中は開き直さない
    assert board.state()["total"] == 1 and board.state()["kind"] == "image"

    clock.t += 30
    c.post("/api/show", headers=H, json={"id": file_id})
    assert opened == [1, 1]  # 猶予を過ぎても繋がってこなければ開き直す


def test_show_does_not_open_when_viewer_connected(tmp_path):
    c, board, opened, _ = make(tmp_path)
    file_id = c.post("/api/upload", headers=H, files=[("files", ("a.mp4", b"x"))]).json()["saved"][0]["id"]
    board.viewer_connected("v1", True)
    c.post("/api/show", headers=H, json={"id": file_id})
    assert opened == [] and board.state()["kind"] == "video"


def test_hidden_viewer_does_not_count(tmp_path):
    """裏に隠れたタブや最小化した窓しか無いなら、タップで新しいビューアーを開く（実際に起きた不具合）。"""
    c, board, opened, _ = make(tmp_path)
    file_id = c.post("/api/upload", headers=H, files=[("files", ("a.jpg", b"x"))]).json()["saved"][0]["id"]
    board.viewer_connected("old-tab", False)
    r = c.post("/api/show", headers=H, json={"id": file_id})
    assert opened == [1] and r.json()["viewers"] == 0


def test_visibility_changes_and_early_report(tmp_path):
    board = ShowBoard()
    board.set_visible("v1", False)       # 再接続直後、登録より先に届いた報告
    board.viewer_connected("v1", True)   # URL の visible は古い値
    assert not board.active_visible
    board.set_visible("v1", True)
    assert board.active_visible
    board.viewer_disconnected("v1")
    assert not board.active_visible and board.active is None


def test_new_viewer_takes_over_and_old_one_pauses():
    board = ShowBoard()
    board.viewer_connected("old", True)
    board.viewer_connected("new", True)
    assert board.active == "new"
    assert board.state("old")["active"] is False and board.state("new")["active"] is True
    assert board.activate("old") and board.active == "old"  # 止まっている方をクリック
    assert not board.activate("gone")


def test_hidden_reconnect_does_not_steal_active():
    """再起動後、裏に残っていた古いタブが後から繋ぎ直してきても、見えている窓が有効のまま。"""
    board = ShowBoard()
    board.viewer_connected("window", True)
    board.viewer_connected("old-tab", False)
    assert board.active == "window"


def test_show_switches_to_visible_viewer_instead_of_opening(tmp_path):
    c, board, opened, _ = make(tmp_path)
    file_id = c.post("/api/upload", headers=H, files=[("files", ("a.jpg", b"x"))]).json()["saved"][0]["id"]
    board.viewer_connected("a", True)
    board.viewer_connected("b", True)   # b が有効
    board.set_visible("b", False)       # b は最小化された
    r = c.post("/api/show", headers=H, json={"id": file_id})
    assert opened == [] and board.active == "a" and r.json()["viewers"] == 1


def test_disconnect_hands_over_to_visible_viewer():
    board = ShowBoard()
    board.viewer_connected("visible", True)
    board.viewer_connected("hidden", False)
    board.viewer_connected("newest", True)
    board.viewer_disconnected("newest")
    assert board.active == "visible"


def test_version_bumps_on_active_change():
    board = ShowBoard()
    v = board.version
    board.viewer_connected("a", True)
    assert board.version > v


def test_visibility_endpoint_is_local_only(tmp_path):
    c, *_ = make(tmp_path)
    assert c.post("/viewer/visibility?cid=x&visible=1").status_code == 403


def test_show_unknown_or_bad(tmp_path):
    c, *_ = make(tmp_path)
    assert c.post("/api/show", headers=H, json={"id": "nope"}).status_code == 404
    assert c.post("/api/show", headers=H, json={"x": 1}).status_code == 400
    assert c.post("/api/show", json={"id": "nope"}).status_code == 401


def test_viewer_endpoints_are_local_only(tmp_path):
    c, *_ = make(tmp_path)
    for path in ("/viewer", "/viewer/events", "/viewer/file/abc"):
        assert c.get(path).status_code == 403  # TestClient のアドレスは外部扱い


def test_kind_of():
    from pathlib import Path

    assert [kind_of(Path(n)) for n in ("a.JPG", "b.mov", "c.pdf")] == ["image", "video", "other"]


def test_browser_opener_uses_edge_app_mode(monkeypatch):
    import landrop.viewer as v

    launched = []
    monkeypatch.setattr(v, "find_edge", lambda: r"C:\Edge\msedge.exe")
    v.browser_opener(8000, launched.append)()
    assert launched == [[r"C:\Edge\msedge.exe", "--app=http://localhost:8000/viewer", "--window-size=1280,800"]]


def test_browser_opener_falls_back_to_default_browser(monkeypatch):
    import landrop.viewer as v

    opened = []
    monkeypatch.setattr(v, "find_edge", lambda: None)
    monkeypatch.setattr(v.webbrowser, "open", opened.append)
    v.browser_opener(8000, lambda cmd: None)()
    assert opened == ["http://localhost:8000/viewer"]


def test_favicon_is_public(tmp_path):
    c, *_ = make(tmp_path)
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] == "image/x-icon"


def upload(c, *names):
    files = [("files", (n, n.encode())) for n in names]
    return [s["id"] for s in c.post("/api/upload", headers=H, files=files).json()["saved"]]


def test_step_moves_within_list_and_stops_at_ends(tmp_path):
    c, board, *_ = make(tmp_path)
    ids = upload(c, "a.jpg", "b.jpg", "c.jpg")
    board.viewer_connected("v", True)
    c.post("/api/show", headers=H, json={"id": ids[1], "ids": ids})
    assert board.state()["pos"] == 2 and board.state()["total"] == 3
    assert board.step(1) and board.state()["name"] == "c.jpg"
    assert not board.step(1)                      # 端では止まる
    (tmp_path / "b.jpg").unlink()                 # 消されたファイルは飛ばす
    assert board.step(-1) and board.state()["name"] == "a.jpg"


def test_delete_moves_to_trash_and_shows_next(tmp_path):
    c, board, *_ = make(tmp_path)
    ids = upload(c, "a.jpg", "b.jpg")
    board.viewer_connected("v", True)
    c.post("/api/show", headers=H, json={"id": ids[0], "ids": ids})
    trashed = []

    def fake_trash(paths):
        trashed.extend(paths)
        for p in paths:
            p.unlink()

    assert board.delete_current(fake_trash) == tmp_path / "a.jpg"
    assert trashed == [tmp_path / "a.jpg"] and board.state()["name"] == "b.jpg" and board.state()["total"] == 1
    board.delete_current(fake_trash)
    assert board.state()["id"] is None            # 最後の1枚を消したら空
    assert c.post("/api/show", headers=H, json={"id": ids[0]}).status_code == 404  # 消したものは映せない


def test_delete_also_trashes_heic_original(tmp_path):
    board = ShowBoard()
    (tmp_path / "_heic").mkdir()
    jpg, heic = tmp_path / "IMG_1.jpg", tmp_path / "_heic" / "IMG_1.HEIC"
    jpg.write_bytes(b"j"); heic.write_bytes(b"h")
    board.show(board.register(jpg))
    trashed = []
    board.delete_current(trashed.extend)
    assert set(trashed) == {jpg, heic}


def test_delete_failure_changes_nothing(tmp_path):
    board = ShowBoard()
    (tmp_path / "a.jpg").write_bytes(b"x")
    fid = board.register(tmp_path / "a.jpg")
    board.show(fid)

    def broken(paths):
        raise OSError("no recycle bin")

    import pytest
    with pytest.raises(OSError):
        board.delete_current(broken)
    assert board.state()["id"] == fid


def test_viewer_step_and_delete_are_local_only(tmp_path):
    c, *_ = make(tmp_path)
    assert c.post("/viewer/step?delta=1").status_code == 403
    assert c.post("/viewer/delete?id=x").status_code == 403


def test_vendor_library_is_served(tmp_path):
    c, *_ = make(tmp_path)
    r = c.get("/vendor/sweetalert2.all.min.js")
    assert r.status_code == 200 and "sweetalert2" in r.text[:200]
