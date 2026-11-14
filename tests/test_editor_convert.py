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


def test_tab_to_space_stops():
    # タブは次のタブ位置まで広がる(タブ幅4)。
    assert convert.tab_to_space("\tx", 4) == "    x"
    assert convert.tab_to_space("a\tx", 4) == "a   x"
    assert convert.tab_to_space("ab\tx", 4) == "ab  x"
    assert convert.tab_to_space("abc\tx", 4) == "abc x"
    # タブ位置ちょうどにあるタブは、タブ幅ぶん進む。
    assert convert.tab_to_space("abcd\tx", 4) == "abcd    x"
    # 2つ目以降のタブも、その位置から数える。
    assert convert.tab_to_space("a\tb\tc", 4) == "a   b   c"
    # タブ幅8でも同じ考え方。
    assert convert.tab_to_space("\tx", 8) == "        x"
    assert convert.tab_to_space("a\tx", 8) == "a       x"
    # タブが無ければそのまま。
    assert convert.tab_to_space("abc", 4) == "abc"
    assert convert.tab_to_space("", 4) == ""


def test_tab_to_space_fullwidth_columns():
    # 桁は表示幅(全角は2)で数えるので、全角の後ろのタブは短い。
    assert convert.tab_to_space("あ\tx", 4) == "あ  x"
    assert convert.tab_to_space("あa\tx", 4) == "あa x"
    assert convert.tab_to_space("あい\tx", 4) == "あい    x"
    # 全角スペース自体は変換されず、幅2として数えられる。
    assert convert.tab_to_space("　\tx", 4) == "　  x"


def test_space_to_tab_leading_only():
    # 行頭のスペースは、タブ位置にそろう分だけタブになる。
    assert convert.space_to_tab("    x", 4) == "\tx"
    assert convert.space_to_tab("        x", 4) == "\t\tx"
    # タブ位置まで届かない余りはスペースのまま残る。
    assert convert.space_to_tab("      x", 4) == "\t  x"
    assert convert.space_to_tab("   x", 4) == "   x"
    assert convert.space_to_tab("x", 4) == "x"
    assert convert.space_to_tab("", 4) == ""
    assert convert.space_to_tab("    ", 4) == "\t"


def test_space_to_tab_leaves_inner_spaces():
    # 文中のスペースは触らない(行頭だけが対象)。
    assert convert.space_to_tab("x    y", 4) == "x    y"
    assert convert.space_to_tab("  x    y", 4) == "  x    y"
    assert convert.space_to_tab("あ    い", 4) == "あ    い"


def test_space_to_tab_keeps_existing_tabs():
    # 既にあるタブはそのまま通る。
    assert convert.space_to_tab("\tx", 4) == "\tx"
    assert convert.space_to_tab("  \tx", 4) == "  \tx"
    # タブの後ろのスペースは、次のタブ位置から数える。
    assert convert.space_to_tab("\t    x", 4) == "\t\tx"
    assert convert.space_to_tab("\t  x", 4) == "\t  x"
    assert convert.space_to_tab("\t      x", 4) == "\t\t  x"


def test_space_to_tab_roundtrip_keeps_shape():
    # タブ化 → 展開すると、元の行頭の形(タブを展開した形)に戻る。
    for line in ("    x", "      x", "  \tx", "\t  x", "        ",
                 " x", "x"):
        assert convert.tab_to_space(
            convert.space_to_tab(line, 4), 4) == \
            convert.tab_to_space(line, 4)


def test_lead_half_to_full():
    assert convert.lead_half_to_full("    x") == "　　x"
    assert convert.lead_half_to_full("  x") == "　x"
    # 奇数個は、全角 + 半角1個(余り)になる。
    assert convert.lead_half_to_full("   x") == "　 x"
    assert convert.lead_half_to_full(" x") == " x"
    # 混ざっていても幅を保って組み直す(全角1個 + 半角2個 → 全角2個)。
    assert convert.lead_half_to_full("　  x") == "　　x"
    assert convert.lead_half_to_full("\u0020\u0020\u0020\u3000x") == "\u3000\u3000\u0020x"
    assert convert.lead_half_to_full("x") == "x"
    assert convert.lead_half_to_full("") == ""


def test_lead_full_to_half():
    assert convert.lead_full_to_half("　　x") == "    x"
    assert convert.lead_full_to_half("　x") == "  x"
    # 混ざっていても、幅のぶんだけ半角スペースになる。
    assert convert.lead_full_to_half("　 x") == "   x"
    assert convert.lead_full_to_half("\u0020\u0020\u0020\u3000x") == "\u0020\u0020\u0020\u0020\u0020x"
    assert convert.lead_full_to_half("  x") == "  x"
    assert convert.lead_full_to_half("x") == "x"
    assert convert.lead_full_to_half("") == ""


def test_lead_conversions_leave_inner_spaces():
    # 行頭以外のスペースは、全角・半角とも触らない。
    assert convert.lead_half_to_full("x    y") == "x    y"
    assert convert.lead_full_to_half("x　　y") == "x　　y"
    assert convert.lead_half_to_full("  x  y") == "　x  y"
    # タブが行頭にあれば、スペースの頭の部分はそこで終わる。
    assert convert.lead_half_to_full("\t  x") == "\t  x"
    assert convert.lead_half_to_full("  \tx") == "　\tx"
    assert convert.lead_full_to_half("\t　x") == "\t　x"


def test_lead_space_roundtrip_keeps_width():
    # 行頭スペースの幅(半角1・全角2の単位数)は、往復しても保たれる。
    for line in ("    x", "   x", "　  x", "  　x", " x", "　x", "x"):
        assert convert.lead_full_to_half(
            convert.lead_half_to_full(line)) == \
            convert.lead_full_to_half(line)
    # 奇数個の余り(半角1個)も往復で消えない。
    assert convert.lead_full_to_half(
        convert.lead_half_to_full("   x")) == "   x"


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
