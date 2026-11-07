import io
import threading
from pathlib import Path

import pillow_heif
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from landrop.config import Settings
from landrop.hooks import ReceivedFile, build_hooks
from landrop.notify import AfterBurst, Debounced, format_message
from landrop.storage import SessionDirs
from landrop.server import create_app

TOKEN = "test-token"
H = {"X-LanDrop-Token": TOKEN}


def heic_bytes(size=(400, 200), orientation=None) -> bytes:
    pillow_heif.register_heif_opener()
    im = Image.new("RGB", size, "red")
    im.paste((0, 0, 255), (0, 0, size[0] // 4, size[1]))  # 左端だけ青
    ex = Image.Exif()
    ex[0x010F] = "Apple"
    ex[0x9003] = "2026:10:17 10:00:00"
    if orientation:
        ex[0x0112] = orientation
    buf = io.BytesIO()
    im.save(buf, format="HEIF", exif=ex.tobytes(), quality=90)
    return buf.getvalue()


def make_client(save_dir: Path, **kw) -> TestClient:
    s = Settings(token=TOKEN, save_dir=save_dir, heic_to_jpeg=True, **kw)
    return TestClient(create_app(s))


def test_heic_converted_and_original_kept(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/upload", headers=H, files=[("files", ("IMG_0001.HEIC", heic_bytes()))])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "IMG_0001.jpg"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["IMG_0001.jpg", "_heic"]
    assert [p.name for p in (tmp_path / "_heic").iterdir()] == ["IMG_0001.HEIC"]
    with Image.open(tmp_path / "IMG_0001.jpg") as im:
        assert im.format == "JPEG"
        ex = im.getexif()
        assert ex.get(0x010F) == "Apple"  # EXIF が引き継がれている


def test_heic_rotation_is_baked_in(tmp_path):
    c = make_client(tmp_path)
    c.post("/api/upload", headers=H, files=[("files", ("rot.heic", heic_bytes(orientation=6)))])
    with Image.open(tmp_path / "rot.jpg") as im:
        assert im.size == (200, 400)  # 90°回転済み
        assert im.getexif().get(0x0112, 1) == 1  # 二重回転しない
        assert im.getpixel((100, 20))[2] > 200  # 左端の青が上端に来ている


def test_heic_original_deleted_when_configured(tmp_path):
    c = make_client(tmp_path, heic_keep_original=False)
    c.post("/api/upload", headers=H, files=[("files", ("a.heic", heic_bytes()))])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg"]


def test_heic_name_clash_does_not_overwrite_existing_jpg(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"existing")
    c = make_client(tmp_path)
    r = c.post("/api/upload", headers=H, files=[("files", ("a.heic", heic_bytes()))])
    assert r.json()["saved"][0]["name"] == "a (1).jpg"
    assert (tmp_path / "a.jpg").read_bytes() == b"existing"


def test_broken_heic_is_kept_as_is(tmp_path):
    c = make_client(tmp_path)
    r = c.post("/api/upload", headers=H, files=[("files", ("bad.heic", b"not an image"))])
    assert r.status_code == 200
    assert r.json()["saved"][0]["name"] == "bad.heic"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.heic"]


def test_non_heic_untouched(tmp_path):
    c = make_client(tmp_path)
    c.post("/api/upload", headers=H, files=[("files", ("x.png", b"png"))])
    assert [p.name for p in tmp_path.iterdir()] == ["x.png"]


def test_build_hooks_respects_flags(tmp_path):
    off = build_hooks(Settings(token=TOKEN, save_dir=tmp_path))
    assert off.transforms == [] and len(off.listeners) == 2  # ログ + AfterBurst（何もしない設定）
    on = build_hooks(Settings(token=TOKEN, save_dir=tmp_path, heic_to_jpeg=True))
    assert len(on.transforms) == 1
    assert [type(a).__name__ for a in on.listeners[1].actions] == ["AfterBurst"]


def rf(name: str, size: int = 1024) -> ReceivedFile:
    return ReceivedFile(Path(name), name, size, "1.2.3.4")


def test_debounced_batches_bursts():
    calls = []
    done = threading.Event()
    d = Debounced([lambda items: (calls.append(items), done.set())], quiet_sec=0.2)
    d([rf("a.jpg")])
    d([rf("b.jpg")])
    d([rf("c.jpg"), rf("d.jpg")])
    assert done.wait(3)
    assert [[it.path.name for it in c] for c in calls] == [["a.jpg", "b.jpg", "c.jpg", "d.jpg"]]


def test_debounced_one_failing_action_does_not_stop_others():
    done = threading.Event()
    d = Debounced([lambda items: 1 / 0, lambda items: done.set()], quiet_sec=0.05)
    d([rf("a.jpg")])
    assert done.wait(3)


class Recorder:
    """AfterBurst に渡す偽物（実際に窓を開いたりクリップボードを書き換えたりしない）。"""

    def __init__(self) -> None:
        self.opened, self.revealed, self.copied, self.toasts = [], [], [], []

    def make(self, tmp_path, **flags) -> AfterBurst:
        s = Settings(token=TOKEN, save_dir=tmp_path, **flags)
        return AfterBurst(
            s,
            opener=self.opened.append,
            set_image=self.copied.append,
            toast=lambda *a, **kw: self.toasts.append((a, kw)),
            reveal=self.revealed.append,
        )


def files(tmp_path, folder, *names, batch=""):
    return [ReceivedFile(tmp_path / folder / n, n, 10, "x", batch) for n in names]


def test_after_burst_reveals_first_file_once_per_batch(tmp_path):
    r = Recorder()
    a = r.make(tmp_path, open_folder=True)
    a(files(tmp_path, "d1", "a.jpg", "b.jpg", batch="b1"))
    a(files(tmp_path, "d1", "c.jpg", batch="b1"))           # 同じ回の続き → 開き直さない
    a(files(tmp_path, "d1", "e.jpg", "f.jpg", batch="b2"))  # 同じ日付フォルダでも別の回 → 開く
    assert r.revealed == [(tmp_path / "d1" / "a.jpg").resolve(), (tmp_path / "d1" / "e.jpg").resolve()]
    assert r.opened == []


def test_single_image_copied_and_opened_instead_of_folder(tmp_path):
    r = Recorder()
    a = r.make(tmp_path, open_folder=True, notify=True, single_image_clipboard=True, single_image_open=True)
    (img,) = files(tmp_path, "d1", "shot.PNG")
    a([img])
    assert r.copied == [img.path]
    assert r.opened == [img.path] and r.revealed == []  # フォルダは開かない
    (title, body), kw = r.toasts[0]
    assert "コピー済み" in title and kw["on_click"] == (tmp_path / "d1").resolve().as_uri()


def test_single_image_flags_off_behaves_like_normal(tmp_path):
    r = Recorder()
    a = r.make(tmp_path, open_folder=True)
    a(files(tmp_path, "d1", "shot.jpg"))
    assert r.copied == [] and r.revealed == [(tmp_path / "d1" / "shot.jpg").resolve()]


@pytest.mark.parametrize("names", [("a.jpg", "b.jpg"), ("movie.mov",), ("doc.pdf",)])
def test_not_single_image(tmp_path, names):
    r = Recorder()
    r.make(tmp_path, single_image_clipboard=True, single_image_open=True)(files(tmp_path, "d1", *names))
    assert r.copied == [] and r.opened == []


def test_flags_are_read_at_call_time(tmp_path):
    r = Recorder()
    a = r.make(tmp_path)
    a.settings.single_image_clipboard = True  # トレイのチェックで実行中に切り替わる
    a(files(tmp_path, "d1", "a.jpg"))
    assert len(r.copied) == 1


def test_clipboard_failure_does_not_block_rest(tmp_path):
    r = Recorder()
    a = r.make(tmp_path, open_folder=True, notify=True, single_image_clipboard=True)
    a._set_image = lambda p: 1 / 0
    a(files(tmp_path, "d1", "a.jpg"))
    assert r.revealed and "コピー済み" not in r.toasts[0][0][0]


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:  # 2027-01-15 17:00 JST
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_date_folder_and_batches_split_by_gap(tmp_path):
    clock = FakeClock()
    s = SessionDirs(tmp_path, gap_sec=60, clock=clock)
    s.begin(); first = s.current_batch_id; folder = s.current; s.add([folder / "a.jpg"]); s.end()
    clock.t += 30
    s.begin(); assert s.current_batch_id == first; s.add([folder / "b.jpg"]); s.end()   # 30秒後 → 同じ回
    clock.t += 61
    s.begin(); second = s.current_batch_id; s.add([s.current / "c.jpg"]); s.end()      # 61秒あいた → 別の回
    assert second != first
    assert folder.parent == tmp_path and len(folder.name) == len("2026-10-17")       # 日付フォルダ
    assert s.current == folder                                                        # 同じ日なら同じフォルダ
    for p in (folder / "a.jpg", folder / "b.jpg", folder / "c.jpg"):
        p.parent.mkdir(exist_ok=True); p.write_bytes(b"x")
    assert [[p.name for p in b.files] for b in s.recent()] == [["c.jpg"], ["a.jpg", "b.jpg"]]


def test_batch_keeps_its_start_date_across_midnight(tmp_path):
    clock = FakeClock(1_800_000_000.0 + 7 * 3600 - 10)  # 日付が変わる10秒前（JST）
    s = SessionDirs(tmp_path, gap_sec=60, clock=clock)
    s.begin(); folder = s.current
    clock.t += 30
    s.begin(); assert s.current == folder  # 受信中に日付が変わっても同じフォルダ
    s.end(); s.end()


def test_long_upload_keeps_batch(tmp_path):
    clock = FakeClock()
    s = SessionDirs(tmp_path, gap_sec=60, clock=clock)
    s.begin(); first = s.current_batch_id     # 大きな動画の受信が始まる
    clock.t += 300
    s.begin(); assert s.current_batch_id == first  # 受信中に来た次のファイルは同じ回
    s.end(); s.end()


def test_date_folders_disabled(tmp_path):
    s = SessionDirs(tmp_path, date_folders=False)
    s.begin()
    assert s.current == tmp_path


def test_recent_skips_deleted_files_and_persists(tmp_path):
    state = tmp_path / ".state.json"
    clock = FakeClock()
    s = SessionDirs(tmp_path, gap_sec=60, clock=clock, state_file=state)
    for name in ("a.jpg", "b.jpg"):
        s.begin(); p = s.current / name; p.parent.mkdir(exist_ok=True); p.write_bytes(b"x"); s.add([p]); s.end()
        clock.t += 120
    (s.current / "a.jpg").unlink()
    again = SessionDirs(tmp_path, state_file=state)  # 再起動しても覚えている
    assert [[p.name for p in b.files] for b in again.recent()] == [["b.jpg"]]


def test_upload_goes_into_date_folder_and_is_remembered(tmp_path):
    c = TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path, date_folders=True)))
    c.post("/api/upload", headers=H, files=[("files", ("a.jpg", b"1"))])
    c.post("/api/upload/raw?filename=b.jpg", headers=H, content=b"2")
    (day,) = [p for p in tmp_path.iterdir() if not p.name.startswith(".")]
    assert day.is_dir() and sorted(p.name for p in day.iterdir()) == ["a.jpg", "b.jpg"]
    (batch,) = c.app.state.sessions.recent()
    assert [p.name for p in batch.files] == ["a.jpg", "b.jpg"]


