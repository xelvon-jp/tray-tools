# tests/test_front_desk_playbooks5.py
# PB-4034 (セッションを起こす) の進み方。
# deep link を開く部分は _open_link の差し替え、記録と状態ファイルは
# 一時フォルダ。Linux でも通る。実行器は playbooks4 と同じ素の
# assert 方式。
#
#   python3 tests/test_front_desk_playbooks5.py
import json
import os
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from front_desk import model, playbooks
from front_desk.playbooks import (CommandResult, PlaybookContext,
                                  session_wake)

MARKER = playbooks.WAKE_MARKER
OPUS = playbooks.WAKE_DEFAULT_OPUS_SESSION
HAIKU = playbooks.WAKE_DEFAULT_HAIKU_SESSION


def make_ctx(settings=None):
    return PlaybookContext(runner=object(), settings=settings or {},
                           sleep_fn=lambda *_: None)


class OpenRecorder:
    """_open_link の差し替え。開いた URL を記録する。"""

    def __init__(self, result=True, fail_first=0):
        self.result = result
        self.fail_first = fail_first
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if len(self.urls) <= self.fail_first:
            return False
        return self.result


class DeliverRecorder:
    """_wake_screen_deliver の差し替え。渡された text を記録する。"""

    def __init__(self, result=(True, "sent")):
        self.result = result
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        return self.result


def patched(open_result=True, screen_result=(True, "sent"),
            open_fail_first=0):
    """_open_link と _notify_pushover と _wake_screen_deliver を
    差し替える文脈管理もどき。"""
    class _Patch:
        def __enter__(self):
            self.opener = OpenRecorder(open_result, open_fail_first)
            self.deliver = DeliverRecorder(screen_result)
            self.notes = []
            self._open = playbooks._open_link
            self._notify = playbooks._notify_pushover
            self._deliver = playbooks._wake_screen_deliver
            playbooks._open_link = self.opener
            playbooks._notify_pushover = \
                lambda title, message: self.notes.append(title) or True
            playbooks._wake_screen_deliver = self.deliver
            return self

        def __exit__(self, *exc):
            playbooks._open_link = self._open
            playbooks._notify_pushover = self._notify
            playbooks._wake_screen_deliver = self._deliver
            return False

    return _Patch()


def _records(log_path):
    with open(log_path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh.read().splitlines() if ln]


def _seed(log_path, records):
    with open(log_path, "w", encoding="utf-8", newline="") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _params(**over):
    params = {"target": "new", "body": "お願いします",
              "delivery_id": "wake-test-1"}
    params.update(over)
    return params


def _status_file(directory, name, entry):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        json.dump(entry, fh)


# ---- 新規セッション (deep link) ----------------------------------------------

def test_pb4034_new_session_success():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        with patched() as p:
            out = session_wake(make_ctx({"log_path": log_path}), _params())
        assert out.ok, out.summary
        assert len(p.opener.urls) == 1
        assert p.deliver.texts == [None]
        url = p.opener.urls[0]
        assert url.startswith("claude://code/new?q="), url
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["q"] == [MARKER + "\nお願いします"], query
        assert "folder" not in query
        records = _records(log_path)
        assert len(records) == 1 and records[0]["outcome"] == "sent"
        assert records[0]["body"] == "お願いします"


def test_pb4034_new_session_folder():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        with patched() as p:
            out = session_wake(make_ctx({"log_path": log_path}),
                               _params(folder=r"R:\claude\tray-tools"))
        assert out.ok, out.summary
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(p.opener.urls[0]).query)
        assert query["folder"] == [r"R:\claude\tray-tools"], query


def test_pb4034_marker_not_doubled():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        body = MARKER + "\nそのまま送る"
        with patched() as p:
            out = session_wake(make_ctx({"log_path": log_path}),
                               _params(body=body))
        assert out.ok, out.summary
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(p.opener.urls[0]).query)
        assert query["q"] == [body]


def test_pb4034_link_failure_notifies():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        with patched(open_result=False) as p:
            out = session_wake(make_ctx({"log_path": log_path}), _params())
        assert not out.ok
        assert len(p.notes) == 1
        assert _records(log_path)[-1]["outcome"] == "failed"


