# tests/test_copilot_status.py
# Copilot 状態監視の「手番の知らせ」とバー表示の保存、右下パネルへの相乗りを確かめる。
# UIA には触らない(手番ファイルの読み書きと純粋な組み立てだけ)。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_copilot_status.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_claude_status.py と同じ形)。
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import copilot_watchdog as cw  # noqa: E402
import claude_progress as cp  # noqa: E402
import settings as settings_module  # noqa: E402


def _write_turn(path, **overrides):
    payload = {"state": "responding", "app": "M365 Copilot", "hwnd": 12345,
               "since": time.time() - 5, "paused": False, "alert": False,
               "updated": time.time()}
    payload.update(overrides)
    Path(path).write_text(json.dumps(payload, ensure_ascii=False),
                          encoding="utf-8")


# -- read_turn_status ----------------------------------------------------

def test_read_turn_status_fresh():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "turn.json"
        _write_turn(path)
        got = cw.read_turn_status(path)
        assert got is not None
        assert got["state"] == "responding"
        assert got["app"] == "M365 Copilot"
        assert got["alert"] is False
        assert got["hwnd"] == 12345


def test_read_turn_status_stale_is_none():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "turn.json"
        _write_turn(path, updated=time.time() - cw.TURN_STALE_SECONDS - 1)
        assert cw.read_turn_status(path) is None


def test_read_turn_status_paused_and_unknown_and_broken():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "turn.json"
        _write_turn(path, paused=True)
        assert cw.read_turn_status(path) is None
        _write_turn(path, state="something_else")
        assert cw.read_turn_status(path) is None
        Path(path).write_text("{壊れた", encoding="utf-8")
        assert cw.read_turn_status(path) is None
        assert cw.read_turn_status(Path(d) / "無い.json") is None


# -- バー表示の保存 --------------------------------------------------------

def test_watchdog_bar_flag_and_partial_save():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "settings.json"
        path.write_text(json.dumps(
            {"capture": {"save_folder": "X"},
             "copilot_status": {"enabled": True, "bar": False}},
            ensure_ascii=False), encoding="utf-8")
        app_settings = settings_module.load_settings(path)
        wd = cw.CopilotWatchdog(app_settings=app_settings, settings_path=path)
        # bar キーが優先。子は生成しただけでは起きない(起こすのは feature_screen)
        assert wd.bar_enabled() is False
        assert wd.is_enabled() is False
        wd.set_bar_enabled(True)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["copilot_status"]["bar"] is True
        # 該当キーだけの差し替え: 他の節と旧キーはそのまま残る
        assert raw["copilot_status"]["enabled"] is True
        assert raw["capture"]["save_folder"] == "X"
        wd.close()


def test_watchdog_bar_defaults():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    # bar が無ければ旧キー enabled を初期値にする(OFF にしていた PC のため)
    wd = cw.CopilotWatchdog(app_settings={"copilot_status": {"enabled": False}},
                            settings_path=None)
    assert wd.bar_enabled() is False
    wd.close()
    # どちらも無ければ表示する(従来の既定)
    wd = cw.CopilotWatchdog(app_settings={}, settings_path=None)
    assert wd.bar_enabled() is True
    assert wd.is_enabled() is False
    wd.close()


def test_settings_defaults_have_copilot_status():
    # bar / enabled の既定値は DEFAULT_SETTINGS に置かない。置くと
    # load_settings の深いマージで bar が常に入り、旧 enabled への引き継ぎが
    # 働かなくなる。既定の判定は copilot_watchdog が生ファイルに対して行う。
    with tempfile.TemporaryDirectory() as d:
        loaded = settings_module.load_settings(Path(d) / "settings.json")
        assert loaded["copilot_status"] == {}


