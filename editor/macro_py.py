# editor/macro_py.py
# EDDy のマクロの Python 形式。マクロの正本は従来どおり「命令の列」
# (editor/macros.py)だが、利用者が読み書きする形は Python に寄せる
# (Nagato の決定、2026-11-14)。このファイルは Qt に依存しない。
#
# できること:
#   commands_to_python(commands, name) … 命令の列を Python の
#     ソースへ変換する。API は ed という1つの名前だけで、命令名と
#     同じ名前の関数を呼ぶ形にする(下の API_SPEC が表面の正本)。
#   validate_python(source) … 構文と、実行環境の約束(下記)に反して
#     いないかを確かめ、問題があれば理由の文を返す(無ければ None)。
#   PythonStepper … ソースを文(statement)単位で実行する。通し
#     実行と1文ずつのステップ実行の両方に使う(窓側のセッションが
#     編集ブロックで包む)。
#
# 逆変換(Python → 命令の列)は作らない。Python は命令の列より表現
# 力が広く(条件分岐・繰り返し・変数が書ける)、戻せる保証が無い
# ため。登録マクロは、命令の列の形と Python の形のどちらでも
# 持てる(editor/macros.py の sanitize_registered が両方受ける)。
#
# 実行環境の約束(サンドボックス寄り):
#   - 名前として渡すのは ed(API)だけ。builtins は必要最小限の組
#     (SAFE_BUILTINS)だけに絞る。open / eval / exec / compile /
#     __import__ / getattr などは渡さない。
#   - import は書けない(検証で落とす)。
#   - 属性は「_」で始まるものに触れない(検証で落とす)。
#     __class__ などをたどる抜け道を塞ぐため。
#   - ed の命令の呼び出し回数と、実行した行数に上限を設ける
#     (無限ループで窓が固まらないため。純粋な計算だけの無限
#     ループは行数の上限で止める)。
#   ファイルや OS へ直接触れる口は API に含めない(ed の読み取りは
#   今の文書の内容だけ)。
import ast
import sys

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
}

# 読み取り専用の問い合わせ(命令ではない。記録もされない)。
# AI に書いてもらうときに、文書の状態で条件を分けられるように。
QUERY_NAMES = ("text", "selected_text", "cursor_line", "line_count")

# 生成コードで位置引数にする命令(先頭の引数だけを裸で書く)。
_POSITIONAL = {"insert_text", "goto_line", "sort_lines"}

# 実行に渡す builtins の最小限の組。print はセッション側が受け
# 取れるよう、Stepper が自前の関数に差し替える。
SAFE_BUILTINS = {
    "range": range, "len": len, "str": str, "int": int,
    "float": float, "bool": bool, "list": list, "dict": dict,
    "tuple": tuple, "set": set, "enumerate": enumerate, "zip": zip,
    "min": min, "max": max, "sum": sum, "abs": abs,
    "sorted": sorted, "reversed": reversed,
}

# 暴走止めの上限。ed の呼び出し回数と、実行した行イベントの数。
API_CALL_LIMIT = 100_000
LINE_EVENT_LIMIT = 5_000_000

FILENAME = "<eddymacro>"


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
# 検証(構文 + 実行環境の約束)
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
# 実行(文単位。通し実行とステップ実行の共通部)
# ---------------------------------------------------------------
class _ApiProxy:
    """ed の包み。呼び出し回数を数えるために挟む。

    属性の読み取りは元のオブジェクトへそのまま通し、呼び出し
    可能なら「数えてから呼ぶ」関数で包む。"""

    def __init__(self, target, on_call):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_on_call", on_call)

    def __getattr__(self, name):
        target = object.__getattribute__(self, "_target")
        value = getattr(target, name)
        if not callable(value):
            return value
        on_call = object.__getattribute__(self, "_on_call")

        def counted(*args, **kwargs):
            on_call()
            return value(*args, **kwargs)

        return counted


