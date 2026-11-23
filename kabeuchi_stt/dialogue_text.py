# -*- coding: utf-8 -*-
"""AI対話モード: 下書きの文章づくり（純関数。GUI/Qt/COM 非依存）。

壁打ちの清書（paragraph.py）と同じ規則を、下書きの発話の連結に使う。
  発話どうしの間（無音の長さ）が
  - force_gap_sec 以上            : 必ず改行。直前の文末が句点系でなければ「。」を補う（末尾の「、」は「。」に置き換える）
  - gap_sec 以上 force_gap_sec 未満 : 直前が句点系（。？！）で終わっているときだけ改行
  - gap_sec 未満                   : 句点を補わず、そのまま連結（話の途中の区切りかもしれない）
  間の長さが分からないとき（時刻なし）は、従来どおりの連結（日本語は区切りなし、英数字どうしだけ空白）。

手で直した下書き（entry の "edited"）の文章そのものには規則を再適用しない（「。」を補わない）。
直したあとに足された発話との境目では、改行を入れるだけ。

確定のとき（finalize_text）は、末尾が句読点で終わっていなければ「。」を補う（絵文字・記号で終わるものは補わない）。
"""
from __future__ import annotations

import re
import unicodedata

from paragraph import SENTENCE_END, _CLOSERS, _join, _terminate, ends_sentence

# 区切り器が、発話の頭に付ける直前の音（preroll）と、末尾に残す無音（tail）の合計。
# 発話どうしの「間」は、記録上の時刻のすき間にこれを足したものが実際の無音に近い。
EDGE_PAD_SEC = 0.6
_ADJACENT_SEC = 0.05      # これ未満のすき間は、無音を挟まない連続（最長での強制の区切り）とみなす

_NO_PERIOD_AFTER = "、,，.;:；：" + SENTENCE_END


def utterance_gap(prev: dict, cur: dict, edge_pad: float = EDGE_PAD_SEC):
    """直前の発話 prev と cur の間の無音の長さ（秒）。時刻が無ければ None。"""
    try:
        end, start = prev.get("end_sec"), cur.get("start_sec")
        if end is None or start is None:
            return None
        raw = float(start) - float(end)
    except (TypeError, ValueError):
        return None
    if raw < _ADJACENT_SEC:
        return 0.0
    return raw + float(edge_pad)


_NL_AFTER = "。？！．｡"        # 文中でこの直後に改行を入れる（英語の「.」・ASCII の「?」「!」は対象外）
_DIGITS = "0123456789０１２３４５６７８９"
_NOISE_PHRASES = ("ご視聴ありがとうございました", "ご視聴ありがとうございます", "ご視聴いただきありがとうございました",
                  "最後までご視聴いただきありがとうございました", "チャンネル登録お願いします", "チャンネル登録よろしくお願いします",
                  "ご清聴ありがとうございました", "thankyouforwatching", "thanksforwatching", "今日は晴れていて気持ちがいいですね", "文字起こしの清書と逐次認識で句読点が入るかを確認します")   # 末尾は、認識に渡す文体の見本（dialogue_session.STYLE_PROMPT）がそのまま出てしまった場合
_NOISE_KEYS = ("ご視聴", "ご清聴", "チャンネル登録", "thankyouforwatching", "thanksforwatching", "今日は晴れていて気持ちがいいですね", "文字起こしの清書と逐次認識で句読点が入るかを確認します")   # 末尾は、認識に渡す文体の見本（dialogue_session.STYLE_PROMPT）がそのまま出てしまった場合


# 見本文「文字起こしの清書と逐次認識で、句読点が入るかを確認します。」の、特徴的な一部（句読点を除いた形）
_PROMPT_ECHO_KEYS = ("文字起こしの清書と逐次認識", "清書と逐次認識で", "逐次認識で句読点", "句読点が入るかを確認", "句読点が入るか確認")


