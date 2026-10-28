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
