"""表示フィルタの条件式（Wireshark の表示フィルタ風）。Qt に依存しない純関数モジュール。

PhotoCuller の core/filter_expr.py を元に、写真の★・キーワード・選択中を足したもの。
元の文法・三値論理・エラーメッセージの形・関数（to_text / canonical / negate_text など）は
そのまま互換で残してある。

文法:
    式     := 論理和
    論理和 := 論理積 ( ('||' | 'or') 論理積 )*
    論理積 := 単項 ( ('&&' | 'and') 単項 )*
    単項   := ('!' | 'not') 単項 | 一次式
    一次式 := 原子 | '(' 式 ')'
    原子   := 人物名 | "引用符で囲んだ人物名"
            | ★比較 | @段階名 | @"段階名" | #キーワード | #"キーワード"
            | sel | face | reject | unrated | keep | del
    ★比較 := ('★' | 'r' | 'rating') 比較演算子 整数     例: ★>=3  r = 5  rating!=-1
    比較演算子 := '=' | '==' | '!=' | '>' | '>=' | '<' | '<='（値は -1〜5）

- 優先順位は ! ＞ && ＞ ||。二項演算子は左結合
- 英単語の演算子（not / and / or）・予約語・r / rating は大文字小文字を無視する
- keep は sel（選択中）の、del は reject（削除候補＝★-1）の別名。読むと sel / reject になる
- 人物名は「演算子・括弧・空白・引用符」以外の文字の並び（日本語可）。空白を含む名前や
  予約語・演算子と同じ名前は "..." で囲む（引用符の中に引用符は書けない）
- 比較の付かない r / rating は（互換のため）人物名。★ だけはエラー
- 空文字列（空白のみ）は「フィルタなし」で、parse は None を返す

原子（Atom）の kind と name:
    person  人物名      face   顔がある       sel     選択中
    rating  ">=3" など（演算子は = != > >= < <= に正規化、値は整数の文字列）
    level   段階名      keyword キーワード    reject  ★-1     unrated ★0

評価は三値論理（Kleene）。原子の値は True / False / None（不明）で、
    !不明 = 不明、偽 && 不明 = 偽、真 && 不明 = 不明、真 || 不明 = 真、偽 || 不明 = 不明
呼び出し側は「結果が True の写真だけを表示する」（不明は表示しない）。
★まわりの原子は meta_atom_value で評価できる（人物・face・sel はアプリ側）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Union

RESERVED = ("sel", "face", "reject", "unrated", "keep", "del")   # 予約語（原子）
_ALIASES = {"keep": "sel", "del": "reject"}
_WORD_OPS = {"not": "NOT", "and": "AND", "or": "OR"}
_DELIMS = frozenset('()!&|"')               # 人物名の区切りになる記号
META_KINDS = frozenset({"rating", "level", "keyword", "reject", "unrated"})

RATING_MIN, RATING_MAX = -1, 5
_RATING_HEAD = re.compile(r"(★|rating|r)(?=[\s<>=!])\s*(==|!=|>=|<=|=|>|<)", re.IGNORECASE)
_NUMBER = re.compile(r"\s*([+-]?\d+(?:\.\d+)?)")


class FilterSyntaxError(Exception):
    """条件式の構文エラー。position は 0 始まりの文字位置、message は「N文字目: 内容」形式の日本語。"""

    def __init__(self, position: int, detail: str):
        self.position = position
        self.detail = detail
        self.message = f"{position + 1}文字目: {detail}"
        super().__init__(self.message)


# ------------------------------------------------------------------ 構文木

@dataclass(frozen=True)
class Atom:
    kind: str                 # "person" / "sel" / "face" / "rating" / "level" / "keyword" / ...
    name: str | None = None   # person: 人物名 / rating: ">=3" / level: 段階名 / keyword: キーワード


@dataclass(frozen=True)
class Not:
    child: "Node"


@dataclass(frozen=True)
class And:
    left: "Node"
    right: "Node"


@dataclass(frozen=True)
class Or:
    left: "Node"
    right: "Node"


Node = Union[Atom, Not, And, Or]


# ------------------------------------------------------------------ 字句解析

@dataclass(frozen=True)
class Token:
    kind: str        # LPAREN / RPAREN / NOT / AND / OR / NAME / KEYWORD / RATING / LEVEL / TAG / EOF
    text: str        # 元の文字列上の表記（エラーメッセージ用）
    pos: int         # 0 始まりの文字位置
    value: str | None = None   # NAME: 人物名 / KEYWORD: 小文字の予約語 / RATING: ">=3" /
    #                            LEVEL: 段階名 / TAG: キーワード


def _read_word(expr: str, i: int) -> int:
    n = len(expr)
    j = i
    while j < n and not expr[j].isspace() and expr[j] not in _DELIMS:
        j += 1
    return j


def _read_quoted(expr: str, i: int, what: str) -> tuple[str, int]:
    """expr[i] の '"' から閉じ引用符までを読む。(中身, 次の位置)"""
    j = expr.find('"', i + 1)
    if j < 0:
        raise FilterSyntaxError(i, "引用符 '\"' が閉じていません")
    name = expr[i + 1:j].strip()
    if not name:
        raise FilterSyntaxError(i, f"引用符の中に{what}がありません")
    return name, j + 1


def _normalize_op(op: str) -> str:
    return "=" if op == "==" else op


def tokenize(expr: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(expr)
    while i < n:
        c = expr[i]
        if c.isspace():
            i += 1
            continue
        if c == "(":
            tokens.append(Token("LPAREN", c, i))
            i += 1
        elif c == ")":
            tokens.append(Token("RPAREN", c, i))
            i += 1
        elif c == "!":
            tokens.append(Token("NOT", c, i))
            i += 1
        elif c in "&|":
            pair = c * 2
            if not expr.startswith(pair, i):
                op = "論理積" if c == "&" else "論理和"
                raise FilterSyntaxError(i, f"'{c}' は1つでは使えません（{op}は '{pair}'）")
            tokens.append(Token("AND" if c == "&" else "OR", pair, i))
            i += 2
        elif c == '"':
            name, j = _read_quoted(expr, i, "名前")
            tokens.append(Token("NAME", expr[i:j], i, name))
            i = j
        elif c in "@#":
            what = "段階名" if c == "@" else "キーワード"
            kind = "LEVEL" if c == "@" else "TAG"
            if i + 1 < n and expr[i + 1] == '"':
                name, j = _read_quoted(expr, i + 1, what)
            else:
                j = _read_word(expr, i + 1)
                name = expr[i + 1:j]
                if not name:
                    raise FilterSyntaxError(i, f"'{c}' の後に{what}がありません")
            tokens.append(Token(kind, expr[i:j], i, name))
            i = j
        else:
            m = _RATING_HEAD.match(expr, i)
            if m:
                tokens.append(_rating_token(expr, i, m))
                i = tokens[-1].pos + len(tokens[-1].text)
                continue
            if expr[i] == "★":
                raise FilterSyntaxError(i, "'★' の後に比較（>=3 など）がありません")
            j = _read_word(expr, i)
            word = expr[i:j]
            low = word.lower()
            if low in _WORD_OPS:
                tokens.append(Token(_WORD_OPS[low], word, i))
            elif low in RESERVED:
                tokens.append(Token("KEYWORD", word, i, low))
            else:
                tokens.append(Token("NAME", word, i, word))
            i = j
    tokens.append(Token("EOF", "", n))
    return tokens


def _rating_token(expr: str, i: int, m: re.Match) -> Token:
    op = _normalize_op(m.group(2))
    num = _NUMBER.match(expr, m.end())
    if not num:
        raise FilterSyntaxError(m.end(), f"'{m.group(0).strip()}' の後に★の値（-1〜5）がありません")
    vpos = num.start(1)
    try:
        f = float(num.group(1))
    except ValueError:
        f = float("nan")
    if not f.is_integer() or not RATING_MIN <= f <= RATING_MAX:
        raise FilterSyntaxError(vpos, f"★の値は -1〜5 の整数です（'{num.group(1)}'）")
    end = num.end()
    if end < len(expr) and not expr[end].isspace() and expr[end] not in _DELIMS:
        raise FilterSyntaxError(end, f"★の値の後に余計な文字があります（'{expr[end]}'）")
    return Token("RATING", expr[i:end], i, f"{op}{int(f)}")


# ------------------------------------------------------------------ 構文解析

class _Parser:
    def __init__(self, expr: str):
        self.expr = expr
        self.tokens = tokenize(expr)
        self.idx = 0

    def peek(self) -> Token:
        return self.tokens[self.idx]

    def next(self) -> Token:
        tok = self.tokens[self.idx]
        self.idx += 1
        return tok

    def parse_or(self, prev: Token | None) -> Node:
        left = self.parse_and(prev)
        while self.peek().kind == "OR":
            op = self.next()
            left = Or(left, self.parse_and(op))
        return left

    def parse_and(self, prev: Token | None) -> Node:
        left = self.parse_unary(prev)
        while self.peek().kind == "AND":
            op = self.next()
            left = And(left, self.parse_unary(op))
        return left

    def parse_unary(self, prev: Token | None) -> Node:
        if self.peek().kind == "NOT":
            op = self.next()
            return Not(self.parse_unary(op))
        return self.parse_primary(prev)

    def parse_primary(self, prev: Token | None) -> Node:
        tok = self.peek()
        if tok.kind == "NAME":
            self.next()
            return Atom("person", tok.value)
        if tok.kind == "KEYWORD":
            self.next()
            return Atom(_ALIASES.get(tok.value, tok.value))
        if tok.kind == "RATING":
            self.next()
            return Atom("rating", tok.value)
        if tok.kind == "LEVEL":
            self.next()
            return Atom("level", tok.value)
        if tok.kind == "TAG":
            self.next()
            return Atom("keyword", tok.value)
        if tok.kind == "LPAREN":
            self.next()
            inner = self.parse_or(tok)
            close = self.peek()
            if close.kind == "RPAREN":
                self.next()
                return inner
            if close.kind == "EOF":
                raise FilterSyntaxError(close.pos, "')' が足りません")
            raise FilterSyntaxError(close.pos, "条件の間に演算子（&& または ||）がありません")
        # ここに来るのは「条件があるべき場所に条件がない」とき
        if prev is not None:
            raise FilterSyntaxError(prev.pos, f"'{prev.text}' の後に条件がありません")
        if tok.kind == "EOF":
            raise FilterSyntaxError(tok.pos, "条件がありません")
        raise FilterSyntaxError(tok.pos, f"'{tok.text}' の前に条件がありません")


def parse(expr: str | None) -> Node | None:
    """条件式を構文木にする。空（空白のみ）なら None。構文エラーは FilterSyntaxError。"""
    if expr is None or not expr.strip():
        return None
    p = _Parser(expr)
    node = p.parse_or(None)
    tok = p.peek()
    if tok.kind != "EOF":
        if tok.kind == "RPAREN":
            raise FilterSyntaxError(tok.pos, "対応する '(' がない ')' です")
        raise FilterSyntaxError(tok.pos, "条件の間に演算子（&& または ||）がありません")
    return node


# ------------------------------------------------------------------ 走査・評価

def atoms(node: Node | None) -> Iterator[Atom]:
    """原子を左から順に列挙する。"""
    if node is None:
        return
    if isinstance(node, Atom):
        yield node
    elif isinstance(node, Not):
        yield from atoms(node.child)
    else:
        yield from atoms(node.left)
        yield from atoms(node.right)


def referenced_persons(node: Node | None) -> list[str]:
    """式に出てくる人物名（出現順・重複なし）。予約語は含めない（"keep" のように引用符付きなら人物）。"""
    seen: list[str] = []
    for a in atoms(node):
        if a.kind == "person" and a.name not in seen:
            seen.append(a.name)
    return seen


def referenced_levels(node: Node | None) -> list[str]:
    """式に出てくる段階名（出現順・重複なし）。設定に無い名前を知らせるのに使う。"""
    seen: list[str] = []
    for a in atoms(node):
        if a.kind == "level" and a.name not in seen:
            seen.append(a.name)
    return seen


def needs_faces(node: Node | None) -> bool:
    """評価に顔解析の結果が必要か（人物名か face を含む）。sel / ★まわりだけなら不要。"""
    return any(a.kind in ("person", "face") for a in atoms(node))


def needs_meta(node: Node | None) -> bool:
    """評価に写真の★・キーワードが必要か。"""
    return any(a.kind in META_KINDS for a in atoms(node))


def uses_face_atom(node: Node | None) -> bool:
    return any(a.kind == "face" for a in atoms(node))


def has_negation(node: Node | None) -> bool:
    """式のどこかに否定（! / not）を含むか。"""
    if node is None or isinstance(node, Atom):
        return False
    if isinstance(node, Not):
        return True
    return has_negation(node.left) or has_negation(node.right)


AtomFn = Callable[[str, "str | None"], "bool | None"]


def evaluate(node: Node | None, atom_fn: AtomFn) -> bool | None:
    """三値論理（Kleene）で評価する。atom_fn(kind, name) は True / False / None（不明）を返す。

    node が None（フィルタなし）なら True。
    """
    if node is None:
        return True
    if isinstance(node, Atom):
        return atom_fn(node.kind, node.name)
    if isinstance(node, Not):
        v = evaluate(node.child, atom_fn)
        return None if v is None else not v
    if isinstance(node, And):
        left = evaluate(node.left, atom_fn)
        if left is False:
            return False
        right = evaluate(node.right, atom_fn)
        if right is False:
            return False
        return None if (left is None or right is None) else True
    # Or
    left = evaluate(node.left, atom_fn)
    if left is True:
        return True
    right = evaluate(node.right, atom_fn)
    if right is True:
        return True
    return None if (left is None or right is None) else False


# ------------------------------------------------------------------ ★まわりの原子の評価

_COMPARE = {
    "=": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


def split_rating(name: str) -> tuple[str, int]:
    """rating 原子の name（">=3"）を (演算子, 値) に分ける。"""
    op = name[:2] if name[:2] in ("!=", ">=", "<=") else name[:1]
    return op, int(name[len(op):])


def meta_atom_value(kind: str, name: str | None, meta, config: dict | None = None):
    """★まわりの原子（rating / level / keyword / reject / unrated）を写真の★・キーワードで評価する。

    meta は photometa.xmp.PhotoMeta（rating と keywords を持つもの）。None なら不明（None）。
    段階名は config（photometa.config の設定）で引く。知らない段階名は偽（未登録の人物と同じ）。
    キーワードは大文字小文字を区別しない。
    人物・face・sel などアプリ側が受け持つ原子には NotImplemented を返す。
    """
    if kind not in META_KINDS:
        return NotImplemented
    if meta is None:
        return None
    rating = meta.rating
    if kind == "rating":
        op, value = split_rating(name or "")
        return _COMPARE[op](rating, value)
    if kind == "reject":
        return rating == -1
    if kind == "unrated":
        return rating == 0
    if kind == "keyword":
        target = (name or "").casefold()
        return any(k.casefold() == target for k in meta.keywords)
    # level
    from .config import DEFAULT_CONFIG, level_min
    mn = level_min(config if config is not None else DEFAULT_CONFIG, name or "")
    return False if mn is None else rating >= mn


def meta_atom_fn(meta, config: dict | None = None, other: AtomFn | None = None) -> AtomFn:
    """evaluate に渡す atom_fn を作る。★まわりは meta で、それ以外は other で（無ければ不明）。"""
    def fn(kind, name):
        v = meta_atom_value(kind, name, meta, config)
        if v is NotImplemented:
            return other(kind, name) if other is not None else None
        return v
    return fn


# ------------------------------------------------------------------ 文字列化

def _needs_quote(name: str) -> bool:
    return (
        not name
        or any(c.isspace() or c in _DELIMS for c in name)
        or name.lower() in RESERVED
        or name.lower() in _WORD_OPS
        or name[0] in "@#★"
        or _RATING_HEAD.match(name) is not None
    )


def quote_name(name: str) -> str:
    """人物名を式に書ける形にする（空白・記号・予約語・演算子と同じ名前は引用符で囲む）。"""
    return f'"{name}"' if _needs_quote(name) else name


def _quote_after_sigil(name: str) -> str:
    """@ / # の後ろに書く名前（空白や記号を含むなら引用符で囲む）。"""
    if not name or name[0] == '"' or any(c.isspace() or c in _DELIMS for c in name):
        return f'"{name}"'
    return name


