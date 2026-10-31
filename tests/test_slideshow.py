# tests/test_slideshow.py
# 写真のスライドショー(slideshow.py / slideshow_core.py / slideshow_launch.py /
# slideshow_settings.py)と、それに合わせて共通化した EXIF の読み取り(filer_core /
# image_load)を確かめる。
#
#   C:\Users\<名前>\.venvs\tray-tools\Scripts\python.exe tests\test_slideshow.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_filer.py と同じ形)。窓は offscreen で作る(実画面に出さない)。
# 触るのは tempfile で作った一時フォルダと一時の settings.json だけ。本物の写真フォルダ・
# settings.json・%LOCALAPPDATA% には触れない(あっふの窓を作るテストも TRAYTOOLS_DATA_DIR を
# 一時フォルダへ向ける)。別プロセスの起動(subprocess.Popen)と、画面を消さない設定
# (SetThreadExecutionState)は差し替えて、実際には起こさない・掛けない。
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_TEST_TEMP = tempfile.mkdtemp(prefix="slideshow_test_tmp_")
os.environ["TRAYTOOLS_DATA_DIR"] = os.path.join(_TEST_TEMP, "data")

import filer_core as core  # noqa: E402
import slideshow_core as sc  # noqa: E402

_app = None


def _qapp():
    global _app
    from PySide6.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])
    return _app


def _touch(path, data=b"x"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def _set_hidden(path, system=False):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    k32.SetFileAttributesW.restype = wintypes.BOOL
    assert k32.SetFileAttributesW(str(path), 0x4 if system else 0x2)


def _junction(link, target) -> bool:
    """ジャンクションを作る(管理者権限が要らない)。作れなければ False。"""
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True)
    return result.returncode == 0 and os.path.isdir(link)


# ---------------------------------------------------------------
# 一覧作り
# ---------------------------------------------------------------
def test_iter_images_extensions_hidden_recursive_and_links():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp, "写真")
        for name in ("a.jpg", "b.JPEG", "c.png", "d.bmp", "e.gif", "f.webp"):
            _touch(root / name)
        for name in ("g.heic", "h.txt", "i.jpg.part", "noext"):
            _touch(root / name)
        hidden = _touch(root / "hidden.jpg")
        _set_hidden(hidden)
        system = _touch(root / "system.jpg")
        _set_hidden(system, system=True)
        _touch(root / "sub" / "s1.jpg")
        _touch(root / "sub" / "deeper" / "s2.png")
        hidden_dir = root / "隠しフォルダ"
        _touch(hidden_dir / "x.jpg")
        _set_hidden(hidden_dir)
        outside = Path(tmp, "外")
        _touch(outside / "linked.jpg")
        made_link = _junction(root / "link", outside)

        flat = sorted(os.path.basename(p) for p in sc.iter_images(str(root), False))
        assert flat == ["a.jpg", "b.JPEG", "c.png", "d.bmp", "e.gif", "f.webp"], flat
        deep = sorted(os.path.basename(p) for p in sc.iter_images(str(root), True))
        assert deep == sorted(flat + ["s1.jpg", "s2.png"]), deep
        assert made_link, "ジャンクションを作れませんでした(テストの前提)"
        assert "linked.jpg" not in deep
        # 各フォルダは先にファイル、次にサブフォルダ(最初の数枚を早く出すため)
        order = [os.path.basename(p) for p in sc.iter_images(str(root), True)]
        assert order.index("s1.jpg") > max(order.index(n) for n in flat)
        # 途中で止める
        import threading
        stop = threading.Event()
        stop.set()
        assert list(sc.iter_images(str(root), True, stop)) == []


def test_iter_images_unreadable_and_missing_folder():
    assert list(sc.iter_images(r"Z:\どこにもない\フォルダ", True)) == []
    with tempfile.TemporaryDirectory() as tmp:
        _touch(Path(tmp, "a.jpg"))
        pool = sc.PhotoPool([{"path": tmp, "recursive": True},
                             {"path": os.path.join(tmp, "無い"), "recursive": True}])
        sc.scan_into(pool)
        assert pool.is_finished() and pool.counts() == [1, 0]
        assert pool.missing == [os.path.join(tmp, "無い")]


def test_scan_interleaves_folders():
    """先頭の大きなフォルダを読み終える前に、ほかのフォルダの写真も入ってくる。"""
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(300):
            _touch(Path(tmp, "big", f"{i:04}.jpg"))
        for i in range(5):
            _touch(Path(tmp, "small", f"{i}.jpg"))
        pool = sc.PhotoPool([{"path": os.path.join(tmp, "big"), "recursive": False},
                             {"path": os.path.join(tmp, "small"), "recursive": False}])
        order = []
        real_add = pool.add
        pool.add = lambda index, paths: (order.append((index, len(paths))), real_add(index, paths))[1]
        sc.scan_into(pool, chunk=50)
        assert order[:2] == [(0, 50), (1, 5)], order
        assert pool.counts() == [300, 5]


