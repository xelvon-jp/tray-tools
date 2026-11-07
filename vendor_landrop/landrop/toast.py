"""Windows のトースト通知を、通知1回ごとに別プロセスで出す。

win11toast（WinRT）を常駐プロセスの中で、しかも受信のたびに別スレッドから呼んでいたら、
_winrt_windows_data_xml_dom の中でアクセス違反（0xC0000005）を起こして常駐ごと落ちた
（2026-10-17。Python の例外にならないのでログにも残らない）。WinRT/COM のオブジェクトは
作ったスレッドと片付けるスレッドが違うと危ういので、常駐からは一切 import しない。
子プロセスなら、仮に落ちても通知が1回出ないだけで済む。

通知はプロセスが終わっても残り、クリック（protocol 起動）は Windows が処理するので、
「フォルダを開く」「ブラウザで開く」はそのまま動く。
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Callable

from .config import PROJECT_ROOT

log = logging.getLogger("landrop")

Launcher = Callable[[list[str]], object]


def _default_launcher(cmd: list[str]) -> object:
    return subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,  # -m landrop.toast を解決できるように
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def toast(title: str, body: str, *, launcher: Launcher | None = None, **kwargs) -> None:
    """win11toast.notify と同じ引数（on_click / buttons / xml など）。待たずに戻る。"""
    payload = json.dumps({"title": title, "body": body, **kwargs}, ensure_ascii=False)
    try:
        (launcher or _default_launcher)([sys.executable, "-m", "landrop.toast", payload])
    except Exception:
        log.exception("通知を出せませんでした")


def _main(argv: list[str]) -> None:
    from win11toast import notify

    notify(**json.loads(argv[1]))


if __name__ == "__main__":
    _main(sys.argv)