def atom_text(atom: Atom) -> str:
    if atom.kind == "person":
        return quote_name(atom.name)
    if atom.kind == "rating":
        return f"★{atom.name}"
    if atom.kind == "level":
        return "@" + _quote_after_sigil(atom.name)
    if atom.kind == "keyword":
        return "#" + _quote_after_sigil(atom.name)
    return atom.kind


def to_text(node: Node | None) -> str:
    """構文木を正規化した文字列にする（必要最小限の括弧・演算子は ! && || に統一）。"""
    if node is None:
        return ""
    if isinstance(node, Atom):
        return atom_text(node)
    if isinstance(node, Not):
        inner = to_text(node.child)
        return f"!({inner})" if isinstance(node.child, (And, Or)) else f"!{inner}"
    if isinstance(node, And):
        left = to_text(node.left)
        right = to_text(node.right)
        if isinstance(node.left, Or):
            left = f"({left})"
        if isinstance(node.right, (And, Or)):
            right = f"({right})"
        return f"{left} && {right}"
    left = to_text(node.left)
    right = to_text(node.right)
    if isinstance(node.right, Or):
        right = f"({right})"
    return f"{left} || {right}"


def canonical(expr: str | None) -> str:
    """式の比較用の正規形（書き方の揺れ たろう&&はなこ / たろう and はなこ を同一視する）。

    構文エラーの式は前後の空白を除いた文字列そのもの。
    """
    try:
        return to_text(parse(expr))
    except FilterSyntaxError:
        return (expr or "").strip()


