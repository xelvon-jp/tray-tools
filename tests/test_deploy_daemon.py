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

from deploy_daemon.adapters import (  # noqa: E402
    CommandTestRunner,
    SendScriptLiveness,
    SendScriptRestarter,
    SubprocessGit,
)
from deploy_daemon.config import DaemonConfig, load_config  # noqa: E402
from deploy_daemon.digest import DigestPoster, build_digest  # noqa: E402
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


class RecordingTransport:
    """ダイジェスト送信用の記録フェイク。常に指定ステータスを返す。"""

    def __init__(self, status=200):
        self.status = status
        self.calls = []  # (method, url, headers, body)

    def request(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        return self.status, b'{"ok": true}'


class RaisingTransport:
    """送るたびに例外を投げるフェイク（送信失敗の握りつぶし確認用）。"""

    def request(self, method, url, headers, body):
        raise RuntimeError("network down")


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


def test_ancestor_ref_is_not_reported_as_applied():
    """dd-20261109-02 の再現: 届け出の ref が現在位置の祖先だと ff-only は
    何もせず成功するが、HEAD は ref に一致しないので成功にしてはならない。"""
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    # 作業用チェックアウトを v2（sha_change）まで進めておく
    _git(world["checkout"], "fetch", "origin", "muse/change")
    _git(world["checkout"], "checkout", world["sha_change"])
    assert _head(world["checkout"]) == world["sha_change"]
    cfg = _config(tmp, world)
    # 祖先（v1 = sha_base）を指す届け出
    transport = FakeTransport([_delivery("dlv-10b", "muse/change", world["sha_base"])])
    restarter = FakeRestarter()
    records = _pipeline(cfg, transport, restarter=restarter).run_once()
    assert records[0].state == DeliveryState.CONFLICT, records[0].history
    assert _head(world["checkout"]) == world["sha_change"]  # 何も動かしていない
    assert restarter.restart_calls == 0  # 再起動もしない
    terminal = [r for r in transport.reports if r["body"]["terminal"]]
    assert terminal and terminal[-1]["body"]["ok"] is False


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


def test_digest_builds_from_store():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    writer = StateWriter(cfg.state_path)
    writer.set_daemon("idle", "見回り完了")
    writer.set_delivery(
        "dlv-d1", "reported", "反映と再起動が完了しました", ref=world["sha_change"]
    )
    writer.set_delivery("dlv-d2", "test_failed", "テストが失敗しました")
    writer.set_delivery("dlv-d3", "deferred", "変更がまだ見えません")
    digest = build_digest(cfg, writer, SubprocessGit())
    assert digest["schema"] == 1
    assert digest["daemon"]["state"] == "idle"
    assert digest["daemon"]["apply_mode"] == cfg.apply_mode
    assert digest["daemon"]["checkout_dir"] == str(world["checkout"])
    assert digest["daemon"]["updated_at"]
    assert digest["checkout_head"] == world["sha_base"]  # 実チェックアウトの HEAD
    by_id = {d["id"]: d for d in digest["deliveries"]}
    assert set(by_id) == {"dlv-d1", "dlv-d2", "dlv-d3"}
    assert by_id["dlv-d1"]["state"] == "reported"
    assert by_id["dlv-d1"]["ok"] is True  # 成功側の終端
    assert by_id["dlv-d1"]["ref"] == world["sha_change"]
    assert by_id["dlv-d1"]["summary"] == "反映と再起動が完了しました"
    assert by_id["dlv-d1"]["updated_at"]
    assert by_id["dlv-d2"]["ok"] is False  # 失敗側の終端
    assert by_id["dlv-d2"]["ref"] is None  # ref の記録が無ければ null
    assert by_id["dlv-d3"]["ok"] is None  # 終端でなければ未確定


def test_digest_deliveries_capped_recent_last():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    writer = StateWriter(cfg.state_path)
    for i in range(25):
        writer.set_delivery(f"dlv-c{i:02d}", "reported", "完了")
    digest = build_digest(cfg, writer, SubprocessGit())
    ids = [d["id"] for d in digest["deliveries"]]
    assert len(ids) == 20  # 直近20件まで
    assert ids == [f"dlv-c{i:02d}" for i in range(5, 25)]  # 古い5件が落ち、最新が末尾


def test_digest_ref_flows_from_pipeline():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-r1", "muse/change", world["sha_change"])])
    pipeline = _pipeline(cfg, transport)
    pipeline.run_once()
    digest = build_digest(cfg, pipeline.state_writer, SubprocessGit())
    by_id = {d["id"]: d for d in digest["deliveries"]}
    assert by_id["dlv-r1"]["state"] == "reported"
    assert by_id["dlv-r1"]["ok"] is True
    assert by_id["dlv-r1"]["ref"] == world["sha_change"]  # _sync 経由で ref が残る
    assert digest["checkout_head"] == world["sha_change"]  # 反映後の HEAD


def _poster_cfg(tmp, world, **overrides):
    base = {
        "intake_url": "https://example.invalid/intake",
        "intake_key": "test-key",
        "digest_heartbeat_polls": 3,
    }
    base.update(overrides)
    return _config(tmp, world, **base)


def test_send_script_bad_bytes_do_not_raise():
    """dd-20261109-04 の教訓: 相手の出力が想定外のバイト列でも
    UnicodeDecodeError で落ちず、"error" として返ること。"""
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "bad_send.py"
    lines = ["import sys", "sys.stdout.buffer.write(bytes([255, 254, 0]) + b'bad')"]
    script.write_bytes((chr(10).join(lines) + chr(10)).encode("utf-8"))
    kind, _message = SendScriptRestarter(sys.executable, script).check()
    assert kind == "error"
    # 生存確認も例外で落ちない（真偽どちらでもよい）
    assert SendScriptLiveness(sys.executable, script).check() in (True, False)


def test_send_script_timeout_is_error_not_hang():
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "slow_send.py"
    lines = ["import time", "time.sleep(30)"]
    script.write_bytes((chr(10).join(lines) + chr(10)).encode("utf-8"))
    restarter = SendScriptRestarter(sys.executable, script, timeout_sec=1.0)
    kind, message = restarter.check()
    assert kind == "error" and "タイムアウト" in message


def test_restarter_exception_becomes_rollback_failed_not_crash():
    """再起動アダプタが例外を投げても、見回りは例外で終わらず
    ロールバック失敗として終端報告されること。"""

    class RaisingRestarter:
        def check(self):
            raise RuntimeError("boom")

        def restart(self):
            raise RuntimeError("boom")

    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _config(tmp, world)
    transport = FakeTransport([_delivery("dlv-raise", "muse/change", world["sha_change"])])
    records = _pipeline(cfg, transport, restarter=RaisingRestarter()).run_once()
    assert records[0].state == DeliveryState.ROLLBACK_FAILED, records[0].history
    terminal = [r for r in transport.reports if r["body"]["terminal"]]
    assert terminal and terminal[-1]["body"]["ok"] is False


def test_guarded_run_once_swallows_exceptions():
    from deploy_daemon.__main__ import _guarded_run_once

    class FakeLogger:
        def __init__(self):
            self.events = []

        def log(self, event, **fields):
            self.events.append(event)

    class FakeStateWriter:
        def __init__(self):
            self.daemon = None

        def set_daemon(self, state, summary):
            self.daemon = (state, summary)

    class BoomPipeline:
        def __init__(self):
            self.logger = FakeLogger()
            self.state_writer = FakeStateWriter()

        def run_once(self):
            raise RuntimeError("boom")

    boom = BoomPipeline()
    assert _guarded_run_once(boom) is None
    assert boom.logger.events == ["loop_error"]
    assert boom.state_writer.daemon is not None
    assert boom.state_writer.daemon[0] == "error"

    class OkPipeline:
        @staticmethod
        def run_once():
            return ["rec"]

    assert _guarded_run_once(OkPipeline()) == ["rec"]


def test_digest_poster_posts_on_processed():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _poster_cfg(tmp, world)
    writer = StateWriter(cfg.state_path)
    writer.set_delivery("dlv-p1", "reported", "完了", ref=world["sha_change"])
    transport = RecordingTransport()
    poster = DigestPoster(cfg, transport, writer, SubprocessGit())
    assert poster.maybe_post(True) is True  # 処理があった見回りでは送る
    assert len(transport.calls) == 1
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://example.invalid/intake/daemon-state"
    assert headers["x-deploy-key"] == "test-key"
    digest = json.loads(body.decode("utf-8"))
    assert digest["schema"] == 1
    assert digest["deliveries"][0]["id"] == "dlv-p1"


def test_digest_poster_heartbeat_cadence():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _poster_cfg(tmp, world)  # heartbeat = 3回に1回
    transport = RecordingTransport()
    poster = DigestPoster(cfg, transport, StateWriter(cfg.state_path), SubprocessGit())
    assert poster.maybe_post(False) is False  # 1回目: まだ送らない
    assert poster.maybe_post(False) is False  # 2回目: まだ送らない
    assert transport.calls == []
    assert poster.maybe_post(False) is True  # 3回目: ハートビートで送る
    assert len(transport.calls) == 1
    assert poster.maybe_post(False) is False  # 成功したら数え直し


def test_digest_poster_swallows_errors():
    tmp = Path(tempfile.mkdtemp())
    world = _make_world(tmp)
    cfg = _poster_cfg(tmp, world)
    writer = StateWriter(cfg.state_path)
    raising = DigestPoster(cfg, RaisingTransport(), writer, SubprocessGit())
    assert raising.maybe_post(True) is False  # 例外を投げず False
    failing = DigestPoster(cfg, RecordingTransport(status=500), writer, SubprocessGit())
    assert failing.maybe_post(True) is False  # 200 でなければ失敗として扱う
    off_transport = RecordingTransport()
    off = DigestPoster(
        _poster_cfg(tmp, world, digest_enabled=False),
        off_transport,
        writer,
        SubprocessGit(),
    )
    assert off.maybe_post(True) is False  # 無効化時は処理があっても送らない
    assert off_transport.calls == []


def test_digest_config_overrides():
    tmp = Path(tempfile.mkdtemp())
    base = tmp / "cfg"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps({"digest_enabled": False, "digest_heartbeat_polls": 5}),
        encoding="utf-8",
    )
    cfg = load_config(base_dir=base, environ={})
    assert cfg.digest_enabled is False
    assert cfg.digest_heartbeat_polls == 5
    cfg2 = load_config(
        base_dir=base,
        environ={
            "DEPLOY_DAEMON_DIGEST_ENABLED": "true",
            "DEPLOY_DAEMON_DIGEST_HEARTBEAT_POLLS": "2",
        },
    )
    assert cfg2.digest_enabled is True  # 環境変数が優先する
    assert cfg2.digest_heartbeat_polls == 2
    cfg3 = load_config(base_dir=tmp / "cfg-empty", environ={})
    assert cfg3.digest_enabled is True  # 既定は有効
    assert cfg3.digest_heartbeat_polls == 10  # 既定は10回に1回


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
