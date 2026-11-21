# -*- coding: utf-8 -*-
"""起動用のショートカット（.lnk）をこのフォルダに作る。タスクバーへのピン留め用。

    python make_shortcut.py                 # 壁打ち文字起こし.lnk を作る
    python make_shortcut.py --python <pythonw.exe>   # 使う pythonw.exe を指定する

- リンク先は pythonw.exe（黒い窓が出ない）＋ main.py。作業フォルダはこのフォルダ。アイコンは assets\\kabeuchi_stt.ico
  （無ければ先に make_assets.py を自動で実行する）。
- 作るのはプロジェクトフォルダの中だけ（デスクトップ等には作らない）。
- WScript.Shell を、別プロセスの一回限りの `powershell -Command`（インライン。.ps1 ファイルは使わない）で呼ぶ。
- pythonw.exe は、いま実行している python と同じフォルダの pythonw.exe（venv のものを自動で使う）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "壁打ち文字起こし"


def find_pythonw(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"指定された pythonw.exe が見つかりません: {p}")
        return p
    here = Path(sys.executable).parent
    for cand in (here / "pythonw.exe", here.parent / "Scripts" / "pythonw.exe"):
        if cand.exists():
            return cand
    raise SystemExit("pythonw.exe が見つかりません。--python で指定してください。")


def ps_quote(s: str) -> str:
    """PowerShell のシングルクォート文字列にする（' は '' に）。"""
    return "'" + s.replace("'", "''") + "'"


def build_command(lnk: Path, target: Path, args: str, workdir: Path, icon: Path, desc: str) -> str:
    return (
        "$w = New-Object -ComObject WScript.Shell; "
        f"$s = $w.CreateShortcut({ps_quote(str(lnk))}); "
        f"$s.TargetPath = {ps_quote(str(target))}; "
        f"$s.Arguments = {ps_quote(args)}; "
        f"$s.WorkingDirectory = {ps_quote(str(workdir))}; "
        f"$s.IconLocation = {ps_quote(str(icon) + ',0')}; "
        f"$s.Description = {ps_quote(desc)}; "
        "$s.WindowStyle = 1; "
        "$s.Save()"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="タスクバーへのピン留め用ショートカットをこのフォルダに作る")
    ap.add_argument("--python", help="使う pythonw.exe（省略すると今の python と同じ場所のもの）")
    ap.add_argument("--print-only", action="store_true", help="作らずに、実行する PowerShell コマンドだけ表示する")
    a = ap.parse_args(argv)

    icon = ROOT / "assets" / "kabeuchi_stt.ico"
    if not icon.exists() and not a.print_only:
        subprocess.run([sys.executable, str(ROOT / "make_assets.py")], check=True)
    pyw = find_pythonw(a.python)
    lnk = ROOT / f"{NAME}.lnk"
    cmd = build_command(lnk, pyw, f'"{ROOT / "main.py"}"', ROOT, icon, "壁打ち文字起こし（ローカル完結）")
    if a.print_only:
        print(cmd)
        return 0
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not lnk.exists():
        print("ショートカットを作れませんでした:", (r.stderr or r.stdout).strip())
        return 1
    print("作成:", lnk)
    print("リンク先:", pyw, f'"{ROOT / "main.py"}"')
    print("使い方: README の「タスクバーにピン留めする」を見てください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
