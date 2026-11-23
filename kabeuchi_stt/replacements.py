# -*- coding: utf-8 -*-
"""自動置換辞書（「誤認識 → 正解」）。GUI/Qt/COM 非依存。

適用ルール（衝突・無限ループを避けるため）:
- 1 パスのみ。置換結果に対して再び置換はしない（a→b, b→c でも a は b になる）。
- 同じ位置で複数の誤認識が当たる場合は、長いものを優先（「一時解析」が「解析」に勝つ）。
- 同じ誤認識が重複登録されたら、後に登録したものが勝つ。空の誤認識は無視。
保存先: %LOCALAPPDATA%/kabeuchi_stt/replacements.json（公開リポジトリに入れない）。
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from paths import data_dir, write_text_lf

SEED_PAIRS = [
    {"from": "一時解析", "to": "一次解析"},
    {"from": "不線", "to": "付箋"},
]


def dict_path() -> Path:
    return data_dir() / "replacements.json"


def load_pairs(path: Path | None = None) -> list[dict]:
    path = path or dict_path()
    if not path.exists():
        save_pairs([dict(p) for p in SEED_PAIRS], path)
        return [dict(p) for p in SEED_PAIRS]
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        pairs = d.get("pairs", []) if isinstance(d, dict) else []
        return [{"from": str(p["from"]), "to": str(p["to"])} for p in pairs
                if isinstance(p, dict) and "from" in p and "to" in p]
    except Exception:
        return []


def save_pairs(pairs: list[dict], path: Path | None = None) -> None:
    path = path or dict_path()
    clean = [{"from": str(p["from"]), "to": str(p["to"])} for p in pairs if str(p.get("from", "")) != ""]
    write_text_lf(path, json.dumps({"pairs": clean}, ensure_ascii=False, indent=2) + "\n")


class Replacer:
    def __init__(self, pairs: list[dict] | None = None):
        self.set_pairs(pairs or [])

    def set_pairs(self, pairs: list[dict]) -> None:
        m: dict[str, str] = {}
        for p in pairs:
            k = str(p.get("from", ""))
            if k:
                m[k] = str(p.get("to", ""))  # 後勝ち
        self._map = m
        keys = sorted(m, key=lambda k: (-len(k), k))
        self._re = re.compile("|".join(re.escape(k) for k in keys)) if keys else None

    def apply(self, text: str) -> str:
        if not self._re or not text:
            return text
        return self._re.sub(lambda mo: self._map[mo.group(0)], text)

    def __len__(self) -> int:
        return len(self._map)


def suggest_replacements(original: str, edited: str, max_len: int = 20) -> list[tuple[str, str]]:
    """元の文と修正後の文の差分から、「誤認識 → 正解」の候補を出す（置換辞書への登録用）。

    文字単位の replace だけを拾う（純挿入・純削除は辞書にしづらいので除外）。
    隣り合う変更は句読点・空白をまたがないよう、行ごとに比較する。
    """
    out: list[tuple[str, str]] = []
    seen = set()
    o_lines, e_lines = original.split("\n"), edited.split("\n")
    pairs = zip(o_lines, e_lines) if len(o_lines) == len(e_lines) else [(original, edited)]
    for o, e in pairs:
        sm = difflib.SequenceMatcher(None, o, e, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "replace":
                continue
            a, b = o[i1:i2].strip(), e[j1:j2].strip()
            if not a or not b or a == b or len(a) > max_len or len(b) > max_len:
                continue
            if "\n" in a or "\n" in b or (a, b) in seen:
                continue
            seen.add((a, b))
            out.append((a, b))
    return out
