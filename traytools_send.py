# traytools_send.py
# 常駐中の tray-tools にコマンドを1つ送るだけの小さなクライアント。
#
#   python traytools_send.py bookmark "C:\現在のパス"
#
# あふｗ(AFXW.EXE)から呼ぶのが主な用途。AFXW.key には次のように書く($P はカレントパス):
#
#   K00xx="4074C:\Users\yotan\.venvs\tray-tools\Scripts\pythonw.exe R:\claude\tray-tools\traytools_send.py bookmark "$P""
#
# PySide6 を import しないのは意図的。Qt の import だけで1秒近くかかり、キーを押してから
# ピッカーが出るまでの体感がそのぶん遅くなる。標準ライブラリだけで書く。
#
# tray-tools の待ち受け(QLocalServer)は Windows では名前付きパイプなので、素の open() で
# 書き込める。tray-tools が起動していなければパイプ自体が無く FileNotFoundError になる。
# その場合はこちらが tray-tools を起動し、待ち受けが立つのを待ってから送り直す
# (あふｗ から呼んだのに「常駐していないので何も起きない」では理由が分からないため。
#  pythonw で呼ばれるとこのスクリプトの標準エラーもどこにも出ない)。
import json
import subprocess
import sys
import time
from pathlib import Path

PIPE_PATH = r"\\.\pipe\traytools.single-instance"
MAIN_SCRIPT = Path(__file__).resolve().parent / "main.py"

# 起動を待つ間隔と回数。Qtの初期化とトレイアイコンの構築で1〜2秒かかる。
STARTUP_POLL_INTERVAL = 0.2
STARTUP_POLL_COUNT = 30

# 親(あふｗ)が終わってもtray-toolsが道連れにならないよう切り離して起動する。
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _write(command: str, args: list) -> None:
    payload = json.dumps({"command": command, "args": args}, ensure_ascii=False)
    with open(PIPE_PATH, "wb") as pipe:
        pipe.write((payload + "\n").encode("utf-8"))


def _pythonw() -> str:
    """このスクリプトを動かしている実行ファイルから、窓の出ない pythonw.exe を割り出す。

    python.exe から呼ばれた場合にそのまま使うと、tray-tools にコンソール窓が付いてくる。
    見つからなければ今の実行ファイルで妥協する(窓は出るが起動はできる)。"""
    current = Path(sys.executable)
    candidate = current.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else current)


def _start_traytools() -> bool:
    if not MAIN_SCRIPT.exists():
        print(f"tray-tools 本体が見つかりません: {MAIN_SCRIPT}", file=sys.stderr)
        return False
    try:
        subprocess.Popen(
            [_pythonw(), str(MAIN_SCRIPT)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except OSError as e:
        print(f"tray-tools を起動できませんでした: {e}", file=sys.stderr)
        return False


def send(command: str, args: list) -> int:
    try:
        _write(command, args)
        return 0
    except FileNotFoundError:
        pass  # 待ち受けが無い＝未起動。下で起動してから送り直す
    except OSError as e:
        print(f"tray-tools へ送れませんでした: {e}", file=sys.stderr)
        return 1

    if not _start_traytools():
        return 1

    for _ in range(STARTUP_POLL_COUNT):
        time.sleep(STARTUP_POLL_INTERVAL)
        try:
            _write(command, args)
            return 0
        except FileNotFoundError:
            continue  # まだ待ち受けが立っていない
        except OSError:
            continue  # 立ち上がりかけでビジー。次の周回で試す

    print("tray-tools を起動しましたが、待ち受けに繋がりませんでした。", file=sys.stderr)
    return 1


def main() -> int:
    if len(sys.argv) < 2:
        print("使い方: traytools_send.py <command> [args...]", file=sys.stderr)
        return 2
    return send(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
