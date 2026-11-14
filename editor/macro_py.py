# editor/macro_py.py
# EDDy のマクロの Python 形式。マクロの正本は従来どおり「命令の列」
# (editor/macros.py)だが、利用者が読み書きする形は Python に寄せる
# (Nagato の決定、2026-11-14)。このファイルは Qt に依存しない。
#
# できること:
#   commands_to_python(commands, name) … 命令の列を Python の
#     ソースへ変換する。API は ed という1つの名前だけで、命令名と
#     同じ名前の関数を呼ぶ形にする(下の API_SPEC が表面の正本)。
#   validate_python(source) … 構文と、書き方の約束(下記)に反して
#     いないかを確かめ、問題があれば理由の文を返す(無ければ None)。
#   PythonStepper … ソースを文(statement)単位で実行するセッション。
#     通し実行と1文ずつのステップ実行の両方に使う(窓側が編集
#     ブロックで包む)。
#
# 逆変換(Python → 命令の列)は作らない。Python は命令の列より表現
# 力が広く(条件分岐・繰り返し・変数が書ける)、戻せる保証が無い
# ため。登録マクロは、命令の列の形と Python の形のどちらでも
# 持てる(editor/macros.py の sanitize_registered が両方受ける)。
#
# 実行の置き場所(2026-11-14 のレビュー req-20261114-08 で変更):
#   Python マクロは「利用者が自分で書いたプログラム」として扱い、
#   EDDy 本体とは別のプロセス(子プロセス)で実行する。本体の中で
#   exec する作りはやめた(ジェネレータの gi_frame → f_back を
#   たどると本体のモジュールに届き、本体ごと落とせたため)。
#   子プロセスとの間は UTF-8 の JSON 行プロトコルで、ed の呼び
#   出しは IPC で親へ届き、親が文書へ適用する。print の出力も
#   IPC で戻す。子の中で何をしても(os に届く・重い計算・無限
#   ループ・子が落ちる)本体は無事で、stop() で子を終了できる。
#   rxfind の子プロセス方式が前例(ただしあちらは1回ごとの実行、
#   こちらはセッションの間ずっと生きる子と往復する)。
#   親の受け口側でも、通してよい名前を API_SPEC と QUERY_NAMES
#   に限り、呼び出し回数を数えて上限を超えたらセッションを
#   止める(子の数えがどうあれ、親の側でも止められるように。
#   req-20261114-09 の指摘)。
#
# 書き方の約束(安全のためではなく、EDDy のマクロとして書く
# ための決まり。実態は利用者のプログラムなので、これを破る
# 方法が残っていても本体は別プロセスで守られる):
#   - 名前として渡すのは ed(API)だけ。builtins は必要最小限の組
#     だけに絞る。open / eval / exec / compile / __import__ /
#     getattr などは渡さない。
#   - import は書けない(検証で落とす)。
#   - 属性は「_」で始まるものに触れない(検証で落とす)。
#   - ed の命令の呼び出し回数と、実行した行数に上限を設ける
#     (暴走したときに、子が黙って回り続けないため)。
#   ファイルや OS へ直接触れる口は API に含めない(ed の読み取りは
#   今の文書の内容だけ)。
import ast
import json
import queue
import subprocess
import sys
import threading
import time

# 引数が必須であることの印(API_SPEC の既定値の代わりに入れる)。
REQUIRED = object()

