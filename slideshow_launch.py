# slideshow_launch.py
# 写真のスライドショー(slideshow.py)を、常駐(トレイ)やあっふから起こすための入口。
# filer_launch.py と同じ作法で、標準ライブラリと、呼ぶ側がすでに読み込んでいる PySide6 だけで書く。
#
# なぜ別プロセスか
#   写真の読み込みは一度に大量のオブジェクトを作る。常駐の中でそれをやると、GC が常駐の
#   抱える comtypes のオブジェクトの __del__ → Release に入って access violation で落ちる
#   (診断窓で実際に起きた。README「診断」)。だから窓は pythonw.exe + DETACHED_PROCESS の
#   別プロセスで出す(背景を透過・診断・あっふと同じ)。常駐を再起動しても道連れにならない。
#
# 単一起動
#   動いているかは名前付きミューテックス(Local\traytools.slideshow)で決める(CLAUDE.md
#   「単一起動の判定」)。動いていれば新しく起こさず、待ち受け(QLocalServer)へ1行の JSON を
#   送って、動いているものに切り替えてもらう:
#     {"cmd": "play", "folders": [...] | null, "settings": パス}  再生し直す(null は登録済みのフォルダ)
#     {"cmd": "config", "settings": パス}                         設定(秒数・モニタなど)を読み直す
#     {"cmd": "quit"}                                             終わる
#   起こされた slideshow.py の側でも同じ判定をして、先客が居れば同じ依頼を送って引き返す
#   (ここで「動いていない」と見えた直後に別の起動が重なっても2枚にならない)。
import json
import subprocess
from pathlib import Path

import filer_launch
from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "slideshow.py"

MUTEX_NAME = "Local\\traytools.slideshow"
SERVER_NAME = "traytools.slideshow"


def command_bytes(cmd: str, folders=None, settings_path=None) -> bytes:
    payload = {"cmd": cmd}
    if cmd == "play":
        payload["folders"] = list(folders) if folders else None
    if settings_path:
        payload["settings"] = str(settings_path)
    return (json.dumps(payload, ensure_ascii=True) + "\n").encode("ascii")


def parse_command(data: bytes):
    """受け取った1行を辞書にする。読めなければ None。"""
    try:
        text = bytes(data).decode("utf-8", "replace").strip().splitlines()
        payload = json.loads(text[0]) if text else None
    except (ValueError, IndexError):
        return None
    if not isinstance(payload, dict) or payload.get("cmd") not in ("play", "config", "quit"):
        return None
    folders = payload.get("folders")
    if folders is not None and not (isinstance(folders, list)
                                    and all(isinstance(f, str) for f in folders)):
        return None
    return payload


def is_running() -> bool:
    return filer_launch.is_running(MUTEX_NAME)


def send(payload: bytes, timeout_ms: int = 1000) -> bool:
    return filer_launch.send_command(SERVER_NAME, payload, timeout_ms)


def build_argv(folders=None, settings_path=None) -> list:
    argv = [pythonw_executable(), str(SCRIPT_PATH)]
    for folder in folders or []:
        argv += ["--folder", str(folder)]
    if settings_path:
        argv += ["--settings", str(settings_path)]
    return argv


def spawn(folders=None, settings_path=None) -> int:
    """slideshow.py を別プロセスで起こす。folders を渡すと、登録済みのフォルダの代わりに
    そのフォルダ(サブフォルダ込み)を一時的に再生する(設定は変えない)。
    起こせなければ OSError(呼ぶ側で通知する)。"""
    filer_launch._allow_foreground()
    proc = subprocess.Popen(
        build_argv(folders, settings_path),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    return proc.pid


def start(folders=None, settings_path=None) -> str:
    """動いていれば切り替えを頼み("sent")、無ければ起こす("spawned")。
    ミューテックスはあるのに待ち受けに繋がらない(起動の途中・終わりかけ)ときは、
    起こす側に倒す(起きた側が改めて判定するので2枚にはならない)。"""
    if is_running() and send(command_bytes("play", folders, settings_path)):
        return "sent"
    spawn(folders, settings_path)
    return "spawned"


def reload_config(settings_path=None) -> bool:
    """動いていれば設定を読み直してもらう。動いていなければ何もしない(False)。"""
    if not is_running():
        return False
    return send(command_bytes("config", settings_path=settings_path))


def stop() -> bool:
    if not is_running():
        return False
    return send(command_bytes("quit"))
