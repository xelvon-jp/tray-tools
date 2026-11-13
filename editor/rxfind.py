# editor/rxfind.py
# 正規表現の検索・置換(Qt に依存しない)。
#
# 実処理は別プロセスで走らせる。理由は2つ:
#   - 不正な正規表現はコンパイルの時点で re.error になるが、文法が
#     通っても破滅的なバックトラック(^(a+)+$ の類)で固まる式がある。
#     Python の re には時間切れが無いので、同じプロセスでは止められない
#   - 子プロセスごと打ち切れる形なら、時間切れを「失敗の文」にできる
#     (アウトラインの設定試験 trial_patterns と同じ作法)
#
# 子プロセスへは標準入力の JSON で {op, text, pattern, repl, start,
# regex, case} を渡し、最後の JSON 行で結果を受ける。op は
# find_next / find_prev / replace_one / replace_all。
# 置換文字列のグループ参照(\1 や \g<name>)は re の expand の流儀。
# regex=False のときは、パターンは文字どおり(escape する)、置換文字列も
# 文字どおり(グループ参照は効かない)。
#
# 純粋関数(find_span_* / replace_*_text)はコンパイル済みパターンを
# 受ける形で、子プロセスの中と、リスクの無い通常置換(regex=False)で
# 使う。窓からは run_operation を使う。
import json
import re
import subprocess
import sys

# 1回の検索・置換の上限(秒)。文書全体を相手にする操作でも、この桁で
# 終わらないものは「固まる式」として扱ってよい。
DEFAULT_TIMEOUT_SEC = 5.0


class RegexFault(Exception):
    """正規表現まわりの失敗。文はそのまま利用者に見せられる日本語。"""


def compile_search(pattern, regex=True, case_sensitive=False):
    """検索パターンをコンパイルする。不正なら RegexFault。

    regex=False なら pattern は文字どおりに探す文字列として扱う。
    ^ と $ は行の頭・末尾に効かせる(MULTILINE)。文書全体ではなく
    行ごとに効くのが、テキストエディタの検索・置換の作法。"""
    source = pattern if regex else re.escape(pattern)
    flags = re.MULTILINE | (0 if case_sensitive else re.IGNORECASE)
    try:
        return re.compile(source, flags)
    except re.error as e:
        raise RegexFault(f"正規表現が不正です（{e}）") from None


def expand_replacement(match, repl, regex=True):
    """置換文字列を、マッチに照らして展開する。

    regex=True なら \1 等のグループ参照を展開する(参照先が無い等の
    不正は RegexFault)。regex=False なら repl を文字どおり返す。"""
    if not regex:
        return repl
    try:
        return match.expand(repl)
    except (re.error, IndexError) as e:
        raise RegexFault(f"置換文字列が不正です（{e}）") from None


def find_span_next(text, rx, start):
    """start 以後の最初のマッチの (開始, 終了)。無ければ None。

    空マッチが start ちょうどで返ると、次へ進めなくなる(同じ場所を
    探し続ける)ので、その場合は1字先から探し直す。"""
    start = max(0, min(start, len(text)))
    match = rx.search(text, start)
    if match is None:
        return None
    if match.start() == match.end() == start and start < len(text):
        retry = rx.search(text, start + 1)
        if retry is not None:
            return retry.start(), retry.end()
    return match.start(), match.end()


def find_span_prev(text, rx, start):
    """start より前に終わる最後のマッチの (開始, 終了)。無ければ None。"""
    start = max(0, min(start, len(text)))
    found = None
    for match in rx.finditer(text, 0, start):
        if match.end() == match.start() and match.end() == start and found:
            break  # start ちょうどの空マッチは「前」には含めない
        found = match
    if found is None:
        return None
    return found.start(), found.end()


def replace_one_text(text, rx, repl, start, regex=True):
    """start 以後の最初のマッチを置換した (新しい本文, 開始, 終了)。

    終了は、置き換えた後の文字列での挿入部分の終わり。マッチが
    無ければ None。"""
    span = find_span_next(text, rx, start)
    if span is None:
        return None
    match = rx.match(text, span[0])
    expanded = expand_replacement(match, repl, regex)
    begin, end = span
    return text[:begin] + expanded + text[end:], begin, begin + len(expanded)


def replace_all_text(text, rx, repl, regex=True):
    """全部置換した (新しい本文, 件数)。"""
    count = 0

    def _sub(match):
        nonlocal count
        count += 1
        return expand_replacement(match, repl, regex)

    new_text = rx.sub(_sub, text)
    return new_text, count


