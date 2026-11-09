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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import Optional

from . import activity, model
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


# -- スケジュールタスクの操作 (PB-2 / PB-3 / PB-7 共通) -------------------

PS_TIMEOUT_SEC = 60
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]+$")


def _ps(ctx: PlaybookContext, script: str,
        timeout: float = PS_TIMEOUT_SEC) -> CommandResult:
    return ctx.runner.run(
        ["powershell", "-NoProfile", "-Command", script], timeout)


def _task_stop(ctx: PlaybookContext, task: str) -> CommandResult:
    return _ps(ctx, f"Stop-ScheduledTask -TaskName {task} -ErrorAction Stop")


def _task_start(ctx: PlaybookContext, task: str) -> CommandResult:
    return _ps(ctx, f"Start-ScheduledTask -TaskName {task} -ErrorAction Stop")


def _task_is_running(ctx: PlaybookContext, task: str) -> bool:
    r = _ps(ctx, f"(Get-ScheduledTask -TaskName {task} "
                 "-ErrorAction Stop).State")
    return r.returncode == 0 and "Running" in (r.output or "")


# -- リポジトリ取り込みの共通ガード (PB-2 / PB-3) -------------------------
# PB-1 のガード1-3 と fetch / heads 確認と同じ進み方。PB-1 本体は
# 実績のある形のまま触らず、こちらは新しいプレイブック同士で共用する。

def _guard_repo_branch_clean(ctx: PlaybookContext, log: StepLog,
                             repo: str, branch: str):
    """止める場合は ExecOutcome、進めてよい場合は None を返す。"""
    r = _git(ctx, repo, "rev-parse", "--is-inside-work-tree")
    if r.returncode != 0:
        log.step("guard.repo", "ng", (r.output or "").strip()[:120])
        log.stopped("git repository", "not a repository")
        return _blocked(log, f"対象が git リポジトリではありません: {repo}")
    log.step("guard.repo", "ok")

    r = _git(ctx, repo, "rev-parse", "--abbrev-ref", "HEAD")
    current_branch = (r.output or "").strip()
    if r.returncode != 0 or current_branch != branch:
        log.step("guard.branch", "ng", current_branch)
        log.stopped(f"branch {branch}", current_branch or "unknown")
        return _blocked(
            log, f"ブランチが {branch} ではないため見送りました"
                 f"（現在: {current_branch or '不明'}）")
    log.step("guard.branch", "ok")

    r = _git(ctx, repo, "status", "--porcelain")
    dirty = (r.output or "").strip()
    if r.returncode != 0 or dirty:
        first = dirty.splitlines()[0][:120] if dirty else ""
        log.step("guard.clean", "ng", first)
        log.stopped("clean tree", "dirty" if dirty else "status failed")
        return _blocked(
            log, "作業ツリーに未保存の変更があるため見送りました")
    log.step("guard.clean", "ok")
    return None


def _fetch_heads(ctx: PlaybookContext, log: StepLog, repo: str,
                 branch: str):
    """(止める場合の ExecOutcome, prev, target) を返す。"""
    r = _git(ctx, repo, "fetch", "origin", branch)
    if r.returncode != 0:
        log.step("fetch", "ng", (r.output or "").strip()[-200:])
        log.stopped("fetch ok", "fetch failed")
        return _blocked(log, "git fetch に失敗したため見送りました"), "", ""
    log.step("fetch", "ok")
    prev = (_git(ctx, repo, "rev-parse", "HEAD").output or "").strip()
    target = (_git(ctx, repo, "rev-parse", f"origin/{branch}").output
              or "").strip()
    if not prev or not target:
        log.step("heads", "ng")
        log.stopped("heads readable", "unreadable")
        return _blocked(log, "HEAD が読めなかったため見送りました"), "", ""
    log.step("heads", "ok", f"{prev[:7]} -> {target[:7]}")
    return None, prev, target


def _merge_ff_only(ctx: PlaybookContext, log: StepLog, repo: str,
                   branch: str):
    """ff-only 反映。失敗したら ExecOutcome、成功なら None。"""
    r = _git(ctx, repo, "merge", "--ff-only", f"origin/{branch}")
    if r.returncode != 0:
        log.step("apply", "ng", (r.output or "").strip()[-200:])
        return _failed(log, "ff-only の反映に失敗しました（変更は入っていません）")
    log.step("apply", "ok")
    return None


