# unified_picker.py
# 統合ピッカーの候補生成とプレビュー文。
#
# J ピッカー / Win+J ピッカー / コマンドパレット / コマンドランチャを1つの窓
# (picker.PickerWindow)に寄せたもの。ここは「候補をどう並べ、Enter で何が起きる
# かをどう見せるか」だけを持ち、窓そのものと実行は呼び出し側(feature_screen、
# filer)が持つ。
#
# 表示ルール(2026-11-11、Nagato の要望で確定):
#   - 開いた直後に出る静的候補は、明示的に登録したものだけ。並びは
#     フォルダ → URL・URI → 実行コマンド の順(フォルダジャンプが基本)。
#     その下にピン留めした機能、内蔵コマンド、文脈の行が続く。
#   - 機能とアプリ(登録の外の大量候補)は最初は出さない。入力に `*` が
#     含まれたときだけ範囲を広げて出す(Exe* で Excel が出る。照合は
#     前方>部分>部分列の score_match)。広げた候補は動的候補として
#     登録の下に出る。
#   - 入力から作る候補(打ったパス・計算・ファイル検索・Web検索)は
#     常に一覧の下の方に出る(呼び出し側が dynamic_last=True で窓を開く)。
#   - `*` 経由で実行した機能・アプリは、呼び出し側が「通常表示(スタメン)へ
#     昇格するか」を聞く。アプリはブックマーク登録、機能はピン留めで昇格する。
#
# 候補の見分け:
#   表示名の頭に種別のアイコンを付け、絞り込みは本来の名前で効くよう
#   PickerWindow の3要素タプル(表示名, データ, 絞り込み名)で渡す。
#   選択中の行については、プレビュー欄に「Enter でどうなるか」を文章で出す
#   (preview_text)。
#
# データ(dict)の kind:
#   jump    フォルダを開く(開き先は文脈: 常駐=launcher.jump / あっふ=自窓の移動)
#   open    URL・URI を OS で開く(url / onenote / uri)
#   exec    登録コマンドを実行(command_launcher.Entry を持つ。param は任意)
#   feature 縦ランチャの機能を呼ぶ(pinned=True はピン留め済み、pool=True は
#           `*` で広げて出した控え)
#   app     スタートメニューのアプリを起動(pool=True は控え)
#   file    Everything の検索結果ファイルを開く
#   calc    計算結果をクリップボードへ
#   web     Web 検索の結果ページを開く
#   builtin 内蔵コマンド(command / config / reload)
#   add     「ここを登録」
#   import  ブラウザのお気に入りの取り込み
#   stash   あっふの一時置き場(あっふ文脈だけ)
import os
import time

import command_launcher
import command_palette
import launcher

ICONS = {
    "folder": "📁",
    "url": "🌐",
    "onenote": "📓",
    "uri": "🔗",
    "app": "🚀",
    "exec": "▶",
    "feature": "🔧",
    "file": "📄",
    "calc": "🧮",
    "web": "🔍",
    "builtin": "⚙️",
}

PLACEHOLDER = "絞り込み（名前・パス・計算 / ; で引数 / * で範囲を広げる / Enter実行）"
HINT = "Tab 掘り下げ・引数へ / Ctrl+D 削除 / Ctrl+↑↓ 並べ替え"


def entry_payload(entry, param=None) -> dict:
    """登録1件(command_launcher.Entry)を候補データにする。"""
    if entry.kind == command_launcher.KIND_FOLDER:
        return {"kind": "jump", "path": entry.target, "name": entry.name,
                "bookmark": True}
    if entry.kind in (command_launcher.KIND_URL, command_launcher.KIND_URI):
        via = "url" if entry.kind == command_launcher.KIND_URL else (
            "onenote" if entry.target.lower().startswith("onenote:") else "uri")
        return {"kind": "open", "target": entry.target, "name": entry.name,
                "via": via, "bookmark": True}
    payload = {"kind": "exec", "entry": entry, "name": entry.name,
               "bookmark": True}
    if param is not None:
        payload["param"] = param
    return payload


