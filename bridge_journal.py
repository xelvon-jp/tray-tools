# bridge_journal.py
# Claude と Muse の「やり取りの記録」を、日ごとのファイルへ1行1イベントで残す。
#
# 書くのは見張り・郵便受けの投稿スクリプト・フックなど複数のプロセスで、読むのは
# 連携パネルの材料集めや、あとからの振り返り。だから作りは単純に寄せてある:
#   - 保存先は <dir>/YYYY-MM-DD.jsonl(その日のローカル日付)。既定の置き場所は
#     default_dir() で、公開リポジトリの外(%LOCALAPPDATA%\traytools\bridge_events)。
#   - 追記は「1行を1回の書き込み(os.open の O_APPEND + os.write 1回)」にする。
#     複数のプロセスが同時に書いても行同士が混ざらないため(1行は4KB未満に収める)。
#   - 記録のせいで本体を止めない: 書けなくても例外は出さず、False を返すだけ。
#   - 漏えい防止: キー名に token / secret / password / key を含む項目は値を伏せる。
# 標準ライブラリだけ(PySide6 不要)。Windows でも Linux でも動く。
#
# イベント名は呼び出し側の自由(ev で縛らない)。想定している例:
#   "kind"     見張りが見た Muse の状態が変わった(prev, kind, header)
#   "approval" 承認カードが出てから押すまで(card の要約, sec)
#   "mail_in"  郵便受けに届いた(id, kind, note)
#   "ack"      郵便受けの ack が返った(id, ms)
#   "post_out" チャットへ投稿した(to, head 先頭80字, ok)
#   "monitor"  見張りの開始・停止(phase="start"|"stop", reason, params)
#   "wake"     起こしに行った(reason)
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

# これより古い日のファイルは消す(残すのは今日を含めて15日分)。
RETENTION_DAYS = 14
# 文字列の値の上限(文字数)。長い値はここで切る。
MAX_VALUE_CHARS = 200
# 1行の上限(バイト)。O_APPEND の1回の書き込みで混ざらないための目安。
MAX_LINE_BYTES = 4096
# キー名にこれらを含む項目は値を伏せる(小文字にして部分一致)。
_SECRET_WORDS = ("token", "secret", "password", "key")
# 行に必ず入る項目(4KBに収まらないときも、これは落とさない)。
_CORE_KEYS = ("ts", "epoch", "pid", "ev")

# 最後に古いファイルを整理した日。呼び出しのたびに一覧を見ないための控え。
_last_cleanup_date = None


def default_dir() -> Path:
    """記録の既定の置き場所(%LOCALAPPDATA%\traytools\bridge_events)。"""
    return Path(os.environ.get("LOCALAPPDATA") or ".") / "traytools" / "bridge_events"


def _resolve_dir(dir) -> Path:
    return Path(dir) if dir is not None else default_dir()


def _sanitize(fields: dict) -> dict:
    """値を安全な形にする: 秘密らしいキーは伏せ、文字列は200字で切る。

    JSON にできない値は文字列化してから切る(落とさない)。"""
    out = {}
    for key, value in fields.items():
        name = str(key)
        if any(word in name.lower() for word in _SECRET_WORDS):
            out[name] = "***"
        elif isinstance(value, str):
            out[name] = value[:MAX_VALUE_CHARS]
        elif value is None or isinstance(value, (int, float, bool)):
            out[name] = value
        else:
            out[name] = str(value)[:MAX_VALUE_CHARS]
    return out


def _encode(record: dict) -> bytes:
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def _fit_line(record: dict) -> bytes:
    """1行が MAX_LINE_BYTES に収まるようにする。

    収まらないときは、大きい項目から落として "truncated": true を付け、
    行そのものは壊さない(壊れた行は読む側が飛ばすので、記録が消えるため)。
    コア項目だけなら必ず収まるので、最終手段のバイト切りはまず来ない。"""
    line = _encode(record)
    if len(line) <= MAX_LINE_BYTES:
        return line
    record = dict(record, truncated=True)
    droppable = sorted(
        (k for k in record if k not in _CORE_KEYS and k != "truncated"),
        key=lambda k: len(str(record[k])), reverse=True)
    for key in droppable:
        del record[key]
        line = _encode(record)
        if len(line) <= MAX_LINE_BYTES:
            return line
    return line[:MAX_LINE_BYTES - 1] + b"\n"


