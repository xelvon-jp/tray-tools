# tests/test_front_desk_playbooks6.py
# PB-5015 (置き場の状態確認) の進み方。
# 置き場・wake-log・見張りログは一時フォルダに作る。Linux でも通る。
# 実行器は playbooks4/5 と同じ素の assert 方式。
#
#   python3 tests/test_front_desk_playbooks6.py
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

from front_desk import playbooks
from front_desk.playbooks import PlaybookContext, inbox_status


def make_ctx(settings=None):
    return PlaybookContext(runner=object(), settings=settings or {},
                           sleep_fn=lambda *_: None)


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _seed(td):
    """desk_dir / inbox / wake-log / monitor log の最小の実在状態。"""
    desk = os.path.join(td, "front-desk")
    inbox = os.path.join(desk, "inbox")
    os.makedirs(inbox)
    _write(os.path.join(inbox, "20261112-223327-req-20261112-64.json"),
           json.dumps({"id": "req-20261112-64", "at": "x", "body": "b"}))
    _write(os.path.join(inbox, "20261112-085114-no-id.json"),
           json.dumps({"at": "x", "body": "b"}))
    _write(os.path.join(desk, "wake-log.jsonl"),
           '{"outcome": "sent", "id": "a"}\n'
           '{"outcome": "read", "by": "midori", "id": "b"}\n')
    monitor = os.path.join(td, "muse_monitor.log")
    _write(monitor,
           "[2026-11-12 22:39:27] midori: 合図 req-20261112-65\n"
           "[2026-11-12 22:40:00] other line\n"
           "[2026-11-12 22:50:18] approved: push\n")
    settings = {"inbox_dir": inbox, "desk_dir": desk,
                "monitor_log_path": monitor}
    return settings


def test_pb5015_basic():
    with tempfile.TemporaryDirectory() as td:
        out = inbox_status(make_ctx(_seed(td)), {})
        assert out.ok, out.summary
        assert "req-20261112-64" in out.detail
        assert "20261112-085114-no-id.json id=(idなし)" in out.detail
        assert '"outcome": "read"' in out.detail
        assert "approved: push" in out.detail
        assert "2 件" in out.summary


def test_pb5015_contains_filter():
    with tempfile.TemporaryDirectory() as td:
        out = inbox_status(make_ctx(_seed(td)),
                           {"contains": "req-20261112-65"})
        assert out.ok, out.summary
        assert "midori: 合図 req-20261112-65" in out.detail
        assert "approved: push" not in out.detail
        # 置き場の一覧は絞り込みの対象外 (実在の確認が目的なので全部出す)
        assert "req-20261112-64" in out.detail


def test_pb5015_missing_pieces():
    with tempfile.TemporaryDirectory() as td:
        settings = {"inbox_dir": os.path.join(td, "no-inbox"),
                    "desk_dir": os.path.join(td, "no-desk"),
                    "monitor_log_path": os.path.join(td, "no-log")}
        out = inbox_status(make_ctx(settings), {})
        assert out.ok, out.summary
        assert "置き場フォルダがありません" in out.detail
        assert "wake-log* に一致するファイルはありません" in out.detail
        assert "読めません" in out.detail


def test_pb5015_guards():
    with tempfile.TemporaryDirectory() as td:
        ctx = make_ctx(_seed(td))
        out = inbox_status(ctx, {"script": "x"})
        assert not out.ok and "未知の params" in out.summary
        out = inbox_status(ctx, {"tail_lines": "15"})
        assert not out.ok and "整数ではありません" in out.summary
        out = inbox_status(ctx, {"tail_lines": True})
        assert not out.ok and "整数ではありません" in out.summary
        out = inbox_status(ctx, {"contains": 5})
        assert not out.ok and "文字列ではありません" in out.summary


def test_pb5015_tail_clamp():
    with tempfile.TemporaryDirectory() as td:
        settings = _seed(td)
        _write(os.path.join(settings["desk_dir"], "wake-log.jsonl"),
               "".join(f"line {i}\n" for i in range(30)))
        out = inbox_status(make_ctx(settings), {"tail_lines": 1000})
        assert out.ok, out.summary
        # 上限 100 に丸められるが、30 行しか無いので全部返る
        assert "line 0" in out.detail and "line 29" in out.detail
        out = inbox_status(make_ctx(settings), {"tail_lines": 3})
        assert "line 29" in out.detail and "line 0" not in out.detail


def test_pb5015_registered():
    assert playbooks.PLAYBOOKS["pb5015"] is inbox_status
    assert playbooks.PLAYBOOK_ALIASES["inbox_status"] == "pb5015"


ALL_TESTS = [
    test_pb5015_basic,
    test_pb5015_contains_filter,
    test_pb5015_missing_pieces,
    test_pb5015_guards,
    test_pb5015_tail_clamp,
    test_pb5015_registered,
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