# ---------------------------------------------------------------
# 選び方
# ---------------------------------------------------------------
def _pool_with(counts, seed=1):
    pool = sc.PhotoPool([{"path": f"C:\\f{i}", "recursive": True} for i in range(len(counts))],
                        rng=random.Random(seed))
    for i, n in enumerate(counts):
        pool.add(i, [f"C:\\f{i}\\{j}.jpg" for j in range(n)])
    return pool


def test_pool_folders_are_equally_likely():
    pool = _pool_with([2000, 10])
    picks = [pool.pick() for _ in range(4000)]
    small = sum(1 for p in picks if p.startswith("C:\\f1\\"))
    assert 1800 < small < 2200, small     # 枚数に比例(約20枚)ではなく、フォルダで半々


def test_pool_bag_no_repeat_until_cycle():
    for seed in range(20):
        pool = _pool_with([12], seed)
        picks = [pool.pick() for _ in range(36)]
        for cycle in range(3):
            chunk = picks[cycle * 12:(cycle + 1) * 12]
            assert len(set(chunk)) == 12, (seed, chunk)
        # 周の変わり目で、同じ1枚が続けて出ない
        assert all(picks[i] != picks[i + 1] for i in range(len(picks) - 1)), seed


def test_pool_add_during_cycle_discard_and_duplicates():
    pool = _pool_with([3])
    first = pool.pick()
    pool.add(0, ["C:\\f0\\new.jpg"])
    rest = [pool.pick() for _ in range(3)]
    assert sorted([first] + rest) == sorted(["C:\\f0\\0.jpg", "C:\\f0\\1.jpg", "C:\\f0\\2.jpg",
                                             "C:\\f0\\new.jpg"])
    pool.discard("C:\\f0\\new.jpg")
    assert "C:\\f0\\new.jpg" not in [pool.pick() for _ in range(20)]
    # 入れ子に登録したフォルダの重なりは1枚として数える
    assert pool.add(0, ["C:\\F0\\0.JPG"]) == 0
    empty = sc.PhotoPool([{"path": "C:\\x", "recursive": True}])
    assert empty.pick() is None


def test_wait_ready():
    pool = _pool_with([0])
    t = [100.0]
    assert not sc.wait_ready(pool, 100.0, lambda: t[0])
    t[0] = 100.0 + sc.START_WAIT
    assert sc.wait_ready(pool, 100.0, lambda: t[0])
    pool2 = _pool_with([sc.START_MIN])
    assert sc.wait_ready(pool2, 100.0, lambda: 100.0)
    pool.mark_finished()
    assert sc.wait_ready(pool, 100.0, lambda: 100.0)


def test_history():
    h = sc.History(limit=3)
    assert h.back() is None and h.forward() is None and h.current() is None
    for p in "abcd":
        h.push(p)
    assert h.items == ["b", "c", "d"] and h.current() == "d" and h.at_end()
    assert h.back() == "c" and h.back() == "b" and h.back() is None
    assert h.forward() == "c" and not h.at_end()
    h.push("e")                        # 戻っている途中で新しい1枚 → 先の分は捨てる
    assert h.items == ["b", "c", "e"]
    # 読めなかった1枚を外す: 同じ向きにもう1つ進めば、外した次(前)に着く
    h = sc.History()
    for p in "abcde":
        h.push(p)
    h.back(); h.back()                 # c
    h.remove_current(-1)
    assert h.back() == "b"
    h = sc.History()
    for p in "abcde":
        h.push(p)
    h.back(); h.back(); h.back()       # b
    h.forward()                        # c
    h.remove_current(+1)
    assert h.forward() == "d"


# ---------------------------------------------------------------
# Ken Burns
# ---------------------------------------------------------------
def test_frame_rect_fit_and_zoom_stays_covered():
    # 動き無し: 画面に収まる大きさで真ん中
    x, y, w, h = sc.frame_rect(4000, 3000, 1920, 1080)
    assert abs(h - 1080) < 1e-6 and abs(w - 1440) < 1e-6 and abs(x - 240) < 1e-6 and y == 0
    rng = random.Random(3)
    for _ in range(50):
        m = sc.random_motion(rng)
        assert {m.z0, m.z1} == {1.0, sc.ZOOM_MAX}
        for t in (0.0, 0.3, 0.7, 1.0):
            x, y, w, h = sc.frame_rect(4000, 3000, 1920, 1080, m, t)
            # 縦は画面いっぱいなので、はみ出したぶんの中だけで動く(上下に黒が出ない)
            assert y <= 1e-6 and y + h >= 1080 - 1e-6, (m, t, y, h)
            # 横は余白のある向き。真ん中のまま
            assert abs((x + w / 2) - 960) < 1e-6
    # 拡大は進む
    m = sc.Motion(1.0, sc.ZOOM_MAX, 0, 0, 0, 0)
    assert sc.frame_rect(1920, 1080, 1920, 1080, m, 1.0)[2] > sc.frame_rect(1920, 1080, 1920, 1080, m, 0.0)[2]
    assert sc.random_motion(rng, enabled=False) is sc.STILL
    assert sc.ease(0) == 0 and sc.ease(1) == 1 and abs(sc.ease(0.5) - 0.5) < 1e-9


