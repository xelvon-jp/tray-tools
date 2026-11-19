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

# 寝かせ役が見張る間隔と上限。上限を過ぎたら後継は起こさない。
# 上限は 120 秒 (2.88 までは 15 秒)。閉じる側の待ち (実行中
# スクリプトの停止や設定保存のロック) や、ダイアログの残りで
# プロセスの終了が遅れると、15 秒では諦めてしまい、開き直しが
# 復活しない実例があった (2026-11-19)。諦めたときは理由を
# ログ (phal-respawn.log) に残す。
RESPAWN_POLL_SECONDS = 0.25
RESPAWN_TIMEOUT_SECONDS = 120.0


def _respawn_log_path():
    """寝かせ役のログの置き場 (script_runner_core.data_dir と
    同じ作法。ここは標準ライブラリだけで書く)。"""
    import os
    from pathlib import Path

    override = os.environ.get("TRAYTOOLS_DATA_DIR")
    if override:
        return Path(override) / "phal-respawn.log"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "traytools" / "phal-respawn.log"


def _respawn_log(message: str) -> None:
    """寝かせ役の出来事を1行残す。失敗しても寝かせ役は止めない。"""
    try:
        path = _respawn_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001  ログで本筋を止めない
        pass


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
                      sleep_fn=time.sleep, clock=time.monotonic,
                      log_fn=None) -> bool:
    """is_running_fn() が False になったら spawn_fn() を1度だけ呼ぶ。

    寝かせ役の心臓部(テストで差し替えられるよう、相手と時計を外から
    受ける)。まだ生きている間は何もしない。timeout を過ぎても消えて
    いなければ、起こさずに諦めて False を返す。後継は高々1つで、
    「再開待ち」のような状態は受け継がない。
    結末 (後継を起こした / 諦めた) は log_fn へ残す (2.88。
    既定は phal-respawn.log への追記)。
    """
    if log_fn is None:
        log_fn = _respawn_log
    deadline = clock() + timeout
    while is_running_fn():
        if clock() >= deadline:
            log_fn(
                f"寝かせ役: {timeout:.0f}秒待っても前の PHAL が"
                "終わらないため、後継を起こさずに諦めました")
            return False
        sleep_fn(interval)
    spawn_fn()
    log_fn("寝かせ役: 前の PHAL の終了を確認し、後継を起こしました")
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
    _respawn_log(
        f"寝かせ役: 開始（上限 {RESPAWN_TIMEOUT_SECONDS:.0f} 秒）")
    respawn_when_gone(is_running, lambda: spawn(settings_path))
    return 0


if __name__ == "__main__":
    sys.exit(_respawn_entry(sys.argv[1:]))