def _entry_icon(entry) -> str:
    if entry.kind == command_launcher.KIND_FOLDER:
        return ICONS["folder"]
    if entry.kind == command_launcher.KIND_URL:
        return ICONS["url"]
    if entry.kind == command_launcher.KIND_URI:
        return ICONS["onenote"] if entry.target.lower().startswith("onenote:") \
            else ICONS["uri"]
    return ICONS["exec"] if entry.params else ICONS["app"]


def _entry_group(entry) -> int:
    """登録の並びグループ。フォルダを最上位に固定する。"""
    if entry.kind == command_launcher.KIND_FOLDER:
        return 0
    if entry.kind in (command_launcher.KIND_URL, command_launcher.KIND_URI):
        return 1
    return 2


def _frecency_sorted(pairs, palette_history, key_of):
    """(表示のもと, Candidate キー) の列を frecency の押し上げ順に並べる。

    押し上げが同点なら元の順序を保つ(sorted は安定)。ブックマークには使わない
    (あちらは本人が並べた登録順が正)。"""
    if not palette_history:
        return pairs
    now = time.time()

    def boost(pair):
        entry = palette_history.get(key_of(pair)) or {}
        return command_palette.frecency_boost(entry, now)

    return sorted(pairs, key=lambda pair: -boost(pair))


def build_items(entries, *, pinned_features=(), add_path=None,
                stash_count=None, show_import=True) -> list:
    """統合ピッカーの静的候補を (表示名, データ, 絞り込み名) で返す。

    開いた直後に出るのは登録とピン留めだけ。機能・アプリの全体はここには
    入れず、`*` を打ったときに make_dynamic_items 側が広げて出す。

    entries: command_launcher.build_entries の結果(登録順)
    pinned_features: [(項目キー, ラベル)] ピン留め済みの機能
    stash_count: あっふ文脈用。一時置き場の件数を渡すと先頭にその行を出す
    show_import: お気に入り取り込みの行を出すか(あっふ文脈では出さない)
    """
    items = []
    if stash_count is not None:
        items.append((f"🧺 一時置き場（{stash_count}件）",
                      {"kind": "stash"}, "一時置き場"))
    ordered = sorted(enumerate(entries),
                     key=lambda pair: (_entry_group(pair[1]), pair[0]))
    for _index, entry in ordered:
        items.append((f"{_entry_icon(entry)} {entry.name}",
                      entry_payload(entry), entry.name))
    for key, label in pinned_features:
        items.append((f"{ICONS['feature']} {label}",
                      {"kind": "feature", "item_key": key, "name": label,
                       "pinned": True},
                      label))
    for name, builtin_id, _desc in command_launcher._BUILTINS:
        if builtin_id == "history":
            continue  # 履歴の窓は統合ピッカーには出さない
        items.append((f"{ICONS['builtin']} {name}",
                      {"kind": "builtin", "id": builtin_id, "name": name},
                      name))
    if add_path:
        items.append((f"{launcher.ADD_ITEM_PREFIX}   {add_path}",
                      {"kind": "add", "path": add_path}, ""))
    if show_import:
        items.append((launcher.IMPORT_ITEM_LABEL, {"kind": "import"}, ""))
    return items


def resolve_input(raw: str, entries):
    """打たれた文字列(名前;引数)を候補データに解釈する。解釈できなければ None。

    名前が登録と完全一致したときだけ拾う(前方一致では拾わない)。"""
    name, param = command_launcher.parse_input(raw or "")
    if not name:
        return None
    for entry in entries:
        if entry.name.casefold() == name.casefold():
            return entry_payload(entry, param)
    return None


