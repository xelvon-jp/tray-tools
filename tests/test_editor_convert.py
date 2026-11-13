# tests/test_editor_convert.py
# 選んだ範囲の文字変換(editor/convert.py、Qt 非依存)を確かめる。
# 全角・半角(英数字・記号・スペース・カタカナ。濁点の合成と分解を
# 含む)と、英字の大文字・小文字。NFKC のような、頼んでいない分解が
# 起きないことも見る。
#
#   & "$env:USERPROFILE\.venvs\tray-tools\Scripts\python.exe" tests\test_editor_convert.py
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from editor import convert  # noqa: E402


def test_halfwidth_ascii_digits_and_symbols():
    assert convert.to_halfwidth("ＡＢＣａｂｃ１２３") == "ABCabc123"
    assert convert.to_halfwidth("！＂＃＄％＆＇（）＊＋，－．／") == "!\"#$%&'()*+,-./"
    assert convert.to_halfwidth("：；＜＝＞？＠［＼］＾＿｀｛｜｝～") == ":;<=>?@[\\]^_`{|}~"


def test_halfwidth_space():
    assert convert.to_halfwidth("あ　い") == "あ い"
    assert convert.to_fullwidth("あ い") == "あ　い"


def test_halfwidth_katakana():
    assert convert.to_halfwidth("アイウエオ") == "ｱｲｳｴｵ"
    assert convert.to_halfwidth("カタカナ") == "ｶﾀｶﾅ"
    assert convert.to_halfwidth("ャュョッー") == "ｬｭｮｯｰ"
    assert convert.to_halfwidth("ヲ") == "ｦ"
    assert convert.to_halfwidth("、。・") == "､｡･"


def test_halfwidth_voiced_katakana_splits():
    # 濁点・半濁点付きは「半角カナ + ﾞ / ﾟ」の2文字になる。
    assert convert.to_halfwidth("ガギグゲゴ") == "ｶﾞｷﾞｸﾞｹﾞｺﾞ"
    assert convert.to_halfwidth("ザジズゼゾ") == "ｻﾞｼﾞｽﾞｾﾞｿﾞ"
    assert convert.to_halfwidth("ダヂヅデド") == "ﾀﾞﾁﾞﾂﾞﾃﾞﾄﾞ"
    assert convert.to_halfwidth("バビブベボ") == "ﾊﾞﾋﾞﾌﾞﾍﾞﾎﾞ"
    assert convert.to_halfwidth("パピプペポ") == "ﾊﾟﾋﾟﾌﾟﾍﾟﾎﾟ"
    assert convert.to_halfwidth("ヴ") == "ｳﾞ"
    assert convert.to_halfwidth("ヷ") == "ﾜﾞ"


def test_fullwidth_katakana_and_compose():
    assert convert.to_fullwidth("ｱｲｳｴｵ") == "アイウエオ"
    assert convert.to_fullwidth("ｶﾀｶﾅ") == "カタカナ"
    # 半角カナ + ﾞ / ﾟ は、濁点・半濁点付きの1文字に合成される。
    assert convert.to_fullwidth("ｶﾞｷﾞｸﾞｹﾞｺﾞ") == "ガギグゲゴ"
    assert convert.to_fullwidth("ﾊﾟﾋﾟﾌﾟﾍﾟﾎﾟ") == "パピプペポ"
    assert convert.to_fullwidth("ｳﾞ") == "ヴ"
    # 単独の ﾞ / ﾟ は全角の独立字になる。
    assert convert.to_fullwidth("ﾞ") == "゛"
    assert convert.to_fullwidth("ﾟ") == "゜"
    # 合成できない組み合わせ(ｱﾞ)は、そのまま並ぶ。
    assert convert.to_fullwidth("ｱﾞ") == "ア゛"


def test_width_leaves_other_characters_alone():
    # ひらがな・漢字は対象外。NFKC なら化ける ① や ㈱ も変えない。
    assert convert.to_halfwidth("ひらがな漢字") == "ひらがな漢字"
    assert convert.to_fullwidth("ひらがな漢字") == "ひらがな漢字"
    assert convert.to_halfwidth("①㈱") == "①㈱"
    assert convert.to_fullwidth("①㈱") == "①㈱"
    # 半角に元字の無いヸ・ヹ(ヰ・ヱの半角が無い)は残る。
    assert convert.to_halfwidth("ヸヹ") == "ヸヹ"


def test_width_roundtrip():
    half = "ABC abc 123 ｶﾀｶﾅ ｶﾞ ﾊﾟ !?"
    assert convert.to_halfwidth(convert.to_fullwidth(half)) == half
    full = "ＡＢＣ　アイウ　ガ　パ　！"
    assert convert.to_fullwidth(convert.to_halfwidth(full)) == full


def test_case_conversion_ascii_only():
    assert convert.to_lower("AbC-XyZ") == "abc-xyz"
    assert convert.to_upper("AbC-xYz") == "ABC-XYZ"
    # 英字以外(全角英字・数字・かな)は変えない。
    assert convert.to_lower("ＡＢＣ１２３") == "ＡＢＣ１２３"
    assert convert.to_upper("ａｂｃ１２３") == "ａｂｃ１２３"
    assert convert.to_lower("") == ""


def main():
    tests = [(name, fn) for name, fn in globals().items()
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {name}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.stdout.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