# ---------------------------------------------------------------
# 設定
# ---------------------------------------------------------------
def test_settings_defaults_and_normalize():
    import settings as settings_module
    section = settings_module.DEFAULT_SETTINGS["slideshow"]
    assert section == {"folders": [], "interval_seconds": 10, "fade_seconds": 1.5,
                       "ken_burns": True, "monitor": "", "fps": 60}
    config = sc.normalize_config({})
    assert config["interval_seconds"] == 10 and config["fade_seconds"] == 1.5
    bad = sc.normalize_config({"interval_seconds": "x", "fade_seconds": 99, "fps": 0,
                               "ken_burns": 0, "monitor": 3,
                               "folders": ["C:\\a", {"path": "C:\\A\\"}, {"path": ""},
                                           {"path": "D:\\b", "recursive": False}, 5]})
    assert bad["interval_seconds"] == 10 and bad["fade_seconds"] == 5     # 表示の半分まで
    assert bad["fps"] == sc.FPS_RANGE[0] and bad["ken_burns"] is False and bad["monitor"] == ""
    assert bad["folders"] == [{"path": "C:\\a", "recursive": True},
                              {"path": "D:\\b", "recursive": False}], bad["folders"]


def test_settings_save_replaces_keys_only():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.json")
        assert sc.read_section(path) == {} and not os.path.exists(path)   # 読むだけでは作らない
        Path(path).write_text(json.dumps({
            "hotkeys": {"filer": "x"}, "slideshow": {"fps": 30, "folders": []}}), encoding="utf-8")
        memory = {"slideshow": {"fps": 30}}
        sc.save_config(path, {"monitor": "\\\\.\\DISPLAY2",
                              "folders": [{"path": "C:\\写真", "recursive": False}]}, memory)
        stored = json.loads(Path(path).read_text(encoding="utf-8"))
        assert stored["hotkeys"] == {"filer": "x"}
        assert stored["slideshow"] == {"fps": 30, "monitor": "\\\\.\\DISPLAY2",
                                       "folders": [{"path": "C:\\写真", "recursive": False}]}
        assert "interval_seconds" not in stored["slideshow"]      # 既定値は焼き込まない
        assert memory["slideshow"]["monitor"] == "\\\\.\\DISPLAY2" and memory["slideshow"]["fps"] == 30
        config = sc.load_config(path)
        assert config["fps"] == 30 and config["interval_seconds"] == 10
        assert config["folders"] == [{"path": "C:\\写真", "recursive": False}]


# ---------------------------------------------------------------
# EXIF(向きと撮影日時。filer と共通の読み取り)
# ---------------------------------------------------------------
def _exif_photo(path, orientation=None, taken=None, size=(40, 20)):
    from PIL import Image
    image = Image.new("RGB", size, (200, 30, 30))
    image.putpixel((0, 0), (0, 0, 255))
    exif = Image.Exif()
    if orientation:
        exif[0x0112] = orientation
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken
    image.save(path, exif=exif.tobytes())
    return str(path)


def test_exif_datetime_and_orientation_shared():
    import filer_preview
    import image_load
    with tempfile.TemporaryDirectory() as tmp:
        for ext in ("jpg", "png", "webp"):
            p = _exif_photo(Path(tmp, f"a.{ext}"), orientation=6, taken="2023:05:01 12:34:56")
            data = core.exif_tiff_bytes(p)
            assert data, ext
            assert core.exif_orientation_from_tiff(data) == 6, ext
            assert core.exif_datetime_original(data) == "2023:05:01 12:34:56", ext
            info = sc.photo_info(p)
            assert info == {"name": f"a.{ext}", "folder": tmp, "taken": "2023/05/01 12:34"}, info
            # 向きは JPEG なら Qt、PNG / WebP なら自前で当たる(縦横が入れ替わる)
            image, original, _scaled = image_load.read_image(p, 1000, 1000)
            assert (image.width(), image.height()) == (20, 40), ext
            assert original == (20, 40), ext
        # container_exif_orientation は今までどおり PNG / WebP だけを見る(JPEG は Qt に任せる)
        assert core.container_exif_orientation(str(Path(tmp, "a.png"))) == 6
        assert core.container_exif_orientation(str(Path(tmp, "a.jpg"))) == 1
        p = _exif_photo(Path(tmp, "plain.jpg"))
        assert core.exif_datetime_original(core.exif_tiff_bytes(p)) == ""
        assert sc.photo_info(p)["taken"] == ""
        assert core.exif_tiff_bytes(_touch(Path(tmp, "broken.jpg"), b"\xff\xd8\xff")) == b""
        assert core.exif_datetime_original(b"garbage") == ""
        # あっふのプレビューは共通の読み込みをそのまま使っている
        assert filer_preview._read_image is image_load.read_image
        assert filer_preview.turned_image is image_load.turned_image
        result = filer_preview.load_preview(str(Path(tmp, "a.webp")), "image", None, box=(100, 100))
        assert result["kind"] == "image" and result["note"] == "20×40", result


