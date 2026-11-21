# -*- coding: utf-8 -*-
"""Copilot 用プロンプトのプリセット（名前＋本文）。GUI/Qt/COM 非依存。

「送る」＝プロンプト＋文字起こしを1つのテキストにしてクリップボードへ（貼るのは本人）。
保存先: %LOCALAPPDATA%/kabeuchi_stt/presets.json
"""
from __future__ import annotations

import json
from pathlib import Path

from paths import data_dir, write_text_lf

SEED_PRESETS = [
    {"name": "要約3行＋ToDo",
     "prompt": "次の文字起こし（独り言の壁打ち）を、(1)3行の要約、(2)ToDo の箇条書き、にまとめてください。"
               "言い淀みや重複は整理し、書かれていないことは補わないでください。"},
    {"name": "論点整理",
     "prompt": "次の文字起こしから論点を抜き出し、「論点／いまの考え／未決のこと」の形で整理してください。"
               "矛盾や飛躍があれば指摘してください。"},
    {"name": "壁打ちへの返事",
     "prompt": "次は私の壁打ち（独り言）の文字起こしです。壁打ち相手として、気づいた点・抜けている視点・"
               "次に考えるとよい問いを、簡潔に返してください。"},
]

SEPARATOR = "-----"


def presets_path() -> Path:
    return data_dir() / "presets.json"


def load_presets(path: Path | None = None) -> list[dict]:
    path = path or presets_path()
    if not path.exists():
        save_presets([dict(p) for p in SEED_PRESETS], path)
        return [dict(p) for p in SEED_PRESETS]
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        items = d.get("presets", []) if isinstance(d, dict) else []
        return [{"name": str(p["name"]), "prompt": str(p.get("prompt", ""))}
                for p in items if isinstance(p, dict) and p.get("name")]
    except Exception:
        return []


def save_presets(presets: list[dict], path: Path | None = None) -> None:
    path = path or presets_path()
    clean = [{"name": str(p["name"]), "prompt": str(p.get("prompt", ""))} for p in presets if str(p.get("name", "")).strip()]
    write_text_lf(path, json.dumps({"presets": clean}, ensure_ascii=False, indent=2) + "\n")


def build_prompt(prompt: str, transcript: str) -> str:
    """プロンプト＋文字起こしを1つにする。区切り線と見出しで境界を明示する。"""
    prompt = (prompt or "").strip()
    transcript = (transcript or "").strip()
    parts = []
    if prompt:
        parts.append(prompt)
    parts.append(f"{SEPARATOR}\n以下が文字起こし\n{transcript}\n{SEPARATOR}" if prompt else transcript)
    return "\n\n".join(parts) + "\n"
