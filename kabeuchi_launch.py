# kabeuchi_launch.py
# 「壁打ち文字起こし」(kabeuchi_stt/main.py)を別プロセスで起こすための入口。本体
# (feature_screen)から import する。標準ライブラリだけで書く(PySide6・faster-whisper・
# numpy を import しない。窓を出す側は kabeuchi_stt の中で、本体はここで「起こす」だけ)。
#
# なぜ別プロセスか
#   Whisper の推論は CPU を使い切る。常駐と同居させると、録音中にホットキーやメニューが
#   もたつく。しかも faster-whisper は重い(別 venv に入れる。setup.py --with-whisper)。
#   背景透過やスライドショーと同じく pythonw.exe + DETACHED_PROCESS で起こすので、常駐を
#   再起動しても開いている窓は道連れにならない。
#
# トレイアイコンを増やさない
#   通知領域のアイコンは2つに固定する方針(AGENTS.md)。kabeuchi_stt は自前のトレイアイコンを
#   持つので、ここからは必ず --no-tray を付けて起こす。--no-tray のときは窓を閉じるとプロセスが
#   終わる(取り残されない)。
#
# 単一起動
#   動いていれば「出して」を送る(kabeuchi_stt の QLocalServer。名前は SERVER_NAME)。届かなければ
#   起こす。起こした側も、先客が居れば自分で「出して」を送って引き返す(kabeuchi_stt/main.py)ので、
#   二重に押しても窓は1つ。判定にミューテックスを使わないのは kabeuchi_stt 側の作りに合わせたため。
#
# 使う Python
#   settings.json の kabeuchi.python → %USERPROFILE%\.venvs\kabeuchi-stt の pythonw.exe →
#   tray-tools の venv(faster_whisper が import できるときだけ)の順。どれも使えなければ、
#   LaunchError の文面(「python setup.py --with-whisper を実行してください」)を呼ぶ側が通知する。
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

from traytools_send import (
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
    pythonw_executable,
)

SCRIPT_PATH = Path(__file__).resolve().parent / "kabeuchi_stt" / "main.py"

# kabeuchi_stt/single_instance.py の SERVER_NAME と同じ。片方を変えたら両方を揃えること。
SERVER_NAME = "kabeuchi_stt_v2_single_instance"
SHOW_COMMAND = b"show"

# settings.json の置き場(ホットキーは hotkeys.py が「ドット区切りの名前」で引く)。
HOTKEY_NAME = "kabeuchi.hotkey"

SETUP_COMMAND = "python setup.py --with-whisper"

# 動かすのに要る Python パッケージ。コード(import 名)で持つ。
REQUIRED_MODULES = ("faster_whisper", "sounddevice", "numpy", "PySide6")


class LaunchError(OSError):
    """起こせなかった。メッセージは、そのまま利用者に見せられる日本語。"""


def default_python(home=None) -> Path:
    """専用 venv の pythonw.exe(setup.py --with-whisper が作る)。"""
    base = Path(home) if home else Path.home()
    return base / ".venvs" / "kabeuchi-stt" / "Scripts" / "pythonw.exe"


def configured_python(settings) -> str:
    """settings.json の kabeuchi.python(空なら "")。壊れた値は空として扱う。"""
    try:
        value = (settings or {}).get("kabeuchi", {}).get("python", "")
    except AttributeError:
        return ""
    return value.strip() if isinstance(value, str) else ""


def configured_hotkey(settings) -> str:
    """settings.json の kabeuchi.hotkey(空 = 割り当てなし。既定は空)。"""
    try:
        value = (settings or {}).get("kabeuchi", {}).get("hotkey", "")
    except AttributeError:
        return ""
    return value.strip() if isinstance(value, str) else ""


def missing_dependencies(python_path, exists=os.path.exists) -> list:
    """python_path の venv に入っていないパッケージ名のリスト。

    venv の作りでないもの(システムの Python など)は中身が分からないので空を返す
    (入っているものとして起こす。無ければ kabeuchi_stt の側で落ちる)。"""
    scripts = Path(python_path).parent
    venv = scripts.parent
    if scripts.name.lower() != "scripts" or not exists(str(venv / "pyvenv.cfg")):
        return []
    site = venv / "Lib" / "site-packages"
    missing = []
    for name in REQUIRED_MODULES:
        if not (exists(str(site / name)) or exists(str(site / (name + ".py")))):
            missing.append(name)
    return missing


def _tray_python_has_whisper(find_spec) -> bool:
    """いま動いている tray-tools の Python に faster_whisper と sounddevice があるか。
    find_spec は読み込まずに探すだけ(常駐に重いものを乗せない)。"""
    try:
        return all(find_spec(name) is not None for name in ("faster_whisper", "sounddevice"))
    except (ImportError, ValueError, AttributeError):
        return False


