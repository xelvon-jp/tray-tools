# tests/test_deploy_daemon.py
# 配備係（deploy_daemon）の状態機械を確かめる。
#
# 輸送はインメモリのフェイク、git は一時リポジトリの実物、再起動と生存確認は
# フェイクで回すので、Linux でも通る。Windows 実機でしか分からない部分
# （traytools_send.py の実応答、常駐の実再起動）はここでは触らない。
#
#   python3 tests/test_deploy_daemon.py    (Linux でも可)
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_muse_post.py と同じ形)。
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from deploy_daemon.adapters import CommandTestRunner, SubprocessGit  # noqa: E402
from deploy_daemon.config import DaemonConfig, load_config  # noqa: E402
from deploy_daemon.intake import FakeTransport, IntakeClient  # noqa: E402
from deploy_daemon.log import JsonlLogger  # noqa: E402
from deploy_daemon.model import DeliveryState  # noqa: E402
from deploy_daemon.pipeline import Pipeline  # noqa: E402
from deploy_daemon.statefile import SeenStore, StateWriter  # noqa: E402


# ---------- フェイク ----------


class FakeRestarter:
    def __init__(self, check_kinds=(), default_check="ok", restart_results=(), default_restart=True):
        self._check_kinds = list(check_kinds)
        self._default_check = default_check
        self._restart_results = list(restart_results)
        self._default_restart = default_restart
        self.check_calls = 0
        self.restart_calls = 0

    def check(self):
        self.check_calls += 1
        kind = self._check_kinds.pop(0) if self._check_kinds else self._default_check
        return kind, f"check:{kind}"

    def restart(self):
        self.restart_calls += 1
        ok = self._restart_results.pop(0) if self._restart_results else self._default_restart
        return ok, "restart ok" if ok else "restart failed"


class FakeLiveness:
    def __init__(self, results=(), default=True):
        self._results = list(results)
        self._default = default
        self.calls = 0

    def check(self):
        self.calls += 1
        return self._results.pop(0) if self._results else self._default


# ---------- 一時リポジトリの世界 ----------


def _env():
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
    )
    return env


def _git(cwd, *args):
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=_env()
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout.strip()