def break_sentences(text: str) -> str:
    """文中の「。」「？」「！」「．」の直後に改行を1つ入れる（文末のものには入れない）。
    連続する句点系（「。。」「？！」）と閉じ括弧・引用符（「はい。」）は1かたまりとして、その直後で改行する。
    すでに改行があるところには足さない。数字の小数点（「３．５」）・英語の「.」は対象外。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        out.append(ch)
        i += 1
        if ch not in _NL_AFTER:
            continue
        if ch == "．" and i - 2 >= 0 and text[i - 2] in _DIGITS and i < n and text[i] in _DIGITS:
            continue
        while i < n and (text[i] in _NL_AFTER or text[i] in _CLOSERS):
            out.append(text[i])
            i += 1
        j = i
        while j < n and text[j] in " 　":
            j += 1
        if j < n and text[j] != "\n":
            out.append("\n")
            i = j
    return "".join(out)


def is_noise_text(text: str) -> bool:
    """認識結果が、無音・ノイズに対する定番の幻覚（「ご視聴ありがとうございました」等）か、記号だけか。
    こうした結果は空として扱う（波形・仮認識を残さない）。"""
    t = "".join(ch for ch in (text or "") if not (ch.isspace() or unicodedata.category(ch)[0] in "PS")).lower()
    if not t:
        return bool((text or "").strip())      # 記号・句読点だけ（空文字そのものは対象外）
    if any(t == p or t == p + "ありがとうございました" for p in _NOISE_PHRASES):
        return True
    # 「最後までご視聴ありがとうございました」「ご視聴ありがとうございました。また次回」など、言い回しの揺れ。
    # 視聴・チャンネル登録の定型句だけでできた短い文（本物の発話に混ざる長い文は対象外）
    if len(t) <= 30 and any(k in t for k in _NOISE_KEYS) and any(k in t for k in ("ありがとう", "お願い", "よろしく", "また次回")):
        return True
    # 認識に渡した句読点の見本文（dialogue_session.STYLE_PROMPT）が、無音・小さな音のときにそのまま（一部だけ・言い換えて）
    # 出てくることがある。見本文の中身そのものを含む短い結果は、幻覚として捨てる
    if len(t) <= 50 and any(k in t for k in _PROMPT_ECHO_KEYS):
        return True
    # 同じ音・語の延々とした繰り返し（「お、お、お、お、お」「解析、解析、解解解…」）。小さな音や雑音での暴走
    if re.fullmatch(r"(.{1,3})\1{3,}", t):
        return True
    return False


def normalize_for_dup(text: str) -> str:
    """同じ文かどうかの比較用。空白・句読点・記号を除き、小文字にそろえる。"""
    return "".join(ch for ch in (text or "") if not (ch.isspace() or unicodedata.category(ch)[0] in "PS")).lower()


_SENT_SPLIT = re.compile(r"[^。？！?!]*[。？！?!]+|[^。？！?!]+")


def collapse_repeats(text: str, min_count: int = 3) -> tuple[str, int]:
    """1つの認識結果の中で、同じ文（句点・？・！区切り）が min_count 回以上出てくるとき、1回（最初の位置）にまとめる。
    戻り値は (まとめたあとの文字, 取り除いた文の数)。該当が無ければ (元の文字, 0)。"""
    t = text or ""
    sents = _SENT_SPLIT.findall(t)
    keys = [normalize_for_dup(s) for s in sents]
    counts: dict[str, int] = {}
    for k in keys:
        if k:
            counts[k] = counts.get(k, 0) + 1
    heavy = {k for k, n in counts.items() if n >= int(min_count)}
    if not heavy:
        return t, 0
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for s, k in zip(sents, keys):
        if k in heavy:
            if k in seen:
                removed += 1
                continue
            seen.add(k)
        out.append(s)
    return "".join(out).strip(), removed


def compose_draft(entries: list, gap_sec: float | None = None, force_gap_sec: float | None = None,
                  edge_pad: float = EDGE_PAD_SEC, newline_after_sentence: bool = False, blank_line: bool = False) -> str:
    """下書きの発話（{"text", "start_sec", "end_sec", "edited"}）の列を、規則に従って1つの文章にする。
    gap_sec が None なら間の規則を使わず従来どおりの連結。
    newline_after_sentence: 各発話の文中の句点系の直後に改行を1つ入れる（手で直した発話には入れない）。
    blank_line: 無音で区切った所（間が実際にある）では、前が句点系で終わっていれば改行2つ（空行）。間が force_gap_sec 以上なら
      句点を補って改行2つ。句点が無いまま区切れたとき（force 未満）は連結（話の途中かもしれない）。
      False のときは従来どおり（gap_sec 以上で句点系なら改行1つ）。最長での強制の区切り（間が 0）は連結。"""
    use_rules = gap_sec is not None
    if use_rules:
        gap_sec = float(gap_sec)
        force = max(gap_sec, float(force_gap_sec if force_gap_sec is not None else gap_sec))
    sep = "\n\n" if blank_line else "\n"
    cur = ""
    prev = None
    for e in entries:
        text = str(e.get("text") or "").strip()
        if not text:
            continue
        if newline_after_sentence and not e.get("edited"):
            text = break_sentences(text)
        if prev is None or not cur:
            cur = text
        else:
            gap = utterance_gap(prev, e, edge_pad) if use_rules else None
            if gap is None:
                cur = _join(cur, text)
            elif gap >= force:
                cur = (cur if prev.get("edited") else _terminate(cur)) + sep + text
            elif blank_line and gap > 0 and ends_sentence(cur):
                cur = cur + sep + text
            elif gap >= gap_sec and ends_sentence(cur):
                cur = cur + "\n" + text
            else:
                cur = _join(cur, text)
        prev = e
    return cur


def _is_emoji(ch: str) -> bool:
    """絵文字・記号（矢印・通貨・その他の記号）。これで終わる文には「。」を補わない。"""
    o = ord(ch)
    return (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0x2B50, 0x2B55, 0xFE0F, 0x200D)
            or unicodedata.category(ch).startswith("S"))


def finalize_text(text: str, punctuate: bool = True) -> str:
    """確定のときの仕上げ。punctuate なら、末尾が句読点で終わっていなければ「。」を補う
    （末尾の「、」は「。」に置き換える。絵文字・記号・閉じ括弧のあとの文末は補わない）。"""
    t = (text or "").strip()
    if not punctuate or not t:
        return t
    if ends_sentence(t):
        return t
    last = t[-1]
    if last in "、,，":
        return t[:-1] + "。"
    if last in _NO_PERIOD_AFTER or _is_emoji(last):
        return t
    return t + "。"
