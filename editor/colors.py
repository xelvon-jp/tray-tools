# editor/colors.py
# エディタの配色プリセット(Qt に依存しない、色の組の表だけ)。
#
# 1つの配色は、次の色の組:
#   background            本文の背景
#   foreground            本文の文字
#   selection_background  選択範囲の背景
#   current_line          現在行の強調の背景
#   invisible             不可視文字(タブ・スペース・改行)の色
#   line_number_background / line_number_foreground  行番号エリア
#   star_background       星印の行の強調(ハイライタ)の背景
#
# 「秀丸風」は、秀丸エディタの既定の見た目の印象に寄せた組:
# 白い背景に黒い文字、現在行は淡い黄色、不可視文字は水色、
# 選択は淡い青。
DEFAULT_SCHEME = "標準"

COLOR_SCHEMES = {
    "標準": {
        "background": "#ffffff",
        "foreground": "#000000",
        "selection_background": "#cce8ff",
        "current_line": "#eff6ff",
        "invisible": "#c0c0c0",
        "line_number_background": "#f2f2f2",
        "line_number_foreground": "#909090",
        "star_background": "#fff3b0",
    },
    "秀丸風": {
        "background": "#ffffff",
        "foreground": "#000000",
        "selection_background": "#b7dbff",
        "current_line": "#ffffcc",
        "invisible": "#7fb3d5",
        "line_number_background": "#f0f0f0",
        "line_number_foreground": "#606060",
        "star_background": "#ffe699",
    },
}


def scheme_names() -> list:
    """プリセットの名前の並び(メニューの表示順)。"""
    return list(COLOR_SCHEMES)


def get_scheme(name) -> dict:
    """名前の配色の組(写し)。知らない名前なら既定(標準)。"""
    return dict(COLOR_SCHEMES.get(name) or COLOR_SCHEMES[DEFAULT_SCHEME])