def _reject_params(log: StepLog, playbook_id: str) -> ExecOutcome:
    return ExecOutcome(
        ok=False, state=model.RESULT_REJECTED,
        summary=f"{playbook_id} に引数はありません",
        detail=log.text())


# -- PB-2: daemon_sync -----------------------------------------------------

def daemon_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """配備係へ muse/deploy-daemon を反映し、タスクを再起動する。"""
    log = StepLog("daemon_sync")
    if params:
        return _reject_params(log, "daemon_sync")
    repo = ctx.settings.get("repo_path") or r"R:\claude\deploy-daemon\app"
    branch = ctx.settings.get("branch") or "muse/deploy-daemon"
    task = ctx.settings.get("task_name") or "DeployDaemon"
    if not TASK_NAME_RE.match(task):
        log.step("guard.task", "ng", task)
        return _blocked(log, f"タスク名が不正です: {task}")

    stopped = _guard_repo_branch_clean(ctx, log, repo, branch)
    if stopped is not None:
        return stopped
    stopped, prev, target = _fetch_heads(ctx, log, repo, branch)
    if stopped is not None:
        return stopped
    if prev == target:
        return ExecOutcome(
            ok=True, summary=f"配備係は既に最新です（{target[:7]}）",
            detail=log.text())

    failed = _merge_ff_only(ctx, log, repo, branch)
    if failed is not None:
        return failed

    # タスク再起動 (止めてから間を空けないと、多重起動防止で新プロセスが
    # 即終了する実績がある)。失敗しても巻き戻しはしない (仕様 §PB-2:
    # 旧コードのまま生きていれば実害がないため)。
    r = _task_stop(ctx, task)
    if r.returncode != 0:
        log.step("task.stop", "ng", (r.output or "").strip()[-200:])
        return _failed(
            log, "配備係のコードは更新しましたが、タスクを止められませんでした"
                 "（手動で確認してください）")
    log.step("task.stop", "ok")
    ctx.sleep_fn(10)
    r = _task_start(ctx, task)
    if r.returncode != 0:
        log.step("task.start", "ng", (r.output or "").strip()[-200:])
        return _failed(log, "配備係のタスクを起動できませんでした")
    log.step("task.start", "ok")

    head = (_git(ctx, repo, "rev-parse", "HEAD").output or "").strip()
    if head != target:
        log.step("verify.head", "ng", head[:7])
        return _failed(log, "再起動後の HEAD が取り込み先と一致しません")
    log.step("verify.head", "ok", target[:7])
    ctx.sleep_fn(2)
    if not _task_is_running(ctx, task):
        log.step("verify.task", "ng", "not Running")
        return _failed(log, "配備係のタスクが Running に戻りませんでした")
    log.step("verify.task", "ok", "Running")
    return ExecOutcome(
        ok=True,
        summary=f"配備係を {prev[:7]} → {target[:7]} に更新し、"
                "タスクを再起動しました",
        detail=log.text())


# -- PB-3: desk_sync ---------------------------------------------------------

def _spawn_delayed_task_restart(ctx: PlaybookContext, task: str,
                                delay_sec: int, gap_sec: int) -> CommandResult:
    """結果を報告した後にタスクを再起動する、切り離したヘルパーを起こす。

    窓口自身を止めると報告 (結果 POST) ができなくなるため、再起動は
    ヘルパー側で delay_sec 待ってから行う (仕様 §PB-3 の特例)。
    ヘルパーは Start-Process で切り離すので、この呼び出し自体は
    すぐ返る。"""
    inner = (f"Start-Sleep -Seconds {delay_sec}; "
             f"Stop-ScheduledTask -TaskName {task}; "
             f"Start-Sleep -Seconds {gap_sec}; "
             f"Start-ScheduledTask -TaskName {task}")
    script = ("Start-Process powershell -WindowStyle Hidden "
              f"-ArgumentList '-NoProfile','-Command','{inner}'")
    return _ps(ctx, script, timeout=30)


