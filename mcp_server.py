# mcp_server.py
# 常駐している tray-tools を、Claude Code から道具として呼べるようにする MCP サーバー。
#
# 何をしているのか
# ----------------
# 既にある外部コマンドの口(名前付きパイプ + traytools_send.py)に、説明書を付けて
# 差し出すだけ。新しい口は開けない。**セキュリティ上、できることは増えていない** ——
# 名前付きパイプは今でもこのマシン上の何からでも書ける。増えるのは
#   - Claude が「そういう道具がある」と知っている状態
#   - 道具ごとに承認の粒度を分けられること(status は読むだけ、sleep は要確認)
# の2つ。
#
# 何を載せるか(main.py の _build_command_handlers と同じ線引き)
# -------------------------------------------------------------
# 載せてよいのは「常駐しているこのプロセスでなければできないこと」だけ。
# ファイル操作もアプリ起動も呼ぶ側が直接できるので載せない。口を通る危険だけが増える。
#
# capture がこの線引きの分かりやすい例で、mss のインスタンスを都度作ると COM/GC 衝突で
# 落ちるため(2026-08-28 に8回)、常駐が抱えているものを通すしか安全な手が無い。
#
# 逆に「画像そのものを返す」ことはしない。撮ったファイルのパスを返せば、読む側は普通に
# ファイルを開けばよく、数MBを base64 で流す必要がない。
#
# PySide6 を import しない
# ------------------------
# このサーバーは Claude Code の起動ごとに立つ。Qt の import だけで1秒近くかかるので、
# それがそのまま毎回の起動遅延になる。traytools_send.py と同じ制約を引き継ぐ
# (MCP の SDK も使わない。stdio 上の JSON-RPC は数十行で足りるうえ、venv に依存を
#  増やさずに済む)。
#
# 約束ごと(stdio トランスポート)
# ------------------------------
# - メッセージは JSON-RPC 2.0 を1行1件、改行区切り。**メッセージ内に改行を含めない**
# - **stdout には MCP のメッセージ以外を一切書かない。** 診断は stderr へ
# - UTF-8
#
# 常駐が起きていなければ
# ----------------------
# 起こしには行かず、「起きていない」と返す。traytools_send.send() は未起動なら本体を
# 起動して最大6秒待つが、それはあふｗから呼ぶときの作法で、道具として呼ばれる場面で
# 黙って常駐を起こすのは筋が違う。
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import traytools_send  # noqa: E402  (標準ライブラリだけ)

SERVER_NAME = "tray-tools"
SERVER_VERSION = "1.0.0"

# こちらが話せる版。クライアントが同じものを求めてきたらそれを返し、知らない版なら
# こちらの最新を返す(仕様どおり。相手が対応できなければ相手が切る)。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL = SUPPORTED_PROTOCOLS[0]

# 応答を待つ上限(秒)。pushover だけは外へ HTTP を投げるので長い。
TIMEOUTS = {"pushover": 25.0}
DEFAULT_TIMEOUT = 8.0

INSTRUCTIONS = (
    "常駐している tray-tools を操作します。"
    "長い作業を始める前に keep_awake で PC が寝るのを止め、終わったら必ず解除してください。"
    "作業前に status を見ると、スリープ予約が入っていないかが分かります。"
)


# ---------------------------------------------------------------------------
# 道具の一覧
# ---------------------------------------------------------------------------
# annotations はクライアントが承認の重さを決める材料になる。読むだけのものに
# readOnlyHint を付けておくと、都度確認せずに使える設定にしやすい。
def _tool(name, title, description, properties=None, required=None, **annotations):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            # 知らないキーを黙って受けると、綴りを間違えたまま気づけない。
            "additionalProperties": False,
        },
        "annotations": {"title": title, **annotations},
    }