def _prefer_pythonw(path: str, exists) -> str:
    """python.exe が指定されていたら、黒い窓が出ない pythonw.exe(同じ場所にあれば)を使う。"""
    p = Path(path)
    if p.name.lower() == "python.exe":
        sibling = p.with_name("pythonw.exe")
        if exists(str(sibling)):
            return str(sibling)
    return path


def resolve_python(settings=None, *, exists=os.path.exists, which=shutil.which,
                   find_spec=importlib.util.find_spec, home=None, tray_python=None) -> str:
    """使う pythonw.exe(のパス)を決める。使えるものが無ければ LaunchError。

    引数の exists / which / find_spec / home / tray_python は、テストで差し替えるためのもの。"""
    configured = configured_python(settings)
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        found = expanded if exists(expanded) else which(expanded)
        if not found:
            raise LaunchError(
                "壁打ち文字起こし: settings.json の kabeuchi.python に指定された Python が"
                f"見つかりません\n{configured}")
        found = _prefer_pythonw(found, exists)
        missing = missing_dependencies(found, exists)
        if missing:
            raise LaunchError(
                "壁打ち文字起こし: kabeuchi.python の Python に必要なパッケージが入っていません"
                f"({', '.join(missing)})\nその Python に pip install -r kabeuchi_stt\\requirements.txt "
                f"を実行するか、kabeuchi.python を空にして {SETUP_COMMAND} を実行してください")
        return found

    dedicated = default_python(home)
    dedicated_missing = None
    if exists(str(dedicated)):
        dedicated_missing = missing_dependencies(dedicated, exists)
        if not dedicated_missing:
            return str(dedicated)

    # 専用 venv が無い(か、中身が足りない)。tray-tools の venv に揃っていればそれを使う。
    tray = tray_python or pythonw_executable()
    if _tray_python_has_whisper(find_spec) and exists(str(tray)):
        return str(tray)

    if dedicated_missing:
        raise LaunchError(
            "壁打ち文字起こし: 専用の venv に必要なパッケージが入っていません"
            f"({', '.join(dedicated_missing)})\ntray-tools のフォルダで {SETUP_COMMAND} を実行してください")
    raise LaunchError(
        "壁打ち文字起こし: 使える Python が見つかりません\n"
        f"tray-tools のフォルダで {SETUP_COMMAND} を実行してください")


def build_argv(python_path, script=None) -> list:
    """起動コマンド。--no-tray を必ず付ける(トレイアイコンを増やさない)。"""
    return [str(python_path), str(script or SCRIPT_PATH), "--no-tray"]


def _allow_foreground() -> None:
    """前面化の権利を譲る(bg_remove_launch と同じ理由)。失敗しても起こすのは続ける。"""
    try:
        import filer_launch

        filer_launch._allow_foreground()
    except Exception:  # noqa: BLE001  前面化の手当てで起動を止めない
        pass


def send_show(timeout_ms: int = 500) -> bool:
    """動いている壁打ち文字起こしに「窓を出して」を送る。届いたら True。

    返事は待たない(kabeuchi_stt は返事を返さない作りで、待つと常駐の UI がその間止まる)。
    PySide6 はここで初めて読む(呼ぶ側の常駐がすでに読み込んでいるもの)。"""
    from PySide6.QtNetwork import QLocalSocket

    _allow_foreground()
    sock = QLocalSocket()
    try:
        sock.connectToServer(SERVER_NAME)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(SHOW_COMMAND)
        sock.flush()
        sock.waitForBytesWritten(timeout_ms)
        sock.disconnectFromServer()
        return True
    finally:
        sock.abort()


def spawn(settings=None, *, popen=subprocess.Popen, script=None, **resolve_kwargs) -> int:
    """kabeuchi_stt を別プロセスで起こす。起こせなければ LaunchError(OSError)。"""
    script_path = Path(script) if script else SCRIPT_PATH
    if not script_path.exists():
        raise LaunchError(
            f"壁打ち文字起こし: {script_path} が見つかりません\n"
            "tray-tools を最新にしてください(kabeuchi_stt フォルダが欠けています)")
    python_path = resolve_python(settings, **resolve_kwargs)
    _allow_foreground()
    try:
        proc = popen(
            build_argv(python_path, script_path),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            cwd=str(script_path.parent),
        )
    except OSError as e:
        raise LaunchError(f"壁打ち文字起こし: 起動できませんでした\n{e}") from e
    return proc.pid


def start(settings=None, *, sender=send_show, spawner=spawn) -> str:
    """動いていれば窓を出してもらい("sent")、無ければ起こす("spawned")。

    起こせなければ LaunchError。判定できない(送れない)ときは起こす側へ倒す。
    二重に起きても、後から起きた側が先客に「出して」を送って引き返す。"""
    try:
        if sender():
            return "sent"
    except Exception:  # noqa: BLE001  送れなければ起こすだけ
        pass
    spawner(settings)
    return "spawned"
