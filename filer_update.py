# filer_update.py
# あっふのタイトルの版表記と、Shift+F5「更新を適用して再起動」の、Qt に依存しない部分。
#
# 版表記
#   git コマンドは使わず .git を自前で読む(diagnostics.read_git_version と同じもの。
#   業務PCには git が入っていないことがある)。.git が無い(GitHub の ZIP を展開した)
#   ときは、ソースの一番新しい .py の更新日時を「ZIP 10/29」のように出す。
#
# 更新
#   git fetch → 取り込めるコミットを見せる → 本人が OK したら git pull --ff-only。
#   早送りできない(手元に変更・分岐がある)ときは何もしない。force も stash もしない。
#   git の実行は呼ぶ側が別スレッドで行う(fetch はネットワーク越し)。ここの関数は
#   run(引数, timeout) を差し替えられる(テストで本物の git を叩かない)。
import os
import subprocess
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
FETCH_TIMEOUT = 60
PULL_TIMEOUT = 120
LOCAL_TIMEOUT = 15
LOG_LIMIT = 20
CREATE_NO_WINDOW = 0x08000000


def has_git(repo_dir=REPO_DIR) -> bool:
    return (Path(repo_dir) / ".git").exists()


def version_label(repo_dir=REPO_DIR, read_git=None, newest=None) -> str:
    """タイトルに出す版。"2f74ed1 (10/29)" / "ZIP 10/29" / ""(どちらも取れない)。"""
    import diagnostics

    read_git = read_git or diagnostics.read_git_version
    newest = newest or diagnostics.newest_source
    repo_dir = Path(repo_dir)
    try:
        info = read_git(repo_dir)
    except Exception:  # noqa: BLE001  版が出ないだけ
        info = {"state": "broken"}
    state = info.get("state")
    if state == "git" and info.get("sha"):
        date = info.get("date")
        if date is None:
            date = _head_file_time(repo_dir)
        short = info["sha"][:7]
        return f"{short} ({date:%m/%d})" if date else short
    if state == "nogit":
        try:
            found = newest(repo_dir)
        except Exception:  # noqa: BLE001
            found = None
        if found:
            return f"ZIP {found[1]:%m/%d}"
    return ""


def _head_file_time(repo_dir: Path):
    """コミットが読めない(pack の差分など)ときの代わり: ブランチの ref か HEAD の更新時刻。"""
    from datetime import datetime

    git_dir = repo_dir / ".git"
    candidates = []
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        if head.startswith("ref:"):
            candidates.append(git_dir / head[4:].strip())
    except OSError:
        pass
    candidates += [git_dir / "packed-refs", git_dir / "HEAD"]
    for path in candidates:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
    return None


def window_title(path: str, version: str) -> str:
    name = f"あっふ {version}" if version else "あっふ"
    return f"{path} - {name}" if path else name


# ---------------------------------------------------------------
# git の実行
# ---------------------------------------------------------------
class GitError(Exception):
    pass


def run_git(args, cwd=REPO_DIR, timeout=LOCAL_TIMEOUT):
    """git を実行して (終了コード, 標準出力, 標準エラー)。git が無い・時間切れは GitError。

    認証のプロンプトで止まらないよう GIT_TERMINAL_PROMPT=0、窓を出さないよう
    CREATE_NO_WINDOW(あっふは pythonw で動くので、そのままだとコンソールが出る)。"""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GCM_INTERACTIVE", "never")
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(cwd), capture_output=True, timeout=timeout,
            env=env, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError as e:
        raise GitError("git が見つかりません（PATH に git がありません）") from e
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {args[0]} が {timeout} 秒で終わりませんでした"
                       "（ネットワークを確かめてください）") from e
    except OSError as e:
        raise GitError(f"git を起動できません: {e}") from e
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _decode(data) -> str:
    if not data:
        return ""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def explain_git_failure(step: str, stderr: str) -> str:
    """git の失敗を、メッセージ窓に出せる一言にする。"""
    low = (stderr or "").lower()
    if "could not resolve host" in low or "unable to access" in low or "connection" in low \
            or "timed out" in low:
        reason = "ネットワークに繋がりません"
    elif "authentication" in low or "permission denied" in low or "terminal prompts disabled" in low \
            or "could not read username" in low:
        reason = "認証に失敗しました"
    elif "not a git repository" in low:
        reason = "git の管理下ではありません"
    else:
        reason = _first_line(stderr) or "理由不明"
    return f"git {step} に失敗: {reason}"