# API の表面(正本)。命令名 → 引数の並び [(引数名, 既定値), ...]。
# 並びの順が、位置引数で渡すときの順と、生成コードでキーワードを
# 並べる順になる。既定値と同値の引数は生成コードから落とす
# (読みやすさのため。実行時の補完は窓側の橋が行う)。
API_SPEC = {
    "insert_text": [("text", REQUIRED)],
    "delete_backward": [("count", 1)],
    "delete_forward": [("count", 1)],
    "delete_selection": [],
    "cut_to_end": [],
    "delete_line": [],
    "find_next": [("text", REQUIRED), ("case", False), ("regex", False)],
    "find_prev": [("text", REQUIRED), ("case", False), ("regex", False)],
    "replace_next": [("text", REQUIRED), ("repl", REQUIRED),
                     ("case", False), ("regex", False)],
    "replace_all": [("text", REQUIRED), ("repl", REQUIRED),
                    ("case", False), ("regex", False)],
    "move_line_start": [("extend", False)],
    "move_line_end": [("extend", False)],
    "goto_line": [("line", REQUIRED)],
    "next_edited_line": [],
    "prev_edited_line": [],
    "toggle_bookmark": [],
    "next_bookmark": [],
    "prev_bookmark": [],
    "move_line_up": [("count", 1), ("extend", False)],
    "move_line_down": [("count", 1), ("extend", False)],
    "move_char_left": [("count", 1), ("extend", False)],
    "move_char_right": [("count", 1), ("extend", False)],
    "move_word_left": [("count", 1), ("extend", False)],
    "move_word_right": [("count", 1), ("extend", False)],
    "move_document_start": [("extend", False)],
    "move_document_end": [("extend", False)],
    "move_page_up": [("count", 1), ("extend", False)],
    "move_page_down": [("count", 1), ("extend", False)],
    "select_all": [],
    "sort_lines": [("order", "asc")],
    "remove_duplicate_lines": [],
    "reformat_text": [],
    "convert_halfwidth": [],
    "convert_fullwidth": [],
    "convert_lower": [],
    "convert_upper": [],
    "convert_tab_to_space": [],
    "convert_space_to_tab": [],
    "convert_lead_half_to_full": [],
    "convert_lead_full_to_half": [],
}

# 読み取り専用の問い合わせ(命令ではない。記録もされない)。
# AI に書いてもらうときに、文書の状態で条件を分けられるように。
QUERY_NAMES = ("text", "selected_text", "cursor_line", "line_count")

# 親の受け口が通してよい名前(命令 + 問い合わせ)。子がこれ以外の
# 名前を送ってきても、実体へは渡さずエラーとして返す(受け口で
# getattr を素通しすると、API の表面に無い名前まで届いてしまう
# ため。req-20261114-09 の指摘)。
ALLOWED_CALL_NAMES = frozenset(API_SPEC) | frozenset(QUERY_NAMES)

# 生成コードで位置引数にする命令(先頭の引数だけを裸で書く)。
_POSITIONAL = {"insert_text", "goto_line", "sort_lines"}

# 子プロセスの中で実行に渡す builtins の最小限の組の名前(print は
# 子側で IPC へ流す自前の関数に差し替える)。本体の守りは別
# プロセスであることであって、この絞り込みは「EDDy のマクロ
# として書くときの約束」をそろえるためのもの。
SAFE_BUILTIN_NAMES = (
    "range", "len", "str", "int", "float", "bool", "list", "dict",
    "tuple", "set", "enumerate", "zip", "min", "max", "sum", "abs",
    "sorted", "reversed",
)

# 暴走止めの上限。ed の呼び出し回数と、実行した行イベントの数
# (子側で数える。値はセッション開始時に子へ渡す)。
API_CALL_LIMIT = 100_000
LINE_EVENT_LIMIT = 5_000_000

FILENAME = "<eddymacro>"

# 子プロセスがソースを受けて ready を返すまでの待ち上限(秒)。
_INIT_TIMEOUT_SEC = 15.0


class PythonMacroError(Exception):
    """マクロの Python の検証・実行の失敗。lineno はソースの行番号
    (1始まり。分からなければ None)。"""

    def __init__(self, message, lineno=None):
        super().__init__(message)
        self.lineno = lineno