# ---- 土台 (上限・二重送信) -----------------------------------------------------

def test_pb4034_duplicate_blocked():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        with patched() as p:
            ctx = make_ctx({"log_path": log_path})
            assert session_wake(ctx, _params()).ok
            out = session_wake(ctx, _params())
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert len(p.opener.urls) == 1
        outcomes = [r["outcome"] for r in _records(log_path)]
        assert outcomes == ["sent", "duplicate"], outcomes


def test_pb4034_retry_after_failure_allowed():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        ctx = make_ctx({"log_path": log_path})
        with patched(open_result=False):
            assert not session_wake(ctx, _params()).ok
        with patched():
            assert session_wake(ctx, _params()).ok
        outcomes = [r["outcome"] for r in _records(log_path)]
        assert outcomes == ["failed", "sent"], outcomes


def test_pb4034_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        now = time.time()
        _seed(log_path, [
            {"ts": now - 60, "delivery_id": f"old-{i}", "outcome": "sent"}
            for i in range(3)])
        with patched() as p:
            out = session_wake(make_ctx({"log_path": log_path}), _params())
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert p.opener.urls == []
        assert _records(log_path)[-1]["outcome"] == "rate_limited"
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        now = time.time()
        _seed(log_path, [
            {"ts": now - 60, "delivery_id": "a", "outcome": "sent"},
            {"ts": now - 120, "delivery_id": "b", "outcome": "sent"},
            {"ts": now - 7200, "delivery_id": "c", "outcome": "sent"},
            {"ts": now - 30, "delivery_id": "d",
             "outcome": "deferred_to_relay"},
        ])
        with patched():
            out = session_wake(make_ctx({"log_path": log_path}), _params())
        assert out.ok, out.summary


def test_pb4034_rate_reset():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        now = time.time()
        _seed(log_path, [
            {"ts": now - 60, "delivery_id": f"old-{i}", "outcome": "sent"}
            for i in range(3)])
        ctx = make_ctx({"log_path": log_path})
        with patched() as p:
            out = session_wake(ctx, _params())
            assert not out.ok
            assert _records(log_path)[-1]["outcome"] == "rate_limited"
            out = session_wake(ctx, {"action": "reset_rate",
                                     "delivery_id": "wake-reset-1",
                                     "reason": "テスト"})
            assert out.ok, out.summary
            out = session_wake(ctx, _params(delivery_id="wake-test-2"))
            assert out.ok, out.summary
        outcomes = [r["outcome"] for r in _records(log_path)]
        assert outcomes[-3:] == ["rate_limited", "rate_reset", "sent"], \
            outcomes


def test_pb4034_guards():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        bad = [
            _params(bogus=1),
            _params(target="desk"),
            _params(body="  "),
            _params(body="x" * (playbooks.WAKE_BODY_MAX_CHARS + 1)),
            _params(delivery_id=""),
            _params(folder=3),
        ]
        with patched() as p:
            ctx = make_ctx({"log_path": log_path})
            for params in bad:
                out = session_wake(ctx, params)
                assert not out.ok \
                    and out.state == model.RESULT_BLOCKED, params
        assert p.opener.urls == []
        outcomes = [r["outcome"] for r in _records(log_path)]
        assert outcomes == ["invalid"] * len(bad), outcomes


# ---- 既存セッション ------------------------------------------------------------

def _opus_ctx(td, opus_entry, haiku_entry):
    status_dir = os.path.join(td, "status")
    os.makedirs(status_dir)
    if opus_entry is not None:
        _status_file(status_dir, "opus.json", opus_entry)
    if haiku_entry is not None:
        _status_file(status_dir, "haiku.json", haiku_entry)
    return make_ctx({"log_path": os.path.join(td, "wake-log.jsonl"),
                     "status_dir": status_dir})


def test_pb4034_opus_busy():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(td, {"host_session_id": OPUS, "state": "running",
                             "updated": now}, None)
        with patched() as p:
            out = session_wake(ctx, _params(target="opus"))
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert len(p.notes) == 1
        assert p.opener.urls == []
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "busy"


