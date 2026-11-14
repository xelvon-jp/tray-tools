# editor/ai_target.py
# 「選択範囲を AI に投げる」の投げ先 (プロファイル) の、Qt に
# 依存しない整え。設定の読み書きの形・カスタムプロファイルの
# 組み立て・窓の一覧の行 → 欄への写し、をここに置く (窓と設定
# 画面は editor/ai_target_dialog.py と editor/window.py)。
#
# プロファイルの形の正本は copilot_loop.BUILTIN_PROFILES。
# プロファイルは「窓の探し方とボタン名を1アプリぶんまとめた
# 辞書」で、copilot_loop.profiles(app_settings) が settings の
# copilot_profiles (辞書の並び) と組み込みを混ぜて返す。
# エディタの設定では、選択を editor 節の ai_target (プロファイル
# の name。空 = 自動) に、カスタムの辞書を editor 節の
# copilot_profiles (並び) に持つ。送信のときは、組み込みなら
# 名前を、カスタムなら辞書そのものを copilot_io へ渡す。
# Qt には依存しない (tests/test_editor_ai_target.py がそのまま叩く)。

# カスタムプロファイルの安定した名前 (設定の中で1つだけ持つ)。
CUSTOM_PROFILE_NAME = "eddy-custom"

# 自動選択を表す選択値 (プロファイル名が空)。
AUTO_SELECTION = ""

# テスト入力の文 (3行)。入力欄に入れるだけで、送信はしない。
# 改行がそのまま入るかも一緒に確かめる。
TEST_TEXT = (
    "EDDy からのテストです。\n"
    "これは送信されません。入力欄に入るかだけを確かめています。\n"
    "改行がそのまま入っていれば成功です。"
)

# 組み込みプロファイルの表示名 (アプリ名そのもの)。
_DISPLAY_NAMES = {
    "mscopilot": "Copilot",
    "m365": "Microsoft 365 Copilot",
}

# カスタムの初期値。窓の探し方以外の項目は、copilot_loop の
# 組み込みと桁をそろえた無難な値にしてある (送信ボタンの文言
# などは相手のアプリで違うので、実機で確かめて直す前提)。
_DEFAULT_RENDER_CLASS = "Chrome_RenderWidgetHostHWND"
_DEFAULT_SEND_BUTTONS = ["送信", "Send", "メッセージの送信"]
_DEFAULT_BUSY_BUTTONS = ["停止", "Stop", "生成を停止する"]
_DEFAULT_INPUT_BAND_PX = 170

# カスタムの編集欄に対応するキー (この並びで画面にも出す)。
FIELD_KEYS = (
    "label",
    "process_name",
    "window_class",
    "window_title_contains",
    "input_automation_id",
    "app_id_prefix",
)


def display_name(profile) -> str:
    """プロファイルの表示名。カスタムの label があればそれ、
    組み込みはアプリ名、どちらも無ければ name。"""
    if not isinstance(profile, dict):
        return ""
    label = profile.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    name = profile.get("name") or ""
    return _DISPLAY_NAMES.get(name, name)


def sanitize_selection(value) -> str:
    """保存された選択値を整える。文字列でなければ自動 ("")。"""
    return value if isinstance(value, str) else AUTO_SELECTION


def sanitize_profiles(value) -> list:
    """保存されたプロファイルの並びを整える。name を持つ辞書だけ残す。"""
    if not isinstance(value, list):
        return []
    return [dict(p) for p in value
            if isinstance(p, dict) and isinstance(p.get("name"), str)
            and p.get("name")]


def find_profile(profiles, name):
    """並びから name のプロファイルを探す。無ければ None。"""
    for profile in profiles or []:
        if isinstance(profile, dict) and profile.get("name") == name:
            return profile
    return None


def upsert_profile(profiles, profile) -> list:
    """同名を差し替えて (無ければ末尾へ足して) 新しい並びを返す。"""
    name = profile.get("name")
    result = [p for p in sanitize_profiles(profiles)
              if p.get("name") != name]
    result.append(dict(profile))
    return result


def fields_from_profile(profile) -> dict:
    """プロファイル辞書から、編集欄のぶんだけを取り出す。"""
    profile = profile if isinstance(profile, dict) else {}
    fields = {}
    for key in FIELD_KEYS:
        value = profile.get(key)
        fields[key] = value if isinstance(value, str) else ""
    if not fields["label"]:
        # label の無い組み込みなどを下書きにするときは、表示名を
        # 初期値にする (名前の手掛かりが無いと、どれのことか
        # 分からなくなるため)。
        name = profile.get("name") or ""
        if name and name != CUSTOM_PROFILE_NAME:
            fields["label"] = _DISPLAY_NAMES.get(name, name)
    aid = profile.get("input_automation_id")
    if isinstance(aid, list):
        # 組み込みには候補の並びで持つものがある (m365)。欄は
        # 1つなので、先頭を出す (保存するとその1つになる)。
        fields["input_automation_id"] = (
            str(aid[0]) if aid else "")
    return fields


def build_custom_profile(fields) -> dict:
    """編集欄の値から、カスタムプロファイルの辞書を組み立てる。

    形は copilot_loop.BUILTIN_PROFILES とそろえる。欄に無い項目は
    初期値 (上の _DEFAULT_*) を入れる。空でよい項目 (マーカー等)
    は空にしておく (当てずっぽうで埋めると、応答の切り出しが
    誤った位置で起きるため。組み込みの m365 と同じ判断)。"""
    fields = fields if isinstance(fields, dict) else {}

    def text(key):
        value = fields.get(key)
        return value.strip() if isinstance(value, str) else ""

    return {
        "name": CUSTOM_PROFILE_NAME,
        "label": text("label"),
        "process_name": text("process_name"),
        "window_class": text("window_class"),
        "window_title_contains": text("window_title_contains"),
        "app_id_prefix": text("app_id_prefix"),
        "render_class": _DEFAULT_RENDER_CLASS,
        "input_automation_id": text("input_automation_id"),
        "send_button": list(_DEFAULT_SEND_BUTTONS),
        "busy_button": list(_DEFAULT_BUSY_BUTTONS),
        "assistant_marker": "",
        "user_marker": "",
        "idle_marker": [],
        "code_copy_button": [],
        "input_band_px": _DEFAULT_INPUT_BAND_PX,
    }


def fields_from_window_row(row) -> dict:
    """窓の一覧の1行 {title, process_name, window_class} から、
    編集欄へ写すぶんを作る。

    タイトルは、そのまま入れると会話の題まで含んでしまうことが
    あるので、「 - 」で区切った最後の部分 (アプリ名のことが多い)
    を使う。区切りが無ければタイトル全体。どちらも60字で切る。"""
    row = row if isinstance(row, dict) else {}
    title = row.get("title") or ""
    title_contains = title.rsplit(" - ", 1)[-1].strip()[:60]
    return {
        "label": title_contains,
        "process_name": row.get("process_name") or "",
        "window_class": row.get("window_class") or "",
        "window_title_contains": title_contains,
    }