TOOLS = [
    _tool(
        "status",
        "tray-tools の状態を見る",
        "スリープ抑止・スリープ予約・画面ミラー・付箋の枚数・マウスジグラーの状態を返す。"
        "長い作業を始める前に、寝る予約が入っていないかを確かめるのに使う。",
        readOnlyHint=True,
    ),
    _tool(
        "keep_awake",
        "スリープ抑止の入切",
        "PC が寝るのを止める／止めるのをやめる。長い作業を始める前に on にして、"
        "終わったら必ず off にすること。分を指定すればその時間で自動的に切れる。",
        {
            "state": {
                "type": "string",
                "enum": ["on", "off"],
                "description": "on で抑止を掛ける、off で解除する",
            },
            "minutes": {
                "type": "integer",
                "minimum": 1,
                "description": "on のときだけ有効。省略すると無期限",
            },
        },
        ["state"],
        idempotentHint=True,
    ),
    _tool(
        "sleep_pc",
        "スリープ／休止の予約",
        "指定した秒数のあとに PC を寝かせる。cancel で予約を取り消す。"
        "予約の時刻になってもすぐには寝ず、声を掛けてから少し待つ。"
        "**作業中に寝られると困るので、頼まれていないのに呼ばないこと。**",
        {
            "action": {
                "type": "string",
                "enum": ["sleep", "hibernate", "cancel"],
                "description": "sleep=スリープ / hibernate=休止状態 / cancel=予約の取り消し",
            },
            "seconds": {
                "type": "integer",
                "minimum": 0,
                "description": "何秒後に寝るか。省略すると猶予のあとすぐ",
            },
        },
        ["action"],
        destructiveHint=True,
    ),
    _tool(
        "notify",
        "トーストを出す",
        "画面にトーストを1枚出す。長い処理の節目を知らせる用。音は鳴らない。",
        {"message": {"type": "string", "description": "出す本文"}},
        ["message"],
    ),
    _tool(
        "beep",
        "音を鳴らす",
        "音を1つ鳴らす。トーストより強い合図で、画面を見ていなくても気づける。"
        "OS のサウンド設定に従うので、消していれば鳴らない。",
        {
            "kind": {
                "type": "string",
                "enum": ["ok", "done", "warn", "error", "ask"],
                "description": "ok=区切り / done=完了 / warn=警告 / error=失敗 / ask=確認待ち",
            }
        },
    ),
    _tool(
        "pushover",
        "スマホへ通知を送る",
        "Pushover でスマホへプッシュ通知を送る。席を外していても届く。"
        "トークンが登録されていなければ失敗する。",
        {
            "message": {"type": "string", "description": "本文"},
            "title": {"type": "string", "description": "見出し"},
            "priority": {
                "type": "integer",
                "minimum": -2,
                "maximum": 2,
                "description": "-2〜2。1 で高優先度、2 は確認するまで鳴り続ける",
            },
            "sound": {"type": "string", "description": "通知音の名前"},
        },
        ["message"],
        openWorldHint=True,
    ),
    _tool(
        "read_log",
        "操作ログを読む",
        "tray-tools の操作ログの末尾を読む。いつ何が実行されたかが分かる。",
        {
            "lines": {
                "type": "integer",
                "minimum": 1,
                "description": "読む行数。省略すると20行",
            }
        },
        readOnlyHint=True,
    ),
    _tool(
        "capture",
        "画面を撮る",
        "画面を1枚撮って保存し、そのパスを返す。返ってきたパスをファイルとして読めば"
        "中身を見られる。画面に出ているエラーや、いまの表示を確かめたいときに使う。",
        {
            "screen": {
                "type": "integer",
                "minimum": 1,
                "description": "撮る画面の番号(1始まり)。省略すると全画面をまとめて1枚",
            }
        },
    ),
    _tool(
        "bookmark",
        "フォルダブックマークを開く",
        "フォルダブックマークのピッカーを画面に出す。パスを渡すと、その場で登録できる。"
        "**画面に窓が出るので、頼まれていないのに呼ばないこと。**",
        {"path": {"type": "string", "description": "登録したいフォルダのパス"}},
    ),
]

TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


# ---------------------------------------------------------------------------
# 道具 → 既存コマンドへの翻訳
# ---------------------------------------------------------------------------
def _to_command(name: str, params: dict):
    """(コマンド名, 引数リスト) を返す。引数が不正なら ValueError。"""
    if name == "status":
        return "status", []

    if name == "keep_awake":
        state = params.get("state")
        if state not in ("on", "off"):
            raise ValueError("state は on か off です")
        args = [state]
        minutes = params.get("minutes")
        if minutes is not None:
            if state == "off":
                raise ValueError("off に分は指定できません")
            args.append(str(int(minutes)))
        return "keep-awake", args

    if name == "sleep_pc":
        action = params.get("action")
        if action == "cancel":
            return "sleep", ["cancel"]
        if action not in ("sleep", "hibernate"):
            raise ValueError("action は sleep / hibernate / cancel です")
        seconds = params.get("seconds")
        args = [str(int(seconds))] if seconds is not None else []
        if action == "hibernate":
            # 本体は「秒 → hibernate」の並びで読むが、秒を省いて hibernate だけでも
            # 受け付ける(_sleep_command が先頭の hibernate を見ている)。
            args.append("hibernate")
        return "sleep", args

    if name == "notify":
        message = (params.get("message") or "").strip()
        if not message:
            raise ValueError("message が空です")
        return "notify", [message]

    if name == "beep":
        return "beep", [params.get("kind") or "done"]

    if name == "pushover":
        message = (params.get("message") or "").strip()
        if not message:
            raise ValueError("message が空です")
        args = [message]
        for key, flag in (("title", "--title"), ("priority", "--priority"), ("sound", "--sound")):
            value = params.get(key)
            if value is not None:
                args += [flag, str(value)]
        return "pushover", args

    if name == "read_log":
        lines = params.get("lines")
        return "log", ([str(int(lines))] if lines is not None else [])

    if name == "capture":
        screen = params.get("screen")
        return "capture", ([str(int(screen))] if screen is not None else [])

    if name == "bookmark":
        path = (params.get("path") or "").strip()
        return "bookmark", ([path] if path else [])

    raise ValueError(f"知らない道具です: {name}")


