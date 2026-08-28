# traytools_send.py
# 常駐中の tray-tools にコマンドを1つ送り、返ってきた応答を標準出力へ出す小さなクライアント。
#
#   python traytools_send.py bookmark "C:\現在のパス"
#   python traytools_send.py status
#   python traytools_send.py log 50
#   python traytools_send.py notify "変換が終わりました"
#   python traytools_send.py beep done
#   python traytools_send.py pushover "変換が終わりました" --title 変換
#
# 【応答】以前この口は一方向で、送った側は「エラーが出ていないから多分成功」という
# 確かめ方しかできなかった。いまは本体が1つ応答を返す。
#   1行目 : "OK ..." なら成功(終了コード0)、"ERR ..." なら失敗(終了コード1)
#   2行目〜: 本文(status / log のように中身を返すコマンドだけ)
# 応答が来ないまま時間切れになったら、送れたものとして 0 で終わる。応答を返さない
# 古い本体が常駐している場合でも、あふｗからの呼び出しが失敗扱いにならないようにするため。
#
# あふｗ(AFXW.EXE)から呼ぶのが主な用途。AFXW.key には次のように書く($P はカレントパス):
#
#   K00xx="4074<venvのpythonw.exe> <このスクリプトのフルパス> bookmark "$P""
#
# PySide6 を import しないのは意図的。Qt の import だけで1秒近くかかり、キーを押してから
# ピッカーが出るまでの体感がそのぶん遅くなる。標準ライブラリだけで書く。
#
# tray-tools の待ち受け(QLocalServer)は Windows では名前付きパイプなので、素の open() で
# 書き込める。tray-tools が起動していなければパイプ自体が無く FileNotFoundError になる。
# その場合はこちらが tray-tools を起動し、待ち受けが立つのを待ってから送り直す
# (あふｗ から呼んだのに「常駐していないので何も起きない」では理由が分からないため。
#  pythonw で呼ばれるとこのスクリプトの標準エラーもどこにも出ない)。
import ctypes
import json
import msvcrt
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

# 応答を待つ上限(秒)。本体は受け取った直後に1行返すので普通は一瞬で終わる。
RESPONSE_TIMEOUT = 5.0
# 応答が来ているかを覗く間隔(秒)。返事は普通すぐ来るので最初は細かく覗き、
# 待ちが長引いたら間隔を広げる(pushover は最長25秒待つ。そこを細かく回す意味は無い)。
RESPONSE_POLL_FAST = 0.002
RESPONSE_POLL_SLOW = 0.02
RESPONSE_POLL_FAST_SECONDS = 0.3
# 待ちの入るコマンドだけ長くする。pushover は外へ HTTP を投げるので数秒かかりうる
# (本体側のタイムアウトは10秒、応答を諦めるのが20秒)。
SLOW_RESPONSE_TIMEOUT = {"pushover": 25.0}


def _response_timeout(command: str) -> float:
    return SLOW_RESPONSE_TIMEOUT.get(command, RESPONSE_TIMEOUT)


def _available(handle) -> int:
    """パイプに読めるバイト数。相手が切っていれば -1。

    ReadFile は1バイトも来ていないと戻ってこない。応答を返さない相手(この機能を足す
    前の本体)に当たると、そこで永久に待つことになる。先に覗いて、来ているぶんだけ
    読むようにする。

    読み取りを別スレッドへ追い出して時間切れで見切る手も試したが、あれは駄目だった。
    ブロック中のスレッドを残したままハンドルを閉じると close() 自体が戻らず、結局
    プロセスが固まる。覗いてから読めば、そもそも誰もブロックしない。"""
    count = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.PeekNamedPipe(
        ctypes.c_void_p(handle), None, 0, None, ctypes.byref(count), None
    )
    return count.value if ok else -1


def _read_response(pipe, timeout: float) -> bytes:
    """応答を最後まで読む。相手が切るか、時間切れになるまで。

    本体は1つ応答を書いたら切断するので、普通は「読める→読む→切れた」で終わる。"""
    handle = msvcrt.get_osfhandle(pipe.fileno())
    started = time.monotonic()
    deadline = started + timeout
    chunks = []
    peer_gone = False
    while True:
        count = _available(handle)
        if count < 0:
            peer_gone = True  # 相手が切った。書き置きがあれば下で拾う
            break
        if count:
            deadline = time.monotonic() + timeout  # 続きが来ている間は待ち直す
            try:
                chunks.append(pipe.read(count))
            except OSError:
                break
            continue
        now = time.monotonic()
        if now >= deadline:
            break  # 応答を返さない相手。ここで見切る
        time.sleep(
            RESPONSE_POLL_FAST if now - started < RESPONSE_POLL_FAST_SECONDS
            else RESPONSE_POLL_SLOW
        )
    # 切れる直前に書かれたぶんが残っていることがある。相手が切ったと分かった場合だけ
    # 拾いにいく——繋がったままのパイプを読むと、そこで戻ってこなくなる
    # (時間切れで抜けてきた道がまさにその状況)。
    if peer_gone:
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError:
            pass
    return b"".join(chunks)