def make_dynamic_items(*, entries, features, apps, web_searches,
                       file_search, palette_history=None, pinned_keys=()):
    """入力から作る候補(dynamic_items フック)を返す関数を作る。

    features / apps は「控え」。静的候補には入らず、入力に `*` が含まれた
    ときだけここから広げて出す。ピン留め済み(pinned_keys)の機能は
    控えではなくスタメン扱いで、`*` の検索でも先に出す。

    file_search: func(文字列) -> パスの列(Everything。失敗時は空)
    """
    searches = command_palette.normalize_web_searches(web_searches or [])
    pinned = set(pinned_keys or ())
    entry_names = [e.name for e in entries]

    def feature_row(key, label):
        return (f"{ICONS['feature']} {label}",
                {"kind": "feature", "item_key": key, "name": label,
                 "pool": key not in pinned,
                 **({"pinned": True} if key in pinned else {})})

    def app_row(cand):
        return (f"{ICONS['app']} {cand.title}",
                {"kind": "app", "path": cand.payload.get("path") or "",
                 "name": cand.title, "pool": True})

    def widened_rows(raw):
        """`*` を含む入力で、登録+機能+アプリ全体から探して返す。

        `*` を除いた残りを command_palette.score_match で照らす(前方一致 >
        部分一致 > 部分列)。Exe* で Excel が出るのはこの部分列のおかげ
        (Excel は Exe では始まらない)。`*` だけなら全部。
        並びは 登録(フォルダ→URL→実行) → ピン留め機能 → 控えの機能 → アプリで、
        グループの中はスコア順(同点は元の順序)。"""
        pattern = raw.split(";", 1)[0].replace("*", "").strip()
        rows = []

        def scored(pairs, name_of, row_of):
            hits = []
            for pair in pairs:
                score = command_palette.score_match(pattern, name_of(pair))
                if score is not None:
                    hits.append((score, row_of(pair)))
            hits.sort(key=lambda hit_row: -hit_row[0])
            rows.extend(row for _score, row in hits)

        ordered = sorted(enumerate(entries),
                         key=lambda pair: (_entry_group(pair[1]), pair[0]))
        scored([entry for _i, entry in ordered],
               lambda entry: entry.name,
               lambda entry: (f"{_entry_icon(entry)} {entry.name}",
                              entry_payload(entry)))
        feature_pairs = _frecency_sorted(
            list(features), palette_history,
            lambda pair: f"feature:{pair[0]}")
        for want_pinned in (True, False):
            scored([pair for pair in feature_pairs
                    if (pair[0] in pinned) == want_pinned],
                   lambda pair: pair[1],
                   lambda pair: feature_row(pair[0], pair[1]))
        app_pairs = _frecency_sorted(
            list(apps), palette_history,
            lambda cand: command_palette.candidate_key(cand) or "")
        scored(app_pairs, lambda cand: cand.title, app_row)
        return rows

    def dynamics(text: str) -> list:
        raw = (text or "").strip()
        if not raw:
            return []
        rows = []
        if "*" in raw:
            rows.extend(widened_rows(raw))
        calc = command_palette.evaluate_query(raw)
        if calc is not None:
            rows.append((f"{ICONS['calc']} {calc.title}",
                         {"kind": "calc", "value": calc.payload.get("value", "")}))
        name, param = command_launcher.parse_input(raw)
        if ";" in raw and name:
            matched = [e for e in entries
                       if e.kind == command_launcher.KIND_FILE
                       and e.name.casefold() == name.casefold()]
            if not matched:
                prefixed = [e for e in entries
                            if e.kind == command_launcher.KIND_FILE
                            and e.name.casefold().startswith(name.casefold())]
                matched = prefixed if len(prefixed) == 1 else []
            for entry in matched:
                rows.append((f"{ICONS['exec']} {entry.name};{param} を実行",
                             entry_payload(entry, param)))
        try:
            typed = launcher.typed_items(text)
        except Exception:  # noqa: BLE001  パスの解釈に失敗しても他の候補は出す
            typed = []
        for label, data in typed:
            kind, path = data
            if kind == launcher.KIND_JUMP:
                rows.append((label, {"kind": "jump", "path": path,
                                     "typed": True}))
        keyword = raw.lower()
        if "*" not in raw and ";" not in raw and not any(
                n.lower().startswith(keyword) for n in entry_names):
            # 前方一致が0件のときだけ、あいまい(部分列)一致を出す。対象は登録
            # の中だけ(控えは `*` を打たない限り出さない)。
            scored = []
            for entry in entries:
                score = command_palette.score_match(raw, entry.name)
                if score is not None:
                    scored.append((score, f"{_entry_icon(entry)} {entry.name}",
                                   entry_payload(entry)))
            scored.sort(key=lambda row: -row[0])
            rows.extend((label, payload) for _s, label, payload in scored[:5])
        if file_search is not None and len(raw) >= 2 and ";" not in raw:
            try:
                paths = file_search(raw) or []
            except Exception:  # noqa: BLE001  探せないことは候補が無いことと同じ
                paths = []
            for path in paths[:5]:
                rows.append((f"{ICONS['file']} {os.path.basename(path) or path}",
                             {"kind": "file", "path": path}))
        for cand in command_palette.build_web_candidates(searches, raw):
            rows.append((f"{ICONS['web']} {cand.title}",
                         {"kind": "web", "url": cand.payload.get("url", ""),
                          "service": cand.payload.get("service", "")}))
        return rows

    return dynamics