class PythonStepper:
    """Python マクロを文(statement)単位で実行する。

    api は ed として渡すオブジェクト(属性が API の関数)。1文が
    複数行にまたがるとき(if 文や関数定義など)は、その文全体を
    1ステップとして実行する。

    状態: position(次に実行する文の番号)、done、error
    (PythonMacroError か None)。step() は実行した文の開始行番号を
    返し、もう文が無ければ None。実行で例外が出たら error に
    入れて done にし、その例外を PythonMacroError に包んで上げる
    (呼び側は error を見てもよい)。"""

    def __init__(self, source, api):
        error = validate_python(source)
        if error is not None:
            raise PythonMacroError(error)
        self._tree = ast.parse(str(source), FILENAME)
        self._statements = list(self._tree.body)
        self._api = api
        self.output = []          # print の受け取り先
        self.position = 0
        self.error = None
        self._api_calls = 0
        namespace = {"__builtins__": dict(SAFE_BUILTINS)}
        namespace["__builtins__"]["print"] = self._print
        namespace["ed"] = _ApiProxy(api, self._count_api_call)
        self._namespace = namespace
        self._line_events = 0

    def _count_api_call(self):
        self._api_calls += 1
        if self._api_calls > API_CALL_LIMIT:
            raise PythonMacroError(
                "ed の呼び出し回数が上限を超えたため止めました"
                "（無限ループの疑い）")

    # ------------------------------------------------------------------
    @property
    def done(self):
        return self.error is not None or self.position >= len(self._statements)

    @property
    def total(self):
        return len(self._statements)

    def next_lineno(self):
        """次に実行する文の開始行番号。無ければ None。"""
        if self.done:
            return None
        return self._statements[self.position].lineno

    def statement_lines(self):
        """(開始行, 終了行) の並び(窓側の現在行表示用)。"""
        return [(node.lineno, node.end_lineno or node.lineno)
                for node in self._statements]

    # ------------------------------------------------------------------
    def _print(self, *args, **_kwargs):
        self.output.append(" ".join(str(arg) for arg in args))

    def _count_lines(self, frame, event, _arg):
        # マクロ自身の行だけを数える(API の中身は数えない)。
        if event == "line" and frame.f_code.co_filename == FILENAME:
            self._line_events += 1
            if self._line_events > LINE_EVENT_LIMIT:
                raise PythonMacroError(
                    "実行した行数が上限を超えたため止めました"
                    "（無限ループの疑い）")
        return self._count_lines

    # ------------------------------------------------------------------
    def step(self):
        """次の1文を実行する。実行した文の開始行番号を返す。"""
        if self.done:
            return None
        node = self._statements[self.position]
        module = ast.Module(body=[node], type_ignores=[])
        try:
            code = compile(module, FILENAME, "exec")
        except SyntaxError as e:  # 解析で通っているので通常は無い
            self.error = PythonMacroError(
                f"{node.lineno} 行目: 構文が読めません（{e.msg}）",
                node.lineno)
            raise self.error
        previous_trace = sys.gettrace()
        try:
            sys.settrace(self._count_lines)
            try:
                exec(code, self._namespace)  # noqa: S102  検証済みの自前コード
            finally:
                sys.settrace(previous_trace)
        except PythonMacroError as e:
            if e.lineno is None:
                e.lineno = node.lineno
            self.error = e
            raise
        except Exception as e:  # noqa: BLE001  利用者のコードの失敗
            self.error = PythonMacroError(
                f"{node.lineno} 行目: {type(e).__name__}: {e}",
                node.lineno)
            raise self.error
        self.position += 1
        return node.lineno

    def run_all(self):
        """残りの文を全部実行する。失敗したらそこで止まる。"""
        while not self.done:
            self.step()


def api_names():
    """ed の後ろに書ける名前の一覧(命令 + 問い合わせ)。"""
    return list(API_SPEC) + list(QUERY_NAMES)