def test_load_photo_real_reader_scales_to_box():
    import slideshow
    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        p = _exif_photo(Path(tmp, "big.jpg"), size=(800, 600), taken="2020:01:02 03:04:05")
        image, info = slideshow.load_photo(p, 400, 400)
        assert (image.width(), image.height()) == (400, 300)
        assert info["taken"] == "2020/01/02 03:04"
        try:
            slideshow.load_photo(_touch(Path(tmp, "bad.jpg"), b"not a jpeg"), 100, 100)
            raise AssertionError("壊れた写真が読めてしまった")
        except OSError:
            pass


# ---------------------------------------------------------------
# 窓(offscreen。時刻・乱数・読み込み・画面を消さない設定は差し替える)
# ---------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _color_reader(colors=None, fail=()):
    """ファイル名で色を決めた QImage を返す読み込み(本物の写真を読まない)。"""
    from PySide6.QtGui import QColor, QImage

    def read(path, box_w, box_h):
        name = os.path.basename(path)
        if any(name.startswith(prefix) for prefix in fail):
            raise OSError("読めません")
        image = QImage(160, 90, QImage.Format_RGB32)
        image.fill(QColor((colors or {}).get(name, "#808080")))
        return image, sc.photo_info(path)
    return read


def _make_window(folders, config=None, reader=None, clock=None, awake=None, size=(320, 180)):
    import slideshow
    _qapp()
    base = {"interval_seconds": 10, "fade_seconds": 2, "ken_burns": True, "fps": 60}
    base.update(config or {})
    calls = awake if awake is not None else []
    w = slideshow.SlideshowWindow(base, time_source=clock or _Clock(), rng=random.Random(5),
                                  reader=reader or _color_reader(),
                                  set_awake=lambda on: calls.append(on))
    w.resize(*size)
    w.show()
    w.play(folders)
    return w, calls


def _wait_until(cond, timeout=5.0):
    from PySide6.QtTest import QTest
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        QTest.qWait(10)
    raise AssertionError("待っても条件が満たされない")


def _photo_folder(tmp, names):
    folder = Path(tmp, "photos")
    for name in names:
        _touch(folder / name)
    return str(folder)


def test_window_fade_zoom_and_auto_advance():
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, [f"{i}.jpg" for i in range(5)])
        clock = _Clock()
        w, awake = _make_window([folder], clock=clock)
        try:
            _wait_until(lambda: w.current is not None and w.pending is not None)
            assert awake == [True]
            first = w.current
            assert w.previous is None and w.fade_progress() == 0.0
            clock.t += 1.0
            assert abs(w.fade_progress() - 0.5) < 1e-9
            r0 = w.slide_rect(first, first.start)
            r1 = w.slide_rect(first, first.start + 6.0)
            assert (r0.width(), r0.x()) != (r1.width(), r1.x())     # ズームが進む
            assert w.animating()
            w.advance()
            assert w.timer.interval() == int(1000 / 60)            # 動いている間は 60fps
            clock.t += 9.0                                         # 表示から10秒
            w.advance()
            assert w.current is not first and w.previous is first
            assert w.history.items == [first.path, w.current.path]
            clock.t += 1.0
            alpha = w.fade_progress()
            assert 0.0 < alpha < 1.0
            clock.t += 1.0
            w.advance()
            assert w.previous is None                              # 古い絵は手放す
            assert len({first.path, w.current.path}) == 2
        finally:
            w.close()
        assert awake == [True, False]


def test_window_crossfade_pixels_do_not_dip():
    from PySide6.QtGui import QColor
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, ["r.jpg", "b.jpg"])
        clock = _Clock()
        w, _ = _make_window([folder], config={"ken_burns": False, "fade_seconds": 2},
                            reader=_color_reader({"r.jpg": "#ff0000", "b.jpg": "#0000ff"}),
                            clock=clock, size=(160, 90))
        try:
            _wait_until(lambda: w.current is not None and w.pending is not None)
            clock.t += 2.0
            w.advance()
            assert not w.animating()                       # 止まっている間は描き直さない
            assert w.timer.interval() == 100
            before = QColor(w.grab().toImage().pixel(80, 45))
            clock.t += 8.0
            w.advance()
            clock.t += 1.0                                 # フェードの半分
            mid = QColor(w.grab().toImage().pixel(80, 45))
            old = (before.red(), before.blue())
            assert old in ((255, 0), (0, 255)), old
            # 古い×0.5 ＋ 新しい×0.5。暗くならない(加算合成)
            assert abs(mid.red() - 128) <= 3 and abs(mid.blue() - 128) <= 3, (mid.red(), mid.blue())
        finally:
            w.close()


