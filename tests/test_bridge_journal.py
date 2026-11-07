# test_bridge_journal.py
# やり取りの記録(bridge_journal)のテスト。標準ライブラリだけで確かめる。
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bridge_journal as bj  # noqa: E402


def _today_file(base: Path) -> Path:
    return base / f"{date.today().isoformat()}.jsonl"


def _raw_lines(base: Path) -> list:
    return _today_file(base).read_text(encoding="utf-8").splitlines()


def _write_day(base: Path, day: date, records: list) -> None:
    path = base / f"{day.isoformat()}.jsonl"
    with open(path, "w", encoding="utf-8", newline="") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def test_default_dir():
    saved = os.environ.get("LOCALAPPDATA")
    try:
        os.environ["LOCALAPPDATA"] = "/somewhere/local"
        assert bj.default_dir() == Path("/somewhere/local") / "traytools" / "bridge_events"
        del os.environ["LOCALAPPDATA"]
        assert bj.default_dir() == Path(".") / "traytools" / "bridge_events"
    finally:
        if saved is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = saved


def test_log_and_tail():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        assert bj.log("mail_in", dir=base, id="mb-1", kind="request", note="合図") is True
        assert bj.log("ack", dir=base, id="mb-1", ms=812) is True
        records = bj.tail(10, dir=base)
        assert [r["ev"] for r in records] == ["mail_in", "ack"]
        first = records[0]
        assert first["id"] == "mb-1" and first["kind"] == "request"
        assert first["pid"] == os.getpid()
        assert "T" in first["ts"] and "." in first["ts"]   # ISO・ミリ秒まで
        assert abs(first["epoch"] - time.time()) < 60
        assert records[1]["ms"] == 812
        # 予約キー dir は記録に残らない
        assert "dir" not in first
        # tail の件数指定と、0件
        assert len(bj.tail(1, dir=base)) == 1 and bj.tail(1, dir=base)[0]["ev"] == "ack"
        assert bj.tail(0, dir=base) == []


def test_tail_crosses_days():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        yesterday = date.today() - timedelta(days=1)
        _write_day(base, yesterday, [
            {"ts": "x", "epoch": 100.0, "pid": 1, "ev": "old-1"},
            {"ts": "x", "epoch": 200.0, "pid": 1, "ev": "old-2"},
        ])
        bj.log("new-1", dir=base)
        bj.log("new-2", dir=base)
        # 日をまたいで古い順に3件
        assert [r["ev"] for r in bj.tail(3, dir=base)] == ["old-2", "new-1", "new-2"]
        assert [r["ev"] for r in bj.tail(99, dir=base)] == ["old-1", "old-2", "new-1", "new-2"]


def test_broken_lines_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        bj.log("ok-1", dir=base)
        with open(_today_file(base), "a", encoding="utf-8", newline="") as f:
            f.write("{これは壊れた行\n")
            f.write('["辞書ではない"]\n')
            f.write("\n")
        bj.log("ok-2", dir=base)
        assert [r["ev"] for r in bj.tail(10, dir=base)] == ["ok-1", "ok-2"]


def test_secret_keys_are_masked():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        bj.log("monitor", dir=base, phase="start", api_token="tok-abc",
               user_password="pw-def", secret_note="sec-ghi", auth_key="key-jkl",
               note="ふつうの項目")
        raw = _today_file(base).read_text(encoding="utf-8")
        for leaked in ("tok-abc", "pw-def", "sec-ghi", "key-jkl"):
            assert leaked not in raw
        (record,) = bj.tail(1, dir=base)
        assert record["api_token"] == "***" and record["user_password"] == "***"
        assert record["secret_note"] == "***" and record["auth_key"] == "***"
        assert record["note"] == "ふつうの項目" and record["phase"] == "start"


def test_string_values_are_truncated():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        bj.log("post_out", dir=base, head="あ" * 500, items=[1] * 300)
        (record,) = bj.tail(1, dir=base)
        assert len(record["head"]) == 200
        assert len(record["items"]) == 200   # 文字列化してから切る


def test_line_fits_in_4kb_and_stays_valid():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fields = {f"field{i}": "あ" * 200 for i in range(12)}   # 合計で4KBを大きく超える
        assert bj.log("kind", dir=base, **fields) is True
        raw = _today_file(base).read_bytes()
        assert len(raw) <= 4096
        (record,) = bj.tail(1, dir=base)   # 壊れていないので読み戻せる
        assert record["ev"] == "kind" and record["truncated"] is True


def test_cleanup_keeps_14_days():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        today = date.today()
        for days_ago in (15, 14, 1):
            _write_day(base, today - timedelta(days=days_ago),
                       [{"ts": "x", "epoch": 1.0, "pid": 1, "ev": f"d{days_ago}"}])
        bj._last_cleanup_date = None   # このプロセスではまだ整理していないことにする
        assert bj.log("wake", dir=base, reason="test") is True
        names = {p.name for p in base.glob("*.jsonl")}
        assert f"{(today - timedelta(days=15)).isoformat()}.jsonl" not in names
        assert f"{(today - timedelta(days=14)).isoformat()}.jsonl" in names
        assert f"{(today - timedelta(days=1)).isoformat()}.jsonl" in names


def test_concurrent_appends_do_not_corrupt():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        workers, per_worker = 8, 40
        barrier = threading.Barrier(workers)

        def run(worker):
            barrier.wait()
            for i in range(per_worker):
                assert bj.log("ack", dir=base, seq=worker * per_worker + i)

        threads = [threading.Thread(target=run, args=(w,)) for w in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = _raw_lines(base)
        assert len(lines) == workers * per_worker
        seqs = {json.loads(line)["seq"] for line in lines}   # 全行が無事に読める
        assert seqs == set(range(workers * per_worker))


def test_write_failure_is_quiet():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        blocker = base / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        # 保存先がファイル(ディレクトリでない)でも例外を出さない
        assert bj.log("wake", dir=blocker, reason="test") is False
        assert bj.log("wake", dir=blocker / "sub", reason="test") is False
        # 無い場所の読み出しは空
        assert bj.tail(5, dir=base / "missing") == []
        assert bj.since(0, dir=base / "missing") == []


def test_since_filters_by_epoch():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        yesterday = date.today() - timedelta(days=1)
        _write_day(base, yesterday, [
            {"ts": "x", "epoch": 100.0, "pid": 1, "ev": "old"},
            {"ts": "x", "epoch": 5000.0, "pid": 1, "ev": "old-but-late"},
        ])
        bj.log("now-1", dir=base)
        got = bj.since(1000.0, dir=base)
        assert [r["ev"] for r in got] == ["old-but-late", "now-1"]
        assert bj.since(time.time() + 3600, dir=base) == []
        assert len(bj.since(0, dir=base)) == 3


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
