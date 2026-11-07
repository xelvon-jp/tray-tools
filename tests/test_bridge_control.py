# test_bridge_control.py
# ブリッジから見張りへの合図ファイル(bridge_control)のテスト。Qt 不要。
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge_control as bc


def test_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "pause.json"   # 親フォルダが無くても作る
        assert bc.is_paused(path) is False
        assert bc.set_paused(True, path) is True
        assert bc.is_paused(path) is True
        assert bc.set_paused(False, path) is True
        assert bc.is_paused(path) is False


def test_missing_and_corrupt_are_not_paused():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        assert bc.is_paused(base / "none.json") is False
        broken = base / "broken.json"
        broken.write_text("{壊れた", encoding="utf-8")
        assert bc.is_paused(broken) is False
        other = base / "other.json"
        other.write_text('{"paused": "yes"}', encoding="utf-8")   # 真は True だけ
        assert bc.is_paused(other) is False
        listed = base / "list.json"
        listed.write_text("[1, 2]", encoding="utf-8")
        assert bc.is_paused(listed) is False


def test_write_failure_is_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        # 親がファイル(フォルダにできない)でも例外を出さない
        assert bc.set_paused(True, blocker / "pause.json") is False
        assert bc.is_paused(blocker / "pause.json") is False


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
