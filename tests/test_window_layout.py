# tests/test_window_layout.py
# 窓の配置(window_layout.py の純ロジック)を確かめる。OS とのやり取り
# (window_tools の列挙・移動)は実機でしか確かめられないので、ここでは
# 計画(どの窓をどこへ)と照合だけを見る。
#
#   %USERPROFILE%\.venvs\tray-tools\Scripts\python.exe tests\test_window_layout.py
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import window_layout as wl  # noqa: E402

MONITORS = [(0, 0, 1920, 1040), (1920, 0, 3840, 1040), (-1920, 0, 0, 1040)]


def _win(hwnd, title, exe, bounds, zoomed=False, rect=None):
    return {"hwnd": hwnd, "title": title, "exe": exe,
            "rect": rect if rect is not None else bounds,
            "bounds": bounds, "zoomed": zoomed}


def test_monitor_helpers():
    assert wl.monitor_at((100, 100), MONITORS) == 0
    assert wl.monitor_at((2500, 100), MONITORS) == 1
    assert wl.monitor_at((-100, 100), MONITORS) == 2
    # どのモニタにも無ければ一番近いもの
    assert wl.monitor_at((5000, 100), MONITORS) == 1
    assert wl.monitor_at((100, 100), []) is None
    assert wl.window_monitor((100, 100, 900, 700), MONITORS) == 0
    # 2つのモニタに跨る窓は、大きく重なる方
    assert wl.window_monitor((1800, 0, 2600, 500), MONITORS) == 1
    # どこにも重ならない(取り残された窓)
    assert wl.window_monitor((5000, 100, 5800, 700), MONITORS) is None


def test_plan_gather():
    wins = [
        _win(1, "A", "C:\\a\\a.exe", (100, 100, 900, 700)),
        _win(2, "B", "C:\\b\\b.exe", (2100, 50, 2900, 650)),
        _win(3, "C", "C:\\c\\c.exe", (-1920, 0, 0, 1040), zoomed=True,
             rect=(-1800, 100, -200, 900)),
        _win(4, "D", "C:\\d\\d.exe", (5000, 100, 5800, 700)),   # 取り残された窓
        _win(5, "E", "C:\\e\\e.exe", (2000, 100, 3900, 1200)),  # 集約先より背が高い
    ]
    moves, target = wl.plan_gather(wins, MONITORS, (100, 100))
    assert target == 0
    by = {m["hwnd"]: m for m in moves}
    assert 1 not in by                                    # 集約先の窓は触らない
    assert by[2]["rect"] == (0, 0, 800, 600)              # 大きさは保ち、左上から
    assert by[3]["zoomed"] and by[3]["rect"] == MONITORS[0]   # 最大化は最大化のまま
    assert 4 in by                                        # 取り残された窓も集める
    # 作業領域を超える分は収まるよう縮む(幅 1900 はそのまま、高さ 1100→1040)
    assert by[5]["rect"][2] - by[5]["rect"][0] == 1900
    assert by[5]["rect"][3] - by[5]["rect"][1] == 1040
    # ずらしは窓ごとに進む(重なって隠れない)
    assert by[2]["rect"][:2] != by[4]["rect"][:2]
    # カーソルが別のモニタならそちらが集約先
    moves2, target2 = wl.plan_gather(wins, MONITORS, (2500, 100))
    assert target2 == 1 and all(m["hwnd"] != 2 for m in moves2)
    # モニタが無ければ何もしない
    assert wl.plan_gather(wins, [], (0, 0)) == ([], None)


def test_undo_moves():
    wins = [_win(1, "A", "C:\\a\\a.exe", (100, 100, 900, 700)),
            _win(2, "B", "C:\\b\\b.exe", (2100, 50, 2900, 650), zoomed=True,
                 rect=(2200, 60, 2800, 600))]
    moves = [{"hwnd": 2, "title": "B", "rect": (0, 0, 1920, 1040), "zoomed": True},
             {"hwnd": 99, "title": "消えた窓", "rect": (0, 0, 10, 10), "zoomed": False}]
    undo = wl.undo_moves(wins, moves)
    # 元の通常時矩形と最大化状態へ戻す。すでに無い窓は飛ばす
    assert undo == [{"hwnd": 2, "title": "B", "rect": (2200, 60, 2800, 600),
                     "zoomed": True}], undo