def test_bar_enabled_from_section_cases():
    # copilot_status が無い → True(初めて使う PC)
    assert cw.bar_enabled_from_section(None) is True
    assert cw.bar_enabled_from_section({}) is True
    # {"enabled": false} だけ → False(旧仕様で OFF にしていた PC)
    assert cw.bar_enabled_from_section({"enabled": False}) is False
    assert cw.bar_enabled_from_section({"enabled": True}) is True
    # bar があれば bar が勝つ
    assert cw.bar_enabled_from_section(
        {"enabled": False, "bar": True}) is True
    assert cw.bar_enabled_from_section(
        {"enabled": True, "bar": False}) is False


def test_bar_enabled_from_raw_file_and_child_agrees():
    # 親(CopilotWatchdog)と子(StatusWatcher の読み直し)が、生ファイルに
    # 対して同じ答えを返すこと(同じ共通関数を通る)。
    # 子のモジュールは ctypes.windll 前提で Linux では import できないため、
    # 子の確認は import できた環境(Windows)でのみ行う。
    try:
        import copilot_status_process as csp
    except Exception:  # noqa: BLE001  Linux VM では import 自体ができない
        csp = None
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    cases = [
        ({"capture": {"save_folder": "X"}}, True),          # 節が無い
        ({"copilot_status": {"enabled": False}}, False),    # 旧 OFF の PC
        ({"copilot_status": {"enabled": False, "bar": True}}, True),
        ({"copilot_status": {"bar": False}}, False),
    ]
    old_settings_path = settings_module.SETTINGS_PATH
    try:
        for payload, expected in cases:
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "settings.json"
                path.write_text(json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8")
                assert cw.resolve_bar_enabled(settings_path=path) is expected
                wd = cw.CopilotWatchdog(
                    app_settings=settings_module.load_settings(path),
                    settings_path=path)
                try:
                    assert wd.bar_enabled() is expected
                finally:
                    wd.close()
                # 子の読み直し(SETTINGS_PATH 経由)も同じ答え
                if csp is not None:
                    settings_module.SETTINGS_PATH = path
                    stub = object.__new__(csp.StatusWatcher)
                    stub._bar_enabled = None
                    stub._refresh_bar_setting()
                    assert stub._bar_enabled is expected
    finally:
        settings_module.SETTINGS_PATH = old_settings_path


# -- 子プロセス側の手番の書き出し -------------------------------------------

def test_publish_turn_writes_on_change_and_heartbeats():
    import copilot_status_process as csp
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "turn.json"
        old = csp.TURN_STATUS_FILE
        csp.TURN_STATUS_FILE = str(target)
        try:
            stub = object.__new__(csp.StatusWatcher)
            stub._state = "waiting_ai"
            stub._state_since = time.time() - 3
            stub._paused = False
            stub._thresholds = dict(csp.DEFAULT_THRESHOLDS)
            stub._turn_published = None
            stub._turn_written_at = 0.0

            class _Cp:
                profile = {"name": "M365 Copilot"}
                hwnd_main = 777
            stub._copilot = _Cp()

            stub._publish_turn()
            first = json.loads(target.read_text(encoding="utf-8"))
            assert first["state"] == "waiting_ai"
            assert first["app"] == "M365 Copilot"
            assert first["hwnd"] == 777
            assert first["alert"] is False

            # 変わらなければ書き直さない(updated が進まない)
            stub._publish_turn()
            again = json.loads(target.read_text(encoding="utf-8"))
            assert again["updated"] == first["updated"]

            # 状態が変われば書く
            stub._state = "responding"
            stub._publish_turn()
            changed = json.loads(target.read_text(encoding="utf-8"))
            assert changed["state"] == "responding"
            assert changed["updated"] >= first["updated"]

            # ハートビート: 間隔を過ぎたら updated だけ進める
            stub._turn_written_at -= csp.TURN_HEARTBEAT_SECONDS + 1
            stub._publish_turn()
            beat = json.loads(target.read_text(encoding="utf-8"))
            assert beat["updated"] > changed["updated"]
            assert beat["state"] == "responding"
        finally:
            csp.TURN_STATUS_FILE = old


def test_classify_delete_all_decays_to_waiting_user():
    # 入力→全削除は監視からは送信と区別がつかない。まず応答待ちに倒れるが、
    # 応答中(busy)を一度も見ないまま経過則を過ぎたら応答済に倒し直すこと。
    import copilot_status_process as csp
    with tempfile.TemporaryDirectory() as d:
        target = Path(d) / "turn.json"
        old = csp.TURN_STATUS_FILE
        csp.TURN_STATUS_FILE = str(target)
        try:
            stub = object.__new__(csp.StatusWatcher)
            stub._paused = False
            stub._state = None
            stub._state_since = None
            stub._last_decisive = None
            stub._elapsed_accum = 0.0
            stub._elapsed_ticked_at = None
            stub._resume_declined = False
            stub._thresholds = dict(csp.DEFAULT_THRESHOLDS)
            stub._turn_published = None
            stub._turn_written_at = 0.0
            stub._copilot = None
            stub._refresh_bar_setting = lambda: None
            snap = {"state": "idle", "input_text": "こんにちは",
                    "window_rect": None, "input_rect": None}
            stub._read_snapshot = lambda: snap
            stub._poll()
            assert stub._state == "typing"
            # 全削除 → 空idle。まず応答待ち(送信と区別がつかない)
            snap = {**snap, "input_text": ""}
            stub._read_snapshot = lambda: snap
            stub._poll()
            assert stub._state == "waiting_ai"
            # 経過則を過ぎても応答中が出なければ、応答済に倒し直す
            stub._state_since = time.time() - csp.WAITING_AI_DECAY_SECONDS - 1
            stub._poll()
            assert stub._state == "waiting_user"
            # 本物の送信は busy が出るので応答中になり、倒し直しの対象外
            snap = {**snap, "input_text": "質問"}
            stub._read_snapshot = lambda: snap
            stub._poll()
            assert stub._state == "typing"
            snap = {**snap, "input_text": "", "state": "busy"}
            stub._read_snapshot = lambda: snap
            stub._poll()
            assert stub._state == "responding"
        finally:
            csp.TURN_STATUS_FILE = old


# -- パネルの Copilot 行 ----------------------------------------------------

def test_copilot_row_shape():
    assert cp.copilot_row(None) is None
    assert cp.copilot_row({"state": "不明"}) is None
    row = cp.copilot_row({"state": "waiting_user", "app": "M365 Copilot",
                          "since": 123.0, "alert": True})
    assert row["_copilot"] is True and row["state"] == "waiting_user"
    assert row["app"] == "M365 Copilot" and row["alert"] is True
    # Copilot 行は Claude の行と同じ高さ(2段組み)
    assert cp.row_height(row) == cp.ROW_HEIGHT
    assert cp.panel_height([row]) == cp.PAD_Y * 2 + cp.ROW_HEIGHT


def test_panel_shows_copilot_row_alone():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        panel = cp.ClaudeProgressPanel(state_path=Path(d) / "panel.json")
        panel.set_entries([], {"state": "responding", "app": "M365 Copilot",
                               "since": time.time(), "alert": False})
        assert panel.isVisible()
        assert len(panel._rows) == 1 and panel._rows[0]["_copilot"] is True
        assert panel.height() == cp.panel_height(panel._rows)
        panel.set_entries([], None)
        assert not panel.isVisible()
        panel.close()


def test_panel_copilot_dismiss_until_turn_changes():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as d:
        panel = cp.ClaudeProgressPanel(state_path=Path(d) / "panel.json")
        turn = {"state": "waiting_user", "app": "M365 Copilot",
                "since": 1000.0, "alert": False}
        panel.set_entries([], turn)
        assert panel.isVisible()
        panel._remove_row(0)   # ✕
        assert not panel.isVisible()
        # 同じ手番が届き直しても出ない
        panel.set_entries([], turn)
        assert not panel.isVisible()
        # 手番が変われば(開始時刻が変われば)また出る
        panel.set_entries([], {**turn, "since": 2000.0})
        assert panel.isVisible()
        panel.close()


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
