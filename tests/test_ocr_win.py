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
