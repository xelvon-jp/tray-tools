# tests/test_editor_restart_blockers.py
# 未保存のエディタを、再起動・終了の巻き添え判定に数える件(レビュー
# req-20261113-99 の重大3)を確かめる。
#
#   - 判定の部品 editor.dirty_window_state(dirty / clean / 読めない窓)
#   - feature_screen.ScreenFeature.restart_blockers への組み込み
#
# filer.FilerWindow.confirm_losses は filer.py が VM では import
# できない(ctypes.WinDLL 等のモジュールレベルの Windows 依存)ため、
# ここでは組み込み先の判定部品までを担保し、confirm_losses 本体は
# 実機確認待ちとする。判定は dirty_window_state に集めてあるので、
# そちらが通れば両方の組み込み先で同じ答えになる。
#
# feature_screen の import には mss が要る。VM に無いので、テストの
# 中だけ空のモジュールを差す(実機では本物が入る。capture 系の実処理は
# このテストでは呼ばない)。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_restart_blockers.py
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import mss  # noqa: F401
except ImportError:
    sys.modules["mss"] = types.ModuleType("mss")

from PySide6.QtWidgets import QApplication  # noqa: E402

import editor  # noqa: E402
import feature_screen  # noqa: E402
from editor import session as sessionmod  # noqa: E402

APP = None


def app():
    global APP
    if APP is None:
        APP = QApplication([])
    return APP


def make_window():
    app()
    window = editor.create_window()
    window.show()
    return window


def type_text(window, text):
    window._edit.textCursor().insertText(text)


def close_window(window):
    window.ask_save_changes = lambda: "discard"
    window.close()
    window.deleteLater()
    app().processEvents()


class BrokenWindow:
    """状態を読むと落ちる窓の代わり。"""

    def is_dirty(self):
        raise RuntimeError("読めない")


def test_dirty_window_state_counts():
    window = make_window()
    try:
        assert editor.dirty_window_state([window]) == (0, 0)
        assert editor.dirty_window_state([]) == (0, 0)
        assert editor.dirty_window_state(None) == (0, 0)
        assert editor.dirty_window_state([None]) == (0, 0)
        type_text(window, "x")
        assert window.is_dirty()
        assert editor.dirty_window_state([window]) == (1, 0)
        assert editor.dirty_window_state([window, window]) == (2, 0)
    finally:
        close_window(window)


def test_dirty_window_state_unreadable():
    assert editor.dirty_window_state([BrokenWindow()]) == (0, 1)
    window = make_window()
    try:
        type_text(window, "x")
        assert editor.dirty_window_state([BrokenWindow(), window]) == (1, 1)
    finally:
        close_window(window)


def _make_feature(text_editor):
    """ScreenFeature を __new__ で作り、restart_blockers が触る
    相手だけを差す(構築は VM ではできないので、判定の確認に絞る)。"""
    feature = feature_screen.ScreenFeature.__new__(
        feature_screen.ScreenFeature)
    feature.screen_mirror = types.SimpleNamespace(is_active=lambda: False)
    feature._agent_loop_active = False
    feature._teams_meeting_running = lambda: False
    feature.sleep_seconds_left = lambda: None
    feature.text_editor = text_editor
    return feature


def _editor_blockers(feature):
    return [b for b in feature.restart_blockers() if "EDDy" in b]


def test_restart_blockers_counts_dirty_editor():
    window = make_window()
    try:
        feature = _make_feature(window)
        assert _editor_blockers(feature) == []  # 未編集なら数えない
        type_text(window, "x")
        assert _editor_blockers(feature) == ["未保存のEDDy 1窓"]
    finally:
        close_window(window)


def test_restart_blockers_counts_session_window_with_failed_save():
    """控えの書き込みが失敗しているセッション窓だけは、再起動の
    妨げに数える。書けている間の未保存タブは数えない。"""
    with tempfile.TemporaryDirectory() as d:
        window = editor.create_window(
            session_path=str(Path(d) / "editor_session.json"))
        window.show()
        try:
            feature = _make_feature(window)
            type_text(window, "x")
            assert _editor_blockers(feature) == []
            original = sessionmod.save

            def broken(path):
                raise OSError("書けない(テスト)")

            sessionmod.save = broken
            try:
                assert window._flush_session() is False
                assert _editor_blockers(feature) == ["未保存のEDDy 1窓"]
            finally:
                sessionmod.save = original
            assert window._flush_session() is True
            assert _editor_blockers(feature) == []
        finally:
            close_window(window)


def test_restart_blockers_without_editor():
    feature = _make_feature(None)
    assert _editor_blockers(feature) == []


def test_restart_blockers_unreadable_editor():
    feature = _make_feature(BrokenWindow())
    assert _editor_blockers(feature) == ["EDDy(状態を読めず)"]


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