def _commit_all(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_world(tmp):
    """src（リモート役）と checkout（作業用チェックアウト役）を作る。

    src の main が v1。muse/change は正常な変更、muse/broken はテストが
    落ちる変更、muse/diverged は main から枝分かれした変更。
    """
    src = tmp / "src"
    src.mkdir()
    _git(src, "init", "-b", "main")
    (src / "app.txt").write_text("v1\n", encoding="utf-8")
    (src / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
    sha_base = _commit_all(src, "v1")

    _git(src, "checkout", "-b", "muse/change")
    (src / "app.txt").write_text("v2\n", encoding="utf-8")
    sha_change = _commit_all(src, "v2")
    _git(src, "checkout", "main")

    _git(src, "checkout", "-b", "muse/broken")
    (src / "check.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    sha_broken = _commit_all(src, "broken")
    _git(src, "checkout", "main")

    _git(src, "checkout", "-b", "muse/diverged")
    (src / "other.txt").write_text("other\n", encoding="utf-8")
    sha_diverged = _commit_all(src, "diverged")
    _git(src, "checkout", "main")

    checkout = tmp / "checkout"
    proc = subprocess.run(
        ["git", "clone", str(src), str(checkout)], capture_output=True, text=True, env=_env()
    )
    assert proc.returncode == 0, proc.stderr
    assert _git(checkout, "rev-parse", "HEAD") == sha_base
    return {
        "src": src,
        "checkout": checkout,
        "sha_base": sha_base,
        "sha_change": sha_change,
        "sha_broken": sha_broken,
        "sha_diverged": sha_diverged,
    }


def _config(tmp, world, **overrides):
    cfg = DaemonConfig(
        base_dir=tmp / "daemon",
        checkout_dir=world["checkout"],
        remotes={"tray-tools": str(world["src"])},
        test_commands=((sys.executable, "check.py"),),
        apply_mode="auto",
        blocker_wait_sec=0,
        blocker_retries=3,
        liveness_retries=2,
        liveness_interval_sec=0,
        poll_interval_sec=0,
    )
    return replace(cfg, **overrides) if overrides else cfg


def _pipeline(cfg, transport, restarter=None, liveness=None):
    return Pipeline(
        config=cfg,
        intake=IntakeClient("https://example.invalid/intake", "test-key", transport=transport),
        git=SubprocessGit(),
        test_runner=CommandTestRunner(cfg.test_commands, cfg.test_timeout_sec),
        restarter=restarter if restarter is not None else FakeRestarter(),
        liveness=liveness if liveness is not None else FakeLiveness(),
        logger=JsonlLogger(cfg.log_path),
        state_writer=StateWriter(cfg.state_path),
        seen=SeenStore(cfg.seen_path),
        sleep=lambda _sec: None,
    )


def _delivery(delivery_id, branch, ref, repo="tray-tools"):
    return {
        "id": delivery_id,
        "repo": repo,
        "branch": branch,
        "ref": ref,
        "summary": f"{branch} の変更",
        "created_at": "2026-11-09T00:00:00+09:00",
    }


def _head(checkout):
    return _git(checkout, "rev-parse", "HEAD")


# ---------- テスト ----------


def test_happy_path_auto():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-1", "muse/change", world["sha_change"])])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert len(records) == 1
    assert records[0].state == DeliveryState.REPORTED, records[0].history
    assert _head(world["checkout"]) == world["sha_change"]
    assert restarter.restart_calls == 1
    terminal = [r for r in transport.reports if r["body"]["terminal"]]
    assert terminal and terminal[-1]["body"]["ok"] is True
    assert terminal[-1]["body"]["state"] == "verified"
    state = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert state["deliveries"]["dlv-1"]["state"] == "reported"


def test_test_failure_never_applies():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-2", "muse/broken", world["sha_broken"])])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.TEST_FAILED, records[0].history
    assert _head(world["checkout"]) == world["sha_base"]  # 実物は無事なまま
    assert restarter.restart_calls == 0
    assert not cfg.staging_dir_for("tray-tools").exists()  # 検証用コピーは破棄


def test_rollback_on_liveness_failure():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-3", "muse/change", world["sha_change"])])
    restarter = FakeRestarter()
    liveness = FakeLiveness(results=[False, False, True])  # 反映後は応答なし→戻したら応答
    records = _pipeline(cfg, transport, restarter=restarter, liveness=liveness).run_once()
    assert records[0].state == DeliveryState.ROLLED_BACK, records[0].history
    assert _head(world["checkout"]) == world["sha_base"]
    assert restarter.restart_calls == 2  # 反映時とロールバック時


def test_rollback_restart_respects_blockers():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world, blocker_retries=1)
    transport = FakeTransport([_delivery("dlv-4", "muse/change", world["sha_change"])])
    # 反映時の check は ok、ロールバック時の check は blocked のまま
    restarter = FakeRestarter(check_kinds=["ok"], default_check="blocked")
    liveness = FakeLiveness(results=[False, False, True])
    records = _pipeline(cfg, transport, restarter=restarter, liveness=liveness).run_once()
    assert records[0].state == DeliveryState.ROLLBACK_FAILED, records[0].history
    assert _head(world["checkout"]) == world["sha_base"]  # コードは戻っている
    assert restarter.restart_calls == 1  # ブロッカー中はロールバック側も再起動しない


def test_duplicate_delivery_skipped():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    delivery = _delivery("dlv-5", "muse/change", world["sha_change"])
    _pipeline(cfg, FakeTransport([delivery])).run_once()
    # 配備係が再起動しても、処理済みの届け出は二度がけしない
    transport2 = FakeTransport([delivery])
    records = _pipeline(cfg, transport2).run_once()
    assert records == []
    assert transport2.reports == []


def test_out_of_scope_rejected():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport(
        [
            _delivery("dlv-6a", "muse/change", world["sha_change"], repo="photo-culler"),
            _delivery("dlv-6b", "feature/x", world["sha_change"]),
        ]
    )
    records = _pipeline(cfg, transport).run_once()
    assert [r.state for r in records] == [DeliveryState.REJECTED, DeliveryState.REJECTED]
    assert _head(world["checkout"]) == world["sha_base"]
    assert not cfg.staging_root.exists()  # 範囲外はステージングすらしない


def test_stop_file_blocks_intake():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    cfg.base_dir.mkdir(parents=True, exist_ok=True)
    cfg.stop_file.write_text("", encoding="utf-8")
    transport = FakeTransport([_delivery("dlv-7", "muse/change", world["sha_change"])])
    records = _pipeline(cfg, transport).run_once()
    assert records == []
    assert transport.fetch_count == 0  # 停止中は受け取り口自体を見に行かない


def test_blockers_wait_then_restart():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-8", "muse/change", world["sha_change"])])
    restarter = FakeRestarter(check_kinds=["blocked", "blocked"], default_check="ok")
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.REPORTED, records[0].history
    assert restarter.check_calls == 3
    assert restarter.restart_calls == 1  # ブロッカーが解けてから1回だけ


def test_blockers_exhausted_held():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world, blocker_retries=2)
    transport = FakeTransport([_delivery("dlv-9", "muse/change", world["sha_change"])])
    restarter = FakeRestarter(default_check="blocked")
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.HELD, records[0].history
    assert restarter.restart_calls == 0  # ブロッカー中は再起動しない
    assert _head(world["checkout"]) == world["sha_change"]  # 反映自体は済んでいる


def test_conflict_not_applied():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    # 作業用チェックアウト側が独自に進んでいる（ff できない）
    (world["checkout"] / "main2.txt").write_text("local\n", encoding="utf-8")
    local_head = _commit_all(world["checkout"], "local advance")
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-10", "muse/diverged", world["sha_diverged"])])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.CONFLICT, records[0].history
    assert _head(world["checkout"]) == local_head
    assert restarter.restart_calls == 0


def test_dirty_checkout_stops():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    (world["checkout"] / "app.txt").write_text("編集中\n", encoding="utf-8")  # 未コミット変更
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-11", "muse/change", world["sha_change"])])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.CONFLICT, records[0].history
    assert _head(world["checkout"]) == world["sha_base"]
    assert restarter.restart_calls == 0


def test_approval_mode_waits_for_signal():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world, apply_mode="approval")
    delivery = _delivery("dlv-12", "muse/change", world["sha_change"])
    transport = FakeTransport([delivery])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.AWAITING_APPLY_APPROVAL, records[0].history
    assert _head(world["checkout"]) == world["sha_base"]  # まだ反映しない
    assert restarter.restart_calls == 0

    # 承認シグナル（ファイル）が来たら、次の見回りで反映まで進む
    cfg.approval_dir.mkdir(parents=True, exist_ok=True)
    (cfg.approval_dir / "dlv-12.approve").write_text("", encoding="utf-8")
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.REPORTED, records[0].history
    assert _head(world["checkout"]) == world["sha_change"]
    assert not (cfg.approval_dir / "dlv-12.approve").exists()  # 承認は使い切り