# ---------------------------------------------------------------
# 命令の列 → Python
# ---------------------------------------------------------------
def commands_to_python(commands, name="") -> str:
    """命令の列を Python のソースにする。

    先頭にコメント(マクロの名前と、EDDy のマクロであること)を
    置く。各命令は ed.命令名(...) の1行にする。引数は、既定値と
    同じものは落とし、残りはキーワードで書く(insert_text /
    goto_line / sort_lines の先頭の引数だけ位置引数)。値の字面は
    repr で作る(日本語はそのまま出る。引用符や改行はエスケープ
    されるので、Copilot などへ貼っても崩れない)。"""
    lines = []
    if name:
        lines.append(f"# EDDy マクロ「{name}」")
    else:
        lines.append("# EDDy マクロ")
    lines.append("# EDDy（テキストエディタ）の中でだけ動く Python です。"
                 "ed の命令が編集操作になります。")
    for entry in commands:
        lines.append(_command_to_line(entry))
    return "\n".join(lines) + "\n"


def _command_to_line(entry):
    cmd = entry["cmd"]
    spec = API_SPEC.get(cmd)
    if spec is None:
        # 知らない命令はコメントにして残す(黙って落とすと、書いた
        # つもりの手順が欠ける。実行はされない)。
        return f"# （対応する命令がありません: {cmd}）"
    args = dict(entry.get("args") or {})
    parts = []
    for index, (key, default) in enumerate(spec):
        if key not in args:
            continue
        value = args[key]
        if default is not REQUIRED and value == default:
            continue
        if index == 0 and cmd in _POSITIONAL:
            parts.append(repr(value))
        else:
            parts.append(f"{key}={value!r}")
    return f"ed.{cmd}({', '.join(parts)})"


# ---------------------------------------------------------------
# 検証(構文 + 書き方の約束)
# ---------------------------------------------------------------
def validate_python(source):
    """ソースを検証し、問題があれば理由の文を、無ければ None を
    返す。構文エラーと、import・「_」で始まる属性の使用を落とす
    (理由は行番号つき)。"""
    try:
        tree = ast.parse(str(source), FILENAME)
    except SyntaxError as e:
        return f"{e.lineno} 行目: 構文が読めません（{e.msg}）"
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return (f"{node.lineno} 行目: import は使えません"
                    "（マクロで使えるのは ed の命令だけです）")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return (f"{node.lineno} 行目: 「_」で始まる属性"
                    f"（{node.attr}）には触れません")
    return None



