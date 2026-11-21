# -*- coding: utf-8 -*-
"""本人のデータ（履歴DB・置換辞書・プリセット・ログ）の置き場。

公開リポジトリに入れないため %LOCALAPPDATA%/kabeuchi_stt/ に置く。
環境変数 KABEUCHI_DATA_DIR があればそちらを使う（テスト用）。
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("KABEUCHI_DATA_DIR")
    if env:
        base = Path(env)
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "kabeuchi_stt"
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_text_lf(path: Path, text: str) -> None:
    """UTF-8・LF で書く（Windows の改行変換を避ける）。"""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
