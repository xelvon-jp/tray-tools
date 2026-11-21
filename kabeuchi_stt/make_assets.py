# -*- coding: utf-8 -*-
"""アプリアイコン（assets\\kabeuchi_stt.ico / .png）と、画面の矢印 SVG（assets\\ui\\）を作る。

    python make_assets.py

マイクをモチーフにした絵を Qt で描いて保存する（窓は出さない）。アイコンの絵を変えたときだけ実行すればよい。
ショートカット（make_shortcut.py）はこの .ico を使う。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # 描画だけ。窓は作らない
    import ui_style as S  # 矢印の SVG もここで assets\ui に出力される

    png, ico = S.write_icon_files(S.ASSETS_DIR)
    print("saved", png, f"({png.stat().st_size} bytes)")
    print("saved", ico, f"({ico.stat().st_size} bytes)")
    print("arrows", sorted(p.name for p in (S.ASSETS_DIR / "ui").glob("*.svg")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