# ---------------------------------------------------------------
# 子プロセス(マクロの実行側)
# ---------------------------------------------------------------
# rxfind と同じく、子は editor パッケージを import しない(パスの
# 前提を作らないため)。検証と文単位の実行は、親側と同じ意味の
# 処理を標準ライブラリだけで書き直したもの。
#
# 親 → 子: {"type": "init", "source", "api_limit", "line_limit"}
#          {"type": "step"} / {"type": "run_all"} /
#          {"type": "run_until", "breakpoints": [行番号, ...]} /
#          {"type": "seek", "index"} / {"type": "quit"}
#          {"type": "result", "id", "value"} /
#          {"type": "call_error", "id", "exc", "message"}
# 子 → 親: {"type": "ready", "total", "statements": [[開始行, 終了行]]}
#          {"type": "call", "id", "name", "args", "kwargs"}
#          {"type": "print", "text"}
#          {"type": "stepped", "lineno", "position", "done"} /
#          {"type": "finished", "position"} /
#          {"type": "paused", "position"} /
#          {"type": "sought", "position"} /
#          {"type": "error", "message", "lineno", "position"?}
# 入出力はバイト列のまま読み書きし、UTF-8 と決め打ちする(rxfind
# と同じ理由。OS 既定の文字コードに任せると日本語が化ける)。
_CHILD_CODE = r"""
import ast, builtins, json, sys

FILENAME = "<eddymacro>"
SAFE_NAMES = ("range", "len", "str", "int", "float", "bool", "list",
              "dict", "tuple", "set", "enumerate", "zip", "min",
              "max", "sum", "abs", "sorted", "reversed")


class Fail(Exception):
    # 親へ error として返す失敗(親で組み立て直さず、この文を使う)。
    def __init__(self, message, lineno):
        super().__init__(message)
        self.message = message
        self.lineno = lineno


class LimitError(Exception):
    pass


class RemoteError(Exception):
    # 親側で ed の実体が上げた例外(型名と文だけを運ぶ)。
    def __init__(self, exc, message):
        super().__init__(message)
        self.exc = exc
        self.message = message


def send(msg):
    sys.stdout.buffer.write(
        json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def read_msg():
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    return json.loads(line.decode("utf-8"))


_state = {"api_calls": 0, "line_events": 0, "next_id": 0,
          "api_limit": 100000, "line_limit": 5000000}


def rpc_call(name, args, kwargs):
    _state["api_calls"] += 1
    if _state["api_calls"] > _state["api_limit"]:
        raise LimitError(
            "ed の呼び出し回数が上限を超えたため止めました"
            "（無限ループの疑い）")
    _state["next_id"] += 1
    cid = _state["next_id"]
    send({"type": "call", "id": cid, "name": name,
          "args": args, "kwargs": kwargs})
    while True:
        msg = read_msg()
        if msg.get("type") == "result" and msg.get("id") == cid:
            return msg.get("value")
        if msg.get("type") == "call_error" and msg.get("id") == cid:
            raise RemoteError(msg.get("exc") or "Error",
                              msg.get("message") or "")


class Ed:
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            return rpc_call(name, list(args), kwargs)

        return call


def macro_print(*args, **_kwargs):
    send({"type": "print",
          "text": " ".join(str(arg) for arg in args)})


def validate(source):
    try:
        tree = ast.parse(source, FILENAME)
    except SyntaxError as e:
        return (f"{e.lineno} 行目: 構文が読めません（{e.msg}）",
                e.lineno)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return (f"{node.lineno} 行目: import は使えません"
                    "（マクロで使えるのは ed の命令だけです）",
                    node.lineno)
        if isinstance(node, ast.Attribute) and \
                node.attr.startswith("_"):
            return (f"{node.lineno} 行目: 「_」で始まる属性"
                    f"（{node.attr}）には触れません", node.lineno)
    return None


def exec_in_child(code, namespace):
    # マクロ自身の行だけを行数上限の対象にする(子の内部は数えない)。
    def tracer(frame, event, _arg):
        if event == "line" and \
                frame.f_code.co_filename == FILENAME:
            _state["line_events"] += 1
            if _state["line_events"] > _state["line_limit"]:
                raise LimitError(
                    "実行した行数が上限を超えたため止めました"
                    "（無限ループの疑い）")
        return tracer

    previous = sys.gettrace()
    try:
        sys.settrace(tracer)
        try:
            exec(code, namespace)
        finally:
            sys.settrace(previous)
    except LimitError:
        raise


def main():
    init = read_msg()
    if init.get("type") != "init":
        return
    _state["api_limit"] = int(init.get("api_limit")
                              or _state["api_limit"])
    _state["line_limit"] = int(init.get("line_limit")
                               or _state["line_limit"])
    source = str(init.get("source") or "")
    problem = validate(source)
    if problem is not None:
        send({"type": "error", "message": problem[0],
              "lineno": problem[1]})
        return
    tree = ast.parse(source, FILENAME)
    statements = list(tree.body)
    safe = {name: getattr(builtins, name) for name in SAFE_NAMES}
    safe["print"] = macro_print
    namespace = {"__builtins__": safe, "ed": Ed()}
    position = [0]

    def run_one():
        node = statements[position[0]]
        module = ast.Module(body=[node], type_ignores=[])
        try:
            code = compile(module, FILENAME, "exec")
        except SyntaxError as e:
            raise Fail(f"{node.lineno} 行目: 構文が読めません"
                       f"（{e.msg}）", node.lineno)
        try:
            exec_in_child(code, namespace)
        except LimitError as e:
            # 上限の文は行番号を付けず、そのまま親へ返す。
            raise Fail(str(e), node.lineno)
        except RemoteError as e:
            raise Fail(f"{node.lineno} 行目: {e.exc}: {e.message}",
                       node.lineno)
        except Fail:
            raise
        except Exception as e:  # 利用者のコードの失敗
            raise Fail(f"{node.lineno} 行目: "
                       f"{type(e).__name__}: {e}", node.lineno)
        position[0] += 1
        return node.lineno

    send({"type": "ready", "total": len(statements),
          "statements": [[node.lineno, node.end_lineno or node.lineno]
                          for node in statements]})
    while True:
        msg = read_msg()
        kind = msg.get("type")
        if kind == "quit":
            return
        if kind == "step":
            if position[0] >= len(statements):
                send({"type": "stepped", "lineno": None,
                      "position": position[0], "done": True})
                continue
            try:
                lineno = run_one()
            except Fail as f:
                send({"type": "error", "message": f.message,
                      "lineno": f.lineno, "position": position[0]})
                continue
            send({"type": "stepped", "lineno": lineno,
                  "position": position[0],
                  "done": position[0] >= len(statements)})
        elif kind == "run_all":
            try:
                while position[0] < len(statements):
                    run_one()
            except Fail as f:
                send({"type": "error", "message": f.message,
                      "lineno": f.lineno, "position": position[0]})
                continue
            send({"type": "finished", "position": position[0]})
        elif kind == "run_until":
            marks = set()
            for value in msg.get("breakpoints") or []:
                try:
                    marks.add(int(value))
                except (TypeError, ValueError):
                    continue
            start = position[0]
            paused = False
            try:
                while position[0] < len(statements):
                    node = statements[position[0]]
                    if position[0] != start and \
                            node.lineno in marks:
                        # 印のある文の手前で止まる (その文はまだ
                        # 実行しない)。ただし、止まっている文
                        # そのものは「続き」では実行する (開始
                        # 位置の文は印があっても実行する。同じ
                        # 場所で止まり続けないため)。
                        paused = True
                        break
                    run_one()
            except Fail as f:
                send({"type": "error", "message": f.message,
                      "lineno": f.lineno, "position": position[0]})
                continue
            if paused:
                send({"type": "paused", "position": position[0]})
            else:
                send({"type": "finished", "position": position[0]})
        elif kind == "seek":
            try:
                index = int(msg.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            position[0] = max(0, min(index, len(statements)))
            send({"type": "sought", "position": position[0]})


try:
    main()
except EOFError:
    pass
"""