def test_window_keys_next_prev_pause_info_escape():
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, [f"{i}.jpg" for i in range(6)])
        clock = _Clock()
        w, awake = _make_window([folder], clock=clock)
        try:
            _wait_until(lambda: w.current is not None and w.pending is not None)
            first = w.current.path
            QTest.keyClick(w, Qt.Key_Right)
            assert w.current.path != first and w.previous is not None
            assert w.current.fade == 0.4                       # 手で送るときは短いフェード
            second = w.current.path
            _wait_until(lambda: w.pending is not None)
            QTest.keyClick(w, Qt.Key_Left)                     # 履歴で戻る(読み直し)
            _wait_until(lambda: w.current.path == first)
            QTest.keyClick(w, Qt.Key_Right)                    # 戻った分を進む
            _wait_until(lambda: w.current.path == second)
            # 一時停止: 時刻が進んでも切り替わらない
            QTest.keyClick(w, Qt.Key_Space)
            assert w.paused
            shown = w.current
            clock.t += 100
            w.advance()
            assert w.current is shown and not w.animating()
            QTest.keyClick(w, Qt.Key_Space)
            assert not w.paused
            w.advance()                                        # 止まっていた分は数えない
            assert w.current is shown
            # 情報表示
            QTest.keyClick(w, Qt.Key_I)
            assert w.show_info
            lines = w.info_lines()
            assert lines[0] == os.path.basename(shown.path) and lines[-1] == folder, lines
            w.grab()                                           # 描いても落ちない
            QTest.keyClick(w, Qt.Key_I)
            assert not w.show_info
            QTest.keyClick(w, Qt.Key_Escape)
            assert not w.isVisible() and w.loader is None
            assert awake == [True, False]
        finally:
            w.close()


def test_window_click_closes_and_cursor():
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, ["a.jpg"])
        w, awake = _make_window([folder])
        try:
            assert w.cursor().shape() == Qt.BlankCursor
            QTest.mouseMove(w, QPoint(10, 10))
            QTest.mouseMove(w, QPoint(60, 60))
            _wait_until(lambda: w.cursor().shape() == Qt.ArrowCursor, 1.0)
            w._hide_cursor()
            assert w.cursor().shape() == Qt.BlankCursor
            QTest.mouseClick(w, Qt.LeftButton, Qt.NoModifier, QPoint(50, 50))
            assert not w.isVisible() and awake == [True, False]
        finally:
            w.close()


def test_window_skips_unreadable_photos():
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, ["bad1.jpg", "bad2.jpg", "good.jpg"])
        w, _ = _make_window([folder], reader=_color_reader(fail=("bad",)))
        try:
            _wait_until(lambda: w.current is not None)
            assert os.path.basename(w.current.path) == "good.jpg"
            _wait_until(lambda: w.pending is not None)
            assert os.path.basename(w.pending.path) == "good.jpg"      # 読めないものは外れた
            assert w.loader.pool.counts() == [1]
        finally:
            w.close()


def test_window_empty_and_unregistered_show_message_then_close():
    import slideshow
    real = slideshow.MESSAGE_CLOSE_MS
    slideshow.MESSAGE_CLOSE_MS = 50
    try:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp, "empty")
            _touch(empty / "memo.txt")
            w, awake = _make_window([str(empty), os.path.join(tmp, "無い")])
            _wait_until(lambda: bool(w.message))
            assert "写真が見つかりません" in w.message and "無い" in w.message, w.message
            assert awake == [True, False]                  # 出すものが無ければすぐ解除
            w.grab()
            _wait_until(lambda: not w.isVisible(), 2.0)
            w2, awake2 = _make_window([])
            assert "登録されていません" in w2.message and awake2 == []
            _wait_until(lambda: not w2.isVisible(), 2.0)
    finally:
        slideshow.MESSAGE_CLOSE_MS = real


def test_window_memory_stays_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, [f"{i}.jpg" for i in range(8)])
        clock = _Clock()
        w, _ = _make_window([folder], clock=clock)
        w.history = sc.History(limit=5)
        try:
            for _ in range(15):
                _wait_until(lambda: w.pending is not None)
                clock.t += 12.0
                w.advance()
            held = [s for s in (w.current, w.previous, w.pending) if s is not None]
            assert len(held) <= 3 and len(w.history.items) == 5
        finally:
            w.close()


