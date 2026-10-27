# filer_rename.py
# あっふの一括改名(Renemo 風。Shift+R / Alt+R → Enter)の、Qt を使わない部分。
#
#   - 全角/半角・大文字/小文字の変換、置換、連番の書式
#   - 確定前の検査(使えない文字・空・末尾の空白やピリオド・予約名・結果の重複・
#     ほかのファイルとの衝突)
#   - 改名の手順づくり: 名前の入れ替え(a→b, b→a)や大文字小文字だけの変更も通るよう、
#     いったん全部を一時名にしてから本来の名前にする(2段階)
#   - 実行と、途中で失敗したときの巻き戻し(それまでに行った改名を逆順に戻す)
#   - 履歴(元のフルパス→新しいフルパスの組)。保存先はリポジトリの外
#     (filer_core.data_dir()。よく行くフォルダの記録と同じ)
import os
import re
import time
import unicodedata
import uuid

import filer_core as core

HISTORY_STORE = "filer_rename_history.json"
HISTORY_LIMIT = 50

_INVALID = re.compile(r'[\\/:*?"<>|]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | \
    {f"LPT{i}" for i in range(1, 10)}


# ---------------------------------------------------------------
# 変換
# ---------------------------------------------------------------
def _build_kana_tables():
    """半角カナ ⇔ 全角カナの表。濁点・半濁点付き(ガ ⇔ ｶﾞ)も入れる。"""
    half_to_full = {}
    for code in range(0xFF61, 0xFFA0):
        half = chr(code)
        full = unicodedata.normalize("NFKC", half)
        if full != half:
            half_to_full[half] = full
    full_to_half = {}
    for half, full in half_to_full.items():
        full_to_half.setdefault(full, half)
    dakuten, handakuten = "ﾞ", "ﾟ"
    for half, full in list(half_to_full.items()):
        for mark, combining in ((dakuten, "゙"), (handakuten, "゚")):
            composed = unicodedata.normalize("NFC", full + combining)
            if len(composed) == 1 and composed != full:
                full_to_half[composed] = half + mark
                half_to_full[half + mark] = composed
    return half_to_full, full_to_half


_HALF_TO_FULL_KANA, _FULL_TO_HALF_KANA = _build_kana_tables()


def to_halfwidth(text: str) -> str:
    """全角の英数字・記号・空白・カタカナを半角にする(漢字やひらがなは変えない)。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        elif ch == "　":
            out.append(" ")
        elif ch in _FULL_TO_HALF_KANA:
            out.append(_FULL_TO_HALF_KANA[ch])
        else:
            out.append(ch)
    return "".join(out)


def to_fullwidth(text: str) -> str:
    """半角の英数字・記号・空白・カタカナを全角にする(濁点付きは1文字にまとめる)。"""
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        pair = text[i:i + 2]
        if len(pair) == 2 and pair in _HALF_TO_FULL_KANA:
            out.append(_HALF_TO_FULL_KANA[pair])
            i += 2
            continue
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif ch == " ":
            out.append("　")
        elif ch in _HALF_TO_FULL_KANA:
            out.append(_HALF_TO_FULL_KANA[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)


CONVERSIONS = {
    "upper": str.upper,
    "lower": str.lower,
    "half": to_halfwidth,
    "full": to_fullwidth,
}


def convert(text: str, mode: str) -> str:
    return CONVERSIONS[mode](text)


def split_ext(name: str, is_dir: bool = False):
    """(名前, 拡張子)。フォルダと先頭がドットの名前は拡張子なし。"""
    if is_dir:
        return name, ""
    stem, ext = os.path.splitext(name)
    return stem, ext


def replace_name(name: str, find: str, repl: str, regex: bool = False,
                 case_sensitive: bool = False, keep_ext: bool = False,
                 is_dir: bool = False) -> str:
    """置換。keep_ext なら拡張子は触らない。正規表現が書けていなければ ValueError。"""
    if not find:
        return name
    stem, ext = split_ext(name, is_dir) if keep_ext else (name, "")
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = find if regex else re.escape(find)
    try:
        compiled = re.compile(pattern, flags)
        # 正規表現でないときは置換後の文字列もそのまま(\1 などを特別扱いしない)。
        result = compiled.sub(repl if regex else (lambda _m: repl), stem)
    except re.error as e:
        raise ValueError(f"正規表現が正しくありません: {e}") from e
    return result + ext


_NUMBER_RE = re.compile(r"\{n(?::(0?)(\d+))?\}")


def format_number(fmt: str, n: int, digits: int = 1) -> str:
    """連番の書式。"{n}" は digits 桁のゼロ埋め、"{n:03}" はその桁数。"""
    def sub(match):
        width = int(match.group(2)) if match.group(2) else digits
        return str(n).zfill(width) if n >= 0 else str(n)
    return _NUMBER_RE.sub(sub, fmt)


def numbered_names(names, fmt: str, start: int = 1, step: int = 1, digits: int = 1,
                   keep_ext: bool = True, dir_flags=None) -> list:
    """names に連番を振った名前。書式に {n} が無ければ末尾に付ける。keep_ext なら
    それぞれの拡張子を残す(書式は名前の部分だけ)。"""
    if not _NUMBER_RE.search(fmt):
        fmt = fmt + "{n}"
    result = []
    for i, name in enumerate(names):
        is_dir = bool(dir_flags[i]) if dir_flags else False
        _stem, ext = split_ext(name, is_dir) if keep_ext else (name, "")
        result.append(format_number(fmt, start + i * step, digits) + ext)
    return result


# ---------------------------------------------------------------
# 検査
# ---------------------------------------------------------------
def name_problem(name: str):
    """1つの名前として使えない理由。使えるなら None。"""
    if not name or not name.strip():
        return "名前が空です"
    if _INVALID.search(name):
        return '使えない文字があります（\\ / : * ? " < > |）'
    if name != name.rstrip(" ."):
        return "末尾に空白やピリオドは置けません"
    if os.path.splitext(name)[0].upper().rstrip(" ") in _RESERVED or name.upper() in _RESERVED:
        return "予約された名前です（CON / PRN / AUX / NUL / COM1〜9 / LPT1〜9）"
    return None


def validate_batch(items, new_names, existing_by_folder) -> dict:
    """{行番号: 理由}。問題が無ければ空。

    items は [(フォルダ, 元の名前), ...]、new_names は同じ並びの新しい名前、
    existing_by_folder は {フォルダ: そのフォルダにある名前の集まり}。
    - 変えない行は問題にしない(元のままなら必ず通る)
    - 結果が重なる(大文字小文字を無視)ものは両方とも
    - ほかのファイル(今回の一括改名に入っていないもの)と重なるもの"""
    problems = {}
    for row, ((_folder, old), new) in enumerate(zip(items, new_names)):
        if new != old:
            reason = name_problem(new)
            if reason:
                problems[row] = reason
    finals = {}
    for row, ((folder, old), new) in enumerate(zip(items, new_names)):
        key = (os.path.normcase(folder), new.casefold())
        finals.setdefault(key, []).append(row)
    for rows in finals.values():
        if len(rows) > 1:
            for row in rows:
                problems.setdefault(row, "結果の名前が重なっています")
    in_batch = {}
    for folder, old in items:
        in_batch.setdefault(os.path.normcase(folder), set()).add(old.casefold())
    for row, ((folder, old), new) in enumerate(zip(items, new_names)):
        if new == old or row in problems:
            continue
        others = {n.casefold() for n in existing_by_folder.get(folder, ())} - \
            in_batch.get(os.path.normcase(folder), set())
        if new.casefold() in others:
            problems[row] = "同じ名前のファイルが既にあります"
    return problems


# ---------------------------------------------------------------
# 手順と実行
# ---------------------------------------------------------------
def plan_renames(pairs) -> list:
    """[(元のフルパス, 新しいフルパス)] から、実際に行う改名の手順 [(から, へ)] を作る。

    全部をいったん一時名にしてから本来の名前にする。入れ替え(a→b, b→a)や連鎖
    (a→b, b→c)、大文字小文字だけの変更(a.txt→A.txt)も、途中で名前がぶつからない。"""
    changed = [(src, dst) for src, dst in pairs if src != dst]
    token = uuid.uuid4().hex[:8]
    first, second = [], []
    for i, (src, dst) in enumerate(changed):
        temp = os.path.join(os.path.dirname(src), f".renemo~{token}~{i}")
        first.append((src, temp))
        second.append((temp, dst))
    return first + second


def execute_plan(steps, rename=os.rename):
    """手順を順に行う。(行った数, 誤り)。途中で失敗したら、それまでの改名を逆順に戻す
    (巻き戻し)。戻すのにも失敗したら、その旨を誤りに足す(どこまで進んだか分かるように)。"""
    done = []
    for src, dst in steps:
        try:
            rename(src, dst)
            done.append((src, dst))
        except OSError as e:
            failures = []
            for back_src, back_dst in reversed(done):
                try:
                    rename(back_dst, back_src)
                except OSError as back:
                    failures.append(f"{back_dst} → {back_src}: {back}")
            message = f"{os.path.basename(src)} を改名できません（{e.strerror or e}）"
            if failures:
                message += "。元に戻せなかったもの: " + " / ".join(failures)
            else:
                message += "。それまでの改名は元に戻しました"
            return 0, message
    return len(done), None


# ---------------------------------------------------------------
# 履歴
# ---------------------------------------------------------------
def add_history(history: list, pairs, now: float = None, limit: int = HISTORY_LIMIT) -> list:
    """一括改名の記録を先頭に足す。[{"time": 時刻, "pairs": [[元, 新], ...]}]。"""
    entry = {"time": now if now is not None else time.time(),
             "pairs": [[src, dst] for src, dst in pairs if src != dst]}
    if not entry["pairs"]:
        return history
    return ([entry] + list(history))[:limit]


def undo_pairs(entry: dict) -> list:
    """記録を元に戻すための組(新 → 元)。"""
    return [(dst, src) for src, dst in entry.get("pairs", [])]


def undo_problems(entry: dict, exists=os.path.exists) -> list:
    """元に戻せない理由の一覧(空なら戻せる)。新しい名前のものが無い・元の名前が
    ほかのもので埋まっている(今回戻すものどうしは除く)。"""
    problems = []
    pairs = entry.get("pairs", [])
    moving_away = {os.path.normcase(dst) for _src, dst in pairs}
    for src, dst in pairs:
        if not exists(dst):
            problems.append(f"見つかりません: {dst}")
        elif exists(src) and os.path.normcase(src) not in moving_away:
            problems.append(f"元の名前が使われています: {src}")
    return problems


def describe_history(entry: dict) -> str:
    pairs = entry.get("pairs", [])
    stamp = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(entry.get("time", 0)))
    first = pairs[0] if pairs else ["", ""]
    head = f"{os.path.basename(first[0])} → {os.path.basename(first[1])}"
    more = f" ほか {len(pairs) - 1}件" if len(pairs) > 1 else ""
    return f"{stamp}  {len(pairs)}件  {head}{more}"


def load_history() -> list:
    data = core.load_store(HISTORY_STORE, [])
    return [e for e in data if isinstance(e, dict) and isinstance(e.get("pairs"), list)]


def save_history(history: list) -> None:
    core.save_store(HISTORY_STORE, history)