def test_unauthorized_request_does_not_start_batch(tmp_path):
    app = create_app(Settings(token=TOKEN, save_dir=tmp_path, date_folders=True))
    TestClient(app).post("/api/upload", files=[("files", ("a.jpg", b"1"))])
    assert app.state.sessions.current_batch_id == ""


@pytest.mark.parametrize("items, title_part, body_part", [
    ([rf("IMG_1.jpg", 2048)], "受信しました", "IMG_1.jpg（2.0 KB）"),
    ([rf("a"), rf("b")], "2件", "a、b（計 2.0 KB）"),
])
def test_format_message(items, title_part, body_part):
    title, body = format_message(items)
    assert title_part in title and body == body_part


def test_same_file_same_day_is_not_saved_again_but_recorded(tmp_path):
    c = TestClient(create_app(Settings(token=TOKEN, save_dir=tmp_path, date_folders=True, session_gap_sec=0)))
    first = c.post("/api/upload", headers=H, files=[("files", ("IMG_0001.jpg", b"photo"))]).json()["saved"][0]
    import time; time.sleep(0.05)
    # iPhone が名前を変えて送ってきても中身で判定
    again = c.post("/api/upload", headers=H, files=[("files", ("image.jpg", b"photo"))]).json()["saved"][0]
    other = c.post("/api/upload", headers=H, files=[("files", ("x.jpg", b"other"))]).json()["saved"][0]
    (day,) = [p for p in tmp_path.iterdir() if not p.name.startswith(".")]
    assert sorted(p.name for p in day.iterdir()) == ["IMG_0001.jpg", "x.jpg"]
    assert again["duplicate"] is True and again["name"] == "IMG_0001.jpg" and again["id"] == first["id"]
    assert other["duplicate"] is False
    # 送り直した回も「直近」「一つ前」に既存のファイルで残る
    assert [[p.name for p in b.files] for b in c.app.state.sessions.recent(3)] == [["x.jpg"], ["IMG_0001.jpg"], ["IMG_0001.jpg"]]


def test_same_file_on_another_day_is_saved(tmp_path):
    from landrop.storage import reuse_same_file

    (tmp_path / "2026-10-16").mkdir()
    (tmp_path / "2026-10-16" / "a.jpg").write_bytes(b"same")
    (tmp_path / "2026-10-17").mkdir()
    new = tmp_path / "2026-10-17" / "a.jpg"
    new.write_bytes(b"same")
    assert reuse_same_file(new) == (new, False) and new.exists()


def test_same_heic_resent_reuses_converted_jpeg(tmp_path):
    c = make_client(tmp_path)  # heic_to_jpeg=True（日付フォルダなし＝保存先の直下）
    data = heic_bytes()
    c.post("/api/upload", headers=H, files=[("files", ("IMG_1.HEIC", data))])
    r = c.post("/api/upload", headers=H, files=[("files", ("IMG_1 copy.HEIC", data))]).json()["saved"][0]
    assert r["duplicate"] is True and r["name"] == "IMG_1.jpg"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["IMG_1.jpg", "_heic"]
    assert [p.name for p in (tmp_path / "_heic").iterdir()] == ["IMG_1.HEIC"]
