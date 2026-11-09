"""front_desk.playbooks — プレイブック (モデルなし実行)。

プレイブックは「手順・ガード・検証・報告」の4点セットを1単位と
する定型操作。tier="playbook" の依頼はモデルを起動せず、ここに
登録されたコードがそのまま実行する。設計は claude-bridge の
playbook-spec.md (A9 の具体化)。

作法は配備係 (deploy_daemon.adapters) の実績に倣う:
- 外部コマンドは差し替え可能な Runner 経由で実行し、単体テスト
  では偽物に替える
- 出力は errors="replace" で読む (相手の文字コードで落ちない)
- tray-tools 本体への接触は traytools_send.py の口だけを使う
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import traceback
from typing import Optional

from . import model
from .executor import ExecOutcome

GIT_TIMEOUT_SEC = 180
SEND_TIMEOUT_SEC = 60
STATUS_TIMEOUT_SEC = 30


# -- コマンド実行 ------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class CommandResult:
    returncode: Optional[int]  # 時間切れ・起動失敗は None
    output: str = ""
    timed_out: bool = False


class SubprocessRunner:
    """実コマンドの実行。deploy_daemon.adapters と同じ読み方。"""

    def run(self, argv, timeout_sec: float) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output if isinstance(exc.output, str) else ""
            err = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(None, (out or "") + (err or ""),
                                 timed_out=True)
        except OSError as exc:
            return CommandResult(None, f"実行を開始できませんでした: {exc}")
        return CommandResult(
            proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


class StepLog:
    """報告 detail に載せる、機械で読める形の step 記録。"""

    def __init__(self, playbook_id: str):
        self.lines = [f"playbook: {playbook_id}"]

    def step(self, name: str, status: str, extra: str = "") -> None:
        line = f"step {name}: {status}"
        if extra:
            line += f" ({extra})"
        self.lines.append(line)

    def stopped(self, expected: str, actual: str) -> None:
        self.lines.append(f"stopped: expected {expected} / actual {actual}")

    def text(self) -> str:
        return "\n".join(self.lines)


@dataclasses.dataclass(frozen=True)
class PlaybookContext:
    runner: SubprocessRunner
    settings: dict
    sleep_fn: object = time.sleep


def _git(ctx: PlaybookContext, repo: str, *args: str,
         timeout: float = GIT_TIMEOUT_SEC) -> CommandResult:
    argv = ["git", "-c", f"safe.directory={repo}", "-C", repo, *args]
    return ctx.runner.run(argv, timeout)


def _send(ctx: PlaybookContext, python_exe: str, send_script: str,
          *args: str, timeout: float = SEND_TIMEOUT_SEC) -> CommandResult:
    return ctx.runner.run([python_exe, send_script, *args], timeout)


def _blocked(log: StepLog, summary: str) -> ExecOutcome:
    return ExecOutcome(ok=False, state=model.RESULT_BLOCKED,
                       summary=summary, detail=log.text())


def _failed(log: StepLog, summary: str) -> ExecOutcome:
    return ExecOutcome(ok=False, summary=summary, detail=log.text())


# -- PB-1: traytools_sync ------------------------------------------------

def traytools_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """tray-tools 本体へ main を反映し、再起動して着地を確認する。"""
    log = StepLog("traytools_sync")
    if params:
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary="traytools_sync に引数はありません",
            detail=log.text())
    repo = ctx.settings.get("repo_path") or r"R:\claude\tray-tools"
    branch = ctx.settings.get("branch") or "main"
    send_script = ctx.settings.get("send_script") or os.path.join(
        repo, "traytools_send.py")
    python_exe = ctx.settings.get("python_executable") or sys.executable

    # ガード1: リポジトリであること
    r = _git(ctx, repo, "rev-parse", "--is-inside-work-tree")
    if r.returncode != 0:
        log.step("guard.repo", "ng", (r.output or "").strip()[:120])
        log.stopped("git repository", "not a repository")
        return _blocked(log, f"対象が git リポジトリではありません: {repo}")
    log.step("guard.repo", "ok")

    # ガード2: 対象ブランチにいること
    r = _git(ctx, repo, "rev-parse", "--abbrev-ref", "HEAD")
    current_branch = (r.output or "").strip()
    if r.returncode != 0 or current_branch != branch:
        log.step("guard.branch", "ng", current_branch)
        log.stopped(f"branch {branch}", current_branch or "unknown")
        return _blocked(
            log, f"ブランチが {branch} ではないため見送りました"
                 f"（現在: {current_branch or '不明'}）")
    log.step("guard.branch", "ok")

    # ガード3: 作業ツリーが clean であること (未保存の変更を踏まない)
    r = _git(ctx, repo, "status", "--porcelain")
    dirty = (r.output or "").strip()
    if r.returncode != 0 or dirty:
        first = dirty.splitlines()[0][:120] if dirty else ""
        log.step("guard.clean", "ng", first)
        log.stopped("clean tree", "dirty" if dirty else "status failed")
        return _blocked(
            log, "作業ツリーに未保存の変更があるため見送りました")
    log.step("guard.clean", "ok")

    # 取り込み先の確認 (fetch は作業ツリーを変えないので、
    # ガードの後・変更の前に置く)
    r = _git(ctx, repo, "fetch", "origin", branch)
    if r.returncode != 0:
        log.step("fetch", "ng", (r.output or "").strip()[-200:])
        log.stopped("fetch ok", "fetch failed")
        return _blocked(log, "git fetch に失敗したため見送りました")
    log.step("fetch", "ok")
    prev = (_git(ctx, repo, "rev-parse", "HEAD").output or "").strip()
    target = (_git(ctx, repo, "rev-parse", f"origin/{branch}").output
              or "").strip()
    if not prev or not target:
        log.step("heads", "ng")
        log.stopped("heads readable", "unreadable")
        return _blocked(log, "HEAD が読めなかったため見送りました")
    if prev == target:
        log.step("heads", "ok", f"already at {target[:7]}")
        return ExecOutcome(
            ok=True, summary=f"tray-tools は既に最新です（{target[:7]}）",
            detail=log.text())
    log.step("heads", "ok", f"{prev[:7]} -> {target[:7]}")

    # ガード4: 再起動してよい状態か (常駐への吹き添え確認。
    # 変更がある場合だけ、反映の直前に確かめる)
    r = _send(ctx, python_exe, send_script, "restart", "--check")
    check_text = (r.output or "").strip()
    if "差し支えありません" in check_text:
        log.step("guard.restart_check", "ok")
    elif "見送るべき" in check_text:
        log.step("guard.restart_check", "blocked", check_text[:120])
        log.stopped("restart allowed", "blocked by resident")
        return _blocked(log, f"常駐の都合で再起動を見送りました: {check_text[:120]}")
    else:
        log.step("guard.restart_check", "ng", check_text[:120])
        log.stopped("restart check readable", "unreadable or no response")
        return _blocked(log, "再起動可否の応答が読めなかったため見送りました")

    # 反映 (ff のみ)
    r = _git(ctx, repo, "merge", "--ff-only", f"origin/{branch}")
    if r.returncode != 0:
        log.step("apply", "ng", (r.output or "").strip()[-200:])
        return _failed(log, "ff-only の反映に失敗しました（変更は入っていません）")
    log.step("apply", "ok")

    # 再起動と検証。失敗したら巻き戻しを1回だけ試す
    problem = ""
    r = _send(ctx, python_exe, send_script, "restart")
    if r.returncode != 0:
        log.step("restart", "ng", (r.output or "").strip()[:200])
        problem = "再起動に失敗しました"
    else:
        log.step("restart", "ok")
        head = (_git(ctx, repo, "rev-parse", "HEAD").output or "").strip()
        if head != target:
            log.step("verify.head", "ng", head[:7])
            problem = "再起動後の HEAD が取り込み先と一致しません"
        else:
            log.step("verify.head", "ok", target[:7])
            r = _send(ctx, python_exe, send_script, "status",
                      timeout=STATUS_TIMEOUT_SEC)
            if r.returncode != 0:
                log.step("verify.liveness", "ng")
                problem = "再起動後の生存確認に失敗しました"
            else:
                log.step("verify.liveness", "ok")
    if not problem:
        return ExecOutcome(
            ok=True,
            summary=f"tray-tools を {prev[:7]} → {target[:7]} に更新し、"
                    "再起動しました",
            detail=log.text())

    # 巻き戻し (ツリーが依然 clean な場合だけ。clean はガード済みだが
    # 反映後に変わっている可能性を再確認してから触る)
    r = _git(ctx, repo, "status", "--porcelain")
    if (r.output or "").strip():
        log.step("rollback", "skipped", "tree no longer clean")
        return _failed(log, problem + "（巻き戻しはしませんでした）")
    r = _git(ctx, repo, "reset", "--hard", prev)
    if r.returncode != 0:
        log.step("rollback", "ng", (r.output or "").strip()[-200:])
        return _failed(log, problem + "（巻き戻しにも失敗しました）")
    log.step("rollback", "ok", f"back to {prev[:7]}")
    r = _send(ctx, python_exe, send_script, "restart")
    if r.returncode == 0:
        r = _send(ctx, python_exe, send_script, "status",
                  timeout=STATUS_TIMEOUT_SEC)
    if r.returncode == 0:
        log.step("rollback.verify", "ok")
        return _failed(log, problem + f"（{prev[:7]} へ巻き戻して復旧しました）")
    log.step("rollback.verify", "ng")
    return _failed(log, problem + "（巻き戻し後の復旧確認に失敗しました）")


# -- 登録簿と実行の振り分け ----------------------------------------------

PLAYBOOKS = {
    "traytools_sync": traytools_sync,
}


class PlaybookExecutor:
    """tier="playbook" の依頼を実行する。モデルの Executor と同じ口。"""

    def __init__(self, config, runner=None):
        self._config = config
        self._runner = runner or SubprocessRunner()

    def run(self, request: model.Request) -> ExecOutcome:
        if request.kind != "ops":
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary=f"プレイブックは kind=ops だけです（kind={request.kind}）")
        try:
            data = json.loads(request.body)
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary="依頼の body が JSON ではありません")
        playbook_id = data.get("playbook")
        params = data.get("params", {})
        if not isinstance(playbook_id, str) or not playbook_id:
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary="依頼に playbook の指定がありません")
        if not isinstance(params, dict):
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary="依頼の params が dict ではありません")
        fn = PLAYBOOKS.get(playbook_id)
        if fn is None:
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary=f"未知のプレイブックです: {playbook_id}")
        if playbook_id not in self._config.allowed_playbooks:
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary=f"プレイブック {playbook_id} はまだ有効化されていません")
        ctx = PlaybookContext(
            runner=self._runner,
            settings=dict(self._config.playbook_settings.get(
                playbook_id, {})))
        try:
            return fn(ctx, params)
        except Exception:
            return ExecOutcome(
                ok=False,
                summary=f"プレイブック {playbook_id} の実行中に例外が発生しました",
                detail=traceback.format_exc()[-2000:])


class RoutingExecutor:
    """tier で実行エンジンを振り分ける (playbook はコード、他はモデル)。"""

    def __init__(self, playbook_executor, model_executor):
        self._playbooks = playbook_executor
        self._models = model_executor

    def run(self, request: model.Request) -> ExecOutcome:
        if request.tier == "playbook":
            return self._playbooks.run(request)
        return self._models.run(request)
