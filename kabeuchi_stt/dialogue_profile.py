# -*- coding: utf-8 -*-
"""AI対話モード: 宛先プロファイルと、入力欄・窓・読み戻しの判定（純関数）。

Win32 / COM / Qt を import しない。実機なしで単体テストできる範囲だけを置く。
窓や UIA に触る部分は dialogue_worker.py にある。

プロファイル（PHAL 互換の辞書）:
  process_name / window_class / window_title_contains … 宛先の窓を探す手がかり
  input_automation_id / input_name / input_control_type / input_class_name / input_chain … 入力欄の手がかり
  click_rel / window_size … クリックした位置（窓の左上からの相対）と、そのときの窓の大きさ
プリセットは %LOCALAPPDATA%\\kabeuchi_stt\\dialogue_targets\\*.json（ベースは引数で差し替えられる）。
"""
from __future__ import annotations

import datetime
import json
import os
import re

SCHEMA = "dialogue-v1"
ACCEPTED_SCHEMAS = (SCHEMA, "stage0-v1")   # 段階0の試作で保存したものも読める
TARGETS_SUBDIR = "dialogue_targets"

# ---------------------------------------------------------------------------
# プロファイルの作成
# ---------------------------------------------------------------------------

# タイトルの区切り。可変部（会話名など）と固定部（アプリ名）をつなぐ記号。
_TITLE_SEPARATORS = (" - ", " | ", " — ", " – ", " ・ ", " · ", " : ")


def title_hint(title):
    """窓タイトルから、窓を探す手がかり（window_title_contains）を素朴に作る。
    最初の区切りの「前」だけを使う。区切りが無ければ空（exe 名とクラスだけで探す）。
    あくまで初期値。人がプリセットの JSON を直す前提。"""
    t = (title or "").strip()
    if not t:
        return ""
    first = None
    for sep in _TITLE_SEPARATORS:
        i = t.find(sep)
        if i > 0 and (first is None or i < first):
            first = i
    if first is None:
        return ""
    return t[:first].strip()


def _chain_entry(d):
    d = d or {}
    return {
        "control_type": d.get("control_type", "") or "",
        "name": d.get("name", "") or "",
        "automation_id": d.get("automation_id", "") or "",
        "class_name": d.get("class_name", "") or "",
    }


def build_profile(win, elem, click_pt, name, now=None):
    """窓の情報・UIA 要素・クリック位置から、プロファイル辞書を作る。

    win  : 窓情報の辞書（exe, class, title, rect=(l,t,r,b), render_class など）
    elem : UIA 要素の辞書。chain は「直接の親 → 根」の順（要素自身は含まない）。欠損があってもよい。
    click_pt : (x, y) 画面座標。窓矩形の左上からの相対にして保存する。
    input_chain は「根 → 要素」の順（要素自身を最後に含む）。"""
    win = win or {}
    elem = elem or {}
    rect = win.get("rect") or (0, 0, 0, 0)
    left, top, right, bottom = rect
    cx, cy = (click_pt or (left, top))
    parents = [_chain_entry(p) for p in (elem.get("chain") or [])]
    parents.reverse()
    chain = parents + [_chain_entry(elem)]
    captured = now or datetime.datetime.now()
    return {
        "schema": SCHEMA,
        "name": name,
        "process_name": win.get("exe", "") or "",
        "window_class": win.get("class", "") or "",
        "window_title_contains": title_hint(win.get("title", "")),
        "render_class": win.get("render_class", "") or "",
        "input_automation_id": elem.get("automation_id", "") or "",
        "input_name": elem.get("name", "") or "",
        "input_control_type": elem.get("control_type", "") or "",
        "input_class_name": elem.get("class_name", "") or "",
        "input_chain": chain,
        "click_rel": [int(cx - left), int(cy - top)],
        "window_size": [int(right - left), int(bottom - top)],
        "captured_at": captured.isoformat(timespec="seconds"),
        "captured_title": win.get("title", "") or "",   # 参考用（同点のときの絞り込みだけに使う）
    }


def profile_label(profile):
    """画面に出す短い名前。"""
    p = profile or {}
    return p.get("name") or p.get("process_name") or "(宛先)"