def test_pb4034_opus_deferred_to_relay():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(
            td,
            {"host_session_id": OPUS, "state": "done", "ended": now},
            {"host_session_id": HAIKU, "state": "done", "ended": now})
        with patched() as p:
            out = session_wake(ctx, _params(target="opus"))
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert p.opener.urls == [] and p.notes == []
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "deferred_to_relay"


def test_pb4034_opus_screen_route():
    now = time.time()
    cases = [
        {"host_session_id": OPUS, "state": "done", "ended": now},
        {"host_session_id": OPUS, "state": "fail", "ended": now},
        None,
    ]
    for entry in cases:
        with tempfile.TemporaryDirectory() as td:
            ctx = _opus_ctx(td, entry, None)
            with patched() as p:
                out = session_wake(ctx, _params(target="opus"))
            assert out.ok, (entry, out.summary)
            assert len(p.opener.urls) == 1
            assert p.opener.urls[0].startswith(
                "claude://claude.ai/epitaxy/"), p.opener.urls
            assert OPUS in p.opener.urls[0]
            assert p.deliver.texts == [MARKER + "\nお願いします"]
            records = _records(os.path.join(td, "wake-log.jsonl"))
            assert records[-1]["outcome"] == "sent"


def test_pb4034_screen_route_second_url():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(
            td, {"host_session_id": OPUS, "state": "done", "ended": now},
            None)
        with patched(open_fail_first=1) as p:
            out = session_wake(ctx, _params(target="opus"))
        assert out.ok, out.summary
        assert len(p.opener.urls) == 2
        assert "continue?session=" in p.opener.urls[1]
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "sent"


def test_pb4034_new_opened_not_sent():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "wake-log.jsonl")
        with patched(screen_result=(False, "user_active")) as p:
            out = session_wake(make_ctx({"log_path": log_path}), _params())
        assert not out.ok
        assert len(p.notes) == 1
        assert _records(log_path)[-1]["outcome"] == "opened_not_sent"


def test_pb4034_midori_screen_route():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(
            td, None,
            {"host_session_id": HAIKU, "state": "done", "ended": now})
        with patched() as p:
            out = session_wake(ctx, _params(target="midori"))
        assert out.ok, out.summary
        assert len(p.opener.urls) == 1 and HAIKU in p.opener.urls[0]
        assert p.deliver.texts == [MARKER + "\nお願いします"]
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "sent"
        assert records[-1]["target"] == "midori"


def test_pb4034_midori_busy():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(
            td, None,
            {"host_session_id": HAIKU, "state": "running", "updated": now})
        with patched() as p:
            out = session_wake(ctx, _params(target="midori"))
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert p.opener.urls == [] and p.deliver.texts == []
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "busy"


def test_pb4034_midori_deferred_to_relay():
    now = time.time()
    with tempfile.TemporaryDirectory() as td:
        ctx = _opus_ctx(
            td,
            {"host_session_id": OPUS, "state": "done", "ended": now},
            {"host_session_id": HAIKU, "state": "done", "ended": now})
        with patched() as p:
            out = session_wake(ctx, _params(target="midori"))
        assert not out.ok and out.state == model.RESULT_BLOCKED
        assert p.opener.urls == []
        records = _records(os.path.join(td, "wake-log.jsonl"))
        assert records[-1]["outcome"] == "deferred_to_relay"


# ---- 登録 ---------------------------------------------------------------------

def test_pb4034_registered():
    assert playbooks.PLAYBOOKS["pb4034"] is session_wake
    assert playbooks.PLAYBOOK_ALIASES["session_wake"] == "pb4034"


ALL_TESTS = [
    test_pb4034_new_session_success,
    test_pb4034_new_session_folder,
    test_pb4034_marker_not_doubled,
    test_pb4034_link_failure_notifies,
    test_pb4034_duplicate_blocked,
    test_pb4034_retry_after_failure_allowed,
    test_pb4034_rate_limit,
    test_pb4034_rate_reset,
    test_pb4034_guards,
    test_pb4034_opus_busy,
    test_pb4034_opus_deferred_to_relay,
    test_pb4034_opus_screen_route,
    test_pb4034_screen_route_second_url,
    test_pb4034_new_opened_not_sent,
    test_pb4034_midori_screen_route,
    test_pb4034_midori_busy,
    test_pb4034_midori_deferred_to_relay,
    test_pb4034_registered,
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
