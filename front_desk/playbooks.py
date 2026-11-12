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

import base64
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
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

    def run_bytes(self, argv, timeout_sec: float) -> CommandResult:
        """出力をバイトで受け、UTF-8 → cp932 の順に読む (PB-4031 用)。"""
        try:
            proc = subprocess.run(
                list(argv), capture_output=True, timeout=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            raw = (exc.output or b"") + (exc.stderr or b"")
            return CommandResult(None, _decode_output(raw), timed_out=True)
        except OSError as exc:
            return CommandResult(None, f"実行を開始できませんでした: {exc}")
        raw = (proc.stdout or b"") + (proc.stderr or b"")
        return CommandResult(proc.returncode, _decode_output(raw))


def _decode_output(raw: bytes) -> str:
    for enc in ("utf-8", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


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


# -- PB-1001: traytools_sync ------------------------------------------------

def traytools_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """tray-tools 本体へ main を反映し、再起動して着地を確認する。"""
    log = StepLog("pb1001")
    if params:
        return ExecOutcome(
            ok=False, state=model.RESULT_REJECTED,
            summary="pb1001 に引数はありません",
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


# -- PB-1002: daemon_sync -----------------------------------------------------

def daemon_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """配備係へ muse/deploy-daemon を反映し、タスクを再起動する。"""
    log = StepLog("pb1002")
    if params:
        return _reject_params(log, "pb1002")
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


# -- PB-1003: desk_sync ---------------------------------------------------------

def _spawn_delayed_task_restart(ctx: PlaybookContext, task: str,
                                delay_sec: int, gap_sec: int) -> CommandResult:
    """結果を報告した後にタスクを再起動する、切り離したヘルパーを起こす。

    窓口自身を止めると報告 (結果 POST) ができなくなるため、再起動は
    ヘルパー側で delay_sec 待ってから行う (仕様 §PB-3 の特例)。
    ヘルパーは Start-Process で切り離すので、この呼び出し自体は
    すぐ返る。

    Stop/Start-ScheduledTask だけでは本体が切り替わらない。
    2026-11-10 09:20 JST の実測で、タスクの停止→開始を回しても
    切り離されて動いている本体プロセスが生き残り、合図ファイルが
    作られず古い窓口が動き続けた。同じ日に手で直した手順
    (CommandLine に front_desk を含む python プロセスを止めてから
    タスクを起こす) を、ヘルパーの中に同じ順序で入れてある。
    ヘルパー自身は powershell なので、python の絞り込みには当たらない。

    絞り込みの文字列は単一引用符で書く。ただし inner をそのまま
    単一引用符で囲んで Start-Process に渡すと、中の単一引用符
    ('python*' など) のところで文字列が切れて文法が壊れ、
    Start-Process の例外になる (2026-11-12 実測、req-20261112-68)。
    inner は UTF-16LE の base64 にして -EncodedCommand で渡す。
    base64 には引用符が含まれないので、この問題が丸ごと消える。
    """
    kill = ("Get-CimInstance Win32_Process | Where-Object "
            "{ $_.Name -like 'python*' -and "
            "$_.CommandLine -like '*front_desk*' } | ForEach-Object "
            "{ Stop-Process -Id $_.ProcessId -Force }")
    inner = (f"Start-Sleep -Seconds {delay_sec}; "
             f"Stop-ScheduledTask -TaskName {task}; "
             f"{kill}; "
             f"Start-Sleep -Seconds {gap_sec}; "
             f"Start-ScheduledTask -TaskName {task}")
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    script = ("Start-Process powershell -WindowStyle Hidden "
              f"-ArgumentList '-NoProfile','-EncodedCommand','{encoded}'")
    return _ps(ctx, script, timeout=30)


def desk_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """窓口自身へ muse/front-desk を反映する。再起動は報告の後。"""
    log = StepLog("pb1003")
    if params:
        return _reject_params(log, "pb1003")
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


# -- PB-4006: traytools_restart -------------------------------------------------

def traytools_restart(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """tray-tools の再起動のみ (PB-1 から反映を除いた形)。"""
    log = StepLog("pb4006")
    if params:
        return _reject_params(log, "pb4006")
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

# -- PB-3005: config_set --------------------------------------------------------
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
    log = StepLog("pb3005")
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


# -- PB-2004: deploy_approve ------------------------------------------------------
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
    log = StepLog("pb2004")
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
            "playbook_settings.pb2004.daemon_base_dir "
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


# -- PB-1007: watcher_sync --------------------------------------------------------
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


def _monitor_start_ts(text: str):
    """events ファイルの本文から monitor_start の ts 一覧を取り出す。

    見張りの events ファイルは追記ではなく全体の書き直し (deque の
    dump) なので、バイト位置で前後を比べることはできない。ts の
    新旧で判定する。ts が無い記録は None として数だけ数える。"""
    out = []
    for m in re.finditer(r'\{[^{}]*"ev"\s*:\s*"monitor_start"[^{}]*\}',
                         text):
        t = re.search(r'"ts"\s*:\s*([0-9]+(?:\.[0-9]+)?)', m.group(0))
        out.append(float(t.group(1)) if t else None)
    return out


def _read_events_text(events_path: str) -> str:
    try:
        with open(events_path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _events_marker(events_path: str):
    """再起動前の monitor_start の (件数, 最大 ts)。"""
    starts = _monitor_start_ts(_read_events_text(events_path))
    dated = [t for t in starts if t is not None]
    return (len(starts), max(dated) if dated else None)


def _fresh_monitor_start(events_path: str, marker) -> bool:
    """marker の時点より新しい monitor_start が記録されていれば True。
    deque が溢れて古い記録が押し出されても、ts の新しさで拾える。"""
    count0, max0 = marker
    starts = _monitor_start_ts(_read_events_text(events_path))
    if len(starts) > count0:
        return True
    return any(t is not None and (max0 is None or t > max0)
               for t in starts)


def watcher_sync(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """見張りの正本を取り込んで差し替え、タスクを再起動する。"""
    log = StepLog("pb1007")
    if params:
        return _reject_params(log, "pb1007")
    source = ctx.settings.get("source_path")
    if not source:
        log.step("guard.config", "ng", "not_configured")
        log.stopped("playbook_settings.pb1007.source_path", "missing")
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


# -- PB-5008: pc_capture -----------------------------------------------------------
# 常駐の capture の口 (traytools_send.py) で画面を撮り、保存された
# 画像を OneDrive の受け渡しフォルダへ写す。スマホからは OneDrive
# 側で見る (2026-11-10 の remote_capture 実験の、窓口から叩く版)。

CAPTURE_FOLDER_NAME = "traytools-remote-capture"


def pc_capture(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """PC の画面を撮って OneDrive のフォルダへ届ける。"""
    log = StepLog("pb5008")
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


# -- PB-4031: powershell_run --------------------------------------------

PS_SCRIPT_MAX_CHARS = 20000
PS_TIMEOUT_DEFAULT_SEC = 120
PS_TIMEOUT_MIN_SEC = 10
PS_TIMEOUT_MAX_SEC = 600
PS_OUTPUT_TAIL_CHARS = 6000
_PS_PROLOGUE = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\n")


def powershell_run(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """依頼に書かれた PowerShell スクリプトを実行する。

    前提はチャット承認 (承認されたスクリプトだけが依頼として届く)。
    スクリプト全文は依頼 body に残るので、報告には sha256 と文字数、
    出力の末尾を残して監査できる形にする。
    """
    log = StepLog("pb4031")
    unknown = sorted(set(params) - {"script", "timeout_sec"})
    if unknown:
        return _blocked(log, f"未知の params です: {unknown}")
    script = params.get("script")
    if not isinstance(script, str) or not script.strip():
        return _blocked(log, "script が空です")
    if len(script) > PS_SCRIPT_MAX_CHARS:
        return _blocked(
            log, f"script が長すぎます ({len(script)} > {PS_SCRIPT_MAX_CHARS})")
    timeout = params.get("timeout_sec", PS_TIMEOUT_DEFAULT_SEC)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return _blocked(log, "timeout_sec が数値ではありません")
    timeout = max(PS_TIMEOUT_MIN_SEC,
                  min(PS_TIMEOUT_MAX_SEC, float(timeout)))
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]
    log.step("script", "received", f"sha256:{digest} chars:{len(script)}")
    return _run_ps_script(ctx, log, script, timeout)


def _run_ps_script(ctx: PlaybookContext, log: StepLog, script: str,
                   timeout: float) -> ExecOutcome:
    """スクリプト文字列を一時 .ps1 にして実行し、末尾つきで報告する。

    PB-4031 と PB-4033 (保存庫からの実行) で共用する。検証 (空・長さ・
    型) は呼び出し側の責務で、ここは実行と報告だけを行う。
    """
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="pb4031_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\xef\xbb\xbf"
                     + (_PS_PROLOGUE + script).encode("utf-8"))
        log.step("write", "ok")
        result = ctx.runner.run_bytes(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", path], timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    output = result.output or ""
    tail = output[-PS_OUTPUT_TAIL_CHARS:]
    if len(output) > PS_OUTPUT_TAIL_CHARS:
        tail = (f"…(先頭 {len(output) - PS_OUTPUT_TAIL_CHARS} 文字を省略)\n"
                + tail)
    if result.timed_out:
        log.step("run", "timeout", f"{timeout:.0f}s")
        return ExecOutcome(
            ok=False,
            summary=f"PowerShell がタイムアウトしました ({timeout:.0f}秒)",
            detail=log.text() + "\n--- output ---\n" + tail)
    log.step("run", f"exit {result.returncode}")
    detail = log.text() + "\n--- output ---\n" + tail
    if result.returncode is None:
        return ExecOutcome(ok=False, summary="PowerShell を開始できませんでした",
                           detail=detail)
    if result.returncode != 0:
        return ExecOutcome(
            ok=False,
            summary=f"PowerShell が失敗しました (exit {result.returncode})",
            detail=detail)
    return ExecOutcome(ok=True, summary="PowerShell を実行しました (exit 0)",
                       detail=detail)


# -- PB-4033: script_library ------------------------------------------------
# PB-4031 で承認・実行したスクリプトを、名前と sha256 で保存しておき、
# 2回目以降は名前で呼び出すための保存庫。保存も実行も、PB-4031 と
# 同じ前提 (Muse がチャットで提示し、Nagato の承認を経た依頼だけが
# 届く) に立つ。実行時は、保存時の sha256 と依頼側が申告する
# sha256 (先頭16桁) の一致を確かめてから実行する。

LIB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
LIB_DEFAULT_DIR = r"R:\claude\front-desk\script-library"
LIB_INDEX = "index.json"


def _lib_dir(ctx: PlaybookContext) -> str:
    return ctx.settings.get("library_dir") or LIB_DEFAULT_DIR


def _lib_load_index(lib_dir: str) -> dict:
    try:
        with open(os.path.join(lib_dir, LIB_INDEX), "r",
                  encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _lib_save_index(lib_dir: str, index: dict) -> None:
    _write_text_atomic(
        os.path.join(lib_dir, LIB_INDEX),
        json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def script_library(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """PB-4031 の承認済みスクリプトの保存・一覧・名前での再実行。"""
    log = StepLog("pb4033")
    action = params.get("action")
    if action not in ("save", "list", "run"):
        return _blocked(log, f"action が不正です: {action}")
    lib_dir = _lib_dir(ctx)
    log.step("library", "ok", lib_dir)
    if action == "list":
        index = _lib_load_index(lib_dir)
        lines = []
        for name in sorted(index):
            e = index[name]
            lines.append(
                f"{name} sha256:{str(e.get('sha256', ''))[:16]} "
                f"chars:{e.get('chars')} saved:{e.get('saved_at')} "
                f"uses:{e.get('uses', 0)}")
        return ExecOutcome(
            ok=True,
            summary=f"保存庫には {len(index)} 件あります",
            detail=log.text() + "\n" + "\n".join(lines))
    name = params.get("name")
    if not isinstance(name, str) or not LIB_NAME_RE.match(name):
        return _blocked(log, f"名前が不正です: {name}")
    if action == "save":
        unknown = sorted(set(params) - {"action", "name", "script"})
        if unknown:
            return _blocked(log, f"未知の params です: {unknown}")
        script = params.get("script")
        if not isinstance(script, str) or not script.strip():
            return _blocked(log, "script が空です")
        if len(script) > PS_SCRIPT_MAX_CHARS:
            return _blocked(
                log, f"script が長すぎます ({len(script)} > {PS_SCRIPT_MAX_CHARS})")
        try:
            os.makedirs(lib_dir, exist_ok=True)
            _write_text_atomic(os.path.join(lib_dir, name + ".ps1"), script)
        except OSError as exc:
            log.step("save", "ng", str(exc)[:120])
            return _failed(log, "保存庫へ書けませんでした")
        index = _lib_load_index(lib_dir)
        digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
        existed = name in index
        index[name] = {
            "sha256": digest, "chars": len(script),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "uses": (index.get(name) or {}).get("uses", 0) if existed else 0,
            "last_used_at": (index.get(name) or {}).get("last_used_at")
            if existed else None,
        }
        _lib_save_index(lib_dir, index)
        log.step("save", "ok", f"sha256:{digest[:16]} chars:{len(script)}"
                               + (" (上書き)" if existed else ""))
        return ExecOutcome(
            ok=True,
            summary=f"スクリプトを保存しました: {name} (sha256:{digest[:16]})",
            detail=log.text())
    # action == "run"
    unknown = sorted(set(params) - {"action", "name", "confirm_sha256",
                                     "timeout_sec"})
    if unknown:
        return _blocked(log, f"未知の params です: {unknown}")
    confirm = params.get("confirm_sha256")
    if not isinstance(confirm, str) or len(confirm) < 16:
        return _blocked(log, "confirm_sha256 (先頭16桁) が必要です")
    timeout = params.get("timeout_sec", PS_TIMEOUT_DEFAULT_SEC)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return _blocked(log, "timeout_sec が数値ではありません")
    timeout = max(PS_TIMEOUT_MIN_SEC,
                  min(PS_TIMEOUT_MAX_SEC, float(timeout)))
    index = _lib_load_index(lib_dir)
    entry = index.get(name)
    if entry is None:
        return _blocked(log, f"保存庫にありません: {name}")
    try:
        with open(os.path.join(lib_dir, name + ".ps1"), "r",
                  encoding="utf-8") as fh:
            script = fh.read()
    except OSError:
        return _blocked(log, f"保存庫のファイルが読めません: {name}")
    digest = hashlib.sha256(script.encode("utf-8")).hexdigest()
    if digest != entry.get("sha256"):
        log.step("verify.hash", "ng", "file changed after save")
        return _blocked(log, "保存後にファイルが変わっています (sha256 不一致)")
    if not digest.startswith(confirm.lower()):
        log.step("verify.confirm", "ng")
        return _blocked(log, "申告された sha256 が保存時と一致しません")
    log.step("script", "received", f"sha256:{digest[:16]} chars:{len(script)}")
    outcome = _run_ps_script(ctx, log, script, timeout)
    if outcome.ok:
        entry["uses"] = int(entry.get("uses", 0)) + 1
        entry["last_used_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        index[name] = entry
        try:
            _lib_save_index(lib_dir, index)
        except OSError:
            pass
    return outcome


# -- PB-5012: log_fetch ----------------------------------------------

WATCHER_LOG_DEFAULT = r"R:\claude\claude-bridge\muse_monitor.log"
WATCHER_LOG_TAIL_BYTES = 1_000_000
WATCHER_LOG_MAX_CHARS = 12000
WATCHER_LOG_DEFAULT_LINES = 300
WATCHER_LOG_MAX_LINES = 2000


def log_fetch(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """見張り (muse_monitor) のログの末尾を、絞って返す。

    障害の切り分けで Muse が実ログを読むための取得系プレイブック。
    ログの場所は playbook_settings.pb5012.log_path、無ければ標準の
    設置場所。返す量は末尾 WATCHER_LOG_MAX_CHARS 字までに制限する。
    """
    log = StepLog("pb5012")
    unknown = sorted(set(params) - {"tail_lines", "contains"})
    if unknown:
        return _blocked(log, f"未知の params です: {unknown}")
    tail_lines = params.get("tail_lines", WATCHER_LOG_DEFAULT_LINES)
    if isinstance(tail_lines, bool) or not isinstance(tail_lines, int):
        return _blocked(log, "tail_lines が整数ではありません")
    tail_lines = max(1, min(WATCHER_LOG_MAX_LINES, tail_lines))
    contains = params.get("contains")
    if contains is not None and not isinstance(contains, str):
        return _blocked(log, "contains が文字列ではありません")
    path = ctx.settings.get("log_path") or WATCHER_LOG_DEFAULT
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > WATCHER_LOG_TAIL_BYTES:
                fh.seek(size - WATCHER_LOG_TAIL_BYTES)
            raw = fh.read()
    except OSError as exc:
        log.step("read", "ng", str(exc)[:120])
        return _blocked(log, f"見張りログが読めません: {path}")
    text = _decode_output(raw)
    lines = text.splitlines()
    lines = lines[-tail_lines:]
    if contains:
        needle = contains.lower()
        lines = [ln for ln in lines if needle in ln.lower()]
    body = "\n".join(lines)
    omitted = 0
    if len(body) > WATCHER_LOG_MAX_CHARS:
        body = body[-WATCHER_LOG_MAX_CHARS:]
        omitted = 1
    log.step("read", "ok", f"{path} ({size} bytes)")
    log.step("lines", "ok", f"{len(lines)} lines"
                            + (f" contains:{contains}" if contains else ""))
    detail = log.text() + "\n--- log ---\n"
    if omitted:
        detail += "…(先頭を省略)\n"
    detail += body
    return ExecOutcome(
        ok=True,
        summary=f"見張りログを {len(lines)} 行返しました",
        detail=detail)


# -- PB-5015: inbox_status -------------------------------------------------
# ミドリへの依頼の置き場 (front-desk inbox) と、読了の記録
# (wake-log)、見張りログの該当行を、決まった形で読み取って返す
# 取得系プレイブック。2026-11-12 夜、@ミドリ投稿の取りこぼし
# (collab Issue #6) の切り分けで PB-4031 の承認付きスクリプト
# として実行した確認を、承認不要の専用 PB にしたもの。
# パスもスクリプトも外からは渡せない。絞り込みは contains の
# 部分一致だけ。読み取り専用で、PC 側の状態は一切変えない。

INBOX_STATUS_DEFAULT_INBOX = r"R:\claude\front-desk\inbox"
INBOX_STATUS_DEFAULT_DESK_DIR = r"R:\claude\front-desk"
INBOX_STATUS_DEFAULT_TAIL = 15
INBOX_STATUS_MAX_TAIL = 100
INBOX_STATUS_MAX_ROWS = 100
INBOX_STATUS_MAX_CHARS = 12000
# 部分ごとの文字数の割り振り。全体を末尾から一括で切ると、最後にある
# 見張りログが先に消える (2026-11-12、アオのレビュー)。部分ごとに
# 絞れば、どこかが膨れても他の部分は残る。合計は全体の上限に収まる。
INBOX_STATUS_INBOX_CHARS = 4000
INBOX_STATUS_WAKE_CHARS = 3000
INBOX_STATUS_MONITOR_CHARS = 4000


def _inbox_listing(inbox_dir: str):
    """置き場の *.json を名前順に (ファイル名, id) の列で返す。

    id が無い・読めないファイルも落とさず、その旨を id の代わりに
    入れて返す (ID の無い置き場ファイル自体が切り分けの対象に
    なるため)。フォルダ自体が無ければ (None, 理由) を返す。
    """
    if not os.path.isdir(inbox_dir):
        return None, f"置き場フォルダがありません: {inbox_dir}"
    rows = []
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".json"):
            continue
        rid = "(idなし)"
        try:
            with open(os.path.join(inbox_dir, name), "r",
                      encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("id"):
                rid = str(data["id"])
        except (OSError, ValueError):
            rid = "(読めない)"
        rows.append((name, rid))
    return rows, ""


def _filtered_tail(path: str, tail: int, contains: str) -> list:
    """ファイルの末尾 tail 行 (contains があれば部分一致で絞る)。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    lines = _decode_output(raw).splitlines()
    if contains:
        needle = contains.lower()
        lines = [ln for ln in lines if needle in ln.lower()]
    return lines[-tail:]


def inbox_status(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """置き場・読了記録・見張りログの状態を決まった形で返す。

    宛先の不在や読めないファイルは失敗にせず、見つからなかった
    こと自体を報告に残す (状態確認が目的なので、欠けている事実
    こそ要る)。params 不正だけは blocked にする。
    """
    log = StepLog("pb5015")
    unknown = sorted(set(params) - {"tail_lines", "contains"})
    if unknown:
        return _blocked(log, f"未知の params です: {unknown}")
    tail = params.get("tail_lines", INBOX_STATUS_DEFAULT_TAIL)
    if isinstance(tail, bool) or not isinstance(tail, int):
        return _blocked(log, "tail_lines が整数ではありません")
    tail = max(1, min(INBOX_STATUS_MAX_TAIL, tail))
    contains = params.get("contains")
    if contains is not None and not isinstance(contains, str):
        return _blocked(log, "contains が文字列ではありません")
    contains = contains or ""

    inbox_dir = ctx.settings.get("inbox_dir") or INBOX_STATUS_DEFAULT_INBOX
    desk_dir = ctx.settings.get("desk_dir") or INBOX_STATUS_DEFAULT_DESK_DIR
    monitor_path = (ctx.settings.get("monitor_log_path")
                    or WATCHER_LOG_DEFAULT)

    parts = []
    rows, err = _inbox_listing(inbox_dir)
    if rows is None:
        log.step("inbox", "ng", err)
        parts.append(f"--- inbox ---\n{err}")
        inbox_count = 0
    else:
        inbox_count = len(rows)
        log.step("inbox", "ok", f"{inbox_dir} ({inbox_count} 件)")
        shown = rows[-INBOX_STATUS_MAX_ROWS:]
        # 文字数の割り振りでも絞る。落とすのは古い方で、新しい方を残す。
        while len(shown) > 1 and sum(
                len(name) + len(rid) + 5 for name, rid in shown
        ) > INBOX_STATUS_INBOX_CHARS:
            shown.pop(0)
        body_lines = []
        if len(shown) < len(rows):
            body_lines.append(f"…(先頭 {len(rows) - len(shown)} 件を省略)")
        body_lines += [f"{name} id={rid}" for name, rid in shown]
        parts.append("--- inbox ---\n"
                     + ("\n".join(body_lines) or "(ファイルなし)"))

    wake_names = []
    if os.path.isdir(desk_dir):
        wake_names = sorted(n for n in os.listdir(desk_dir)
                            if n.startswith("wake-log"))
    if not wake_names:
        log.step("wake-log", "ng", "wake-log* に一致するファイルなし")
        parts.append("--- wake-log ---\n"
                     "wake-log* に一致するファイルはありません")
    else:
        log.step("wake-log", "ok", ", ".join(wake_names))
        seg = ["--- wake-log ---"]
        for name in wake_names:
            seg.append(f"[{name}]")
            try:
                seg.extend(_filtered_tail(os.path.join(desk_dir, name),
                                          tail, contains))
            except OSError as exc:
                seg.append(f"(読めません: {str(exc)[:80]})")
        wake_body = "\n".join(seg[1:])
        if len(wake_body) > INBOX_STATUS_WAKE_CHARS:
            wake_body = ("…(この部分の先頭を省略)\n"
                         + wake_body[-INBOX_STATUS_WAKE_CHARS:])
        parts.append(seg[0] + "\n" + wake_body)

    try:
        size = os.path.getsize(monitor_path)
        with open(monitor_path, "rb") as fh:
            if size > WATCHER_LOG_TAIL_BYTES:
                fh.seek(size - WATCHER_LOG_TAIL_BYTES)
            raw = fh.read()
        mlines = _decode_output(raw).splitlines()
        if contains:
            needle = contains.lower()
            mlines = [ln for ln in mlines if needle in ln.lower()]
        mlines = mlines[-tail:]
        log.step("monitor", "ok", f"{monitor_path} ({len(mlines)} 行)")
        mbody = "\n".join(mlines)
        if len(mbody) > INBOX_STATUS_MONITOR_CHARS:
            mbody = ("…(この部分の先頭を省略)\n"
                     + mbody[-INBOX_STATUS_MONITOR_CHARS:])
        parts.append("--- monitor log ---\n"
                     + (mbody or "(該当行なし)"))
    except OSError as exc:
        log.step("monitor", "ng", str(exc)[:120])
        parts.append(f"--- monitor log ---\n読めません: {monitor_path}")

    detail = log.text() + "\n" + "\n".join(parts)
    if len(detail) > INBOX_STATUS_MAX_CHARS:
        detail = detail[:INBOX_STATUS_MAX_CHARS] + "\n…(末尾を省略)"
    return ExecOutcome(
        ok=True,
        summary=(f"置き場 {inbox_count} 件・wake-log "
                 f"{len(wake_names)} ファイルの状態を返しました"),
        detail=detail)


# -- PB-4034: session_wake -------------------------------------------------
# 待ち受けのない Claude セッションを、陽太さんの依頼で起こす。
# 設計は 2026-11-12 に Nagato が決定: 宛先は Opus セッションと
# 新規セッションの2つだけ、上限は合計で1時間3回、全件記録、本文の
# 頭には必ず『[Muse経由・陽太さんの依頼]』を付ける。報告は窓口の
# 通常の報告と PC のローカル記録ファイル、失敗通知は Pushover。
#
# 届け方 (2026-11-12、Opus の回答で確定):
# - 起きている Claude へは、セッション同士の SendMessage が公式の
#   方法。窓口 (Haiku) が起きている間は、窓口が取り次ぐのが一番
#   安全なので、このプレイブックは画面経由を使わず、取り次ぎに
#   任せる (deferred_to_relay で記録して返す)。
# - 誰も起きていないときだけ、画面経由 (deep link で開き、入力欄
#   に書いて送信) を使う。ただし入力と送信の実動作は Nagato の
#   OK が前提で、実装も実地テスト待ち (not_implemented で返す)。
# - 新規セッションは deep link (claude://code/new) で開く。
# - CLI とファイル経由は使わない。
#
# 上限と記録は「届けた側が同じ記録に同じ形式で書く」が共通の
# 決め。窓口が SendMessage で届けた分も wake-log に sent として
# 残る前提で、このプレイブックも同じ記録を読んで上限を数える。
# 記録は1試行1行の JSONL (設定 log_path、既定は窓口のフォルダ)。
# 二重送信は「送信済みの delivery_id の再送」だけを指し、失敗した
# 試行の再送は妨げない。
#
# セッションの状態は claude_status.py と同じ置き場所・同じ TTL で
# ファイルから読む (import はせず、置き場所の約束だけ合わせる)。
# 状態の意味: running/ask は作業中、done は入力待ちで空き、
# fail は直近の失敗。一覧に無い (TTL 切れを含む) ものは absent。

WAKE_MARKER = "[Muse経由・陽太さんの依頼]"
WAKE_DEFAULT_LOG = r"R:\claude\front-desk\wake-log.jsonl"
WAKE_DEFAULT_OPUS_SESSION = "local_87a918e5-457a-4531-87e0-165e674e5d17"
WAKE_DEFAULT_HAIKU_SESSION = "local_5463a201-c272-463c-931e-680b9c76f23e"
WAKE_NEW_URL = "claude://code/new"
WAKE_RATE_LIMIT = 3
WAKE_RATE_WINDOW_SEC = 3600
WAKE_BODY_MAX_CHARS = 20000
_WAKE_TARGETS = ("opus", "new")


def _wake_log_path(ctx: PlaybookContext) -> str:
    return ctx.settings.get("log_path") or WAKE_DEFAULT_LOG


def _wake_read_records(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    records = []
    for line in lines[-500:]:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def _wake_append(path: str, record: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def _wake_status_dir(ctx: PlaybookContext) -> str:
    configured = ctx.settings.get("status_dir")
    if configured:
        return configured
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or "."
    return os.path.join(temp, "tray-tools-hooks", "status")


def _wake_session_state(ctx: PlaybookContext, session_id: str,
                        now: float) -> str:
    """セッションの状態を busy / idle / failed / absent で返す。

    状態ファイルはセッションごとに1つ。デスクトップアプリの
    セッション ID (local_…) は entry の host_session_id に入って
    いるので、全ファイルを走査して一致するものを探す。
    TTL は claude_status.py に合わせる (done/fail は30分、
    running/ask は6時間で一覧から外れたものとして扱う)。
    """
    directory = _wake_status_dir(ctx)
    try:
        names = os.listdir(directory)
    except OSError:
        return "absent"
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), "r",
                      encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if (entry.get("host_session_id") != session_id
                and entry.get("session_id") != session_id):
            continue
        state = entry.get("state")
        updated = entry.get("ended") or entry.get("updated") or 0
        if state in ("done", "fail"):
            if now - updated >= 30 * 60:
                return "absent"
            return "idle" if state == "done" else "failed"
        if state in ("running", "ask"):
            if now - updated >= 6 * 3600:
                return "absent"
            return "busy"
        return "absent"
    return "absent"


def _open_link(url: str) -> bool:
    """deep link を OS に開かせる。Windows の os.startfile 前提。

    テストでは差し替える。開けない環境 (VM 等) では False。
    """
    try:
        os.startfile(url)
        return True
    except (AttributeError, OSError):
        return False


def _notify_pushover(title: str, message: str) -> bool:
    """Pushover で陽太さんへ知らせる。送れなければ False (落とさない)。

    pushover.py はリポジトリ直下の部品で、資格情報は Windows の
    資格情報マネージャにある。import できない環境 (VM のテスト等)
    では黙って諦める。PB-4034 の成否は通知に依存させない。
    """
    try:
        import pushover
        pushover.send(message, title=title)
        return True
    except Exception:
        return False


def _wake_finish(ctx: PlaybookContext, log: StepLog, record: dict,
                 outcome: ExecOutcome, notify: bool = False) -> ExecOutcome:
    if _wake_append(_wake_log_path(ctx), record):
        log.step("record", "ok", _wake_log_path(ctx))
    else:
        log.step("record", "ng", "記録ファイルへ書けませんでした")
    if notify:
        sent = _notify_pushover(
            "ARiA: セッションを起こせませんでした",
            f"{record.get('target')} 宛の起こしが {record['outcome']} で"
            f"止まりました。delivery_id={record.get('delivery_id')}")
        log.step("notify", "ok" if sent else "skipped")
    detail = log.text() + ("\n" + outcome.detail if outcome.detail else "")
    return ExecOutcome(ok=outcome.ok, summary=outcome.summary,
                       detail=detail, timed_out=outcome.timed_out,
                       state=outcome.state)


def session_wake(ctx: PlaybookContext, params: dict) -> ExecOutcome:
    """待ち受けのない Claude セッションを起こす (PB-4034)。"""
    log = StepLog("pb4034")
    now = time.time()
    record = {
        "ts": now,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "delivery_id": params.get("delivery_id"),
        "target": params.get("target"),
        "body": params.get("body"),
        "outcome": "invalid",
    }

    def finish(outcome: ExecOutcome, result: str,
               notify: bool = False) -> ExecOutcome:
        record["outcome"] = result
        return _wake_finish(ctx, log, record, outcome, notify=notify)

    unknown = sorted(set(params) - {"target", "body", "delivery_id",
                                     "folder"})
    if unknown:
        return finish(_blocked(log, f"未知の params です: {unknown}"),
                      "invalid")
    target = params.get("target")
    if target not in _WAKE_TARGETS:
        return finish(_blocked(log, f"target が不正です: {target}"),
                      "invalid")
    body = params.get("body")
    if not isinstance(body, str) or not body.strip():
        return finish(_blocked(log, "body が空です"), "invalid")
    if len(body) > WAKE_BODY_MAX_CHARS:
        return finish(
            _blocked(log, f"body が長すぎます ({len(body)} > "
                          f"{WAKE_BODY_MAX_CHARS})"), "invalid")
    delivery_id = params.get("delivery_id")
    if not isinstance(delivery_id, str) or not delivery_id.strip():
        return finish(_blocked(log, "delivery_id が必要です"), "invalid")
    folder = params.get("folder")
    if folder is not None and not isinstance(folder, str):
        return finish(_blocked(log, "folder が文字列ではありません"),
                      "invalid")
    log.step("params", "ok", f"target:{target} delivery:{delivery_id}")

    records = _wake_read_records(_wake_log_path(ctx))
    if any(r.get("delivery_id") == delivery_id
           and r.get("outcome") == "sent" for r in records):
        log.step("dedupe", "ng", "送信済みの delivery_id です")
        return finish(_blocked(log, "この delivery_id は送信済みです "
                                    "(二重送信の防止)"), "duplicate")
    log.step("dedupe", "ok")
    sent_recent = sum(
        1 for r in records
        if r.get("outcome") == "sent"
        and now - (r.get("ts") or 0) < WAKE_RATE_WINDOW_SEC)
    if sent_recent >= WAKE_RATE_LIMIT:
        log.step("rate", "ng", f"直近1時間で {sent_recent} 回送信済み")
        return finish(
            _blocked(log, f"上限 (1時間に {WAKE_RATE_LIMIT} 回) に"
                          "達しています"), "rate_limited", notify=True)
    log.step("rate", "ok", f"直近1時間の送信 {sent_recent} 回")

    text = body if body.startswith(WAKE_MARKER) \
        else WAKE_MARKER + "\n" + body

    if target == "new":
        folder = folder or ctx.settings.get("default_folder") or ""
        url = WAKE_NEW_URL + "?q=" + urllib.parse.quote(text)
        if folder:
            url += "&folder=" + urllib.parse.quote(folder)
        log.step("open", "start", WAKE_NEW_URL)
        if _open_link(url):
            log.step("open", "ok")
            return finish(
                ExecOutcome(
                    ok=True,
                    summary="新規セッションを開きました。セッションの"
                            "報告は記録ファイルに delivery_id と"
                            "対応付けて書かれます"),
                "sent")
        log.step("open", "ng")
        return finish(
            _failed(log, "新規セッションの deep link を開けませんでした"),
            "failed", notify=True)

    # target == "opus": まず両セッションの状態を見る。
    opus_id = ctx.settings.get("opus_session_id") \
        or WAKE_DEFAULT_OPUS_SESSION
    haiku_id = ctx.settings.get("haiku_session_id") \
        or WAKE_DEFAULT_HAIKU_SESSION
    opus_state = _wake_session_state(ctx, opus_id, now)
    log.step("session.opus", opus_state, opus_id)
    if opus_state == "busy":
        return finish(
            _blocked(log, "宛先のセッションが作業中です (running/ask)"),
            "busy", notify=True)
    haiku_state = _wake_session_state(ctx, haiku_id, now)
    log.step("session.haiku", haiku_state, haiku_id)
    if haiku_state != "absent":
        # 窓口が起きている間は SendMessage の取り次ぎが一番安全。
        # 画面経由は使わず、取り次ぎに任せる。
        return finish(
            _blocked(log, "窓口が起きているため、画面経由は使わず、"
                          "窓口の SendMessage 取り次ぎに任せます"),
            "deferred_to_relay")
    # 誰も起きていないので画面経由の対象だが、入力と送信は
    # 実地テスト (Nagato の OK) 待ちで未実装。
    return finish(
        _failed(log, "誰も起きていないため画面経由の対象ですが、"
                     "入力と送信の実装は実地テスト待ちです "
                     f"(セッションの状態: {opus_state})"),
        "not_implemented")


# -- 登録簿と実行の振り分け ----------------------------------------------

PLAYBOOKS = {
    "pb1001": traytools_sync,
    "pb1002": daemon_sync,
    "pb1003": desk_sync,
    "pb2004": deploy_approve,
    "pb3005": config_set,
    "pb4006": traytools_restart,
    "pb4031": powershell_run,
    "pb4033": script_library,
    "pb4034": session_wake,
    "pb1007": watcher_sync,
    "pb5008": pc_capture,
    "pb5012": log_fetch,
    "pb5015": inbox_status,
}

# 移行期間の別名 (旧名 → 正規 ID)。PC 側の許可リストと設定が 4桁 ID に
# 揃ったら外す。許可判定は、正規 ID と別名のどちらが設定にあっても通す。
PLAYBOOK_ALIASES = {
    "traytools_sync": "pb1001",
    "daemon_sync": "pb1002",
    "desk_sync": "pb1003",
    "deploy_approve": "pb2004",
    "config_set": "pb3005",
    "traytools_restart": "pb4006",
    "watcher_sync": "pb1007",
    "pc_capture": "pb5008",
    "script_library": "pb4033",
    "session_wake": "pb4034",
    "log_fetch": "pb5012",
    "watcher_log_fetch": "pb5012",
    "inbox_status": "pb5015",
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
        canonical = PLAYBOOK_ALIASES.get(playbook_id, playbook_id)
        fn = PLAYBOOKS.get(canonical)
        if fn is None:
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary=f"未知のプレイブックです: {playbook_id}")
        names = {canonical} | {
            alias for alias, target in PLAYBOOK_ALIASES.items()
            if target == canonical}
        if not names & set(self._config.allowed_playbooks):
            return ExecOutcome(
                ok=False, state=model.RESULT_REJECTED,
                summary=f"プレイブック {canonical} はまだ有効化されていません")
        settings = self._config.playbook_settings.get(canonical)
        if settings is None:
            settings = self._config.playbook_settings.get(playbook_id, {})
        ctx = PlaybookContext(
            runner=self._runner, settings=dict(settings))
        t0 = time.time()
        activity.begin(canonical, request.id,
                       path=self._activity_path, now=t0)
        try:
            outcome = fn(ctx, params)
        except Exception:
            outcome = ExecOutcome(
                ok=False,
                summary=f"プレイブック {canonical} の実行中に例外が発生しました",
                detail=traceback.format_exc()[-2000:])
        activity.finish(canonical, request.id, outcome,
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
