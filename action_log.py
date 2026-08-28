# action_log.py
# 「何をしたか」を1行ずつ残す。error.log(例外)や crash.log(プロセス即死)と違い、
# 正常に動いたことを記録する側。
#
# 要るようになったのは、外(名前付きパイプ)からスリープや抑止を操作できるようにして
# からである。あの口は一方向で、送った側は結果を受け取れない。実際に予約が入ったのか、
# 引数を間違えて無視されたのかが分からず、「エラーが出ていないから多分成功」という
# 確かめ方しかできなかった。ここに残しておけば後から確かめられる。
#
# 電源まわりに限らず、後から「いつ何をしたか」を辿りたい操作は同じ口で記録する。
import sys
from datetime import datetime
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent / "action.log"

# 切り詰める大きさ(バイト)。1行100バイト前後なので、これで数万行ぶん残る。
# 日ごとにファイルを分けないのは、跨いで探すほうが面倒なため。
MAX_BYTES = 512 * 1024
KEEP_BYTES = 256 * 1024


def _trim() -> None:
    """大きくなりすぎたら古いほうを捨てる。

    行の途中で切れると読みにくいので、最初の改行まで進めてから残す。"""
    try:
        if LOG_PATH.stat().st_size <= MAX_BYTES:
            return
        data = LOG_PATH.read_bytes()[-KEEP_BYTES:]
        cut = data.find(b"\n")
        if cut >= 0:
            data = data[cut + 1:]
        LOG_PATH.write_bytes(data)
    except OSError:
        pass


def record(action: str, detail: str = "", source: str = "") -> None:
    """1行残す。書けなくても何も投げない。

    source は「誰が呼んだか」(menu / hotkey / external など)。同じ操作でも、自分で
    押したのか外から送られたのかで、後から見たときの意味が変わる。「気づいたら寝て
    いた」ときに、自分の予約なのか外からの指示なのかが分かる。

    ログを書けないことは、その操作の失敗ではない。呼ぶ側の流れを止めないこと。"""
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [stamp, action]
        if detail:
            parts.append(detail)
        if source:
            parts.append(f"({source})")
        line = "  ".join(parts) + "\n"
        with open(LOG_PATH, "a", encoding="utf-8", newline="") as f:
            f.write(line)
        _trim()
    except OSError as e:
        print(f"[tray-tools] 操作ログを書けません: {e}", file=sys.stderr)


def tail(lines: int = 20) -> str:
    """末尾を読む。無ければ空文字。外から様子を見るとき用。"""
    try:
        text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "".join(text.splitlines(keepends=True)[-lines:])