def negate_text(expr: str | None) -> str:
    """H キー用: 式を否定した文字列を返す（ユーザーの書いた表記をなるべく保つ）。

    - !X（X が原子）→ X、!(…)（全体を囲む括弧）→ 括弧の中身、!Y（その他）→ Y
    - それ以外: 原子1つなら !X、複合式なら !(式)
    - 空文字列はそのまま空。構文エラーの式は FilterSyntaxError
    """
    s = (expr or "").strip()
    if not s:
        return ""
    node = parse(s)
    if isinstance(node, Not):
        toks = tokenize(s)
        if toks[0].kind != "NOT":
            # (!たろう) のように否定全体が括弧で囲まれている: 中身を正規化して返す
            return to_text(node.child)
        rest = toks[1:-1]   # 先頭の否定と EOF を除いたトークン
        if rest and rest[0].kind == "LPAREN" and _matching_paren(rest, 0) == len(rest) - 1:
            return s[rest[0].pos + 1:rest[-1].pos].strip()
        return s[rest[0].pos:].strip()
    if isinstance(node, Atom):
        return f"!{s}"
    return f"!({s})"


def _matching_paren(tokens: list[Token], open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(tokens)):
        if tokens[i].kind == "LPAREN":
            depth += 1
        elif tokens[i].kind == "RPAREN":
            depth -= 1
            if depth == 0:
                return i
    return -1


def legacy_person_filter_to_text(pf) -> str:
    """旧形式の人物抽出条件 {"person": 名前, "mode": with/without/all} を式文字列へ変換する。"""
    if isinstance(pf, str):
        return pf
    if not isinstance(pf, dict):
        return ""
    person = pf.get("person") or ""
    mode = pf.get("mode")
    if not person or mode == "all":
        return ""
    q = quote_name(person)
    return f"!{q}" if mode == "without" else q