def desk_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """窓口自身へ muse/front-desk を反映する。再起動は報告の後。"""
    log = StepLog("desk_sync")
    if params:
        return _reject_params(log, "desk_sync")
    repo = ctx.settings.get("repo_path") or r"R:\claude\front-desk\app"
    branch = ctx.settings.get("branch") or "muse/front-desk"
    task = ctx.settings.get("task_name") or "FrontDesk"
    try:
        delay_sec = int(ctx.settings.get("restart_delay_sec") or 20)
    except (TypeError, ValueError):
        delay_sec = 20
    if not TASK_NAME_RE.match(task):
        log.step("guard.task", "ng", task)
        return _blocked(log, f"タスク名が不正です: {task}")

    stopped = _guard_repo_branch_clean(ctx, log, repo, branch)
    if stopped is not None:
        return stopped
    stopped, prev, target = _fetch_heads(ctx, log, repo, branch)
    if stopped is not None:
        return stopped
    if prev == target:
        return ExecOutcome(
            ok=True, summary=f"窓口は既に最新です（{target[:7]}）",
            detail=log.text())

    failed = _merge_ff_only(ctx, log, repo, branch)
    if failed is not None:
        return failed

    # 報告時点の検証は HEAD 一致まで (仕様 §PB-3)。再起動後の生存は
    # 次回の desk-state の鮮度で Muse 側が確認する。
    head = (_git(ctx, repo, "rev-parse", "HEAD").output or "").strip()
    if head != target:
        log.step("verify.head", "ng", head[:7])
        return _failed(log, "反映後の HEAD が取り込み先と一致しません")
    log.step("verify.head", "ok", target[:7])

    r = _spawn_delayed_task_restart(ctx, task, delay_sec, 10)
    if r.returncode != 0:
        log.step("restart.schedule", "ng", (r.output or "").strip()[-200:])
        return _failed(
            log, "窓口のコードは更新しましたが、遅延再起動を予約できませんでした"
                 "（手動で再起動してください）")
    log.step("restart.schedule", "ok",
             f"delay {delay_sec}s, after this report")
    return ExecOutcome(
        ok=True,
        summary=f"窓口を {prev[:7]} → {target[:7]} に更新しました"
                "（再起動はこの報告の後に遅延実行します）",
        detail=log.text())


# -- PB-6: traytools_restart -------------------------------------------------

def traytools_restart(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """tray-tools の再起動のみ (PB-1 から反映を除いた形)。"""
    log = StepLog("traytools_restart")
    if params:
        return _reject_params(log, "traytools_restart")
    repo = ctx.settings.get("repo_path") or r"R:\claude\tray-tools"
    send_script = ctx.settings.get("send_script") or os.path.join(
        repo, "traytools_send.py")
    python_exe = ctx.settings.get("python_executable") or sys.executable

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

    r = _send(ctx, python_exe, send_script, "restart")
    if r.returncode != 0:
        log.step("restart", "ng", (r.output or "").strip()[:200])
        return _failed(log, "再起動に失敗しました（巻き戻しはしません）")
    log.step("restart", "ok")

    r = _send(ctx, python_exe, send_script, "status",
              timeout=STATUS_TIMEOUT_SEC)
    if r.returncode != 0:
        log.step("verify.liveness", "ng")
        return _failed(log, "再起動後の生存確認に失敗しました")
    log.step("verify.liveness", "ok")
    return ExecOutcome(
        ok=True, summary="tray-tools を再起動しました", detail=log.text())

# -- PB-5: config_set --------------------------------------------------------
# 初期範囲は %LOCALAPPDATA%\traytools\ の合図ファイル4つだけ (仕様 §PB-5)。
# 実パスは引数で受けず、許可リストのキーから引く。窓口・配備係自身の
# config と tray-tools の settings.json は範囲に含めない。

FLAG_FILES = {
    "stall_nudge": "muse_stall_nudge.json",
    "bridge_pause": "muse_bridge_pause.json",
    "coax_realinput": "muse_coax_realinput.json",
    "logview": "muse_logview.json",
}


def _flag_base_dir(ctx: PlaybookContext) -> str:
    configured = ctx.settings.get("base_dir")
    if configured:
        return configured
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "traytools")


def _write_text_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _short_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)[:120]
    except (TypeError, ValueError):
        return str(value)[:120]


