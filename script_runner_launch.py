# script_runner_launch.py
# 「スクリプト実行パネル」(script_runner.py)を別プロセスで起こすための入口。
# slideshow_launch.py と同じ作法(標準ライブラリと、呼ぶ側がすでに読み込んで
# いる PySide6 だけで書く)。本体(feature_screen)・あっふ(filer.py)・
# 外部コマンド(main.py の script-runner)の3か所から import する。
#
# 窓は別プロセス(常駐を巻き込んで落とさない。仕様の前提)。
# 既に開いていれば新しく起こさず、開いているものを前面に出す(判定は
# 名前付きミューテックス。AGENTS.md「単一起動の判定」)。
#
# 再開(restart)は、更新でディスクのコードが新しくなっても開いている
# パネルは古いコードのまま、という問題への口。パネル側が「寝かせ役」
# (このファイルを --respawn で動かす別プロセス。respawn_when_gone)を
# 置いてから自分を閉じ、寝かせ役が消えたのを見て後継を起こす。コードの
# 更新そのもの(自動更新の pull・zip の適用)は tray-tools 側の仕事で、
# ここではやらない。
import subprocess
import sys
import time
from pathlib import Path

from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "script_runner.py"
LAUNCH_PATH = Path(__file__).resolve()

MUTEX_NAME = "Local\\traytools.script_runner"
SERVER_NAME = "traytools.script_runner"
SHOW_COMMAND = b"show\n"
RESTART_COMMAND = b"restart\n"

# 寝かせ役が見張る間隔と上限。パネルが閉じてミューテックスが手放される
# までは数秒もかからない想定で、上限を過ぎたら後継は起こさない。
RESPAWN_POLL_SECONDS = 0.25
RESPAWN_TIMEOUT_SECONDS = 15.0


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


def restart(settings_path=None) -> str:
    """動いていれば再開を頼み("sent")、送れなければ・動いていなければ
    起こす("spawned")。start と同じ分岐で、頼む内容が違うだけ。

    再開の進み方そのもの(寝かせ役を置いて閉じる)はパネル側が持つ。
    ここは「頼む」ところまでで、後継が上がったかは確かめない。
    """
    if is_running() and send(RESTART_COMMAND):
        return "sent"
    spawn(settings_path)
    return "spawned"


def respawn_when_gone(is_running_fn, spawn_fn,
                      timeout=RESPAWN_TIMEOUT_SECONDS,
                      interval=RESPAWN_POLL_SECONDS,
                      sleep_fn=time.sleep, clock=time.monotonic) -> bool:
    """is_running_fn() が False になったら spawn_fn() を1度だけ呼ぶ。

    寝かせ役の心臓部(テストで差し替えられるよう、相手と時計を外から
    受ける)。まだ生きている間は何もしない。timeout を過ぎても消えて
    いなければ、起こさずに諦めて False を返す。後継は高々1つで、
    「再開待ち」のような状態は受け継がない。
    """
    deadline = clock() + timeout
    while is_running_fn():
        if clock() >= deadline:
            return False
        sleep_fn(interval)
    spawn_fn()
    return True


def spawn_respawn_helper(settings_path=None) -> int:
    """寝かせ役(このファイルを --respawn で動かす別プロセス)を起こす。

    パネル自身が再開するときに呼ぶ。パネルが先に終わっても寝かせ役が
    残れるよう、spawn() と同じく切り離して起こす。起こせなければ
    OSError(呼ぶ側が「閉じない」判断に使う)。
    """
    import filer_launch

    filer_launch._allow_foreground()
    argv = [pythonw_executable(), str(LAUNCH_PATH), "--respawn"]
    if settings_path:
        argv += ["--settings", str(settings_path)]
    proc = subprocess.Popen(
        argv,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def _respawn_entry(argv) -> int:
    """寝かせ役の入口。パネルが消えるのを待って、後継を1つ起こす。"""
    settings_path = None
    if "--settings" in argv:
        index = argv.index("--settings")
        if index + 1 < len(argv):
            settings_path = argv[index + 1]
    respawn_when_gone(is_running, lambda: spawn(settings_path))
    return 0


if __name__ == "__main__":
    sys.exit(_respawn_entry(sys.argv[1:]))
