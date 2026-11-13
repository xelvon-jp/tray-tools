# run_all.py
# tests/test_*.py を1ファイルずつ別プロセスで全部回し、結果を一覧にする。
#
# 背景: CI が回すのは長く4ファイルだけ(test_filer / test_slideshow /
# test_photometa / test_folder_watch)で、残りのテストは push しても
# どこでも実行されなかった(2026-11-13 の討論、テーマ6)。
#
# 使い方:
#   python tests/run_all.py                 全部(1ファイルずつ、遅い)
#   python tests/run_all.py --match picker  名前に部分一致するものだけ
#   python tests/run_all.py --exclude test_filer.py,test_slideshow.py
#
# skip の宣言: この環境では回せないテストファイルは、冒頭で
#   print("SKIP: 理由") して exit 0 する。run_all は SKIP として数える。
# 実機でしか確かめられないものは、環境変数 TRAYTOOLS_REAL=1 が無いときに
# そう宣言すること(任意依存が無い場合も同じ)。
#
# 終了コード: FAIL / TIMEOUT があれば 1。SKIP は失敗に数えない。
import argparse
import subprocess
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
DEFAULT_TIMEOUT = 600  # 秒。test_filer のような長いものに合わせる


def discover(match: str, exclude: set) -> list:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    chosen = []
    for path in files:
        if path.name in exclude:
            continue
        if match and match not in path.name:
            continue
        chosen.append(path)
    return chosen


def run_one(path: Path, timeout: int) -> tuple:
    """(結果, 秒数, 出力の末尾)。結果は PASS / FAIL / SKIP / TIMEOUT。"""
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-u", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.time() - started, ""
    elapsed = time.time() - started
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and any(
            line.startswith("SKIP:") for line in out.splitlines()):
        return "SKIP", elapsed, out
    if proc.returncode == 0:
        return "PASS", elapsed, out
    return "FAIL", elapsed, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--match", default="")
    parser.add_argument("--exclude", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    exclude = {name.strip() for name in args.exclude.split(",") if name.strip()}

    files = discover(args.match, exclude)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "TIMEOUT": 0}
    failures = []
    for path in files:
        result, elapsed, out = run_one(path, args.timeout)
        counts[result] += 1
        print(f"{result:7} {path.name} ({elapsed:.1f}s)", flush=True)
        if result in ("FAIL", "TIMEOUT"):
            failures.append((path.name, out))
    print()
    print(" ".join(f"{key}={counts[key]}" for key in
                   ("PASS", "FAIL", "SKIP", "TIMEOUT")))
    for name, out in failures:
        tail = "\n".join(out.splitlines()[-15:])
        print(f"\n----- {name} の出力の末尾 -----\n{tail}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
