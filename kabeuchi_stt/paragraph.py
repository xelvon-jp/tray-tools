# -*- coding: utf-8 -*-
"""清書の後処理: 区間のすき間の長さで改行する（純関数・GUI/Qt/COM 非依存）。

履歴に残す元の区間（時刻つき・無加工）は変えない。txt / md / クリップボードの出力にだけ適用する。
規則（直前の区間の終了〜次の区間の開始 = すき間）:
  - すき間 >= force_gap_sec : 常に改行。直前の文末が句点系（。？！）でなければ「。」を補う
  - gap_sec <= すき間 < force_gap_sec : 直前が句点系で終わっているときだけ改行（句点がなければ考え中の沈黙かもしれないので改行しない）
  - すき間 < gap_sec : 何もしない（そのままつなげる）
空の区間は無視する（すき間は、空でない直前の区間の終了から測る）。フィラー（えーと等）の除去はしない。
"""
from __future__ import annotations

# 文末とみなす記号（全角・半角）。… は文を閉じる記号として扱う
SENTENCE_END = "。？！?!．｡…"
# 文末記号のあとに付きうる閉じ括弧・引用符（「はい。」のように閉じていても文末と判定する）
_CLOSERS = "」』）)］]】〕〉》”’\"'"


def ends_sentence(text: str) -> bool:
    """文末が句点系か（閉じ括弧・空白は読み飛ばす）。"""
    t = (text or "").rstrip()
    while t and t[-1] in _CLOSERS:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in SENTENCE_END


def _terminate(text: str) -> str:
    """文末が句点系でなければ「。」を補う（末尾の「、」は「。」に置き換える）。"""
    t = text.rstrip()
    if not t or ends_sentence(t):
        return t
    if t[-1] in "、,，":
        return t[:-1] + "。"
    return t + "。"


def _join(prev: str, nxt: str) -> str:
    """改行しないでつなげる。英数字どうしが隣り合うときだけ空白を入れる。"""
    if prev and nxt and prev[-1].isascii() and prev[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum():
        return prev + " " + nxt
    return prev + nxt


def format_paragraphs(segments: list, gap_sec: float = 2.0, force_gap_sec: float = 3.0, gaps: list | None = None) -> str:
    """(start, end, text) の列を、すき間の規則で段落に分けた全文にする。

    gaps[i] は区間 i の直前の無音の長さ（音声から測った値。gaps.py）。Whisper の区間は無音を区間の中に含めて
    連続して返すことがあり、時刻だけではすき間が 0 に見える。gaps があれば、時刻のすき間と大きいほうを使う。
    """
    if force_gap_sec < gap_sec:  # 設定の取り違えに備えて、強制のほうを大きく保つ
        force_gap_sec = gap_sec
    lines: list[str] = []
    cur = ""
    prev_end: float | None = None
    for idx, seg in enumerate(segments):
        try:
            start, end, text = float(seg[0]), float(seg[1]), str(seg[2] or "").strip()
        except (TypeError, ValueError, IndexError):
            continue
        if not text:
            continue
        if prev_end is None or not cur:
            cur = text
        else:
            gap = start - prev_end
            if gaps is not None and idx < len(gaps) and gaps[idx] is not None:
                gap = max(gap, float(gaps[idx]))
            if gap >= force_gap_sec:
                lines.append(_terminate(cur))
                cur = text
            elif gap >= gap_sec and ends_sentence(cur):
                lines.append(cur)
                cur = text
            else:
                cur = _join(cur, text)
        prev_end = max(end, start)
    if cur:
        lines.append(cur)
    return "\n".join(lines)


def plain_text(segments: list) -> str:
    """整形しない従来の全文（1区間=1行）。"""
    return "\n".join(str(s[2]) for s in segments if str(s[2]).strip())


def format_for_output(segments: list, settings: dict, gaps: list | None = None) -> str:
    """設定に従って全文を作る（paragraph_enabled が偽なら従来どおり）。"""
    if not settings.get("paragraph_enabled", True):
        return plain_text(segments)
    return format_paragraphs(segments, float(settings.get("paragraph_gap_sec", 2.0)),
                             float(settings.get("paragraph_force_gap_sec", 3.0)), gaps)