# ---------------------------------------------------------------------------
# プリセットの保存・一覧・読み込み・削除
# ---------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
    | {f"LPT{i}" for i in range(1, 10)}


def sanitize_name(name):
    """ファイル名に使える形にする。日本語はそのまま。空になったら 'target'。"""
    s = _BAD_CHARS.sub("_", (name or "").strip())
    s = s.strip(" .")
    if not s:
        return "target"
    s = s[:80].rstrip(" .")
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s or "target"


def default_base_dir():
    """本体のデータ置き場（paths.data_dir と同じ決め方。ここでは作らない）。
    KABEUCHI_DATA_DIR があればそれ、なければ %LOCALAPPDATA%\\kabeuchi_stt。"""
    env = os.environ.get("KABEUCHI_DATA_DIR")
    if env:
        return env
    local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(local, "kabeuchi_stt")


def targets_dir(base_dir=None):
    return os.path.join(base_dir or default_base_dir(), TARGETS_SUBDIR)


def target_path(base_dir, name):
    return os.path.join(targets_dir(base_dir), sanitize_name(name) + ".json")


def preset_exists(name, base_dir=None):
    """同名のプリセットがあるか。上書き前の確認は呼び出し側が行う。"""
    return os.path.exists(target_path(base_dir, name))


def save_preset(profile, base_dir=None, overwrite=False):
    """<targets>/<name>.json に保存してパスを返す。既存があって overwrite=False なら
    FileExistsError（黙って上書きしない）。LF・UTF-8。一時ファイル経由で置き換える。"""
    d = targets_dir(base_dir)
    os.makedirs(d, exist_ok=True)
    path = target_path(base_dir, profile.get("name", ""))
    if os.path.exists(path) and not overwrite:
        raise FileExistsError(path)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def load_target(path):
    """プリセットのファイルを読む。壊れている・形式違いなら ValueError。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError(f"読めません: {os.path.basename(path)} ({e})") from e
    if not isinstance(data, dict) or data.get("schema") not in ACCEPTED_SCHEMAS:
        raise ValueError(f"形式が違います（schema が {SCHEMA} ではない）: {os.path.basename(path)}")
    return data


def load_preset(name, base_dir=None):
    return load_target(target_path(base_dir, name))


def list_presets(base_dir=None):
    """[(パス, プロファイル)] を名前順で返す。読めないものは黙って飛ばす。"""
    d = targets_dir(base_dir)
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".json"):
            continue
        p = os.path.join(d, fn)
        try:
            out.append((p, load_target(p)))
        except ValueError:
            continue
    return out


def delete_preset(name, base_dir=None):
    """削除して True。無ければ False。"""
    path = target_path(base_dir, name)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------------------
# 窓の絞り込み
# ---------------------------------------------------------------------------

def match_window(info, profile):
    """窓の情報 info がプロファイルの宛先に当てはまるか。
    exe 名・クラスは完全一致（大文字小文字は無視）、タイトルは「含む」（空なら条件にしない）。
    条件が全部空のプロファイルは何にも一致させない（全窓に当たる事故を防ぐ）。"""
    pn = (profile.get("process_name") or "").lower()
    wc = (profile.get("window_class") or "").lower()
    tc = (profile.get("window_title_contains") or "").lower()
    if not (pn or wc or tc):
        return False
    if pn and (info.get("exe") or "").lower() != pn:
        return False
    if wc and (info.get("class") or "").lower() != wc:
        return False
    if tc and tc not in (info.get("title") or "").lower():
        return False
    return True


def select_window(infos, profile, held_hwnd=0):
    """窓の一覧から宛先を1つに決める。曖昧・無しのときは決めない（別の窓へは送らない）。

    戻りは {"status": "ok"|"none"|"ambiguous", "hwnd": 最上位HWND(ok のとき), "reason", "candidates"}。
    - 握っているハンドル（held_hwnd）がまだ条件に合っていれば、それを使う。
    - 条件に合う最上位窓が1つだけなら、それ。
    - 複数あるときは、保存時のタイトルと完全一致する窓がちょうど1つのときだけ、それ。それ以外は ambiguous。"""
    matches = {}
    for i in infos or []:
        if not match_window(i, profile):
            continue
        root = i.get("root") or i.get("hwnd")
        if root:
            matches.setdefault(int(root), i)
    cands = [{"hwnd": r, "title": (i.get("title") or "")[:60]} for r, i in matches.items()]
    held = int(held_hwnd or 0)
    if held and held in matches:
        return {"status": "ok", "hwnd": held, "reason": "held", "candidates": cands}
    if not matches:
        return {"status": "none", "hwnd": 0, "reason": "no_match", "candidates": []}
    if len(matches) == 1:
        (root,) = matches.keys()
        return {"status": "ok", "hwnd": root, "reason": "unique", "candidates": cands}
    cap = (profile.get("captured_title") or "").strip()
    if cap:
        exact = [r for r, i in matches.items() if (i.get("title") or "").strip() == cap]
        if len(exact) == 1:
            return {"status": "ok", "hwnd": exact[0], "reason": "title_exact", "candidates": cands}
    return {"status": "ambiguous", "hwnd": 0, "reason": "multiple_matches", "candidates": cands}


# ---------------------------------------------------------------------------
# 入力欄の選び方（UIA から集めた要素の辞書リストを受ける）
# ---------------------------------------------------------------------------

_INPUT_TYPES = ("Edit", "ComboBox", "Document")


def _as_list(v):
    if isinstance(v, (list, tuple)):
        return [x for x in v if x]
    return [v] if v else []


def _rect_area(r):
    if not r:
        return 0
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _contains(r, pt):
    return bool(r) and r[0] <= pt[0] < r[2] and r[1] <= pt[1] < r[3]


def _inside(inner, outer, slack=2):
    return (inner[0] >= outer[0] - slack and inner[1] >= outer[1] - slack
            and inner[2] <= outer[2] + slack and inner[3] <= outer[3] + slack)


def expected_point(profile, win_rect):
    """保存した click_rel を、いまの窓矩形の左上に足した画面座標。
    窓サイズが変わっていたら size_changed=True（相対位置のずれの目印）。"""
    rel = profile.get("click_rel") or [0, 0]
    size = profile.get("window_size") or [0, 0]
    l, t, r, b = win_rect
    changed = bool(size[0] and size[1]) and (size[0] != r - l or size[1] != b - t)
    return (l + rel[0], t + rel[1]), changed


def choose_input(infos, profile, win_rect=None):
    """入力欄を選ぶ。戻りは (index, how, 理由メモ)。見つからなければ (None, "", メモ)。

    infos は 1 要素 1 辞書: index, control_type(名前文字列), name, automation_id,
    class_name, rect=(l,t,r,b), has_value(True/False/None)。
    順に: 自動化ID → 名前（＋種類）→ 種類＋クラス名 → 保存した位置付近の矩形。
    どの段でも、見えない（面積0）要素より見える要素を優先する。"""
    def pick(cands):
        if not cands:
            return None
        vis = [c for c in cands if _rect_area(c.get("rect")) > 0]
        return (vis or cands)[0]["index"]

    aids = _as_list(profile.get("input_automation_id"))
    if aids:
        hit = pick([i for i in infos if i.get("automation_id") in aids])
        if hit is not None:
            return hit, "automation_id", ""
    name = profile.get("input_name") or ""
    ctype = profile.get("input_control_type") or ""
    if name:
        hit = pick([i for i in infos if i.get("name") == name
                    and (not ctype or i.get("control_type") == ctype)])
        if hit is not None:
            return hit, "name", ""
    cls = profile.get("input_class_name") or ""
    if ctype and cls:
        hit = pick([i for i in infos if i.get("control_type") == ctype
                    and i.get("class_name") == cls])
        if hit is not None:
            return hit, "control_type+class", ""
    if win_rect and profile.get("click_rel"):
        pt, changed = expected_point(profile, win_rect)
        near = [i for i in infos if _contains(i.get("rect"), pt)
                and (i.get("has_value") or i.get("control_type") in _INPUT_TYPES)]
        near.sort(key=lambda i: _rect_area(i.get("rect")))   # 一番小さい（内側の）要素
        if near:
            return near[0]["index"], ("rect(size_changed)" if changed else "rect"), ""
    return None, "", "どの手掛かりでも見つからない"


def descend_input(infos, rect, point=None):
    """値パターンの無い要素（入力欄を包むボタン・文書全体など）が当たったとき、その矩形の中にある
    Edit/ComboBox/Document（値パターンあり）へ降りる。戻りは index か None。
    矩形に入っている候補のうち、point（保存したクリック位置）を含むものがあればそれに絞り、
    その中で面積が最小のもの。"""
    if not rect:
        return None
    cands = [i for i in infos
             if i.get("has_value") is True and i.get("control_type") in _INPUT_TYPES
             and _rect_area(i.get("rect")) > 0 and _inside(i["rect"], rect)]
    if point is not None:
        at = [i for i in cands if _contains(i["rect"], point)]
        if at:
            cands = at
    if not cands:
        return None
    cands.sort(key=lambda i: _rect_area(i["rect"]))
    return cands[0]["index"]


def resolve_input(infos, profile, win_rect=None):
    """choose_input ＋ 値パターンの無い要素からの「降りる」。

    戻りは {"index", "how", "descended", "parent_index", "note"}。見つからなければ index=None。
    当たった要素が値パターンを持つ（has_value=True）ならそのまま。持たない／不明なら降りる。
    降りた先も無ければ、当たった要素をそのまま返す（書き込みは呼び出し側で「書けない」と分かる）。"""
    idx, how, note = choose_input(infos, profile, win_rect)
    if idx is None:
        return {"index": None, "how": "", "descended": False, "parent_index": None, "note": note}
    el = next((i for i in infos if i["index"] == idx), None) or {}
    if el.get("has_value") is True:
        return {"index": idx, "how": how, "descended": False, "parent_index": None, "note": ""}
    pt = None
    if win_rect and profile.get("click_rel"):
        pt, _chg = expected_point(profile, win_rect)
    inner = descend_input([i for i in infos if i["index"] != idx], el.get("rect"), pt)
    if inner is not None:
        return {"index": inner, "how": how + "+descend", "descended": True,
                "parent_index": idx, "note": ""}
    return {"index": idx, "how": how, "descended": False, "parent_index": None,
            "note": "値パターンの無い要素で、降りる先も無い"}


def input_candidates(infos, limit=15):
    """見つからなかったときに見せる、入力欄っぽい要素の一覧。"""
    out = []
    for i in infos:
        if i.get("has_value") or i.get("control_type") in _INPUT_TYPES:
            out.append({k: i.get(k) for k in
                        ("control_type", "name", "automation_id", "class_name", "rect")})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 読み戻しの判定
# ---------------------------------------------------------------------------

_INVISIBLE = dict.fromkeys(map(ord, "￼​‌‍⁠﻿"))


def _norm_lines(s):
    # 見えない文字（オブジェクト置換文字 U+FFFC・ゼロ幅スペース等）は無視する。Copilot の空の入力欄は U+FFFC を1つ返す
    s = (s or "").translate(_INVISIBLE).replace("\r\n", "\n").replace("\r", "\n")
    return [ln.rstrip() for ln in s.split("\n")]


def normalize_text(s):
    """改行コードの差・行末の空白・前後の空白（空行を含む）を無視する正規化。"""
    return "\n".join(_norm_lines(s)).strip()


def texts_equivalent(a, b):
    """改行コード・行末空白・前後の空白の差だけなら同じとみなす。"""
    return normalize_text(a) == normalize_text(b)


def judge_readback(expected, actual):
    """読み戻しの判定。actual が None（読めなかった）なら 'unreadable'、
    空なら 'empty'、実質一致なら 'ok'、それ以外は 'mismatch'。"""
    if actual is None:
        return "unreadable"
    if normalize_text(actual) == "":
        return "empty"
    return "ok" if texts_equivalent(expected, actual) else "mismatch"


def judge_cleared(actual):
    """片付け後の判定。空なら 'ok'、読めなければ 'unreadable'、残っていれば 'remain'。"""
    if actual is None:
        return "unreadable"
    return "ok" if normalize_text(actual) == "" else "remain"