def _exchange(command: str, args: list, timeout: float):
    """1行送って応答を読む。応答が来なければ None。

    名前付きパイプは双方向(QLocalServer は PIPE_ACCESS_DUPLEX で開く)なので、
    同じハンドルで書いて読める。PySide6 を import しない方針は変えない——Qt の
    import だけで1秒近くかかり、キーを押してからウインドウが出るまでの体感が
    そのぶん遅くなる(このスクリプトの存在理由そのもの)。"""
    payload = json.dumps({"command": command, "args": args}, ensure_ascii=False)
    try:
        pipe = open(PIPE_PATH, "r+b", buffering=0)
    except FileNotFoundError:
        raise  # 待ち受けが無い＝未起動。呼んだ側が起動してから送り直す
    except OSError:
        # 読み書き両用で開けない事情があっても、コマンドだけは届ける
        # (この機能を足す前と同じ一方通行の送り方に落ちる)。
        _write(command, args)
        return None
    with pipe:
        pipe.write((payload + "\n").encode("utf-8"))
        data = _read_response(pipe, timeout)
    if not data:
        return None
    return data.decode("utf-8", errors="replace").rstrip("\r\n")


def _write(command: str, args: list) -> None:
    """応答を読まずに送るだけの口。本体側はこの使い方でも壊れない
    (相手が居なくなったソケットへの書き込みは例外にならない)。"""
    payload = json.dumps({"command": command, "args": args}, ensure_ascii=False)
    with open(PIPE_PATH, "wb") as pipe:
        pipe.write((payload + "\n").encode("utf-8"))


def _relax_console() -> None:
    """コンソールに出せない文字が来ても落ちないようにする。

    応答には日本語が混じる。日本語 Windows のコンソールは cp932 なので、絵文字などが
    1文字混じるだけで UnicodeEncodeError になり、応答が丸ごと消える。文字コード自体は
    変えない(utf-8 にするとコンソールで化ける)。置換に倒すだけ。

    pythonw.exe から呼ばれると sys.stdout は None になる。その場合 print は黙って
    何もしないので、ここで触らないよう避ける。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


def pythonw_executable() -> str:
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
            [pythonw_executable(), str(MAIN_SCRIPT)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        return True
    except OSError as e:
        print(f"tray-tools を起動できませんでした: {e}", file=sys.stderr)
        return False


def _report(response) -> int:
    """応答を標準出力へ出し、終了コードを決める。

    応答が無い(None)のは失敗扱いにしない。応答を返さない古い本体が常駐している場合や、
    こちらが待ち切れなかった場合が該当し、コマンド自体は届いている。あふｗから呼んだ
    ときにここで 1 を返すと、届いているのに失敗したように見える。"""
    if response is None:
        print("tray-tools へ送りました（応答はありませんでした）", file=sys.stderr)
        return 0
    print(response)
    return 0 if response.startswith("OK") else 1


def send(command: str, args: list) -> int:
    timeout = _response_timeout(command)
    try:
        return _report(_exchange(command, args, timeout))
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
            return _report(_exchange(command, args, timeout))
        except FileNotFoundError:
            continue  # まだ待ち受けが立っていない
        except OSError:
            continue  # 立ち上がりかけでビジー。次の周回で試す

    print("tray-tools を起動しましたが、待ち受けに繋がりませんでした。", file=sys.stderr)
    return 1


USAGE = """使い方: traytools_send.py <command> [args...]

  bookmark [パス]              フォルダブックマークを開く
  keep-awake on|off [分]       スリープ抑止の入切
  sleep [秒|cancel] [hibernate] スリープ／休止の予約・取り消し
  status                       いまの状態を返す
  log [行数]                   操作ログの末尾を読む（既定20行）
  notify <本文>                トーストを1枚出す
  beep [ok|done|warn|error|ask] 音を1つ鳴らす
  pushover <本文> [--title T] [--priority N] [--sound S]
                               スマホへプッシュ通知（要トークン登録）"""


def main() -> int:
    _relax_console()
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    return send(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
