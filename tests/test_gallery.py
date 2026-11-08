import io
import os

from fastapi.testclient import TestClient
from PIL import Image

from landrop.config import Settings
from landrop.gallery import folder_label, list_folders, resolve_folder
from landrop.server import create_app
from landrop.viewer import ShowBoard

TOKEN = "test-token"
H = {"X-LanDrop-Token": TOKEN}


def jpeg(w=800, h=600) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(buf, "JPEG")
    return buf.getvalue()


def setup_dirs(tmp_path):
    (tmp_path / "2026-10-17_130544").mkdir()
    (tmp_path / "2026-10-17_130544" / "b.jpg").write_bytes(jpeg())
    (tmp_path / "2026-10-17_130544" / "a.mov").write_bytes(b"mov")
    (tmp_path / "2026-10-17_130544" / "memo.txt").write_bytes(b"x")
    (tmp_path / "2026-10-16_090000").mkdir()
    (tmp_path / "2026-10-16_090000" / "c.png").write_bytes(b"png")
    (tmp_path / "empty").mkdir()
    (tmp_path / "_heic").mkdir()
    (tmp_path / "_heic" / "x.jpg").write_bytes(b"x")
    (tmp_path / "root.jpg").write_bytes(b"x")
    # 受信した順（更新日時）に並ぶこと
    os.utime(tmp_path / "2026-10-17_130544" / "b.jpg", (1000, 1000))
    os.utime(tmp_path / "2026-10-17_130544" / "a.mov", (2000, 2000))


def client(tmp_path):
    board = ShowBoard()
    return TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path), board=board)), board


def test_list_folders_newest_first_skips_empty_and_hidden(tmp_path):
    setup_dirs(tmp_path)
    got = [(f["name"], f["count"]) for f in list_folders(tmp_path)]
    assert got == [("2026-10-17_130544", 2), ("2026-10-16_090000", 1), ("", 1)]


def test_folder_label():
    assert folder_label("2026-10-17_130544") == "10/17 13:05"
    assert folder_label("旅行") == "旅行"


def test_resolve_folder_rejects_escape(tmp_path):
    setup_dirs(tmp_path)
    assert resolve_folder(tmp_path, "") == tmp_path
    assert resolve_folder(tmp_path, "2026-10-16_090000") == tmp_path / "2026-10-16_090000"
    for bad in ("..", "../x", "a/b", "_heic", ".", "nope"):
        assert resolve_folder(tmp_path, bad) is None


def test_folder_api_lists_media_in_receive_order_with_stable_ids(tmp_path):
    setup_dirs(tmp_path)
    c, board = client(tmp_path)
    assert [f["label"] for f in c.get("/api/folders", headers=H).json()["folders"]][0] == "10/17 13:05"
    items = c.get("/api/folder", headers=H, params={"name": "2026-10-17_130544"}).json()["items"]
    assert [(i["name"], i["kind"]) for i in items] == [("b.jpg", "image"), ("a.mov", "video")]
    again = c.get("/api/folder", headers=H, params={"name": "2026-10-17_130544"}).json()["items"]
    assert [i["id"] for i in again] == [i["id"] for i in items]  # 何度一覧しても同じ id
    assert c.post("/api/show", headers=H, json={"id": items[1]["id"]}).status_code == 200


def test_folder_api_requires_token_and_valid_name(tmp_path):
    setup_dirs(tmp_path)
    c, _ = client(tmp_path)
    assert c.get("/api/folders").status_code == 401
    assert c.get("/api/folder", headers=H, params={"name": "../"}).status_code == 404


def test_thumb_is_small_jpeg_and_only_for_registered_images(tmp_path):
    setup_dirs(tmp_path)
    c, _ = client(tmp_path)
    items = c.get("/api/folder", headers=H, params={"name": "2026-10-17_130544"}).json()["items"]
    r = c.get(f"/thumb/{items[0]['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) <= 320
    assert c.get(f"/thumb/{items[1]['id']}").status_code == 404  # 動画
    assert c.get("/thumb/0123456789ab").status_code == 404        # 知らない id


def test_same_minute_labels_get_seconds(tmp_path):
    for name in ("2026-10-17_133853", "2026-10-17_133857", "2026-10-17_120000"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.jpg").write_bytes(b"x")
    assert [f["label"] for f in list_folders(tmp_path)] == ["10/17 13:38:57", "10/17 13:38:53", "10/17 12:00"]


def test_date_folder_label_and_order(tmp_path):
    for name in ("2026-10-16", "2026-10-17", "2026-10-17_130544"):  # 以前のバッチごとのフォルダも混在
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.jpg").write_bytes(b"x")
    assert [f["label"] for f in list_folders(tmp_path)] == ["10/17 13:05", "10/17", "10/16"]


def test_recent_entries_and_api(tmp_path):
    from datetime import datetime

    c = TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path, date_folders=True, session_gap_sec=0)))
    c.post("/api/upload", headers=H, files=[("files", ("a.jpg", jpeg())), ("files", ("b.mov", b"m"))])
    import time; time.sleep(0.05)  # gap 0 秒なので次は別の回
    c.post("/api/upload", headers=H, files=[("files", ("c.jpg", jpeg()))])
    data = c.get("/api/folders", headers=H).json()
    hhmm = datetime.now().strftime("%H:%M")
    assert [r["label"] for r in data["recent"]] == [f"直近（{hhmm}・1件）", f"一つ前（{hhmm}・2件）"]
    assert [f["label"] for f in data["folders"]] == [datetime.now().strftime("%m/%d")]
    one_before = c.get("/api/folder", headers=H, params={"recent": 1}).json()["items"]
    assert [(i["name"], i["kind"]) for i in one_before] == [("a.jpg", "image"), ("b.mov", "video")]
    assert c.get("/api/folder", headers=H, params={"recent": 5}).status_code == 404