def test_presets_normalize_and_match():
    raw = [{"name": "作業", "items": [
                {"exe": "C:\\b\\b.exe", "title": "B - メモ帳", "rect": [10, 20, 810, 620],
                 "zoomed": False},
                {"exe": "C:\\c\\c.exe", "title": "", "rect": [0, 0, 1920, 1040],
                 "zoomed": True}]},
           {"name": "壊れた項目", "items": [{"exe": 1, "rect": [1, 2]}]},
           {"items": []},
           "文字列"]
    presets = wl.normalize_presets(raw)
    assert len(presets) == 1 and presets[0]["name"] == "作業", presets
    assert presets[0]["items"][0]["rect"] == [10, 20, 810, 620]
    wins = [_win(1, "A", "C:\\a\\a.exe", (0, 0, 100, 100)),
            _win(2, "B - メモ帳", "c:\\B\\B.EXE", (0, 0, 100, 100)),
            _win(3, "C の窓", "C:\\c\\c.exe", (0, 0, 100, 100)),
            _win(4, "B - メモ帳", "C:\\b\\b.exe", (0, 0, 100, 100))]
    moves, missing = wl.match_preset(wins, presets[0]["items"])
    assert missing == 0
    by = {m["hwnd"]: m for m in moves}
    assert by[2]["rect"] == (10, 20, 810, 620)     # 最初に合った窓を使う(4 ではない)
    assert by[3]["zoomed"] is True
    # タイトルが変わっていても前方一致なら同じ窓とみる
    wins2 = [_win(7, "B - メモ帳 (2)", "C:\\b\\b.exe", (0, 0, 100, 100))]
    moves2, missing2 = wl.match_preset(wins2, presets[0]["items"][:1])
    assert len(moves2) == 1 and missing2 == 0
    # 開いていない窓は missing に数える
    moves3, missing3 = wl.match_preset(wins[:1], presets[0]["items"])
    assert moves3 == [] and missing3 == 2
    # exe が違えばタイトルが同じでも別物
    wins4 = [_win(8, "B - メモ帳", "C:\\x\\x.exe", (0, 0, 100, 100))]
    assert wl.match_preset(wins4, presets[0]["items"][:1]) == ([], 1)
    # preset_item は列挙した窓から作る
    item = wl.preset_item(_win(9, "T", "C:\\t\\t.exe", (1, 2, 3, 4), zoomed=True))
    assert item == {"exe": "C:\\t\\t.exe", "title": "T", "rect": [1, 2, 3, 4],
                    "zoomed": True}


def test_layout_target_excludes_shell_and_tool_windows():
    # 2026-11-06 実機: 2画面目のタスクバーが「集める」で動いてしまった
    assert not wl.is_layout_target("Shell_SecondaryTrayWnd", 0, "")
    assert not wl.is_layout_target("Shell_SecondaryTrayWnd", 0, "タスクバー")
    assert not wl.is_layout_target("Shell_TrayWnd", 0, "")
    assert not wl.is_layout_target("Progman", 0, "Program Manager")
    assert not wl.is_layout_target("Chrome_WidgetWin_1", wl.WS_EX_TOOLWINDOW, "浮き窓")
    assert not wl.is_layout_target("Chrome_WidgetWin_1", wl.WS_EX_NOACTIVATE, "通知")
    assert not wl.is_layout_target("Notepad", 0, "   ")
    assert wl.is_layout_target("Notepad", 0, "メモ - メモ帳")
    assert wl.is_layout_target("Chrome_WidgetWin_1", 0x100, "Edge")  # WS_EX_WINDOWEDGE は可


def main() -> int:
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL  {name}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
