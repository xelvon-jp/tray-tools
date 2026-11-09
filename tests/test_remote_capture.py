# test_remote_capture.py
# リモート撮影(remote_capture)の純粋な判定部分のテスト。
# 撮影や投稿そのものは Qt / UIA が要るので実機確認に任せ、ここでは
# 「合言葉を読む・消費を記録する・保存先を決める」だけを確かめる。
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import remote_capture as rc  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_read_trigger_nonce(tmp_path):
    p = _write(tmp_path / "t.json", '{"nonce": "abc-123"}')
    assert rc.read_trigger_nonce(p) == "abc-123"


def test_read_trigger_nonce_strips_and_rejects_empty(tmp_path):
    p = _write(tmp_path / "t.json", '{"nonce": "  x  "}')
    assert rc.read_trigger_nonce(p) == "x"
    p = _write(tmp_path / "t.json", '{"nonce": ""}')
    assert rc.read_trigger_nonce(p) is None
    p = _write(tmp_path / "t.json", '{"nonce": "   "}')
    assert rc.read_trigger_nonce(p) is None


def test_read_trigger_nonce_tolerates_broken(tmp_path):
    assert rc.read_trigger_nonce(tmp_path / "missing.json") is None
    p = _write(tmp_path / "t.json", "{not json")
    assert rc.read_trigger_nonce(p) is None
    p = _write(tmp_path / "t.json", '["nonce"]')
    assert rc.read_trigger_nonce(p) is None
    p = _write(tmp_path / "t.json", '{"nonce": 42}')
    assert rc.read_trigger_nonce(p) is None
    p = _write(tmp_path / "t.json", '{"other": "x"}')
    assert rc.read_trigger_nonce(p) is None


def test_seen_roundtrip(tmp_path):
    p = tmp_path / "sub" / "seen.json"
    assert rc.load_seen_nonce(p) is None
    rc.save_seen_nonce(p, "n-1")
    assert rc.load_seen_nonce(p) == "n-1"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"nonce": "n-1"}


def test_pending_nonce_one_shot(tmp_path):
    trigger = _write(tmp_path / "remote_capture.json", '{"nonce": "shot-1"}')
    seen = tmp_path / "seen.json"
    assert rc.pending_nonce(trigger, seen) == "shot-1"
    rc.save_seen_nonce(seen, "shot-1")
    assert rc.pending_nonce(trigger, seen) is None
    # 合言葉が新しくなれば、また発火してよい。
    _write(trigger, '{"nonce": "shot-2"}')
    assert rc.pending_nonce(trigger, seen) == "shot-2"


def test_pending_nonce_empty_trigger_never_fires(tmp_path):
    trigger = _write(tmp_path / "remote_capture.json", '{"nonce": ""}')
    seen = tmp_path / "seen.json"
    assert rc.pending_nonce(trigger, seen) is None


def test_resolve_target_folder():
    env = {"OneDrive": r"C:\Users\yotan\OneDrive"}
    assert rc.resolve_target_folder(env) == (
        Path(r"C:\Users\yotan\OneDrive") / "traytools-remote-capture")
    assert rc.resolve_target_folder({}) is None
    assert rc.resolve_target_folder({"OneDrive": "  "}) is None


def test_app_data_dir():
    env = {"LOCALAPPDATA": r"C:\Users\yotan\AppData\Local"}
    assert rc.app_data_dir(env) == (
        Path(r"C:\Users\yotan\AppData\Local") / "traytools")
