# photometa/config.py
# 写真の★の共通設定 %LOCALAPPDATA%\traytools\photo_ratings.json（あっふと PhotoCuller が読む）と、
# それから作る写真メニュー（PhotoCuller の I キー）・絞り込みの小メニュー（PhotoCuller の I → /）の中身。
# あっふは★の表示名（rating_name / rating_badge / rating_short / range_label）と range_expr を使う。
#
# 設定は辞書のまま扱う。読み込み時に normalize して、壊れた値は既定に戻す（項目ごと）。
# 知らないキーは残す（後で項目が増えても、古い版が書き戻して消さないように）。
# このファイルは photometa 専用なので、保存は丸ごと書いてよい（一時ファイル→置き換え）。
#
# 保存先は CONFIG_PATH。テストはこのモジュール変数を一時フォルダに差し替える
# （環境変数 LOCALAPPDATA は差し替えないこと）。環境変数 TRAYTOOLS_DATA_DIR があれば
# そこを使う（あっふの他の記録と同じ。tests/test_filer.py などが一時フォルダへ向けている）。
from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass

DATA_DIR_ENV = "TRAYTOOLS_DATA_DIR"


def data_dir() -> str:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "traytools")


CONFIG_PATH = os.path.join(data_dir(), "photo_ratings.json")

# 写真メニューで、段階が無い★に付ける行（★2・★1。頭の文字は★の数）
MENU_PLAIN_RATINGS = (2, 1)
# 写真メニュー・絞り込みメニューで固定の意味を持つキー（設定の段階・キーワードには使えない）
MENU_FIXED_KEYS = frozenset({"0", "-", "/", "E"} | {str(r) for r in MENU_PLAIN_RATINGS})
FILTER_FIXED_KEYS = frozenset({"U", "E", "A"})

# ★の値の範囲（-1=削除候補 / 0=未選別 / 1〜5）
RATING_MIN, RATING_MAX = -1, 5

# filter_key（段階の）と key（プリセットの）は §7 の既定に足した任意の項目。
# 絞り込みの小メニュー（B/F/M/D/U/E/A）の頭の文字を決める。
# short（段階の）は任意。★の範囲スライダーの目盛り・★列のバッジ・あっふの R の★のボタンに出す短い名前。
# icon（段階の）は任意。★列のバッジの頭に付ける絵文字。
DEFAULT_CONFIG: dict = {
    "levels": [
        {"key": "5", "name": "みてね用", "min": 5, "short": "みてね", "icon": "💌"},
        {"key": "4", "name": "フォトフレーム用", "min": 4, "filter_key": "F", "short": "フレーム",
         "icon": "🖼️"},
        {"key": "3", "name": "ベストショット", "min": 3, "filter_key": "B", "short": "ベスト",
         "icon": "⭐"},
    ],
    "keywords": [{"key": "K", "name": "みてね済"}],
    "presets": [
        {"name": "ベスト", "expr": "★>=3"},
        {"name": "みてね未", "expr": "★=5 && !#みてね済", "key": "M",
         "label": "みてね用でまだ上げていない"},
        {"name": "削除候補", "expr": "reject", "key": "D", "label": "削除候補だけ"},
    ],
    "frame_targets": [],
    # frame_targets のうち、削除がクラウドへ反映されない同期先（Amazon Photos のデスクトップアプリが
    # 自動バックアップしているフォルダなど）。ここから消した写真は「手で消す写真の一覧」に残す
    # （photometa.export.pending_manual_deletes）。frame_targets の形は変えない（古い PhotoCuller も読む）
    "frame_manual_delete": [],
    # フォトフレーム用に作る JPEG（photometa.export.sync_frame）
    "frame": {"max_long_edge": 2560, "quality": 85, "strip_gps": True},
    # みてね用に集める先。空なら %TEMP%\みてね用（photometa.export.mitene_dir）
    "mitene_dir": "",
    "exiftool": "",
}

FRAME_EDGE_RANGE = (256, 20000)
FRAME_QUALITY_RANGE = (1, 100)


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


# ------------------------------------------------------------------ 正規化