def config_set(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """合図ファイルの決まったキーを1つ差し替える。"""
    log = StepLog("config_set")
    file_key = params.get("file")
    key = params.get("key")
    if not isinstance(file_key, str) or file_key not in FLAG_FILES:
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary=f"対象ファイルが許可リストにありません: {file_key}",
            detail=log.text())
    if not isinstance(key, str) or not key:
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary="差し替えるキーの指定がありません",
            detail=log.text())
    if "value" not in params:
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary="新しい値 (value) の指定がありません",
            detail=log.text())
    new_value = params["value"]
    has_expected = "expected" in params
    expected = params.get("expected")

    filename = FLAG_FILES[file_key]
    path = os.path.join(_flag_base_dir(ctx), filename)
    log.step("target", "ok", filename)

    # ガード: JSON として読めること、キーが存在すること、
    # expected が指定されていれば現在値と一致すること。
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_before = fh.read()
        data = json.loads(raw_before)
    except OSError:
        log.step("guard.read", "ng", "file not found")
        log.stopped("readable flag file", "missing")
        return _blocked(log, f"対象ファイルがありません: {filename}")
    except ValueError:
        log.step("guard.read", "ng", "invalid json")
        log.stopped("readable flag file", "invalid json")
        return _blocked(log, f"対象ファイルが JSON として読めません: {filename}")
    if not isinstance(data, dict):
        log.step("guard.read", "ng", "not an object")
        log.stopped("json object", type(data).__name__)
        return _blocked(log, f"対象ファイルの JSON がオブジェクトではありません: {filename}")
    log.step("guard.read", "ok")
    if key not in data:
        log.step("guard.key", "ng", key)
        log.stopped(f"key {key} present", "absent")
        return _blocked(log, f"キー {key} が {filename} にありません")
    old_value = data[key]
    log.step("guard.key", "ok", f"{key}={_short_json(old_value)}")
    if has_expected and old_value != expected:
        log.step("guard.expected", "ng",
                 f"expected {_short_json(expected)}")
        log.stopped(f"{key} == {_short_json(expected)}",
                    _short_json(old_value))
        return _blocked(
            log, f"現在の値が期待と違うため見送りました: {key}={_short_json(old_value)}")
    if has_expected:
        log.step("guard.expected", "ok")

    # 手順: read-modify-write (指定キーだけ差し替える)
    new_data = dict(data)
    new_data[key] = new_value
    new_text = json.dumps(new_data, ensure_ascii=False, indent=2) + "\n"
    try:
        _write_text_atomic(path, new_text)
    except OSError as exc:
        log.step("write", "ng", str(exc)[:120])
        return _failed(log, f"{filename} への書き込みに失敗しました")
    log.step("write", "ok", f"{key}={_short_json(new_value)}")

    # 検証: 読み直して値が一致すること。NG なら変更前へ書き戻す。
    verified = False
    try:
        with open(path, "r", encoding="utf-8") as fh:
            after = json.load(fh)
        verified = isinstance(after, dict) and after.get(key) == new_value
    except (OSError, ValueError):
        verified = False
    if verified:
        log.step("verify.value", "ok")
        return ExecOutcome(
            ok=True,
            summary=f"{filename} の {key} を "
                    f"{_short_json(old_value)} → {_short_json(new_value)} "
                    "に変更しました",
            detail=log.text())
    log.step("verify.value", "ng")
    try:
        _write_text_atomic(path, raw_before)
        log.step("rollback", "ok", "previous content restored")
        return _failed(
            log, "書き込み後の検証が合わなかったため、変更前の内容へ書き戻しました")
    except OSError as exc:
        log.step("rollback", "ng", str(exc)[:120])
        return _failed(
            log, "書き込み後の検証が合わず、書き戻しにも失敗しました"
                 "（手動で確認してください）")


# -- PB-4: deploy_approve ------------------------------------------------------
# 【有効化に関する注意】このプレイブックは、Nagato の明示の了承が
# ある場合にだけ依頼を登録する前提のもの (仕様 §PB-4 の前提)。
# コードは置くが、allowed_playbooks への追加 (有効化) は了承を
# 得てから別途行う。ここには有効化の経路を含めない。
#
# 実機構は配備係 (tray-tools muse/deploy-daemon ブランチの
# deploy_daemon) の実コードに合わせる:
# - 承認は base_dir/approvals/<delivery_id>.approve の空ファイル
#   (deploy_daemon.pipeline._approval_path。id の無害化も同じ式)
# - 届け出の状態は base_dir/state.json の deliveries[<id>].state
#   (deploy_daemon.statefile の形。ダイジェスト相当の読み取り)
# - 終端状態は deploy_daemon.model.TERMINAL_STATES と同じ集合
# base_dir の実値は配備係 config 依存のため、playbook_settings で
# 受ける。未設定なら not_configured で fail-closed に止める。