def test_deferred_when_ref_missing():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-13", "muse/change", "f" * 40)])
    pipeline = _pipeline(cfg, transport)
    records = pipeline.run_once()
    assert records[0].state == DeliveryState.DEFERRED, records[0].history
    assert not pipeline.seen.is_terminal("dlv-13")  # 終端ではない（次回また試す）
    records = pipeline.run_once()
    assert records[0].state == DeliveryState.DEFERRED


def test_config_env_overrides():
    tmp = Path(tempfile.mkdtemp())
    cfg = load_config(
        base_dir=tmp / "cfg",
        environ={
            "DEPLOY_DAEMON_CHECKOUT_DIR": str(tmp / "co"),
            "DEPLOY_DAEMON_TEST_COMMAND": "python check.py",
            "DEPLOY_DAEMON_INTAKE_KEY": "dummy-key",
            "DEPLOY_DAEMON_APPLY_MODE": "auto",
        },
    )
    assert cfg.checkout_dir == tmp / "co"
    assert cfg.test_commands == (("python", "check.py"),)
    assert cfg.intake_key == "dummy-key"
    assert cfg.apply_mode == "auto"
    assert cfg.allowed_repos == ("tray-tools",)  # 既定は tray-tools のみ


def test_urllib_transport_sets_user_agent():
    # Cloudflare が Python 既定 UA を弾く（1010）ため、実輸送は
    # curl 相当の User-Agent を付けることを確かめる。
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from deploy_daemon.intake import UrllibTransport

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["ua"] = self.headers.get("User-Agent")
            body = b'{"deliveries": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/deliveries"
        status, _body = UrllibTransport(timeout=5.0).request(
            "GET", url, {"x-deploy-key": "k"}, None
        )
    finally:
        server.server_close()
    assert status == 200
    assert seen.get("ua"), "User-Agent が送られていません"
    assert "Python-urllib" not in seen["ua"]


def test_config_send_script_override():
    tmp = Path(tempfile.mkdtemp())
    base = tmp / "cfg"
    base.mkdir(parents=True)
    stub = tmp / "stub_send.py"
    (base / "config.json").write_text(
        json.dumps({"checkout_dir": str(tmp / "co"), "send_script": str(stub)}),
        encoding="utf-8",
    )
    cfg = load_config(base_dir=base, environ={})
    assert cfg.send_script == stub  # config.json の send_script が効く
    cfg2 = load_config(
        base_dir=base,
        environ={"DEPLOY_DAEMON_SEND_SCRIPT": str(tmp / "env_stub.py")},
    )
    assert cfg2.send_script == tmp / "env_stub.py"  # 環境変数が優先する
    cfg3 = load_config(
        base_dir=tmp / "cfg2",
        environ={"DEPLOY_DAEMON_CHECKOUT_DIR": str(tmp / "co")},
    )
    assert cfg3.send_script == tmp / "co" / "traytools_send.py"  # 既定はチェックアウト内


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
