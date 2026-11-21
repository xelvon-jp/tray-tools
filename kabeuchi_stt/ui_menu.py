# -*- coding: utf-8 -*-
"""文字起こし部分の右クリックメニューの「項目」を組み立てる（Qt 非依存・純関数）。

項目のリストを返す関数と、メニューを実際に表示する処理（ui_main 側。差し替え可能）を分けてあるので、
テストでは表示せずに項目の中身を検証できる。
"""
from __future__ import annotations

from dataclasses import dataclass

SEP = "-"


@dataclass
class MenuItem:
    key: str                # copy_selection / copy_part / copy_all / copy_from_here / play_from / "-"（区切り）
    label: str = ""
    enabled: bool = True
    payload: str = ""       # 即コピーできる文字（copy_part / copy_selection）。全文・ここから下は実行時に作る
    index: int = -1         # 対象の行（段落）番号


def is_text_row(r) -> bool:
    """文字のある行（仮認識・清書済み）か。波形だけ・無音・一時停止の印は False。"""
    return (getattr(r, "kind", "audio") == "audio" and getattr(r, "state", "") in ("draft", "final")
            and bool((getattr(r, "text", "") or "").strip()))


def rows_text_from(rows: list, idx: int = 0) -> str:
    """idx 行から最後までの、文字のある行を時刻順に連結（行ごとに改行）。"""
    return "\n".join(r.text.strip() for r in rows[max(idx, 0):] if is_text_row(r))


def timeline_menu(rows: list, idx: int, selection: str = "") -> list[MenuItem]:
    """タイムラインの行を右クリックしたときの項目。波形・無音・印の行では、その行に関する項目は出さない。"""
    items: list[MenuItem] = []
    r = rows[idx] if 0 <= idx < len(rows) else None
    text_row = r is not None and is_text_row(r)
    any_text = any(is_text_row(x) for x in rows)
    if selection.strip():
        items.append(MenuItem("copy_selection", "選択部分をコピー", payload=selection))
    if text_row:
        items.append(MenuItem("copy_part", "このパートをコピー", payload=r.text.strip(), index=idx))
    if any_text:
        items.append(MenuItem("copy_all", "全文コピー"))
    if text_row:
        items.append(MenuItem("copy_from_here", "ここから下をコピー", index=idx))
    return items


def history_menu(blocks: list[str], idx: int, selection: str = "", can_play: bool = False) -> list[MenuItem]:
    """履歴の閲覧画面（1 区間 = 1 段落）の本文を右クリックしたときの項目。"""
    items: list[MenuItem] = []
    have = 0 <= idx < len(blocks) and bool(blocks[idx].strip())
    if selection.strip():
        items.append(MenuItem("copy_selection", "選択部分をコピー", payload=selection))
    if have:
        items.append(MenuItem("copy_part", "このパートをコピー", payload=blocks[idx].strip(), index=idx))
    if any(b.strip() for b in blocks):
        items.append(MenuItem("copy_all", "全文コピー"))
    if have:
        items.append(MenuItem("copy_from_here", "ここから下をコピー", index=idx))
    if have and can_play:
        items.append(MenuItem(SEP))
        items.append(MenuItem("play_from", "ここから再生", index=idx))
    return items


COPIED_MESSAGE = {
    "copy_selection": "コピーしました（選択部分）",
    "copy_part": "コピーしました（このパート）",
    "copy_all": "コピーしました（全文）",
    "copy_from_here": "コピーしました（ここから下）",
}