# ---------------------------------------------------------------
# 別プロセスでの実行
# ---------------------------------------------------------------
# 子プロセスで走らせるコード。このファイルの純粋関数と同じ意味の
# 処理を、標準ライブラリだけで書き直したもの(子は editor パッケージ
# を import しない。パスの前提を作らないため)。
_CHILD_CODE = r"""
import json, re, sys

def compile_search(pattern, regex, case):
    source = pattern if regex else re.escape(pattern)
    flags = re.MULTILINE | (0 if case else re.IGNORECASE)
    return re.compile(source, flags)

def expand(match, repl, regex):
    if not regex:
        return repl
    return match.expand(repl)

def span_next(text, rx, start):
    start = max(0, min(start, len(text)))
    m = rx.search(text, start)
    if m is None:
        return None
    if m.start() == m.end() == start and start < len(text):
        retry = rx.search(text, start + 1)
        if retry is not None:
            return [retry.start(), retry.end()]
    return [m.start(), m.end()]

def span_prev(text, rx, start):
    start = max(0, min(start, len(text)))
    found = None
    for m in rx.finditer(text, 0, start):
        if m.end() == m.start() and m.end() == start and found is not None:
            break
        found = m
    if found is None:
        return None
    return [found.start(), found.end()]

def main():
    data = json.load(sys.stdin)
    op = data["op"]
    text = data["text"]
    regex = bool(data.get("regex", True))
    try:
        rx = compile_search(data["pattern"], regex, bool(data.get("case")))
    except re.error as e:
        print(json.dumps({"ok": False, "error": "pattern", "detail": str(e)}))
        return
    try:
        if op == "find_next":
            span = span_next(text, rx, int(data.get("start", 0)))
            result = {"ok": True, "span": span}
        elif op == "find_prev":
            span = span_prev(text, rx, int(data.get("start", 0)))
            result = {"ok": True, "span": span}
        elif op == "replace_one":
            span = span_next(text, rx, int(data.get("start", 0)))
            if span is None:
                result = {"ok": True, "span": None}
            else:
                m = rx.match(text, span[0])
                expanded = expand(m, data.get("repl", ""), regex)
                result = {"ok": True, "span": span, "replacement": expanded}
        elif op == "replace_all":
            count = [0]
            def sub(m):
                count[0] += 1
                return expand(m, data.get("repl", ""), regex)
            new_text = rx.sub(sub, text)
            result = {"ok": True, "text": new_text, "count": count[0]}
        else:
            result = {"ok": False, "error": "op", "detail": op}
    except (re.error, IndexError) as e:
        result = {"ok": False, "error": "repl", "detail": str(e)}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")

main()
"""


def run_operation(op, *, text, pattern, repl="", start=0, regex=True,
                  case_sensitive=False, timeout=DEFAULT_TIMEOUT_SEC):
    """検索・置換を別プロセスで1回実行し、結果の辞書を返す。

    戻り値:
      find_next / find_prev … {"span": (開始, 終了) | None}
      replace_one          … {"span": (開始, 終了) | None,
                              "replacement": 置き換えた後の文字列}
      replace_all          … {"text": 新しい本文, "count": 件数}
    不正な式・不正な置換文字列・時間切れ・子プロセスの異常は
    RegexFault(利用者向けの文)にする。"""
    if not pattern:
        raise RegexFault("検索する文字列が空です")
    payload = json.dumps({
        "op": op, "text": text, "pattern": pattern, "repl": repl,
        "start": int(start), "regex": bool(regex),
        "case": bool(case_sensitive),
    }, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_CODE],
            input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RegexFault(
            "正規表現の処理に時間がかかりすぎたため、中止しました"
            "（式を簡単にすると通ることがあります）") from None
    except OSError as e:
        raise RegexFault(f"検索を実行できませんでした（{e}）") from None
    result = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            result = parsed
            break
    if result is None:
        raise RegexFault("検索の実行結果が読めませんでした")
    if not result.get("ok"):
        kind = result.get("error")
        detail = result.get("detail") or ""
        if kind == "pattern":
            raise RegexFault(f"正規表現が不正です（{detail}）")
        if kind == "repl":
            raise RegexFault(f"置換文字列が不正です（{detail}）")
        raise RegexFault(f"検索を実行できませんでした（{detail}）")
    if "span" in result and result["span"] is not None:
        result["span"] = tuple(result["span"])
    return result
