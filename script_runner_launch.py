# script_runner_launch.py
# 「スクリプト実行パネル」(script_runner.py)を別プロセスで起こすための入口。
# slideshow_launch.py と同じ作法(標準ライブラリと、呼ぶ側がすでに読み込んで
# いる PySide6 だけで書く)。本体(feature_screen)・あっふ(filer.py)・
# 外部コマンド(main.py の script-runner)の3か所から import する。
#
# 窓は別プロセス(常駐を巻き込んで落とさない。仕様の前提)。
# 既に開いていれば新しく起こさず、開いているものを前面に出す(判定は
# 名前付きミューテックス。AGENTS.md「単一起動の判定」)。
import subprocess
from pathlib import Path

from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "script_runner.py"

MUTEX_NAME = "Local\\traytools.script_runner"
SERVER_NAME = "traytools.script_runner"
SHOW_COMMAND = b"show\n"


def is_running() -> bool:
    import filer_launch

    return filer_launch.is_running(MUTEX_NAME)


def send(payload: bytes, timeout_ms: int = 1000) -> bool:
    import filer_launch

    return filer_launch.send_command(SERVER_NAME, payload, timeout_ms)


def build_argv(settings_path=None) -> list:
    argv = [pythonw_executable(), str(SCRIPT_PATH)]
    if settings_path:
        argv += ["--settings", str(settings_path)]
    return argv


def spawn(settings_path=None) -> int:
    """script_runner.py を別プロセスで起こす。起こせなければ OSError。"""
    import filer_launch

    filer_launch._allow_foreground()
    proc = subprocess.Popen(
        build_argv(settings_path),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def start(settings_path=None) -> str:
    """動いていれば前面化を頼み("sent")、無ければ起こす("spawned")。"""
    if is_running() and send(SHOW_COMMAND):
        return "sent"
    spawn(settings_path)
    return "spawned"
