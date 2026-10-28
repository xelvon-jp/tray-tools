# tests/test_screen_mirror.py
# screen_mirror.py の「保存した設定(シーン)」まわりを確かめる。実機のモニタには
# 頼らず、設定の辞書と偽のミラー窓で組み立てる。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_screen_mirror.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_diagnostics.py と同じ形。pytest があればそのまま拾える)。
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRect  # noqa: E402

import screen_mirror as sm  # noqa: E402


def test_scene_entries_validation():
    """書き損じ・同名・大きさ0を落とし、座標は整数に寄せる。"""
    settings = {"screen_mirror": {"scenes": [
        {"name": "会議用", "x": -1920, "y": 32, "width": 1920, "height": 1080,
         "target_screen": "\\\\.\\DISPLAY2"},
        {"name": "会議用", "x": 0, "y": 0, "width": 100, "height": 100},
        {"name": "壊れ", "width": 0, "height": 100},
        {"name": "", "width": 100, "height": 100},
        {"name": "文字列混じり", "x": "10", "y": None, "width": "800", "height": 600},
        "not-a-dict",
    ]}}
    scenes = sm.scene_entries(settings)
    assert [s["name"] for s in scenes] == ["会議用", "文字列混じり"]
    assert scenes[0]["x"] == -1920 and scenes[0]["target_screen"] == "\\\\.\\DISPLAY2"
    assert scenes[1] == {"name": "文字列混じり", "x": 10, "y": 0, "width": 800,
                         "height": 600, "target_screen": ""}
    assert sm.scene_entries({}) == []
    assert sm.scene_entries({"screen_mirror": {"scenes": "x"}}) == []
    rect = sm.scene_rect(scenes[0])
    assert (rect.x(), rect.y(), rect.width(), rect.height()) == (-1920, 32, 1920, 1080)


class _FakeMirror:
    """MirrorController が触る最小限のミラー窓(source_rect と screen_name と
    静止の状態だけ)。"""

    def __init__(self, rect, screen_name):
        self.source_rect = QRect(rect)
        self.screen_name = screen_name
        self.frozen = False


def _controller(settings):
    controller = sm.MirrorController(settings, settings_path=None,
                                     notify=lambda *_args: None)
    return controller


def test_scene_save_and_delete():
    """動作中の状態を保存し、同名は置き換わり、削除で消える。保存先は
    app_settings の screen_mirror.scenes(settings_path=None なので
    ファイルには書かれない)。"""
    settings = {"screen_mirror": {}}
    controller = _controller(settings)
    assert controller.save_scene("会議用") is False   # 動作中でないと保存できない
    controller._mirror = _FakeMirror(QRect(10, 42, 1600, 900), "\\\\.\\DISPLAY2")
    assert controller.save_scene("会議用") is True
    scenes = controller.saved_scenes()
    assert scenes == [{"name": "会議用", "x": 10, "y": 42, "width": 1600,
                       "height": 900, "target_screen": "\\\\.\\DISPLAY2"}]
    controller._mirror = _FakeMirror(QRect(0, 0, 1280, 720), "\\\\.\\DISPLAY2")
    assert controller.save_scene("会議用") is True    # 同名は置き換え
    scenes = controller.saved_scenes()
    assert len(scenes) == 1 and scenes[0]["width"] == 1280
    assert controller.save_scene("配信用") is True
    assert [s["name"] for s in controller.saved_scenes()] == ["会議用", "配信用"]
    assert controller.delete_scene("会議用") is True
    assert [s["name"] for s in controller.saved_scenes()] == ["配信用"]
    assert controller.delete_scene("会議用") is False  # もう無い
    assert settings["screen_mirror"]["scenes"][0]["name"] == "配信用"


def test_apply_scene_starts_with_saved_rect():
    """停止中の呼び出しは、保存した範囲で開始する(ミラー先は対象モニタが
    無ければ自動選択に落ちる)。"""
    settings = {"screen_mirror": {"scenes": [
        {"name": "会議用", "x": 0, "y": 32, "width": 800, "height": 600,
         "target_screen": ""}]}}
    controller = _controller(settings)
    started = []
    controller._start_mirror = lambda rect: started.append(QRect(rect)) or True
    assert controller.apply_scene("会議用") is True
    assert len(started) == 1
    assert (started[0].x(), started[0].y(),
            started[0].width(), started[0].height()) == (0, 32, 800, 600)
    assert controller.apply_scene("無い名前") is False