def call_tool(name: str, params: dict) -> dict:
    """道具を1つ実行して、MCP のツール結果を返す。

    失敗はプロトコルのエラーにせず、isError を立てた結果として返す。そのほうが
    Claude 側が読んで対処できる(プロトコルのエラーは会話に載らない)。"""
    try:
        command, args = _to_command(name, params or {})
    except (ValueError, TypeError) as e:
        return _result(f"引数が不正です: {e}", is_error=True)

    timeout = TIMEOUTS.get(command, DEFAULT_TIMEOUT)
    try:
        response = traytools_send._exchange(command, args, timeout)
    except FileNotFoundError:
        return _result(
            "tray-tools が起動していません。"
            "トレイに常駐していないので、この道具は使えません。",
            is_error=True,
        )
    except OSError as e:
        return _result(f"tray-tools へ送れませんでした: {e}", is_error=True)

    if response is None:
        # 届いてはいるが返事が来なかった。失敗とは言い切れない。
        return _result("送りましたが、応答がありませんでした。")
    return _result(response, is_error=response.startswith("ERR"))


def _result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
def _negotiate(requested):
    """相手が求めた版がこちらで話せるならそれを、話せないならこちらの最新を返す。"""
    return requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL


def handle(message: dict):
    """1件処理して、返すべき応答(または返さないなら None)を返す。"""
    method = message.get("method")
    message_id = message.get("id")
    # id が無いものは通知。応答してはいけない。
    is_notification = "id" not in message

    if method == "initialize":
        params = message.get("params") or {}
        return _ok(
            message_id,
            {
                "protocolVersion": _negotiate(params.get("protocolVersion")),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        )

    if is_notification:
        return None  # notifications/initialized など。黙って受ける

    if method == "ping":
        return _ok(message_id, {})

    if method == "tools/list":
        return _ok(message_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if name not in TOOLS_BY_NAME:
            return _error(message_id, -32602, f"知らない道具です: {name}")
        return _ok(message_id, call_tool(name, params.get("arguments") or {}))

    return _error(message_id, -32601, f"未対応のメソッドです: {method}")


def _ok(message_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _write(out, payload: dict) -> None:
    """1行1件で書き出す。**改行を含めないこと**が仕様なので separators で潰す。"""
    out.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    out.write("\n")
    out.flush()


def serve(stdin=None, stdout=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as e:
            # id が分からないので null で返すしかない(JSON-RPC の parse error)。
            _write(stdout, _error(None, -32700, f"JSON として読めません: {e}"))
            continue
        if not isinstance(message, dict):
            _write(stdout, _error(None, -32600, "オブジェクトではありません"))
            continue
        try:
            response = handle(message)
        except Exception as e:  # noqa: BLE001  1件の失敗でサーバーごと落とさない
            print(f"[tray-tools mcp] 想定外の失敗: {e}", file=sys.stderr)
            response = _error(message.get("id"), -32603, f"内部エラー: {e}")
        if response is not None:
            _write(stdout, response)
    return 0


def main() -> int:
    # stdout は MCP 専用。日本語を確実に通すため UTF-8 に固定し、改行の変換も止める
    # (Windows の既定では \n が \r\n になり、行の区切りが崩れうる)。
    for stream, encoding in ((sys.stdout, "utf-8"), (sys.stdin, "utf-8"), (sys.stderr, "utf-8")):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding=encoding, newline="", errors="replace")
            except (OSError, ValueError):
                pass
    return serve()


if __name__ == "__main__":
    sys.exit(main())
