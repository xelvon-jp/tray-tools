# unified_picker.py
# 統合ピッカーの候補生成とプレビュー文。
#
# J ピッカー / Win+J ピッカー / コマンドパレット / コマンドランチャを1つの窓
# (picker.PickerWindow)に寄せたもの。ここは「候補をどう並べ、Enter で何が起きる
# かをどう見せるか」だけを持ち、窓そのものと実行は呼び出し側(feature_screen、
# filer)が持つ。
#
# モード(2026-11-12、Nagato の絵で確定した仕様。予約文字で切り替える1本の軸):
#   `[` フォルダ選択 … 登録したフォルダ。Enter は文脈の行き先(常駐=launcher.jump /
#        あっふ=自窓の移動)。あっふ文脈の一時置き場・「ここを登録」はここに出る。
#   `]` フォルダヒストリ … あっふの最近のフォルダ(H と同じ記録。settings の
#        filer.recent にあるので、Win+J 側からも同じものが読める)。
#   `!` コマンド・ランチャ … 登録した Web・プログラム → ピン留め機能 → 全機能
#        → 内蔵コマンドの順。走査した未登録アプリは最初は出さず、一番下の
#        「未登録アプリも探す」の行でその場に広げる。
#   `?` 検索 … ファイル検索(Everything)と Web 検索だけ。補完(Tab)は効かない。
#   `=` 電卓 … ピッカーは閉じて電卓の窓に移る(picker 側 on_calc の役目)。
#        `!` で電卓の機能を選ぶのと同じ扱いで、`=` はそのエイリアス。
#
# 照合: 既定は前方一致(picker.name_matches)。`*` / `?` を含む入力は
# ワイルドカードとして名前の中に探す。以前の「`*` を含むと機能・アプリへ
# 展開」する仕組みと、`名前;引数` の解釈は廃止した(モードが明示になったため)。
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
#   exec    登録コマンドを実行(command_launcher.Entry を持つ)
#   feature 縦ランチャの機能を呼ぶ(pinned=True はピン留め済み)
#   app     スタートメニューのアプリを起動(未登録アプリの走査から)
#   file    Everything の検索結果ファイルを開く
#   web     Web 検索の結果ページを開く
#   builtin 内蔵コマンド(command / config / reload)
#   add     「ここを登録」
#   import  ブラウザのお気に入りの取り込み
#   stash   あっふの一時置き場(あっふ文脈だけ)
#   more_apps 「未登録アプリも探す」(`!` の一番下。選ぶとその場で広げる)
import os

import command_launcher
import command_palette
import launcher
import picker as picker_mod

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

PLACEHOLDER = "モードを選ぶか、名前で絞り込み"
HINT = "Ctrl+Enter メニュー / Tab 掘り下げ / Ctrl+D 削除 / Ctrl+↑↓ 並べ替え"

# モードの表。(予約文字, モード名, チップの色)。picker.PickerWindow の
# modes にそのまま渡す。開いたときの初期モードは `[`。
MODES = [
    ("[", "フォルダ選択", "#1a73e8"),
    ("]", "フォルダヒストリ", "#7c3aed"),
    ("!", "コマンド・ランチャ", "#b45309"),
    ("?", "検索", "#0d9488"),
    ("=", "電卓", "#4d7c0f"),
]
INITIAL_MODE = "["

MORE_APPS_LABEL = "🔍 未登録アプリも探す"


def entry_payload(entry) -> dict:
    """登録1件(command_launcher.Entry)を候補データにする。"""
    if entry.kind == command_launcher.KIND_FOLDER:
        return {"kind": "jump", "path": entry.target, "name": entry.name,
                "bookmark": True}
    if entry.kind in (command_launcher.KIND_URL, command_launcher.KIND_URI):
        via = "url" if entry.kind == command_launcher.KIND_URL else (
            "onenote" if entry.target.lower().startswith("onenote:") else "uri")
        return {"kind": "open", "target": entry.target, "name": entry.name,
                "via": via, "bookmark": True}
    return {"kind": "exec", "entry": entry, "name": entry.name,
            "bookmark": True}


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


def recent_folders(app_settings) -> list:
    """あっふの最近のフォルダ(`]` の中身)。filer が settings の filer 節の
    recent に書いている記録で、Win+J 側からも同じものを読める。"""
    section = (app_settings or {}).get("filer")
    if not isinstance(section, dict):
        return []
    recent = section.get("recent")
    if not isinstance(recent, list):
        return []
    return [path for path in recent if isinstance(path, str) and path]


