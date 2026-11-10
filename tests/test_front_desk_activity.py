# tests/test_front_desk_activity.py
# ブリッジ表示用の合図ファイル (front_desk.activity) のテスト。
# プレイブックの実行開始と結果が desk_activity.json に書かれることを確かめる。
#
#   python3 tests/test_front_desk_activity.py    (Linux でも可)
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from front_desk import activity, model, playbooks
from front_desk.config import DeskConfig
from front_desk.executor import ExecOutcome
from front_desk.playbooks import PlaybookExecutor


def _tmp() -> str:
    return os.path.join(tempfile.mkdtemp(), "desk_activity.json")


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_begin_writes_running():
    path = _tmp()
    activity.begin("traytools_restart", "req-1", path=path, now=1000.0)
    d = _read(path)
    assert d["state"] == "running"
    assert d["playbook"] == "traytools_restart"
    assert d["request_id"] == "req-1"
    assert d["started_at"] == 1000.0
    assert d["finished_at"] is None and d["ok"] is None


def test_finish_writes_done():
    path = _tmp()
    outcome = SimpleNamespace(ok=True, summary="再起動しました")
    activity.finish("traytools_restart", "req-1", outcome, path=path,
                    started_at=1000.0, now=1042.0)
    d = _read(path)
    assert d["state"] == "done" and d["ok"] is True
    assert d["summary"] == "再起動しました"
    assert d["started_at"] == 1000.0 and d["finished_at"] == 1042.0


def test_executor_writes_activity_for_real_run():
    path = _tmp()
    playbooks.PLAYBOOKS["echo_test"] = (
        lambda ctx, params: ExecOutcome(ok=True, summary="echo ok"))
    try:
        config = DeskConfig(allowed_kinds=("ops",),
                            allowed_playbooks=("echo_test",))
        ex = PlaybookExecutor(config, runner=object(), activity_path=path)
        req = model.Request(id="req-t9", tier="playbook", kind="ops",
                            body=json.dumps({"playbook": "echo_test"}))
        out = ex.run(req)
        assert out.ok
        d = _read(path)
        assert d["state"] == "done" and d["ok"] is True
        assert d["playbook"] == "echo_test" and d["request_id"] == "req-t9"
        assert d["summary"] == "echo ok"
        assert d["started_at"] is not None and d["finished_at"] is not None
    finally:
        playbooks.PLAYBOOKS.pop("echo_test", None)


def test_executor_writes_nothing_for_rejected():
    path = _tmp()
    config = DeskConfig(allowed_kinds=("ops",),
                        allowed_playbooks=("traytools_sync",))
    ex = PlaybookExecutor(config, runner=object(), activity_path=path)
    req = model.Request(id="req-t10", tier="playbook", kind="ops",
                        body=json.dumps({"playbook": "no_such_playbook"}))
    out = ex.run(req)
    assert not out.ok
    assert not os.path.exists(path)


def test_heartbeat_merges_and_is_preserved():
    path = _tmp()
    # ハートビートだけ先に書かれている状態から begin しても落ちない
    activity.heartbeat(path=path, now=500.0)
    d = _read(path)
    assert d == {"heartbeat_at": 500.0}
    activity.begin("pb1001", "req-9", path=path, now=1000.0)
    d = _read(path)
    assert d["state"] == "running" and d["heartbeat_at"] == 500.0
    # ハートビートはプレイブックの状態を保ったまま時刻だけ進める
    activity.heartbeat(path=path, now=1060.0)
    d = _read(path)
    assert d["state"] == "running" and d["playbook"] == "pb1001"
    assert d["heartbeat_at"] == 1060.0
    # finish でも引き継ぐ
    outcome = SimpleNamespace(ok=True, summary="完了")
    activity.finish("pb1001", "req-9", outcome, path=path,
                    started_at=1000.0, now=1090.0)
    d = _read(path)
    assert d["state"] == "done" and d["heartbeat_at"] == 1060.0


ALL_TESTS = [test_begin_writes_running, test_finish_writes_done,
             test_executor_writes_activity_for_real_run,
             test_executor_writes_nothing_for_rejected,
             test_heartbeat_merges_and_is_preserved]


if __name__ == "__main__":
    for test in ALL_TESTS:
        test()
        print(f"PASS {test.__name__}")