def test_execution_state_flags_set_and_released():
    import keep_awake
    import slideshow
    _qapp()
    calls = []
    real = keep_awake._SetThreadExecutionState
    keep_awake._SetThreadExecutionState = lambda flags: (calls.append(flags), 1)[1]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            folder = _photo_folder(tmp, ["a.jpg"])
            w = slideshow.SlideshowWindow({}, time_source=_Clock(), reader=_color_reader())
            w.resize(100, 60)
            w.show()
            w.play([folder])
            w.close()
            w.shutdown()                                   # 2回目は何もしない
    finally:
        keep_awake._SetThreadExecutionState = real
    on = keep_awake.ES_CONTINUOUS | keep_awake.ES_SYSTEM_REQUIRED | keep_awake.ES_DISPLAY_REQUIRED
    assert calls == [on, keep_awake.ES_CONTINUOUS], [hex(c) for c in calls]
    # argtypes / restype が付いている(0x80000000 を符号なしで渡す)
    import ctypes
    assert real.argtypes == [ctypes.c_uint32] and real.restype is ctypes.c_uint32


def test_place_on_screen_and_prefetch_box():
    """モニタを名前で選び(無ければ主モニタ)、先読みの大きさは画面×ズームのぶん。"""
    import slideshow
    from PySide6.QtGui import QGuiApplication
    _qapp()
    primary = QGuiApplication.primaryScreen()
    assert slideshow.pick_screen("") is primary
    assert slideshow.pick_screen(r"\\.\NO-SUCH-DISPLAY") is primary
    assert slideshow.pick_screen(primary.name()) is primary
    w = slideshow.SlideshowWindow({"ken_burns": True}, time_source=_Clock(),
                                  reader=_color_reader(), set_awake=lambda on: None)
    try:
        w.place_on(primary)
        geometry, ratio = primary.geometry(), primary.devicePixelRatio()
        assert w.box() == (int(geometry.width() * ratio * sc.ZOOM_MAX),
                           int(geometry.height() * ratio * sc.ZOOM_MAX)), w.box()
        w.config["ken_burns"] = False
        assert w.box() == (int(geometry.width() * ratio), int(geometry.height() * ratio))
    finally:
        w.close()


def test_command_server_handles_play_config_quit():
    import slideshow
    with tempfile.TemporaryDirectory() as tmp:
        folder = _photo_folder(tmp, ["a.jpg", "b.jpg"])
        other = Path(tmp, "other")
        _touch(other / "z.jpg")
        settings_path = os.path.join(tmp, "settings.json")
        Path(settings_path).write_text(json.dumps({"slideshow": {
            "folders": [{"path": folder, "recursive": False}], "fps": 30}}), encoding="utf-8")
        w, _ = _make_window([folder])
        server = slideshow.CommandServer.__new__(slideshow.CommandServer)
        server.window, server.settings_path = w, settings_path
        w.place_on = lambda screen: None
        try:
            server.handle({"cmd": "config", "settings": settings_path})
            assert w.config["fps"] == 30
            server.handle({"cmd": "play", "folders": [str(other)]})
            _wait_until(lambda: w.current is not None)
            assert os.path.basename(w.current.path) == "z.jpg" and w.folders == [str(other)]
            server.handle({"cmd": "quit"})
            assert not w.isVisible()
        finally:
            w.close()


# ---------------------------------------------------------------
# 起動(別プロセス)
# ---------------------------------------------------------------
def test_launch_commands_and_spawn():
    import slideshow_launch as sl
    payload = sl.command_bytes("play", ["D:\\写真 2024"], "C:\\s.json")
    assert payload.endswith(b"\n")
    assert sl.parse_command(payload) == {"cmd": "play", "folders": ["D:\\写真 2024"],
                                         "settings": "C:\\s.json"}
    assert sl.parse_command(sl.command_bytes("play"))["folders"] is None
    assert sl.parse_command(sl.command_bytes("quit")) == {"cmd": "quit"}
    for bad in (b"", b"show\n", b'{"cmd":"rm"}', b'{"cmd":"play","folders":[1]}'):
        assert sl.parse_command(bad) is None, bad

    calls, sent = [], []
    # subprocess モジュールそのものの Popen は差し替えない(ほかのスレッドも使う)。
    # slideshow_launch から見える subprocess だけを偽物にする。
    from types import SimpleNamespace
    real = (sl.subprocess, sl.is_running, sl.send)
    sl.subprocess = SimpleNamespace(Popen=lambda argv, **kw: (calls.append((argv, kw)),
                                                              SimpleNamespace(pid=1))[1])
    try:
        sl.is_running = lambda: False
        sl.send = lambda data, timeout_ms=1000: (sent.append(data), True)[1]
        assert sl.start(["D:\\写真"], "C:\\s.json") == "spawned"
        argv, kw = calls[0]
        assert argv[1].endswith("slideshow.py")
        assert argv[2:] == ["--folder", "D:\\写真", "--settings", "C:\\s.json"], argv
        assert kw["creationflags"] & 0x00000008          # DETACHED_PROCESS
        assert sl.stop() is False and not sent
        sl.is_running = lambda: True
        assert sl.start(None, "C:\\s.json") == "sent" and len(calls) == 1
        assert sl.parse_command(sent[-1])["folders"] is None
        assert sl.stop() is True and sl.parse_command(sent[-1]) == {"cmd": "quit"}
        assert sl.reload_config("C:\\s.json") and sl.parse_command(sent[-1])["cmd"] == "config"
        sl.send = lambda data, timeout_ms=1000: False   # 動いているのに繋がらない → 起こす
        assert sl.start() == "spawned" and len(calls) == 2
    finally:
        sl.subprocess, sl.is_running, sl.send = real