DELIVERY_ID_RE = re.compile(r"^dd-\d{8}-\d{2,}$")
DELIVERY_TERMINAL_STATES = frozenset({
    "reported", "rejected", "test_failed", "conflict", "held",
    "rolled_back", "rollback_failed", "fetch_failed",
})


def _approval_filename(delivery_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", delivery_id)
    return f"{safe}.approve"


def deploy_approve(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """配備の承認ファイルを置く (案A の1タップ承認の置き換え)。"""
    log = StepLog("deploy_approve")
    delivery_id = params.get("delivery_id")
    if not isinstance(delivery_id, str) \
            or not DELIVERY_ID_RE.match(delivery_id):
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary="delivery_id の形式が dd-YYYYMMDD-NN ではありません",
            detail=log.text())

    approvals_dir = ctx.settings.get("approvals_dir")
    state_path = ctx.settings.get("state_path")
    base_dir = ctx.settings.get("daemon_base_dir")
    if base_dir and not approvals_dir:
        approvals_dir = os.path.join(base_dir, "approvals")
    if base_dir and not state_path:
        state_path = os.path.join(base_dir, "state.json")
    if not approvals_dir or not state_path:
        log.step("guard.config", "ng", "not_configured")
        log.stopped(
            "playbook_settings.deploy_approve.daemon_base_dir "
            "(or approvals_dir/state_path)", "missing")
        return _blocked(
            log, "配備係の置き場が未設定のため実行できません"
                 "（not_configured: daemon_base_dir）")

    # ガード1: 届け出が存在し、終端状態でないこと
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state_data = json.load(fh)
    except OSError:
        log.step("guard.delivery", "ng", "state file not found")
        log.stopped("readable daemon state", "missing")
        return _blocked(log, "配備係の状態ファイルが読めません")
    except ValueError:
        log.step("guard.delivery", "ng", "invalid json")
        log.stopped("readable daemon state", "invalid json")
        return _blocked(log, "配備係の状態ファイルが JSON として読めません")
    deliveries = state_data.get("deliveries") \
        if isinstance(state_data, dict) else None
    entry = deliveries.get(delivery_id) \
        if isinstance(deliveries, dict) else None
    if not isinstance(entry, dict):
        log.step("guard.delivery", "ng", "not found")
        log.stopped(f"delivery {delivery_id} exists", "absent")
        return _blocked(log, f"届け出が見つかりません: {delivery_id}")
    delivery_state = entry.get("state")
    log.step("guard.delivery", "ok", f"state={delivery_state}")
    if delivery_state in DELIVERY_TERMINAL_STATES:
        log.step("guard.terminal", "ng", str(delivery_state))
        log.stopped("not terminal", str(delivery_state))
        return _blocked(
            log, f"届け出は既に終端状態です: {delivery_id} ({delivery_state})")
    log.step("guard.terminal", "ok")

    # ガード2 (冪等): 承認ファイルが既にあれば設置済みで done
    approval_path = os.path.join(
        approvals_dir, _approval_filename(delivery_id))
    if os.path.exists(approval_path):
        log.step("guard.approval", "ok", "already exists")
        return ExecOutcome(
            ok=True,
            summary=f"承認ファイルは設置済みです: {delivery_id}",
            detail=log.text())
    log.step("guard.approval", "ok", "not yet")

    # 手順: 承認ファイルを置く。検証: 存在すること。
    try:
        os.makedirs(approvals_dir, exist_ok=True)
        with open(approval_path, "wb"):
            pass
    except OSError as exc:
        log.step("write", "ng", str(exc)[:120])
        return _failed(log, "承認ファイルを置けませんでした")
    log.step("write", "ok", os.path.basename(approval_path))
    if not os.path.exists(approval_path):
        log.step("verify.exists", "ng")
        return _failed(log, "承認ファイルを置いたはずが確認できません")
    log.step("verify.exists", "ok")
    return ExecOutcome(
        ok=True,
        summary=f"配備の承認ファイルを置きました: {delivery_id}"
                "（反映は配備係の次回見回りが行います）",
        detail=log.text())


# -- PB-7: watcher_sync --------------------------------------------------------
# 見張り (muse_monitor.py) の正本の置き場は未決 (仕様 §PB-7 の前提)
# のため、正本パスは playbook_settings で受ける (コードに固定しない)。
# 未設定なら not_configured で fail-closed に止める。
# 再起動の作法は実績どおり Stop → 10秒以上 → Start (短いと多重起動
# 防止で新プロセスが即終了し、旧版が残る)。

def _sha256_of(path: str):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _read_bytes(path: str):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_bytes_atomic(path: str, data: bytes) -> bool:
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _events_marker(events_path: str) -> int:
    try:
        return os.path.getsize(events_path)
    except OSError:
        return 0


def _fresh_monitor_start(events_path: str, marker: int) -> bool:
    """marker 以降に monitor_start が記録されていれば True。"""
    try:
        with open(events_path, "rb") as fh:
            fh.seek(marker)
            new_content = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return "monitor_start" in new_content


def watcher_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """見張りの正本を取り込んで差し替え、タスクを再起動する。"""
    log = StepLog("watcher_sync")
    if params:
        return _reject_params(log, "watcher_sync")
    source = ctx.settings.get("source_path")
    if not source:
        log.step("guard.config", "ng", "not_configured")
        log.stopped("playbook_settings.watcher_sync.source_path", "missing")
        return _blocked(
            log, "見張りの正本の置き場が未設定のため実行できません"
                 "（not_configured: source_path。正本の git 管理先が未決）")
    target = ctx.settings.get("target_path") \
        or r"R:\claude\claude-bridge\muse_monitor.py"
    task = ctx.settings.get("task_name") or "MuseBridgeMonitor"
    target_dir = os.path.dirname(target)
    backup_dir = ctx.settings.get("backup_dir") or target_dir
    events_path = ctx.settings.get("events_path") or os.path.join(
        target_dir, "muse_monitor.events.json")
    try:
        gap_sec = int(ctx.settings.get("restart_gap_sec") or 12)
    except (TypeError, ValueError):
        gap_sec = 12
    gap_sec = max(gap_sec, 10)  # 10秒未満は多重起動防止で失敗する実績
    try:
        verify_timeout = int(ctx.settings.get("verify_timeout_sec") or 60)
    except (TypeError, ValueError):
        verify_timeout = 60
    if not TASK_NAME_RE.match(task):
        log.step("guard.task", "ng", task)
        return _blocked(log, f"タスク名が不正です: {task}")

    # ガード: 正本と現行があり、ハッシュが違い、バックアップ先が書ける
    if not os.path.isfile(source):
        log.step("guard.source", "ng", "not found")
        log.stopped("source file exists", "missing")
        return _blocked(log, f"正本ファイルがありません: {source}")
    log.step("guard.source", "ok")
    if not os.path.isfile(target):
        log.step("guard.target", "ng", "not found")
        log.stopped("installed watcher exists", "missing")
        return _blocked(log, f"設置済みの見張りがありません: {target}")
    log.step("guard.target", "ok")
    if not os.path.isdir(backup_dir) or not os.access(backup_dir, os.W_OK):
        log.step("guard.backup", "ng", backup_dir)
        log.stopped("writable backup dir", "not writable")
        return _blocked(log, f"バックアップ先に書けません: {backup_dir}")
    log.step("guard.backup", "ok")
    source_hash = _sha256_of(source)
    target_hash = _sha256_of(target)
    if source_hash is None or target_hash is None:
        log.step("guard.hash", "ng")
        log.stopped("hashes readable", "unreadable")
        return _blocked(log, "ファイルのハッシュが読めませんでした")
    if source_hash == target_hash:
        log.step("guard.hash", "ok", f"already at {source_hash[:12]}")
        return ExecOutcome(
            ok=True,
            summary="見張りは既に正本と同じ内容です（差し替えなし）",
            detail=log.text())
    log.step("guard.hash", "ok",
             f"{target_hash[:12]} -> {source_hash[:12]}")

    source_bytes = _read_bytes(source)
    current_bytes = _read_bytes(target)
    if source_bytes is None or current_bytes is None:
        log.step("read", "ng")
        return _failed(log, "ファイルの読み取りに失敗しました（変更はしていません）")
    log.step("read", "ok")

    # 手順1: 現行を退避する
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = os.path.splitext(os.path.basename(target))[0]
    backup_path = os.path.join(backup_dir, f"{stem}_{stamp}_backup.py")
    if not _write_bytes_atomic(backup_path, current_bytes):
        log.step("backup", "ng", backup_path)
        return _failed(log, "バックアップが作れなかったため中止しました"
                            "（変更はしていません）")
    log.step("backup", "ok", os.path.basename(backup_path))

    def rollback(reason: str) -> ExecOutcome:
        """バックアップから戻して再起動し、failed で報告する。"""
        restored = _write_bytes_atomic(target, current_bytes)
        log.step("rollback.file", "ok" if restored else "ng")
        _task_stop(ctx, task)
        ctx.sleep_fn(gap_sec)
        r = _task_start(ctx, task)
        log.step("rollback.restart", "ok" if r.returncode == 0 else "ng")
        if restored and r.returncode == 0:
            return _failed(log, reason + "（バックアップから戻して再起動しました）")
        return _failed(log, reason + "（巻き戻しが完全にはできませんでした。"
                                     "手動で確認してください）")

    # 手順2: 差し替える
    if not _write_bytes_atomic(target, source_bytes):
        log.step("replace", "ng")
        return rollback("正本への差し替えに失敗しました")
    log.step("replace", "ok")

    # 手順3: タスク再起動 (Stop → 10秒以上 → Start)
    marker = _events_marker(events_path)
    r = _task_stop(ctx, task)
    if r.returncode != 0:
        log.step("task.stop", "ng", (r.output or "").strip()[-200:])
        return rollback("見張りのタスクを止められませんでした")
    log.step("task.stop", "ok")
    ctx.sleep_fn(gap_sec)
    r = _task_start(ctx, task)
    if r.returncode != 0:
        log.step("task.start", "ng", (r.output or "").strip()[-200:])
        return rollback("見張りのタスクを起動できませんでした")
    log.step("task.start", "ok")

    # 検証: 設置ファイルのハッシュ一致と、新しい monitor_start の到着
    if _sha256_of(target) != source_hash:
        log.step("verify.hash", "ng")
        return rollback("差し替え後のハッシュが正本と一致しません")
    log.step("verify.hash", "ok", source_hash[:12])
    fresh = False
    waited = 0
    while waited <= verify_timeout:
        if _fresh_monitor_start(events_path, marker):
            fresh = True
            break
        ctx.sleep_fn(2)
        waited += 2
    if not fresh:
        log.step("verify.monitor_start", "ng",
                 f"no new monitor_start in {verify_timeout}s")
        return rollback("新しい monitor_start が確認できませんでした")
    log.step("verify.monitor_start", "ok")
    return ExecOutcome(
        ok=True,
        summary=f"見張りを正本へ更新しました"
                f"（{target_hash[:7]} → {source_hash[:7]}、"
                f"バックアップ: {os.path.basename(backup_path)}）",
        detail=log.text())


# -- PB-8: pc_capture -----------------------------------------------------------
# 常駐の capture の口 (traytools_send.py) で画面を撮り、保存された
# 画像を OneDrive の受け渡しフォルダへ写す。スマホからは OneDrive
# 側で見る (2026-11-10 の remote_capture 実験の、窓口から叩く版)。

CAPTURE_FOLDER_NAME = "traytools-remote-capture"


def pc_capture(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """PC の画面を撮って OneDrive のフォルダへ届ける。"""
    log = StepLog("pc_capture")
    screen = params.get("screen")
    if screen is not None:
        if isinstance(screen, bool) or not isinstance(screen, int) \
                or screen < 1:
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary="screen は1以上の整数で指定してください",
                detail=log.text())
    repo = ctx.settings.get("repo_path") or r"R:\claude\tray-tools"
    send_script = ctx.settings.get("send_script") or os.path.join(
        repo, "traytools_send.py")
    python_exe = ctx.settings.get("python_executable") or sys.executable

    # ガード1: 届け先 (OneDrive) が決まること
    onedrive = ctx.settings.get("onedrive_dir") \
        or os.environ.get("OneDrive")
    if not onedrive:
        log.step("guard.onedrive", "ng", "not_configured")
        log.stopped("OneDrive folder", "missing")
        return _blocked(
            log, "OneDrive のフォルダが分からないため実行できません"
                 "（not_configured: OneDrive）")
    log.step("guard.onedrive", "ok")
    dest_dir = os.path.join(onedrive, CAPTURE_FOLDER_NAME)

    # ガード2: 常駐が応答すること
    r = _send(ctx, python_exe, send_script, "status",
              timeout=STATUS_TIMEOUT_SEC)
    if r.returncode != 0:
        log.step("guard.liveness", "ng")
        log.stopped("resident status ok", "no response")
        return _blocked(log, "常駐が応答しないため見送りました")
    log.step("guard.liveness", "ok")

    # 手順: 撮影する (応答の OK 行の次に保存先パスが返る)
    args = ["capture"]
    if screen is not None:
        args.append(str(screen))
    r = _send(ctx, python_exe, send_script, *args)
    output = (r.output or "").strip()
    if r.returncode != 0 or "OK" not in output:
        log.step("capture", "ng", output[-200:])
        return _failed(log, f"撮影に失敗しました: {output[-120:]}")
    log.step("capture", "ok")
    saved_path = None
    for line in reversed(output.splitlines()):
        candidate = line.strip().strip('"')
        if candidate and os.path.isfile(candidate):
            saved_path = candidate
            break
    if saved_path is None:
        log.step("capture.path", "ng")
        return _failed(log, "撮影の応答から保存先パスが読めませんでした")
    log.step("capture.path", "ok", saved_path)

    # 手順: OneDrive の受け渡しフォルダへ写す
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        log.step("copy.mkdir", "ng", str(exc)[:120])
        return _failed(log, f"届け先フォルダを作れません: {dest_dir}")
    dest_path = os.path.join(dest_dir, os.path.basename(saved_path))
    if os.path.abspath(dest_path) != os.path.abspath(saved_path):
        try:
            shutil.copy2(saved_path, dest_path)
        except OSError as exc:
            log.step("copy", "ng", str(exc)[:120])
            return _failed(log, "画像の受け渡しコピーに失敗しました")
    log.step("copy", "ok", dest_path)

    # 検証: 届け先にあり、大きさが一致すること
    try:
        same_size = os.path.getsize(dest_path) == os.path.getsize(saved_path)
    except OSError:
        same_size = False
    if not same_size:
        log.step("verify.size", "ng")
        return _failed(log, "届け先の画像の大きさが元と一致しません")
    log.step("verify.size", "ok")
    return ExecOutcome(
        ok=True,
        summary=f"画面を撮って OneDrive へ届けました: {dest_path}",
        detail=log.text())


# -- 登録簿と実行の振り分け ----------------------------------------------

PLAYBOOKS = {
    "traytools_sync": traytools_sync,
    "daemon_sync": daemon_sync,
    "desk_sync": desk_sync,
    "deploy_approve": deploy_approve,
    "config_set": config_set,
    "traytools_restart": traytools_restart,
    "watcher_sync": watcher_sync,
    "pc_capture": pc_capture,
}


class PlaybookExecutor:
    """tier="playbook" の依頼を実行する。モデルの Executor と同じ口。"""

    def __init__(self, config, runner=None, activity_path=None):
        self._config = config
        self._runner = runner or SubprocessRunner()
        self._activity_path = activity_path

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
        t0 = time.time()
        activity.begin(playbook_id, request.id,
                       path=self._activity_path, now=t0)
        try:
            outcome = fn(ctx, params)
        except Exception:
            outcome = ExecOutcome(
                ok=False,
                summary=f"プレイブック {playbook_id} の実行中に例外が発生しました",
                detail=traceback.format_exc()[-2000:])
        activity.finish(playbook_id, request.id, outcome,
                        path=self._activity_path, started_at=t0)
        return outcome


class RoutingExecutor:
    """tier で実行エンジンを振り分ける (playbook はコード、他はモデル)。"""

    def __init__(self, playbook_executor, model_executor):
        self._playbooks = playbook_executor
        self._models = model_executor

    def run(self, request: model.Request) -> ExecOutcome:
        if request.tier == "playbook":
            return self._playbooks.run(request)
        return self._models.run(request)