def _str(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _key(v) -> str | None:
    """メニューの頭の文字。1文字で、英字は大文字にそろえる。"""
    s = _str(v)
    if s is None or len(s) != 1:
        return None
    return s.upper()


def _norm_levels(v) -> list | None:
    if not isinstance(v, list):
        return None
    out, keys, names = [], set(MENU_FIXED_KEYS), set()
    for e in v:
        if not isinstance(e, dict):
            continue
        key, name, mn = _key(e.get("key")), _str(e.get("name")), e.get("min")
        if isinstance(mn, float) and mn.is_integer():
            mn = int(mn)
        if (key is None or key in keys or name is None or name in names
                or isinstance(mn, bool) or not isinstance(mn, int) or not 1 <= mn <= 5):
            continue
        item = dict(e)
        item.update(key=key, name=name, min=mn)
        fk = _key(e.get("filter_key"))
        if fk is None or fk in FILTER_FIXED_KEYS:
            item.pop("filter_key", None)
        else:
            item["filter_key"] = fk
        for opt in ("short", "icon"):
            value = _str(e.get(opt))
            if value is None:
                item.pop(opt, None)
            else:
                item[opt] = value
        out.append(item)
        keys.add(key)
        names.add(name)
    return out or None


def _norm_keywords(v, used_keys: set) -> list | None:
    if not isinstance(v, list):
        return None
    out, keys, names = [], set(used_keys), set()
    for e in v:
        if not isinstance(e, dict):
            continue
        key, name = _key(e.get("key")), _str(e.get("name"))
        if key is None or key in keys or name is None or name in names:
            continue
        item = dict(e)
        item.update(key=key, name=name)
        out.append(item)
        keys.add(key)
        names.add(name)
    return out


def _norm_presets(v) -> list | None:
    if not isinstance(v, list):
        return None
    out = []
    for e in v:
        if not isinstance(e, dict):
            continue
        name, expr = _str(e.get("name")), _str(e.get("expr"))
        if name is None or expr is None:
            continue
        item = dict(e)
        item.update(name=name, expr=expr)
        k = _key(e.get("key"))
        if k is None or k in FILTER_FIXED_KEYS:
            item.pop("key", None)
        else:
            item["key"] = k
        if _str(e.get("label")) is None:
            item.pop("label", None)
        out.append(item)
    return out


def _int_in(v, lo: int, hi: int) -> int | None:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return None
    return v


def normalize_frame(v) -> dict:
    """"frame"（フォトフレーム用 JPEG の作り方）を項目ごとに正規化する。壊れた項目は既定。"""
    out = dict(DEFAULT_CONFIG["frame"])
    if not isinstance(v, dict):
        return out
    edge = _int_in(v.get("max_long_edge"), *FRAME_EDGE_RANGE)
    if edge is not None:
        out["max_long_edge"] = edge
    quality = _int_in(v.get("quality"), *FRAME_QUALITY_RANGE)
    if quality is not None:
        out["quality"] = quality
    if isinstance(v.get("strip_gps"), bool):
        out["strip_gps"] = v["strip_gps"]
    return out


def normalize(cfg) -> dict:
    """壊れた値を既定に戻した設定を返す（元の辞書は変えない）。知らないキーは残す。"""
    out = default_config()
    if not isinstance(cfg, dict):
        return out
    for k, v in cfg.items():
        if k not in out:
            out[k] = copy.deepcopy(v)
    levels = _norm_levels(cfg.get("levels"))
    if levels is not None:
        out["levels"] = levels
    used = set(MENU_FIXED_KEYS) | {lv["key"] for lv in out["levels"]}
    kws = _norm_keywords(cfg.get("keywords"), used)
    if kws is not None:
        out["keywords"] = kws
    else:
        out["keywords"] = _norm_keywords(out["keywords"], used) or []
    presets = _norm_presets(cfg.get("presets"))
    if presets is not None:
        out["presets"] = presets
    ft = cfg.get("frame_targets")
    if isinstance(ft, list):
        out["frame_targets"] = [s for s in (_str(x) for x in ft) if s]
    fm = cfg.get("frame_manual_delete")
    if isinstance(fm, list):
        out["frame_manual_delete"] = [s for s in (_str(x) for x in fm) if s]
    out["frame"] = normalize_frame(cfg.get("frame"))
    md = cfg.get("mitene_dir")
    if isinstance(md, str):
        out["mitene_dir"] = md.strip()
    et = cfg.get("exiftool")
    if isinstance(et, str):
        out["exiftool"] = et.strip()
    return out


# ------------------------------------------------------------------ 読み書き

def load_config(path: str | None = None) -> dict:
    """設定を読む。無い・壊れている JSON なら既定値。"""
    path = path or CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default_config()
    return normalize(data)


def save_config(cfg: dict, path: str | None = None) -> None:
    """設定を書く（normalize してから、一時ファイルに書いて置き換える）。"""
    path = path or CONFIG_PATH
    data = json.dumps(normalize(cfg), ensure_ascii=False, indent=2) + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as f:
        f.write(data.encode("utf-8"))
    os.replace(tmp, path)


def find_exiftool(config: dict | None = None) -> str | None:
    """ExifTool の場所。設定の "exiftool"（ファイルかフォルダ）→ PATH 上の exiftool.exe の順。

    見つからなければ None。
    """
    value = (config or {}).get("exiftool") or ""
    if isinstance(value, str) and value.strip():
        value = os.path.expandvars(value.strip().strip('"'))
        if os.path.isfile(value):
            return value
        if os.path.isdir(value):
            for name in ("exiftool.exe", "exiftool(-k).exe", "exiftool"):
                cand = os.path.join(value, name)
                if os.path.isfile(cand):
                    return cand
    return shutil.which("exiftool")


# ------------------------------------------------------------------ 段階

def levels_by_min(config: dict, descending: bool = True) -> list[dict]:
    return sorted(config.get("levels") or [], key=lambda lv: lv["min"], reverse=descending)


def level_min(config: dict, name: str) -> int | None:
    """段階名 → しきい値（★がこれ以上ならその段階）。知らない名前は None。"""
    for lv in config.get("levels") or []:
        if lv.get("name") == name:
            return lv.get("min")
    return None


def level_for_rating(config: dict, rating: int) -> dict | None:
    """その★が入る一番上の段階（★3 なら ベストショット）。どこにも入らなければ None。"""
    for lv in levels_by_min(config):
        if rating >= lv["min"]:
            return lv
    return None


def level_exact(config: dict | None, rating: int) -> dict | None:
    """しきい値がちょうどその★の段階（★4 なら フォトフレーム用）。無ければ None。"""
    for lv in (config or DEFAULT_CONFIG).get("levels") or []:
        if lv.get("min") == rating:
            return lv
    return None


_DEFAULT_SHORTS = {lv["name"]: lv["short"] for lv in DEFAULT_CONFIG["levels"]}
_DEFAULT_ICONS = {lv["name"]: lv["icon"] for lv in DEFAULT_CONFIG["levels"]}


def level_short(level: dict) -> str:
    """段階の短い名前。short が無ければ、既定と同じ名前の段階なら既定の short、それも無ければ name。

    （short を足す前に保存した photo_ratings.json にも短い名前が出るように）"""
    return _str(level.get("short")) or _DEFAULT_SHORTS.get(level.get("name")) or level["name"]


def level_icon(level: dict) -> str:
    """段階の絵文字。icon が無ければ、既定と同じ名前の段階なら既定の icon、それも無ければ空。"""
    return _str(level.get("icon")) or _DEFAULT_ICONS.get(level.get("name")) or ""


# ------------------------------------------------------------------ ★の表示名

def rating_name(rating: int, config: dict | None = None) -> str:
    """★の名前（削除候補 / 未選別 / ベストショット / ★2 など）。"""
    if rating == -1:
        return "削除候補"
    if rating == 0:
        return "未選別"
    lv = level_exact(config, rating)
    return lv["name"] if lv else f"★{rating}"


def rating_tag(rating: int, config: dict | None = None) -> str:
    """一覧やビュアの★表示（❌削除候補 / 空欄 / ベストショット / ★2 など）。"""
    if rating == -1:
        return "❌削除候補"
    if rating == 0:
        return ""
    return rating_name(rating, config)


def rating_short(rating: int, config: dict | None = None) -> str:
    """★の範囲スライダーの目盛りの文字（❌ / 未 / 1 / 2 / ベスト / フレーム / みてね）。"""
    if rating == -1:
        return "❌"
    if rating == 0:
        return "未"
    lv = level_exact(config, rating)
    return level_short(lv) if lv else str(rating)


def rating_badge(rating: int | None, config: dict | None = None) -> tuple[str, str] | None:
    """★列のバッジの (絵文字, 短い名前)。未選別・★を読めていないなら None。

    -1 → ("❌", "削除")、段階のしきい値ちょうど → (icon, short)（★4 なら ("🖼️", "フレーム")）、
    それ以外 → ("", "★2") のように絵文字なし。"""
    if rating is None or rating == 0:
        return None
    if rating == -1:
        return "❌", "削除"
    lv = level_exact(config, rating)
    if lv:
        return level_icon(lv), level_short(lv)
    return "", f"★{rating}"


def is_full_range(lo: int, hi: int) -> bool:
    """★の範囲が全部（-1〜5。★では絞り込まない）か。"""
    return lo <= RATING_MIN and hi >= RATING_MAX


def range_expr(lo: int, hi: int) -> str:
    """★の範囲を絞り込みの式にする（「★>=3 && ★<=4」）。全部なら空。

    端の側の比較は書かない（★>=-1 は「★を読めた」と同じで余計な条件になるため）。"""
    parts = []
    if lo > RATING_MIN:
        parts.append(f"★>={lo}")
    if hi < RATING_MAX:
        parts.append(f"★<={hi}")
    return " && ".join(parts)


def range_label(lo: int, hi: int, config: dict | None = None) -> str:
    """★の範囲の表記（すべて / ベストショットだけ / ベストショット以上 / 未選別まで / ★1〜★2）。"""
    if is_full_range(lo, hi):
        return "すべて"
    if lo == hi:
        return f"{rating_name(lo, config)}だけ"
    if hi >= RATING_MAX:
        return f"{rating_name(lo, config)}以上"
    if lo <= RATING_MIN:
        return f"{rating_name(hi, config)}まで"
    return f"{rating_name(lo, config)}〜{rating_name(hi, config)}"


def rating_display(rating: int | None, keywords=(), config: dict | None = None) -> str:
    """一覧の★列の表示（rating_tag。キーワードの1つ目が付いていれば後ろに「 済」）。

    未選別で済だけなら「済」。★を読めていなければ空。"""
    if rating is None:
        return ""
    cfg = config or DEFAULT_CONFIG
    text = rating_tag(rating, cfg)
    kws = cfg.get("keywords") or []
    if kws and kws[0]["name"] in keywords:
        text = f"{text} 済" if text else "済"
    return text


# ------------------------------------------------------------------ メニュー

@dataclass(frozen=True)
class MenuItem:
    key: str            # 頭の文字（押すとすぐ実行）。区切り線は ""
    label: str          # 表示名
    detail: str         # 右に添える補足（★5 / -1 / 式など）
    action: tuple       # 動作（下を参照）


SEPARATOR = MenuItem("", "", "", ("separator", None))


def menu_title(count: int) -> str:
    return f"写真の★（{count}枚）"


def menu_items(config: dict, export: bool = False) -> list[MenuItem]:
    """写真メニュー（PhotoCuller の I）の行。動作は次のどれか：

    ("rating", 5) / ("rating", 0) / ("rating", -1) … 選んだ写真に★を付ける
                                                      （段階の行 → ★2・★1 → ★を外す → 削除候補。
                                                      ★2・★1 は段階のしきい値に無いときだけ。
                                                      ★の高い順に並べる）
    ("keyword", "みてね済")                          … キーワードを付ける／外す
    ("filter_menu", None)                             … 絞り込みの小メニューを開く
    ("export_menu", None)                             … 書き出しの小メニューを開く（export=True のときだけ）
    ("separator", None)                               … 区切り線（SEPARATOR）
    """
    rows = [(lv["min"], MenuItem(lv["key"], lv["name"], f"★{lv['min']}", ("rating", lv["min"])))
            for lv in levels_by_min(config)]
    # 段階の無い★（★2・★1）。頭の文字は MENU_FIXED_KEYS にあるので段階・キーワードとぶつからない
    covered = {r for r, _ in rows}
    rows += [(r, MenuItem(str(r), rating_name(r, config), str(r), ("rating", r)))
             for r in MENU_PLAIN_RATINGS if r not in covered]
    rows.sort(key=lambda row: row[0], reverse=True)     # 同じ★の段階どうしは元の順のまま
    items = [item for _, item in rows]
    items.append(MenuItem("0", "★を外す", "0", ("rating", 0)))
    items.append(MenuItem("-", "削除候補", "-1", ("rating", -1)))
    kws = config.get("keywords") or []
    if kws:
        items.append(SEPARATOR)
        for kw in kws:
            items.append(MenuItem(kw["key"], f"{kw['name']} を付ける／外す", "",
                                  ("keyword", kw["name"])))
    items.append(SEPARATOR)
    items.append(MenuItem("/", "★で絞り込む ▸", "", ("filter_menu", None)))
    if export:
        items.append(MenuItem("E", "書き出し ▸", "", ("export_menu", None)))
    return items


def export_menu_items(config: dict) -> list[MenuItem]:
    """書き出しの小メニュー（PhotoCuller の I → E）の行。動作は次のどれか：

    ("frame_sync", None)     … フォトフレームへ同期（photometa.export.sync_frame）
    ("mitene_collect", None) … みてね用に集める（photometa.export.collect_mitene）
    ("mitene_finish", None)  … みてね済にして片付ける（photometa.export.finish_mitene）
    ("frame_manual_list", None) … 手で消す写真の一覧（photometa.export.pending_manual_deletes）
                                  知らない動作の行は、古いアプリが飛ばす
    """
    from . import export as ex

    kws = config.get("keywords") or []
    done = kws[0]["name"] if kws else "みてね済"
    pending = len(ex.pending_manual_deletes())
    return [
        MenuItem("F", "フォトフレームへ同期", ex.frame_expr(config), ("frame_sync", None)),
        MenuItem("M", "みてね用に集める", ex.mitene_expr(config), ("mitene_collect", None)),
        MenuItem("D", f"{done}にして片付ける", "", ("mitene_finish", None)),
        MenuItem("L", f"手で消す写真の一覧（{pending}枚）", "", ("frame_manual_list", None)),
    ]


def filter_menu_items(config: dict) -> list[MenuItem]:
    """絞り込みの小メニュー（PhotoCuller の I → /）の行。動作は次のどれか：

    ("filter", 式)           … その式で絞り込む
    ("filter_input", None)   … 式を入力する欄を出す
    ("filter_clear", None)   … 絞り込みを解く

    作り方：段階のうち filter_key のあるもの（低い順。「ベストショットだけ ★>=3」）→
    プリセット（同じ式がすでにあれば飛ばす。key が無ければ Alt+数字と同じ番号）→
    「未選別だけ unrated」→「式を入力…」「絞り込みを解く」。
    """
    from . import filter_expr as fe

    items: list[MenuItem] = []
    seen_exprs: set[str] = set()
    used_keys = {"E", "A"}

    def add(key, label, expr):
        canon = fe.canonical(expr)
        if canon in seen_exprs or key in used_keys:
            return
        seen_exprs.add(canon)
        used_keys.add(key)
        items.append(MenuItem(key, label, expr, ("filter", expr)))

    for lv in levels_by_min(config, descending=False):
        fk = lv.get("filter_key")
        if fk:
            add(fk, f"{lv['name']}だけ", f"★>={lv['min']}")
    for i, p in enumerate(config.get("presets") or [], start=1):
        key = p.get("key") or (str(i) if i <= 9 else None)
        if key:
            add(key, p.get("label") or p["name"], p["expr"])
    add("U", "未選別だけ", "unrated")
    items.append(MenuItem("E", "式を入力…", "", ("filter_input", None)))
    items.append(MenuItem("A", "絞り込みを解く", "", ("filter_clear", None)))
    return items