def make_tab_text():
    """Tab の行き先。フォルダは掘り下げ、実行コマンドは引数入力へ。"""

    def tab_text(_name, data):
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind == "jump":
            return launcher.tab_text(_name, (launcher.KIND_JUMP,
                                             data.get("path") or ""))
        if kind == "exec":
            return f"{data.get('name') or ''};"
        if kind == "file":
            folder = os.path.dirname(data.get("path") or "")
            if folder:
                return folder.rstrip("\\/") + "\\"
        return None

    return tab_text


def preview_text(data, *, context: str = "resident") -> str:
    """選択中の候補について「Enter でどうなるか」を文章で返す。"""
    if not isinstance(data, dict):
        return ""
    kind = data.get("kind")
    if kind == "stash":
        return "Enter で一時置き場を開きます"
    if kind == "jump":
        if context == "filer":
            return f"Enter でアクティブな窓をこのフォルダへ移動します: {data.get('path') or ''}"
        return f"Enter でフォルダを開きます: {data.get('path') or ''}"
    if kind == "open":
        target = data.get("target") or ""
        via = data.get("via")
        if via == "url":
            return f"Enter でブラウザで開きます: {target}"
        if via == "onenote":
            return f"Enter で OneNote で開きます: {target}"
        return f"Enter でアプリで開きます: {target}"
    if kind == "exec":
        entry = data.get("entry")
        if entry is None:
            return ""
        text = f"Enter でコマンドを実行します: {entry.target}"
        params = entry.params or ""
        if data.get("param") is not None and not params:
            params = data.get("param") or ""
        if params:
            text += f" {params}"
        if "%param%" in (entry.params or ""):
            text += "（%param% は ; の後ろに打った文字）"
        return text
    if kind == "feature":
        return f"Enter で機能を呼び出します: {data.get('name') or ''}"
    if kind == "app":
        return f"Enter でアプリを起動します: {data.get('path') or ''}"
    if kind == "file":
        return f"Enter でファイルを開きます: {data.get('path') or ''}"
    if kind == "calc":
        return f"Enter で結果「{data.get('value') or ''}」をクリップボードにコピーします"
    if kind == "web":
        return f"Enter で {data.get('service') or ''} の検索結果をブラウザで開きます"
    if kind == "builtin":
        return {"command": "Enter で登録の一覧と編集を開きます",
                "config": "Enter で settings.json を開きます",
                "reload": "Enter で登録を読み直します"}.get(data.get("id"), "")
    if kind == "add":
        return f"Enter でこのフォルダを登録します: {data.get('path') or ''}"
    if kind == "import":
        return "Enter でブラウザのお気に入りを取り込みます"
    return ""
