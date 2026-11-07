from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from landrop.config import Settings, load_settings
from landrop.hooks import HookRegistry, ReceivedFile
from landrop.server import create_app
from landrop.storage import safe_filename

TOKEN = "test-token"
H = {"X-LanDrop-Token": TOKEN}


@pytest.fixture
def save_dir(tmp_path: Path) -> Path:
    return tmp_path / "inbox"


@pytest.fixture
def client(save_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(token=TOKEN, save_dir=save_dir, max_file_mb=1)))


def files_in(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir())


def test_index_is_public(client):
    r = client.get("/")
    assert r.status_code == 200 and "LAN Drop" in r.text


@pytest.mark.parametrize("headers", [{}, {"X-LanDrop-Token": "wrong"}, {"Authorization": "Bearer wrong"}])
def test_api_requires_token(client, save_dir, headers):
    assert client.get("/api/ping", headers=headers).status_code == 401
    r = client.post("/api/upload", headers=headers, files=[("files", ("a.jpg", b"x"))])
    assert r.status_code == 401
    assert not save_dir.exists()


def test_bearer_token_accepted(client):
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_upload_multiple_and_dedupe(client, save_dir):
    files = [("files", ("IMG_0001.JPG", b"one")), ("files", ("IMG_0001.JPG", b"two")), ("files", ("b.png", b"3"))]
    r = client.post("/api/upload", headers=H, files=files)
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["saved"]] == ["IMG_0001.JPG", "IMG_0001 (1).JPG", "b.png"]
    assert files_in(save_dir) == ["IMG_0001 (1).JPG", "IMG_0001.JPG", "b.png"]
    assert (save_dir / "IMG_0001 (1).JPG").read_bytes() == b"two"


def test_path_traversal_is_neutralized(client, save_dir):
    r = client.post("/api/upload", headers=H, files=[("files", ("..\\..\\evil.txt", b"x"))])
    assert r.json()["saved"][0]["name"] == "evil.txt"
    assert files_in(save_dir) == ["evil.txt"]


def test_too_large_is_rejected_and_cleaned(client, save_dir):
    big = b"0" * (1024 * 1024 + 1)
    r = client.post("/api/upload/raw?filename=big.bin", headers=H, content=big)
    assert r.status_code == 413
    assert not save_dir.exists() or files_in(save_dir) == []


def test_raw_upload_with_header_filename(client, save_dir):
    h = {**H, "X-Filename": "%E5%86%99%E7%9C%9F.heic", "Content-Type": "image/heic"}
    r = client.post("/api/upload/raw", headers=h, content=b"heic")
    assert r.status_code == 200
    assert files_in(save_dir) == ["写真.heic"]


def test_raw_upload_without_filename_uses_content_type(client, save_dir):
    r = client.post("/api/upload/raw", headers={**H, "Content-Type": "image/jpeg"}, content=b"jpg")
    name = r.json()["saved"][0]["name"]
    assert name.startswith("landrop_") and name.endswith(".jpg")


def test_raw_upload_empty_body(client, save_dir):
    assert client.post("/api/upload/raw", headers=H, content=b"").status_code == 400
    assert not save_dir.exists() or files_in(save_dir) == []


def test_setup_page_is_localhost_only(client):
    # TestClient のクライアントアドレスは "testclient" なので外部扱い
    assert client.get("/setup").status_code == 403


def test_hooks_are_called(save_dir):
    seen: list[ReceivedFile] = []
    hooks = HookRegistry()
    hooks.transforms.append(lambda f: ReceivedFile(f.path, f.original_name, f.size, "changed"))
    hooks.listeners.append(seen.extend)
    c = TestClient(create_app(Settings(token=TOKEN, save_dir=save_dir), hooks))
    c.post("/api/upload", headers=H, files=[("files", ("a.jpg", b"x"))])
    assert [f.client for f in seen] == ["changed"]


@pytest.mark.parametrize("raw, expected", [
    ("a:b?.jpg", "a_b_.jpg"),
    ("CON.txt", "_CON.txt"),
    ("", "file"),
    (".hidden", "file.hidden"),
    ("name. ", "name"),
    ("/etc/passwd", "passwd"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_config_autogenerates_token(tmp_path):
    p = tmp_path / "config.toml"
    s1 = load_settings(p)
    assert p.exists() and len(s1.token) >= 20
    assert load_settings(p).token == s1.token


def test_config_regenerates_empty_token_keeping_other_keys(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('token = ""\nport = 9000\n', encoding="utf-8")
    s = load_settings(p)
    assert s.token and s.port == 9000
    assert load_settings(p).token == s.token


@pytest.mark.parametrize("advertise, expected", [
    ("", "http://MYPC.local:8000/#t=test-token"),
    ("ip", "http://10.0.0.5:8000/#t=test-token"),
    ("pc.example", "http://pc.example:8000/#t=test-token"),
])
def test_setup_url_host(monkeypatch, tmp_path, advertise, expected):
    import landrop.server as srv
    monkeypatch.setattr(srv.socket, "gethostname", lambda: "MYPC")
    monkeypatch.setattr(srv, "lan_ip", lambda: "10.0.0.5")
    s = Settings(token=TOKEN, save_dir=tmp_path, advertise_host=advertise)
    assert srv.setup_url(s) == expected


def test_connect_qr_requires_token_and_is_svg(client):
    assert client.get("/api/qr").status_code == 401
    r = client.get("/api/qr", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg")