def test_filer_folder_menu_starts_slideshow():
    """あっふの Ctrl+Enter(フォルダ)に「このフォルダでスライドショー」が出て、選ぶと
    そのフォルダを --folder に付けて別プロセスを起こす(起動は差し替え)。書庫の中では出ない。"""
    import filer
    import filer_extmenu as m
    import slideshow_launch as sl
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    lines = m.read_menu_file(m.MENU_DIR / "folder.txt")
    item = [l for l in lines if "SLIDESHOW" in l.command]
    assert item and item[0].key == "L" and item[0].command == "&SLIDESHOW $MF", item
    resolver = m.Resolver(tools={}, app_path=lambda n: None, which=lambda n: None,
                          exists=lambda p: False)
    assert any(l.label == item[0].label for l in m.visible_items(lines, None, resolver))
    assert not any(l.label == item[0].label
                   for l in m.visible_items(lines, None, resolver, in_archive=True))

    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        left, right = Path(tmp, "left"), Path(tmp, "right")
        _touch(left / "旅行 2024" / "a.jpg")
        right.mkdir()
        w = filer.FilerWindow({"filer": {"left_path": str(left), "right_path": str(right),
                                         "editor": "x"}}, None)
        w.menu_local_dir = os.path.join(tmp, "localmenus")
        calls = []
        from types import SimpleNamespace
        real = (sl.subprocess, sl.is_running)
        sl.subprocess = SimpleNamespace(Popen=lambda argv, **kw: (calls.append(list(argv)),
                                                                  SimpleNamespace(pid=1))[1])
        sl.is_running = lambda: False
        try:
            w.show()
            w.start()
            deadline = time.monotonic() + 5
            while (w.left.loading or w.right.loading) and time.monotonic() < deadline:
                QTest.qWait(20)
            w.left.set_cursor(w.left._row_of("旅行 2024"))
            seen = []

            def drive(menu, chosen):
                fired = []
                menu.triggered.connect(lambda action: fired.append(chosen.get(action)))
                seen.append([a.text() for a in menu.actions() if not a.isSeparator()])
                menu.popup(w.mapToGlobal(QPoint(40, 40)))
                QTest.qWait(30)
                QTest.keyClick(menu, Qt.Key_L)
                QTest.keyClick(menu, Qt.Key_Return)
                QTest.qWait(30)
                menu.hide()
                return fired[0] if fired else None
            w._exec_menu = drive
            QTest.keyClick(w.active.view, Qt.Key_Return, Qt.ControlModifier)
            assert any("スライドショー" in t for t in seen[0]), seen
            assert len(calls) == 1, calls
            argv = calls[0]
            assert argv[1].endswith("slideshow.py")
            assert argv[2:] == ["--folder", str(left / "旅行 2024")], argv
        finally:
            sl.subprocess, sl.is_running = real
            w.close()
            w.deleteLater()


# ---------------------------------------------------------------
# 設定ダイアログ・トレイのメニュー
# ---------------------------------------------------------------
def test_settings_dialog_keys_and_save():
    import slideshow_settings
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    _qapp()
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = os.path.join(tmp, "settings.json")
        a = Path(tmp, "A")
        b = Path(tmp, "B")
        a.mkdir()
        b.mkdir()
        Path(settings_path).write_text(json.dumps({
            "hotkeys": {"filer": ""},
            "slideshow": {"folders": [{"path": str(a), "recursive": True},
                                      {"path": os.path.join(tmp, "消えた"), "recursive": False}],
                          "fps": 45}}), encoding="utf-8")
        memory = {}
        d = slideshow_settings.SlideshowSettingsDialog(
            sc.load_config(settings_path), settings_path, memory,
            choose_folder=lambda: str(b))
        try:
            d.show()
            assert d.list.count() == 2 and "見つかりません" in d.list.item(1).text()
            assert d.recursive_box.isChecked()
            assert d.add_folder() is True and d.list.currentRow() == 2
            assert d.add_folder(str(b) + "\\") is False and d.list.count() == 3   # 重ねない
            # Space で行のチェック(サブフォルダも含める)を切り替える
            d.list.setFocus()
            d.list.setCurrentRow(0)
            QTest.keyClick(d.list, Qt.Key_Space)
            assert d.list.item(0).checkState() == Qt.Unchecked
            assert not d.recursive_box.isChecked()
            # 下のチェックボックスでも同じ
            d.list.setCurrentRow(2)
            d.recursive_box.setChecked(False)
            assert d.list.item(2).checkState() == Qt.Unchecked
            # Del で削除
            d.list.setCurrentRow(1)
            QTest.keyClick(d.list, Qt.Key_Delete)
            assert d.list.count() == 2
            d.interval.setValue(7)
            d.fade.setValue(1.0)
            d.ken_burns.setChecked(False)
            d.accept()
        finally:
            d.close()
        stored = json.loads(Path(settings_path).read_text(encoding="utf-8"))
        assert stored["hotkeys"] == {"filer": ""}
        assert stored["slideshow"] == {
            "folders": [{"path": str(a), "recursive": False},
                        {"path": str(b), "recursive": False}],
            "fps": 45, "interval_seconds": 7.0, "fade_seconds": 1.0, "ken_burns": False}
        assert memory["slideshow"]["interval_seconds"] == 7.0


