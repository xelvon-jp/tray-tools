"""git・テスト実行・再起動・生存確認の実アダプタ（subprocess）。

pipeline は小さなインターフェース越しにこれらを使うので、テストでは
フェイクに差し替えられる。再起動は既存の安全な口
（traytools_send.py restart --check → restart）だけを使い、
常駐のプロセスを自前で落とすことはしない。
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _tail(text: str, limit: int = 2000) -> str:
    text = text or ""
    return text if len(text) <= limit else "…" + text[-limit:]


class SubprocessGit:
    """git CLI の薄いラッパ。失敗は GitError（stderr の末尾つき）。"""

    def _run(self, repo_dir: Path | None, *args: str) -> str:
        cmd = ["git"]
        if repo_dir is not None:
            cmd += ["-C", str(repo_dir)]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise GitError(
                f"git {' '.join(args[:2])} が失敗しました: {_tail(proc.stderr.strip())}"
            )
        return proc.stdout.strip()

    def ensure_clone(self, remote: str, dest: Path, branch: str) -> None:
        if (dest / ".git").is_dir():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--branch", branch, remote, str(dest)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            # ブランチ名で clone できない場合（既定ブランチと違う等）は
            # 丸ごと clone してから対象ブランチを追う
            proc = subprocess.run(
                ["git", "clone", remote, str(dest)], capture_output=True, text=True
            )
            if proc.returncode != 0:
                raise GitError(f"git clone が失敗しました: {_tail(proc.stderr.strip())}")

    def fetch(self, repo_dir: Path, branch: str | None = None) -> None:
        args = ["fetch", "origin"]
        if branch:
            args.append(branch)
        self._run(repo_dir, *args)

    def head(self, repo_dir: Path) -> str:
        return self._run(repo_dir, "rev-parse", "HEAD")

    def has_object(self, repo_dir: Path, ref: str) -> bool:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "cat-file", "-e", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0

    def checkout(self, repo_dir: Path, ref: str) -> None:
        self._run(repo_dir, "checkout", "--detach", ref)

    def diff_names(self, repo_dir: Path, base: str, ref: str) -> list:
        out = self._run(repo_dir, "diff", "--name-only", f"{base}..{ref}")
        return [line for line in out.splitlines() if line.strip()]

    def is_dirty(self, repo_dir: Path) -> bool:
        return bool(self._run(repo_dir, "status", "--porcelain"))

    def merge_ff_only(self, repo_dir: Path, ref: str) -> None:
        self._run(repo_dir, "merge", "--ff-only", ref)

    def reset_hard(self, repo_dir: Path, ref: str) -> None:
        self._run(repo_dir, "reset", "--hard", ref)


class CommandTestRunner:
    """設定されたテストコマンド列を検証用コピーの中で実行する。"""

    def __init__(self, commands: tuple, timeout_sec: float = 1800.0):
        self.commands = commands
        self.timeout_sec = timeout_sec

    def run(self, cwd: Path):
        """-> (ok, output_tail)。コマンド未設定なら安全側に倒して失敗を返す。"""
        if not self.commands:
            return False, "テストコマンドが設定されていません（安全のため反映しません）"
        outputs = []
        for cmd in self.commands:
            try:
                proc = subprocess.run(
                    list(cmd),
                    cwd=str(cwd),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_sec,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                return False, f"テストコマンドを実行できませんでした {cmd}: {e}"
            outputs.append(proc.stdout + proc.stderr)
            if proc.returncode != 0:
                return False, _tail("\n".join(outputs))
        return True, _tail("\n".join(outputs))


class SendScriptRestarter:
    """traytools_send.py 経由の再起動（--check で吹き添え確認をしてから）。

    filer_update.restart_resident と同じ読み方をする：
    --check の応答に「差し支えありません」が含まれれば進めてよい、
    「見送るべきです」が含まれればブロッカーあり。
    言い回しが読めない場合は安全側（再起動しない）に倒す。
    """

    def __init__(self, python_executable: str, send_script: Path, timeout_sec: float = 60.0):
        self.python_executable = python_executable
        self.send_script = Path(send_script)
        self.timeout_sec = timeout_sec

    def _send(self, *args: str):
        # errors="replace"： 相手の出力が想定外のバイト列でも
        # UnicodeDecodeError で落ちない（dd-20261109-04 の教訓）。
        # timeout： 応答が無い相手に無限に付き合わない。
        proc = subprocess.run(
            [self.python_executable, str(self.send_script), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=self.timeout_sec,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def check(self):
        """-> (kind, message)。kind は "ok" | "blocked" | "error"。"""
        try:
            _code, out = self._send("restart", "--check")
        except subprocess.TimeoutExpired:
            return "error", "traytools_send.py の応答がタイムアウトしました"
        except OSError as e:
            return "error", f"traytools_send.py を実行できません: {e}"
        text = out.strip()
        if "差し支えありません" in text:
            return "ok", text
        if "見送るべき" in text:
            return "blocked", text
        if not text:
            return "error", "常駐から返事がありませんでした"
        return "error", text

    def restart(self):
        """-> (ok, message)"""
        try:
            code, out = self._send("restart")
        except subprocess.TimeoutExpired:
            return False, "traytools_send.py の応答がタイムアウトしました"
        except OSError as e:
            return False, f"traytools_send.py を実行できません: {e}"
        return code == 0, out.strip()


class SendScriptLiveness:
    """生存確認： traytools_send.py status が応答すれば生きているとみなす。

    注意： traytools_send.py は待ち受けが無いと常駐を自動で起動する作りのため、
    「落ちていたのを起こした」ケースと区別がつかない。実機での切り分けは残件。
    """

    def __init__(self, python_executable: str, send_script: Path, timeout_sec: float = 30.0):
        self.python_executable = python_executable
        self.send_script = Path(send_script)
        self.timeout_sec = timeout_sec

    def check(self) -> bool:
        try:
            proc = subprocess.run(
                [self.python_executable, str(self.send_script), "status"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=self.timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0