# ---------------------------------------------------------------
# 取り込めるかの判定
# ---------------------------------------------------------------
def decide(ahead: int, behind: int, dirty: list) -> str:
    """"uptodate"(取り込むもの無し) / "behind"(早送りできる) / "diverged"(分岐) /
    "dirty"(手元に変更がある)。"""
    if behind <= 0:
        return "uptodate"
    if ahead > 0:
        return "diverged"
    if dirty:
        return "dirty"
    return "behind"


def resident_files(files) -> list:
    """変わったファイルのうち、常駐(main.py 側)に効くもの。

    あっふ自身(filer*.py・filer_menus/)、テスト・道具・文書は除く。それ以外の .py は
    常駐が読むものとして扱う(settings.py などあっふと共用のものも含む。常駐の再起動を
    勧めて困ることは無い)。"""
    result = []
    for name in files or []:
        path = name.replace("\\", "/")
        base = path.rsplit("/", 1)[-1]
        if not path.endswith(".py"):
            continue
        if base.startswith("filer") or path.startswith(("filer_menus/", "tests/", "tools/",
                                                        "docs/", ".github/")):
            continue
        result.append(path)
    return result


def check_updates(repo_dir=REPO_DIR, run=None) -> dict:
    """fetch して、取り込めるかを調べる。

    {"state": "nogit"|"error"|"uptodate"|"behind"|"diverged"|"dirty",
     "message", "commits": ["abc1234 1行目", …], "behind", "ahead", "dirty": [...],
     "files": [変わるファイル]}"""
    if not has_git(repo_dir):
        return {"state": "nogit"}
    run = run or (lambda args, timeout: run_git(args, repo_dir, timeout))
    try:
        code, _out, err = run(["fetch", "--quiet"], FETCH_TIMEOUT)
        if code != 0:
            return {"state": "error", "message": explain_git_failure("fetch", err)}
        code, out, err = run(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
                             LOCAL_TIMEOUT)
        if code != 0:
            return {"state": "error",
                    "message": "追跡するブランチがありません（git pull する先が決まっていません）"}
        upstream = out.strip()
        code, out, err = run(["rev-list", "--left-right", "--count", "HEAD...@{u}"],
                             LOCAL_TIMEOUT)
        if code != 0:
            return {"state": "error", "message": explain_git_failure("rev-list", err)}
        parts = out.split()
        ahead, behind = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)
        code, out, err = run(["status", "--porcelain", "--untracked-files=no"], LOCAL_TIMEOUT)
        dirty = [line[3:] for line in out.splitlines() if line.strip()] if code == 0 else []
        commits, files = [], []
        if behind > 0:
            code, out, _err = run(["log", f"--max-count={LOG_LIMIT}", "--format=%h %s",
                                   "HEAD..@{u}"], LOCAL_TIMEOUT)
            commits = [line for line in out.splitlines() if line.strip()] if code == 0 else []
            code, out, _err = run(["diff", "--name-only", "HEAD", "@{u}"], LOCAL_TIMEOUT)
            files = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
    except GitError as e:
        return {"state": "error", "message": str(e)}
    except (ValueError, IndexError) as e:
        return {"state": "error", "message": f"git の出力を読めません: {e}"}
    state = decide(ahead, behind, dirty)
    return {"state": state, "upstream": upstream, "ahead": ahead, "behind": behind,
            "dirty": dirty, "commits": commits, "files": files}


def describe_block(info: dict) -> str:
    """取り込めないときの理由(メッセージ窓へ)。"""
    if info.get("state") == "diverged":
        return (f"更新できません: 手元に {info.get('ahead')}件の未送信のコミットがあり、"
                f"{info.get('upstream') or '追跡ブランチ'} と分岐しています（何もしていません）")
    if info.get("state") == "dirty":
        names = ", ".join((info.get("dirty") or [])[:5])
        return f"更新できません: 手元に変更があります（{names}）。何もしていません"
    return info.get("message") or "更新できません"


def pull(repo_dir=REPO_DIR, run=None) -> dict:
    """git pull --ff-only。{"ok": bool, "message"}。"""
    run = run or (lambda args, timeout: run_git(args, repo_dir, timeout))
    try:
        code, out, err = run(["pull", "--ff-only"], PULL_TIMEOUT)
    except GitError as e:
        return {"ok": False, "message": str(e)}
    if code != 0:
        return {"ok": False, "message": explain_git_failure("pull --ff-only", err)}
    return {"ok": True, "message": _first_line(out) or "更新しました"}
