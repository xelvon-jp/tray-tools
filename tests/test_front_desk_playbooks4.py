# tests/test_front_desk_playbooks4.py
# PB-4033 (スクリプト保存庫) と PB-5012 (見張りログ取得) の進み方。
# 外部コマンドは FakePS (run_bytes の偽物)、ファイルは一時フォルダ。
# Linux でも通る。実行器は playbooks3 と同じ素の assert 方式。
#
#   python3 tests/test_front_desk_playbooks4.py
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from front_desk import model
from front_desk.playbooks import (CommandResult, PlaybookContext,
                                  script_library, log_fetch)


def make_ctx(runner, settings=None):
    return PlaybookContext(runner=runner, settings=settings or {},
                           sleep_fn=lambda *_: None)


class FakePS:
    def __init__(self, result=None):
        self.result = result or CommandResult(0, "hello\n")
        self.argv = None
        self.script_bytes = None

    def run(self, argv, timeout_sec):
        raise AssertionError("run_bytes を使うはず")

    def run_bytes(self, argv, timeout_sec):
        self.argv = list(argv)
        with open(argv[-1], "rb") as fh:
            self.script_bytes = fh.read()
        return self.result


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- PB-4033 ---------------------------------------------------------------

def test_pb4033_save_list_run():
    with tempfile.TemporaryDirectory() as td:
        fake = FakePS()
        ctx = make_ctx(fake, {"library_dir": td})
        script = "Write-Output 'from library'"
        out = script_library(ctx, {"action": "save", "name": "demo",
                                   "script": script})
        assert out.ok, out
        assert os.path.exists(os.path.join(td, "demo.ps1"))
        out = script_library(ctx, {"action": "list"})
        assert out.ok and "demo" in out.detail
        assert _sha(script)[:16] in out.detail
        out = script_library(ctx, {"action": "run", "name": "demo",
                                   "confirm_sha256": _sha(script)})
        assert out.ok, out
        assert b"from library" in fake.script_bytes
        index = json.load(open(os.path.join(td, "index.json"),
                               encoding="utf-8"))
        assert index["demo"]["uses"] == 1
        assert index["demo"]["last_used_at"]


def test_pb4033_run_guards():
    with tempfile.TemporaryDirectory() as td:
        fake = FakePS()
        ctx = make_ctx(fake, {"library_dir": td})
        script = "Write-Output 'x'"
        script_library(ctx, {"action": "save", "name": "demo",
                             "script": script})
        # 申告ハッシュが違う
        out = script_library(ctx, {"action": "run", "name": "demo",
                                   "confirm_sha256": "0" * 64})
        assert not out.ok and out.state == model.RESULT_BLOCKED
        # 保存後にファイルが変わった
        with open(os.path.join(td, "demo.ps1"), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write("Write-Output 'tampered'")
        out = script_library(ctx, {"action": "run", "name": "demo",
                                   "confirm_sha256": _sha(script)})
        assert not out.ok and out.state == model.RESULT_BLOCKED
        # 無い名前・不正な名前・不正な action
        for params in ({"action": "run", "name": "ghost",
                        "confirm_sha256": _sha(script)},
                       {"action": "save", "name": "bad name!",
                        "script": script},
                       {"action": "jump"}):
            out = script_library(ctx, params)
            assert not out.ok and out.state == model.RESULT_BLOCKED, params
        assert fake.argv is None  # ガードで止まったら実行しない


def test_pb4033_overwrite_keeps_uses():
    with tempfile.TemporaryDirectory() as td:
        fake = FakePS()
        ctx = make_ctx(fake, {"library_dir": td})
        s1, s2 = "Write-Output 'v1'", "Write-Output 'v2'"
        script_library(ctx, {"action": "save", "name": "demo", "script": s1})
        script_library(ctx, {"action": "run", "name": "demo",
                             "confirm_sha256": _sha(s1)})
        out = script_library(ctx, {"action": "save", "name": "demo",
                                   "script": s2})
        assert out.ok
        index = json.load(open(os.path.join(td, "index.json"),
                               encoding="utf-8"))
        assert index["demo"]["sha256"] == _sha(s2)
        assert index["demo"]["uses"] == 1  # 実行回数は引き継ぐ


# ---- PB-5012 ---------------------------------------------------------------

def _write_log(path, lines):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")


def test_pb5012_tail_and_contains():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "muse_monitor.log")
        _write_log(path, [f"line {i}" for i in range(50)]
                   + ["ERROR boom happened", "line 51"])
        ctx = make_ctx(FakePS(), {"log_path": path})
        out = log_fetch(ctx, {"tail_lines": 5})
        assert out.ok, out
        assert "line 51" in out.detail and "line 0" not in out.detail
        out = log_fetch(ctx, {"contains": "boom"})
        assert out.ok and "ERROR boom happened" in out.detail
        assert "line 51" not in out.detail


def test_pb5012_guards():
    with tempfile.TemporaryDirectory() as td:
        ctx = make_ctx(FakePS(),
                       {"log_path": os.path.join(td, "missing.log")})
        out = log_fetch(ctx, {})
        assert not out.ok and out.state == model.RESULT_BLOCKED
        path = os.path.join(td, "x.log")
        _write_log(path, ["a"])
        ctx = make_ctx(FakePS(), {"log_path": path})
        for params in ({"tail_lines": "5"}, {"tail_lines": True},
                       {"contains": 3}, {"bogus": 1}):
            out = log_fetch(ctx, params)
            assert not out.ok and out.state == model.RESULT_BLOCKED, params


ALL_TESTS = [
    test_pb4033_save_list_run,
    test_pb4033_run_guards,
    test_pb4033_overwrite_keeps_uses,
    test_pb5012_tail_and_contains,
    test_pb5012_guards,
]


def main():
    failed = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"{len(ALL_TESTS) - failed}/{len(ALL_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