def log(ev, **fields) -> bool:
    """イベントを1行追記する。書けたら True、失敗したら False(例外は出さない)。

    項目には時刻(ts はローカル時刻の ISO 文字列・ミリ秒まで、epoch は秒)と
    pid と ev が自動で入る。dir は予約キー: 保存先の上書き(テスト用)で、
    記録には残らない。"""
    base = _resolve_dir(fields.pop("dir", None))
    try:
        stamp = time.time()
        now = datetime.fromtimestamp(stamp).astimezone()
        record = {"ts": now.isoformat(timespec="milliseconds"),
                  "epoch": stamp, "pid": os.getpid(), "ev": str(ev)}
        record.update(_sanitize(fields))
        line = _fit_line(record)
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{now.date().isoformat()}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)   # 1行を1回で書く(他プロセスの行と混ざらない)
        finally:
            os.close(fd)
        _maybe_cleanup(base, now.date())
        return True
    except Exception:  # noqa: BLE001  記録のせいで呼び出し元を止めない
        return False


# ---------------------------------------------------------------------------
# 古いファイルの整理(1日1回だけ)
# ---------------------------------------------------------------------------
def _list_files(base: Path) -> list:
    """(日付, パス) の一覧を古い順に。YYYY-MM-DD.jsonl の形のものだけ。"""
    out = []
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        if len(name) == 16 and name.endswith(".jsonl"):
            try:
                day = date.fromisoformat(name[:-6])
            except ValueError:
                continue
            out.append((day, base / name))
    out.sort(key=lambda item: item[0])
    return out


def _maybe_cleanup(base: Path, today: date) -> None:
    """RETENTION_DAYS より古い日のファイルを消す。実行は1日1回まで。

    「最後に整理した日」をモジュールに覚えておくので、log のたびに
    ファイル一覧は見ない(プロセスが替われば、そのプロセスの初回に走る)。"""
    global _last_cleanup_date
    if _last_cleanup_date == today:
        return
    _last_cleanup_date = today
    try:
        for day, path in _list_files(base):
            if (today - day).days > RETENTION_DAYS:
                try:
                    path.unlink()
                except OSError:
                    pass
    except Exception:  # noqa: BLE001  整理の失敗で記録を止めない
        pass


# ---------------------------------------------------------------------------
# 読む側
# ---------------------------------------------------------------------------
def _iter_records(path: Path):
    """ファイルの記録を書かれた順に。壊れた行・辞書でない行は飛ばす。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _record_epoch(record: dict):
    """記録の epoch 秒。無ければ ts から復元する。どちらも無ければ None。"""
    value = record.get("epoch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    ts = record.get("ts")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).timestamp()
        except ValueError:
            return None
    return None


def tail(n, dir=None) -> list:
    """直近 n 件を古い順に返す。日をまたいで、足りなければ前日へさかのぼる。"""
    if n <= 0:
        return []
    base = _resolve_dir(dir)
    chunks = []
    remaining = n
    for _day, path in reversed(_list_files(base)):
        records = list(_iter_records(path))
        chunks.append(records[-remaining:])
        remaining -= len(records)
        if remaining <= 0:
            break
    out = []
    for chunk in reversed(chunks):
        out.extend(chunk)
    return out


def since(epoch, dir=None) -> list:
    """指定した epoch 秒以降の記録を古い順に返す。"""
    base = _resolve_dir(dir)
    out = []
    for _day, path in _list_files(base):
        for record in _iter_records(path):
            stamp = _record_epoch(record)
            if stamp is not None and stamp >= epoch:
                out.append(record)
    return out