def test_notes_drag_zone_and_apply():
    """カンペのドラッグ: 端の判定、移動の画面端クランプ、撮影範囲への
    侵入防止(軸ごと)、伸縮の最小サイズと侵入防止。"""
    from PySide6.QtCore import QPoint, Qt

    zone = sm.notes_drag_zone
    assert zone(QPoint(100, 30), 380, 600) == "move"
    assert zone(QPoint(2, 30), 380, 600) == "l"
    assert zone(QPoint(378, 598), 380, 600) == "br"
    assert zone(QPoint(200, 2), 380, 600) == "t"
    assert sm.notes_zone_cursor("br") == Qt.SizeFDiagCursor
    assert sm.notes_zone_cursor("move") == Qt.ArrowCursor

    area = QRect(0, 0, 1920, 1080)
    apply_ = sm.notes_drag_apply
    start = QRect(1500, 100, 380, 600)
    r = apply_(start, "move", -100, 50, area)
    assert (r.x(), r.y()) == (1400, 150)
    r = apply_(start, "move", 1000, 0, area)
    assert r.right() == 1919                      # 画面の外へは出ない

    source = QRect(100, 100, 800, 600)            # 撮影範囲
    start = QRect(1000, 100, 380, 600)
    r = apply_(start, "move", -300, 0, area, avoid=(source,))
    assert r.x() == 1000                          # 掛かる方向には動かない
    r = apply_(start, "move", -300, 50, area, avoid=(source,))
    assert (r.x(), r.y()) == (1000, 150)          # 掛からない軸だけ動く

    r = apply_(QRect(100, 100, 380, 600), "r", 120, 0, area)
    assert r.width() == 500
    r = apply_(QRect(100, 100, 380, 600), "l", 300, 0, area)
    assert r.width() == sm.NOTES_MIN_WIDTH and r.left() == 240
    r = apply_(QRect(100, 100, 380, 600), "b", 0, -1000, area)
    assert r.height() == sm.NOTES_MIN_HEIGHT
    r = apply_(QRect(100, 100, 380, 600), "r", 2000, 0, area,
               avoid=(QRect(600, 0, 500, 1080),))
    assert r.width() == 380                       # 避ける矩形へは広げない


def test_saved_notes_rect():
    """手で置いた場所の持ち越し: 使える形だけが残り、撮影範囲に掛かる
    保存値は自動配置へ落ちる(None)。"""
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    screen_area = None
    from PySide6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    assert screen is not None
    screen_area = screen.geometry()
    # 画面の左上に収まる小さな保存値で確かめる(実画面の大きさに依らない)
    w = min(380, screen_area.width())
    h = min(400, screen_area.height())
    settings = {"screen_mirror": {"notes_rect": [
        screen_area.left(), screen_area.top(), w, h]}}
    far = QRect(screen_area.right() - 50, screen_area.bottom() - 50, 40, 40)
    got = sm.saved_notes_rect(settings, far)
    assert got is not None and got.width() == max(w, sm.NOTES_MIN_WIDTH)
    # 撮影範囲に重なる保存値は使えない
    assert sm.saved_notes_rect(settings, QRect(got)) is None
    assert sm.saved_notes_rect({}, far) is None
    assert sm.saved_notes_rect({"screen_mirror": {"notes_rect": []}}, far) is None
    assert sm.saved_notes_rect({"screen_mirror": {"notes_rect": [1, 2]}}, far) is None


def test_pill_geometry():
    """再開ボタンは画面(タスクバーを除いた範囲)の左下に、余白を空けて置く。"""
    g = sm.pill_geometry(QRect(0, 0, 1920, 1040))
    assert (g.width(), g.height()) == (sm.PILL_WIDTH, sm.PILL_HEIGHT)
    assert g.left() == sm.PILL_MARGIN
    assert g.bottom() == 1040 - sm.PILL_MARGIN
    g = sm.pill_geometry(QRect(100, 50, 1280, 720))
    assert g.left() == 100 + sm.PILL_MARGIN
    assert g.bottom() == 50 + 720 - sm.PILL_MARGIN


def test_minimize_restore():
    """最小化: 静止+道具が隠れる+ボタンが出る。再開で全部戻って静止が解ける。
    最小化中の静止解除は「戻る」として扱い、終了でボタンも畳む。"""
    controller = sm.MirrorController({"screen_mirror": {}}, "x",
                                     lambda *args: None)

    class _FakeMirror:
        def __init__(self):
            self.frozen = False
            self.source_rect = QRect(0, 0, 100, 100)

        def set_frozen(self, value):
            self.frozen = bool(value)

        def close(self):
            pass

        def deleteLater(self):
            pass

    class _FakeWindow:
        def __init__(self):
            self.visible = True

        def hide(self):
            self.visible = False

        def show(self):
            self.visible = True

        def set_frozen(self, value):
            pass

        def refresh_state(self):
            pass

        def close(self):
            pass

        def deleteLater(self):
            pass

    controller._mirror = _FakeMirror()
    controller._frame = _FakeWindow()
    controller._toolbar = _FakeWindow()
    controller._notes = _FakeWindow()
    controller.is_active = lambda: True

    assert controller.minimize() is True
    assert controller.is_minimized()
    assert controller._mirror.frozen is True
    assert not controller._frame.visible
    assert not controller._toolbar.visible
    assert not controller._notes.visible
    assert controller._pill is not None
    assert controller.minimize() is False          # 二重にはならない

    # 最小化中に静止を解く操作 = 戻る
    assert controller.set_freeze(False) is False
    assert not controller.is_minimized()
    assert controller._frame.visible and controller._toolbar.visible
    assert controller._mirror.frozen is False
    assert controller._pill is None

    # 最小化したまま終了したら、ボタンも残らない
    assert controller.minimize() is True
    controller.stop()
    assert not controller.is_minimized()
    assert controller._pill is None


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
