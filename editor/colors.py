# editor/colors.py
# エディタの配色プリセット(Qt に依存しない、色の組の表だけ)。
#
# 配色の持ち方は「プリセット+上書き」(2026-11-13 にアオイと合意)。
# 設定は editor 節の color_preset(プリセット名)と color_overrides
# (項目名 → 色の対応)で、上書きの無い項目はプリセットの値に追随する。
# 実効の組は resolve_scheme で作る。色の設定画面(editor/color_dialog.py)
# が項目の一覧(COLOR_ITEMS)と、この解決を使う。
#
# 1つの配色は、次の色の組:
#   background            本文の背景
#   foreground            本文の文字
#   selection_background  選択範囲の背景
#   current_line          現在行の強調の背景
#   invisible             不可視文字(タブ・スペース・改行)の色
#   line_number_background / line_number_foreground  行番号エリア
#   line_number_edited_foreground  編集マーク(開いて・保存してから
#     編集した行)の行番号の色
#   star_background       星印の行の強調(ハイライタ)の背景
#   heading1_background 〜 heading6_background
#                         Markdown 見出しの背景(階層が深いほど薄い)
#   code_block_background Markdown コードブロックの背景
#   inline_code_background / inline_code_foreground
#                         Markdown 行内コードの背景と文字
#   link_foreground       Markdown リンクの文字
#   quote_foreground      Markdown 引用の文字
#   bullet_foreground     Markdown 箇条書き記号の文字
#
# 「秀丸風」は、秀丸エディタの既定の見た目の印象に寄せた組:
# 白い背景に黒い文字、現在行は淡い黄色、不可視文字は水色、
# 選択は淡い青。Markdown の色付けは EDDy 独自のものなので、
# 秀丸風でも標準と同じ値を持たせてある(プリセットで差は無い)。
import re

DEFAULT_SCHEME = "標準"

# Markdown の色(明るい背景前提の、落ち着いた色。ハイライタ側に
# あった定数をここへ寄せた。見出しは階層が深いほど背景を薄くする)。
_MARKDOWN_COLORS = {
    "heading1_background": "#d9e2f3",
    "heading2_background": "#e2eaf6",
    "heading3_background": "#eaf0f9",
    "heading4_background": "#eff4fb",
    "heading5_background": "#f3f7fc",
    "heading6_background": "#f6f9fd",
    "code_block_background": "#f5f5f5",
    "inline_code_background": "#f0f0f0",
    "inline_code_foreground": "#a4262c",
    "link_foreground": "#0563c1",
    "quote_foreground": "#5a7d5a",
    "bullet_foreground": "#c55a11",
}

COLOR_SCHEMES = {
    "標準": {
        "background": "#ffffff",
        "foreground": "#000000",
        "selection_background": "#cce8ff",
        "current_line": "#eff6ff",
        "invisible": "#c0c0c0",
        "line_number_background": "#f2f2f2",
        "line_number_foreground": "#909090",
        "line_number_edited_foreground": "#e07020",
        "star_background": "#fff3b0",
        **_MARKDOWN_COLORS,
    },
    "秀丸風": {
        "background": "#ffffff",
        "foreground": "#000000",
        "selection_background": "#b7dbff",
        "current_line": "#ffffcc",
        "invisible": "#7fb3d5",
        "line_number_background": "#f0f0f0",
        "line_number_foreground": "#606060",
        "line_number_edited_foreground": "#d06000",
        "star_background": "#ffe699",
        **_MARKDOWN_COLORS,
    },
}

# 色の設定画面に出す項目の並び(項目名, 表示名)。
COLOR_ITEMS = (
    ("background", "本文の背景"),
    ("foreground", "本文の文字"),
    ("selection_background", "選択範囲の背景"),
    ("current_line", "現在行の背景"),
    ("invisible", "不可視文字"),
    ("line_number_background", "行番号の背景"),
    ("line_number_foreground", "行番号の文字"),
    ("line_number_edited_foreground", "編集マーク（行番号）"),
    ("star_background", "星印行の背景"),
    ("heading1_background", "Markdown 見出し1の背景"),
    ("heading2_background", "Markdown 見出し2の背景"),
    ("heading3_background", "Markdown 見出し3の背景"),
    ("heading4_background", "Markdown 見出し4の背景"),
    ("heading5_background", "Markdown 見出し5の背景"),
    ("heading6_background", "Markdown 見出し6の背景"),
    ("code_block_background", "Markdown コードブロックの背景"),
    ("inline_code_background", "Markdown 行内コードの背景"),
    ("inline_code_foreground", "Markdown 行内コードの文字"),
    ("link_foreground", "Markdown リンクの文字"),
    ("quote_foreground", "Markdown 引用の文字"),
    ("bullet_foreground", "Markdown 箇条書き記号の文字"),
)

ITEM_KEYS = frozenset(key for key, _label in COLOR_ITEMS)

_COLOR_VALUE_RX = re.compile(r"^#[0-9a-fA-F]{6}$")


def scheme_names() -> list:
    """プリセットの名前の並び(メニューの表示順)。"""
    return list(COLOR_SCHEMES)


def get_scheme(name) -> dict:
    """名前の配色の組(写し)。知らない名前なら既定(標準)。"""
    return dict(COLOR_SCHEMES.get(name) or COLOR_SCHEMES[DEFAULT_SCHEME])


def sanitize_overrides(overrides) -> dict:
    """上書きの対応を、知っている項目と正しい色(#rrggbb)だけに絞る。

    設定ファイルは手で書き換えられることもあるので、不正な項目や
    色は黙って捨てる(窓が開かなくなるよりまし)。色は小文字に揃える。
    並びは COLOR_ITEMS の順に揃える(保存した姿が安定する)。"""
    if not isinstance(overrides, dict):
        return {}
    clean = {}
    for key, _label in COLOR_ITEMS:
        value = overrides.get(key)
        if isinstance(value, str) and _COLOR_VALUE_RX.match(value):
            clean[key] = value.lower()
    return clean


def resolve_scheme(name, overrides=None) -> dict:
    """プリセットに上書きを重ねた、実効の配色の組。

    上書きの無い項目はプリセットの値のまま(プリセットを切り替えたら
    追随する)。上書きした項目は、プリセットを変えてもそのまま残る。"""
    scheme = get_scheme(name)
    scheme.update(sanitize_overrides(overrides))
    return scheme