def build_mode_items(mode, entries, *, features=(), pinned_keys=(),
                     recent=(), add_path=None, stash_count=None,
                     show_import=True) -> list:
    """モードごとの静的候補を (表示名, データ, 絞り込み名) で返す。

    entries: command_launcher.build_entries の結果(登録順)
    features: [(項目キー, ラベル)] 機能の全体(`!` 用)
    pinned_keys: ピン留め済みの機能キー(`!` で機能の先頭に並べる)
    recent: 最近のフォルダのパス列(`]` 用。recent_folders の結果)
    stash_count: あっふ文脈用。一時置き場の件数を渡すと `[` の先頭にその行を出す
    show_import: お気に入り取り込みの行を出すか(あっふ文脈では出さない)
    """
    items = []
    if mode == "[":
        if stash_count is not None:
            items.append((f"🧺 一時置き場（{stash_count}件）",
                          {"kind": "stash"}, "一時置き場"))
        ordered = sorted(enumerate(entries),
                         key=lambda pair: (_entry_group(pair[1]), pair[0]))
        for _index, entry in ordered:
            if entry.kind != command_launcher.KIND_FOLDER:
                continue
            items.append((f"{_entry_icon(entry)} {entry.name}",
                          entry_payload(entry), entry.name))
        if add_path:
            items.append((f"{launcher.ADD_ITEM_PREFIX}   {add_path}",
                          {"kind": "add", "path": add_path}, ""))
        if show_import:
            items.append((launcher.IMPORT_ITEM_LABEL, {"kind": "import"}, ""))
        return items
    if mode == "]":
        for path in recent:
            items.append((f"{ICONS['folder']} {path}",
                          {"kind": "jump", "path": path, "name": path,
                           "recent": True},
                          path))
        return items
    if mode == "!":
        ordered = sorted(enumerate(entries),
                         key=lambda pair: (_entry_group(pair[1]), pair[0]))
        for _index, entry in ordered:
            if entry.kind == command_launcher.KIND_FOLDER:
                continue
            items.append((f"{_entry_icon(entry)} {entry.name}",
                          entry_payload(entry), entry.name))
        pinned = set(pinned_keys or ())
        labels = dict(features)
        for key in pinned_keys or ():
            if key in labels:
                items.append((f"{ICONS['feature']} {labels[key]}",
                              {"kind": "feature", "item_key": key,
                               "name": labels[key], "pinned": True},
                              labels[key]))
        for key, label in features:
            if key in pinned:
                continue
            items.append((f"{ICONS['feature']} {label}",
                          {"kind": "feature", "item_key": key, "name": label},
                          label))
        for name, builtin_id, _desc in command_launcher._BUILTINS:
            if builtin_id == "history":
                continue  # 履歴の窓は統合ピッカーには出さない
            items.append((f"{ICONS['builtin']} {name}",
                          {"kind": "builtin", "id": builtin_id, "name": name},
                          name))
        return items
    return []


def make_mode_dynamics(*, entries, apps, web_searches, file_search, state):
    """入力から作る候補(dynamic_items フック)を返す関数を作る。

    state は呼び出し側が持つ共有の dict。state["mode"] にいまのモードの
    予約文字、state["apps_expanded"] に `!` で未登録アプリを広げたかが入り、
    この関数は呼ぶたびに現在値を見る(モードが切り替わっても作り直さない)。

    file_search: func(文字列) -> パスの列(Everything。失敗時は空)
    """
    searches = command_palette.normalize_web_searches(web_searches or [])
    folder_entries = [e for e in entries
                      if e.kind == command_launcher.KIND_FOLDER]

    def app_row(cand):
        return (f"{ICONS['app']} {cand.title}",
                {"kind": "app", "path": cand.payload.get("path") or "",
                 "name": cand.title})

    def dynamics(text: str) -> list:
        mode = state.get("mode") or INITIAL_MODE
        raw = (text or "").strip()
        if mode == "!":
            if not state.get("apps_expanded"):
                # 一番下(dynamic_last で静的候補の後ろに付く)に常に出す。
                # 動的候補は (表示名, データ) の2要素が窓側との約束。
                return [(MORE_APPS_LABEL, {"kind": "more_apps"})]
            keyword = raw.lower()
            return [app_row(cand) for cand in apps
                    if picker_mod.name_matches(cand.title, keyword)]
        if not raw:
            return []
        if mode == "[":
            rows = []
            try:
                typed = launcher.typed_items(text)
            except Exception:  # noqa: BLE001  パスの解釈に失敗しても他は出す
                typed = []
            for label, data in typed:
                kind, path = data
                if kind == launcher.KIND_JUMP:
                    rows.append((label, {"kind": "jump", "path": path,
                                         "typed": True}))
            keyword = raw.lower()
            if not any(e.name.lower().startswith(keyword)
                       for e in folder_entries):
                # 前方一致が0件のときだけ、あいまい(部分列)一致を出す。
                # 対象は `[` の登録(フォルダ)だけ。
                scored = []
                for entry in folder_entries:
                    score = command_palette.score_match(raw, entry.name)
                    if score is not None:
                        scored.append(
                            (score, f"{_entry_icon(entry)} {entry.name}",
                             entry_payload(entry)))
                scored.sort(key=lambda row: -row[0])
                rows.extend((label, payload)
                            for _s, label, payload in scored[:5])
            return rows
        if mode == "?":
            rows = []
            if file_search is not None and len(raw) >= 2:
                try:
                    paths = file_search(raw) or []
                except Exception:  # noqa: BLE001  探せないことは候補が無いことと同じ
                    paths = []
                for path in paths[:5]:
                    rows.append(
                        (f"{ICONS['file']} {os.path.basename(path) or path}",
                         {"kind": "file", "path": path}))
            for cand in command_palette.build_web_candidates(searches, raw):
                rows.append((f"{ICONS['web']} {cand.title}",
                             {"kind": "web",
                              "url": cand.payload.get("url", ""),
                              "service": cand.payload.get("service", "")}))
            return rows
        return []

    return dynamics


def make_tab_text(state=None):
    """Tab の行き先。フォルダは掘り下げる。`?` では補完は効かない。

    state は make_mode_dynamics と同じ共有 dict(モードを見るため)。"""

    def tab_text(_name, data):
        if not isinstance(data, dict):
            return None
        if (state or {}).get("mode") == "?":
            return None
        kind = data.get("kind")
        if kind == "jump":
            return launcher.tab_text(_name, (launcher.KIND_JUMP,
                                             data.get("path") or ""))
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
        if entry.params:
            text += f" {entry.params}"
        return text
    if kind == "feature":
        return f"Enter で機能を呼び出します: {data.get('name') or ''}"
    if kind == "app":
        return f"Enter でアプリを起動します: {data.get('path') or ''}"
    if kind == "file":
        return f"Enter でファイルを開きます: {data.get('path') or ''}"
    if kind == "more_apps":
        return "Enter で未登録のアプリも一覧に加えます"
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
