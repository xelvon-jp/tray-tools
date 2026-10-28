# tests/test_ocr_win.py
# ocr_win.py の、WinRT に頼らない部分(言語の選択・入口の出し分け)を確かめる。
# 実際の認識は Windows 実機でしか確かめられない。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_ocr_win.py
#
# pytest が無くても動くように、素の assert と自前の小さな実行器で書いてある
# (tests/test_screen_mirror.py と同じ形)。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ocr_win  # noqa: E402


def test_choose_language_prefers_japanese():
    """使える言語から日本語を優先し、無ければ先頭、空なら None。"""
    choose = ocr_win.choose_language
    assert choose(["en-US", "ja-JP"]) == "ja-JP"
    assert choose(["ja"]) == "ja"
    assert choose(["en-US", "fr-FR"]) == "en-US"
    assert choose([]) is None
    assert choose(None) is None
    assert choose(["", "en-US"]) == "en-US"


def test_available_matches_setup_probe():
    """available() は bool を返し、例外を出さない(入口の出し分けに使う)。"""
    assert isinstance(ocr_win.available(), bool)


def test_needs_invert():
    """地の明るさの平均が半分未満なら暗い地とみなして反転する。"""
    assert ocr_win.needs_invert(20.0) is True
    assert ocr_win.needs_invert(127.9) is True
    assert ocr_win.needs_invert(128.0) is False
    assert ocr_win.needs_invert(240.0) is False


def test_ocr_scale():
    """短辺 1600px 未満は広げ(最大3倍)、上限 10000px は超えない。"""
    scale = ocr_win.ocr_scale
    assert scale(800, 600) == 1600 / 600          # 短辺を 1600 へ
    assert scale(100, 100) == 3.0                  # 上限は3倍
    assert scale(2400, 1600) == 1.0                # 十分大きい
    assert scale(2000, 12000) == 10000 / 12000     # 上限に合わせて縮小
    assert scale(9000, 9000) == 1.0
    assert scale(0, 0) == 1.0


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
