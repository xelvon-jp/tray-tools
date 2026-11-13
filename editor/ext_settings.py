# editor/ext_settings.py
# 拡張子別の設定(Qt に依存しない、設定の対応表と解決だけ)。
#
# 持ち方は色の設定と同じ「全体+上書き」の考え方: 設定は editor 節の
# extension_settings(拡張子 → 項目 → 値の対応)で、上書きの無い項目
# は全体の設定(wrap_mode / wrap_column / markdown_highlight /
# font_family / font_size / tab_width)に追随する。実効の組は
# resolve で作る。設定画面(editor/ext_settings_dialog.py)が項目の
# 一覧(ITEMS)と、この解決を使う。
#
# 拡張子別にできる項目(既存の全体設定のうち、文書の種類で変えたく
# なるものに限った):
#   wrap_mode          折り返しの方式(column / window / none)
#   wrap_column        折り返しの桁数
#   markdown_highlight Markdown の色付けの有効・無効
#   font_family        フォント名
#   font_size          フォントの大きさ(pt)
#   tab_width          タブの表示幅(半角の文字数。全体の既定は None
#                      = Qt の既定のまま触らない)
#
# タブキーでスペースを入れる等の「タブとスペースの扱い」は、既存の
# 設定が無い(タブは常にタブ文字)うえ、文書の内容を変える新しい
# 編集の挙動になるので、この仕組みの対象には含めていない。
import os
import re

# 折り返しの方式の値域(editor/window.py の WRAP_MODES と一致させる。
# window 側を import すると循環するので、こちらにも写しを持ち、
# 一致はテストで見張る)。
WRAP_MODES = ("column", "window", "none")

WRAP_COLUMN_MIN = 8
WRAP_COLUMN_MAX = 1000
FONT_SIZE_MIN = 6
FONT_SIZE_MAX = 72
FONT_FAMILY_MAX = 128
TAB_WIDTH_MIN = 1
TAB_WIDTH_MAX = 16

# 設定画面に出す項目の並び(項目名, 表示名)。
ITEMS = (
    ("wrap_mode", "折り返しの方式"),
    ("wrap_column", "折り返しの桁数"),
    ("markdown_highlight", "Markdown の色付け"),
    ("font_family", "フォント名"),
    ("font_size", "フォントの大きさ"),
    ("tab_width", "タブの幅"),
)

ITEM_KEYS = frozenset(key for key, _label in ITEMS)

# 設定画面の拡張子の候補(よくあるもの)。開いているタブや設定済みの
# 拡張子は、窓側がこれへ足して渡す。
COMMON_EXTENSIONS = (
    "txt", "md", "py", "ps1", "log", "json", "csv", "ini",
    "html", "css", "js", "xml", "yaml", "yml", "bat", "sh",
)

_EXT_RX = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_extension(value) -> str:
    """拡張子の表記を揃える(小文字・先行ドット無し)。

    使えない表記(空・記号混じり等)は "" を返す。"""
    if not isinstance(value, str):
        return ""
    ext = value.strip().lower().lstrip(".")
    return ext if _EXT_RX.match(ext) else ""


def extension_of(path) -> str:
    """パスから拡張子(小文字・ドット無し)を取る。無ければ ""。"""
    if not path:
        return ""
    return normalize_extension(os.path.splitext(str(path))[1])


def _clean_int(value, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def sanitize_item(key, value):
    """1項目の値を確かめて、使える形ならその値、駄目なら None。

    tab_width の None は「全体の既定(Qt の既定)」として意味のある
    値なので、呼び側は戻り値が None のとき「不正」と「既定」の
    区別が要るなら、事前に None かどうかを見てから呼ぶこと。"""
    if key == "wrap_mode":
        return value if value in WRAP_MODES else None
    if key == "wrap_column":
        return _clean_int(value, WRAP_COLUMN_MIN, WRAP_COLUMN_MAX)
    if key == "markdown_highlight":
        return value if isinstance(value, bool) else None
    if key == "font_family":
        if isinstance(value, str) and value.strip():
            return value.strip()[:FONT_FAMILY_MAX]
        return None
    if key == "font_size":
        return _clean_int(value, FONT_SIZE_MIN, FONT_SIZE_MAX)
    if key == "tab_width":
        if value is None:
            return None
        return _clean_int(value, TAB_WIDTH_MIN, TAB_WIDTH_MAX)
    return None


def sanitize_tab_width(value):
    """全体のタブ幅の設定値。None は Qt の既定(触らない)。"""
    if value is None:
        return None
    return _clean_int(value, TAB_WIDTH_MIN, TAB_WIDTH_MAX)


def sanitize(settings) -> dict:
    """拡張子 → 項目 → 値の対応を、使えるものだけに絞る。

    設定ファイルは手で書き換えられることもあるので、不正な拡張子・
    項目・値は黙って捨てる(窓が開かなくなるよりまし)。上書きが
    1つも残らなかった拡張子は、対応ごと落とす。並びは拡張子・
    項目とも ITEMS と拡張子の辞書順に揃える(保存した姿が安定する)。"""
    if not isinstance(settings, dict):
        return {}
    clean = {}
    for raw_ext in sorted(settings, key=lambda v: str(v)):
        ext = normalize_extension(raw_ext)
        items = settings.get(raw_ext)
        if not ext or ext in clean or not isinstance(items, dict):
            continue
        kept = {}
        for key, _label in ITEMS:
            if key not in items:
                continue
            value = items[key]
            if key == "tab_width" and value is None:
                continue  # 上書きで「既定」は表せない(全体に戻すで表す)
            cleaned = sanitize_item(key, value)
            if cleaned is not None:
                kept[key] = cleaned
        if kept:
            clean[ext] = kept
    return clean


def resolve(global_values, settings, ext) -> dict:
    """全体の値に、その拡張子の上書きを重ねた実効の組。

    global_values は6項目すべての辞書(tab_width は None でもよい)。
    上書きの無い項目は全体の値のまま(全体を変えたら追随する)。
    設定の対応は sanitize 済みの前提だが、解決の時点でも不正な
    上書きは落とす(全体の値が壊れないように)。"""
    effective = {key: global_values.get(key) for key, _label in ITEMS}
    overrides = (settings or {}).get(normalize_extension(ext)) or {}
    for key, _label in ITEMS:
        if key not in overrides:
            continue
        if key == "tab_width" and overrides[key] is None:
            continue
        cleaned = sanitize_item(key, overrides[key])
        if cleaned is not None:
            effective[key] = cleaned
    return effective
