# photometa/config.py
# 写真の★の共通設定 %LOCALAPPDATA%\traytools\photo_ratings.json（あっふと PhotoCuller が読む）と、
# それから作る写真メニュー（I キー）・絞り込みの小メニュー（I → /）の中身。
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

# 写真メニュー・絞り込みメニューで固定の意味を持つキー（設定の段階・キーワードには使えない）
MENU_FIXED_KEYS = frozenset({"0", "-", "/"})
FILTER_FIXED_KEYS = frozenset({"U", "E", "A"})

# filter_key（段階の）と key（プリセットの）は §7 の既定に足した任意の項目。
# 絞り込みの小メニュー（B/F/M/D/U/E/A）の頭の文字を決める。
DEFAULT_CONFIG: dict = {
    "levels": [
        {"key": "5", "name": "みてね用", "min": 5},
        {"key": "4", "name": "フォトフレーム用", "min": 4, "filter_key": "F"},
        {"key": "3", "name": "ベストショット", "min": 3, "filter_key": "B"},
    ],
    "keywords": [{"key": "K", "name": "みてね済"}],
    "presets": [
        {"name": "ベスト", "expr": "★>=3"},
        {"name": "みてね未", "expr": "★=5 && !#みてね済", "key": "M",
         "label": "みてね用でまだ上げていない"},
        {"name": "削除候補", "expr": "reject", "key": "D", "label": "削除候補だけ"},
    ],
    "frame_targets": [],
    "exiftool": "",
}


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


def rating_display(rating: int | None, keywords=(), config: dict | None = None) -> str:
    """一覧の★列の表示（★5 / ✖ / 空欄。キーワードの1つ目が付いていれば後ろに「済」）。"""
    if rating is None:
        return ""
    text = "✖" if rating == -1 else (f"★{rating}" if rating > 0 else "")
    cfg = config or DEFAULT_CONFIG
    kws = cfg.get("keywords") or []
    if kws and kws[0]["name"] in keywords:
        text += "済"
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


def menu_items(config: dict) -> list[MenuItem]:
    """写真メニュー（I）の行。動作は次のどれか：

    ("rating", 5) / ("rating", 0) / ("rating", -1) … 選んだ写真に★を付ける
    ("keyword", "みてね済")                          … キーワードを付ける／外す
    ("filter_menu", None)                             … 絞り込みの小メニューを開く
    ("separator", None)                               … 区切り線（SEPARATOR）
    """
    items = [MenuItem(lv["key"], lv["name"], f"★{lv['min']}", ("rating", lv["min"]))
             for lv in levels_by_min(config)]
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
    return items


def filter_menu_items(config: dict) -> list[MenuItem]:
    """絞り込みの小メニュー（I → /）の行。動作は次のどれか：

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
