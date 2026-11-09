"""front_desk.executor — 依頼の実行 (tier ごとのコマンド起動)。

本文は標準入力で渡し、出力を結果にする。作法は front-desk の
README を参照。具体的なコマンド (Claude Code の起動作法) は
PC 側での実地確認 (A7) の後に設定で確定する。
"""
from __future__ import annotations

import dataclasses
import subprocess
import typing

from .model import Request

SUMMARY_MAX_CHARS = 300
DETAIL_MAX_CHARS = 4000


@dataclasses.dataclass(frozen=True)
class ExecOutcome:
    ok: bool
    summary: str = ""
    detail: str = ""
    timed_out: bool = False


def summarize_output(text: str) -> tuple:
    """出力から (要約, 詳細) を作る。

    要約は最後の空でない行 (最大 SUMMARY_MAX_CHARS 文字)、
    詳細は末尾 DETAIL_MAX_CHARS 文字。実行側には「最後の行に
    結論を1行で書く」よう依頼文で指示する前提。
    """
    cleaned = (text or "").replace("\r\n", "\n")
    lines = [ln.strip() for ln in cleaned.split("\n")]
    nonempty = [ln for ln in lines if ln]
    summary = nonempty[-1][:SUMMARY_MAX_CHARS] if nonempty else ""
    detail = cleaned.strip()[-DETAIL_MAX_CHARS:]
    return summary, detail


class Executor(typing.Protocol):
    def run(self, request: Request) -> ExecOutcome: ...


class SubprocessExecutor:
    """tier ごとの argv でサブプロセスを起動する実装。"""

    def __init__(self, commands: dict, timeout_sec: int, workdir: str = ""):
        self._commands = dict(commands or {})
        self._timeout = max(1, int(timeout_sec))
        self._workdir = workdir or None

    def run(self, request: Request) -> ExecOutcome:
        argv = self._commands.get(request.tier)
        if not argv:
            return ExecOutcome(
                ok=False,
                summary=f"tier={request.tier} の実行コマンドが未設定です",
            )
        try:
            proc = subprocess.run(
                list(argv),
                input=request.body,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self._timeout,
                cwd=self._workdir,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output if isinstance(exc.output, str) else ""
            summary, detail = summarize_output(out)
            return ExecOutcome(
                ok=False, timed_out=True,
                summary="時間切れで打ち切りました"
                        + (f"（最後の出力: {summary}）" if summary else ""),
                detail=detail,
            )
        except OSError as exc:
            return ExecOutcome(
                ok=False, summary=f"実行を開始できませんでした: {exc}")
        combined = (proc.stdout or "") + (proc.stderr or "")
        summary, detail = summarize_output(combined)
        if proc.returncode != 0:
            if not summary:
                summary = f"終了コード {proc.returncode} で失敗しました"
            return ExecOutcome(ok=False, summary=summary, detail=detail)
        return ExecOutcome(ok=True, summary=summary, detail=detail)


class FakeExecutor:
    """テストとデモ用。決まった結果を返し、呼ばれた依頼を記録する。"""

    def __init__(self, outcome: ExecOutcome = None, by_id: dict = None):
        self.calls: list = []
        self._outcome = outcome or ExecOutcome(
            ok=True, summary="確認しました（フェイク）", detail="fake")
        self._by_id = dict(by_id or {})

    def run(self, request: Request) -> ExecOutcome:
        self.calls.append(request)
        return self._by_id.get(request.id, self._outcome)