def test_tray_slideshow_menu_refresh_and_monitor():
    """トレイの「🖼 スライドショー」: 開く直前に停止の可否とモニタの一覧を作り直し、
    モニタを選ぶと slideshow.monitor だけを書く。ScreenFeature 全体は作らず、
    メソッドを最小の偽物に当てて確かめる。"""
    try:
        import feature_screen
    except ImportError as e:      # 常駐の依存(pycaw など)が無い環境では見られない
        print(f"skip  test_tray_slideshow_menu_refresh_and_monitor: {e}")
        return
    import slideshow_launch as sl
    from types import SimpleNamespace
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu
    _qapp()

    class FakeScreen:
        def __init__(self, name):
            self._name = name

        def name(self):
            return self._name

    with tempfile.TemporaryDirectory() as tmp:
        settings_path = os.path.join(tmp, "settings.json")
        Path(settings_path).write_text(json.dumps({"hotkeys": {}}), encoding="utf-8")
        fake = SimpleNamespace(
            app_settings={"slideshow": {"monitor": "\\\\.\\GONE"}},
            settings_path=settings_path,
            _slideshow_stop_action=QAction("stop"),
            _slideshow_screen_menu=QMenu(),
            _slideshow_screen_actions=[],
            _log_failure=lambda where: (_ for _ in ()).throw(AssertionError(where)),
            _notify=lambda *a: None)
        fake._set_slideshow_monitor = lambda name: feature_screen.ScreenFeature._set_slideshow_monitor(fake, name)
        real = (feature_screen.screen_mirror.available_screens, feature_screen.screen_mirror.screen_label,
                sl.is_running, sl.send)
        feature_screen.screen_mirror.available_screens = lambda: [FakeScreen("\\\\.\\D1"), FakeScreen("\\\\.\\D2")]
        feature_screen.screen_mirror.screen_label = lambda s: s.name()
        sl.is_running = lambda: False
        sl.send = lambda data, timeout_ms=1000: True
        try:
            feature_screen.ScreenFeature._refresh_slideshow_menu(fake)
            texts = [a.text() for a in fake._slideshow_screen_menu.actions() if not a.isSeparator()]
            assert texts == ["主モニタ", "\\\\.\\D1", "\\\\.\\D2"], texts
            assert fake._slideshow_screen_actions[0].isChecked()      # 外れたモニタ → 主モニタ
            assert not fake._slideshow_stop_action.isEnabled()
            fake._slideshow_screen_actions[2].trigger()
            stored = json.loads(Path(settings_path).read_text(encoding="utf-8"))
            assert stored == {"hotkeys": {}, "slideshow": {"monitor": "\\\\.\\D2"}}, stored
            assert fake.app_settings["slideshow"]["monitor"] == "\\\\.\\D2"
            sl.is_running = lambda: True
            feature_screen.ScreenFeature._refresh_slideshow_menu(fake)
            assert fake._slideshow_stop_action.isEnabled()
            assert fake._slideshow_screen_actions[2].isChecked()
        finally:
            (feature_screen.screen_mirror.available_screens, feature_screen.screen_mirror.screen_label,
             sl.is_running, sl.send) = real


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
            if os.environ.get("GITHUB_ACTIONS"):
                tb = traceback.format_exc().replace("%", "%25").replace("\r", "%0D")
                print(f"::error title={name}::" + tb[-900:].replace("\n", "%0A"))
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::notice::{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    if os.environ.get("GITHUB_ACTIONS"):
        try:
            os.dup2(1, 2)
            sys.stderr = sys.stdout
        except Exception:  # noqa: BLE001
            pass
    code = main()
    shutil.rmtree(_TEST_TEMP, ignore_errors=True)
    sys.stdout.flush()
    sys.stderr.flush()
    # test_filer.py と同じ終わり方(Qt の静的デストラクタで落ちて終了コードが化けないように)
    try:
        import ctypes
        _k32 = ctypes.WinDLL("kernel32")
        _k32.GetCurrentProcess.restype = ctypes.c_void_p
        _k32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _k32.TerminateProcess.restype = ctypes.c_int
        _k32.TerminateProcess(_k32.GetCurrentProcess(), code)
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