def statement_index_for_line(lines, lineno):
    """行番号(1始まり)が属する文の番号(0始まり)を返す。

    lines は PythonStepper.statement_lines() の (開始行, 終了行)
    の並び。文の途中の行ならその文、文と文の間の行(空行や
    コメントだけの行)なら次の文を返し、最後の文より後ろの行
    なら None を返す。"""
    for index, (_start, end) in enumerate(lines):
        if lineno <= end:
            return index
    return None


def statement_lines_for_source(source):
    """ソースを文に分けた (開始行, 終了行) の並びを返す。
    構文が読めなければ None。

    実行セッションを作る前に、ブレークポイントの印を文の
    開始行へ寄せるために使う(切り分けはセッション側の
    statement_lines と同じ、トップレベルの文の並び)。"""
    try:
        tree = ast.parse(str(source), FILENAME)
    except (SyntaxError, ValueError):
        return None
    return [(node.lineno, node.end_lineno or node.lineno)
            for node in tree.body]


def snap_breakpoints(lines, marks):
    """ブレークポイントの行番号を、文の開始行へ寄せた並びを
    返す。

    lines は (開始行, 終了行) の並び。文の範囲(開始行〜終了行)
    の中にある印は、その文の開始行になる。どの文の範囲にも
    入らない印(空行・文の間のコメント行)は落とす。実行側
    (run_until)は文の開始行しか見ないので、渡す前にここを
    通す。"""
    starts = set()
    for mark in marks:
        for start, end in lines:
            if start <= mark <= end:
                starts.add(start)
                break
    return sorted(starts)


# ---------------------------------------------------------------
# 実行セッション(親側。実体は子プロセス)
# ---------------------------------------------------------------
class PythonStepper:
    """Python マクロを文(statement)単位で実行するセッション。

    実体は子プロセス(上の _CHILD_CODE)で、このクラスは親側の
    受け口。api は ed の実体(属性が API の関数であるオブジェ
    クト)で、子からの呼び出しを親が受けて適用する。1文が複数
    行にまたがるとき(if 文や関数定義など)は、その文全体を
    1ステップとして実行する。

    状態: position(次に実行する文の番号)、done、stopped
    (stop() で止めた)、error(PythonMacroError か None)。
    step() は実行した文の開始行番号を返し、もう文が無ければ
    None(止めたときも None で、error には入らない)。実行で
    例外が出たら error に入れて done にし、その例外を
    PythonMacroError に包んで上げる(呼び側は error を見ても
    よい)。print の出力は output に溜まる。
    run_until(行番号の集まり) はブレークポイントの手前で止まる
    通し実行、seek(文の番号) は実行せずに位置だけを移す
    (ペイン側の「カーソル行から実行」「クリックで実行位置を
    移す」用)。

    pump は、子の応答を待つ間に繰り返し呼ばれる口(窓側が
    イベント処理を渡す。画面を固まらせず、停止を受け付ける
    ため)。スレッドから stop() を呼んでもよい。"""

    def __init__(self, source, api, pump=None):
        error = validate_python(source)
        if error is not None:
            raise PythonMacroError(error)
        self._api = api
        self.pump = pump
        self._calls_since_pump = 0
        self.output = []          # print の受け取り先
        self.position = 0
        # 直近の seek で前へ飛ばした文の数 (後ろへ戻したときは
        # 0)。飛ばした文が作るはずだった変数が無いことを、窓側
        # が知らせるために持つ。
        self.seek_skipped = 0
        self.error = None
        self.stopped = False
        self._total = 0
        self._statement_lines = []
        self._queue = queue.Queue()
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()
        self._closed = False
        # 親側でも呼び出し回数を数える(上限は子へ渡す値と同じ。
        # 構築の時点で読み、テストの定数の差し替えにも追随する)。
        self._api_limit = API_CALL_LIMIT
        self._api_calls = 0
        self._limit_error = None
        popen_kwargs = {}
        if sys.platform == "win32":
            # 本体が pythonw で動くとき、子にコンソール窓を
            # 出さないための指定(Windows のみ。他の環境では
            # 何もしない)。
            popen_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-c", _CHILD_CODE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, **popen_kwargs)
        except OSError as e:
            raise PythonMacroError(
                f"マクロを実行できませんでした（{e}）") from None
        self._reader = threading.Thread(
            target=self._read_loop, name="eddymacro-reader",
            daemon=True)
        self._reader.start()
        try:
            self._send({"type": "init", "source": str(source),
                        "api_limit": self._api_limit,
                        "line_limit": LINE_EVENT_LIMIT})
            reply = self._serve({"ready", "error"},
                                timeout=_INIT_TIMEOUT_SEC)
        except PythonMacroError:
            self.close()
            raise
        if reply is None:  # 開始前に止められた
            self.close()
            raise PythonMacroError("マクロの実行を止めました")
        if reply.get("type") == "error":
            message = reply.get("message") or "マクロを実行できません"
            lineno = reply.get("lineno")
            self.close()
            raise PythonMacroError(message, lineno)
        self._total = int(reply.get("total") or 0)
        self._statement_lines = [
            (int(pair[0]), int(pair[1]))
            for pair in (reply.get("statements") or [])]

    # ---------------------------------------------------------------
    # 子との往復
    # ---------------------------------------------------------------
    def _read_loop(self):
        try:
            for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._queue.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(None)   # 終わり(EOF)の印

    def _send(self, msg):
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        data = (json.dumps(msg, ensure_ascii=False).encode("utf-8")
                + b"\n")
        with self._send_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                pass    # 子が既に終わっている。受け側で検出する

    def _answer_call(self, msg):
        """子からの ed の呼び出しを、親の実体へ適用して返す。

        通してよい名前は API_SPEC と QUERY_NAMES だけ。それ以外は
        実体へ渡さず、無い名前としてエラーで返す。呼び出し回数は
        親でも数え、上限を超えたらセッションを止める(子の側の
        上限と同じ値。子が数えを誤魔化す形になっていても、親で
        止められる)。"""
        cid = msg.get("id")
        name = str(msg.get("name") or "")
        if name not in ALLOWED_CALL_NAMES:
            self._send({"type": "call_error", "id": cid,
                        "exc": "AttributeError",
                        "message": f"ed に {name} はありません"})
            return
        self._api_calls += 1
        if self._api_calls > self._api_limit:
            self.error = PythonMacroError(
                "ed の呼び出し回数が上限を超えたため止めました"
                "（無限ループの疑い）")
            self._limit_error = self.error
            self.stop()
            return
        fn = getattr(self._api, name, None)
        if fn is None or not callable(fn):
            self._send({"type": "call_error", "id": cid,
                        "exc": "AttributeError",
                        "message": f"ed に {name} はありません"})
            return
        args = msg.get("args") or []
        kwargs = msg.get("kwargs") or {}
        try:
            value = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001  型名と文だけ子へ返す
            self._send({"type": "call_error", "id": cid,
                        "exc": type(e).__name__, "message": str(e)})
            return
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            value = None    # JSON で運べない戻り値は None に落とす
        self._send({"type": "result", "id": cid, "value": value})

    def _serve(self, kinds, timeout=None):
        """kinds のどれかの消息が来るまで、call / print を捌く。

        その消息を返す。stop() で止められたら None(呼び側が
        stopped を立てる)。子が消息なく終わったら
        PythonMacroError。timeout(秒)を過ぎても同じ。"""
        deadline = None if timeout is None \
            else time.monotonic() + timeout
        while True:
            if self._stop_event.is_set():
                self._kill()
                if self._limit_error is not None:
                    raise self._limit_error
                return None
            wait = 0.01
            if deadline is not None:
                rest = deadline - time.monotonic()
                if rest <= 0:
                    raise PythonMacroError(
                        "マクロの実行が時間内に始まりませんでした")
                wait = min(wait, rest)
            try:
                msg = self._queue.get(timeout=wait)
            except queue.Empty:
                if self.pump is not None:
                    self.pump()
                continue
            if msg is None:     # 子が終わった
                if self._limit_error is not None:
                    raise self._limit_error
                if self._stop_event.is_set():
                    return None
                raise PythonMacroError(
                    "マクロの実行が途中で終わりました"
                    "（実行側のプロセスが終了しました）")
            kind = msg.get("type")
            if kind == "call":
                self._answer_call(msg)
                # 呼び出しが続く間も画面の更新が進むよう、一定
                # 回数ごとにイベントを回す (待ちが空のときだけ
                # 回すと、ループを含む文の実行中は画面が止まって
                # 見える)。
                self._calls_since_pump += 1
                if (self.pump is not None
                        and self._calls_since_pump >= 16):
                    self._calls_since_pump = 0
                    self.pump()
                continue
            if kind == "print":
                self.output.append(str(msg.get("text") or ""))
                continue
            if kind in kinds:
                return msg
            # 想定外の消息は読み捨てる。

    def _kill(self):
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # ---------------------------------------------------------------
    # 状態
    # ---------------------------------------------------------------
    @property
    def done(self):
        return (self.error is not None or self.stopped
                or self.position >= self._total)

    @property
    def total(self):
        return self._total

    def next_lineno(self):
        """次に実行する文の開始行番号。無ければ None。"""
        if self.done or self.position >= len(self._statement_lines):
            return None
        return self._statement_lines[self.position][0]

    def statement_lines(self):
        """(開始行, 終了行) の並び(窓側の現在行表示用)。"""
        return list(self._statement_lines)

    # ---------------------------------------------------------------
    # 実行
    # ---------------------------------------------------------------
    def step(self):
        """次の1文を実行する。実行した文の開始行番号を返す。"""
        if self.done:
            return None
        self._send({"type": "step"})
        reply = self._serve({"stepped", "error"})
        if reply is None:
            self.stopped = True
            return None
        if reply.get("type") == "error":
            self.error = PythonMacroError(
                reply.get("message") or "マクロの実行に失敗しました",
                reply.get("lineno"))
            raise self.error
        self.position = int(reply.get("position") or 0)
        return reply.get("lineno")

    def run_all(self):
        """残りの文を全部実行する。失敗したらそこで止まる。"""
        if self.done:
            return
        self._send({"type": "run_all"})
        reply = self._serve({"finished", "error"})
        if reply is None:
            self.stopped = True
            return
        if reply.get("type") == "error":
            self.error = PythonMacroError(
                reply.get("message") or "マクロの実行に失敗しました",
                reply.get("lineno"))
            raise self.error
        self.position = int(reply.get("position") or 0)

    def run_until(self, breakpoints=()):
        """次のブレークポイントの手前か、最後まで実行する。

        breakpoints はソースの行番号(1始まり)の集まり。印のある
        文の手前で止まったら False、最後まで行ったら True を
        返す。ただし、実行を始める位置の文そのものに印があって
        も、その文は実行して先へ進む(ブレークポイントで止まった
        あとに「続き」を押したとき、同じ場所で止まり続けない
        ため)。失敗したら run_all と同じく例外を上げる。止め
        られたときは stopped が立ち、False を返す。"""
        if self.done:
            return self.error is None and self.position >= self._total
        marks = sorted({int(b) for b in breakpoints})
        self._send({"type": "run_until", "breakpoints": marks})
        reply = self._serve({"finished", "paused", "error"})
        if reply is None:
            self.stopped = True
            return False
        if reply.get("type") == "error":
            self.error = PythonMacroError(
                reply.get("message") or "マクロの実行に失敗しました",
                reply.get("lineno"))
            raise self.error
        self.position = int(reply.get("position") or 0)
        return reply.get("type") == "finished"

    def seek(self, index):
        """次に実行する文を、index 番目(0始まり)の文へ移す。

        実行はしない。移せたら True。エラー・停止で終わっている
        セッションや、閉じたセッションでは移せない(False)。
        index は文の数の範囲に丸める(末尾を指定すると、次に
        実行する文は無くなる)。前へ飛んだときは、飛ばした文の
        数を seek_skipped に残す。"""
        if self.error is not None or self.stopped or self._closed:
            return False
        index = max(0, min(int(index), self._total))
        before = self.position
        self._send({"type": "seek", "index": index})
        reply = self._serve({"sought"})
        if reply is None:
            self.stopped = True
            return False
        self.position = int(reply.get("position") or 0)
        self.seek_skipped = max(0, self.position - before)
        return True

    def stop(self):
        """実行を止める(子プロセスを終了する)。

        無限ループの最中でも効く(子ごと落とすため)。実行待ちの
        step / run_all は、例外ではなく stopped を立てて戻る。"""
        self._stop_event.set()
        self._kill()

    def close(self):
        """セッションを閉じ、子プロセスを残さない。"""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            self._send({"type": "quit"})
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._kill()
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass


def api_names():
    """ed の後ろに書ける名前の一覧(命令 + 問い合わせ)。"""
    return list(API_SPEC) + list(QUERY_NAMES)
